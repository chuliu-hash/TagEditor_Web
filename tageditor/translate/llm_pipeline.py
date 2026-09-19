# -*- coding: utf-8 -*-
"""LLM 三层翻译增强管线（general/fallback/entity）。

从 SQLite 读取标签，分为三类处理：
- general: 有英文 Wiki → 翻译 + 扩展中文名 + NSFW 判定
- fallback: 无 Wiki → 依赖模型知识库
- entity: 角色/作品标签 → Bangumi API 查证 + LLM 防幻觉重写

输出回写 SQLite 的 cn_wiki/nsfw/cn_name 字段。
"""
import json
import os
import re
from tageditor.core.config import get_prompt, USER_AGENT
import sys
import time
import random
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests as req
import urllib3
from pathlib import Path
from tageditor.core.config import get_tag_db_config, resolve_api_key
from tageditor.db.build_tag_db import normalize_tag_key
import logging

# 抑制 verify=False 时的 SSL 警告（Bangumi API 偶发 TLS 兼容性问题）

log = logging.getLogger(__name__)

warnings.filterwarnings('ignore', category=urllib3.exceptions.InsecureRequestWarning)


# ── 常量 ──────────────────────────────────────────────────────────────────
_DEBUG = False

_HANZI_RE = re.compile(
    r"[一-鿿㐀-䶿\U00020000-\U0002a6df"
    r"\U0002a700-\U0002ceaf豈-﫿]"
)


def _dbg(label: str, content=None):
    if not _DEBUG:
        return
    log.info(f"[LLM Pipe DEBUG] {label}")
    if content is not None:
        if isinstance(content, (dict, list)):
            log.info(json.dumps(content, ensure_ascii=False, indent=2)[:500])
        else:
            log.info(str(content)[:500])


# ── 数据库工具 ─────────────────────────────────────────────────────────────

def _get_conn(db_path: str):
    import sqlite3
    conn = sqlite3.connect(db_path, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def _load_tags(conn) -> list[dict]:
    """加载所有标签。"""
    rows = conn.execute("""
        SELECT name, cn_name, en_wiki, cn_wiki, category, post_count, other_names, nsfw, cn_name_locked, cn_wiki_locked
        FROM tags ORDER BY post_count DESC
    """).fetchall()
    return [dict(r) for r in rows]


def _update_tag(conn, name: str, cn_name: str = None,
                cn_wiki: str = None, nsfw: int = None):
    """更新单条标签的 LLM 处理结果。cn_name/cn_wiki 始终写入（可覆盖已有值），
    nsfw 始终写入。受 cn_name_locked / cn_wiki_locked 独立守卫。"""
    set_clauses = []
    params = {'name': name}
    if cn_name is not None:
        set_clauses.append("cn_name = CASE WHEN cn_name_locked = 1 THEN cn_name ELSE :cn_name END")
        params['cn_name'] = cn_name
    if cn_wiki is not None:
        set_clauses.append("cn_wiki = CASE WHEN cn_wiki_locked = 1 THEN cn_wiki ELSE :cn_wiki END")
        params['cn_wiki'] = cn_wiki
    if nsfw is not None:
        set_clauses.append("nsfw = :nsfw")
        params['nsfw'] = nsfw
    if not set_clauses:
        return
    conn.execute(f"UPDATE tags SET {', '.join(set_clauses)} WHERE name = :name", params)


# ── Bangumi API ────────────────────────────────────────────────────────────

# 熔断状态：{失败计数}。Bangumi 挂掉时（实测 MeilisearchCommunicationError 会
# 让它对每个请求返 500），不改这个的话每个标签都要撞满 13 个请求 × timeout=10s，
# 一批 32 个标签最坏 8.7 分钟，而结果必然是空 —— 纯白等。
#
# 语义：失败达阈值就「跳闸」，之后本次进程内不再发包，直接返回空结果。
# 任何一次成功都会把计数清零（说明服务恢复了）。
# 不加时间窗口自动恢复，是因为恢复判定的代价同样是一次完整请求；
# 让用户重启进程或重新触发任务更直观，且失败会被日志明确说明。
#
# **阈值 = 1（探针语义）**，不是 5。原先是 5，但一批 8 个 entity 标签是
# **并发**发起的：它们同时开始、同时失败，在第一次 attempt 时谁都没累加到 5，
# 于是必须等整批 8 个全部跑完才跳闸 —— 第一批照样白等 2~3 分钟才轮到 LLM。
# 改成 1 之后，第一个标签失败即跳闸，同批其余 7 个直接拿到空结果。
_BANGUMI_FAIL_THRESHOLD = 1
_bangumi_fail_count = 0
_bangumi_tripped = False


def _bangumi_circuit_open() -> bool:
    return _bangumi_tripped


def _bangumi_note_success():
    global _bangumi_fail_count, _bangumi_tripped
    _bangumi_fail_count = 0
    _bangumi_tripped = False


def _bangumi_note_failure(tag_name: str, reason: str):
    """记一次失败；达阈值则跳闸并只在这一刻打一条明确日志。

    阈值当前是 1，所以不说「连续 N 次失败」—— 计数为 1 时那句话既冗余又别扭。
    只在阈值 > 1 时才提次数。
    """
    global _bangumi_fail_count, _bangumi_tripped
    _bangumi_fail_count += 1
    if _bangumi_fail_count >= _BANGUMI_FAIL_THRESHOLD and not _bangumi_tripped:
        _bangumi_tripped = True
        if _BANGUMI_FAIL_THRESHOLD > 1:
            head = '连续 %d 次查询失败' % _bangumi_fail_count
        else:
            head = '查询失败'
        log.warning(
            "[LLM] Bangumi %s（%s；示例标签 %s）。"
            "本次任务内不再尝试 Bangumi 查证，改用本地 wiki/LLM 兜底。"
            "多是代理未开或 Bangumi 服务故障，修好后重启进程即可恢复。",
            head, reason, tag_name)


def reset_bangumi_circuit():
    """手动复位熔断（测试用；正常流程靠一次成功自动恢复）。"""
    global _bangumi_fail_count, _bangumi_tripped
    _bangumi_fail_count = 0
    _bangumi_tripped = False


def _build_bangumi_session():
    """创建 Bangumi API 专用的 requests Session。

    **不对 5xx 做重试**。原先 status_forcelist 含 500/502/503/504，服务端持续
    故障时 urllib3 会把每次尝试放大成 4 个 HTTP 请求，再叠上内层 3 次 attempt
    与 verify_ssl 两轮 —— 单标签最坏 13 个请求、约 130 秒，而结果必然是空。

    实测（Bangumi 搜索后端故障期间）：`POST /v0/search/*` **0.5 秒**就返回 502，
    是明确的「服务端坏了」信号，不是网络抖动。对明确故障重试没有价值，只会
    把故障放大成白等。真正需要重试的是网络抖动（连接被重置、读超时），
    那由内层 attempt 循环 + 熔断器处理，不在这里。

    429 仍保留重试：那是限流，等一会儿确实能好。
    """
    sess = req.Session()
    # 只对 429（限流）做自动重试。**5xx 不重试** —— 实测 Bangumi 搜索后端故障时
    # `POST /v0/search/*` 0.5 秒就返回 502，那是明确的服务端故障信号，重试没有价值
    # （原先 status_forcelist 含 500/502/503/504，把每次尝试放大成 4 个 HTTP 请求）。
    # 读超时也不重试（urllib3 默认就不对 read timeout 重试）：服务端不响应时
    # 重试同一请求几乎不可能好转，直接失败交给熔断更快。
    retries = urllib3.Retry(total=2, backoff_factor=1,
                            allowed_methods=["POST"],
                            status_forcelist=[429])
    adapter = req.adapters.HTTPAdapter(max_retries=retries)
    sess.mount('https://', adapter)
    return sess


def _fetch_bangumi_entity(tag_name: str, category: int, token: str) -> dict:
    """从 Bangumi API 获取实体信息（角色/作品），返回 {cn_name, summary}。

    只处理 category 3（作品）/ 4（角色）；其它分类直接返回空。
    失败一次即熔断（见 `_BANGUMI_FAIL_THRESHOLD`）。

    **探针语义**：第一个标签会真正发包；只要它失败就立刻跳闸，后续所有标签
    零开销。这是被实测逼出来的 —— 原先阈值 5 + 并发 8，8 个标签同时开始、
    谁都没累加到 5，必须等整批跑完才跳闸，第一批仍要白等 2~3 分钟。
    探针的代价是正常时多一次往返（0.1~0.5s），换来故障时第一批秒级返回。
    """
    result = {"cn_name": "", "summary": ""}
    if category not in (3, 4):
        return result
    # 熔断：服务端已确认故障时不再为每个标签白撞
    if _bangumi_circuit_open():
        return result

    qualifier_match = re.search(r"_\(([^)]+)\)$", str(tag_name))
    qualifier = qualifier_match.group(1) if qualifier_match else ""
    clean_name = re.sub(r"_\(.*\)$", "", str(tag_name)).replace("_", " ").strip().lower()

    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    # 与 Danbooru 爬取使用同一代理
    proxy = os.environ.get('DANBOORU_PROXY', '')
    proxies = {'http': proxy, 'https': proxy} if proxy else None

    # SSL 降级兜底：首次正常请求 → 遇 SSL 错误时换 verify=False 再试。
    # **只对 SSL 错误有意义**：服务端 5xx 时再跑一轮是纯浪费（实测每标签多 10s）。
    urls = {3: "https://api.bgm.tv/v0/search/subjects",
            4: "https://api.bgm.tv/v0/search/characters"}
    url = urls[category]
    # 最后一次失败的原因，用于熔断日志
    last_reason = ''
    # 是否还需要跑 verify_ssl=False 那一轮。只有 SSL 错误才置 True ——
    # 裸 `break` 只能跳出内层 attempt 循环，外层 for 仍会继续，实测会导致
    # 5xx 时白跑一轮 verify=False（每标签多 10s）。
    try_ssl_fallback = False

    for verify_ssl in (True, False):
        if not verify_ssl and not try_ssl_fallback:
            break          # 没遇到 SSL 错误，不需要降级重试
        sess = _build_bangumi_session() if verify_ssl else req.Session()
        try:
            for attempt in range(3 if verify_ssl else 1):
                try:
                    payload = {"keyword": clean_name}
                    resp = sess.post(url, json=payload, headers=headers,
                                     timeout=10, proxies=proxies, verify=verify_ssl)
                    # 状态码非 200。分两类处理：
                    #   5xx = 服务端明确故障（实测 0.5s 就返回 502），立即放弃并
                    #         上报熔断 —— 重试只会把故障放大成白等。
                    #   4xx = 请求本身有问题，重试同样无用，立即放弃。
                    #   429 = 限流，值得等一下再试（由 urllib3 的 Retry 处理）。
                    # 旧代码在这里无条件 `succeeded = True; break`，等于把 500 当成
                    # 「这一轮成功了」，既不重试也不计入失败，语义完全错乱。
                    if resp.status_code != 200:
                        last_reason = 'HTTP %s' % resp.status_code
                        if resp.status_code == 429 and attempt < 2:
                            time.sleep(3)
                            continue
                        break          # 明确失败：离开 attempt 循环，直接记熔断

                    items = resp.json().get("data") or []
                    if not items:
                        # 去掉空格再搜一次（Bangumi 对空格的匹配很挑剔）
                        payload["keyword"] = clean_name.replace(" ", "")
                        resp = sess.post(url, json=payload, headers=headers,
                                         timeout=10, proxies=proxies, verify=verify_ssl)
                        items = (resp.json().get("data") or []) if resp.status_code == 200 else []

                    # 200 且拿到响应 = 服务可用，即便没有匹配项也算「成功」
                    # （没有匹配项是正常结果，不是故障）
                    _bangumi_note_success()
                    if items:
                        if category == 3:
                            item = items[0]
                            name_lower = str(item.get("name", "")).lower()
                            name_cn_lower = str(item.get("name_cn", "")).lower()
                            is_valid = clean_name in name_lower or clean_name in name_cn_lower
                            if not is_valid:
                                cp = set(clean_name.split())
                                np_ = set(name_lower.replace(":", " ").replace("-", " ").split())
                                if cp and cp.issubset(np_):
                                    is_valid = True
                            if is_valid:
                                result["cn_name"] = item.get("name_cn") or item.get("name")
                                if item.get("summary"):
                                    result["summary"] = item["summary"].replace("\r", "").replace("\n", "")
                        else:
                            for char_data in items[:3]:
                                validated = _validate_bangumi_char(char_data, clean_name, qualifier)
                                if validated:
                                    result["cn_name"] = validated
                                    if char_data.get("summary"):
                                        result["summary"] = char_data["summary"].replace("\r", "").replace("\n", "")[:200]
                                    break
                    return result

                except req.exceptions.SSLError as e:
                    last_reason = 'SSL: %s' % e
                    if verify_ssl:
                        try_ssl_fallback = True   # 允许外层跑 verify=False
                        break
                    if attempt < 2:
                        time.sleep(2)
                except req.exceptions.RequestException as e:
                    # 网络层失败（超时 / 连接重置 / urllib3 重试耗尽）。
                    # 重试 3 次只为兜偶发抖动；但**必须在内层就放弃**，
                    # 否则每个标签都要等满 3 轮 × timeout，实测单标签 120s+，
                    # 一批 8 个并发就是 2 分钟起步 —— 而熔断计数要等整批跑完
                    # 才累加够，等于第一批必然白等。
                    last_reason = '%s: %s' % (type(e).__name__, str(e)[:120])
                    if attempt < 2:
                        time.sleep(2)
                    else:
                        break      # 内层放弃；外层由 try_ssl_fallback 守卫拦住
                except Exception as e:
                    # 解析异常：请求本身是成功的，不计入熔断，避免误跳闸
                    log.error("[LLM] Bangumi 解析异常 (%s): %s", tag_name, e)
                    return result
        finally:
            sess.close()

    _bangumi_note_failure(tag_name, last_reason or '未知原因')
    return result


def _validate_bangumi_char(char_data: dict, clean_name: str, qualifier: str) -> str | None:
    """验证 Bangumi 角色数据，返回有效中文名或 None。"""
    aliases = set()
    raw_name = char_data.get("name")
    default_name = str(raw_name) if raw_name else ""
    if default_name:
        aliases.add(default_name.lower())

    cn_name = default_name
    for info in (char_data.get("infobox") or []):
        if not isinstance(info, dict):
            continue
        key = str(info.get("key", ""))
        val = info.get("value")
        vals = []
        if isinstance(val, str):
            vals.append(val)
        elif isinstance(val, list):
            for v in val:
                if isinstance(v, dict) and "v" in v and v["v"] is not None:
                    vals.append(str(v["v"]))
                elif isinstance(v, str):
                    vals.append(v)
        for v in vals:
            if v:
                aliases.add(v.lower())
        if key in ("简体中文名", "中文名") and vals:
            cn_name = vals[0]

    clean_parts = set(clean_name.split())
    short_name = len(clean_parts) == 1 and len(clean_name) <= 4
    valid = False
    for alias in aliases:
        alias_l = str(alias).lower()
        if clean_name == alias_l:
            valid = True
            break
        if not short_name:
            alias_parts = set(alias_l.replace(",", " ").split())
            if clean_parts and clean_parts.issubset(alias_parts):
                valid = True
                break
    if not valid:
        return None
    if qualifier:
        q_lower = qualifier.lower().replace("_", " ")
        q_words = set(q_lower.split())
        found = False
        for alias in aliases:
            alias_norm = alias.lower().replace("_", " ").replace("-", " ")
            if q_words.issubset(set(alias_norm.split())):
                found = True
                break
        if not found:
            return None
    return cn_name


# ── 标签组工具 ─────────────────────────────────────────────────────────────

def _load_tag_groups(db_path: str) -> tuple[dict, dict]:
    """加载 tag_groups.json，返回 (tag_to_groups, group_cn_names)。"""
    tg_path = Path(db_path).parent / 'tag_groups.json'
    if not tg_path.exists():
        return {}, {}
    try:
        with open(tg_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data.get('tag_to_groups', {}), data.get('group_cn_names', {})
    except Exception as e:
        log.error(f"[LLM] 加载 tag_groups.json 失败: {e}")
        return {}, {}


# ── 共现数据 ────────────────────────────────────────────────────────────────

# 进程级缓存 {tag: [(related, count), ...]}，每个标签固定保留 _COOC_CACHE_DEPTH 条。
# 各调用方的 top_k 不同（prompt_tool 8 / /tag_cooc 20），若把 top_k 写进缓存键，
# 两个调用方交替访问就会每次命中失败、反复付 3.8s 的重载成本。故一次按最大深度建好，
# 调用方各自切片（切片是 O(top_k)，可忽略）。
_COOC_CACHE_DEPTH = 20
_cooc_cache = None
# 缓存键：(parquet 路径, mtime_ns, 大小)。文件被 /trim_cooc 重写后自动失效。
_cooc_cache_key = None
_cooc_file_missing = False  # 只用于日志抑制，不参与缓存判定


def _cooc_parquet_path(db_path: str) -> Path:
    return Path(db_path).parent / 'cooc' / 'cooccurrence_clean.parquet'


def _ensure_cooc_loaded(db_path: str) -> dict:
    """确保 _cooc_cache 已按 _COOC_CACHE_DEPTH 建好，返回**原始缓存对象**（勿改）。

    返回的就是 `_cooc_cache` 本身，不做任何复制/裁剪——这是热点路径：
    缓存里约 5.2 万个标签，`{t: v[:top_k] for ...}` 这种"顺手的"拷贝实测要 98ms
    **每次请求都付一遍**。/tag_cooc 是 Danbooru 页每点一个标签就打一次的接口，
    因此需要单标签查询的调用方请直接用 `_cooc_is_a` 取该标签的切片。
    """
    global _cooc_cache, _cooc_cache_key, _cooc_file_missing
    cooc_path = _cooc_parquet_path(db_path)
    try:
        st = cooc_path.stat()
        key = (str(cooc_path), st.st_mtime_ns, st.st_size)
    except OSError:
        if not _cooc_file_missing:
            log.warning("[LLM] 共现数据不存在，跳过（跑 fetch-cooc 后自动生效，无需重启）")
            _cooc_file_missing = True
        return {}
    _cooc_file_missing = False
    if _cooc_cache is not None and _cooc_cache_key == key:
        return _cooc_cache
    try:
        import numpy as np
        import pandas as pd
        df = pd.read_parquet(cooc_path)
        a, b, c = df['tag_a'].to_numpy(), df['tag_b'].to_numpy(), df['count'].to_numpy()
        n = a.size
        # 双向展开：a→b / b→a 交替，与旧实现的追加顺序一致
        src = np.empty(n * 2, dtype=a.dtype); src[0::2] = a; src[1::2] = b
        tgt = np.empty(n * 2, dtype=a.dtype); tgt[0::2] = b; tgt[1::2] = a
        cnt = np.repeat(c, 2)
        codes, names = pd.factorize(np.concatenate([src, tgt]))
        src_c, tgt_c = codes[:n * 2], codes[n * 2:]
        # 复合键 = 标签码 * 步长 - count，一次排序即得「按标签分组、组内 count 降序」
        sort_key = src_c.astype(np.int64) * (int(cnt.max()) + 1) - cnt.astype(np.int64)
        order = np.argsort(sort_key, kind='stable')
        src_c, tgt_c, cnt = src_c[order], tgt_c[order], cnt[order]
        uniq, starts = np.unique(src_c, return_index=True)
        # 每行在其所属标签组内的位次，位次 < 深度即入选
        pos = np.arange(src_c.size) - starts[np.searchsorted(uniq, src_c)]
        keep = pos < _COOC_CACHE_DEPTH
        lookup = {}
        for tag, related, count in zip(src_c[keep].tolist(),
                                       tgt_c[keep].tolist(),
                                       cnt[keep].tolist()):
            lookup.setdefault(names[tag], []).append((names[related], int(count)))
        _cooc_cache = lookup
        _cooc_cache_key = key
        log.info(f"[LLM] 共现数据加载完成: {len(lookup)} 个标签有共现关系 "
              f"(深度 {_COOC_CACHE_DEPTH})")
        return lookup
    except Exception as e:
        # 不写缓存：parquet 可能正在被 /trim_cooc 原子替换，一次读失败不该钉死整个进程
        log.error(f"[LLM] 加载共现数据失败（不缓存，下次请求重试）: {e}")
        return {}


def _cooc_is_a(db_path: str, tag: str, top_k: int = 10) -> list:
    """单标签共现查询（O(top_k)，不复制整张缓存表）。命中返回 [(related, count), ...]。"""
    top_k = max(1, min(int(top_k or 0) or 10, _COOC_CACHE_DEPTH))
    cache = _ensure_cooc_loaded(db_path)
    if not cache:
        return []
    return cache.get(tag, [])[:top_k]


def _load_cooc_data(db_path: str, top_k: int = 10) -> dict:
    """加载共现数据，返回 {tag: [(related_tag, count), ...]}，每个标签最多 top_k 条。

    实现说明（性能敏感，勿改回逐行写法）：旧实现用 df.iterrows() 逐行累积，
    133 万行需 ~52s；此处先把标签 factorize 成整数码（字符串排序比整数慢约 4 倍），
    再用复合键一次稳定排序，实测 ~3.8s。稳定排序保证并列 count 的次序与旧实现一致。

    缓存按 (路径, mtime, 大小) 失效，**不缓存「文件不存在」和「加载异常」**——
    旧实现把它们永久写成 {}，用户随后 `fetch-cooc` 跑出数据，不重启进程就一直是空的。

    返回的是按 top_k 裁剪后的**新 dict**（约 98ms，5.2 万条）。
    只查单个标签的调用方请用 `_cooc_is_a`，别为了取一条而在这里付全量复制的成本。
    真正需要遍历多标签的批量管线（llm_pipeline 的 payload 构建）用这个接口是对的。
    """
    top_k = max(1, min(int(top_k or 0) or 10, _COOC_CACHE_DEPTH))
    cache = _ensure_cooc_loaded(db_path)
    if not cache:
        return {}
    return {t: v[:top_k] for t, v in cache.items()}


def _invalidate_cooc_cache():
    """让下次 _load_cooc_data 重新读盘。文件被外部重写时用。"""
    global _cooc_cache, _cooc_cache_key
    _cooc_cache = None
    _cooc_cache_key = None


# ── LLM Prompt ─────────────────────────────────────────────────────────────
# 系统提示词统一从 prompts/ 目录读取（llm_entity / llm_general / llm_fallback.txt），
# 支持占位符 {TAG_GROUPS_RULE} / {COOC_RULE}（内容取自 prompts/rules_*.txt）。


def get_system_prompt(key: str) -> str:
    """从 prompts/ 目录读取 LLM 系统提示词并注入规则片段。

    支持两个占位符（读取后自动注入）：
    - {TAG_GROUPS_RULE} → prompts/rules_tag_groups.txt（tag_groups 处理规则）
    - {COOC_RULE}       → prompts/rules_cooc.txt（cooc_tags 共现规则）
    每次调用重新读取（prompts/ 修改后热更新生效）。
    提示词文件缺失或为空时抛 ValueError —— 提示词必须由文件提供。
    """
    text = get_prompt(key)
    if not text:
        raise ValueError(f'提示词文件缺失或为空: prompts/{key}.txt')
    text = text.replace('{TAG_GROUPS_RULE}', get_prompt('rules_tag_groups'))
    text = text.replace('{COOC_RULE}', get_prompt('rules_cooc'))
    return text


# ── LLM 调用层 ─────────────────────────────────────────────────────────────

_CONTEXT_OVERFLOW_KEYWORDS = [
    "context size has been exceeded",
    "context_length_exceeded",
    "too many tokens",
    "maximum context length",
]


class _OutputTruncated(ValueError):
    """输出被 max_tokens 截断（思考模式占满额度、content 为空）。

    继承 ValueError：调用方现有的 `except ValueError` 分支（单条翻译路由等）
    能直接把它转成可见的错误消息，而不是 500。"""


def _llm_max_tokens() -> int:
    """LLM 单次输出上限。与思考模式的 reasoning_content 共享额度——太小会把
    content 挤成空串，且返回空列表让调用方以为「这批本来就没结果」。"""
    return int(os.environ.get('LLM_TEXT_MAX_TOKENS', '8192'))


def _llm_timeout() -> int:
    """单次请求超时（秒）。**必须大于一批的真实生成时间** ——
    实测 batch=8 约 1000 token @10.86 tok/s → 每批约 94 秒。
    设小了每次都超时，而远端在客户端断开后仍会把那批跑完，重试纯属白烧。"""
    return int(os.environ.get('LLM_TEXT_TIMEOUT', '240'))


def _llm_thinking_on() -> bool:
    """是否让模型思考。默认关：思考内容与正式回答共享 max_tokens，
    实测 444 个输出 token 里思考占 410、正式回答只有 30，额度吃光就返回空。"""
    return (os.environ.get('LLM_TEXT_THINKING', 'off').strip().lower()
            in ('on', 'true', '1', 'enabled'))


def _call_llm(client, model: str, system_prompt: str,
              batch_data: list, temperature: float) -> list:
    """调用 LLM，返回 items 列表。

    上下文超限 / 输出被 max_tokens 截断时自动将 batch_data 拆半递归重试，
    不再继续用原大小重试。
    """
    max_attempts = 5
    last_error = None
    for attempt in range(max_attempts):
        try:
            # 超时**起点**来自 .env（LLM_TEXT_TIMEOUT，默认 240s），后续尝试递增。
            # 起点必须大于一批的真实生成时间，否则每次尝试都必然失败。
            # 实测（RTX 4060 笔记本跑 Qwen3.5-9B Q8_0，10.86 tok/s）：
            #   batch=8 每批约 1000 token → eval 93s。
            # 原先是 60 + 30*attempt（60/90/120…），前两次尝试 100% 超时；
            # 更糟的是 llama.cpp 在客户端断开后**仍会把这批跑完**，
            # 所以每次超时重试都在远端重跑一遍 93 秒，队列越堆越长、
            # 界面进度永远不推进（done 只在成功后累加）。
            current_timeout = _llm_timeout() + 120 * attempt
            max_tokens = _llm_max_tokens()
            # 思考模式：关掉时显式告诉端点别思考（额度与正式回答共享）。
            # 不支持的端点会忽略这个参数，不会报错。
            extra = {}
            if not _llm_thinking_on():
                extra['extra_body'] = {'thinking': {'type': 'disabled'}}
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": json.dumps(batch_data, ensure_ascii=False)},
                ],
                temperature=temperature,
                response_format={"type": "json_object"},
                max_tokens=max_tokens,
                timeout=current_timeout,
                **extra,
            )
            finish = response.choices[0].finish_reason
            raw = response.choices[0].message.content
            # content 为空且被截断 = 额度被思考占满，正式回答一个字没出。
            # 必须显式抛错：静默返回 [] 会让调用方以为「这批没有结果」，
            # 整批白跑、不记历史、一轮轮重试，且日志里看不出异常。
            if not (raw or '').strip() and finish == 'length':
                reasoning = getattr(response.choices[0].message, 'reasoning_content', '') or ''
                raise _OutputTruncated(
                    f"输出被 max_tokens({max_tokens}) 截断，content 为空"
                    + (f"（思考占满额度，reasoning 长度 {len(reasoning)}）" if reasoning else '')
                    + "；请调大 .env 的 LLM_TEXT_MAX_TOKENS"
                )
            # 去除 markdown 代码块包裹
            if raw:
                stripped = raw.strip()
                if stripped.startswith("```"):
                    stripped = re.sub(r'^```[a-zA-Z]*\n?', '', stripped)
                    stripped = re.sub(r'\n?```$', '', stripped)
                    raw = stripped.strip()
            _dbg("LLM 响应", raw[:300] if raw else "(空)")
            try:
                parsed = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                try:
                    import json_repair
                    parsed = json_repair.loads(raw) if raw else {}
                except ImportError:
                    log.error(f"[LLM] JSON 解析失败（未安装 json_repair），尝试宽松匹配")
                    match = re.search(r'\{"items":.*?\}\]\}', raw or '', re.DOTALL)
                    if match:
                        parsed = json.loads(match.group())
                    else:
                        preview = (raw or '')[:200]
                        raise ValueError(f"LLM 返回内容无法解析为 JSON: {preview}")
            if isinstance(parsed, list):
                results = parsed
            elif not isinstance(parsed, dict):
                raise ValueError(f"非 dict 类型: {type(parsed).__name__}")
            else:
                # 优先取 items 键，否则取第一个列表值
                items = parsed.get("items")
                if isinstance(items, list):
                    results = items
                else:
                    results = None
                    for v in parsed.values():
                        if isinstance(v, list):
                            results = v
                            break
                    # 平铺对象兜底：可能是单条返回省略了 items 包装
                    if results is None and ("name" in parsed or "cn_name" in parsed):
                        results = [parsed]
                    if results is None:
                        results = []
            # 用原始输入名称覆盖 LLM 可能写错的 name。
            # 必须按名字匹配而非按下标：一批 8 条模型只返 5 条或调换顺序时，
            # 按下标会把 A 的中文名写到 B 上——静默错库，且事后无从分辨。
            by_key = {}
            for entry in batch_data:
                if isinstance(entry, dict) and entry.get("name"):
                    by_key.setdefault(normalize_tag_key(entry["name"]), entry["name"])
            matched, unmatched = [], []
            for item in results:
                if not isinstance(item, dict):
                    continue
                canonical = by_key.get(normalize_tag_key(str(item.get("name") or "")))
                if canonical is None:
                    unmatched.append(item)
                else:
                    item["name"] = canonical
                    matched.append(item)
            # 一条都没对上但条数吻合 → 模型可能整批省略/改写了 name，按输入顺序对齐
            # （batch_size=1 时必然走这里：只有一条，位置无歧义）
            if not matched and unmatched and len(unmatched) == len(batch_data):
                for entry, item in zip(batch_data, unmatched):
                    if isinstance(entry, dict) and entry.get("name"):
                        item["name"] = entry["name"]
                        matched.append(item)
            # 剩余对不上的直接丢弃：调用方按 results 里的 name 记历史，
            # 丢掉即该标签不进历史，下轮重试，不会静默错配
            dropped = len(results) - len(matched)
            if dropped:
                log.warning(f"[LLM] 警告：{dropped} 条结果的名字不在本批输入中，已丢弃（不进历史，下轮重试）")
            return matched
        except Exception as e:
            err_msg = str(e).lower()
            is_truncated = isinstance(e, _OutputTruncated)
            is_overflow = any(kw in err_msg for kw in _CONTEXT_OVERFLOW_KEYWORDS)
            # 截断同样靠「拆小批次」缓解：条目少 → 正式输出短 → 给思考留的余量更大
            if (is_truncated or is_overflow) and len(batch_data) > 1:
                reason = '输出被 max_tokens 截断' if is_truncated else '上下文超限'
                mid = len(batch_data) // 2
                log.warning(f"[LLM] {reason}（batch_size={len(batch_data)} 过大），拆分为 {mid}+{len(batch_data)-mid} 两批递归重试")
                left = _call_llm(client, model, system_prompt, batch_data[:mid], temperature)
                right = _call_llm(client, model, system_prompt, batch_data[mid:], temperature)
                return left + right
            last_error = e
            if attempt == max_attempts - 1:
                log.error(f"[LLM] 请求失败，已重试 {max_attempts} 次: {e}")
                raise
            # 超时类错误要等**更久**再重试，不能立刻重发：
            # llama.cpp 在客户端断开后仍会把这批跑完（日志里 release 才结束），
            # 立刻重试等于把同一份工作再排一次队，让它一直忙在已经没人要的结果上。
            # 等待时间取「刚等的那个超时」，给远端足够时间把手上这单做完。
            _is_timeout = ('timeout' in err_msg or 'timed out' in err_msg)
            if _is_timeout:
                wait = min(2 ** attempt * 30 + random.uniform(0, 5), 300)
                log.warning(f"[LLM] 请求超时 (尝试 {attempt + 1}/{max_attempts})，"
                            f"远端可能仍在生成该批，等 {wait:.0f}s 后再试: {e}")
            else:
                wait = min(2 ** attempt + random.uniform(0, 1), 60)
                log.warning(f"[LLM] 请求出错 (尝试 {attempt + 1}/{max_attempts})，{wait:.1f}s 后重试: {e}")
            time.sleep(wait)
    raise last_error or RuntimeError("LLM 调用异常")


# ── Checkpoint ─────────────────────────────────────────────────────────────

def _load_history(db_path: str) -> set:
    """加载已处理标签历史。"""
    path = Path(db_path).parent / 'checkpoint' / 'llm_history.json'
    if not path.exists():
        return set()
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return set(json.load(f))
    except Exception:
        return set()


def _save_history(db_path: str, names: set):
    path = Path(db_path).parent / 'checkpoint' / 'llm_history.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(sorted(names), f, ensure_ascii=False)


# ── Payload builder ────────────────────────────────────────────────────────

def _extract_chinese_hint(other_names_raw: str) -> str:
    """从 other_names JSON 数组中提取中文别名。"""
    if not other_names_raw:
        return ""
    try:
        names = json.loads(other_names_raw) if isinstance(other_names_raw, str) else other_names_raw
    except Exception:
        return ""
    if not isinstance(names, list):
        return ""
    for name in names:
        if isinstance(name, str) and len(_HANZI_RE.findall(name)) >= 2:
            return name.strip()
    return ""


def _resolve_tag_groups(tag_name: str, tag_to_groups: dict,
                        group_cn_names: dict) -> list[str]:
    """将 tag 的 group ID 转为中文名称。"""
    result = []
    for g in tag_to_groups.get(tag_name, []):
        cn = group_cn_names.get(g, "")
        result.append(cn if cn else g.replace("tag_group:", ""))
    return result


def _build_general_payload(tag: dict, tag_to_groups: dict,
                           group_cn_names: dict,
                           cooc_data: dict = None) -> dict:
    """构建普通标签 payload。cooc_data 可选，为 {tag: [(related, count)]}。"""
    other_names_list = []
    try:
        other_names_list = json.loads(tag['other_names']) if tag['other_names'] else []
    except Exception:
        pass
    cn_hint = _extract_chinese_hint(tag['other_names'])
    tg = _resolve_tag_groups(tag['name'], tag_to_groups, group_cn_names)

    payload = {
        "name": tag['name'],
        "cn_name": tag['cn_name'],
        "other_names": [n for n in other_names_list if isinstance(n, str) and n.strip()],
        "cn_hint": cn_hint,
        "tag_groups": tg,
    }
    if tag.get('en_wiki', '').strip():
        payload["wiki_data"] = tag['en_wiki']
    # 共现
    if cooc_data:
        related = cooc_data.get(tag['name'], [])
        if related:
            payload["cooc_tags"] = [t for t, _ in related]
    return payload


def _build_entity_payload(tag: dict, tag_to_groups: dict,
                          group_cn_names: dict, bangumi_token: str,
                          cooc_data: dict = None) -> dict:
    """构建实体标签 payload（带 Bangumi 查证）。cooc_data 可选。"""
    category = int(tag.get('category', -1))
    other_names_list = []
    try:
        other_names_list = json.loads(tag['other_names']) if tag['other_names'] else []
    except Exception:
        pass

    ref_cn = _extract_chinese_hint(tag['other_names'])
    ref_wiki = tag.get('en_wiki', '')
    bangumi_summary = ""

    if not ref_cn:
        ext_info = _fetch_bangumi_entity(tag['name'], category, bangumi_token)
        if ext_info["cn_name"]:
            ref_cn = ext_info["cn_name"]
            bangumi_summary = ext_info["summary"]
            log.info(f"  [Bangumi] {tag['name']} → {ref_cn}")

    if not ref_wiki and bangumi_summary:
        ref_wiki = bangumi_summary

    tg = _resolve_tag_groups(tag['name'], tag_to_groups, group_cn_names)

    payload = {
        "name": tag['name'],
        "raw_cn_name": tag['cn_name'],
        "ref_cn": ref_cn,
        "ref_wiki": ref_wiki,
        "other_names": [n for n in other_names_list if isinstance(n, str) and n.strip()],
        "tag_groups": tg,
    }
    # 共现
    if cooc_data:
        related = cooc_data.get(tag['name'], [])
        if related:
            payload["cooc_tags"] = [t for t, _ in related]
    return payload


def _build_entity_payloads_batch(batch, tag_to_groups, group_cn_names,
                                  bangumi_token, cooc_data,
                                  max_workers: int = 8) -> list:
    """并行构建一批 entity payloads（Bangumi API 查询自动并发）。

    每个 tag 的 _fetch_bangumi_entity 是独立网络调用，使用 ThreadPoolExecutor
    并行发起，将串行 N 次请求的耗时降低到约 1/max_workers。
    异常时自动降级为普通 payload（不含 ref_cn/ref_wiki）。
    """
    payloads = [None] * len(batch)
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        future_map = {}
        for idx, t in enumerate(batch):
            future = pool.submit(_build_entity_payload, t,
                                 tag_to_groups, group_cn_names,
                                 bangumi_token, cooc_data)
            future_map[future] = idx
        for f in as_completed(future_map):
            idx = future_map[f]
            try:
                payloads[idx] = f.result()
            except Exception as e:
                name = batch[idx].get('name', '?')
                log.error(f"[LLM] 并行 Bangumi 查询失败 ({name}): {e}")
                payloads[idx] = _build_general_payload(
                    batch[idx], tag_to_groups, group_cn_names, cooc_data)
    return payloads


# ── 结果应用 ───────────────────────────────────────────────────────────────

_CN_SEP_RE = re.compile(r"[,，]")


def _combine_cn(base_cn: str, ext_cn: str) -> str:
    """合并基础中文名与扩展中文名：去重 + 统一半角逗号分隔。

    去重的必要性：LLM 常把 base 在 extended_cn_name 里重复一遍（base=透明衣物 /
    ext=透明衣物,透视装），直接拼接会得到「透明衣物,透明衣物,透视装」。
    全角逗号也当分隔符——LLM 两种混用，而前端 cn_name.split(',') 只认半角，
    不拆全角会把「彩虹社，Anycolor」粘成一段显示。保留首次出现顺序。"""
    seen, out = set(), []
    for part in _CN_SEP_RE.split(f"{base_cn or ''},{ext_cn or ''}"):
        part = part.strip()
        if part and part not in seen:
            seen.add(part)
            out.append(part)
    return ','.join(out)


def _apply_results(conn, results: list):
    """将 LLM 结果写入 SQLite。"""
    updated = 0
    for item in results:
        name = item.get("name", "")
        base_cn = str(item.get("cn_name", "")).strip()
        ext_cn = str(item.get("extended_cn_name", "")).strip()
        combined = _combine_cn(base_cn, ext_cn)
        wiki = str(item.get("chinese_wiki", "")).strip()
        nsfw = item.get("nsfw")
        if nsfw is not None:
            try:
                nsfw = int(nsfw)
            except (ValueError, TypeError):
                nsfw = 0
        _update_tag(conn, name,
                    cn_name=combined if combined else None,
                    cn_wiki=wiki if wiki else None,
                    nsfw=nsfw)
        updated += 1
    conn.commit()
    return updated


def translate_one_tag(tag_data: dict, db_path: str = None) -> dict:
    """深度翻译单个标签，复用三层管线逻辑（entity/general/fallback）。

    供主表标签（translation.translate_single_tag）与用户新标签
    （translation.translate_user_tag）共用：层级判定、payload 构建、
    提示词、温度、LLM 调用完全一致。层级由 tag_data 自动决定——
    user_tags 的标签无 en_wiki/category，自然走 fallback 层。

    tag_data: {name, cn_name, en_wiki, category, other_names}（与 _load_tags 返回结构一致）
    返回 {'cn_name': 合并后中文名, 'cn_wiki': 中文 wiki, 'nsfw': int 或 None}。
    LLM_TEXT_API_URL 未配置时抛 ValueError（API Key 可空，本地部署无需配置）；
    提示词缺失由 get_system_prompt 抛 ValueError。
    """
    if db_path is None:
        db_path = get_tag_db_config()['db_path']
    tag_to_groups, group_cn_names = _load_tag_groups(db_path)
    cooc_data = _load_cooc_data(db_path)

    cat = int(tag_data.get('category', -1))
    has_wiki = bool((tag_data.get('en_wiki') or '').strip())
    if cat in (3, 4):
        payload = [_build_entity_payload(
            tag_data, tag_to_groups, group_cn_names,
            os.environ.get('BANGUMI_ACCESS_TOKEN', ''), cooc_data
        )]
        system_prompt = get_system_prompt('llm_entity')
        temperature = 0.1
    elif has_wiki:
        payload = [_build_general_payload(tag_data, tag_to_groups, group_cn_names, cooc_data)]
        system_prompt = get_system_prompt('llm_general')
        temperature = 0.4
    else:
        payload = [_build_general_payload(tag_data, tag_to_groups, group_cn_names, cooc_data)]
        system_prompt = get_system_prompt('llm_fallback')
        temperature = 0.5

    # 本地部署（Ollama / LM Studio / vLLM 等）无需 API Key：空值由 resolve_api_key
    # 归一化为占位串（SDK 2.x 对空串同样抛 OpenAIError）。真正要守的是端点地址。
    base_url = os.environ.get('LLM_TEXT_API_URL', '')
    if not base_url:
        raise ValueError('未配置 LLM_TEXT_API_URL')
    from openai import OpenAI
    client = OpenAI(base_url=base_url,
                    api_key=resolve_api_key(os.environ.get('LLM_TEXT_API_KEY', '')))
    results = _call_llm(client, os.environ.get('LLM_TEXT_MODEL', 'default'),
                        system_prompt, payload, temperature=temperature)

    if not results:
        return {'cn_name': '', 'cn_wiki': '', 'nsfw': None}
    item = results[0]
    cn_name = _combine_cn(str(item.get('cn_name', '')).strip(),
                          str(item.get('extended_cn_name', '')).strip())
    cn_wiki = str(item.get('chinese_wiki', '')).strip()
    nsfw = item.get('nsfw')
    if nsfw is not None:
        try:
            nsfw = int(nsfw)
        except (ValueError, TypeError):
            nsfw = 0
    return {'cn_name': cn_name, 'cn_wiki': cn_wiki, 'nsfw': nsfw}


# ── 主入口 ─────────────────────────────────────────────────────────────────

def run_llm_process(db_path: str = None, preview: bool = False,
                    debug: bool = False, reprocess_wiki_updates: bool = False,
                    batch_size: int = 8):
    global _DEBUG
    _DEBUG = debug

    if db_path is None:
        db_path = get_tag_db_config()['db_path']

    # LLM 客户端（本地部署无需 API Key，空值经 resolve_api_key 归一化为占位串）
    base_url = os.environ.get('LLM_TEXT_API_URL', '')
    api_key = resolve_api_key(os.environ.get('LLM_TEXT_API_KEY', ''))
    model = os.environ.get('LLM_TEXT_MODEL', 'default')
    bangumi_token = os.environ.get('BANGUMI_ACCESS_TOKEN', '')

    if not preview and not base_url:
        log.error("[LLM] 错误：未配置 LLM_TEXT_API_URL")
        return

    from openai import OpenAI
    client = OpenAI(base_url=base_url, api_key=api_key) if not preview else None

    conn = _get_conn(db_path)
    tags = _load_tags(conn)
    log.info(f"[LLM] 本地共 {len(tags)} 条标签")

    # 历史豁免
    history = _load_history(db_path)
    log.info(f"[LLM] 历史已处理: {len(history)} 条")

    # 加载标签组
    tag_to_groups, group_cn_names = _load_tag_groups(db_path)

    # 加载共现数据
    cooc_data = _load_cooc_data(db_path)

    # 分类
    entity_tags = []  # category 3/4
    general_tags = []  # 有 en_wiki
    fallback_tags = []  # 无 en_wiki

    for tag in tags:
        name = tag['name']
        if name in history and not reprocess_wiki_updates:
            continue
        cat = int(tag.get('category', -1))
        has_wiki = bool(tag.get('en_wiki', '').strip())
        has_chinese = bool(tag.get('cn_wiki', '').strip())

        # 如果已经有 wiki/chinese_desc 且不在强制重处理模式，跳过
        if has_chinese and name in history and not reprocess_wiki_updates:
            continue

        if cat in (3, 4):
            entity_tags.append(tag)
        elif has_wiki:
            general_tags.append(tag)
        else:
            fallback_tags.append(tag)

    if preview:
        log.info(f"\n[LLM] 预览 - 待处理统计:")
        log.info(f"  entity（角色/作品）: {len(entity_tags)} 条")
        log.info(f"  general（有 Wiki）: {len(general_tags)} 条")
        log.info(f"  fallback（无 Wiki）: {len(fallback_tags)} 条")
        log.info(f"  总计: {len(entity_tags) + len(general_tags) + len(fallback_tags)} 条")
        conn.close()
        return

    current_run = set()

    # ── Entity 处理 ──────────────────────────────────────────────────────
    if entity_tags:
        log.info(f"\n[LLM] 开始实体处理（{len(entity_tags)} 条）...")
        for i in range(0, len(entity_tags), batch_size):
            batch = entity_tags[i:i + batch_size]
            payload = _build_entity_payloads_batch(batch, tag_to_groups, group_cn_names, bangumi_token, cooc_data)
            log.info(f"[LLM] Entity 进度: {min(i + batch_size, len(entity_tags))}/{len(entity_tags)}")
            results = _call_llm(client, model, get_system_prompt('llm_entity'), payload, temperature=0.1)
            n = _apply_results(conn, results)
            current_run.update(item["name"] for item in results if item.get("name"))

    # ── General 处理 ─────────────────────────────────────────────────────
    if general_tags:
        log.info(f"\n[LLM] 开始常规翻译（{len(general_tags)} 条）...")
        for i in range(0, len(general_tags), batch_size):
            batch = general_tags[i:i + batch_size]
            payload = [_build_general_payload(t, tag_to_groups, group_cn_names, cooc_data)
                       for t in batch]
            log.info(f"[LLM] General 进度: {min(i + batch_size, len(general_tags))}/{len(general_tags)}")
            results = _call_llm(client, model, get_system_prompt('llm_general'), payload, temperature=0.4)
            _apply_results(conn, results)
            current_run.update(item["name"] for item in results if item.get("name"))

    # ── Fallback 处理 ────────────────────────────────────────────────────
    if fallback_tags:
        log.info(f"\n[LLM] 开始无 Wiki 兜底（{len(fallback_tags)} 条）...")
        for i in range(0, len(fallback_tags), batch_size):
            batch = fallback_tags[i:i + batch_size]
            payload = [_build_general_payload(t, tag_to_groups, group_cn_names, cooc_data)
                       for t in batch]
            log.info(f"[LLM] Fallback 进度: {min(i + batch_size, len(fallback_tags))}/{len(fallback_tags)}")
            results = _call_llm(client, model, get_system_prompt('llm_fallback'), payload, temperature=0.5)
            _apply_results(conn, results)
            current_run.update(item["name"] for item in results if item.get("name"))

    # ── 保存历史 ─────────────────────────────────────────────────────────
    if current_run:
        history.update(current_run)
        _save_history(db_path, history)
        log.info(f"[LLM] 已完成 {len(current_run)} 条，历史总计 {len(history)} 条")
    else:
        log.info("[LLM] 没有需要处理的数据")

    conn.close()
    log.info("[LLM] LLM 处理完成")
