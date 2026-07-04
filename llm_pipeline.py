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
import sys
import time
import random
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests as req
import urllib3
from pathlib import Path
from config import get_tag_db_config

# 抑制 verify=False 时的 SSL 警告（Bangumi API 偶发 TLS 兼容性问题）
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
    print(f"[LLM Pipe DEBUG] {label}")
    if content is not None:
        if isinstance(content, (dict, list)):
            print(json.dumps(content, ensure_ascii=False, indent=2)[:500])
        else:
            print(str(content)[:500])


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

def _build_bangumi_session():
    """创建 Bangumi API 专用的 requests Session，带 SSL 降级和重试适配器。"""
    sess = req.Session()
    retries = urllib3.Retry(total=3, backoff_factor=1,
                            allowed_methods=["POST"],
                            status_forcelist=[429, 500, 502, 503, 504])
    adapter = req.adapters.HTTPAdapter(max_retries=retries)
    sess.mount('https://', adapter)
    return sess


def _fetch_bangumi_entity(tag_name: str, category: int, token: str) -> dict:
    """从 Bangumi API 获取实体信息（角色/作品），返回 {cn_name, summary}。"""
    qualifier_match = re.search(r"_\(([^)]+)\)$", str(tag_name))
    qualifier = qualifier_match.group(1) if qualifier_match else ""
    clean_name = re.sub(r"_\(.*\)$", "", str(tag_name)).replace("_", " ").strip().lower()

    headers = {"User-Agent": "TagEditorWeb/1.0", "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    # 与 Danbooru 爬取使用同一代理
    proxy = os.environ.get('DANBOORU_PROXY', '')
    proxies = {'http': proxy, 'https': proxy} if proxy else None
    result = {"cn_name": "", "summary": ""}
    succeeded = False

    # 尝试 SSL 降级兜底：首次正常请求 → 遇 SSL 错误时换 verify=False 再试
    for verify_ssl in (True, False):
        if succeeded:
            break
        if verify_ssl:
            sess = _build_bangumi_session()
        else:
            sess = req.Session()  # verify=False 不需要重试适配器
        for attempt in range(3 if verify_ssl else 1):
            try:
                if category == 3:  # 作品（Bangumi v0 API：POST /v0/search/subjects）
                    url = "https://api.bgm.tv/v0/search/subjects"
                    payload = {"keyword": clean_name}
                    resp = sess.post(url, json=payload, headers=headers, timeout=10, proxies=proxies, verify=verify_ssl)
                    if resp.status_code == 200:
                        items = resp.json().get("data") or []
                        if not items:
                            payload["keyword"] = clean_name.replace(" ", "")
                            resp = sess.post(url, json=payload, headers=headers, timeout=10, proxies=proxies, verify=verify_ssl)
                            items = (resp.json().get("data") or []) if resp.status_code == 200 else []
                        if items:
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
                    succeeded = True
                    break
                elif category == 4:  # 角色
                    url = "https://api.bgm.tv/v0/search/characters"
                    payload = {"keyword": clean_name}
                    resp = sess.post(url, json=payload, headers=headers, timeout=10, proxies=proxies, verify=verify_ssl)
                    if resp.status_code == 200 and not resp.json().get("data"):
                        payload["keyword"] = clean_name.replace(" ", "")
                        resp = sess.post(url, json=payload, headers=headers, timeout=10, proxies=proxies, verify=verify_ssl)
                    if resp.status_code == 200:
                        for char_data in (resp.json().get("data") or [])[:3]:
                            validated = _validate_bangumi_char(char_data, clean_name, qualifier)
                            if validated:
                                result["cn_name"] = validated
                                if char_data.get("summary"):
                                    result["summary"] = char_data["summary"].replace("\r", "").replace("\n", "")[:200]
                                break
                    succeeded = True
                    break
            except req.exceptions.SSLError as e:
                if verify_ssl:
                    # SSL 错误 → 外层循环降级为 verify=False 重试
                    break
                if attempt < 2:
                    time.sleep(2)
                else:
                    print(f"[LLM] Bangumi 网络失败 ({tag_name}): {e}")
            except req.exceptions.RequestException as e:
                if attempt < 2:
                    time.sleep(2)
                else:
                    print(f"[LLM] Bangumi 网络失败 ({tag_name}): {e}")
            except Exception as e:
                print(f"[LLM] Bangumi 解析异常 ({tag_name}): {e}")
                break
        sess.close()
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
        print(f"[LLM] 加载 tag_groups.json 失败: {e}")
        return {}, {}


# ── 共现数据 ────────────────────────────────────────────────────────────────

_cooc_cache = None  # 进程级缓存 {tag: [(related, count), ...]}


def _load_cooc_data(db_path: str, top_k: int = 10) -> dict:
    """加载共现数据，返回 {tag: [(related_tag, count), ...]}，每个标签最多 top_k 条。"""
    global _cooc_cache
    if _cooc_cache is not None:
        return _cooc_cache
    cooc_path = Path(db_path).parent / 'cooc' / 'cooccurrence_clean.parquet'
    if not cooc_path.exists():
        print("[LLM] 共现数据不存在，跳过")
        _cooc_cache = {}
        return _cooc_cache
    try:
        import pandas as pd
        df = pd.read_parquet(cooc_path)
        # tag_a / tag_b / count 三列
        lookup = {}
        for _, row in df.iterrows():
            a, b, c = row['tag_a'], row['tag_b'], row['count']
            for src, tgt in [(a, b), (b, a)]:
                if src not in lookup:
                    lookup[src] = []
                lookup[src].append((tgt, c))
        # 按 count 降序，取 top_k
        for tag in lookup:
            lookup[tag].sort(key=lambda x: -x[1])
            lookup[tag] = lookup[tag][:top_k]
        _cooc_cache = lookup
        print(f"[LLM] 共现数据加载完成: {len(lookup)} 个标签有共现关系")
        return lookup
    except Exception as e:
        print(f"[LLM] 加载共现数据失败: {e}")
        _cooc_cache = {}
        return _cooc_cache


# ── LLM Prompt ─────────────────────────────────────────────────────────────

_TAG_GROUPS_RULE = """
        - `tag_groups`：该标签所属的 Danbooru 分类组列表（已翻译为中文，可能为空）。
          若非空，这些分类词即为搜索锚点，**必须**全部纳入扩展中文名。
          若为空，则根据标签语义自由生成上位概念或同义词。"""

_COOC_RULE = """
        - `cooc_tags`：该标签的常见共现标签列表（常与其一同出现的标签），用于辅助理解标签的语义上下文。
          若非空，可作为翻译和描述时的参考语境。
          若为空，则忽略此项。"""

_SYSTEM_PROMPT_GENERAL = f"""
# Role
你是一个 Danbooru 标签数据库的专家。

# Task
用户会提供一批标签数据，每条包含以下字段：
- `wiki_data`：官方英文描述，可能缺失
- `cn_name`：数据库中已有的中文名，可能为空或机翻错误
- `other_names`：Danbooru Wiki 记录的别名列表（含各语言）
- `cn_hint`：从 other_names 中自动提取的中文别名（若非空，可优先作为中文名参考）
{_TAG_GROUPS_RULE}
{_COOC_RULE}

请完成以下四个动作：

1. **生成中文描述 (chinese_wiki)**:
   - 将 `wiki_data` 里的核心信息完整翻译为中文。
   - 如果 `wiki_data` 为空，请根据知识库写一句该标签的中文视觉描述。
   - 如果知识库中没有相关信息且 `wiki_data` 也无效，返回空字符串。
   - 不要在输出里包含任何字数统计或备注信息，只输出描述本身。

2. **修正中文名 (cn_name)**:
   - 若 `cn_hint` 非空，优先将其作为基础中文名。
   - 否则检查 `cn_name` 是否准确，若存在机翻错误则修正为二次元语境下最准确的基础中文标签名。

3. **扩展中文名 (extended_cn_name)**:
   - 按照上方 `tag_groups` 处理规则生成分类锚点词，再补充 1~2 个同义词或近义词。
   - 只写扩展词，不要包含基础中文名，用半角逗号分隔。
   - 扩展中文名的总数为 2~4 个。

4. **NSFW 判定 (nsfw)**:
   - 包含裸露、性行为、生殖器、恋物癖(Fetish)、血腥暴力则为 1，否则为 0。

必须以合法 JSON 格式输出，结构如下，不要输出任何其他内容：
{{"items": [{{"name": "原始英文名", "cn_name": "修正后的准确基础中文名", "extended_cn_name": "扩展词（逗号分隔）", "chinese_wiki": "中文视觉描述", "nsfw": 0}}]}}
"""

_SYSTEM_PROMPT_FALLBACK = f"""
# Role
你是一个 Danbooru 标签数据库的资深专家。

# Task
用户会提供一批缺失 Wiki 描述的普通标签，每条包含：
- `cn_name`：数据库中已有的中文名，可能为空或机翻错误
- `other_names`：Danbooru Wiki 记录的别名列表
- `cn_hint`：从 other_names 中自动提取的中文别名
{_TAG_GROUPS_RULE}
{_COOC_RULE}

请根据标签英文名、`cn_name`、`other_names` 及你的内部知识库完成：

1. **生成中文描述 (chinese_wiki)**: 解释这个标签的视觉定义或含义，一句简练的中文描述（30字左右）。如果完全无法识别该标签，返回空字符串。
2. **修正中文名 (cn_name)**: 若 `cn_hint` 非空，优先采用。否则检查 `cn_name` 是否准确，修正机翻错误。
3. **扩展中文名 (extended_cn_name)**: 生成分类锚点词 + 1~2 个同义词，半角逗号分隔。总数 2~4 个。
4. **NSFW 判定 (nsfw)**: 包含裸露、性行为等则为 1，否则为 0。

必须以合法 JSON 格式输出：
{{"items": [{{"name": "...", "cn_name": "...", "extended_cn_name": "...", "chinese_wiki": "...", "nsfw": 0}}]}}
"""

_SYSTEM_PROMPT_ENTITY = f"""
# Role
你是一个严谨的 ACG 领域防幻觉整理专家。

# Task
用户会提供角色名/作品名的标签数据，每条包含：
- `ref_cn`：外部数据源（Bangumi）给出的官方中文名，可能为空
- `ref_wiki`：外部数据源的简介，可能为空
- `other_names`：Danbooru Wiki 记录的别名列表
- `raw_cn_name`：数据库中已有的中文名（可能为空或机翻错误）
{_TAG_GROUPS_RULE}
{_COOC_RULE}

请完成：

1. **生成中文描述 (chinese_wiki)**: 将 `ref_wiki` 完整翻译为中文简述。若为空，根据知识库写约 50 字简介。
2. **确定中文名 (cn_name)**: 优先级：`ref_cn` > `other_names` 中的中文名 > 你的知识库。
   如果 `ref_cn` 存在，直接采纳。如果以上均为空，且你对该角色/作品的官方汉字名有把握则填写，否则保留原英文名。
3. **扩展中文名 (extended_cn_name)**: 生成分类锚点词 + 所属作品名/阵营/常见别名（1~2 个），半角逗号分隔。
4. **NSFW 判定 (nsfw)**: 包含裸露、性暗示等则为 1，否则为 0。

输出格式（合法 JSON）：
{{"items": [{{"name": "原始英文名", "cn_name": "确定的基础中文名", "extended_cn_name": "扩展词", "chinese_wiki": "中文简介", "nsfw": 0}}]}}
"""

# ── LLM 调用层 ─────────────────────────────────────────────────────────────

_CONTEXT_OVERFLOW_KEYWORDS = [
    "context size has been exceeded",
    "context_length_exceeded",
    "too many tokens",
    "maximum context length",
]


def _call_llm(client, model: str, system_prompt: str,
              batch_data: list, temperature: float) -> list:
    """调用 LLM，返回 items 列表。

    上下文超限时自动将 batch_data 拆半递归重试，不再继续用原大小重试。
    """
    max_attempts = 5
    last_error = None
    for attempt in range(max_attempts):
        try:
            current_timeout = 60 + 30 * attempt
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": json.dumps(batch_data, ensure_ascii=False)},
                ],
                temperature=temperature,
                response_format={"type": "json_object"},
                timeout=current_timeout,
            )
            raw = response.choices[0].message.content
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
                    print(f"[LLM] JSON 解析失败（未安装 json_repair），尝试宽松匹配")
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
            # 用原始输入名称覆盖 LLM 可能写错的 name
            for i, item in enumerate(results):
                if i < len(batch_data) and isinstance(batch_data[i], dict) and "name" in batch_data[i]:
                    item["name"] = batch_data[i]["name"]
            return results
        except Exception as e:
            err_msg = str(e).lower()
            is_overflow = any(kw in err_msg for kw in _CONTEXT_OVERFLOW_KEYWORDS)
            if is_overflow and len(batch_data) > 1:
                mid = len(batch_data) // 2
                print(f"[LLM] 上下文超限（batch_size={len(batch_data)} 过大），拆分为 {mid}+{len(batch_data)-mid} 两批递归重试")
                left = _call_llm(client, model, system_prompt, batch_data[:mid], temperature)
                right = _call_llm(client, model, system_prompt, batch_data[mid:], temperature)
                return left + right
            last_error = e
            if attempt == max_attempts - 1:
                print(f"[LLM] 请求失败，已重试 {max_attempts} 次: {e}")
                raise
            wait = min(2 ** attempt + random.uniform(0, 1), 60)
            print(f"[LLM] 请求出错 (尝试 {attempt + 1}/{max_attempts})，{wait:.1f}s 后重试: {e}")
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
            print(f"  [Bangumi] {tag['name']} → {ref_cn}")

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
                print(f"[LLM] 并行 Bangumi 查询失败 ({name}): {e}")
                payloads[idx] = _build_general_payload(
                    batch[idx], tag_to_groups, group_cn_names, cooc_data)
    return payloads


# ── 结果应用 ───────────────────────────────────────────────────────────────

def _combine_cn(base_cn: str, ext_cn: str) -> str:
    return re.sub(r",+", ",", ",".join(filter(None, [base_cn, ext_cn])).strip(","))


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


# ── 主入口 ─────────────────────────────────────────────────────────────────

def run_llm_process(db_path: str = None, preview: bool = False,
                    debug: bool = False, reprocess_wiki_updates: bool = False,
                    batch_size: int = 20):
    global _DEBUG
    _DEBUG = debug

    if db_path is None:
        db_path = get_tag_db_config()['db_path']

    # LLM 客户端
    api_key = os.environ.get('LLM_API_KEY', '')
    base_url = os.environ.get('LLM_API_URL', '')
    model = os.environ.get('LLM_MODEL', 'default')
    bangumi_token = os.environ.get('BANGUMI_ACCESS_TOKEN', '')

    if not preview and not api_key:
        print("[LLM] 错误：未配置 LLM_API_KEY")
        return

    from openai import OpenAI
    client = OpenAI(base_url=base_url, api_key=api_key) if not preview else None

    conn = _get_conn(db_path)
    tags = _load_tags(conn)
    print(f"[LLM] 本地共 {len(tags)} 条标签")

    # 历史豁免
    history = _load_history(db_path)
    print(f"[LLM] 历史已处理: {len(history)} 条")

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
        print(f"\n[LLM] 预览 - 待处理统计:")
        print(f"  entity（角色/作品）: {len(entity_tags)} 条")
        print(f"  general（有 Wiki）: {len(general_tags)} 条")
        print(f"  fallback（无 Wiki）: {len(fallback_tags)} 条")
        print(f"  总计: {len(entity_tags) + len(general_tags) + len(fallback_tags)} 条")
        conn.close()
        return

    current_run = set()

    # ── Entity 处理 ──────────────────────────────────────────────────────
    if entity_tags:
        print(f"\n[LLM] 开始实体处理（{len(entity_tags)} 条）...")
        for i in range(0, len(entity_tags), batch_size):
            batch = entity_tags[i:i + batch_size]
            payload = _build_entity_payloads_batch(batch, tag_to_groups, group_cn_names, bangumi_token, cooc_data)
            print(f"[LLM] Entity 进度: {min(i + batch_size, len(entity_tags))}/{len(entity_tags)}")
            results = _call_llm(client, model, _SYSTEM_PROMPT_ENTITY, payload, temperature=0.1)
            n = _apply_results(conn, results)
            current_run.update(item["name"] for item in results if item.get("name"))

    # ── General 处理 ─────────────────────────────────────────────────────
    if general_tags:
        print(f"\n[LLM] 开始常规翻译（{len(general_tags)} 条）...")
        for i in range(0, len(general_tags), batch_size):
            batch = general_tags[i:i + batch_size]
            payload = [_build_general_payload(t, tag_to_groups, group_cn_names, cooc_data)
                       for t in batch]
            print(f"[LLM] General 进度: {min(i + batch_size, len(general_tags))}/{len(general_tags)}")
            results = _call_llm(client, model, _SYSTEM_PROMPT_GENERAL, payload, temperature=0.4)
            _apply_results(conn, results)
            current_run.update(item["name"] for item in results if item.get("name"))

    # ── Fallback 处理 ────────────────────────────────────────────────────
    if fallback_tags:
        print(f"\n[LLM] 开始无 Wiki 兜底（{len(fallback_tags)} 条）...")
        for i in range(0, len(fallback_tags), batch_size):
            batch = fallback_tags[i:i + batch_size]
            payload = [_build_general_payload(t, tag_to_groups, group_cn_names, cooc_data)
                       for t in batch]
            print(f"[LLM] Fallback 进度: {min(i + batch_size, len(fallback_tags))}/{len(fallback_tags)}")
            results = _call_llm(client, model, _SYSTEM_PROMPT_FALLBACK, payload, temperature=0.5)
            _apply_results(conn, results)
            current_run.update(item["name"] for item in results if item.get("name"))

    # ── 保存历史 ─────────────────────────────────────────────────────────
    if current_run:
        history.update(current_run)
        _save_history(db_path, history)
        print(f"[LLM] 已完成 {len(current_run)} 条，历史总计 {len(history)} 条")
    else:
        print("[LLM] 没有需要处理的数据")

    conn.close()
    print("[LLM] LLM 处理完成")
