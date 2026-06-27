# -*- coding: utf-8 -*-
"""标签翻译模块。

翻译查询优先级：SQLite（danbooru_tags.db 的 cn_name 列）→ LLM（未命中时）→ 回写 SQLite。
已移除 translation_cache.json 缓存层，所有翻译持久化到 SQLite。
"""
import os
import re
import json
import requests
from flask import Blueprint, request, jsonify, current_app, Response
from config import load_env, get_llm_config
from sse_utils import sse_event

translation_bp = Blueprint('translation', __name__)

# 进程级 requests.Session：HTTP 连接池复用（keep-alive）。
# 批量翻译会对同一 LLM 端点连续发多次请求，复用 TCP/TLS 连接省去每次握手的 ~100ms 开销，
# 翻译数百标签时累积省时显著。单用户本地工具，LLM 请求串行发起（batch_translate 串行循环），
# 无并发写同一 Session。
_llm_session = requests.Session()

# 线程局部 Session：并发批处理时每个工作线程用自己的 Session（requests.Session 非线程安全，
# 多线程共享同一 Session 会损坏连接池）。线程退出时自动释放连接。
import threading as _threading
_thread_local = _threading.local()
def _get_thread_session():
    """获取当前线程的 requests.Session（线程隔离，并发安全）。"""
    s = getattr(_thread_local, 'session', None)
    if s is None:
        s = requests.Session()
        _thread_local.session = s
    return s

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
        from build_tag_db import SCHEMA, _ensure_fts_index, _rebuild_fts_index, _table_exists
        from config import get_tag_db_config
        db_path = get_tag_db_config()['db_path']
        if not os.path.isfile(db_path):
            _tag_db_available = False
            return None
        # check_same_thread=False 允许跨线程；busy_timeout 让写锁竞争时等待而非立即报错；
        # journal_mode=WAL 允许「增量更新写」与本连接「读」并发不阻塞
        _tag_db_conn = sqlite3.connect(db_path, check_same_thread=False, timeout=5)
        _tag_db_conn.execute('PRAGMA busy_timeout = 5000')
        _tag_db_conn.execute('PRAGMA journal_mode = WAL')
        _tag_db_conn.executescript(SCHEMA)
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


def _fetch_en_wiki_from_db(tags):
    """从 SQLite 批量取英文 wiki（en_wiki 列），供 LLM 翻译时作参考上下文。
    返回 {tag: en_wiki_str}，key 为原始 tag（带空格），与 _lookup_cn_from_db 保持一致。"""
    conn = _get_tag_db_conn()
    if conn is None or not tags:
        return {}
    try:
        from build_tag_db import lookup_tags
        rows = lookup_tags(conn, tags)
        result = {}
        for tag in tags:  # 用原始 tag 作 key
            norm = tag.strip().replace(' ', '_').lower()  # 与 lookup_tags 内部规范化一致
            info = rows.get(norm)
            if info:
                wiki = (info.get('en_wiki') or '').strip()
                if wiki:
                    result[tag] = wiki
        return result
    except Exception:
        return {}


def _build_prompt_with_bodies(tags, bodies):
    """构造带英文 wiki 参考的 user message（仅 en→zh 时用）。
    要求 LLM 只返回翻译后的字符串数组（与 _llm_translate_tags 返回解析兼容）。"""
    lines = ['请翻译以下标签为中文。部分标签附带英文释义供参考，但只返回翻译后的中文标签名（JSON 字符串数组，与输入顺序一致）。', '']
    for i, tag in enumerate(tags, start=1):
        body = bodies.get(tag, '')
        if body:
            body_short = body[:300].replace('\n', ' ')
            lines.append(f'{i}. {tag}')
            lines.append(f'   释义: {body_short}')
        else:
            lines.append(f'{i}. {tag}')
            lines.append('   释义: (无)')
    return '\n'.join(lines)


def _llm_call_with_retry(session, api_url, headers, payload, max_retries=3):
    """发送 LLM 请求，429/网络异常时指数退避重试。返回 (response, error)。
    并发批处理时各工作线程用线程局部 Session 调用，429 退避避免限流整批失败。"""
    import time as _time
    s = session if session is not None else _llm_session
    response = None
    for attempt in range(max_retries + 1):
        try:
            response = s.post(api_url, headers=headers, json=payload, timeout=120)
        except Exception as e:
            if attempt < max_retries:
                _time.sleep(2 ** attempt)  # 2s→4s→8s
                continue
            return None, f'网络异常: {e}'
        if response.status_code == 429 and attempt < max_retries:
            _time.sleep(2 ** attempt)
            continue
        break
    return response, None


def _llm_translate_tags(cfg, tags, src_name, dst_name, session=None, bodies=None):
    """调用 LLM 翻译一批标签。返回 (translations, error)。
    成功：translations 为与 tags 等长的列表，error 为 None。
    失败：translations 为 None，error 为错误消息。

    session：可选 requests.Session。并发批处理时传线程局部 Session（_get_thread_session）。
    bodies：可选预取的英文 wiki 上下文 {tag: wiki}。并发场景下由主线程预取传入，
        避免工作线程并发读共享 SQLite 连接（线程不安全）。
    429 限流：指数退避重试（2s→4s→8s，最多 3 次）。"""
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {cfg["api_key"]}',
    }
    if bodies is None:
        bodies = _fetch_en_wiki_from_db(tags) if (src_name == '英文' and dst_name == '中文') else {}
    user_content = _build_prompt_with_bodies(tags, bodies) if bodies else json.dumps(tags, ensure_ascii=False)
    payload = {
        'model': cfg['model'],
        'messages': [
            {'role': 'system', 'content': cfg['prompt'].format(src_name=src_name, dst_name=dst_name)},
            {'role': 'user', 'content': user_content},
        ],
        'temperature': 0.3,
    }
    response, err = _llm_call_with_retry(session, cfg['api_url'], headers, payload)
    if err:
        return None, err
    result = response.json()
    if 'error' in result:
        return None, result['error'].get('message', '未知错误')
    content = result['choices'][0]['message']['content'].strip()
    match = re.search(r'\[.*?\]', content, re.DOTALL)
    translations = json.loads(match.group()) if match else [content] * len(tags)
    while len(translations) < len(tags):
        translations.append('')
    return translations[:len(tags)], None


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


@translation_bp.route('/translate_tags', methods=['POST'])
def translate_tags():
    """批量翻译标签。SQLite 未命中的走 LLM，结果回写 SQLite。"""
    data = request.get_json()
    tags = data.get('tags', [])
    src = data.get('src', 'en')
    dst = data.get('dst', 'zh')
    if not tags:
        return jsonify({'translations': []})

    isEn2Zh = (src == 'en' and dst == 'zh')
    isZh2En = (src == 'zh' and dst == 'en')

    # 层 1：SQLite 查询
    results = [None] * len(tags)
    if isEn2Zh:
        hits = _lookup_cn_from_db(tags)
    elif isZh2En:
        hits = _lookup_en_from_db(tags)
    else:
        hits = {}
    uncached = []
    uncached_indices = []
    for i, tag in enumerate(tags):
        if tag in hits:
            results[i] = hits[tag]
        else:
            uncached.append(tag)
            uncached_indices.append(i)

    if not uncached:
        return jsonify({'translations': [r or '' for r in results]})

    # 层 2：LLM 翻译
    cfg = get_llm_config()
    if not cfg['prompt']:
        return jsonify({'error': '未配置翻译系统提示（LLM_TAG_TRANSLATE_PROMPT）'}), 400
    lang_map = {'zh': '中文', 'en': '英文'}
    src_name = lang_map.get(src, src)
    dst_name = lang_map.get(dst, dst)

    try:
        translations, err = _llm_translate_tags(cfg, uncached, src_name, dst_name)
        if err:
            return jsonify({'error': err}), 500
    except requests.exceptions.Timeout:
        return jsonify({'error': '翻译超时'}), 504
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    # 回写 SQLite
    conn = _get_tag_db_conn()
    if conn is not None:
        from build_tag_db import update_translation
        for j, idx in enumerate(uncached_indices):
            translated = translations[j]
            results[idx] = translated
            if isEn2Zh and translated.strip():
                update_translation(conn, uncached[j], translated, commit=False)
            elif isZh2En and translated.strip():
                # 中译英：translated 是英文标签名，写入时把它作为 name，原中文作为 cn_name
                update_translation(conn, translated, uncached[j], commit=False)
        conn.commit()  # 统一提交

    return jsonify({'translations': [r or '' for r in results]})


@translation_bp.route('/batch_translate', methods=['POST'])
def batch_translate():
    """扫描所有标签文件，将未翻译的标签批量翻译并写回 SQLite（SSE 流式）"""
    import os  # 函数内 import（必须在 os.listdir 之前；放函数顶部避免 UnboundLocalError）
    data = request.get_json()
    src = data.get('src', 'en')
    dst = data.get('dst', 'zh')
    upload_dir = current_app.config['UPLOAD_FOLDER']

    isEn2Zh = (src == 'en' and dst == 'zh')
    isZh2En = (src == 'zh' and dst == 'en')

    # 收集所有标签文件中的标签，去重
    all_tags = set()
    file_tags = {}
    for filename in os.listdir(upload_dir):
        if not filename.endswith('.txt'):
            continue
        txt_path = os.path.join(upload_dir, filename)
        with open(txt_path, 'r', encoding='utf-8') as f:
            content = f.read().strip()
        if not content:
            continue
        tags = [t.strip() for t in content.split(',') if t.strip()]
        file_tags[filename] = tags
        for tag in tags:
            all_tags.add(tag)

    # 过滤已翻译的（SQLite 已命中的跳过）
    if isEn2Zh:
        hits = _lookup_cn_from_db(list(all_tags))
    elif isZh2En:
        hits = _lookup_en_from_db(list(all_tags))
    else:
        hits = {}
    to_translate = [t for t in all_tags if t not in hits]

    if not to_translate:
        return jsonify({'translated': 0, 'files': len(file_tags), 'message': '所有标签已翻译'})

    cfg = get_llm_config()
    if not cfg['prompt']:
        return jsonify({'error': '未配置翻译系统提示（LLM_TAG_TRANSLATE_PROMPT）'}), 400

    lang_map = {'zh': '中文', 'en': '英文'}
    src_name = lang_map.get(src, src)
    dst_name = lang_map.get(dst, dst)

    # 批次大小：每批发送给 LLM 的标签数。越大请求数越少（更快），但单次 prompt 更长，
    # 过大可能触发 LLM 输出截断或标签对齐丢失。默认 100（实测单批 100 个短标签稳定）。
    # 可经 LLM_BATCH_SIZE 环境变量覆盖。
    batch_size = max(10, int(os.environ.get('LLM_BATCH_SIZE', '100')))
    total_batches = (len(to_translate) + batch_size - 1) // batch_size
    # 并发度：批量翻译时同时进行的 LLM 请求数。默认 2（保守，避免触发 API 限流；
    # 配合 _llm_call_with_retry 的 429 指数退避）。可经 LLM_CONCURRENCY 调高（如 3~4）。
    concurrency = max(1, int(os.environ.get('LLM_CONCURRENCY', '2')))

    def generate():
        translated_count = 0
        error_count = 0
        try:
            conn = _get_tag_db_conn()
            # 主线程预取每批的英文 wiki 上下文（串行读 SQLite，避免工作线程并发读共享连接）。
            # bodies_by_batch 按 batch 索引存预取结果，工作线程只做 HTTP，不碰 DB。
            batches = []
            for i in range(0, len(to_translate), batch_size):
                batch = to_translate[i:i+batch_size]
                batch_desc = f"标签 {i+1}-{min(i+batch_size, len(to_translate))}"
                # 主线程预取 wiki（仅 en→zh 需要）
                bodies = (_fetch_en_wiki_from_db(batch)
                          if (src == 'en' and dst == 'zh') else {})
                batches.append((batch, batch_desc, bodies))

            from concurrent.futures import ThreadPoolExecutor, as_completed
            # 工作线程任务：纯 HTTP（用线程局部 Session）+ 解析，不碰 DB/共享状态。
            # 返回 (batch, batch_desc, translations, err)，供生成器线程回写 DB + 上报进度。
            def worker(batch, batch_desc, bodies):
                translations, err = _llm_translate_tags(
                    cfg, batch, src_name, dst_name,
                    session=_get_thread_session(), bodies=bodies)
                return (batch, batch_desc, translations, err)

            done_count = 0
            with ThreadPoolExecutor(max_workers=concurrency) as ex:
                futures = [ex.submit(worker, b, desc, bodies) for (b, desc, bodies) in batches]
                for fut in as_completed(futures):
                    batch, batch_desc, translations, err = fut.result()
                    done_count += 1
                    yield sse_event('progress', {
                        'current': done_count, 'total': total_batches, 'item': batch_desc
                    })
                    if err:
                        error_count += 1
                        yield sse_event('error', {'item': batch_desc, 'error': err})
                        continue
                    # DB 写入在生成器线程串行执行（SQLite 单连接非线程安全）
                    if conn is not None and translations:
                        from build_tag_db import update_translation
                        for j, tag in enumerate(batch):
                            tr = translations[j].strip()
                            if tr:
                                translated_count += 1
                                if isEn2Zh:
                                    update_translation(conn, tag, tr, commit=False)
                                elif isZh2En:
                                    update_translation(conn, tr, tag, commit=False)
                        conn.commit()

            yield sse_event('complete', {'translated': translated_count, 'files': len(file_tags), 'errors': error_count})
        except Exception as e:
            # 生成器级别的未预期异常（如 DB 连接失败、yield 中断）：发 fatal，前端能正常收尾
            yield sse_event('fatal', {'error': f'批量翻译异常终止: {e}'})

    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


# ---------------------------------------------------------------------------
# 标签详情（wiki 展示 + 翻译）
# ---------------------------------------------------------------------------

@translation_bp.route('/tag_detail/<path:tag>')
def tag_detail(tag):
    """返回单个标签的完整信息（cn_name/en_wiki/cn_wiki/other_names）"""
    conn = _get_tag_db_conn()
    if conn is None:
        return jsonify({'error': '标签数据库未配置'}), 500
    try:
        from build_tag_db import lookup_tags
        rows = lookup_tags(conn, [tag])
        # lookup_tags 返回的 key 是规范化后的（小写+下划线），不能用原始 tag 直接 in 检查
        norm = tag.strip().replace(' ', '_').lower()
        if norm not in rows:
            return jsonify({'tag': tag, 'cn_name': '', 'en_wiki': '', 'cn_wiki': '', 'other_names': '[]'})
        info = rows[norm]
        return jsonify({'tag': tag, **info})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@translation_bp.route('/translate_tag_wiki', methods=['POST'])
def translate_tag_wiki():
    """翻译单个标签的英文 wiki 为中文，回写 SQLite 的 cn_wiki 列。
    body: {tag, en_wiki}"""
    data = request.get_json()
    tag = (data.get('tag') or '').strip()
    en_wiki = (data.get('en_wiki') or '').strip()
    if not tag or not en_wiki:
        return jsonify({'error': '缺少 tag 或 en_wiki'}), 400

    cfg = get_llm_config()
    if not cfg['prompt']:
        return jsonify({'error': '未配置翻译系统提示（LLM_TAG_TRANSLATE_PROMPT）'}), 400

    # 用 LLM 翻译整段 wiki（不同于标签名翻译的数组 prompt）
    wiki_prompt = (
        '你是一个专业的动漫图库标签词典翻译员。用户会给你一段英文标签释义（Danbooru wiki），'
        '请翻译为简体中文，保留 DText 链接的语义（[[xxx]] 翻译为 xxx 的中文），'
        '只输出翻译后的中文文本，不要输出任何解释、标注或引号。'
    )
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {cfg["api_key"]}',
    }
    # wiki 可能很长，截断前 1500 字符控制 token
    payload = {
        'model': cfg['model'],
        'messages': [
            {'role': 'system', 'content': wiki_prompt},
            {'role': 'user', 'content': en_wiki[:1500]},
        ],
        'temperature': 0.3,
    }
    try:
        response = _llm_session.post(cfg['api_url'], headers=headers, json=payload, timeout=120)
        result = response.json()
        if 'error' in result:
            return jsonify({'error': result['error'].get('message', '未知错误')}), 500
        cn_wiki = result['choices'][0]['message']['content'].strip()
    except requests.exceptions.Timeout:
        return jsonify({'error': '翻译超时'}), 504
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    # 回写 SQLite
    conn = _get_tag_db_conn()
    if conn is not None and cn_wiki:
        try:
            from build_tag_db import update_cn_wiki
            update_cn_wiki(conn, tag, cn_wiki)
        except Exception as e:
            print(f"[translate_tag_wiki] 回写 SQLite 失败: {e}")

    return jsonify({'cn_wiki': cn_wiki})


@translation_bp.route('/update_tag_wiki', methods=['POST'])
def update_tag_wiki():
    """手动编辑并保存单个标签的 wiki（en_wiki 或 cn_wiki）。body: {tag, lang, content}。
    lang: 'en' → en_wiki；'zh' → cn_wiki。不更新 updated_at（与 update_cn_wiki/update_en_wiki 一致），
    避免手动编辑影响 Danbooru 增量抓取的时间锚点。"""
    data = request.get_json() or {}
    tag = (data.get('tag') or '').strip()
    lang = (data.get('lang') or '').strip().lower()
    content = data.get('content')
    if content is None:
        content = ''
    if not tag:
        return jsonify({'error': '缺少 tag'}), 400
    if lang not in ('en', 'zh'):
        return jsonify({'error': 'lang 必须为 en 或 zh'}), 400

    conn = _get_tag_db_conn()
    if conn is None:
        return jsonify({'error': '标签数据库未配置'}), 500
    try:
        from build_tag_db import update_cn_wiki, update_en_wiki
        if lang == 'en':
            update_en_wiki(conn, tag, content)
        else:
            update_cn_wiki(conn, tag, content)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    return jsonify({'ok': True, 'lang': lang, 'content': content})


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
        events = []

        def cb(event):
            events.append(event)

        def worker():
            try:
                update_from_danbooru(db_path, verbose=False, progress_callback=cb)
            except Exception as e:
                cb({'type': 'error', 'message': str(e)})

        t = threading.Thread(target=worker, daemon=True)
        t.start()

        last_sent = 0
        finished = False  # 是否已发送结束事件（complete/fatal），避免 worker 异常后再补发 complete
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
                elif etype == 'complete':
                    yield sse_event('complete', {'new_count': evt['new_count']})
                    finished = True
                    break
            if finished:
                break
            if t.is_alive():
                # 等待新事件，用短 sleep 避免忙等
                _time.sleep(0.5)

        # 线程结束但没收到 complete/error 事件（worker 未捕获异常退出兜底）
        if not finished:
            yield sse_event('fatal', {'error': '增量更新异常终止（未收到完成事件）'})

    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})
