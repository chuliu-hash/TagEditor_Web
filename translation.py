# -*- coding: utf-8 -*-
"""标签翻译模块。

翻译查询优先级：SQLite（danbooru_tags.db 的 cn_name 列）→ LLM（未命中时）→ 回写 SQLite。
已移除 translation_cache.json 缓存层，所有翻译持久化到 SQLite。
"""
import os
import json
from flask import Blueprint, request, jsonify, current_app, Response
from sse_utils import sse_event

translation_bp = Blueprint('translation', __name__)


# 本地 SQLite 连接（懒加载，进程级单例）。None 表示 DB 暂不可用。
# 使用 check_same_thread=False 允许 Flask 多请求（不同线程）复用同一连接。
# 本应用为单用户本地工具，写并发极低，SQLite 写锁足够保证一致性。
# 注意：_tag_db_available=False 只是「上一次检测失败」的缓存，不永久否定——
# 后续调用会重新检测（DB 可能刚被 build_tag_db.py init 创建）。
_tag_db_conn = None
_tag_db_available = None  # None=未检测, True/False=上一次检测结果


def _get_tag_db_conn():
    """懒加载 SQLite 连接。DB 不存在时返回 None，后续查询跳过 SQLite 层直接走 LLM。
    连接以 check_same_thread=False 打开，允许跨线程复用（Flask 每请求一线程）。
    不永久缓存「不可用」状态——每次调用都重新检查 DB 是否已出现（build_tag_db.py 可能刚建好）。"""
    global _tag_db_conn, _tag_db_available
    # 已有可用连接则复用
    if _tag_db_conn is not None:
        return _tag_db_conn
    try:
        import sqlite3
        from build_tag_db import SCHEMA, _ensure_fts_index, _rebuild_fts_index, _table_exists, _migrate_to_target_schema
        from config import get_tag_db_config
        db_path = get_tag_db_config()['db_path']

        # sqlite3.connect 会自动创建不存在的文件，所以不需要提前检查 os.path.isfile
        # check_same_thread=False 允许跨线程；busy_timeout 让写锁竞争时等待而非立即报错；
        # journal_mode=WAL 允许「增量更新写」与本连接「读」并发不阻塞
        _tag_db_conn = sqlite3.connect(db_path, check_same_thread=False, timeout=5)
        _tag_db_conn.row_factory = sqlite3.Row
        _tag_db_conn.execute('PRAGMA busy_timeout = 5000')
        _tag_db_conn.execute('PRAGMA journal_mode = WAL')
        _tag_db_conn.executescript(SCHEMA)
        # 旧库兼容：列集合与目标不符就迁移
        if _migrate_to_target_schema(_tag_db_conn):
            _tag_db_conn.commit()
        # FTS5 全文索引（search_tags 加速）：建表 + 触发器；空则补数据
        _ensure_fts_index(_tag_db_conn)
        fts_count = _tag_db_conn.execute("SELECT count(*) FROM tags_fts").fetchone()[0]
        tag_count = _tag_db_conn.execute("SELECT count(*) FROM tags").fetchone()[0]
        if tag_count > 0 and fts_count == 0:
            _rebuild_fts_index(_tag_db_conn)
            _tag_db_conn.commit()
        _tag_db_available = True
        return _tag_db_conn
    except Exception:
        import traceback
        traceback.print_exc()
        _tag_db_conn = None
        _tag_db_available = False
        return None


def _lookup_cn_from_db(tags):
    """从 SQLite 批量查标签的中文翻译（en→zh）。返回 {tag: cn_name_first}，key 为原始 tag（带空格）。
    cn_name 可能是逗号分隔的多词（"蓝发,蓝色头发"），取第一项作主翻译。

    注意 key 一致性：lookup_tags 返回的 dict key 是 DB 里的下划线形式（name 列存的是 on_bed），
    但调用方用原始 tag（on bed）做 hits.get(tag) 查找。这里必须用原始 tag 作 key，
    否则带空格的标签（on bed / bed sheet / 角色名等）全部查不到 → 翻译显示为空。"""
    conn = _get_tag_db_conn()
    if conn is None or not tags:
        return {}
    try:
        from build_tag_db import lookup_tags
        rows = lookup_tags(conn, tags)  # 返回 {normalized_name: info}
        result = {}
        for tag in tags:  # 用原始 tag 作 key，保证下游 hits.get(tag) 命中
            norm = tag.strip().replace(' ', '_').lower()  # 与 lookup_tags 内部规范化一致
            info = rows.get(norm)
            if info:
                cn = (info.get('cn_name') or '').strip()
                if cn:
                    result[tag] = cn.split(',')[0].strip()
        return result
    except Exception:
        return {}


def _lookup_en_from_db(cn_names):
    """从 SQLite 反向查（zh→en）：中文翻译 → 英文标签名。返回 {cn: en_name}。"""
    conn = _get_tag_db_conn()
    if conn is None or not cn_names:
        return {}
    try:
        from build_tag_db import lookup_tag_by_cn
        result = {}
        for cn in cn_names:
            en = lookup_tag_by_cn(conn, cn)
            if en:
                result[cn] = en
        return result
    except Exception:
        return {}


import threading as _threading


@translation_bp.route('/lookup_cache', methods=['POST'])
def lookup_cache():
    """查标签翻译（保留原路由名兼容前端）。直接查 SQLite。
    支持双向：前端传 tags + 可选 src/dst（默认 en→zh）。"""
    data = request.get_json()
    tags = data.get('tags', [])
    src = data.get('src', 'en')
    dst = data.get('dst', 'zh')
    if not tags:
        return jsonify({'translations': []})

    if src == 'en' and dst == 'zh':
        hits = _lookup_cn_from_db(tags)
        translations = [hits.get(t, '') for t in tags]
    elif src == 'zh' and dst == 'en':
        hits = _lookup_en_from_db(tags)
        translations = [hits.get(t, '') for t in tags]
    else:
        translations = [''] * len(tags)

    return jsonify({'translations': translations})






# ---------------------------------------------------------------------------
# 批量翻译数据库标签（标签名 / Wiki）
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 管线操作：同步标签库 / 爬取标签组 / LLM 深度翻译
# ---------------------------------------------------------------------------

@translation_bp.route('/sync_tags_db', methods=['POST'])
def sync_tags_db():
    """从上游 GitHub 同步新标签到本地数据库（SSE 流式）。
    下载 tag.sqlite，筛选 post_count≥100 且 category∈{0,3,4} 的新标签写入。"""
    from config import get_tag_db_config
    from sync_tags import _download_sqlite
    from pathlib import Path
    db_path = get_tag_db_config()['db_path']

    def generate():
        # 客户端断开时自动取消
        try:
            yield from _generate()
        except GeneratorExit:
            raise

    def _generate():
        sqlite_path = str(Path(db_path).parent / 'raw' / 'tag.sqlite')
        cancel_evt = _register_cancel("sync_tags_db")
        try:
            yield sse_event('progress', {'current': 0, 'total': 5, 'item': '准备同步...'})
            if cancel_evt.is_set():
                yield sse_event('cancelled', {'new_count': 0, 'update_count': 0, 'message': '已取消'})
                return
            Path(sqlite_path).parent.mkdir(parents=True, exist_ok=True)

            yield sse_event('progress', {'current': 1, 'total': 5, 'item': '下载最新 tag.sqlite...'})
            _dl_cancelled = {'flag': False}
            def _dl_cancel_check():
                if cancel_evt.is_set():
                    _dl_cancelled['flag'] = True
                    return True
                return False
            ok = _download_sqlite(sqlite_path, cancel_check=_dl_cancel_check)
            if _dl_cancelled['flag']:
                yield sse_event('cancelled', {'new_count': 0, 'update_count': 0, 'message': '已取消'})
                return
            if not ok:
                yield sse_event('fatal', {'error': '下载 tag.sqlite 失败'})
                return

            yield sse_event('progress', {'current': 2, 'total': 5, 'item': '读取上游数据...'})
            import sqlite3
            up_conn = sqlite3.connect(sqlite_path)
            up_conn.row_factory = sqlite3.Row
            try:
                rows = up_conn.execute(
                    "SELECT name, category, cn_name, post_count FROM tags"
                ).fetchall()
            except Exception as e:
                up_conn.close()
                yield sse_event('fatal', {'error': f'读取上游数据失败: {e}'})
                return
            up_conn.close()
            if cancel_evt.is_set():
                yield sse_event('cancelled', {'new_count': 0, 'update_count': 0, 'message': '已取消'})
                return
            yield sse_event('progress', {'current': 3, 'total': 5, 'item': f'上游共 {len(rows)} 条标签，同步到本地...'})

            conn = _get_tag_db_conn()
            if conn is None:
                yield sse_event('fatal', {'error': '本地数据库未配置'})
                return

            if cancel_evt.is_set():
                yield sse_event('cancelled', {'new_count': 0, 'update_count': 0, 'message': '已取消'})
                return
            local_names = {r[0] for r in conn.execute("SELECT name FROM tags").fetchall()}
            new_tags = []
            for r in rows:
                name = r['name']
                cat = int(r['category']) if r['category'] is not None else -1
                pc = int(r['post_count']) if r['post_count'] is not None else 0
                cn = (r['cn_name'] or '').strip()
                if pc >= 100 and cat in (0, 3, 4) and name not in local_names:
                    new_tags.append((name, cn, cat, pc))

            if cancel_evt.is_set():
                yield sse_event('cancelled', {'new_count': 0, 'update_count': 0, 'message': '已取消'})
                return
            if new_tags:
                conn.execute("BEGIN")
                for name, cn, cat, pc in new_tags:
                    conn.execute(
                        "INSERT OR IGNORE INTO tags (name, cn_name, category, post_count) VALUES (?, ?, ?, ?)",
                        (name, cn, cat, pc)
                    )
                conn.commit()
                print(f'[sync_tags_db] 新增 {len(new_tags)} 个标签')
                yield sse_event('progress', {'current': 4, 'total': 5, 'item': f'已写入 {len(new_tags)} 个新标签'})
            else:
                print('[sync_tags_db] 无新标签')
                yield sse_event('progress', {'current': 4, 'total': 5, 'item': '无新标签需要写入'})

            if cancel_evt.is_set():
                yield sse_event('cancelled', {'new_count': len(new_tags), 'update_count': 0, 'message': '已取消'})
                return
            up_map = {r['name']: r for r in rows}
            update_count = 0
            for name in local_names:
                if name in up_map:
                    r = up_map[name]
                    cat = int(r['category']) if r['category'] is not None else -1
                    pc = int(r['post_count']) if r['post_count'] is not None else 0
                    conn.execute(
                        "UPDATE tags SET category = CASE WHEN ? >= 0 THEN ? ELSE category END, "
                        "post_count = CASE WHEN ? > 0 THEN ? ELSE post_count END WHERE name = ?",
                        (cat, cat, pc, pc, name)
                    )
                    update_count += 1
            conn.commit()
            print(f'[sync_tags_db] 完成: 新增 {len(new_tags)} 条, 更新 {update_count} 条')
            yield sse_event('progress', {'current': 5, 'total': 5, 'item': f'已更新 {update_count} 条已有标签'})
            yield sse_event('complete', {'new_count': len(new_tags), 'update_count': update_count})
        except Exception as e:
            print(f'[sync_tags_db] 异常终止: {e}')
            yield sse_event('fatal', {'error': f'同步异常终止: {e}'})
        finally:
            _unregister_cancel("sync_tags_db")
            if os.path.exists(sqlite_path):
                os.remove(sqlite_path)

    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@translation_bp.route('/crawl_tag_groups', methods=['POST'])
def crawl_tag_groups():
    """爬取 Danbooru 标签组体系（SSE 流式）。"""
    from config import get_tag_db_config
    from tag_groups import run as groups_run
    db_path = get_tag_db_config()['db_path']

    def generate():
        try:
            yield from _generate()
        except GeneratorExit:
            raise

    def _generate():
        cancel_evt = _register_cancel("crawl_tag_groups")
        try:
            yield sse_event('progress', {'current': 0, 'total': '?', 'item': '开始爬取标签组...'})
            import threading
            import time as _time

            events = []
            worker_failed = False

            def cb(event):
                events.append(event)

            def worker():
                nonlocal worker_failed
                try:
                    groups_run(db_path=db_path, progress_callback=cb,
                               cancel_check=cancel_evt.is_set)
                except Exception as e:
                    worker_failed = True
                    cb({'type': 'fatal', 'error': str(e)})

            t = threading.Thread(target=worker, daemon=True)
            t.start()

            last_sent = 0
            finished = False
            while t.is_alive() or last_sent < len(events):
                while last_sent < len(events):
                    evt = events[last_sent]
                    last_sent += 1
                    etype = evt.get('type')
                    if etype == 'progress':
                        yield sse_event('progress', {
                            'current': evt.get('page', last_sent),
                            'total': evt.get('total', '?'),
                            'item': evt.get('item', '')
                        })
                    elif etype == 'complete':
                        yield sse_event('complete', {'message': evt.get('item', '标签组爬取完成')})
                        finished = True
                        break
                    elif etype == 'fatal':
                        yield sse_event('fatal', {'error': evt.get('error', '爬取过程出错')})
                        finished = True
                        break
                    elif etype == 'cancelled':
                        yield sse_event('cancelled', {
                            'message': '已取消',
                            'item': evt.get('item', ''),
                            'new_count': evt.get('new_count', 0)
                        })
                        finished = True
                        break
                if finished:
                    break
                if t.is_alive():
                    _time.sleep(0.3)

            # 重新加载缓存
            global _tag_groups_cache
            _tag_groups_cache = None

            if not finished:
                if worker_failed:
                    yield sse_event('fatal', {'error': '爬取过程出错，详情见日志'})
                else:
                    yield sse_event('complete', {'message': '标签组爬取完成'})
        except GeneratorExit:
            cancel_evt.set()
            raise
        except Exception as e:
            print(f'[crawl_tag_groups] 异常终止: {e}')
            yield sse_event('fatal', {'error': f'爬取异常终止: {e}'})
        finally:
            _unregister_cancel("crawl_tag_groups")

    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@translation_bp.route('/llm_process_db', methods=['POST'])
def llm_process_db():
    """LLM 三层深度翻译管线（SSE 流式）。
    处理数据库中所有标签，生成中文描述/扩展中文名/NSFW 判定。
    body: {reprocess: bool} — 是否重新处理已处理的标签（默认 false）"""
    data = request.get_json() or {}
    reprocess = data.get('reprocess', False)

    from config import get_tag_db_config
    db_path = get_tag_db_config()['db_path']

    cancel_evt = _register_cancel("llm_process_db")

    def generate():
        try:
            yield from _generate()
        except GeneratorExit:
            cancel_evt.set()
            print('[LLM 翻译] 前端中断连接，已设置取消信号')
            raise
        except Exception as e:
            print(f'[LLM 翻译] 致命错误: {e}')
            import traceback
            traceback.print_exc()
            yield sse_event('fatal', {'error': f'翻译管线异常: {str(e)}'})
        finally:
            _unregister_cancel("llm_process_db")

    def _generate():
        import time
        try:
            yield sse_event('progress', {'current': 0, 'total': 5, 'item': '准备 LLM 深度翻译...'})
            print('[LLM 翻译] 准备 LLM 深度翻译...')

            api_key = os.environ.get('LLM_API_KEY', '')
            base_url = os.environ.get('LLM_API_URL', '')
            model = os.environ.get('LLM_MODEL', 'default')
            if not api_key:
                print('[LLM 翻译] 错误: 未配置 LLM_API_KEY')
                yield sse_event('fatal', {'error': '未配置 LLM_API_KEY'})
                return

            if cancel_evt.is_set():
                print('[LLM 翻译] 已取消')
                yield sse_event('cancelled', {'translated': 0})
                return

            from openai import OpenAI
            client = OpenAI(base_url=base_url, api_key=api_key)

            conn = _get_tag_db_conn()
            if conn is None:
                print('[LLM 翻译] 错误: 标签数据库未配置')
                yield sse_event('fatal', {'error': '标签数据库未配置'})
                return

            yield sse_event('progress', {'current': 1, 'total': 5, 'item': '加载标签数据...'})
            import llm_pipeline as lp
            tags = lp._load_tags(conn)
            history = lp._load_history(db_path)
            tag_to_groups, group_cn_names = lp._load_tag_groups(db_path)
            cooc_data = lp._load_cooc_data(db_path)

            # 分类
            entity_tags = []
            general_tags = []
            fallback_tags = []

            for tag in tags:
                name = tag['name']
                if name in history and not reprocess:
                    continue
                cat = int(tag.get('category', -1))
                has_wiki = bool(tag.get('en_wiki', '').strip())
                if cat in (3, 4):
                    entity_tags.append(tag)
                elif has_wiki:
                    general_tags.append(tag)
                else:
                    fallback_tags.append(tag)

            total = len(entity_tags) + len(general_tags) + len(fallback_tags)
            if total == 0:
                print('[LLM 翻译] 所有标签已处理，无需深度翻译')
                yield sse_event('complete', {'message': '所有标签已处理，无需深度翻译', 'translated': 0})
                return

            print(f'[LLM 翻译] 待处理 {total} 条（实体 {len(entity_tags)} / 常规 {len(general_tags)} / 兜底 {len(fallback_tags)}）')
            yield sse_event('progress', {'current': 2, 'total': 5,
                'item': f'待处理 {total} 条（实体 {len(entity_tags)} / 常规 {len(general_tags)} / 兜底 {len(fallback_tags)}）'})

            batch_size = 32
            current_run = set()
            done = 0

            banner_msg = None  # 中断时标记，避免 final complete/cancelled 冲突

            # ── Entity ──
            if entity_tags and not cancel_evt.is_set():
                print(f'[LLM 翻译] 开始实体层，共 {len(entity_tags)} 条')
                for i in range(0, len(entity_tags), batch_size):
                    if cancel_evt.is_set():
                        banner_msg = 'cancelled'
                        print('[LLM 翻译] 实体层被中断')
                        break
                    batch = entity_tags[i:i + batch_size]
                    payload = lp._build_entity_payloads_batch(batch, tag_to_groups, group_cn_names,
                                                             os.environ.get('BANGUMI_ACCESS_TOKEN', ''),
                                                             cooc_data)
                    try:
                        results = lp._call_llm(client, model, lp._SYSTEM_PROMPT_ENTITY, payload, temperature=0.1)
                    except Exception as e:
                        print(f'[LLM 翻译] 实体批处理失败: {e}')
                        yield sse_event('error', {'item': f'实体批 {i}-{i + len(batch)}', 'error': str(e)})
                        continue
                    lp._apply_results(conn, results)
                    current_run.update(item["name"] for item in results if item.get("name"))
                    done += len(batch)
                    # 每批保存历史，支持断点续传和中途恢复
                    lp._save_history(db_path, history | current_run)
                    print(f'[LLM 翻译] 实体 {i + len(batch)}/{len(entity_tags)}')
                    yield sse_event('progress', {'current': done, 'total': total, 'item': f'实体 {i + len(batch)}/{len(entity_tags)}'})
                    time.sleep(0.5)

            # ── General ──
            if general_tags and not cancel_evt.is_set():
                print(f'[LLM 翻译] 开始常规层，共 {len(general_tags)} 条')
                for i in range(0, len(general_tags), batch_size):
                    if cancel_evt.is_set():
                        banner_msg = 'cancelled'
                        print('[LLM 翻译] 常规层被中断')
                        break
                    batch = general_tags[i:i + batch_size]
                    payload = [lp._build_general_payload(t, tag_to_groups, group_cn_names, cooc_data)
                               for t in batch]
                    try:
                        results = lp._call_llm(client, model, lp._SYSTEM_PROMPT_GENERAL, payload, temperature=0.4)
                    except Exception as e:
                        print(f'[LLM 翻译] 常规批处理失败: {e}')
                        yield sse_event('error', {'item': f'常规批 {i}-{i + len(batch)}', 'error': str(e)})
                        continue
                    lp._apply_results(conn, results)
                    current_run.update(item["name"] for item in results if item.get("name"))
                    done += len(batch)
                    # 每批保存历史
                    lp._save_history(db_path, history | current_run)
                    print(f'[LLM 翻译] 常规 {i + len(batch)}/{len(general_tags)}')
                    yield sse_event('progress', {'current': done, 'total': total, 'item': f'常规 {i + len(batch)}/{len(general_tags)}'})
                    time.sleep(0.5)

            # ── Fallback ──
            if fallback_tags and not cancel_evt.is_set():
                print(f'[LLM 翻译] 开始兜底层，共 {len(fallback_tags)} 条')
                for i in range(0, len(fallback_tags), batch_size):
                    if cancel_evt.is_set():
                        banner_msg = 'cancelled'
                        print('[LLM 翻译] 兜底层被中断')
                        break
                    batch = fallback_tags[i:i + batch_size]
                    payload = [lp._build_general_payload(t, tag_to_groups, group_cn_names, cooc_data)
                               for t in batch]
                    try:
                        results = lp._call_llm(client, model, lp._SYSTEM_PROMPT_FALLBACK, payload, temperature=0.5)
                    except Exception as e:
                        print(f'[LLM 翻译] 兜底批处理失败: {e}')
                        yield sse_event('error', {'item': f'兜底批 {i}-{i + len(batch)}', 'error': str(e)})
                        continue
                    current_run.update(item["name"] for item in results if item.get("name"))
                    done += len(batch)
                    # 每批保存历史
                    lp._save_history(db_path, history | current_run)
                    print(f'[LLM 翻译] 兜底 {i + len(batch)}/{len(fallback_tags)}')
                    yield sse_event('progress', {'current': done, 'total': total, 'item': f'兜底 {i + len(batch)}/{len(fallback_tags)}'})
                    time.sleep(0.5)

            # ── 保存历史 ──
            if current_run:
                history.update(current_run)
                lp._save_history(db_path, history)

            if banner_msg == 'cancelled':
                print(f'[LLM 翻译] 已中断，已处理 {len(current_run)} 条')
                yield sse_event('cancelled', {'translated': len(current_run), 'message': f'已中断，已处理 {len(current_run)} 条'})
            else:
                print(f'[LLM 翻译] 完成: 共处理 {len(current_run)}/{total} 条')
                yield sse_event('complete', {'translated': len(current_run), 'total': total,
                                             'message': f'LLM 深度翻译完成：{len(current_run)} 条'})
        except Exception as e:
            print(f'[LLM 翻译] 异常终止: {e}')
            yield sse_event('fatal', {'error': f'LLM 深度翻译异常终止: {e}'})
            return

    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


# ---------------------------------------------------------------------------
# 标签详情（wiki 展示 + 翻译）
# ---------------------------------------------------------------------------

@translation_bp.route('/tag_detail/<path:tag>')
def tag_detail(tag):
    """返回单个标签的完整信息（cn_name/en_wiki/cn_wiki/other_names/nsfw/cn_name_locked/cn_wiki_locked/tag_groups）"""
    conn = _get_tag_db_conn()
    if conn is None:
        return jsonify({'error': '标签数据库未配置'}), 500
    try:
        from build_tag_db import lookup_tags
        rows = lookup_tags(conn, [tag])
        norm = tag.strip().replace(' ', '_').lower()
        if norm not in rows:
            return jsonify({'tag': tag, 'cn_name': '', 'en_wiki': '', 'cn_wiki': '',
                           'other_names': '[]', 'nsfw': 0, 'cn_name_locked': 0, 'cn_wiki_locked': 0})
        info = rows[norm]
        # 补充 tag_groups
        info['tag_groups'] = _get_tag_groups_for(norm)
        return jsonify({'tag': tag, **info})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── 标签组缓存 ─────────────────────────────────────────────────────────────
_tag_groups_cache = None


def _load_tag_groups_cache():
    """加载 tag_groups.json 到缓存。"""
    global _tag_groups_cache
    if _tag_groups_cache is not None:
        return _tag_groups_cache
    import os
    root = current_app.root_path
    tg_path = os.path.join(root, 'data', 'tag_groups.json')
    try:
        with open(tg_path, 'r', encoding='utf-8') as f:
            _tag_groups_cache = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        _tag_groups_cache = {'tag_to_groups': {}, 'group_to_tags': {}, 'group_cn_names': {}}
    return _tag_groups_cache


def _get_tag_groups_for(tag_name: str) -> list[dict]:
    """返回标签所属的分组列表。"""
    tg = _load_tag_groups_cache()
    groups = tg.get('tag_to_groups', {}).get(tag_name, [])
    cn_names = tg.get('group_cn_names', {})
    return [{'id': g, 'cn_name': cn_names.get(g, '')} for g in groups]


@translation_bp.route('/tag_group_tags/<path:group_id>')
def tag_group_tags(group_id):
    """返回指定分组下的所有标签列表。"""
    tg = _load_tag_groups_cache()
    tags = tg.get('group_to_tags', {}).get(group_id, [])
    cn_name = tg.get('group_cn_names', {}).get(group_id, '')
    # 去重排序
    tags = sorted(set(tags))
    display = cn_name or group_id.replace('tag_group:', '')
    return jsonify({'group_id': group_id, 'cn_name': cn_name,
                    'display': display, 'tags': tags, 'count': len(tags)})


@translation_bp.route('/tag_cooc/<path:tag>')
def tag_cooc(tag):
    """返回标签的共现推荐标签列表。"""
    norm = tag.strip().replace(' ', '_').lower()
    cooc_dir = os.path.join(current_app.root_path, 'data', 'cooc')
    cooc_path = os.path.join(cooc_dir, 'cooccurrence_clean.parquet')
    if not os.path.exists(cooc_path):
        return jsonify({'cooc': []})
    try:
        import pandas as pd
        df = pd.read_parquet(cooc_path)
        # tag_a tag_b count
        related = df[(df['tag_a'] == norm) | (df['tag_b'] == norm)].copy()
        related['related'] = related.apply(
            lambda r: r['tag_b'] if r['tag_a'] == norm else r['tag_a'], axis=1)
        related = related.sort_values('count', ascending=False).head(20)
        return jsonify({'cooc': related[['related', 'count']].to_dict(orient='records')})
    except Exception as e:
        return jsonify({'error': str(e)}), 500




@translation_bp.route('/update_tag_wiki', methods=['POST'])
def update_tag_wiki():
    """手动编辑并保存标签的中文 wiki。body: {tag, lang, content}。
    lang: 'zh' → cn_wiki。en_wiki 手动编辑已禁用（仅 Danbooru 增量更新可改）。
    受 cn_wiki_locked 守卫：中文 wiki 锁定后跳过更新。"""
    data = request.get_json() or {}
    tag = (data.get('tag') or '').strip()
    lang = (data.get('lang') or '').strip().lower()
    content = data.get('content')
    if content is None:
        content = ''
    if not tag:
        return jsonify({'error': '缺少 tag'}), 400
    if lang == 'en':
        return jsonify({'error': '英文 wiki 不支持手动编辑'}), 400
    if lang != 'zh':
        return jsonify({'error': 'lang 必须为 zh'}), 400

    conn = _get_tag_db_conn()
    if conn is None:
        return jsonify({'error': '标签数据库未配置'}), 500
    try:
        from build_tag_db import update_cn_wiki
        update_cn_wiki(conn, tag, content)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    return jsonify({'ok': True, 'lang': lang, 'content': content})


# ---------------------------------------------------------------------------
# 手动编辑标签中文名
# ---------------------------------------------------------------------------

@translation_bp.route('/update_cn_name', methods=['POST'])
def update_cn_name():
    """手动编辑并保存单个标签的中文名（cn_name）。body: {tag, cn_name}。"""
    data = request.get_json() or {}
    tag = (data.get('tag') or '').strip()
    cn_name = data.get('cn_name', '')
    if cn_name is None:
        cn_name = ''
    if not tag:
        return jsonify({'error': '缺少 tag'}), 400

    conn = _get_tag_db_conn()
    if conn is None:
        return jsonify({'error': '标签数据库未配置'}), 500
    try:
        from build_tag_db import update_translation
        update_translation(conn, tag, cn_name)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    return jsonify({'ok': True, 'cn_name': cn_name})


# ---------------------------------------------------------------------------
# 锁定/解锁标签中文名
# ---------------------------------------------------------------------------

@translation_bp.route('/toggle_cn_lock', methods=['POST'])
def toggle_cn_lock():
    """切换标签中文名或中文 wiki 的锁定状态。body: {tag, field}。
    field: 'name' → cn_name_locked；'wiki' → cn_wiki_locked。
    锁定后对应字段不能被深度翻译或手动编辑覆盖。"""
    data = request.get_json() or {}
    tag = (data.get('tag') or '').strip()
    field = (data.get('field') or '').strip()
    if not tag:
        return jsonify({'error': '缺少 tag'}), 400
    if field not in ('name', 'wiki'):
        return jsonify({'error': 'field 必须为 name 或 wiki'}), 400

    col = 'cn_name_locked' if field == 'name' else 'cn_wiki_locked'

    conn = _get_tag_db_conn()
    if conn is None:
        return jsonify({'error': '标签数据库未配置'}), 500

    norm = tag.strip().replace(' ', '_').lower()
    row = conn.execute(f"SELECT {col} FROM tags WHERE name = ?", (norm,)).fetchone()
    if not row:
        return jsonify({'error': f'标签 {tag} 不存在'}), 404
    new_val = 0 if row[0] else 1
    conn.execute(f"UPDATE tags SET {col} = ? WHERE name = ?", (new_val, norm))
    conn.commit()
    return jsonify({'ok': True, 'cn_locked': new_val, 'field': field})


# ---------------------------------------------------------------------------
# 单标签深度翻译
# ---------------------------------------------------------------------------

@translation_bp.route('/translate_single_tag', methods=['POST'])
def translate_single_tag():
    """深度翻译单个标签（使用 llm_pipeline 逻辑）。
    body: {tag}
    返回 {cn_name, cn_wiki, nsfw}"""
    data = request.get_json() or {}
    tag = (data.get('tag') or '').strip()
    if not tag:
        return jsonify({'error': '缺少 tag'}), 400

    conn = _get_tag_db_conn()
    if conn is None:
        return jsonify({'error': '标签数据库未配置'}), 500

    from build_tag_db import lookup_tags
    norm = tag.strip().replace(' ', '_').lower()
    info = lookup_tags(conn, [tag]).get(norm)
    if not info:
        return jsonify({'error': f'标签 {tag} 不在数据库中'}), 404

    # 加载 tag_groups 和共现数据
    from llm_pipeline import (
        _build_entity_payload, _build_general_payload,
        _SYSTEM_PROMPT_ENTITY, _SYSTEM_PROMPT_GENERAL, _SYSTEM_PROMPT_FALLBACK,
        _call_llm, _apply_results, _load_tag_groups, _load_cooc_data,
    )
    from config import get_tag_db_config
    db_path = get_tag_db_config()['db_path']
    tag_to_groups, group_cn_names = _load_tag_groups(db_path)
    cooc_data = _load_cooc_data(db_path)

    # 构造 tag_data（与 _load_tags 返回结构一致）
    tag_data = {
        'name': norm,
        'cn_name': info.get('cn_name', ''),
        'en_wiki': info.get('en_wiki', ''),
        'category': int(info.get('category', -1)),
        'other_names': info.get('other_names', '[]'),
    }

    # 确定层级
    cat = int(info.get('category', -1))
    has_wiki = bool(info.get('en_wiki', '').strip())
    if cat in (3, 4):
        payload = [_build_entity_payload(
            tag_data, tag_to_groups, group_cn_names,
            os.environ.get('BANGUMI_ACCESS_TOKEN', ''),
            cooc_data
        )]
        system_prompt = _SYSTEM_PROMPT_ENTITY
        temperature = 0.1
    elif has_wiki:
        payload = [_build_general_payload(tag_data, tag_to_groups, group_cn_names, cooc_data)]
        system_prompt = _SYSTEM_PROMPT_GENERAL
        temperature = 0.4
    else:
        payload = [_build_general_payload(tag_data, tag_to_groups, group_cn_names, cooc_data)]
        system_prompt = _SYSTEM_PROMPT_FALLBACK
        temperature = 0.5

    # LLM 调用
    from openai import OpenAI
    api_key = os.environ.get('LLM_API_KEY', '')
    base_url = os.environ.get('LLM_API_URL', '')
    model = os.environ.get('LLM_MODEL', 'default')
    if not api_key:
        return jsonify({'error': '未配置 LLM_API_KEY'}), 400
    client = OpenAI(base_url=base_url, api_key=api_key)

    results = _call_llm(client, model, system_prompt, payload, temperature=temperature)
    _apply_results(conn, results)

    # 重新查最新结果
    conn.commit()
    from build_tag_db import lookup_tags
    updated = lookup_tags(conn, [tag]).get(norm, {})
    return jsonify({
        'cn_name': updated.get('cn_name', ''),
        'cn_wiki': updated.get('cn_wiki', ''),
        'nsfw': updated.get('nsfw', 0),
    })


# ---------------------------------------------------------------------------
# Danbooru 标签查询页面专用
# ---------------------------------------------------------------------------

@translation_bp.route('/danbooru_search', methods=['POST'])
def danbooru_search():
    """模糊搜索标签库。body: {keyword, limit=20}"""
    data = request.get_json() or {}
    keyword = (data.get('keyword') or '').strip()
    limit = min(int(data.get('limit', 20)), 500)  # 上限 500，防止单次返回过多拖慢传输/渲染
    if not keyword:
        return jsonify({'results': []})

    conn = _get_tag_db_conn()
    if conn is None:
        return jsonify({'error': '标签数据库未配置'}), 500
    try:
        from build_tag_db import search_tags
        results = search_tags(conn, keyword, limit)
        return jsonify({'results': results})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@translation_bp.route('/danbooru_random', methods=['GET'])
def danbooru_random():
    """返回随机 N 条标签（含 cn_name），用于首页推荐展示。"""
    n = request.args.get('n', 50, type=int)
    n = min(max(n, 1), 200)
    conn = _get_tag_db_conn()
    if conn is None:
        return jsonify({'tags': []})
    rows = conn.execute(
        "SELECT name, cn_name, category, post_count FROM tags WHERE cn_name != '' AND cn_name IS NOT NULL "
        "ORDER BY RANDOM() LIMIT ?", (n,)
    ).fetchall()
    return jsonify({'tags': [dict(r) for r in rows]})


@translation_bp.route('/danbooru_update', methods=['POST'])
def danbooru_update():
    """触发增量更新（SSE 流式）。包装 update_from_danbooru 的 progress_callback 为 SSE 事件。"""
    from config import get_tag_db_config
    from build_tag_db import update_from_danbooru
    db_path = get_tag_db_config()['db_path']

    def generate():
        # worker 在后台线程跑 update_from_danbooru，通过 cb 回调把事件追加到 events；
        # 主线程（SSE generator）轮询 events 顺序 yield 为 SSE。
        import threading
        import time as _time
        cancel_evt = _register_cancel("danbooru_update")
        events = []

        def cb(event):
            events.append(event)

        def worker():
            try:
                update_from_danbooru(
                    db_path, verbose=False, progress_callback=cb,
                    cancel_check=cancel_evt.is_set
                )
            except Exception as e:
                cb({'type': 'error', 'message': str(e)})

        t = threading.Thread(target=worker, daemon=True)
        t.start()

        last_sent = 0
        finished = False  # 是否已发送结束事件（complete/fatal/cancelled），避免 worker 异常后再补发 complete
        while t.is_alive() or last_sent < len(events):
            while last_sent < len(events):
                evt = events[last_sent]
                last_sent += 1
                etype = evt.get('type')
                if etype == 'progress':
                    yield sse_event('progress', {
                        'current': evt['page'],
                        'total': '?',  # 总页数未知（取决于增量数据量）
                        'item': f"第 {evt['page']} 页（已新增 {evt['new_count']} 条）"
                    })
                elif etype == 'error':
                    yield sse_event('fatal', {'error': evt['message']})
                    finished = True
                    break
                elif etype == 'cancelled':
                    # 用户中断：已爬数据已落库，断点已保存。前端据此停止读取流。
                    yield sse_event('cancelled', {'new_count': evt['new_count']})
                    finished = True
                    break
                elif etype == 'complete':
                    yield sse_event('complete', {'new_count': evt['new_count']})
                    finished = True
                    break
            if finished:
                break
            if t.is_alive():
                # 等待新事件，用短 sleep 避免忙等
                _time.sleep(0.5)

        # 线程结束但没收到 complete/error/cancelled 事件（worker 未捕获异常退出兜底）
        if not finished:
            yield sse_event('fatal', {'error': '增量更新异常终止（未收到完成事件）'})

        _unregister_cancel("danbooru_update")

    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


# 取消事件注册表：各 SSE 路线创建各自的 Event，/danbooru_cancel 统一取消所有活跃操作。
import threading as _threading
_active_cancel_events: dict[str, _threading.Event] = {}
_cancel_events_lock = _threading.Lock()

def _register_cancel(name: str) -> _threading.Event:
    """注册一个操作名到取消事件，返回新建的 Event。每个 SSE 路线各自注册。"""
    evt = _threading.Event()
    with _cancel_events_lock:
        _active_cancel_events[name] = evt
    return evt

def _unregister_cancel(name: str):
    """操作完成/取消后注销。"""
    with _cancel_events_lock:
        _active_cancel_events.pop(name, None)

def _cancel_all():
    """设置所有活跃的取消事件（前端一键取消）。"""
    with _cancel_events_lock:
        events = list(_active_cancel_events.values())
    for e in events:
        e.set()


# ---------------------------------------------------------------------------
# 共现数据更新
# ---------------------------------------------------------------------------

@translation_bp.route('/fetch_cooc', methods=['POST'])
def fetch_cooc():
    """增量抓取标签共现数据（SSE 流式）。只抓新标签的共现。"""
    from config import get_tag_db_config
    db_path = get_tag_db_config()['db_path']

    def generate():
        try:
            yield from _generate()
        except GeneratorExit:
            raise

    def _generate():
        cancel_evt = _register_cancel("fetch_cooc")
        try:
            yield sse_event('progress', {'current': 0, 'total': '?', 'item': '开始增量抓取共现...'})
            from cooc_pipeline import run_fetch_cooc
            import threading
            import time as _time

            events = []
            worker_failed = False

            def cb(event):
                events.append(event)

            def cb_fetch(event):
                # 过滤 fetch 自身的 complete 事件（由后续 trim 统一发）
                if event.get('type') == 'complete':
                    events.append({'type': 'progress', 'item': '抓取完成，开始 PMI 裁剪...'})
                else:
                    events.append(event)

            def worker():
                nonlocal worker_failed
                try:
                    run_fetch_cooc(db_path=db_path, progress_callback=cb_fetch,
                                   cancel_check=cancel_evt.is_set)
                    if not cancel_evt.is_set() and not worker_failed:
                        from cooc_pipeline import run_trim_cooc
                        run_trim_cooc(db_path=db_path, progress_callback=cb,
                                      cancel_check=cancel_evt.is_set)
                except Exception as e:
                    worker_failed = True
                    cb({'type': 'fatal', 'error': str(e)})

            t = threading.Thread(target=worker, daemon=True)
            t.start()

            last_sent = 0
            finished = False
            while t.is_alive() or last_sent < len(events):
                while last_sent < len(events):
                    evt = events[last_sent]
                    last_sent += 1
                    etype = evt.get('type')
                    if etype == 'progress':
                        yield sse_event('progress', {
                            'current': evt.get('current', evt.get('page', 0)),
                            'total': evt.get('total', '?'),
                            'item': evt.get('item', '')
                        })
                    elif etype == 'complete':
                        yield sse_event('complete', {'message': f"共现抓取完成（{evt.get('new_count', 0)} 个标签）"})
                        finished = True
                        break
                    elif etype == 'fatal':
                        yield sse_event('fatal', {'error': evt.get('error', '抓取过程出错')})
                        finished = True
                        break
                    elif etype == 'cancelled':
                        yield sse_event('cancelled', {
                            'message': '已取消',
                            'new_count': evt.get('new_count', 0)
                        })
                        finished = True
                        break
                if finished:
                    break
                if t.is_alive():
                    _time.sleep(0.3)

            if not finished:
                if worker_failed:
                    yield sse_event('fatal', {'error': '抓取共现失败，详情见日志'})
        except GeneratorExit:
            cancel_evt.set()
            raise
        except Exception as e:
            print(f'[fetch_cooc] 异常: {e}')
            yield sse_event('fatal', {'error': f'异常终止: {e}'})
        finally:
            _unregister_cancel("fetch_cooc")

    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@translation_bp.route('/trim_cooc', methods=['POST'])
def trim_cooc():
    """PMI 降维裁剪共现数据（SSE 流式），支持中断。"""
    from config import get_tag_db_config
    db_path = get_tag_db_config()['db_path']

    def generate():
        try:
            yield from _generate()
        except GeneratorExit:
            raise

    def _generate():
        cancel_evt = _register_cancel("trim_cooc")
        try:
            yield sse_event('progress', {'current': 0, 'total': '?', 'item': '开始 PMI 裁剪...'})
            from cooc_pipeline import run_trim_cooc
            import threading
            import time as _time

            events = []
            worker_failed = False

            def cb(event):
                events.append(event)

            def worker():
                nonlocal worker_failed
                try:
                    run_trim_cooc(db_path=db_path, progress_callback=cb,
                                  cancel_check=cancel_evt.is_set)
                except Exception as e:
                    worker_failed = True
                    cb({'type': 'fatal', 'error': str(e)})

            t = threading.Thread(target=worker, daemon=True)
            t.start()

            last_sent = 0
            finished = False
            while t.is_alive() or last_sent < len(events):
                while last_sent < len(events):
                    evt = events[last_sent]
                    last_sent += 1
                    etype = evt.get('type')
                    if etype == 'progress':
                        yield sse_event('progress', {
                            'current': evt.get('current', 0),
                            'total': evt.get('total', '?'),
                            'item': evt.get('item', '')
                        })
                    elif etype == 'complete':
                        yield sse_event('complete', {'message': '共现 PMI 裁剪完成'})
                        finished = True
                        break
                    elif etype == 'fatal':
                        yield sse_event('fatal', {'error': evt.get('error', '裁剪过程出错')})
                        finished = True
                        break
                    elif etype == 'cancelled':
                        yield sse_event('cancelled', {'new_count': 0, 'message': '已取消 PMI 裁剪'})
                        finished = True
                        break
                    elif etype == 'error':
                        yield sse_event('progress', {'current': 0, 'total': '?',
                                                      'item': evt.get('message', '')})
                if finished:
                    break
                if t.is_alive():
                    _time.sleep(0.3)

            if not finished:
                if worker_failed:
                    yield sse_event('fatal', {'error': 'PMI 裁剪失败，详情见日志'})

            # 清除共现缓存，下次加载新数据
            if not worker_failed:
                import llm_pipeline as lp
                lp._cooc_cache = None
        except GeneratorExit:
            cancel_evt.set()
            raise
        except Exception as e:
            print(f'[trim_cooc] 异常: {e}')
            yield sse_event('fatal', {'error': f'异常终止: {e}'})
        finally:
            _unregister_cancel("trim_cooc")

    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


# ---------------------------------------------------------------------------
# Danbooru 取消
# ---------------------------------------------------------------------------

@translation_bp.route('/danbooru_cancel', methods=['POST'])
def danbooru_cancel():
    """请求取消正在进行的同步/爬取/更新等操作。
    设置取消标志，worker 在下个检查点退出。
    已处理的数据已落库（每批 commit），下次操作可继续。
    无任务运行时不报错（幂等）。"""
    _cancel_all()
    return jsonify({'message': '正在取消，已保存进度...'})
