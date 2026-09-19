# -*- coding: utf-8 -*-
"""提示词优化器（/prompt_tool）：图片 + 提示词 + 优化要求 → 以标签库为知识库重调提示词。

提示词是**两段式**：标签行 + 自然语言描述段。实测真实文件（uploads/HRdo_aAbQAIZrm6..txt）
1808 字符、只有 1 个换行、50 个逗号块——第 0~35 块是真标签，换行落在第 36 块中间，
第 37~49 块是散文段（英文句读的逗号被逗号切分炸成 14 个假标签）。所以：
**首个换行是标签与描述的唯一可靠分界，逗号切分只作用于标签行。**

用户的输入是**对这份提示词的优化要求**，不一定是画面效果 —— 还可能是精炼、清理假标签、
按图校正、调整动作、去重/规范格式等元操作。所以**不在本地猜意图**（硬编码中文关键词表对
「精简一下」抽出的词是 `提示词/精简/一下`，搜 cn_name 全是噪声）：先跑一次纯文本「规划」调用，
由模型判断意图并点名要查哪些工具，本地执行（零 LLM），再带图改写。

三次模型调用：
  ① `_call_planner`    纯文本规划（不带图、便宜）→ {intent, understanding, plan, tools:[{tool, ...}]}
  ② `_call_vlm_json`   带图综合改写（payload 里带 ① 的意图 + 工具结果）
  ③ `_call_llm_repair` 未收录标签修补（仅当 ② 产出了库内查不到的标签）

四个工具全部复用现成函数，不新写检索逻辑（见 _TOOL_SPECS）：
  search_tags → `_tool_search_tags`（中文走 cn_name 首段/FTS，英文走 build_tag_db.search_tags）
  tag_detail  → `classify_tags`
  cooc        → `cooc_recommendations`
  tag_groups  → data/tag_groups.json（复用 translation._load_tag_groups_cache 的进程级缓存）

本页**不写入任何文件**：产出只供复制（原 `/save_prompt_result` 已删除，本页与标签编辑功能独立）。
"""
import base64
import io
import json
import os
import re
import time

from flask import Blueprint, Response, current_app, jsonify, request

from tageditor.core.config import get_prompt, get_prompt_tool_config, get_tag_db_config, is_within_directory, safe_filename
from tageditor.core.sse_utils import sse_event
import logging


log = logging.getLogger(__name__)

prompt_tool_bp = Blueprint('prompt_tool', __name__)

# ── 常量 ───────────────────────────────────────────────────────────────────

# 标签库不收的质量/元标签类目（sync 只收 category∈{0,3,4} 且 post_count>=100）。
# 命中即「库不收 ≠ 编造」，默认保留，不允许因为"查不到"就删。
_META_TAGS = {
    'masterpiece', 'best_quality', 'good_quality', 'normal_quality', 'worst_quality',
    'low_quality', 'bad_quality', 'high_quality', 'very_aesthetic', 'aesthetic',
    'highres', 'absurdres', 'lowres', 'newest', 'oldest', 'recent', 'quality',
    'bad_proportions', 'bad_anatomy', 'bad_hands', 'very_bad_quality', 'error',
    'jpeg_artifacts', 'signature', 'watermark', 'artist_name', 'username',
}

# 中文关键词抽取的停用词（n-gram 里出现即丢弃）
_CN_STOPWORDS = {
    '想要', '希望', '效果', '风格', '感觉', '一点', '一些', '稍微', '比较', '非常', '更加',
    '不要', '需要', '应该', '尽量', '有点', '可以', '变成', '改成', '换成', '保持', '增加',
    '减少', '画面', '图片', '照片', '角色', '人物', '整体', '现在', '还是', '就是', '一个',
    '什么', '怎么', '这样', '那样', '时候', '并且', '而且', '但是', '如果', '让她', '让他',
}

# 新增标签数量（与 prompts/prompt_adjust.txt 的硬规则 4 保持一致）
_MAX_ADD = 12

# 规划器可点名的工具清单：白名单 + 参数说明 + 返回上限。
# 每个工具只吃**一个列表参数**（`param`），因为四次检索都是「给我一批词，还你一批标签」的形状。
# 这份 dict 同时是三个地方的唯一出处：规划提示词的 available_tools、参数裁剪的 bounds、
# 以及执行时的分派 —— 加工具只改这里（记得同时更新 prompts/prompt_planner.txt 的选择准则）。
_TOOL_SPECS = {
    'search_tags': {
        'param': 'keywords', 'bound': 6, 'limit': 48,
        'example': ['侧躺', '室内夜景'],
        'desc': '按关键词检索标签库里的真实标签（中文英文都可以）。要求涉及画面内容/风格/动作时用它找词。'
                '已在提示词里的标签不会重复返回（它们的含义见 input_prompt）',
    },
    'tag_detail': {
        'param': 'names', 'bound': 40, 'limit': 40,
        'example': ['backlighting', 'black_bowtie'],
        'desc': '查一批标签的确切含义（中文名/分类/热度/wiki 摘要）与是否收录，用来判断某个标签到底指什么、'
                '以及未收录的标签有没有近似真实项',
    },
    'cooc': {
        'param': 'seeds', 'bound': 30, 'limit': 16,
        'example': ['lying', 'on_bed'],
        'desc': '给定一批已有标签，返回库里常与它们同时出现的标签（按 lift 排序）。用于按图补缺、动作/场景补全',
    },
    'tag_groups': {
        'param': 'names', 'bound': 40, 'limit': 20,
        'example': ['red_lingerie', 'red_bra'],
        'desc': '返回标签所在的 Danbooru 标签组及同组标签。同组常有上下位/近义关系，是判断「冗余、可合并」的硬依据。'
                '注意标签组只覆盖一部分标签，查不到不等于有冗余',
    },
}

# 工具结果的总字符预算（进 ② 的 payload，与其他知识一起挤 max_tokens）
_TOOL_RESULT_CHARS = 12000

# tag_groups **每个标签**的成员预算（防止 people 这种 1831 人的大组撑爆 payload）
_TG_MEMBER_BUDGET = 500
_TG_MAX_GROUPS_PER_TAG = 3

# 中文描述性虚词：命中即判定该块是「诉求/描述」而非标签（只在无换行的回退路径上用）
_PROSE_MARKERS = ('想要', '希望', '感觉', '氛围', '效果', '风格', '尽量', '不要', '一点',
                  '稍微', '应该', '然后', '而且', '但是', '这样', '那样')

_CJK_RE = re.compile(r'[一-鿿]')
_HANZI_RE = re.compile(r'[一-鿿]')
# 权重语法：(tag:1.2) / (tag) / [tag]
_WEIGHT_PAREN_RE = re.compile(r'^\(\s*(.+?)\s*(?::\s*([0-9]*\.?[0-9]+)\s*)?\)$')
_WEIGHT_BRACKET_RE = re.compile(r'^\[\s*(.+?)\s*\]$')
# 权重的可接受区间。模型偶尔返回 0 / 负数 / 9.9 这类值，
# 0 会让该标签在前端被完全抹掉，越界值则是明显的输出错误。
_WEIGHT_MIN, _WEIGHT_MAX = 0.1, 2.0


class _PromptFatal(ValueError):
    """致命错误：模型失败 / 输出不可解析 / 被 max_tokens 截断。

    继承 ValueError，路由里 except 后转成 SSE 的 fatal 事件并 return 终止 generator。"""


class _UnsupportedResponseFormat(Exception):
    """服务端不支持 response_format —— 触发降级重试，不是真错误。"""


# ── ① 解析输入提示词 ───────────────────────────────────────────────────────

def _split_by_comma(text):
    """按半角/全角逗号切分（cn_name 与用户输入都混用两种逗号）。"""
    return [p for p in re.split(r'[,，]', text)]


def _sanitize_weight(value, fallback=None):
    """把模型返回的 weight 收敛成合法浮点数或 None（= 无权重语法）。

    模型经常返回 null / "" / "1.2" / 越界值。直接透传会让前端拼出
    `(tag:null)` 这种坏语法；把 None 当成「无权重」而不是 1.0，
    是因为「显式写了 (tag:1.0)」和「没写权重」在用户提示词里是不同的东西。
    """
    if value is None or value == '':
        return fallback
    try:
        w = float(value)
    except (TypeError, ValueError):
        return fallback
    if w != w or w in (float('inf'), float('-inf')):  # NaN / inf
        return fallback
    return min(max(w, _WEIGHT_MIN), _WEIGHT_MAX)


def _parse_tag_chunk(chunk):
    """解析单个标签块，保留权重语法（丢了等于偷改用户提示词）。

    返回 {'raw', 'tag', 'weight'}；空块返回 None。
    """
    raw = (chunk or '').strip()
    if not raw:
        return None
    weight = 1.0
    inner = raw
    m = _WEIGHT_PAREN_RE.match(raw)
    if m:
        inner = m.group(1).strip()
        if m.group(2):
            try:
                weight = float(m.group(2))
            except ValueError:
                weight = 1.0
    else:
        m = _WEIGHT_BRACKET_RE.match(raw)
        if m:
            inner = m.group(1).strip()
            weight = 0.9  # [tag] 在多数前端里等价于 (tag:0.9)
    if not inner:
        return None
    return {'raw': raw, 'tag': inner, 'weight': weight}


def _looks_like_prose(chunk):
    """散文判定：去空白后 >80 字符，或英文词数 >8，或含句读 '. '，或中文长句/含描述性虚词。

    仅在**没有换行**的输入上作为回退使用——那时无法靠结构分界，只能按块猜。
    中文拿捏：中文标签一般很短（白发 / 双马尾 / 黑色过膝袜），而「想要黄昏逆光的氛围感」
    这种是诉求不是标签。判错方向的代价不对称——把描述当标签会污染 diff（多出一条无法校验的
    uncollected 假标签并混进输出），把标签当描述只是少一次中文转英文，故宁偏描述。
    """
    text = (chunk or '').strip()
    if not text:
        return False
    if len(text) > 80:
        return True
    if len(text.split()) > 8:
        return True
    if '. ' in text:
        return True
    if _CJK_RE.search(text):
        if len(text) >= 8:
            return True
        if any(m in text for m in _PROSE_MARKERS):
            return True
    return False


def _split_prompt_entries(raw):
    """解析用户提示词 → {'mode', 'tags': [...], 'prose': str}。

    mode ∈ {both, tags_only, prose_only, empty}。
    换行是硬分界：换行前 = 标签行（按逗号切），换行后 = 描述段（**整段保留，绝不按逗号切**）。
    无换行时回退到逐块散文判定，一旦出现散文块，其后所有块都归入描述段。
    """
    text = (raw or '').replace('\r\n', '\n').replace('\r', '\n').strip()
    if not text:
        return {'mode': 'empty', 'tags': [], 'prose': ''}

    if '\n' in text:
        head, _, tail = text.partition('\n')
        tags = [e for e in (_parse_tag_chunk(c) for c in _split_by_comma(head)) if e]
        prose = tail.strip()
    else:
        tags, prose_parts, prose_started = [], [], False
        for chunk in _split_by_comma(text):
            if prose_started or _looks_like_prose(chunk):
                prose_started = True
                if chunk.strip():
                    prose_parts.append(chunk.strip())
            else:
                e = _parse_tag_chunk(chunk)
                if e:
                    tags.append(e)
        prose = ', '.join(prose_parts)

    if tags and prose:
        mode = 'both'
    elif tags:
        mode = 'tags_only'
    elif prose:
        mode = 'prose_only'
    else:
        mode = 'empty'
    return {'mode': mode, 'tags': tags, 'prose': prose}


# ── 检索原语（刻意不复用 search_tags）────────────────────────────────────────

def _conn_exec(conn, sql, params):
    """执行查询并把行转成 dict（连接未设置 row_factory）。"""
    cur = conn.execute(sql, params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _batch_cn_first_segment(conn, terms, per_term=8):
    """按 cn_name 首段批量反查中文输入 → {term: [row, ...]}。

    **不用 search_tags**：它的 MATCH 里 OR 了 other_names，中文被别名污染
    （搜 '白发' 首位是 red_eyes，'和服' 混进 leotard）。
    cn_name 首段同时兼容半角/全角逗号（实测 1girl = 单人少女,单人女性，一位女孩，独居少女）。

    写法用**范围比较而非 LIKE**：`cn_name >= '词,' AND cn_name < '词/'` 能走
    idx_tags_cn_name 的索引范围扫描，N 个词的 OR 由 SQLite 用 MULTI-INDEX OR 合并
    （5 词实测 0.09ms）；原先的 `LIKE '词,%'` 是非前缀通配，SQLite 无法优化，
    退化成 SCAN tags（同条件 110ms，差 1200 倍）。',' 是 0x2C、全角'，' 是 U+FF0C，
    上界取 '词' + chr(0x2C+1) 即可覆盖所有以「词,」开头的值。
    """
    terms = [t for t in dict.fromkeys(terms) if t]
    if not terms:
        return {}
    # 每个词一组 (精确 | [词, 半角逗号) | [词， 全角逗号+1)，组间 OR
    clause = "(cn_name >= ? AND cn_name < ?) OR cn_name = ? OR (cn_name >= ? AND cn_name < ?)"
    params = []
    for t in terms:
        params.extend([t + ',', t + chr(0x2C + 1), t,
                       t + '，', t + chr(0xFF0C + 1)])
    sql = (f"SELECT name, cn_name, category, post_count FROM tags "
           f"WHERE {' OR '.join(['(' + clause + ')'] * len(terms))}")
    rows = _conn_exec(conn, sql, params)

    term_set = set(terms)
    buckets = {}
    for row in rows:
        cn = row.get('cn_name') or ''
        first = re.split(r'[,，]', cn, 1)[0].strip()
        if first in term_set:
            buckets.setdefault(first, []).append(row)
    for k in buckets:
        buckets[k].sort(key=lambda r: (r.get('post_count') or 0), reverse=True)
        buckets[k] = buckets[k][:per_term]
    return buckets


def _search_cn_fts(conn, kw, limit=8):
    """cn_name 单列 FTS5 trigram 匹配（≥3 字符，2~4ms）。

    刻意不复用 search_tags：它的 `OR other_names` 会引入别名噪音。
    """
    kw = (kw or '').strip()
    if len(kw) < 3:
        return []
    try:
        q = 'cn_name:"{}"'.format(kw.replace('"', '""'))
        return _conn_exec(conn, """
            SELECT name, cn_name, category, post_count FROM tags WHERE rowid IN (
                SELECT rowid FROM tags_fts WHERE tags_fts MATCH ?
            ) ORDER BY post_count DESC, length(name), name LIMIT ?
        """, (q, limit))
    except Exception:
        return []


def _extract_terms(text, limit=24):
    """从中文要求里抽关键词（纯规则、零成本、确定性）。

    只服务于「规划器失败时的兜底检索」（见 /prompt_adjust 阶段 4），别处不再依赖它：
    主路径是模型自己点名工具，规则抽词只是它挂掉时的保险。

    中文连续段生成 **2~4 字** n-gram（跳过停用词与含停用词的 gram），英文词原样保留。
    抽出的词是拿去和 `cn_name` 做**首段 / 子串**匹配的（`_batch_cn_first_segment`、
    `_search_cn_fts`），所以**短词优先**：5 字以上的长 gram 在中文标签名里几乎不存在，
    长词优先只会把 limit 名额占满 —— 实测「白发少女在窗边逆光」12 个名额全被 5~6 字
    gram 吃掉，命中 0 条；短词优先则能拿到 `白发 / 少女 / 窗边 / 逆光`。
    """
    if not text:
        return []
    terms = []
    # 英文词是完整词，命中率最高，先占名额
    for word in re.findall(r'[A-Za-z][A-Za-z_\-]{2,}', text):
        w = word.lower()
        if w not in terms:
            terms.append(w)
    for run in re.findall(r'[一-鿿]+', text):
        n = len(run)
        for size in range(2, min(4, n) + 1):
            for i in range(n - size + 1):
                gram = run[i:i + size]
                if gram in _CN_STOPWORDS:
                    continue
                if any(sw in gram for sw in _CN_STOPWORDS):
                    continue
                if gram not in terms:
                    terms.append(gram)
    return terms[:limit]


# ── ③ 标签校验（含输入侧含义注入）───────────────────────────────────────────

def _tag_suggestions(conn, name, limit=3):
    """给未收录的「像标签的」名字取库内近似项。

    只对短名跑（长句跑 LIKE 没意义还慢）。中文允许：中文标签很短（白发/背光），
    ≥3 字走 cn_name FTS、2 字走全表 LIKE，都很快——**输入提示词里混中文是常态**。
    """
    norm = name.strip().lower().replace(' ', '_')
    if not norm or len(norm.split('_')) > 4 or len(norm) > 40:
        return []
    if _CJK_RE.search(norm) and len(norm) > 12:
        return []
    try:
        from tageditor.db.build_tag_db import search_tags
        return [{'name': r['name'], 'cn_name': r.get('cn_name') or ''}
                for r in search_tags(conn, norm, limit)]
    except Exception:
        return []


def classify_tags(conn, names, cfg, with_wiki=False):
    """批量分类标签 → {规范化 name: {status, cn_name, category, post_count, ...}}。

    status: in_db / user_tag / quality_meta / uncollected
    只用主表 + user_tags 判定，**不做** cn_name 回落猜测。
    """
    if not names:
        return {}
    from tageditor.db.build_tag_db import lookup_tags, lookup_user_tags, normalize_tag_key

    norm_list = []
    for n in names:
        k = normalize_tag_key(n)
        if k and k not in norm_list:
            norm_list.append(k)

    main = lookup_tags(conn, norm_list)
    user = lookup_user_tags(conn, [k for k in norm_list if k not in main])

    result = {}
    for k in norm_list:
        info = main.get(k)
        if info:
            item = {
                'status': 'in_db',
                'cn_name': info.get('cn_name') or '',
                'category': info.get('category'),
                'post_count': info.get('post_count') or 0,
            }
            hint = ''
            try:
                from tageditor.translate.llm_pipeline import _extract_chinese_hint
                hint = _extract_chinese_hint(info.get('other_names'))
            except Exception:
                hint = ''
            if hint:
                item['cn_hint'] = hint
            if with_wiki and info.get('en_wiki'):
                item['wiki_excerpt'] = info['en_wiki'][:cfg['wiki_chars']]
            result[k] = item
            continue
        u = user.get(k)
        if u:
            result[k] = {'status': 'user_tag', 'cn_name': u.get('cn_name') or ''}
            continue
        if k in _META_TAGS:
            result[k] = {'status': 'quality_meta'}
            continue
        result[k] = {'status': 'uncollected', 'suggestions': _tag_suggestions(conn, k)}
    return result


def _trim_knowledge(entries, limit=8000):
    """token 预算：整块超出 limit 字符时，按 post_count 升序先丢 wiki_excerpt，再丢 cn_hint。"""
    def size():
        return len(json.dumps(entries, ensure_ascii=False))

    if size() <= limit:
        return entries
    order = sorted(range(len(entries)), key=lambda i: entries[i].get('post_count') or 0)
    for drop in ('wiki_excerpt', 'cn_hint'):
        for i in order:
            if drop in entries[i]:
                entries[i].pop(drop)
                if size() <= limit:
                    return entries
    return entries


def build_input_knowledge(conn, entries, cfg):
    """给输入的标签逐条补上「库里的确切含义」，让模型知道每个词在 Danbooru 里指什么。

    两条补充链路（都在这一层做完，模型不必猜）：
    - 中文输入（白发 / 背光）→ `_batch_cn_first_segment` 反查库内英文标签，附 cn_match，
      模型据此产出 op:'modify' 的中文转英文（用户可拒绝）
    - 未收录的英文输入 → 给近似真实标签（同 validate_tags 口径）
    """
    from tageditor.db.build_tag_db import normalize_tag_key
    names = [e['tag'] for e in entries]
    knowledge = classify_tags(conn, names, cfg, with_wiki=True)

    cn_matches = {}
    cn_terms = []
    for e in entries:
        k = normalize_tag_key(e['tag'])
        if _CJK_RE.search(e['tag']) and (knowledge.get(k) or {}).get('status') == 'uncollected':
            if e['tag'] not in cn_terms:
                cn_terms.append(e['tag'])
    if cn_terms:
        for term, rows in _batch_cn_first_segment(conn, cn_terms, per_term=3).items():
            cn_matches[term] = [{'name': r['name'], 'cn_name': r.get('cn_name') or ''} for r in rows]

    out = []
    for e in entries[:cfg['max_input_tags']]:
        k = normalize_tag_key(e['tag'])
        item = {'raw': e['raw'], 'tag': e['tag'], 'weight': e['weight']}
        item.update(knowledge.get(k) or {'status': 'uncollected'})
        item.pop('suggestions', None)   # 输入侧统一用 cn_match / 输入侧不需要建议列表
        if e['tag'] in cn_matches:
            item['cn_match'] = cn_matches[e['tag']]
        out.append(item)
    return _trim_knowledge(out), knowledge


def validate_tags(conn, names, cfg):
    """校验模型输出的标签是否真实存在。suggestions 只在未收录时给。"""
    return classify_tags(conn, names, cfg, with_wiki=False)


# ── ④ 共现推荐 ─────────────────────────────────────────────────────────────

_tags_total_cache = None


def _invalidate_tags_total():
    """标签库被重建/同步后调用，让下次 _tags_total 重新计数。"""
    global _tags_total_cache
    _tags_total_cache = None


def _tags_total(conn):
    """tags 表总行数（lift 的 N），进程级缓存。

    只缓存成功结果：库被 build_tag_db.py init 重建、或 sync 同步进上万条新标签后，
    行数会显著变化，而 lift = P(a,b)/(P(a)P(b)) 里的 N 直接参与分母。旧实现把
    一次 count 永久钉死，重建后的 lift 会系统性偏大。异常时返回 1 且**不缓存**——
    缓存 1 会让后续所有请求都用一个错误的分母算出巨大的 lift。"""
    global _tags_total_cache
    if _tags_total_cache is None:
        try:
            _tags_total_cache = conn.execute("SELECT count(*) FROM tags").fetchone()[0] or 1
        except Exception:
            return 1
    return _tags_total_cache


def cooc_recommendations(conn, seeds, exclude, cfg, warnings):
    """真实共现推荐，按 **lift** 排序。

    实测裸 count 会把 1girl 引向 three-tone_hair(778670)/music/multiple_penises；
    lift = count*N/(post_count_a*post_count_b) 才捞得出 cat_ears→cat_tail、
    squatting→spread_legs 这类有信息量的补充。count<200 的一律丢（长尾噪声）。

    两道一致性防线（实测驱动，别删）：
    - count > 该标签自己的 post_count 是不可能的（共现数不可能超过标签自身出现数），
      出现即说明 parquet 与 tags 表的统计口径对不上——实测 three-tone_hair 在 tags 表里
      post_count=237，却与 1girl 共现 778670 次，lift 被抬到 20.78 霸榜。这类行一律丢。
    - 候选自身 post_count < cfg['cooc_min_post']（config._PROMPT_COOC_MIN_POST，500）的冷门标签，
      lift 天然虚高。该常量只是这条防线的下限，**不是**可在 .env 里调的口径开关。
    """
    from tageditor.db.build_tag_db import normalize_tag_key
    db_path = get_tag_db_config()['db_path']
    try:
        from tageditor.translate.llm_pipeline import _load_cooc_data
        cooc = _load_cooc_data(db_path, top_k=cfg['cooc_topk'])
    except Exception as e:
        warnings.append(f'共现数据加载失败，已跳过推荐：{e}')
        return []
    if not cooc:
        return []

    seed_keys = [normalize_tag_key(s) for s in seeds]
    seed_keys = [s for s in seed_keys if s]
    if not seed_keys:
        return []

    agg = {}   # name -> {'count': 最大共现次数, 'seed_hits': set, 'seed': 该 count 对应的 seed}
    for s in seed_keys:
        for rel, cnt in cooc.get(s, []):
            if cnt < 200:
                continue
            e = agg.setdefault(rel, {'count': 0, 'seed_hits': set(), 'seed': s})
            e['seed_hits'].add(s)
            if cnt > e['count']:
                e['count'] = cnt
                e['seed'] = s
    if not agg:
        return []

    exclude = {e for e in exclude if e} | set(seed_keys)
    names = [n for n in agg if n not in exclude]
    if not names:
        return []

    rows = {}
    for i in range(0, len(names), 500):
        chunk = names[i:i + 500]
        ph = ','.join('?' * len(chunk))
        for r in _conn_exec(conn,
                            f"SELECT name, cn_name, category, post_count, nsfw FROM tags WHERE name IN ({ph})",
                            chunk):
            rows[r['name']] = r
    # seed 的 post_count 也要用于 lift
    seed_pc = {}
    for i in range(0, len(seed_keys), 500):
        chunk = seed_keys[i:i + 500]
        ph = ','.join('?' * len(chunk))
        for r in _conn_exec(conn, f"SELECT name, post_count FROM tags WHERE name IN ({ph})", chunk):
            seed_pc[r['name']] = r['post_count'] or 0

    N = _tags_total(conn)
    hide_nsfw = cfg['cooc_nsfw'] != 'show'
    min_post = cfg['cooc_min_post']
    out = []
    dropped = 0
    for name in names:
        row = rows.get(name)
        if not row:
            continue    # parquet 里有、tags 表没有的标签（无法给中文名与 lift）
        if hide_nsfw and row.get('nsfw'):
            continue
        pc_b = row['post_count'] or 0
        pc_a = seed_pc.get(agg[name]['seed'], 0)
        if pc_b < min_post or pc_a <= 0:
            dropped += 1
            continue
        if agg[name]['count'] > pc_b:
            dropped += 1     # 口径对不上的行：共现数不可能超过标签自身 post_count
            continue
        lift = agg[name]['count'] * N / (pc_a * pc_b)
        out.append({
            'name': name,
            'cn_name': row.get('cn_name') or '',
            'category': row.get('category'),
            'post_count': pc_b,
            'count': agg[name]['count'],
            'lift': round(lift, 2),
            'seed_hits': sorted(agg[name]['seed_hits']),
        })
    if dropped and not out:
        # 全被过滤掉时要说清原因，否则用户看到的是"共现推荐为空"这种没法排查的现象
        warnings.append(f'共现数据与标签库的统计口径不一致（或候选过于冷门），'
                        f'{dropped} 条推荐已过滤，本次不给共现建议')
    out.sort(key=lambda c: (-len(c['seed_hits']), -c['lift']))
    return out[:cfg['cooc_topk'] * 2]


# ── ⑤ 工具层（规划器点名 → 本地执行，零 LLM）─────────────────────────────────

def _clean_arg_list(values, bound, warnings, tool, param):
    """工具参数清洗：非列表 → 空；元素强制 str、去空白、去重、按 bound 截断。"""
    if values is None:
        return []
    if isinstance(values, (str, int, float)):
        values = [values]
    if not isinstance(values, list):
        warnings.append(f'工具 {tool} 的参数 {param} 不是列表，已忽略')
        return []
    out = []
    for v in values:
        if not isinstance(v, (str, int, float)):
            continue
        s = str(v).strip()
        if s and s not in out:
            out.append(s)
    if len(out) > bound:
        warnings.append(f'工具 {tool} 的参数 {param} 超过 {bound} 项，已截断')
        out = out[:bound]
    return out


def _tool_search_tags(conn, keywords, exclude, cfg):
    """search_tags 工具：关键词 → 真实候选标签（新增标签的唯一词源）。

    中英文走两条路，**别合并**：
    - 含 CJK → `_batch_cn_first_segment`（cn_name 首段精确）+ `_search_cn_fts`（cn_name 单列 FTS）。
      不用 build_tag_db.search_tags：它的 MATCH OR 了 other_names，中文会被别名污染
      （实测搜 '白发' 首位是 red_eyes、'和服' 混进 leotard）
    - 纯 ASCII → `build_tag_db.search_tags`（name_norm + other_names + cn_name 三列 FTS），
      这正是英文需要的：模型给的是英文标签词，别名命中是加分项

    排序沿用原 retrieve_candidates 的口径：(被几个关键词命中 DESC, post_count DESC)。
    纯 post_count 会让 1girl/long_hair 这类巨物霸榜。
    """
    from tageditor.db.build_tag_db import search_tags as fts_search
    if not keywords:
        return []

    hits = {}   # name -> {'row':..., 'kw': set()}
    for kw in keywords:
        if _CJK_RE.search(kw):
            rows = []
            for bucket in _batch_cn_first_segment(conn, [kw], per_term=8).values():
                rows.extend(bucket)
            rows.extend(_search_cn_fts(conn, kw, limit=8))
        else:
            rows = fts_search(conn, kw, limit=8)
        for row in rows:
            name = row.get('name')
            if name:
                hits.setdefault(name, {'row': row, 'kw': set()})['kw'].add(kw)

    exclude = {e for e in exclude if e}
    categories = cfg['candidate_categories']
    out = []
    for name, e in hits.items():
        row = e['row']
        if name in exclude:
            continue
        if categories and row.get('category') not in categories:
            continue
        if (row.get('post_count') or 0) < 20:
            continue
        out.append({'name': name, 'cn_name': row.get('cn_name') or '',
                    'category': row.get('category'), 'post_count': row.get('post_count') or 0,
                    'kw_hits': len(e['kw'])})
    out.sort(key=lambda c: (-c['kw_hits'], -c['post_count']))
    for c in out:
        c.pop('kw_hits', None)
    return out[:_TOOL_SPECS['search_tags']['limit']]


def _tool_tag_detail(conn, names, cfg):
    """tag_detail 工具：一批标签的确切含义（复用输入侧那套 classify_tags，含 wiki 摘要与近似建议）。"""
    out = []
    for k, item in classify_tags(conn, names, cfg, with_wiki=True).items():
        out.append(dict(item, name=k))
    return out[:_TOOL_SPECS['tag_detail']['limit']]


def _tool_cooc(conn, seeds, exclude, cfg, warnings):
    """cooc 工具：给一批种子标签，返回常与它们同现的标签（lift 排序 + 口径一致性过滤，见 cooc_recommendations）。

    只回 name/lift/seed_hits —— `count`/`post_count` 对模型判断没用，白占 token。
    """
    seeds = [s for s in seeds if s]
    if not seeds:
        return []
    drop = {e for e in exclude if e} | set(seeds)
    rows = cooc_recommendations(conn, seeds, drop, cfg, warnings)
    return [{'name': c.get('name'), 'lift': c.get('lift'), 'seed_hits': c.get('seed_hits')}
            for c in rows][:_TOOL_SPECS['cooc']['limit']]


def _tool_tag_groups(names):
    """tag_groups 工具：标签 → 所属标签组 + 同组成员。

    数据源是 data/tag_groups.json（**不是 SQLite**），复用 translation._load_tag_groups_cache
    的进程级缓存 —— 爬取完成后 translation.py 会把它置 None，所以这里不必自建缓存。
    members 按字母序截断（没有 DB 连接，拿不到 post_count 排序），`member_total` 给出真实组大小。

    注意：tag_to_groups 只覆盖一万多个标签，`groups` 为空**只说明库里没收录它的分组**，
    不是「它有冗余」—— 这条口径同时写进了 prompts/prompt_adjust.txt，别在这里补猜测。
    """
    from tageditor.translate.translation import _load_tag_groups_cache
    tg = _load_tag_groups_cache()
    tag_to_groups = tg.get('tag_to_groups') or {}
    group_to_tags = tg.get('group_to_tags') or {}
    cn_names = tg.get('group_cn_names') or {}

    out = []
    for name in names[:_TOOL_SPECS['tag_groups']['limit']]:
        # 预算按标签重置：预算的语义是「每个标签最多带回多少成员」。
        # 旧实现把 budget 初始化和递减都放在外层，第一个标签吃掉 500 后，
        # 后面所有标签的 members 全是空数组——看起来像「这些标签没有分组」。
        budget = _TG_MEMBER_BUDGET
        key = name.strip().lower().replace(' ', '_')
        groups = []
        for gid in (tag_to_groups.get(key) or [])[:_TG_MAX_GROUPS_PER_TAG]:
            members = sorted(set(group_to_tags.get(gid) or []))
            take = members[:max(budget, 0)]
            budget -= len(take)
            groups.append({'id': gid, 'cn_name': cn_names.get(gid, ''),
                           'member_total': len(members), 'members': take})
        out.append({'name': key, 'groups': groups})
    return out


def _run_tool(name, conn, values, cfg, warnings, exclude):
    """分派到具体工具实现。DB 类工具在 conn is None 时返回空；tag_groups 只读 JSON，照常可跑。"""
    if not values:
        return []
    if name == 'tag_groups':
        return _tool_tag_groups(values)
    if conn is None:
        warnings.append(f'标签库不可用，工具 {name} 本次没有结果')
        return []
    if name == 'search_tags':
        return _tool_search_tags(conn, values, exclude, cfg)
    if name == 'tag_detail':
        return _tool_tag_detail(conn, values, cfg)
    if name == 'cooc':
        return _tool_cooc(conn, values, exclude, cfg, warnings)
    return []


def _trim_tool_results(tool_results, warnings):
    """整块超过 _TOOL_RESULT_CHARS 时按块丢弃（先丢最大的那块），保证改写轮的输入不失控。"""
    while tool_results:
        size = len(json.dumps(tool_results, ensure_ascii=False))
        if size <= _TOOL_RESULT_CHARS:
            break
        biggest = max(tool_results, key=lambda k: len(json.dumps(tool_results[k], ensure_ascii=False)))
        tool_results.pop(biggest)
        warnings.append(f'工具结果过大，「{biggest}」本次未送入模型')
    return tool_results


def _available_tools(allowed):
    """本次开放的工具清单（进规划器的 payload）—— 被开关关掉的不写进去，模型自然不会点。"""
    return [{'name': n, 'args': {s['param']: s['example']}, 'bound': s['bound'], 'desc': s['desc']}
            for n, s in _TOOL_SPECS.items() if n in allowed]


def _tool_brief(tool_results):
    """progress 文案用的一句话摘要。"""
    return ' / '.join(f'{k} {len(v)} 条' for k, v in tool_results.items() if v) or '无命中'


def _normalize_plan(parsed):
    """规划器输出归一化。字段缺失 / 类型不对一律降级为空计划，**不抛**——
    「一个工具都不点」本来就是合法计划（纯格式类要求），不能当错误处理。"""
    if not isinstance(parsed, dict):
        parsed = {}
    tools = parsed.get('tools')
    if not isinstance(tools, list):
        tools = []
    return {
        'intent': str(parsed.get('intent') or '').strip(),
        'understanding': str(parsed.get('understanding') or '').strip(),
        'plan': str(parsed.get('plan') or '').strip(),
        'tools': [t for t in tools if isinstance(t, dict)],
    }


def execute_tool_calls(conn, calls, cfg, warnings, exclude, allowed):
    """执行规划器点名的工具 → {工具名: [结果...]}。

    三道防线，模型乱点也不会 500：
    1. 白名单 —— 名字不在 _TOOL_SPECS，或不在本次 allowed（前端开关决定）里 → 丢弃 + warnings
    2. 参数清洗 —— 非列表/非字符串/空串/重复 → 清理，超 bound 截断
    3. 单项 try/except —— 某个工具炸了只记 warnings，其余照常返回
    同一工具的多次调用**合并**（模型可能把关键词拆成几个调用），每个工具只执行一次。
    """
    if not isinstance(calls, list):
        return {}
    if allowed is None:
        allowed = set(_TOOL_SPECS)

    pending = {}   # tool -> [原始参数值, ...]
    for call in calls:
        if not isinstance(call, dict):
            continue
        name = (call.get('tool') or '').strip()
        spec = _TOOL_SPECS.get(name)
        if spec is None:
            warnings.append(f'规划器点名了未知工具「{name}」，已忽略')
            continue
        if name not in allowed:
            warnings.append(f'规划器点名了本次未开放的工具「{name}」，已忽略')
            continue
        if call.get(spec['param']) is not None:
            pending.setdefault(name, []).append(call.get(spec['param']))

    results = {}
    for name, raws in pending.items():
        spec = _TOOL_SPECS[name]
        merged = []
        for raw in raws:
            merged.extend(_clean_arg_list(raw, spec['bound'], warnings, name, spec['param']))
        merged = _clean_arg_list(merged, spec['bound'], warnings, name, spec['param'])
        try:
            found = _run_tool(name, conn, merged, cfg, warnings, exclude)
        except Exception as e:
            log.error(f"[PromptTool] 工具 {name} 执行失败: {e}")
            warnings.append(f'工具 {name} 执行失败：{e}')
            continue
        if found:
            results[name] = found   # 空结果不落 key：与「这个工具没被调用」对模型是一回事，省 token
    return results


# ── 模型调用层 ─────────────────────────────────────────────────────────────

def _system_prompt(key):
    """读取 prompts/<key>.txt（缺失即报错，与 llm_pipeline.get_system_prompt 同口径）。"""
    text = get_prompt(key)
    if not text:
        raise _PromptFatal(f'提示词文件缺失或为空: prompts/{key}.txt')
    return text


def _create_completion(client, attempts=3, **kwargs):
    """带退避重试的 create：网络/5xx 重试 2 次；response_format 不被支持时立刻抛出交由调用方降级。"""
    last = None
    for i in range(attempts):
        try:
            return client.chat.completions.create(**kwargs)
        except Exception as e:
            msg = str(e)
            if 'response_format' in msg or 'json_object' in msg:
                raise _UnsupportedResponseFormat(msg) from e
            last = e
            if i < attempts - 1:
                time.sleep(1.5 * (i + 1))
    raise last


def _parse_json_loose(text):
    """三级回退解析：json.loads → json_repair → 正则抽最外层 {...}。"""
    text = (text or '').strip()
    if not text:
        return None
    # 去掉 markdown 代码块围栏
    if text.startswith('```'):
        text = re.sub(r'^```[a-zA-Z]*\s*', '', text)
        text = re.sub(r'\s*```$', '', text).strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    try:
        import json_repair
        parsed = json_repair.loads(text)
        if isinstance(parsed, dict) and parsed:
            return parsed
    except Exception:
        pass
    m = re.search(r'\{.*\}', text, re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            return None
    return None


def _encode_image(file_path, cfg, warnings):
    """读图 → (base64, mime)。超字节上限或最长边超限时用 Pillow 压缩到 JPEG q90。"""
    ext = os.path.splitext(file_path)[1].lstrip('.').lower() or 'png'
    mime = 'image/jpeg' if ext in ('jpg', 'jpeg') else f'image/{ext}'
    with open(file_path, 'rb') as f:
        raw = f.read()

    need_shrink = len(raw) > cfg['image_max_bytes']
    try:
        from PIL import Image
        with Image.open(io.BytesIO(raw)) as im:
            if max(im.size) > cfg['image_max_side']:
                need_shrink = True
    except Exception as e:
        log.warning(f"[PromptTool] 图片尺寸检查跳过（Pillow 不可用）: {e}")
    if not need_shrink:
        return base64.b64encode(raw).decode('ascii'), mime

    try:
        from PIL import Image
        img = Image.open(io.BytesIO(raw))
        if img.mode in ('RGBA', 'LA', 'P'):
            img = img.convert('RGBA')
            bg = Image.new('RGB', img.size, (255, 255, 255))
            bg.paste(img, mask=img.split()[-1])
            img = bg
        else:
            img = img.convert('RGB')
        side = cfg['image_max_side']
        if max(img.size) > side:
            img.thumbnail((side, side))
        buf = io.BytesIO()
        img.save(buf, format='JPEG', quality=90)
        data = buf.getvalue()
        warnings.append(f'图片已压缩后送模型（{len(raw) // 1024}KB → {len(data) // 1024}KB）')
        return base64.b64encode(data).decode('ascii'), 'image/jpeg'
    except Exception as e:
        # 压缩失败时不再无条件按原图发送：need_shrink 为真说明这张图已经
        # 超字节上限或超最长边，原样送出会撞服务端的请求体上限，
        # 报错却是「请求过大」这种与图片无关的文案，用户查不到原因。
        mb = cfg['image_max_bytes'] / 1024 / 1024
        warnings.append(f'图片压缩失败（{e}），且原图 {len(raw) // 1024}KB 可能超过服务端上限'
                        f'（配置上限 {mb:.0f}MB），本次仍按原图发送')
        return base64.b64encode(raw).decode('ascii'), mime


def _completion_text(resp):
    """取正式回答；为空且被 max_tokens 截断时抛 _PromptFatal（**不静默返回空结果**）。"""
    choice = resp.choices[0]
    content = (choice.message.content or '').strip()
    if content:
        return content
    finish = choice.finish_reason
    reasoning = getattr(choice.message, 'reasoning_content', None)
    if finish == 'length':
        raise _PromptFatal('模型输出被 max_tokens 截断（思考与正式回答共享额度）：'
                           '请调大 PROMPT_TOOL_MAX_TOKENS 或减少输入标签')
    if reasoning:
        raise _PromptFatal(f'模型把额度花在思考上没有正式回答（finish={finish}）：'
                           f'请确认 PROMPT_TOOL_THINKING=off')
    raise _PromptFatal(f'模型返回空内容（finish={finish}）')


def _call_vlm_json(image_b64, mime, payload, cfg):
    """第 1 轮：带图的综合改写，返回解析后的 JSON dict。"""
    from openai import OpenAI
    client = OpenAI(base_url=cfg['api_url'], api_key=cfg['api_key'], timeout=cfg['timeout'])
    system_prompt = _system_prompt('prompt_adjust')

    user_content = []
    if image_b64:
        user_content.append({'type': 'image_url',
                             'image_url': {'url': f'data:{mime};base64,{image_b64}'}})
    # 无图时的提示由调用方写进 payload['warnings']（这里再 copy 一份就传不回前端了）
    user_content.append({'type': 'text', 'text': json.dumps(payload, ensure_ascii=False)})

    extra = {}
    if cfg['thinking'] not in ('on', 'true', '1', 'enabled'):
        extra['extra_body'] = {'thinking': {'type': 'disabled'}}

    kwargs = dict(model=cfg['model'],
                  messages=[{'role': 'system', 'content': system_prompt},
                            {'role': 'user', 'content': user_content}],
                  temperature=cfg['temperature'],
                  max_tokens=cfg['max_tokens'],
                  **extra)
    try:
        resp = _create_completion(client, response_format={'type': 'json_object'}, **kwargs)
    except _UnsupportedResponseFormat:
        # 端点不支持 response_format：降级为不带，并在系统提示词里再强调一次只输出 JSON
        log.warning("[PromptTool] 端点不支持 response_format，降级重试")
        kwargs['messages'][0]['content'] = system_prompt + '\n\n只输出一个 JSON 对象，不要 markdown 代码块、不要解释文字。'
        resp = _create_completion(client, **kwargs)

    content = _completion_text(resp)
    parsed = _parse_json_loose(content)
    if not isinstance(parsed, dict):
        raise _PromptFatal('模型输出无法解析为 JSON：' + content[:200])
    return parsed


def _call_json_text(prompt_key, payload, cfg, label):
    """纯文本 JSON 调用（不重发图片）：规划轮与修补轮共用。"""
    from openai import OpenAI
    client = OpenAI(base_url=cfg['api_url'], api_key=cfg['api_key'], timeout=cfg['timeout'])
    system_prompt = _system_prompt(prompt_key)

    extra = {}
    if cfg['thinking'] not in ('on', 'true', '1', 'enabled'):
        extra['extra_body'] = {'thinking': {'type': 'disabled'}}

    kwargs = dict(model=cfg['model'],
                  messages=[{'role': 'system', 'content': system_prompt},
                            {'role': 'user',
                             'content': json.dumps(payload, ensure_ascii=False)}],
                  temperature=cfg['temperature'],
                  max_tokens=cfg['max_tokens'],
                  **extra)
    try:
        resp = _create_completion(client, response_format={'type': 'json_object'}, **kwargs)
    except _UnsupportedResponseFormat:
        # 端点不支持 response_format：降级为不带，并在系统提示词里再强调一次只输出 JSON
        log.warning("[PromptTool] 端点不支持 response_format，降级重试")
        kwargs['messages'][0]['content'] = system_prompt + '\n\n只输出一个 JSON 对象，不要 markdown 代码块、不要解释文字。'
        resp = _create_completion(client, **kwargs)

    content = _completion_text(resp)
    parsed = _parse_json_loose(content)
    if not isinstance(parsed, dict):
        raise _PromptFatal(f'{label}输出无法解析为 JSON：' + content[:200])
    return parsed


def _call_planner(payload, cfg):
    """规划轮（在带图改写之前）：模型自报意图并点名要查的工具。

    **不带图**：它只决定「改什么、要查哪些事实」，看图对决策无增益，带了纯属加钱加时。
    """
    return _call_json_text('prompt_planner', payload, cfg, '规划轮')


def _call_llm_repair(payload, cfg):
    """第 2 轮：纯文本修补未收录标签（不重发图片），返回解析后的 JSON dict。"""
    return _call_json_text('prompt_repair', payload, cfg, '修补轮')


# ── diff 归一与输出契约 ────────────────────────────────────────────────────

_VALID_OPS = {'keep', 'add', 'remove', 'modify'}


def _normalize_diff(parsed, entries, conn, cfg, warnings):
    """防御性归一：保证前端拿到的 diff 结构自洽且覆盖全部输入标签。

    1. 模型没提到的输入条目补 {op:'keep'}（前端永远拿到完整列表）
    2. add/modify 的目标标签逐个回库校验，合并 status/cn_name/category/post_count/suggestions
    3. op 非法 / tag 为空 → 丢弃并记 warnings
    4. 返回 (diff, uncollected_targets) —— 后者非空时触发第 2 轮修补
    """
    from tageditor.db.build_tag_db import normalize_tag_key

    raw_diff = parsed.get('diff')
    if not isinstance(raw_diff, list):
        warnings.append('模型返回的 diff 不是列表，已按全部保留处理')
        raw_diff = []

    by_key = {}
    for item in raw_diff:
        if not isinstance(item, dict):
            continue
        op = (item.get('op') or '').strip().lower()
        if op not in _VALID_OPS:
            warnings.append(f'模型返回了非法操作 op={item.get("op")!r}，已丢弃该条')
            continue
        tag = (item.get('tag') or '').strip()
        if not tag:
            warnings.append('模型返回了空标签，已丢弃该条')
            continue
        key = normalize_tag_key(item.get('from') if op == 'modify' else tag)
        if not key:
            continue
        by_key.setdefault(key, {'op': op, 'tag': tag,
                                'from': (item.get('from') or '').strip(),
                                'reason': (item.get('reason') or '').strip(),
                                'cn_name': (item.get('cn_name') or '').strip(),
                                'weight': _sanitize_weight(item.get('weight'))})

    diff = []
    for e in entries:
        key = normalize_tag_key(e['tag'])
        got = by_key.get(key)
        if not got:
            diff.append({'op': 'keep', 'tag': e['tag'], 'raw': e['raw'],
                         'weight': e['weight'], 'reason': '模型未提及，默认保留'})
            continue
        op = got['op']
        if op == 'modify':
            diff.append({'op': 'modify', 'tag': got['tag'], 'from': e['tag'],
                         'raw': e['raw'], 'weight': e['weight'],
                         'cn_name': got['cn_name'], 'reason': got['reason']})
        elif op == 'remove':
            diff.append({'op': 'remove', 'tag': e['tag'], 'raw': e['raw'],
                         'weight': e['weight'], 'reason': got['reason']})
        else:
            # 只有 add 落到已有标签上时才退化成 keep，避免同一标签既在原文又"新增"
            diff.append({'op': 'keep', 'tag': e['tag'], 'raw': e['raw'],
                         'weight': e['weight'], 'reason': got['reason'] or '保留'})

    adds = []
    for key, got in by_key.items():
        if got['op'] != 'add':
            continue
        # 同一标签既在原文里又被"新增" → 交给上面那条 keep，不重复列出
        if any(normalize_tag_key(e['tag']) == key for e in entries):
            continue
        adds.append({'op': 'add', 'tag': got['tag'], 'weight': got['weight'],
                     'cn_name': got['cn_name'], 'reason': got['reason']})
    if len(adds) > _MAX_ADD:
        warnings.append(f'模型新增了 {len(adds)} 条标签，已截断到 {_MAX_ADD} 条')
        adds = adds[:_MAX_ADD]
    diff.extend(adds)

    # 校验 add/modify 的目标标签（remove/keep 的目标本来就在输入里，输入侧已校验过）
    targets = [d['tag'] for d in diff if d['op'] in ('add', 'modify')]
    knowledge = validate_tags(conn, targets, cfg) if targets else {}
    uncollected = []
    for d in diff:
        if d['op'] not in ('add', 'modify'):
            continue
        info = knowledge.get(normalize_tag_key(d['tag'])) or {'status': 'uncollected'}
        d.update(info)
        if not d.get('cn_name'):
            d['cn_name'] = ''
        if info.get('status') == 'uncollected':
            uncollected.append({'key': normalize_tag_key(d['tag']), 'tag': d['tag'],
                                'op': d['op'], 'from': d.get('from', ''),
                                'reason': d.get('reason', ''),
                                'suggestions': info.get('suggestions') or []})
    return diff, uncollected


def _repair_key(item):
    """修补轮 diff 条目的匹配键：modify 用 from 对齐，其余用 tag。"""
    from tageditor.db.build_tag_db import normalize_tag_key
    op = (item.get('op') or '').strip().lower()
    name = (item.get('from') if op == 'modify' else item.get('tag')) or ''
    return normalize_tag_key(name)


def _apply_repair(base_diff, repair_parsed, uncollected):
    """把第 2 轮的裁决**合并**到第 1 轮结果上。

    刻意不做"整体替换"：模型若没把未裁决的条目带全，整体替换会把第 1 轮的 remove/keep
    悄悄退回成 keep（等于什么都没改）。这里只应用被裁决标签的结论。
    """
    from tageditor.db.build_tag_db import normalize_tag_key
    uc = {u['key']: u for u in uncollected}
    by_tag = {}
    for d in base_diff:
        if d['op'] in ('add', 'modify'):
            by_tag[normalize_tag_key(d['tag'])] = d
    raw = repair_parsed.get('diff')
    if not isinstance(raw, list):
        return base_diff
    for item in raw:
        if not isinstance(item, dict):
            continue
        op = (item.get('op') or '').strip().lower()
        if op not in _VALID_OPS:
            continue
        key = _repair_key(item)
        if key not in uc:
            continue        # 只管被裁决的标签，其余一律以第 1 轮为准
        base = by_tag.get(key)
        if base is None:
            continue
        reason = (item.get('reason') or '').strip()
        new_tag = (item.get('tag') or '').strip()
        base['repaired'] = True
        if op == 'remove':
            if base['op'] == 'add':
                base['op'] = 'dropped'      # 新增项被删 → 直接从 diff 移除
            else:
                base['op'] = 'remove'
                base['tag'] = base.get('from') or base['tag']
            base['reason'] = reason or base.get('reason') or ''
        elif op == 'modify' and new_tag:
            # add 项被换成真实标签 → 仍是新增；modify 项 → 换掉目标名
            if base['op'] == 'modify':
                pass
            base['tag'] = new_tag
            # _sanitize_weight 的 fallback 用原值：模型在 modify 里没写 weight
            # （返回 None 或缺字段）时必须保留第 1 轮定下的权重。
            # 旧写法 item.get('weight', base.get('weight')) 只在「键不存在」时兜底，
            # 模型显式返回 "weight": null 时会用 None 覆盖掉原本正确的权重，
            # 前端拼出 (tag:null) 或直接丢权重语法。
            base['weight'] = _sanitize_weight(item.get('weight'), base.get('weight'))
            base['reason'] = reason or base.get('reason') or ''
        else:   # keep：保留原结论，只更新理由
            if reason:
                base['reason'] = reason
    return [d for d in base_diff if d.get('op') != 'dropped']


# ── 路由：主流程（SSE）──────────────────────────────────────────────────────

@prompt_tool_bp.route('/prompt_adjust', methods=['POST'])
def prompt_adjust():
    """主路由：规划（模型自决意图）→ 本地执行工具 → 带图改写 → 未收录修补（SSE 流式）。"""
    data = request.get_json(silent=True) or {}
    image = (data.get('image') or '').strip()
    prompt_raw = data.get('prompt') or ''
    user_request = (data.get('request') or '').strip()
    # 必须显式判类型：`data.get('options') or {}` 只在 None/''/{}/0 时兜底，
    # 前端传成列表时它原样返回 list，下游 options.get(...) 直接 AttributeError → 500。
    options = data.get('options')
    if not isinstance(options, dict):
        options = {}

    # 前置校验：失败仍是普通 JSON 400（前端按 Content-Type 分流）
    # 「优化要求」必填：没有它就没有本次调整的目标（提示词本身可空，允许从零/纯描述起步）
    if not user_request:
        return jsonify({'error': '请填写优化要求（告诉模型怎么改这份提示词）'}), 400
    image_path = None
    if image:
        filename = safe_filename(image)
        if filename != image:
            return jsonify({'error': '非法图片名'}), 400
        upload_dir = os.path.abspath(current_app.config['UPLOAD_FOLDER'])
        image_path = os.path.abspath(os.path.join(upload_dir, filename))
        if not is_within_directory(image_path, upload_dir):
            return jsonify({'error': '非法路径'}), 400
        if not os.path.isfile(image_path):
            return jsonify({'error': f'图片不存在：{filename}'}), 400
    try:
        # 提示词文件缺失时立刻 400（进 generator 之前，用户能看到明确原因）
        _system_prompt('prompt_planner')
        _system_prompt('prompt_adjust')
        _system_prompt('prompt_repair')
    except _PromptFatal as e:
        return jsonify({'error': str(e)}), 400

    from tageditor.translate.translation import _register_cancel, _unregister_cancel

    def generate():
        cancel_evt = _register_cancel('prompt_tool')
        try:
            yield from _run(cancel_evt)
        except GeneratorExit:
            # 客户端断开 / 用户点「中断」：GeneratorExit 继承 BaseException，
            # 不走下面的 except Exception，必须在此处显式置位，
            # 否则 _run 内的 6 处 _cancelled() 检查全是死代码，已发出的 LLM 请求会跑满超时。
            cancel_evt.set()
            raise
        except _PromptFatal as e:
            log.error(f"[PromptTool] fatal: {e}")
            yield sse_event('fatal', {'error': str(e)})
        except Exception as e:
            import traceback
            traceback.print_exc()
            yield sse_event('fatal', {'error': f'内部错误：{e}'})
        finally:
            _unregister_cancel('prompt_tool')

    def _run(cancel_evt):
        cfg = get_prompt_tool_config()
        warnings = []
        total = 6

        def _cancelled():
            return cancel_evt.is_set()

        yield sse_event('progress', {'current': 0, 'total': total, 'item': '正在准备...'})

        # 阶段 1：读取图片与已有描述
        yield sse_event('progress', {'current': 1, 'total': total, 'item': '读取图片与已有描述'})
        if _cancelled():
            yield sse_event('cancelled', {'message': '已取消'})
            return
        image_b64, mime = (None, None)
        image_caption = ''
        if image_path:
            try:
                base = os.path.splitext(image_path)[0]
                nl_path = f'{base}.nl.txt'
                if os.path.isfile(nl_path):
                    with open(nl_path, 'r', encoding='utf-8') as f:
                        image_caption = f.read().strip()
                image_b64, mime = _encode_image(image_path, cfg, warnings)
            except Exception as e:
                warnings.append(f'读取图片失败（{e}），改为纯文本模式')
                image_b64 = None
        if not image_b64:
            # 写进共享的 warnings 列表：模型与前端结果页看到的是同一条（别改回 _call_vlm_json 内部 copy）
            warnings.append('本次没有图片，只依据提示词与文字要求调整')

        # 阶段 2：解析输入提示词
        parsed = _split_prompt_entries(prompt_raw)
        yield sse_event('progress', {
            'current': 2, 'total': total,
            'item': f"解析输入提示词：标签 {len(parsed['tags'])} 条，描述 {len(parsed['prose'])} 字"})

        conn = None
        if options.get('use_db', True):
            from tageditor.translate.translation import _get_tag_db_conn
            conn = _get_tag_db_conn()
        if conn is None:
            warnings.append('标签库不可用，本次未做检索与校验（新增标签无法保证真实）')
            use_db = False
        else:
            use_db = True

        entries_knowledge, raw_knowledge = ([], {})
        if use_db:
            entries_knowledge, raw_knowledge = build_input_knowledge(conn, parsed['tags'], cfg)
        else:
            entries_knowledge = [{'raw': e['raw'], 'tag': e['tag'], 'weight': e['weight']}
                                 for e in parsed['tags']]
        in_db_count = sum(1 for k in raw_knowledge.values() if k.get('status') == 'in_db')
        yield sse_event('progress', {
            'current': 2, 'total': total,
            'item': f"解析输入提示词：标签 {len(parsed['tags'])} 条（库内命中 {in_db_count} 条）"
                    f"，描述 {len(parsed['prose'])} 字"})

        # 阶段 3：让模型判断优化意图与检索计划（纯文本，不带图）
        yield sse_event('progress', {'current': 3, 'total': total,
                                     'item': '让模型判断优化意图与检索计划（2~5 秒）'})
        if _cancelled():
            yield sse_event('cancelled', {'message': '已取消'})
            return
        exclude = set()
        for e in parsed['tags']:
            exclude.add(e['tag'].strip().lower().replace(' ', '_'))
        # 本次开放哪些工具，由前端两个开关决定（关掉的工具不进规划器清单，模型自然不会点）
        allowed = {'tag_groups'}
        if use_db:
            allowed |= {'search_tags', 'tag_detail'}
        if use_db and options.get('use_cooc', True):
            allowed.add('cooc')

        plan, planner_ok = _normalize_plan(None), True
        try:
            plan = _normalize_plan(_call_planner({
                'user_request': user_request,
                'has_image': bool(image_b64),
                'input_mode': parsed['mode'],
                'input_prompt': entries_knowledge,
                'input_prose': parsed['prose'],
                'available_tools': _available_tools(allowed),
                'warnings': warnings,
            }, cfg))
        except Exception as e:
            # 规划器是**增强**不是必需：失败就降级继续（没有工具结果仍可改写），绝不 fatal
            planner_ok = False
            log.error(f"[PromptTool] 规划轮失败，降级为关键词直检: {e}")
            warnings.append(f'意图规划失败（{e}），已改用关键词直接检索兜底')

        # 阶段 4：本地执行检索计划（零 LLM）
        yield sse_event('progress', {'current': 4, 'total': total, 'item': '执行检索计划'})
        if _cancelled():
            yield sse_event('cancelled', {'message': '已取消'})
            return
        tool_results = execute_tool_calls(conn, plan['tools'], cfg, warnings, exclude, allowed)
        if not planner_ok and 'search_tags' in allowed:
            # 兜底：规划失败时用规则抽词直检（_extract_terms 现在只剩这个用途），
            # 至少给改写阶段一份真实词表，否则它一个新标签都加不了
            fallback_terms = _extract_terms(user_request, limit=12)
            fallback_terms += [t for t in _extract_terms(parsed['prose'], limit=6)
                               if t not in fallback_terms]
            if fallback_terms:
                got = _tool_search_tags(conn, fallback_terms[:16], exclude, cfg)
                if got:
                    tool_results['search_tags'] = got
        _trim_tool_results(tool_results, warnings)
        yield sse_event('progress', {
            'current': 4, 'total': total,
            'item': f"执行检索计划：{_tool_brief(tool_results)}"})

        if _cancelled():
            yield sse_event('cancelled', {'message': '已取消'})
            return

        # 阶段 5：调用视觉模型
        yield sse_event('progress', {'current': 5, 'total': total,
                                     'item': '调用视觉模型（10~60 秒）'})
        payload = {
            'input_mode': parsed['mode'],
            'input_prompt': entries_knowledge,
            'input_prose': parsed['prose'],
            'user_request': user_request,
            'planner_intent': plan['intent'],
            'planner_plan': plan['plan'],
            'image_caption': image_caption,
            'tool_results': tool_results,
            'warnings': warnings,
        }
        parsed_model = _call_vlm_json(image_b64, mime, payload, cfg)
        summary = (parsed_model.get('summary') or '').strip()
        caption = (parsed_model.get('caption') or '').strip()
        caption_note = (parsed_model.get('caption_note') or '').strip()

        if not caption:
            # 不静默给空串：能回落就回落（用户原来的描述），否则明确警告
            caption = parsed['prose']
            caption_note = caption_note or ('模型未返回描述，已保留原描述'
                                            if caption else '模型未返回描述')
            warnings.append('模型未返回自然语言描述' + ('，已沿用原描述' if caption else '，请手动补全'))

        if _cancelled():
            yield sse_event('cancelled', {'message': '已取消'})
            return

        diff, uncollected = ([], [])
        if use_db:
            diff, uncollected = _normalize_diff(parsed_model, parsed['tags'], conn, cfg, warnings)
        else:
            for e in parsed['tags']:
                diff.append({'op': 'keep', 'tag': e['tag'], 'raw': e['raw'],
                             'weight': e['weight'], 'reason': '标签库不可用，未做校验'})
            for item in (parsed_model.get('diff') or []):
                if isinstance(item, dict) and (item.get('op') or '').lower() == 'add' and item.get('tag'):
                    diff.append({'op': 'add', 'tag': item['tag'],
                                 'weight': _sanitize_weight(item.get('weight')),
                                 'cn_name': item.get('cn_name') or '',
                                 'reason': item.get('reason') or '', 'status': 'uncollected'})

        # 阶段 6：未收录标签修补（仅当存在未收录项）
        repaired = False
        if uncollected and use_db:
            yield sse_event('progress', {
                'current': 6, 'total': total,
                'item': f'修补未收录标签（{len(uncollected)} 条）'})
            if not _cancelled():
                try:
                    repair_parsed = _call_llm_repair({
                        'uncollected': uncollected,
                        'input_prompt': entries_knowledge,
                        'input_prose': parsed['prose'],
                        'user_request': user_request,
                        'planner_intent': plan['intent'],
                        'planner_plan': plan['plan'],
                        'caption': caption,
                        'diff': diff,
                        'tool_results': tool_results,
                    }, cfg)
                    diff = _apply_repair(diff, repair_parsed, uncollected)
                    new_caption = (repair_parsed.get('caption') or '').strip()
                    if new_caption:
                        caption = new_caption
                    if (repair_parsed.get('caption_note') or '').strip():
                        caption_note = repair_parsed['caption_note'].strip()
                    # 修补后重新校验一遍，仍未收录的留在 diff 里标红，由用户决定
                    targets = [d['tag'] for d in diff if d['op'] in ('add', 'modify')]
                    knowledge = validate_tags(conn, targets, cfg) if targets else {}
                    from tageditor.db.build_tag_db import normalize_tag_key
                    for d in diff:
                        if d['op'] in ('add', 'modify'):
                            info = knowledge.get(normalize_tag_key(d['tag']))
                            if info:
                                d.pop('suggestions', None)   # 换过名字后旧建议已失效
                                d.update(info)
                    repaired = True
                except _PromptFatal as e:
                    # 修补轮失败不该毁掉整轮结果：保留第 1 轮 diff，标记未收录即可
                    warnings.append(f'未收录标签自动修补失败：{e}')
                except Exception as e:
                    warnings.append(f'未收录标签自动修补失败：{e}')

        still_uncollected = [d['tag'] for d in diff
                             if d['op'] in ('add', 'modify') and d.get('status') == 'uncollected']
        stats = {
            'input_tags': len(parsed['tags']),
            'in_db': in_db_count,
            'keep': sum(1 for d in diff if d['op'] == 'keep'),
            'add': sum(1 for d in diff if d['op'] == 'add'),
            'remove': sum(1 for d in diff if d['op'] == 'remove'),
            'modify': sum(1 for d in diff if d['op'] == 'modify'),
            'uncollected': len(still_uncollected),
        }
        yield sse_event('complete', {
            'summary': summary,
            'caption': caption,
            'caption_note': caption_note,
            'input_mode': parsed['mode'],
            'diff': diff,
            'stats': stats,
            'warnings': warnings,
            'parsed_input': entries_knowledge,
            'plan': plan,
            'tool_results': tool_results,
            'repaired': repaired,
            'uncollected': still_uncollected,
        })

    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

