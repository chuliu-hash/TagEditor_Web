# -*- coding: utf-8 -*-
"""标签翻译模块。

翻译查询优先级：SQLite（danbooru_tags.db 的 cn_name 列）→ LLM（未命中时）→ 回写 SQLite。
已移除 translation_cache.json 缓存层，所有翻译持久化到 SQLite。
"""
import os
import json
import threading as _threading
from flask import Blueprint, request, jsonify, Response
from tageditor.core.sse_utils import sse_event
import logging


log = logging.getLogger(__name__)

translation_bp = Blueprint('translation', __name__)

# /tag_cooc 返回的推荐条数（前端共现面板固定展示 20 条，与旧实现 head(20) 同口径）
_TAG_COOC_TOP_K = 20


# ── 本地 SQLite 连接：**线程本地**，不是进程级单例 ──────────────────────────
#
# 为什么必须是线程本地：`app.run()` 只传了 debug/port，而 Flask 3.x 内部是
# `options.setdefault("threaded", True)` → **每请求一线程**。原先全进程共用一个
# `check_same_thread=False` 的连接，`sqlite3.threadsafety == 3` 只保证不内存损坏，
# **事务是连接级的**，于是：
#   · 线程 A 开着事务时，线程 B 的 commit() 会把 A 的半截事务一起提交；
#   · 线程 B 的 rollback() 会把 A 已发出但未提交的写回滚掉（A 的 commit 返回成功却什么都没落）；
#   · B 执行 BEGIN 直接抛 "cannot start a transaction within a transaction"。
# 实测复现过最严重的一条：`llm_pipeline._apply_results` 的 commit 变成 no-op 却不报错，
# 而紧随其后的 `_save_history` 已把这批标签记为「已处理」→ 下一轮按 history 永久跳过
# → **这批翻译永久丢失，界面还显示「完成」**。
#
# 改成线程本地后，并发写由 SQLite 自己的 busy_timeout 跨连接串行化（这才是正确做法）。
# 已实测：线程结束时 thread-local 里的连接会被回收，不会按请求数泄漏句柄
# （300 个线程建连接后存活 Connection == 0）。
_local = _threading.local()
_conn_create_lock = _threading.Lock()
# schema / 迁移 / FTS 只需在本进程里初始化一次：连接现在是每线程一条，
# 若不记这个标志，每个新线程都要重跑 executescript + 迁移探测 + 两次 count()。
_schema_ready = False


def _get_tag_db_conn():
    """取当前线程的 SQLite 连接（懒加载 + 线程本地）。

    DB 不存在时返回 None，后续查询跳过 SQLite 层直接走 LLM。
    不永久缓存「不可用」状态——每次调用都重新检查 DB 是否已出现
    （build_tag_db.py 可能刚建好）。"""
    global _schema_ready, _tag_db_available
    conn = getattr(_local, 'conn', None)
    if conn is not None:
        return conn
    try:
        import sqlite3
        from tageditor.db.build_tag_db import (SCHEMA, _PERSISTENT_PRAGMAS,
                                               _ensure_fts_index,
                                               _rebuild_fts_index,
                                               _migrate_to_target_schema,
                                               apply_conn_pragmas)
        from tageditor.core.config import get_tag_db_config
        db_path = get_tag_db_config()['db_path']

        # sqlite3.connect 会自动创建不存在的文件，所以不需要提前检查 os.path.isfile。
        # check_same_thread 保持默认 True —— 连接只服务本线程，正好让 SQLite
        # 用跨连接的方式（busy_timeout + WAL）处理并发，而不是同连接内的假串行。
        conn = sqlite3.connect(db_path, timeout=5)
        # row_factory 必须在建连接后立刻设：/tag_detail 等处依赖 dict(r)
        conn.row_factory = sqlite3.Row
        # 只设连接级 PRAGMA；journal_mode 是持久属性且有写锁开销，
        # 放在下面那把锁里设一次（见注释）
        apply_conn_pragmas(conn, set_persistent=False)

        if not _schema_ready:
            # 建表/迁移/FTS 是重活且含 DDL，多个线程同时跑会互相撞锁（也浪费），
            # 故串行化；只在首次真正需要时执行。
            with _conn_create_lock:
                if not _schema_ready:
                    # journal_mode=WAL 是**写库头**的操作，需要短暂写锁。它只需成功设一次
                    # （属性持久），放在这把锁内就不会与别的线程的 DDL 互撞。
                    for _k, _v in _PERSISTENT_PRAGMAS:
                        conn.execute(f'PRAGMA {_k} = {_v}')
                    conn.executescript(SCHEMA)
                    # 旧库兼容：列集合与目标不符就迁移
                    if _migrate_to_target_schema(conn):
                        conn.commit()
                    # FTS5 全文索引（search_tags 加速）：建表 + 触发器；空则补数据
                    fts_rebuilt = _ensure_fts_index(conn)
                    fts_count = conn.execute("SELECT count(*) FROM tags_fts").fetchone()[0]
                    tag_count = conn.execute("SELECT count(*) FROM tags").fetchone()[0]
                    # fts_rebuilt 也要触发重建：布局版本升级时表被 DROP 重建，
                    # 只靠 fts_count==0 判断在「重建后恰好非空」的情况下会漏掉。
                    if tag_count > 0 and (fts_count == 0 or fts_rebuilt):
                        _rebuild_fts_index(conn)
                        conn.commit()
                    _schema_ready = True

        _local.conn = conn
        _tag_db_available = True
        return conn
    except Exception:
        import traceback
        traceback.print_exc()
        _tag_db_available = False
        return None


def _lookup_cn_from_db(tags):
    """从 SQLite 批量查标签的中文翻译（en→zh）。返回 {tag: cn_name_first}，key 为原始 tag（带空格）。
    cn_name 可能是逗号分隔的多词（"蓝发,蓝色头发"），取第一项作主翻译。

    主表优先：tags 表有中文名时用主表的；主表未收录或中文名为空时，回落到 user_tags
    （用户新标签表）——用户自己翻译过的新标签也要在标签编辑页显示，否则该列为空。

    注意 key 一致性：lookup_tags 返回的 dict key 是 DB 里的下划线形式（name 列存的是 on_bed），
    但调用方用原始 tag（on bed）做 hits.get(tag) 查找。这里必须用原始 tag 作 key，
    否则带空格的标签（on bed / bed sheet / 角色名等）全部查不到 → 翻译显示为空。"""
    conn = _get_tag_db_conn()
    if conn is None or not tags:
        return {}
    try:
        from tageditor.db.build_tag_db import lookup_tags, lookup_user_tags
        rows = lookup_tags(conn, tags)  # 返回 {normalized_name: info}
        result = {}
        norm_of = {}   # 原始 tag -> 规范化 name
        miss = {}      # 主表无中文名的规范化 name（去重后批量查 user_tags）
        for tag in tags:  # 用原始 tag 作 key，保证下游 hits.get(tag) 命中
            norm = tag.strip().replace(' ', '_').lower()  # 与 lookup_tags 内部规范化一致
            norm_of[tag] = norm
            info = rows.get(norm)
            cn = (info.get('cn_name') or '').strip() if info else ''
            if cn:
                result[tag] = cn.split(',')[0].strip()
            else:
                miss[norm] = True
        if miss:
            # 单独 try：user_tags 表异常时只丢回落部分，不影响主表翻译
            try:
                user_rows = lookup_user_tags(conn, list(miss))
            except Exception:
                user_rows = {}
            for tag in tags:
                if tag in result:
                    continue
                cn = (user_rows.get(norm_of[tag], {}).get('cn_name') or '').strip()
                if cn:
                    result[tag] = cn.split(',')[0].strip()
        return result
    except Exception:
        # 静默 return {} 会让「SQL 报错」与「标签确实没翻译」在前端完全无法区分——
        # 用户看到满屏空白翻译，日志里什么都没有。打日志，返回值语义不变。
        import traceback
        log.error('[translation] _lookup_cn_from_db 查询失败，本次翻译列为空:')
        traceback.print_exc()
        return {}


def _lookup_en_from_db(cn_names):
    """从 SQLite 反向查（zh→en）：中文翻译 → 英文标签名。返回 {cn: en_name}。"""
    conn = _get_tag_db_conn()
    if conn is None or not cn_names:
        return {}
    try:
        from tageditor.db.build_tag_db import lookup_tag_by_cn
        result = {}
        for cn in cn_names:
            en = lookup_tag_by_cn(conn, cn)
            if en:
                result[cn] = en
        return result
    except Exception:
        import traceback
        log.error('[translation] _lookup_en_from_db 查询失败，本次反查结果为空:')
        traceback.print_exc()
        return {}


@translation_bp.route('/lookup_cache', methods=['POST'])
def lookup_cache():
    """查标签翻译（保留原路由名兼容前端）。直接查 SQLite。
    支持双向：前端传 tags + 可选 src/dst（默认 en→zh）。"""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({'error': '请求体必须是 JSON 对象'}), 400
    tags = data.get('tags', [])
    # tags 必须是列表：传字符串时下面的 len()/迭代会得到逐字符的结果
    # （'abc' → 查 3 个单字），静默返回错误长度的翻译数组。
    if not isinstance(tags, list):
        return jsonify({'error': 'tags 必须是数组'}), 400
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
    from tageditor.core.config import get_tag_db_config
    from tageditor.db.sync_tags import _download_sqlite
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
        conn = None
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
            log.info(f'[sync_tags_db] 上游共 {len(rows)} 条标签（含 4 列，约 '
                  f'{len(rows) * 120 / 1024 / 1024:.0f}MB 常驻内存）')
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
                try:
                    # 连接现在是线程本地的，正常情况下没有残留事务；但本线程若在该连接上
                    # 留过一个隐式事务（写过但没 commit），显式 BEGIN 会直接抛
                    # "cannot start a transaction within a transaction"。
                    # 这里先收干净，让 BEGIN 的语义确定。
                    if conn.in_transaction:
                        conn.rollback()
                    conn.execute("BEGIN")
                    conn.executemany(
                        "INSERT OR IGNORE INTO tags (name, cn_name, category, post_count) VALUES (?, ?, ?, ?)",
                        new_tags
                    )
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
                log.info(f'[sync_tags_db] 新增 {len(new_tags)} 个标签')
                yield sse_event('progress', {'current': 4, 'total': 5, 'item': f'已写入 {len(new_tags)} 个新标签'})
            else:
                log.info('[sync_tags_db] 无新标签')
                yield sse_event('progress', {'current': 4, 'total': 5, 'item': '无新标签需要写入'})

            if cancel_evt.is_set():
                yield sse_event('cancelled', {'new_count': len(new_tags), 'update_count': 0, 'message': '已取消'})
                return
            up_map = {r['name']: r for r in rows}
            # 先算好参数再 executemany：逐行 execute 在这张 5 万+ 行的表上会产生同样多次
            # 独立写事务往返，实测慢一个量级，且中途异常时没有统一的回滚点。
            upd_params = []
            for name in local_names:
                r = up_map.get(name)
                if r is None:
                    continue
                cat = int(r['category']) if r['category'] is not None else -1
                pc = int(r['post_count']) if r['post_count'] is not None else 0
                upd_params.append((cat, cat, pc, pc, name))
            update_count = len(upd_params)
            if upd_params:
                try:
                    conn.executemany(
                        "UPDATE tags SET category = CASE WHEN ? >= 0 THEN ? ELSE category END, "
                        "post_count = CASE WHEN ? > 0 THEN ? ELSE post_count END WHERE name = ?",
                        upd_params
                    )
                    conn.commit()
                except Exception:
                    # 进程级共享连接：不留半截未提交事务，否则后续请求会撞 database is locked
                    conn.rollback()
                    raise
            # 同步会新增标签，tags 总行数变了 → prompt_tool 计算 lift 的分母 N 失效
            if new_tags:
                try:
                    import tageditor.translate.prompt_tool as pt
                    pt._invalidate_tags_total()
                except Exception:
                    pass
            log.info(f'[sync_tags_db] 完成: 新增 {len(new_tags)} 条, 更新 {update_count} 条')
            yield sse_event('progress', {'current': 5, 'total': 5, 'item': f'已更新 {update_count} 条已有标签'})
            yield sse_event('complete', {'new_count': len(new_tags), 'update_count': update_count})
        except Exception as e:
            log.error(f'[sync_tags_db] 异常终止: {e}')
            # 兜底回滚：连接是进程级共享的（check_same_thread=False），
            # 上面若在 BEGIN 之后、commit 之前抛出且未被就地 rollback，事务会一直挂着，
            # 后续所有请求都撞 "database is locked"。
            try:
                if conn is not None and conn.in_transaction:
                    conn.rollback()
            except Exception:
                pass
            yield sse_event('fatal', {'error': f'同步异常终止: {e}'})
        finally:
            _unregister_cancel("sync_tags_db", cancel_evt)
            if os.path.exists(sqlite_path):
                os.remove(sqlite_path)

    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@translation_bp.route('/crawl_tag_groups', methods=['POST'])
def crawl_tag_groups():
    """爬取 Danbooru 标签组体系（SSE 流式）。"""
    from tageditor.core.config import get_tag_db_config
    from tageditor.db.tag_groups import run as groups_run
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
            last_error = None
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
                    elif etype == 'error':
                        # 与 fetch_cooc 同一个缺口：worker 报的具体原因不能丢，
                        # 否则前端只能显示「未收到完成信号」，用户不知道该怎么修。
                        last_error = evt.get('message') or evt.get('error') or '未知错误'
                        yield sse_event('error', {'error': last_error})
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
                if last_error:
                    # 报过 error 却走到这里 = worker 出错后正常返回：如实报失败，
                    # 不要发出「完成」把错误盖掉。
                    yield sse_event('fatal', {'error': f'爬取失败：{last_error}'})
                elif worker_failed:
                    yield sse_event('fatal', {'error': '爬取过程出错，详情见日志'})
                else:
                    yield sse_event('complete', {'message': '标签组爬取完成'})
        except GeneratorExit:
            cancel_evt.set()
            raise
        except Exception as e:
            log.error(f'[crawl_tag_groups] 异常终止: {e}')
            yield sse_event('fatal', {'error': f'爬取异常终止: {e}'})
        finally:
            _unregister_cancel("crawl_tag_groups", cancel_evt)

    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@translation_bp.route('/llm_process_db', methods=['POST'])
def llm_process_db():
    """LLM 三层深度翻译管线（SSE 流式）。
    处理数据库中所有标签，生成中文描述/扩展中文名/NSFW 判定。
    body: {reprocess: bool} — 是否重新处理已处理的标签（默认 false）"""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        data = {}
    reprocess = data.get('reprocess', False)

    from tageditor.core.config import get_tag_db_config
    db_path = get_tag_db_config()['db_path']

    # 并发守卫：本轮启动前，把**同名的**旧轮停掉。
    #
    # 为什么必须有：原先没有任何守卫，用户重复点「批量深度翻译」（或页面刷新后
    # 重新点）会叠出多轮同时跑 —— 它们抢同一个 llama.cpp 单 slot、同时写同一个
    # SQLite，界面进度还会互相覆盖（日志里同时出现 708/667/662/659 四组计数）。
    # 更糟的是旧轮停不下来：它的 cancel_evt 已被后来者的 _unregister 摘掉。
    #
    # 只停同名的旧轮，不调 _cancel_all —— 标签库同步/共现抓取可能也在跑，
    # 它们不该被「翻译」这个动作牵连。
    stale = _cancel_name("llm_process_db")
    if stale:
        log.warning('[LLM 翻译] 检测到 %d 个同名旧轮次，已请求其停止（本轮重新开始）', stale)

    cancel_evt = _register_cancel("llm_process_db")

    def generate():
        try:
            yield from _generate()
        except GeneratorExit:
            # 注意措辞：这里接的是**任何**形式的连接终止，不只是用户点「中断」——
            # 页面刷新/导航、关标签页、浏览器回收响应流都会走到这。原先写「前端中断
            # 连接」，于是每次刷新页面都留下一条像用户主动取消的日志，极难排查。
            cancel_evt.set()
            log.info('[LLM 翻译] 连接断开（页面刷新/导航/取消），已设置取消信号')
            raise
        except Exception as e:
            log.error(f'[LLM 翻译] 致命错误: {e}')
            import traceback
            traceback.print_exc()
            yield sse_event('fatal', {'error': f'翻译管线异常: {str(e)}'})
        finally:
            _unregister_cancel("llm_process_db", cancel_evt)

    def _generate():
        import time
        try:
            yield sse_event('progress', {'current': 0, 'total': 5, 'item': '准备 LLM 深度翻译...'})
            log.info('[LLM 翻译] 准备 LLM 深度翻译...')

            # 本地部署（Ollama 等）无需 API Key，只要求端点地址；
            # 空 key 由 resolve_api_key 归一化为占位串（SDK 2.x 对空串也会抛 OpenAIError）
            from tageditor.core.config import resolve_api_key
            base_url = os.environ.get('LLM_TEXT_API_URL', '')
            model = os.environ.get('LLM_TEXT_MODEL', 'default')
            if not base_url:
                log.error('[LLM 翻译] 错误: 未配置 LLM_TEXT_API_URL')
                yield sse_event('fatal', {'error': '未配置 LLM_TEXT_API_URL'})
                return

            if cancel_evt.is_set():
                log.info('[LLM 翻译] 已取消')
                yield sse_event('cancelled', {'translated': 0})
                return

            from openai import OpenAI
            client = OpenAI(base_url=base_url,
                            api_key=resolve_api_key(os.environ.get('LLM_TEXT_API_KEY', '')))

            conn = _get_tag_db_conn()
            if conn is None:
                log.error('[LLM 翻译] 错误: 标签数据库未配置')
                yield sse_event('fatal', {'error': '标签数据库未配置'})
                return

            yield sse_event('progress', {'current': 1, 'total': 5, 'item': '加载标签数据...'})
            import tageditor.translate.llm_pipeline as lp
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
                log.info('[LLM 翻译] 所有标签已处理，无需深度翻译')
                yield sse_event('complete', {'message': '所有标签已处理，无需深度翻译', 'translated': 0})
                return

            log.info(f'[LLM 翻译] 待处理 {total} 条（实体 {len(entity_tags)} / 常规 {len(general_tags)} / 兜底 {len(fallback_tags)}）')
            yield sse_event('progress', {'current': 2, 'total': 5,
                'item': f'待处理 {total} 条（实体 {len(entity_tags)} / 常规 {len(general_tags)} / 兜底 {len(fallback_tags)}）'})

            # 8。演进过 32 → 20 → 8 → 4 → 1 → 8，别再往下调了。
            #
            # 为什么不是更小：实测单条请求**耗时 94 秒，其中只有 12 秒在生成**
            # （135 token @10.86 tok/s）—— 剩下约 82 秒是每次请求的固定开销
            # （隧道往返 / n_slots=1 排队 / KV cache 重建，未最终定位）。
            # 这个开销**每次请求都要付**，所以 batch 越小、总耗时越长：
            #   batch=8：251 次请求 × ~170s ≈ 12 小时
            #   batch=1：2003 次请求 × ~94s  ≈ 52 小时
            # 结论是「别把批次切得太碎」——切碎只会让固定开销被重复支付更多次。
            #
            # 为什么不是更大：batch=8 每批约 1000 token → 生成 93 秒，而
            # **客户端一旦超时断开，llama.cpp 仍会把这批跑完**（远端日志里
            # `release` 才结束），重试就是纯浪费且会把队列越堆越长，
            # 表现为界面进度永远停在 0（done 只在成功后累加）。
            # 配合 _call_llm 的 240s 超时，8 条约有 2.6 倍余量，够用。
            batch_size = 8
            current_run = set()
            done = 0

            banner_msg = None  # 中断时标记，避免 final complete/cancelled 冲突

            def _stage(label, idx, n_total, phase, extra=''):
                """中间态进度：批次**还没跑完**，所以 current/total 保持不变（进度条不动），
                只更新 item 文案告诉用户「现在卡在哪一步」。

                为什么需要它：原先只有「一批处理完」才 yield progress，而一批内部要
                先查 Bangumi（entity 层，每个标签最多 4 次尝试 × timeout=10s × 两轮
                verify_ssl）再调 LLM（重试 5 次）。任一步慢都会让界面长时间静止，
                看起来像进度坏了。实测日志里 Bangumi SSL 失败 + LLM 超时可让一批
                卡住好几分钟，期间零 progress 事件。
                """
                n_batches = max(1, (n_total + batch_size - 1) // batch_size)
                return sse_event('progress', {
                    'current': done, 'total': total,
                    'item': f'{label} {idx}/{n_batches} 批 · {phase}（{len(batch)} 个{extra}）',
                })

            # ── Entity ──
            if entity_tags and not cancel_evt.is_set():
                log.info(f'[LLM 翻译] 开始实体层，共 {len(entity_tags)} 条')
                for i in range(0, len(entity_tags), batch_size):
                    if cancel_evt.is_set():
                        banner_msg = 'cancelled'
                        log.info('[LLM 翻译] 实体层被中断')
                        break
                    batch = entity_tags[i:i + batch_size]
                    # entity 层的 payload 构建会**逐个标签查 Bangumi**，是这一批里
                    # 最慢的一步，先报出来，避免用户以为卡死
                    yield _stage('实体', i // batch_size + 1, len(entity_tags), '查询 Bangumi')
                    payload = lp._build_entity_payloads_batch(batch, tag_to_groups, group_cn_names,
                                                             os.environ.get('BANGUMI_ACCESS_TOKEN', ''),
                                                             cooc_data)
                    yield _stage('实体', i // batch_size + 1, len(entity_tags), '调用模型')
                    try:
                        results = lp._call_llm(client, model, lp.get_system_prompt('llm_entity'), payload, temperature=0.1)
                    except Exception as e:
                        log.error(f'[LLM 翻译] 实体批处理失败: {e}')
                        yield sse_event('error', {'item': f'实体批 {i}-{i + len(batch)}', 'error': str(e)})
                        continue
                    # 只把**真正写入了字段**的标签记入本轮结果：原先无条件
                    # `results` 里的 name 全算已处理，而模型可能返回 cn_wiki 为空的条目
                    # → 记进 history 后被永久跳过（实测 512 条有中文名却没中文 wiki）。
                    current_run.update(lp._apply_results(conn, results))
                    done += len(batch)
                    # 每批保存历史，支持断点续传和中途恢复
                    lp._save_history(db_path, history | current_run)
                    log.info(f'[LLM 翻译] 实体 {i + len(batch)}/{len(entity_tags)}')
                    yield sse_event('progress', {'current': done, 'total': total, 'item': f'实体 {i + len(batch)}/{len(entity_tags)}'})
                    time.sleep(0.5)

            # ── General ──
            if general_tags and not cancel_evt.is_set():
                log.info(f'[LLM 翻译] 开始常规层，共 {len(general_tags)} 条')
                for i in range(0, len(general_tags), batch_size):
                    if cancel_evt.is_set():
                        banner_msg = 'cancelled'
                        log.info('[LLM 翻译] 常规层被中断')
                        break
                    batch = general_tags[i:i + batch_size]
                    payload = [lp._build_general_payload(t, tag_to_groups, group_cn_names, cooc_data)
                               for t in batch]
                    yield _stage('常规', i // batch_size + 1, len(general_tags), '调用模型')
                    try:
                        results = lp._call_llm(client, model, lp.get_system_prompt('llm_general'), payload, temperature=0.4)
                    except Exception as e:
                        log.error(f'[LLM 翻译] 常规批处理失败: {e}')
                        yield sse_event('error', {'item': f'常规批 {i}-{i + len(batch)}', 'error': str(e)})
                        continue
                    # 只把**真正写入了字段**的标签记入本轮结果：原先无条件
                    # `results` 里的 name 全算已处理，而模型可能返回 cn_wiki 为空的条目
                    # → 记进 history 后被永久跳过（实测 512 条有中文名却没中文 wiki）。
                    current_run.update(lp._apply_results(conn, results))
                    done += len(batch)
                    # 每批保存历史
                    lp._save_history(db_path, history | current_run)
                    log.info(f'[LLM 翻译] 常规 {i + len(batch)}/{len(general_tags)}')
                    yield sse_event('progress', {'current': done, 'total': total, 'item': f'常规 {i + len(batch)}/{len(general_tags)}'})
                    time.sleep(0.5)

            # ── Fallback ──
            if fallback_tags and not cancel_evt.is_set():
                log.info(f'[LLM 翻译] 开始兜底层，共 {len(fallback_tags)} 条')
                for i in range(0, len(fallback_tags), batch_size):
                    if cancel_evt.is_set():
                        banner_msg = 'cancelled'
                        log.info('[LLM 翻译] 兜底层被中断')
                        break
                    batch = fallback_tags[i:i + batch_size]
                    payload = [lp._build_general_payload(t, tag_to_groups, group_cn_names, cooc_data)
                               for t in batch]
                    yield _stage('兜底', i // batch_size + 1, len(fallback_tags), '调用模型')
                    try:
                        results = lp._call_llm(client, model, lp.get_system_prompt('llm_fallback'), payload, temperature=0.5)
                    except Exception as e:
                        log.error(f'[LLM 翻译] 兜底批处理失败: {e}')
                        yield sse_event('error', {'item': f'兜底批 {i}-{i + len(batch)}', 'error': str(e)})
                        continue
                    # 必须与 entity/general 层一样落库：兜底层花的是同样的 LLM 调用，
                    # 只记历史不写库 = 结果丢弃 + 该标签被永久跳过（修 bug：原先漏了这一行）
                    # 只把**真正写入了字段**的标签记入本轮结果：原先无条件
                    # `results` 里的 name 全算已处理，而模型可能返回 cn_wiki 为空的条目
                    # → 记进 history 后被永久跳过（实测 512 条有中文名却没中文 wiki）。
                    current_run.update(lp._apply_results(conn, results))
                    done += len(batch)
                    # 每批保存历史
                    lp._save_history(db_path, history | current_run)
                    log.info(f'[LLM 翻译] 兜底 {i + len(batch)}/{len(fallback_tags)}')
                    yield sse_event('progress', {'current': done, 'total': total, 'item': f'兜底 {i + len(batch)}/{len(fallback_tags)}'})
                    time.sleep(0.5)

            # ── 保存历史 ──
            if current_run:
                history.update(current_run)
                lp._save_history(db_path, history)

            if banner_msg == 'cancelled':
                log.info(f'[LLM 翻译] 已中断，已处理 {len(current_run)} 条')
                yield sse_event('cancelled', {'translated': len(current_run), 'message': f'已中断，已处理 {len(current_run)} 条'})
            else:
                log.info(f'[LLM 翻译] 完成: 共处理 {len(current_run)}/{total} 条')
                yield sse_event('complete', {'translated': len(current_run), 'total': total,
                                             'message': f'LLM 深度翻译完成：{len(current_run)} 条'})
        except Exception as e:
            log.error(f'[LLM 翻译] 异常终止: {e}')
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
        from tageditor.db.build_tag_db import lookup_tags, lookup_user_tags
        rows = lookup_tags(conn, [tag])
        norm = tag.strip().replace(' ', '_').lower()

        def _user_tag_fallback():
            """主表无中文名时回落 user_tags；表异常时返回空，不影响主表结果。"""
            try:
                return lookup_user_tags(conn, [norm]).get(norm, {})
            except Exception:
                return {}

        if norm not in rows:
            # 主表未收录：用 user_tags（用户新标签表）的翻译，否则编辑器详情卡对用户标签显示空
            u = _user_tag_fallback()
            return jsonify({'tag': tag, 'cn_name': u.get('cn_name', ''), 'en_wiki': '',
                           'cn_wiki': u.get('cn_wiki', ''), 'other_names': '[]', 'nsfw': 0,
                           'cn_name_locked': 0, 'cn_wiki_locked': 0, 'in_main_db': False})
        info = rows[norm]
        # 显式标记是否主库收录：前端据此禁用「编辑/锁定」（这些写操作只作用于主库）
        info['in_main_db'] = True
        # 主表有记录但无中文名 → 用 user_tags 补齐（主表优先：主表有值时不覆盖）
        if not (info.get('cn_name') or '').strip():
            u = _user_tag_fallback()
            if u.get('cn_name'):
                info['cn_name'] = u['cn_name']
            if u.get('cn_wiki') and not (info.get('cn_wiki') or '').strip():
                info['cn_wiki'] = u['cn_wiki']
        # 补充 tag_groups
        info['tag_groups'] = _get_tag_groups_for(norm)
        return jsonify({'tag': tag, **info})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── 标签组缓存 ─────────────────────────────────────────────────────────────
_tag_groups_cache = None


def _load_tag_groups_cache():
    """加载 tag_groups.json 到缓存。

    路径走 get_tag_db_config() 的 db_path 同级目录（与 llm_pipeline._load_tag_groups 同款），
    **不用 current_app.root_path** —— SSE generator 里没有 app context（Flask 在返回响应时就把
    request context pop 了，之后才消费生成器），用 current_app 会抛 RuntimeError。
    prompt_tool.py 的 tag_groups 工具在流式生成器里直接复用本函数。
    """
    global _tag_groups_cache
    if _tag_groups_cache is not None:
        return _tag_groups_cache
    from tageditor.core.config import get_tag_db_config
    tg_path = os.path.join(os.path.dirname(get_tag_db_config()['db_path']), 'tag_groups.json')
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
    """返回标签的共现推荐标签列表。

    走 llm_pipeline 的进程级共现缓存，不再每次请求重新 read_parquet：
    该文件实测 9.8MB / 133 万行，单次读取 0.70s、峰值 64MB 内存，
    而本接口在标签详情页是高频调用（每次点标签都打一次）。
    缓存按 (路径, mtime, 大小) 失效，/trim_cooc 重写文件后自动生效。"""
    norm = tag.strip().replace(' ', '_').lower()
    if not norm:
        return jsonify({'cooc': []})
    try:
        import tageditor.translate.llm_pipeline as lp
        from tageditor.core.config import get_tag_db_config
        db_path = get_tag_db_config()['db_path']
        # 用 _cooc_is_a 而不是 _load_cooc_data：后者要按 top_k 重建整张 5.2 万条的表
        # （实测 98ms/次），而这里只需要一个标签的切片。
        related = lp._cooc_is_a(db_path, norm, top_k=_TAG_COOC_TOP_K)
        return jsonify({'cooc': [{'related': r, 'count': c} for r, c in related]})
    except Exception as e:
        return jsonify({'error': str(e)}), 500




@translation_bp.route('/update_tag_wiki', methods=['POST'])
def update_tag_wiki():
    """手动编辑并保存标签的中文 wiki。body: {tag, lang, content}。
    lang: 'zh' → cn_wiki。en_wiki 手动编辑已禁用（仅 Danbooru 增量更新可改）。
    受 cn_wiki_locked 守卫：中文 wiki 锁定后跳过更新。
    同 update_cn_name：仅允许编辑主库已收录的标签，避免
    update_cn_wiki 的 INSERT ... ON CONFLICT 把未收录标签插进主库。"""
    data = request.get_json(silent=True)
    # 非 dict 一律 400：漏 Content-Type 的调用拿到 HTML 错误页（前端按 Content-Type
    # 分流，真实的参数错误提示传不到用户）；合法 JSON 的非对象会让 .get() 抛 500。
    if not isinstance(data, dict):
        return jsonify({'error': '请求体必须是 JSON 对象'}), 400
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
    norm = tag.strip().replace(' ', '_').lower()
    if not conn.execute("SELECT 1 FROM tags WHERE name = ?", (norm,)).fetchone():
        return jsonify({'error': f'标签 {tag} 未收录于主标签库，不能在此编辑'}), 404
    try:
        from tageditor.db.build_tag_db import update_cn_wiki
        update_cn_wiki(conn, tag, content)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    return jsonify({'ok': True, 'lang': lang, 'content': content})


# ---------------------------------------------------------------------------
# 手动编辑标签中文名
# ---------------------------------------------------------------------------

@translation_bp.route('/update_cn_name', methods=['POST'])
def update_cn_name():
    """手动编辑并保存单个标签的中文名（cn_name）。body: {tag, cn_name}。

    仅允许编辑主库已收录的标签：update_translation 是 INSERT ... ON CONFLICT，
    对未收录标签会往 tags 表插一行（主库其余字段全是默认值），把用户新标签
    污染进爬取的主库。用户新标签的翻译走 user_tags，不经本路由。"""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({'error': '请求体必须是 JSON 对象'}), 400
    tag = (data.get('tag') or '').strip()
    cn_name = data.get('cn_name', '')
    if cn_name is None:
        cn_name = ''
    if not tag:
        return jsonify({'error': '缺少 tag'}), 400

    conn = _get_tag_db_conn()
    if conn is None:
        return jsonify({'error': '标签数据库未配置'}), 500
    norm = tag.strip().replace(' ', '_').lower()
    if not conn.execute("SELECT 1 FROM tags WHERE name = ?", (norm,)).fetchone():
        return jsonify({'error': f'标签 {tag} 未收录于主标签库，不能在此编辑'}), 404
    try:
        from tageditor.db.build_tag_db import update_translation
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
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({'error': '请求体必须是 JSON 对象'}), 400
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
    """深度翻译单个标签（复用 llm_pipeline.translate_one_tag 三层管线）。
    body: {tag}
    返回 {cn_name, cn_wiki, nsfw}

    只处理主库已收录的标签（前端详情卡对 in_main_db=false 的标签已隐藏翻译按钮）：
    _update_tag 是纯 UPDATE，未收录标签不会插入行。用户新标签的翻译走 /user_tags/translate。"""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({'error': '请求体必须是 JSON 对象'}), 400
    tag = (data.get('tag') or '').strip()
    if not tag:
        return jsonify({'error': '缺少 tag'}), 400

    conn = _get_tag_db_conn()
    if conn is None:
        return jsonify({'error': '标签数据库未配置'}), 500

    from tageditor.db.build_tag_db import lookup_tags
    norm = tag.strip().replace(' ', '_').lower()
    info = lookup_tags(conn, [tag]).get(norm)
    if not info:
        return jsonify({'error': f'标签 {tag} 未收录于主标签库'}), 404

    # 构造 tag_data（与 _load_tags 返回结构一致），层级由 translate_one_tag 判定
    tag_data = {
        'name': norm,
        'cn_name': info.get('cn_name', ''),
        'en_wiki': info.get('en_wiki', ''),
        'category': int(info.get('category', -1)),
        'other_names': info.get('other_names', '[]'),
    }

    from tageditor.translate.llm_pipeline import translate_one_tag, _update_tag
    from tageditor.core.config import get_tag_db_config
    try:
        result = translate_one_tag(tag_data, get_tag_db_config()['db_path'])
    except ValueError as e:
        return jsonify({'error': str(e)}), 400

    _update_tag(conn, norm,
                cn_name=result['cn_name'] or None,
                cn_wiki=result['cn_wiki'] or None,
                nsfw=result['nsfw'])
    conn.commit()

    # 重新查最新结果（受锁定守卫影响，以库中值为准）
    updated = lookup_tags(conn, [tag]).get(norm, {})
    return jsonify({
        'cn_name': updated.get('cn_name', ''),
        'cn_wiki': updated.get('cn_wiki', ''),
        'nsfw': updated.get('nsfw', 0),
    })


# ---------------------------------------------------------------------------
# 用户新标签（user_tags）：打标中遇到、主库未收录的标签，独立于爬取的 tags 表
# ---------------------------------------------------------------------------

@translation_bp.route('/user_tags', methods=['GET'])
def list_user_tags_api():
    """列出全部用户新标签，附主库收录标注（in_main_db：主表 tags 是否已收录同名标签）。"""
    conn = _get_tag_db_conn()
    if conn is None:
        return jsonify({'error': '标签数据库未配置'}), 500
    try:
        from tageditor.db.build_tag_db import list_user_tags, lookup_tags
        rows = list_user_tags(conn)
        main = lookup_tags(conn, [r['name'] for r in rows]) if rows else {}
        for r in rows:
            info = main.get(r['name'])
            r['in_main_db'] = info is not None
            # 主表优先：主库已收录时展示主库的中文名/中文 wiki（主库为空则保留本表值）
            if info:
                r['cn_name'] = (info.get('cn_name') or '').strip() or r['cn_name']
                r['cn_wiki'] = (info.get('cn_wiki') or '').strip() or r['cn_wiki']
        return jsonify({'user_tags': rows})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@translation_bp.route('/user_tags', methods=['POST'])
def upsert_user_tag_api():
    """新增/更新用户新标签。body: {name, cn_name?, cn_wiki?}。
    新增时若主表已收录同名标签则拒绝（两表同名时以主表为准）；
    编辑已存在的行不受此限制（主库后续收录不影响已有记录）。"""
    from tageditor.db.build_tag_db import lookup_tags, normalize_tag_key, upsert_user_tag
    data = request.get_json(silent=True)
    # `or {}` 会把合法 JSON 的非对象（如 [1,2]、"abc"）也兜成默认值往下走，
    # 这里必须显式判类型——「参数没看懂就执行」不是可接受的降级。
    if not isinstance(data, dict):
        return jsonify({'error': '请求体必须是 JSON 对象'}), 400
    name_key = normalize_tag_key(data.get('name') or '')
    if not name_key:
        return jsonify({'error': '请填写标签名'}), 400
    if ',' in name_key or '，' in name_key:
        return jsonify({'error': '标签名不能包含逗号'}), 400
    conn = _get_tag_db_conn()
    if conn is None:
        return jsonify({'error': '标签数据库未配置'}), 500
    try:
        exists = conn.execute("SELECT 1 FROM user_tags WHERE name = ?", (name_key,)).fetchone()
        if not exists and name_key in lookup_tags(conn, [name_key]):
            return jsonify({'error': f'标签 {name_key} 已在主标签库中（以主库为准，无需添加）'}), 400
        cn_name = data.get('cn_name')
        cn_wiki = data.get('cn_wiki')
        upsert_user_tag(
            conn, name_key,
            cn_name=str(cn_name).strip() if cn_name is not None else None,
            cn_wiki=str(cn_wiki).strip() if cn_wiki is not None else None,
        )
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    return jsonify({'ok': True, 'name': name_key})


@translation_bp.route('/user_tags/delete', methods=['POST'])
def delete_user_tag_api():
    """删除用户新标签。body: {name}。"""
    from tageditor.db.build_tag_db import normalize_tag_key, delete_user_tag
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({'error': '请求体必须是 JSON 对象'}), 400
    name_key = normalize_tag_key(data.get('name') or '')
    if not name_key:
        return jsonify({'error': '缺少 name'}), 400
    conn = _get_tag_db_conn()
    if conn is None:
        return jsonify({'error': '标签数据库未配置'}), 500
    try:
        delete_user_tag(conn, name_key)
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    return jsonify({'ok': True, 'name': name_key})


@translation_bp.route('/user_tags/translate', methods=['POST'])
def translate_user_tag():
    """LLM 深度翻译单个用户新标签，结果写入 user_tags 自己的字段（不碰主表）。
    body: {name}。主表已收录且有中文名时直接返回主表数据（以主表为准）。
    返回 {cn_name, cn_wiki, source}，source 为 'main_db' 或 'llm'。"""
    from tageditor.db.build_tag_db import lookup_tags, normalize_tag_key, upsert_user_tag
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({'error': '请求体必须是 JSON 对象'}), 400
    name_key = normalize_tag_key(data.get('name') or '')
    if not name_key:
        return jsonify({'error': '缺少 name'}), 400
    conn = _get_tag_db_conn()
    if conn is None:
        return jsonify({'error': '标签数据库未配置'}), 500

    # 主表优先：主库已收录且有中文名，直接用主库翻译
    main_info = lookup_tags(conn, [name_key]).get(name_key)
    if main_info and (main_info.get('cn_name') or '').strip():
        return jsonify({'cn_name': main_info.get('cn_name', ''),
                        'cn_wiki': main_info.get('cn_wiki', ''),
                        'source': 'main_db'})

    if not conn.execute("SELECT 1 FROM user_tags WHERE name = ?", (name_key,)).fetchone():
        return jsonify({'error': f'标签 {name_key} 不在用户新标签表中'}), 404

    # LLM 翻译：复用深度翻译管线（translate_one_tag 按 tag_data 自动判层级，
    # 新标签无 en_wiki/category → 走 fallback 层，temperature=0.5）
    from tageditor.translate.llm_pipeline import translate_one_tag
    from tageditor.core.config import get_tag_db_config
    tag_data = {'name': name_key, 'cn_name': '', 'en_wiki': '', 'category': -1, 'other_names': '[]'}
    try:
        result = translate_one_tag(tag_data, get_tag_db_config()['db_path'])
    except ValueError as e:
        return jsonify({'error': str(e)}), 400

    # 空串必须转成 None：upsert_user_tag 只把 None 当「保持原值」，
    # 直接传 '' 会在 LLM 返回空时抹掉用户已存的中文名/中文 wiki
    upsert_user_tag(conn, name_key,
                    cn_name=result['cn_name'] or None,
                    cn_wiki=result['cn_wiki'] or None)
    return jsonify({'cn_name': result['cn_name'], 'cn_wiki': result['cn_wiki'], 'source': 'llm'})


# ---------------------------------------------------------------------------
# Danbooru 标签查询页面专用
# ---------------------------------------------------------------------------

@translation_bp.route('/danbooru_search', methods=['POST'])
def danbooru_search():
    """模糊搜索标签库。body: {keyword, limit=20, light=false}"""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({'error': '请求体必须是 JSON 对象'}), 400
    keyword = (data.get('keyword') or '').strip()
    # 上限 500，防止单次返回过多拖慢传输/渲染；下限 1 —— 不能只钳上限：
    # limit=-1 在 SQLite 里是「不限量」，会一次吐回整张 5 万行的表。
    # 显式校验并返回 400：int(None) 抛的是 TypeError，不是 ValueError，
    # 原先裸 int() 会被下面的 except 兜成 500「内部错误」，看不出是参数问题。
    raw_limit = data.get('limit', 20)
    try:
        limit = int(raw_limit)
    except (TypeError, ValueError):
        return jsonify({'error': f'limit 必须是整数，收到 {raw_limit!r}'}), 400
    limit = min(max(limit, 1), 500)  # 上限 500，防止单次返回过多拖慢传输/渲染
    if not keyword:
        return jsonify({'results': []})

    conn = _get_tag_db_conn()
    if conn is None:
        return jsonify({'error': '标签数据库未配置'}), 500
    try:
        from tageditor.db.build_tag_db import search_tags
        results = search_tags(conn, keyword, limit, light=bool(data.get('light')))
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
    # 随机推荐：**别用 ORDER BY RANDOM()**。
    # 它要为全部 5.3 万行求值 RANDOM() 再整体排序（实测 107.6ms，
    # PLAN: SCAN tags + USE TEMP B-TREE FOR ORDER BY），而这是**每次打开首页**
    # 都会打的接口。改成「随机取一段 rowid 区间，再按 rowid 顺序取 N 条」后
    # PLAN 变成 SEARCH tags USING INTEGER PRIMARY KEY，实测 0.1ms（约 1000 倍）。
    # 代价：N 条会聚集在 rowid 相邻的一段（同一批同步进来的标签相邻），
    # 对「首页随机推荐」这个用途可以接受。
    import random as _random
    _max = conn.execute("SELECT max(rowid) FROM tags").fetchone()[0] or 0
    if _max <= 0:
        return jsonify({'tags': []})
    _window = max(n * 20, 2000)
    _lo = _random.randint(1, max(1, _max - _window + 1))
    rows = conn.execute(
        "SELECT name, cn_name, category, post_count FROM tags "
        "WHERE rowid >= ? AND rowid < ? AND cn_name != '' AND cn_name IS NOT NULL "
        "ORDER BY rowid LIMIT ?", (_lo, _lo + _window, n)
    ).fetchall()
    if not rows:
        # 该 rowid 段恰好没有带中文名的标签（尾部稀疏）→ 回退全表随机，
        # 保证「宁可慢一点也不能返回空」；正常情况走不到这里。
        rows = conn.execute(
            "SELECT name, cn_name, category, post_count FROM tags WHERE cn_name != '' AND cn_name IS NOT NULL "
            "ORDER BY RANDOM() LIMIT ?", (n,)
        ).fetchall()
    return jsonify({'tags': [dict(r) for r in rows]})


@translation_bp.route('/danbooru_update', methods=['POST'])
def danbooru_update():
    """触发增量更新（SSE 流式）。包装 update_from_danbooru 的 progress_callback 为 SSE 事件。"""
    from tageditor.core.config import get_tag_db_config
    from tageditor.db.build_tag_db import update_from_danbooru
    db_path = get_tag_db_config()['db_path']

    # 与 llm_process_db 同款：取消事件在**路由体**里注册，_generate 与 generate 共同闭包它，
    # 这样 generate 的 GeneratorExit 分支才能置位同一个 event。
    cancel_evt = _register_cancel("danbooru_update")

    def _generate():
        # worker 在后台线程跑 update_from_danbooru，通过 cb 回调把事件追加到 events；
        # 主线程（SSE generator）轮询 events 顺序 yield 为 SSE。
        import threading
        import time as _time
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
        try:
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
        finally:
            # **必须放 finally**：原先 _unregister_cancel 写在生成器主体末尾，
            # 而客户端断开（页面刷新/导航/关标签页/前端对旧流 abort）会让生成器在
            # 任意一个 yield 处收到 GeneratorExit 直接退出 —— 那行就永远执行不到。
            # 后果有两层，都很隐蔽：
            #   ① 取消登记永久残留 → _has_active() 永远为真（以后所有「互斥」判断都误判）；
            #   ② worker 是 daemon 线程，它按 cancel_evt.is_set 决定要不要停，而这个
            #      event 从来没被置位 → **用户以为停了，爬虫继续抓取并写库**；
            #      再点一次就是两个爬虫并发写同一个 db_path 与同一份断点锚点。
            _unregister_cancel("danbooru_update", cancel_evt)

    def generate():
        # 与本文件其它 6 条 SSE 路由同款收尾（crawl_tag_groups / fetch_cooc / trim_cooc /
        # llm_process_db 都有，只有这条漏了）：断开时置取消信号，让 worker 自己停。
        try:
            yield from _generate()
        except GeneratorExit:
            # 注意措辞：这里接的是**任何**形式的连接终止，不只是用户点「中断」——
            # 页面刷新/导航、关标签页、前端对旧流 abort()（danbooru_wiki.html 在启动新
            # 操作时会主动 abort 上一条）都会走到这。
            cancel_evt.set()
            log.info('[danbooru_update] 连接断开（页面刷新/导航/取消），已请求后台爬虫停止')
            raise

    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


# 取消事件注册表：各 SSE 路线创建各自的 Event，/danbooru_cancel 统一取消所有活跃操作。
#
# **同一个操作名可能同时有多轮在跑**（用户重复点「批量深度翻译」，或页面刷新后
# 旧的那轮还没退出）。原先用 `dict[name] = evt` 直接覆盖，于是：
#   1) 旧的一轮结束时 `_unregister_cancel(name)` 把**新**一轮的 event 摘掉了 ——
#      此后点「中断」对新那轮无效（_cancel_all 找不到它），它会一直烧 GPU 到跑完。
#   2) 即使都在字典里，也只能存下一轮，先启动的那轮永远取消不掉。
# 改成 set 收集同一个名字下的所有 event，并在注销时**按对象身份**移除，
# 互不干扰。
_active_cancel_events: dict[str, set] = {}   # name -> {Event, ...}
_cancel_events_lock = _threading.Lock()

def _register_cancel(name: str) -> _threading.Event:
    """注册一个操作名到取消事件，返回新建的 Event。每个 SSE 路线各自注册。

    同名多轮各自持有独立的 Event 并**同时**登记在册，任意一轮的结束都不会
    影响其它轮的可取消性。调用方必须把返回的 Event 传给 _unregister_cancel。
    """
    evt = _threading.Event()
    with _cancel_events_lock:
        _active_cancel_events.setdefault(name, set()).add(evt)
    return evt

def _unregister_cancel(name: str, evt=None):
    """操作完成/取消后注销。

    **必须传 evt**：只移除「就是自己这一个」的登记。省略 evt 时按名字整组清除，
    仅用于确认没有并发同名的场景（当前所有调用点都传）。
    """
    with _cancel_events_lock:
        if evt is None:
            _active_cancel_events.pop(name, None)
            return
        group = _active_cancel_events.get(name)
        if group is not None:
            group.discard(evt)
            if not group:
                _active_cancel_events.pop(name, None)

def _cancel_all():
    """设置所有活跃的取消事件（前端一键取消）。"""
    with _cancel_events_lock:
        events = [e for group in _active_cancel_events.values() for e in group]
    for e in events:
        e.set()

def _cancel_name(name: str) -> int:
    """只取消指定名字下的所有活跃操作，返回置位的事件数。

    用于「重复触发同一个操作」：新的一轮要把**同名的旧轮**停掉，但不该连带
    停掉别的操作（比如正在同步标签库时点了深度翻译）。
    """
    with _cancel_events_lock:
        events = list(_active_cancel_events.get(name, ()))
    for e in events:
        e.set()
    return len(events)

def _has_active(name: str) -> bool:
    """该操作名下是否已有活跃轮次（用于重复触发守卫）。"""
    with _cancel_events_lock:
        return bool(_active_cancel_events.get(name))


# ---------------------------------------------------------------------------
# 共现数据更新
# ---------------------------------------------------------------------------

@translation_bp.route('/fetch_cooc', methods=['POST'])
def fetch_cooc():
    """增量抓取标签共现数据（SSE 流式）。只抓新标签的共现。"""
    from tageditor.core.config import get_tag_db_config
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
            from tageditor.db.cooc_pipeline import run_fetch_cooc
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
                        from tageditor.db.cooc_pipeline import run_trim_cooc
                        run_trim_cooc(db_path=db_path, progress_callback=cb,
                                      cancel_check=cancel_evt.is_set)
                except Exception as e:
                    worker_failed = True
                    cb({'type': 'fatal', 'error': str(e)})

            t = threading.Thread(target=worker, daemon=True)
            t.start()

            last_sent = 0
            finished = False
            last_error = None  # worker 报过的具体原因（用于结尾兜底时告诉用户真正的问题）
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
                    elif etype == 'error':
                        # **原先没有这个分支**：worker 通过 error 事件报的具体原因
                        # （例如 cooc_pipeline 的「找不到原始共现文件，先运行 fetch-cooc」）
                        # 被整个丢掉，而 worker 随后正常 return、又不发终态事件 →
                        # 前端只能报「连接异常中断：未收到完成信号 / 请重试」，
                        # 重试同样失败，用户永远看不到该怎么修 —— 而那句话正是唯一有用的信息。
                        last_error = evt.get('message') or evt.get('error') or '未知错误'
                        yield sse_event('error', {'error': last_error})
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

            # **一律发终态事件**：没有终态事件的流会让前端报「连接异常中断」，
            # 而 worker 其实是有结论地结束了（正常早退 / 出错）。有具体原因就带上它。
            if not finished:
                if last_error:
                    yield sse_event('fatal', {'error': f'抓取共现失败：{last_error}'})
                elif worker_failed:
                    yield sse_event('fatal', {'error': '抓取共现失败，详情见日志'})
                else:
                    yield sse_event('fatal', {'error': '抓取共现异常终止（未收到完成事件，详情见日志）'})
        except GeneratorExit:
            cancel_evt.set()
            raise
        except Exception as e:
            log.error(f'[fetch_cooc] 异常: {e}')
            yield sse_event('fatal', {'error': f'异常终止: {e}'})
        finally:
            _unregister_cancel("fetch_cooc", cancel_evt)

    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@translation_bp.route('/trim_cooc', methods=['POST'])
def trim_cooc():
    """PMI 降维裁剪共现数据（SSE 流式），支持中断。"""
    from tageditor.core.config import get_tag_db_config
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
            from tageditor.db.cooc_pipeline import run_trim_cooc
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
            # （用 _invalidate_cooc_cache 而非直接赋 _cooc_cache=None：
            #  缓存键也要一起清，否则 key 仍是旧的、新文件 mtime 对不上虽然也能重建，
            #  但留着不一致的状态容易误判。）
            if not worker_failed:
                import tageditor.translate.llm_pipeline as lp
                lp._invalidate_cooc_cache()
        except GeneratorExit:
            cancel_evt.set()
            raise
        except Exception as e:
            log.error(f'[trim_cooc] 异常: {e}')
            yield sse_event('fatal', {'error': f'异常终止: {e}'})
        finally:
            _unregister_cancel("trim_cooc", cancel_evt)

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
