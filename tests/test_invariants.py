# -*- coding: utf-8 -*-
"""关键不变量测试 —— 锁住 CLAUDE.md 里那些「别退回」的约定。

为什么需要这个文件：本项目的多数 bug 是**静默**的（翻译列空白、徽标错、
描述被炸成假标签、数据写坏），没有异常、没有日志，只有人眼能发现。
这些不变量都是踩过坑之后写进文档的，一旦被无意改回去，症状要过很久才浮现。

运行（在**项目根**下执行，测试脚本都放在 tests/）：
    python tests/test_invariants.py          # 全部
    python tests/test_invariants.py -v       # 显示每条断言

只用标准库（本项目无测试框架依赖）。不连网络、不碰真实 uploads/、
不需要标签库存在——纯函数与临时目录。
"""
import os
import re
import shutil
import sys
import tempfile
import traceback
from pathlib import Path

VERBOSE = '-v' in sys.argv

# **项目根**（本文件在 tests/ 下，故上跳一级）。所有源码级断言都基于它拼路径，
# 这样模块被移动到包里的其它位置时，只要改下面的 srcdir 映射即可。
ROOT = Path(__file__).resolve().parent.parent

# 脚本被 `python tests/test_invariants.py` 直接执行时，sys.path[0] 是 tests/ 而不是
# 项目根 —— 断言里的 `import app` 会 ModuleNotFoundError。这里显式补上项目根。
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def srcdir(module_stem):
    """按模块名找它现在所在的目录（相对 ROOT）。

    源码级断言要读 .py 原文（比如「OpenAI() 必须过 resolve_api_key」），
    模块重组后位置会变，所以不写死路径，用一个映射 + 兜底搜索。
    """
    known = {
        'config': 'tageditor/core',
        'logging_setup': 'tageditor/core',
        'sse_utils': 'tageditor/core',
        'build_tag_db': 'tageditor/db',
        'sync_tags': 'tageditor/db',
        'cooc_pipeline': 'tageditor/db',
        'tag_groups': 'tageditor/db',
        'translation': 'tageditor/translate',
        'llm_pipeline': 'tageditor/translate',
        'prompt_tool': 'tageditor/translate',
        'prompt_workspace': 'tageditor/translate',
        'tagger': 'tageditor/image',
        'image_editor': 'tageditor/image',
        'realesrgan_utils': 'tageditor/image',
        'birefnet_utils': 'tageditor/image',
        'file_ops': 'tageditor/ops',
        'tag_operations': 'tageditor/ops',
    }
    if module_stem in known:
        return ROOT / known[module_stem]
    for p in ROOT.rglob('%s.py' % module_stem):
        return p.parent
    return ROOT


def all_source_files():
    """**应用**的 Python 源文件（用于「全仓扫描」类断言，如 OpenAI() 检查）。

    范围 = 根目录的入口脚本 + `tageditor/` 包，**不含 `tests/`**。这是刻意的：
    这里四条断言（OpenAI 构造点、unregister 事件名、「log 调用不带 print 专属
    参数」、env 键有记录）讲的都是**应用代码**的约定，把测试脚本自己也算进去
    只会让它们在写测试辅助代码时误报。

    （早先测试脚本放在根目录，于是被 `glob('*.py')` 顺带扫进来 —— 那是位置造成
    的巧合，不是设计。移到 `tests/` 后这个边界才显式化。）
    """
    files = sorted(ROOT.glob('*.py'))
    pkg = ROOT / 'tageditor'
    if pkg.is_dir():
        files += sorted(pkg.rglob('*.py'))
    return files


def _skip_js_regex(src, i):
    """从 `src[i] == '/'` 起跳过整个正则字面量，返回其后的下标。

    字符类 `[...]` 内的 `/` 不结束正则（`/[/]/` 合法），`\\` 转义同理，都要单独跟。
    扫到换行还没收尾说明判错了（正则不能跨行）→ 返回 `i + 1`，只当掉一个普通字符，
    绝不吞文件。
    """
    n, j, in_class = len(src), i + 1, False
    while j < n:
        c = src[j]
        if c == '\\':
            j += 2; continue
        if c == '\n':
            break
        if in_class:
            if c == ']':
                in_class = False
        elif c == '[':
            in_class = True
        elif c == '/':
            j += 1
            while j < n and src[j].isalpha():   # 标志位 gimsuy
                j += 1
            return j
        j += 1
    return i + 1


def strip_js_comments(src):
    """去掉 JS 的行注释与块注释（**字符串与正则里的 `//` 不算注释**）。

    源码级断言必须先在无注释文本上做：项目里解释「别退回成 X」的注释本身会把 X
    写进去，拿含注释的源码去查 X 就会命中**解释文字**。本文件的 JS 断言已踩过四次
    （`current_app` 两次、`参考图不会被清空`、`onCaptionInput()` 各一次），
    所以这里做成公共工具，别再从注释里挑代码看了。

    **正则字面量是必须处理的，不是洁癖**：`escapeHtml` 里有
    `.replace(/'/g,'&#39;')`，里面那个 `'` 会让朴素的引号状态机误以为字符串开始，
    于是从该行起**整个文件相位错开**、一半注释漏剥 —— 而漏剥的表现是断言去匹配
    注释文字（正是本函数要防的那件事），且不会报错，只会莫名其妙地失败。实测踩到。
    另一个兜底：`'` 和 `"` 不可能跨行，遇到换行一律复位，让任何残留的误判
    **活不过本行**（模板字符串可以跨行，故反引号不复位）。
    """
    out, i, n, quote = [], 0, len(src), None
    prev, word = '', ''            # 前一个有意义的字符 / 以它结尾的标识符
    while i < n:
        c = src[i]
        if quote:
            out.append(c)
            if c == '\\' and i + 1 < n:        # 转义字符整体跳过
                out.append(src[i + 1]); i += 2; continue
            if c == quote:
                quote = None
            elif c == '\n' and quote != '`':   # 单双引号字符串不能跨行 → 状态机复位
                quote = None
            i += 1
            continue
        if c in ('"', "'", '`'):
            quote = c; out.append(c); prev, word = c, ''; i += 1; continue
        if c == '/' and i + 1 < n and src[i + 1] == '/':
            j = src.find('\n', i)              # 保留换行，别把两行粘起来
            i = n if j < 0 else j
            continue
        if c == '/' and i + 1 < n and src[i + 1] == '*':
            j = src.find('*/', i + 2)
            i = n if j < 0 else j + 2
            continue
        if c == '/' and _prev_allows_regex(prev, word):
            j = _skip_js_regex(src, i)
            out.append(src[i:j]); prev, word = '/', ''; i = j; continue
        out.append(c)
        if not c.isspace():
            word = (word + c) if (c.isalnum() or c in '_$') else ''
            prev = c
        i += 1
    return ''.join(out)


# `/` 前面是这些字符时它是**除号**，不是正则开头；反过来说前面是 `(` `,` `=` `return`
# 之类才是正则。判错的方向代价不对称：把正则当除号 → 注释漏剥（断言响亮地失败）；
# 把除号当正则 → 会吞掉真正的代码（断言可能假通过）。所以从严判「除号」。
_JS_VALUE_END = set(')]}')
_JS_REGEX_KEYWORDS = ('return', 'typeof', 'case', 'in', 'of', 'new', 'delete',
                      'void', 'instanceof', 'do', 'else', 'yield', 'await')


def _prev_allows_regex(prev, word):
    """判断此处 `/` 能否开启正则字面量。`word` 是紧邻其前的标识符（可能为空）。"""
    if not prev:
        return True                        # 文件/语句开头
    if prev in _JS_VALUE_END or prev.isdigit():
        return False                       # `x / y`、`f(a) / 2`、`a[0] / 2`
    if prev.isalnum() or prev in '_$':
        # 标识符之后一般是除号，但 `return /re/`、`case /re/:` 是例外
        return word in _JS_REGEX_KEYWORDS
    return True                            # `(,=:[!&|?{};` 等之后都是正则


def js_of(page):
    """读 templates/ 下某个页面**主脚本**的源码，并去掉注释。

    页面里常有多个 `<script>` 块（prompt_tool.html 第一块是 tailwind.config），
    所以取**最长的那块**，不靠顺序 —— 哪天调整了引用顺序，断言不该跟着碎。
    """
    html = (ROOT / 'templates' / page).read_text(encoding='utf-8')
    blocks = re.findall(r'<script>(.*?)</script>', html, re.S)
    if not blocks:
        raise AssertionError('找不到 %s 的 <script> 段' % page)
    return strip_js_comments(max(blocks, key=len))


def src_of(module_stem):
    """读某模块的源码文本。找不到时抛清晰错误而不是静默返回空串。

    **统一换行为 \\n**：工作区文件是 CRLF（git autocrlf 在 checkout 时转换），
    而断言里的锚点字面量写的是 \\n。不做归一化的话，跨行的 `anchor in text`
    会「看起来一模一样却不匹配」，非常难查。
    现有断言都是单行片段所以没受影响，但加上这层保护，以后写多行断言不会踩坑。
    """
    p = srcdir(module_stem) / ('%s.py' % module_stem)
    if not p.exists():
        raise AssertionError('找不到源码 %s（模块被移动了？请更新 srcdir 映射）' % p)
    return p.read_text(encoding='utf-8').replace('\r\n', '\n').replace('\r', '\n')


_results = []


def case(fn):
    """把一个 test_* 函数登记为用例。"""
    _results.append(fn)
    return fn


def eq(a, b, what=''):
    if a != b:
        raise AssertionError('%s\n      期望: %r\n      实际: %r' % (what or '值不等', b, a))


def ok(cond, what=''):
    if not cond:
        raise AssertionError(what or '条件不成立')


# ═══════════════════════════════════════════════════════════════════════════
# 一、翻译合并 _combine_cn —— CLAUDE.md「改这里时别退回 ",".join」
# （引文用 grep 定位：CLAUDE.md 是未跟踪文件、改一次行号就漂，别写行号）
# ═══════════════════════════════════════════════════════════════════════════

@case
def test_combine_cn_dedup():
    """必须去重：LLM 常把 base 在 extended 里重复一遍。"""
    from tageditor.translate.llm_pipeline import _combine_cn
    eq(_combine_cn('透明衣物', '透明衣物,透视装'), '透明衣物,透视装',
       'base 在 ext 里重复时必须去掉')
    eq(_combine_cn('单人', '独图,单独,单人'), '单人,独图,单独',
       '重复项保留首次出现位置')


@case
def test_combine_cn_splits_fullwidth():
    """必须拆全角逗号：前端 split(',') 只认半角，不拆会把两段粘成一段。"""
    from tageditor.translate.llm_pipeline import _combine_cn
    eq(_combine_cn('彩虹社，Anycolor', ''), '彩虹社,Anycolor',
       '全角逗号要被切开并换成半角')
    eq(_combine_cn('a，b', 'c，d'), 'a,b,c,d', '两侧全角都要拆')


@case
def test_combine_cn_order_and_edges():
    from tageditor.translate.llm_pipeline import _combine_cn
    eq(_combine_cn('a,b', 'b,c'), 'a,b,c', '保序去重')
    eq(_combine_cn('', 'x'), 'x', 'base 为空')
    eq(_combine_cn('a', ''), 'a', 'ext 为空')
    eq(_combine_cn('', ''), '', '都为空')
    eq(_combine_cn(None, None), '', 'None 不崩')


# ═══════════════════════════════════════════════════════════════════════════
# 二、提示词切分 _split_prompt_entries —— CLAUDE.md「别退回全文按逗号切」
# ═══════════════════════════════════════════════════════════════════════════

@case
def test_prompt_split_newline_is_hard_boundary():
    """换行是硬分界：换行后整段是描述，绝不按逗号切。

    这是实测驱动的一条：真实提示词文件里换行落在第 36 个逗号块中间，
    换行后的散文段若按逗号切会炸成 14 个假标签。

    注意 tags 里每项是 dict（{raw, tag, weight}），不是字符串。
    """
    from tageditor.translate.prompt_tool import _split_prompt_entries
    raw = '1girl, solo, long_hair\nShe is standing in the rain, looking up, with a wistful smile.'
    r = _split_prompt_entries(raw)
    eq([t['tag'] for t in r['tags']], ['1girl', 'solo', 'long_hair'], '换行前按逗号切')
    ok('wistful smile' in r['prose'], '换行后整段保留为描述')
    ok('looking up, with' in r['prose'],
       '描述段内部的逗号不能被切开（切开就会变成两个假标签）')
    # 最关键的一条：描述段绝不能被当成标签
    ok(not any('rain' in t['tag'] for t in r['tags']),
       '描述内容不该出现在标签列表里')


@case
def test_prompt_split_modes():
    from tageditor.translate.prompt_tool import _split_prompt_entries
    eq(_split_prompt_entries('')['mode'], 'empty', '空输入')
    eq(_split_prompt_entries('a, b')['mode'], 'tags_only', '只有标签')
    eq(_split_prompt_entries('a, b\nsome prose here.')['mode'], 'both', '两段都有')


@case
def test_prompt_split_preserves_weight_syntax():
    """权重语法必须原样保留（丢了等于偷改用户提示词）。"""
    from tageditor.translate.prompt_tool import _split_prompt_entries
    r = _split_prompt_entries('(1girl:1.2), [solo], plain')
    tags = {t['tag']: t['weight'] for t in r['tags']}
    eq(tags.get('1girl'), 1.2, '(tag:1.2) 的权重')
    eq(tags.get('solo'), 0.9, '[tag] 等价于 (tag:0.9)')
    eq(tags.get('plain'), 1.0, '无权重语法默认 1.0')


# ═══════════════════════════════════════════════════════════════════════════
# 三、API Key 归一化 —— CLAUDE.md「所有 OpenAI() 构造点必须过 resolve_api_key」
# ═══════════════════════════════════════════════════════════════════════════

@case
def test_resolve_api_key_blank_becomes_placeholder():
    """空 key 必须归一化成占位串，否则 openai SDK 抛 Missing credentials。"""
    from tageditor.core.config import resolve_api_key, API_KEY_PLACEHOLDER
    eq(resolve_api_key(''), API_KEY_PLACEHOLDER, '空串')
    eq(resolve_api_key('   '), API_KEY_PLACEHOLDER, '全空白')
    eq(resolve_api_key(None), API_KEY_PLACEHOLDER, 'None')
    eq(resolve_api_key('sk-real-key'), 'sk-real-key', '真实 key 原样透传')


@case
def test_all_openai_construction_sites_use_resolver():
    """源码级断言：任何 OpenAI(...) 构造点的 api_key 都必须是已归一化的。

    这条守「以后有人加了个新的 OpenAI() 但忘了归一化」，它只在**端点是本地部署
    且没配 key** 时才暴露——用户会看到 SDK 抛 Missing credentials，很难联想到
    是漏了 resolve_api_key。

    判定分两种合法形态（不能只看调用点是字面量写了 resolve_api_key）：
      A. 实参直接是 resolve_api_key(...)
      B. 实参取自 config 的配置函数（get_llm_config / get_vision_config /
         get_prompt_tool_config），这些函数内部已归一化
    """
    # 先确认配置层确实归一化了 —— B 形态的正确性依赖这一点。
    # 注意：**不能**调用 get_*_config() 来验证 —— 那会读真实 .env，
    # 结果随用户的配置而变（配了 key 就会返回真实 key，既让断言失效，
    # 也会把凭据打进测试输出）。直接测归一化函数本身即可。
    from tageditor.core.config import resolve_api_key, API_KEY_PLACEHOLDER
    for raw in ('', '   ', None):
        eq(resolve_api_key(raw), API_KEY_PLACEHOLDER,
           'resolve_api_key(%r) 应归一化为占位串（B 形态依赖它）' % (raw,))

    # 再确认配置函数确实**调用了**归一化（源码级，不看运行时取值）
    cfg_src = src_of('config')
    for fn in ('get_llm_config', 'get_vision_config'):
        blk = cfg_src[cfg_src.index('def %s(' % fn):]
        blk = blk[:blk.index('\ndef ', 1)]
        ok('resolve_api_key' in blk, '%s 必须调用 resolve_api_key' % fn)

    cfg_derived = re.compile(r"cfg\[['\"]api_key['\"]\]|vcfg\[['\"]api_key['\"]\]")
    bad = []
    for f in all_source_files():
        src = f.read_text(encoding='utf-8')
        for m in re.finditer(r'OpenAI\((.*?)\)', src, re.S):
            args = m.group(1)
            if 'api_key' not in args:
                continue
            # 形态 A / B
            if 'resolve_api_key' in args or cfg_derived.search(args):
                continue
            # 形态 C：实参是局部变量 api_key=xxx。用简易数据流确认——该变量在
            # 本文件里**每一个**赋值点都必须经过 resolve_api_key。
            # （对症下药，而不是给整个文件开白名单：那样以后新增一个未归一化
            # 的赋值点就查不出来了。）
            if re.search(r'api_key\s*=\s*\w+\s*(,|\))', args) or \
               re.search(r'\bapi_key\s*=\s*api_key\b', args):
                assigns = re.findall(r'^\s*api_key\s*=\s*(.+)$', src, re.M)
                if assigns and all('resolve_api_key' in a for a in assigns):
                    continue
                bad.append('%s: api_key 存在未归一化的赋值 (%s)'
                           % (f, [a.strip()[:40] for a in assigns]))
                continue
            bad.append('%s: OpenAI(%s)' % (f, ' '.join(args.split())[:70]))
    eq(bad, [], '存在未经归一化的 OpenAI() 构造点')


# ═══════════════════════════════════════════════════════════════════════════
# 四、lookup_tags 只查主表 —— CLAUDE.md「勿把回落塞进 lookup_tags」
# ═══════════════════════════════════════════════════════════════════════════

@case
def test_lookup_tags_queries_main_table_only():
    """lookup_tags 必须只查 tags；回落语义属于 lookup_user_tags。

    /user_tags 的 in_main_db 徽标与翻译的 source='main_db' 都依赖这个语义，
    一旦把回落塞进去，两者都会错。
    """
    import tageditor.db.build_tag_db as B
    tmp = tempfile.mkdtemp(prefix='inv_')
    try:
        db = os.path.join(tmp, 't.db')
        conn = B.get_conn(db)
        # 主表收录 white_hair，user_tags 收录一个主表没有的
        conn.execute("INSERT INTO tags (name, cn_name) VALUES ('white_hair', '白发')")
        conn.execute("INSERT INTO user_tags (name, cn_name) VALUES ('my_own_tag', '我的标签')")
        conn.commit()

        r = B.lookup_tags(conn, ['white_hair', 'my_own_tag'])
        ok('white_hair' in r, '主表标签应查到')
        ok('my_own_tag' not in r, 'lookup_tags 不该查到 user_tags 里的标签')

        ru = B.lookup_user_tags(conn, ['my_own_tag'])
        ok('my_own_tag' in ru, 'lookup_user_tags 应能查到')
        conn.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@case
def test_user_tags_not_touched_by_schema_rebuild():
    """user_tags 与爬取的 tags 表独立：重建 tags 不影响它。"""
    import tageditor.db.build_tag_db as B
    tmp = tempfile.mkdtemp(prefix='inv_')
    try:
        db = os.path.join(tmp, 't.db')
        conn = B.get_conn(db)
        conn.execute("INSERT INTO user_tags (name, cn_name) VALUES ('keepme', '保留我')")
        conn.commit()
        # 模拟重建：清空并重填 tags
        conn.execute("DELETE FROM tags")
        conn.execute("INSERT INTO tags (name, cn_name) VALUES ('x', 'y')")
        conn.commit()
        n = conn.execute("SELECT count(*) FROM user_tags").fetchone()[0]
        eq(n, 1, 'user_tags 不该被 tags 表的重建清掉')
        conn.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════════════════
# 五、标签名规范化口径一致
# ═══════════════════════════════════════════════════════════════════════════

@case
def test_normalize_tag_key():
    from tageditor.db.build_tag_db import normalize_tag_key
    eq(normalize_tag_key('On Bed'), 'on_bed', '空格→下划线 + 小写')
    eq(normalize_tag_key('  WHITE_HAIR  '), 'white_hair', 'strip + 小写')
    eq(normalize_tag_key(None), '', 'None 安全')
    eq(normalize_tag_key(''), '', '空串')


# ═══════════════════════════════════════════════════════════════════════════
# 六、原子写 —— 用户资产不被写坏
# ═══════════════════════════════════════════════════════════════════════════

@case
def test_atomic_write_keeps_old_content_on_failure():
    """写入失败时目标文件必须保持旧内容（不能变成空文件/半截）。"""
    from tageditor.core.config import write_text_atomic
    tmp = tempfile.mkdtemp(prefix='inv_')
    try:
        p = os.path.join(tmp, 'a.txt')
        write_text_atomic(p, 'OLD')
        try:
            write_text_atomic(p, b'bytes-not-str')   # write 需要 str
        except Exception:
            pass
        eq(open(p, encoding='utf-8').read(), 'OLD', '失败后旧内容必须完好')
        eq([f for f in os.listdir(tmp) if '.tmp' in f], [], '不留 .tmp 垃圾')
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@case
def test_atomic_write_no_tmp_leftover_on_success():
    from tageditor.core.config import write_text_atomic
    tmp = tempfile.mkdtemp(prefix='inv_')
    try:
        for name in ('x.txt', '中文 名.txt', 'a.b.c.txt'):
            p = os.path.join(tmp, name)
            write_text_atomic(p, 'v')
            eq(open(p, encoding='utf-8').read(), 'v', '写入 %s' % name)
        eq([f for f in os.listdir(tmp) if '.tmp' in f], [], '无残留')
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@case
def test_atomic_write_concurrent_same_file():
    """并发写同一文件不能丢内容或报错（Windows 的 os.replace 需要按路径串行）。"""
    import threading
    from tageditor.core.config import write_text_atomic
    tmp = tempfile.mkdtemp(prefix='inv_')
    try:
        p = os.path.join(tmp, 'same.txt')
        errs = []

        def worker(i):
            for k in range(25):
                try:
                    write_text_atomic(p, 'w%d-%d' % (i, k))
                except Exception as e:
                    errs.append('%s: %s' % (type(e).__name__, e))

        ts = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        eq(errs, [], '并发写同一文件不该报错')
        final = open(p, encoding='utf-8').read()
        ok(re.fullmatch(r'w\d-\d+', final), '最终内容应是某次完整写入，实际 %r' % final)
        eq([f for f in os.listdir(tmp) if '.tmp' in f], [], '无残留')
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════════════════
# 七、批量操作的安全语义
# ═══════════════════════════════════════════════════════════════════════════

@case
def test_prepend_tags_is_idempotent():
    """重复点「添加触发词」不能累积（原先是 mychar, mychar, mychar）。"""
    import app as m
    real = os.path.abspath(m.app.config['UPLOAD_FOLDER'])
    before = sorted(os.listdir(real))
    tmp = tempfile.mkdtemp(prefix='inv_')
    try:
        m.app.config['UPLOAD_FOLDER'] = tmp
        c = m.app.test_client()
        with open(os.path.join(tmp, 'a.txt'), 'w', encoding='utf-8') as f:
            f.write('1girl, solo')
        for _ in range(3):
            c.post('/prepend_tags', json={'triggers': 'mychar', 'position': 'start'})
        eq(open(os.path.join(tmp, 'a.txt'), encoding='utf-8').read(),
           'mychar, 1girl, solo', '重复点击不得累积')
        eq(sorted(os.listdir(real)), before, '真实 uploads 不得被触碰')
    finally:
        m.app.config['UPLOAD_FOLDER'] = real
        shutil.rmtree(tmp, ignore_errors=True)


@case
def test_prepend_idempotent_is_item_wise_not_substring():
    """幂等判定按整项：my_character 不该被判成「已有 mychar」。"""
    import app as m
    real = os.path.abspath(m.app.config['UPLOAD_FOLDER'])
    tmp = tempfile.mkdtemp(prefix='inv_')
    try:
        m.app.config['UPLOAD_FOLDER'] = tmp
        c = m.app.test_client()
        with open(os.path.join(tmp, 'a.txt'), 'w', encoding='utf-8') as f:
            f.write('my_character, solo')
        c.post('/prepend_tags', json={'triggers': 'mychar', 'position': 'start'})
        got = open(os.path.join(tmp, 'a.txt'), encoding='utf-8').read()
        ok(got.startswith('mychar, my_character'), '应按整项判定，实际 %r' % got)
    finally:
        m.app.config['UPLOAD_FOLDER'] = real
        shutil.rmtree(tmp, ignore_errors=True)


@case
def test_find_replace_is_exact_match():
    """整项精确匹配：solo 不该命中 solo_focus。"""
    import app as m
    real = os.path.abspath(m.app.config['UPLOAD_FOLDER'])
    tmp = tempfile.mkdtemp(prefix='inv_')
    try:
        m.app.config['UPLOAD_FOLDER'] = tmp
        c = m.app.test_client()
        with open(os.path.join(tmp, 'a.txt'), 'w', encoding='utf-8') as f:
            f.write('1girl, solo, solo_focus')
        c.post('/find_replace', json={'find': 'solo', 'replace': 'alone'})
        eq(open(os.path.join(tmp, 'a.txt'), encoding='utf-8').read(),
           '1girl, alone, solo_focus', '子串不该被替换')
    finally:
        m.app.config['UPLOAD_FOLDER'] = real
        shutil.rmtree(tmp, ignore_errors=True)


@case
def test_batch_routes_reject_non_dict_body():
    """批量路由是「一改全改」的入口，body 非对象必须 400 而不是 500。"""
    import app as m
    real = os.path.abspath(m.app.config['UPLOAD_FOLDER'])
    tmp = tempfile.mkdtemp(prefix='inv_')
    try:
        m.app.config['UPLOAD_FOLDER'] = tmp
        c = m.app.test_client()
        for ep in ('/prepend_tags', '/find_replace'):
            for body in ([1, 2], 'abc', 123):
                r = c.post(ep, json=body)
                eq(r.status_code, 400, '%s body=%r 应 400' % (ep, body))
    finally:
        m.app.config['UPLOAD_FOLDER'] = real
        shutil.rmtree(tmp, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════════════════
# 八、clear_all 的危险默认值（曾因此删光 uploads，必须有防线）
# ═══════════════════════════════════════════════════════════════════════════

@case
def test_clear_all_rejects_unknown_mode():
    """参数不认识时必须 400，绝不回落到默认 mode='all'。"""
    import app as m
    real = os.path.abspath(m.app.config['UPLOAD_FOLDER'])
    tmp = tempfile.mkdtemp(prefix='inv_')
    try:
        m.app.config['UPLOAD_FOLDER'] = tmp
        c = m.app.test_client()
        with open(os.path.join(tmp, 'keep.txt'), 'w', encoding='utf-8') as f:
            f.write('x')
        for body in ([1, 2], 'all', {'mode': 'ALL'}, {'mode': ' ALL '},
                     {'mode': 123}, {'mode': 'bogus'}):
            r = c.post('/clear_all', json=body)
            eq(r.status_code, 400, 'body=%r 应 400' % (body,))
        # 文件必须还在
        ok(os.path.exists(os.path.join(tmp, 'keep.txt')),
           '非法请求不得删掉任何文件')
        eq(sorted(os.listdir(real)), sorted(os.listdir(real)), '真实目录不受影响')
    finally:
        m.app.config['UPLOAD_FOLDER'] = real
        shutil.rmtree(tmp, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════════════════
# 九、前端源码级约定（模板里那些「必须走统一入口」的规则）
# ═══════════════════════════════════════════════════════════════════════════

@case
def test_translation_cell_written_only_via_helper():
    """翻译格必须走 _setTranslationCell，不能直接 div.textContent = tr。

    否则会把「无翻译」占位 span 连同它的 onclick 一起抹掉，
    补出翻译后再清空标签，那一格就永久失去点击入口。
    """
    src = open(ROOT / 'templates/tag_editor.html', encoding='utf-8').read()
    # 去掉注释后，在 helper 自身之外不应再出现直接写单元格的写法
    no_cmt = re.sub(r'/\*.*?\*/', '', src, flags=re.S)
    no_cmt = re.sub(r'//[^\n]*', '', no_cmt)
    start = no_cmt.index('function _setTranslationCell')
    end = no_cmt.index('function ', start + 10)
    outside = no_cmt[:start] + no_cmt[end:]
    ok('div.textContent = tr' not in outside,
       'helper 之外不应直接写翻译格')


@case
def test_modal_guard_does_not_hand_list_ids():
    """弹窗守卫必须按结构判定，不能手写 id 清单（漏一个就会被遮挡触发快捷键）。

    `_anyModalOpen` 本体已抽到 `static/js/tageditor-common.js`（四页共享），
    断言改盯那份文件——守卫口径改一处即全局生效。
    """
    common = (ROOT / 'static/js/tageditor-common.js').read_text(encoding='utf-8')
    ok('function _anyModalOpen' in common, 'common.js 应有 _anyModalOpen')
    ok("querySelectorAll('div.fixed.inset-0')" in common,
       'common.js 的弹窗守卫应按结构判定遮罩层')


@case
def test_no_stale_variable_names_in_templates():
    """曾经导致静默失效的拼写错误不能再回来。"""
    te = open(ROOT / 'templates/tag_editor.html', encoding='utf-8').read()
    no_cmt = re.sub(r'//[^\n]*', '', te)
    ok('currentImgName' not in no_cmt,
       'currentImgName 是未声明变量（正确名是 currentImageName），会导致保存描述抛 ReferenceError')
    # escapeHtml 必须能接受 null/undefined（本体已抽到 common.js，断言改盯那份文件）
    common = (ROOT / 'static/js/tageditor-common.js').read_text(encoding='utf-8')
    m = re.search(r'function escapeHtml\(str\)\s*\{(.{0,200})', common, re.S)
    ok(m and ('str == null' in m.group(1) or 'String(str ==' in m.group(1)),
       'common.js 的 escapeHtml 应先归一化 null/undefined')


@case
def test_nav_link_to_danbooru_page_has_one_name():
    """指向 `/` 的导航项在四个页面必须同名：「Danbooru 标签查询」。

    同一个目标曾有两种叫法 —— 提示词优化页写「标签查询」，标签编辑页与图片
    编辑页写「Danbooru 查询」。而 `/` 是 **Danbooru** 标签查询页（`index()`，
    不是标签编辑页，也不是通用的「标签查询」），两个名字会让用户在页面间跳转时
    以为去的是两个不同功能。新加页面时照抄这一项即可，别自己起名。
    """
    found = 0
    for f in sorted((ROOT / 'templates').glob('*.html')):
        src = f.read_text(encoding='utf-8')
        # 只看**导航按钮**：`tag_editor.html` 正文里还有一个指向 `/` 的行内链接
        # （「到 Danbooru 查询页的新标签入口」，是句子的一部分，不是导航项），
        # 用整段 `<a>` 匹配会把它也要求成常量文案。导航按钮的特征就是那串
        # `border border-gray-300` 样式。
        for m in re.finditer(r'<a ([^>]*?)href="/"([^>]*)>(.*?)</a>', src, re.S):
            if 'border-gray-300' not in m.group(1) + m.group(2):
                continue
            found += 1
            text = re.sub(r'<[^>]+>', '', m.group(3)).strip()
            eq(text, 'Danbooru 标签查询',
               '%s 里指向 `/` 的导航项文案必须是「Danbooru 标签查询」' % f.name)
    ok(found >= 3,
       '应有标签编辑/图片编辑/提示词优化三个页面链接到 `/`，实际找到 %d 个' % found)


# ── Font Awesome 4.7 图标白名单 ──────────────────────────────────────────
# 四个页面 <head> 引的都是 font-awesome@**4.7.0**，而 4.7 的名字与 FA5/6 不同：
#     fa-history  → FA6 叫 fa-clock-rotate-left
#     fa-times    → FA6 叫 fa-xmark
#     fa-trash    → FA6 叫 fa-trash-can
#     fa-magic    → FA6 叫 fa-wand-magic-sparkles
# 写错名字**不报错、不缺字形、日志里也没有痕迹**，只是那个 <i> 渲染成空白 ——
# 按钮变成一个看得见却认不出的空方块。实测「会话记录」按钮（左栏「会话」标题
# 右边那两个小图标之一）就这么消失了：用户反馈「好像没有这个功能」，而功能
# 完好无损，只是入口没图标；连弹窗里的 × 和垃圾桶也都是空的。
# 白名单 = 已核对在 4.7 中存在的名字。加图标要往这里登记，顺手上
# https://cdn.jsdelivr.net/npm/font-awesome@4.7.0/css/font-awesome.min.css
# 搜一下 `.fa-<名字>:before`。
_FA47_ICONS = frozenset("""
    adjust arrow-down arrow-right arrow-up bar-chart book caret-down caret-up
    check chevron-down chevron-left chevron-right clipboard columns crop database
    desktop diamond download eraser exclamation-circle exclamation-triangle expand
    external-link eye eye-slash file-text-o folder-open history i-cursor image
    info-circle keyboard-o language link lock long-arrow-right magic paint-brush
    pencil picture-o plus plus-square question-circle refresh repeat rotate-left
    save search sort-asc sort-desc spin spinner tag tags times trash undo unlock
    upload wrench
""".split())


@case
def test_font_awesome_icons_exist_in_the_loaded_version():
    """模板里的图标名必须在**页面实际引用的** FA 4.7 中存在，否则渲染成空白。

    正则只认**完整的**名字（以字母数字结尾、后面不接 `-` 或单词字符），所以
    `'fa mr-1 fa-chevron-' + (hidden ? 'right' : 'down')` 这类拼接抓不到 ——
    本项目只有 prompt_tool.html 一处，其拼出的两个名字都在白名单里。
    """
    for f in sorted((ROOT / 'templates').glob('*.html')):
        src = f.read_text(encoding='utf-8')
        used = set(re.findall(r'\bfa-([a-z0-9]+(?:-[a-z0-9]+)*)(?![-\w])', src))
        bad = sorted(used - _FA47_ICONS)
        ok(not bad, '%s 用了 FA 4.7 中不存在的图标名：%s（会渲染成空白按钮）'
           % (f.name, '、'.join('fa-' + b for b in bad)))
    # 共享 JS 也扫：搬走的代码若带图标名，白名单会静默失去覆盖
    for f in sorted((ROOT / 'static' / 'js').glob('*.js')):
        src = f.read_text(encoding='utf-8')
        used = set(re.findall(r'\bfa-([a-z0-9]+(?:-[a-z0-9]+)*)(?![-\w])', src))
        bad = sorted(used - _FA47_ICONS)
        ok(not bad, '%s 用了 FA 4.7 中不存在的图标名：%s（会渲染成空白按钮）'
           % (f.name, '、'.join('fa-' + b for b in bad)))


# 在 common.js 里的共享函数（四页共享，别往页面里抄副本）
_COMMON_JS_FUNCS = (
    'escapeHtml', 'showNotification', '_anyModalOpen', 'closeConfirm',
    'confirmOk', 'confirmCancel', 'escCloseModal', 'debounce',
    '_dtextLoadThumbnails', '_loadAssetThumbnail',
)


@case
def test_shared_js_is_wired_and_not_copied_back():
    """四页必须引入 common.js，且不得再内联定义共享函数。

    抽取的目的是「修复只落一处」——页面上又抄一份副本会让两份实现并存，
    改 common.js 不生效、改页面又丢掉共享。两类退化都要响亮报警：
    ① `<script src>` 被删 → 全部行内 onclick 抛 ReferenceError（功能性崩溃）；
    ② 页面又内联 `function escapeHtml(...)` → 静默覆盖/并存（结构性退化）。
    """
    common = (ROOT / 'static/js/tageditor-common.js').read_text(encoding='utf-8')
    for fn in _COMMON_JS_FUNCS:
        ok(re.search(r'function %s\(' % re.escape(fn), common),
           'common.js 应有 %s（被删了？）' % fn)
    for f in sorted((ROOT / 'templates').glob('*.html')):
        src = f.read_text(encoding='utf-8')
        # 必须盯 **script 标签**，不能查「路径字符串出现过」——注释里解释「函数在
        # common.js」的解释文字也含这个路径，查字符串会被自己的注释骗过（实测漏网）
        ok(re.search(r'<script src="/static/js/tageditor-common\.js">', src),
           '%s 必须用 <script src> 引入 tageditor-common.js（删掉后全部行内 onclick 抛 ReferenceError）'
           % f.name)
        for fn in _COMMON_JS_FUNCS:
            ok(not re.search(r'function %s\(' % re.escape(fn), src),
               '%s 不得再内联定义 %s（已在 common.js，页面副本会让两份实现并存）'
               % (f.name, fn))


@case
def test_generator_exit_guard_present_where_cancellable():
    """可取消的 SSE 生成器必须显式 except GeneratorExit。

    GeneratorExit 继承 BaseException，绕过 except Exception；
    不显式接住，cancel_evt.set() 永远不执行，已发出的 LLM 请求会跑满超时。
    """
    src = src_of('translation')
    n_gen = len(re.findall(r'def (_?generate)\(', src))
    n_ge = len(re.findall(r'except GeneratorExit', src))
    ok(n_ge >= 8, 'translation.py 的 GeneratorExit 块数异常（%d），可能被改坏了'
       % n_ge)
    pp = src_of('prompt_tool')
    ok('except GeneratorExit' in pp, 'prompt_tool.py 必须保留 GeneratorExit 处理')
    ok('cancel_evt.set()' in pp, 'GeneratorExit 分支里必须置位取消事件')


@case
def test_cancel_registry_survives_overlapping_runs():
    """同名多轮并发时，两轮必须**同时**可被取消，且结束一轮不得摘掉另一轮。

    原 bug：注册表是 `dict[name] = evt`，后来的覆盖先前的。两个后果：
      1) 先启动的那轮从注册表里消失 → _cancel_all 找不到它 → 永远取消不掉，
         一直烧 GPU 到跑完（日志里 17:51 启动的那轮 20 分钟后还在输出）
      2) 先启动的那轮结束时 `_unregister_cancel(name)` 会把**当前**登记的那个
         摘掉 → 后启动的那轮也失去可取消性
    日志里的表现是同一个操作名出现多组 total（708/667/662/659 同时存在）。
    """
    import tageditor.translate.translation as tr
    saved = dict(tr._active_cancel_events)
    try:
        tr._active_cancel_events.clear()
        old = tr._register_cancel('llm_process_db')
        new = tr._register_cancel('llm_process_db')

        # 关键断言 1：两轮都在册，_cancel_all 必须同时停掉两轮。
        # 用 `dict[name] = {evt}`（只覆盖但仍是集合）的实现能骗过「新轮还活着」
        # 那条断言，却会让 old 从注册表消失 —— 必须显式检查 old 也能被取消。
        tr._cancel_all()
        ok(old.is_set(), '先启动的那一轮也必须可被取消（原先被覆盖丢失）')
        ok(new.is_set(), '后启动的那一轮也必须可被取消')

        # 关键断言 2：重新来过，旧轮结束时只注销自己，不得摘掉新轮
        tr._active_cancel_events.clear()
        old = tr._register_cancel('llm_process_db')
        new = tr._register_cancel('llm_process_db')
        tr._unregister_cancel('llm_process_db', old)
        tr._cancel_all()
        ok(new.is_set(), '旧轮注销后，新轮仍必须可被 _cancel_all 取消')
        ok(tr._has_active('llm_process_db'), '新轮的登记不该被旧轮摘掉')
    finally:
        tr._active_cancel_events.clear()
        tr._active_cancel_events.update(saved)


@case
def test_cancel_name_stops_only_same_name():
    """_cancel_name 只停同名轮次，不牵连同时在跑的其它操作。

    重复点「批量深度翻译」时要把旧轮停掉，但此刻可能正在同步标签库/
    抓共现 —— 那些不该被一个翻译动作带停。
    """
    import tageditor.translate.translation as tr
    saved = dict(tr._active_cancel_events)
    try:
        tr._active_cancel_events.clear()
        llm = tr._register_cancel('llm_process_db')
        sync = tr._register_cancel('sync_tags_db')
        n = tr._cancel_name('llm_process_db')
        eq(n, 1, '只应置位 1 个事件')
        ok(llm.is_set(), '同名操作必须被停')
        ok(not sync.is_set(), '别的操作不得被牵连')
    finally:
        tr._active_cancel_events.clear()
        tr._active_cancel_events.update(saved)


@case
def test_cancel_registry_does_not_leak_or_over_remove():
    """注销必须按对象身份，且全部注销后不留空 key。"""
    import threading
    import tageditor.translate.translation as tr
    saved = dict(tr._active_cancel_events)
    try:
        tr._active_cancel_events.clear()
        a = tr._register_cancel('fetch_cooc')
        b = tr._register_cancel('fetch_cooc')
        # 注销一个不属于自己的 event：不得误伤
        tr._unregister_cancel('fetch_cooc', threading.Event())
        ok(tr._has_active('fetch_cooc'), '外来 event 不该摘掉真实登记')
        tr._unregister_cancel('fetch_cooc', a)
        ok(tr._has_active('fetch_cooc'), '还剩 b，不该整组清掉')
        tr._unregister_cancel('fetch_cooc', b)
        ok(not tr._has_active('fetch_cooc'), '全部注销后应无活跃')
        ok('fetch_cooc' not in tr._active_cancel_events, '空组不该残留 key')
    finally:
        tr._active_cancel_events.clear()
        tr._active_cancel_events.update(saved)


@case
def test_all_unregister_calls_pass_their_event():
    """每个 _unregister_cancel 调用都必须传自己的 evt。

    不传 evt 会走「按名字整组清除」的兜底分支 —— 在并发同名场景下就是原 bug
    的另一种写法。所有调用点的 evt 都在同一闭包作用域内，没有理由省略。
    """
    import ast
    offenders = []
    for f in all_source_files():
        try:
            tree = ast.parse(f.read_text(encoding='utf-8'))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                    and getattr(node.func, 'id', '') == '_unregister_cancel'
                    and len(node.args) < 2):
                offenders.append('%s:%d' % (f.relative_to(ROOT), node.lineno))
    eq(offenders, [], '这些 _unregister_cancel 调用没传 evt')


@case
def test_repeated_trigger_stops_previous_run():
    """重复触发同一操作时，必须先把同名旧轮停掉（并发守卫）。

    源码级断言：路由里要出现 _cancel_name("<自己的名字>")。
    没有它，重复点击会叠出多轮抢同一个模型端点 + 同时写同一个 SQLite。
    """
    tr = src_of('translation')
    ok(re.search(r'_cancel_name\(\s*["\']llm_process_db["\']', tr),
       'llm_process_db 缺少重复触发守卫（应先 _cancel_name 停掉同名旧轮）')
    pp = src_of('prompt_tool')
    ok(re.search(r'_cancel_name\(\s*["\']prompt_tool["\']', pp),
       'prompt_tool 缺少重复触发守卫')


@case
def test_generator_exit_log_does_not_claim_user_action():
    """GeneratorExit 的日志不得写成「前端中断连接」这类断言用户操作的说法。

    该分支覆盖页面刷新/导航/关标签页/浏览器回收，不只是点「中断」。
    原先的措辞让每次刷新页面都留下一条像用户主动取消的日志 —— 排查时被误导。
    """
    tr = src_of('translation')
    for pat in (r'前端中断连接', r'用户已取消', r'用户主动中断'):
        ok(not re.search(pat, tr),
           'GeneratorExit 分支的日志措辞断言了用户操作（%s）；'
           '该分支也覆盖页面刷新/导航' % pat)


@case
def test_session_id_is_whitelisted_not_blacklisted():
    """会话 ID 必须走**白名单**正则，不能靠黑名单过滤 `..` / `/`。

    黑名单永远漏一种写法（URL 编码、反斜杠、Windows 短名、UNC…），而会话目录
    一旦被拼到别处，delete 就是 shutil.rmtree —— 删除是不可逆的。
    白名单让任何不在 `YYYYMMDD-HHMMSS-xxxxxx` 形状内的输入在第一步就被拒。
    """
    ws = src_of('prompt_workspace')
    ok(re.search(r'_SID_RE\s*=\s*re\.compile\(', ws), '找不到 _SID_RE 白名单正则')
    # 正则必须锚定首尾，否则 'xxx<script>' 这类前缀匹配能通过
    m = re.search(r"_SID_RE\s*=\s*re\.compile\(r'([^']+)'\)", ws)
    ok(m, '无法解析 _SID_RE 的模式串')
    pat = m.group(1)
    ok(pat.startswith('^') and pat.endswith('$'),
       '_SID_RE 必须用 ^...$ 锚定首尾，当前: %s' % pat)
    # 交付的判定函数必须真的用它
    ok('_SID_RE.match(sid)' in ws, 'session_dir 必须用 _SID_RE.match 校验 sid')


@case
def test_session_delete_removes_its_own_directory_only():
    """删除会话只能 rmtree 该会话自己的目录（会话是整体，图随它一起删）。

    断言 rmtree 的目标由 session_dir() 解析而来 —— 不能是调用方传来的任意路径。
    """
    ws = src_of('prompt_workspace')
    m = re.search(r'def sessions_delete.*?(?=\n@|\ndef )', ws, re.S)
    ok(m, '找不到 sessions_delete')
    body = m.group()
    ok('session_dir(sid)' in body, 'sessions_delete 必须先用 session_dir(sid) 校验 sid')
    ok('shutil.rmtree(d)' in body, 'rmtree 的目标必须是校验后的 d，不是原始输入')
    ok('rmtree(sid' not in body and 'rmtree(data' not in body,
       'rmtree 绝不能直接吃请求里的原始值')


@case
def test_workspace_write_endpoints_reject_arbitrary_modes():
    """清空参考图的接口不接受任何 mode 参数。

    `/clear_all` 曾把 `" ALL "` 归一化成 `'all'` 而误清空上传目录（不可逆）。
    这里的语义是「清当前会话的图」，作用目录由 sid 解析 —— 不存在「换个参数就清别的」。
    """
    ws = src_of('prompt_workspace')
    m = re.search(r'def images_clear.*?(?=\n@|\ndef )', ws, re.S)
    ok(m, '找不到 images_clear')
    body = m.group()
    ok('request.get_json' not in body,
       'images_clear 不该读取请求体参数（读了就有被用作 mode 的口子）')
    ok('images_dir(sid)' in body, 'images_clear 的目录必须由 sid 解析')


@case
def test_upload_refuses_to_create_a_session():
    """上传参考图到一个不存在的会话必须 404，不能替它把会话建出来。

    早先 `images_dir(sid, create=True)` 的语义就是「顺手建目录」，唯一调用方正是
    上传接口 —— 于是往一个**格式合法但不存在**的 sid 传图会造出一个没有
    `session.json` 的幽灵会话。更糟的是它目录 mtime 最新，列表接口会把它当作
    `current`：用户刷新页面就被带进这个空壳（实测复现过）。

    断言两件事：`images_dir` 不再有 create 形参；上传接口先过 `session_dir(sid)` 存在性。
    """
    ws = src_of('prompt_workspace')
    ok(re.search(r'def images_dir\(sid: str\):', ws),
       'images_dir 不该再带 create 参数（它就是幽灵会话的来源）')
    m = re.search(r'def images_upload.*?(?=\n@|\ndef )', ws, re.S)
    ok(m, '找不到 images_upload')
    body = m.group()
    ok('session_dir(sid)' in body,
       'images_upload 必须先确认会话存在（session_dir 返回 None 就 404）')
    ok('create=True' not in body,
       'images_upload 不该再传 create=True')


@case
def test_session_json_has_no_second_writer_route():
    """`session.json` 只能有两个写入点，别再加一个「随手保存」路由。

    曾有一个 `POST /prompt_sessions/<sid>/save`，全仓无任何调用方，且它能写
    title/prompt/request/result —— 与 `_save_result_to_session` 构成两个真相来源。
    """
    ws = src_of('prompt_workspace')
    ok("'/prompt_sessions/<sid>/save'" not in ws,
       '会话保存路由已删，别加回来（写入点只能有 create_session 与 _save_result_to_session）')


@case
def test_new_session_text_says_images_are_not_carried_over():
    """「新建会话」的文案不得声称参考图会留下。

    图属于会话，新会话是一块**空白**工作台。早先文案写「参考图不会被清空」，
    而代码切到的是空会话、图从工作区消失 —— 用户按字面理解会以为图还在。
    文案与行为对不上比行为本身更伤人：用户不会去验证，只会以为功能坏了。
    """
    src = js_of('prompt_tool.html')
    m = re.search(r'function newSession\(\).*?\n        \}', src, re.S)
    ok(m, '找不到 newSession')
    body = m.group()
    # 只截 message: 到 onOk: 之间的**表达式**，不能拿整个函数体去查字符串 ——
    # 函数里那段注释正解释着「早先写的是『参考图不会被清空』」，整段匹配会把
    # 解释文字本身判成违规。（同类坑在本文件已踩过三次：current_app 两次、这次。）
    mm = re.search(r'message:(.*?)onOk:', body, re.S)
    ok(mm, '找不到 newSession 的 message 字段')
    msg = mm.group(1)
    ok('参考图不会被清空' not in msg,
       'newSession 文案不得写「参考图不会被清空」（新会话是空工作台）')
    ok('含参考图' in msg,
       'newSession 文案应说明新会话连参考图一起是空的')
    ok('applyImages(data)' in body,
       '新建后必须用服务端返回的（空）图片清单刷新图区，否则界面会残留旧图')


@case
def test_dead_sid_in_url_falls_back_to_a_new_session():
    """地址栏 `?sid=` 指向已删除的会话时，必须落到新建一个空会话。

    书签 / 历史记录里的 sid 会失效（会话被删过）。早先这条路径只弹一句报错就
    `return`，`_sid` 留空 —— 页面看起来是完整的，但每次点「运行」只弹
    「会话未就绪」，用户不知道要干什么。要让页面**立刻可用**，不是停在空壳里。
    """
    src = js_of('prompt_tool.html')
    # loadSession 必须留失败回调的口子
    ok(re.search(r'function loadSession\(sid, then, onFail\)', src),
       'loadSession 应接受失败回调（onFail）')
    # 光有形参不够：**必须真的在失败路径上调它**，否则接线是断的 ——
    # 调用方传了 onFail 也永远不会被触发，上面那个空壳状态原样回来。
    # （这条是变异测试补出来的：去掉调用点，原断言全绿。）
    lb = re.search(r'function loadSession\(sid, then, onFail\) \{(.*?)\n        \}', src, re.S)
    ok(lb, '找不到 loadSession 函数体')
    ok(lb.group(1).count('onFail();') >= 2,
       'loadSession 的两条失败路径（resp.error 分支 + catch）都要调 onFail()')
    # init 的 ?sid= 分支必须传失败回调
    m = re.search(r"if \(want\) \{(.*?)\n            \}", src, re.S)
    ok(m, '找不到 init 的 want 分支')
    br = m.group(1)
    ok('loadSession(want' in br and 'onFail' not in br,
       'want 分支应通过第三个参数（匿名函数）传失败回调')
    ok('newSessionQuiet()' in br,
       'sid 失效时必须新建空会话兜底，不能停在没会话的状态')
    # 「请稍候」是误导性文案：没有任何东西在加载，用户会白等
    m2 = re.search(r"if \(!_sid\) \{ showNotification\('([^']*)'", src)
    ok(m2, '找不到 !_sid 的提示')
    ok('请稍候' not in m2.group(1),
       '!_sid 的提示不得写「请稍候」（没有东西在加载，要指路而不是让人等）')


def _fn_body(src, name):
    """截出某个 JS 函数体（源码级断言只该看这个函数，别拿整页去查）。"""
    m = re.search(r'function %s\((.*?)\n        \}' % re.escape(name), src, re.S)
    ok(m, '找不到函数 %s' % name)
    return m.group(0)


@case
def test_delete_session_closes_list_modal_first():
    """删除会话前必须先关掉「会话记录」弹窗，否则确认框被它盖住。

    `#sessions-modal` 与 `#confirm-modal` 都是 `z-50`，而它在 DOM 里更靠后 ——
    同层级时后出现的在上层。不关就直接 showConfirm，确认框整块被盖住，
    点垃圾桶看起来「毫无反应」，**删除会话这个入口实际不可用**。
    """
    src = js_of('prompt_tool.html')
    body = _fn_body(src, 'deleteSession')
    ok('closeSessionsModal()' in body,
       'deleteSession 必须先 closeSessionsModal()（否则确认框被同层级的它盖住）')
    ok(body.index('closeSessionsModal()') < body.index('showConfirm('),
       'closeSessionsModal() 必须在 showConfirm() 之前调用')


@case
def test_rerun_does_not_pop_the_stale_confirm_dialog():
    """新一轮结果到达时不得弹「重算最终提示词」确认框。

    `renderResult` 里先 `onCaptionInput()`（它会经 regenFinal 检查 `_ptFinalDirty`
    并弹确认框），紧接着又 `regenFinal(true)` 强行覆盖 —— 那个框点哪个都一样，
    而且常被进度弹窗盖住、关掉后才冒出来。必须在渲染新结果前复位 `_ptFinalDirty`。
    """
    src = js_of('prompt_tool.html')
    body = _fn_body(src, 'renderResult')
    ok('_ptFinalDirty = false' in body, 'renderResult 必须复位 _ptFinalDirty')
    ok(body.index('_ptFinalDirty = false') < body.index('onCaptionInput()'),
       '_ptFinalDirty 的复位必须在 onCaptionInput() 之前')


@case
def test_diff_renders_only_changed_rows():
    """diff 列表只渲染增删改，保留行不显示 —— 但下标必须仍是 _ptDiff 的原始下标。

    一轮 53 条里可能只有 5 条真的动了，keep 混在列表里会把改动淹掉。
    真正的风险在**下标**：`toggleRow` / `applySuggestion` 都拿行下标回查
    `_ptDiff`，若为了「只渲染可见行」而用 filter 压紧下标，这两个回调就会
    操作到别的标签上 —— 界面上看不出异常，只是勾选/换词落到了别的行。
    所以这里同时守两件事：不渲染 keep，且行内回调仍用原始下标 `i`。
    """
    body = _fn_body(js_of('prompt_tool.html'), 'renderDiff')
    ok("if (d.op === 'keep') continue;" in body,
       '保留行不应渲染（列表只显示增删改）')
    ok("toggleRow(' + i + '" in body,
       '行内回调必须用循环变量 i（= _ptDiff 的原始下标）')
    ok('.filter(' not in body,
       '不得用 filter 压紧下标 —— toggleRow/applySuggestion 会操作到别的行')


@case
def test_session_title_comes_from_the_prompt():
    """会话标题取**提示词的标签行**，不是优化要求；且不落盘。

    要求说的是「这轮让它干什么」（精简到 30 条 / 去掉查不到的 / 按图2 改动作），
    同一份提示词迭代三轮就并排出现三条互不相干的标题 —— 认不出是同一个角色的图。
    实测三条真实会话的标题分别是「用图1的人物配图2的动作，背景改成黄昏的街道」
    「提示词代表图2的人物特征和衣服特征 请将图2的人」「当前提示词是负面提示词 ，
    还可以添加什么负面提示」，后两条还硬截在 24 字上、停在半句话中间。

    另一条不能退回的点：**标题不落盘**。存一份就与 prompt 脱节 —— 改了提示词
    重跑，磁盘上还是上一轮的标题，而列表显示的是现算的（两个真相来源）。
    """
    from tageditor.translate.prompt_workspace import session_title
    # 提示词优先，且只取首个换行之前（描述段进标题会被截成半句散文）
    eq(session_title({'prompt': 'yoshimiya mafuyu, 1girl, solo\nA girl by the window...',
                      'request': '精简到 30 条'}),
       'yoshimiya mafuyu, 1girl, solo',
       '标题应取提示词的标签行，不得混进描述段')
    # 超长要加省略号（早先硬截在 24 字上，看起来像语病）
    t = session_title({'prompt': 'x' * 60})
    ok(t.endswith('…') and len(t) == 41, '超长标题要截断并加省略号，实际拿到 %r' % t)
    # 没跑过（无 prompt）才退到要求
    eq(session_title({'request': '精简到 30 条'}), '精简到 30 条',
       '无提示词时才退到优化要求')
    eq(session_title({}), '空会话', '两者都没有时显示「空会话」')
    # 落盘不许再写 title
    m = re.search(r'def create_session.*?(?=\n@|\ndef )', src_of('prompt_workspace'), re.S)
    ok(m, '找不到 create_session')
    ok("'title'" not in m.group(),
       'create_session 不该再往 session.json 写 title（读时现算，存了就是两个真相来源）')
    m2 = re.search(r'def _save_result_to_session.*?(?=\n@|\ndef )', src_of('prompt_tool'), re.S)
    ok(m2, '找不到 _save_result_to_session')
    ok("data['title']" not in m2.group(),
       '_save_result_to_session 不该再写 title（改提示词重跑后它还是上一轮的）')


@case
def test_reference_thumbnails_are_not_cropped():
    """参考图缩略图不得裁剪图片：`object-cover` 会把竖图裁成中间一条。

    实测这批参考图**全是竖图**（比例 0.31~0.78 的立绘），而单列格子有 216px 宽。
    `object-cover` 要填满 `h-20`（80px）的横条，就得按宽度铺满（竖图撑到近 700px
    高）再裁掉上下近九成 —— 一张立绘只剩中间一条，等于看不见内容。
    改为两列 + 2/3 竖框 + `object-contain`：完整显示，留白由浅灰底兜着。
    """
    html = (ROOT / 'templates/prompt_tool.html').read_text(encoding='utf-8')
    body = _fn_body(js_of('prompt_tool.html'), 'renderImageGrid')
    ok('object-contain' in body, '缩略图必须用 object-contain（object-cover 会裁掉竖图）')
    ok('object-cover' not in body, '缩略图不得再用 object-cover')
    m = re.search(r'id="pt-image-grid"[^>]*class="([^"]*)"', html)
    ok(m, '找不到 pt-image-grid 容器')
    cls = m.group(1)
    ok('grid-cols-2' in cls, '参考图容器应为两列网格（单列时格子太宽，竖图填不满）')
    ok('content-start' in cls,
       '参考图容器需要 content-start，否则 grid 行会被 flex-1 的高度均分撑成大格子')


@case
def test_progress_modal_settles_its_numbers_on_complete():
    """complete 到达时要把阶段计数补到 N / N，并在成功后自动关窗。

    原先只把百分比写成 100%，count 原样停在最后一个 progress（如「5 / 6」），
    同一个弹窗里「5 / 6」与「100%」并存 —— 看着像卡在第五步没跑完。subtitle
    也一直停在调用点写死的静态文案上（本页传的 summaryFn 是 null，没有兜底）。
    有 warnings 时**不得**自动关窗：那些提示正是「结果为什么不对」的解释。
    """
    src = js_of('prompt_tool.html')
    m = re.search(r"currentEventType === 'complete'\) \{(.*?)currentEventType === 'fatal'",
                  src, re.S)
    ok(m, '找不到 complete 分支')
    body = m.group(1)
    mc = re.search(r'count\.textContent\s*=\s*([^;]+);', body)
    ok(mc, 'complete 必须重设 count，否则停在「5 / 6」与「100%」自相矛盾')
    ok('_tn' in mc.group(1) and 'current' not in mc.group(1),
       'count 要设成「总数 / 总数」，不能再用 _lastProgress.current（那还是 5 / 6）')
    ok('closeProgressModal()' in body, '成功后应自动关窗（结果已渲染在右侧面板）')
    ok('_warns.length' in body, '有 warnings 时应跳过自动关窗')
    ok(body.index('_warns.length') < body.index('closeProgressModal()'),
       '自动关窗必须在「无 warnings」分支里，不能无条件关')


@case
def test_clear_result_panel_also_clears_the_plan_panel():
    """清空结果面板时必须连「检索计划」面板与描述提示一起清。

    这两处只有 renderPlan / onCaptionInput 会写。漏清时切到没有结果的会话，
    diff 与描述都空了，却留着**上一个会话**的「点 N 个工具 · 命中 M 条」和
    工具命中条目 —— 用户会把上一个会话的结论当成这次的。
    """
    src = js_of('prompt_tool.html')
    body = _fn_body(src, 'clearResultPanel')
    for eid in ('pt-cand-count', 'pt-candidates-panel', 'pt-caption-hint'):
        ok(eid in body, 'clearResultPanel 必须清 #%s（否则残留上一个会话的内容）' % eid)


@case
def test_session_async_responses_are_guarded():
    """会话相关的异步响应必须校验「发起时的会话 == 现在的会话」。

    上传/删图/清空在途时用户可能已经切走会话。不校验的话旧会话的图清单会被写进
    新会话的 `_images`，而缩略图 URL 用**当前** sid 拼 → 全 404，且「图1/图2」
    与实际送模型的图**静默错位**（后端注释里最担心的那种错）。
    同理连点两次「加载」时两个 fetch 竞争，后回来的决定显示哪个会话。
    """
    src = js_of('prompt_tool.html')
    # 新增异步函数时必须加进这个元组 —— 漏了就等于「新函数没有守卫，也没有断言在盯」
    for fn in ('uploadImages', 'deleteImage', 'toggleImage'):
        body = _fn_body(src, fn)
        ok(re.search(r'var sid = _sid;', body),
           '%s 必须记住发起时的 sid' % fn)
        ok('sid !== _sid' in body,
           '%s 的响应必须校验 sid 未变（切走了就丢弃）' % fn)
    lb = _fn_body(src, 'loadSession')
    ok('var seq = ++_sessionSeq' in lb,
       'loadSession 必须在发起 fetch 前取号（同 tag_editor 的 loadSeq）')
    # 断言**每个**异步续体都有守卫，不能只查「出现过」。loadSession 有 `.then` 与
    # `.catch` 两条续体，各需一道；写成子串存在性时，删掉其中一条另条仍命中，
    # 断言照样全绿 —— 变异测试实测漏网过。
    ok(lb.count('seq !== _sessionSeq') >= 2,
       'loadSession 的 .then 与 .catch 各自都要丢弃过期响应（守卫数应 ≥2，实际 %d）'
       % lb.count('seq !== _sessionSeq'))


@case
def test_selection_resolves_in_workspace_order():
    """`resolve_selection` 的四条规则 —— 编号口径唯一的出处，必须直接测。

    这是「用户说的图N == 模型看到的图N」这条约定的落点。它错的方式全是**静默**的：
    提示词看着正常，人物与动作却张冠李戴，没有异常也没有日志。
    """
    from tageditor.translate.prompt_workspace import MAX_SUBMIT_IMAGES, resolve_selection

    eq(MAX_SUBMIT_IMAGES, 5, '单次提交上限是 5 张')
    names = ['a.png', 'b.png', 'c.png', 'd.png', 'e.png', 'f.png']

    # 用户勾了哪几张不重要，**编号一律按工作区序**——勾选先后序会让同一套勾选在
    # 重勾/刷新后得到不同编号，而「图N」被写在了会持久化的优化要求里
    eq(resolve_selection(names, ['e.png', 'b.png', 'd.png']), ['b.png', 'd.png', 'e.png'],
       '编号顺序必须是工作区自然序，不是勾选先后序')

    # 引用已删文件的项自然消失，且不打乱其余顺序
    eq(resolve_selection(names, ['b.png', 'gone.png', 'e.png']), ['b.png', 'e.png'],
       '悬空引用要被丢弃（只在写入时清理会留下窗口）')

    # 没有勾选记录 = 默认全选并截到上限。这条覆盖老会话 / 新会话 / selection.json
    # 被手工删掉；默认成「一张不选」会让存量会话升级后静默变成纯文本模式
    eq(resolve_selection(names, None), ['a.png', 'b.png', 'c.png', 'd.png', 'e.png'],
       'stored 为 None 时默认全选（截到上限），不能默认成一张不选')

    # 存储里超了上限也要截断（防手工改坏 selection.json）
    eq(len(resolve_selection(names, names)), MAX_SUBMIT_IMAGES,
       'stored 超过上限时应截断')

    # 一张都不勾是合法状态（纯文本模式），不能被当成「没记录」而回落成全选
    eq(resolve_selection(names, []), [], '显式勾了 0 张就是 0 张，不能回落成全选')

    eq(resolve_selection([], None), [], '空工作区解析出空清单')
    eq(resolve_selection(names, 'not-a-list'), names[:5],
       'stored 类型不对时按「没记录」处理，不要抛错')


@case
def test_reference_image_cap_limits_submission_not_workspace():
    """上限只管「送模型」，不管「工作区存多少」，且**不在上传处**拦。

    早先一个 `MAX_IMAGES = 6` 同时管两件事。改成只约束提交后，上传路径里就不该
    再出现任何数量判断 —— 留着它，用户攒到第 7 张就会被无声拒绝，而界面显示的
    是「数量不限」。
    """
    ws = src_of('prompt_workspace')
    m = re.search(r'def images_upload.*?(?=\n@|\ndef )', ws, re.S)
    ok(m, '找不到 images_upload')
    body = m.group()
    # 只查这个函数体内有没有拿数量去比 —— 不能用「MAX_SUBMIT_IMAGES 出现过就算」
    # 这类反向写法（上传逻辑要写勾选态，本来就会引用它）
    ok(not re.search(r'len\(existing\)\s*\+.*>=', body),
       'images_upload 不该再按数量拒图（工作区不限量）')
    ok('已达上限' not in body, 'images_upload 不该再有「已达上限」这种拒绝理由')
    ok('write_selection' in body,
       '上传后应把新图补进勾选（不补勾用户会以为「传了就是送了」）')

    # 提交路径必须真的按上限截断，且用 resolve_selection（唯一出处）
    pt = src_of('prompt_tool')
    ok('selected_images(sid)' in pt,
       'prompt_adjust 应只取勾选的图（selected_images），不是工作区全部')
    ok('MAX_SUBMIT_IMAGES' in pt, '提交路径要引用单次上限')


@case
def test_prompt_adjust_reads_images_from_its_own_session():
    """prompt_adjust 的参考图必须来自会话目录，而不是 uploads/。

    「提示词优化器与标签编辑完全独立」这条约定在代码层的落点：一旦这里回头去
    读 uploads/（或前端传图片名列表），两套功能就重新纠缠上了。
    """
    pt = src_of('prompt_tool')
    ok('list_images(sid)' in pt, '参考图应从会话取（list_images(sid)）')
    ok('resolve_image(sid,' in pt, '参考图路径应经 resolve_image(sid, name) 校验')
    ok("config['UPLOAD_FOLDER']" not in pt,
       'prompt_tool 不该再引用 UPLOAD_FOLDER —— 本页与标签编辑独立')
    ok('/uploads/' not in pt, 'prompt_tool 不该拼 /uploads/ 路径')


@case
def test_prompt_tool_page_does_not_inherit_uploads():
    """页面路由不得把 uploads/ 的图片列表注入 prompt_tool.html。

    注入了就等于「本页的图片来自标签编辑」，与独立工作区的设计直接冲突。
    """
    src = (ROOT / 'app.py').read_text(encoding='utf-8').replace('\r\n', '\n')
    m = re.search(r'def prompt_tool_page\(\):(.*?)(?=\n@app\.route|\ndef )', src, re.S)
    ok(m, '找不到 prompt_tool_page')
    body = m.group(1)
    ok('get_image_files' not in body,
       'prompt_tool_page 不该调用 get_image_files（那是 uploads/ 的列表）')
    ok("render_template('prompt_tool.html'" in body, '应渲染 prompt_tool.html')


@case
def test_multiple_images_are_numbered_for_the_model():
    """多图送模型时必须打「图N」标记。

    用户用「用图1 的人物配图2 的动作」指代，而 OpenAI 的图片数组没有编号字段 ——
    模型只能按出现顺序数。不打标记时数错是**静默错误**：提示词看着正常，
    人物与动作却张冠李戴。标记是让「图N」在上下文里有锚点。
    """
    pt = src_of('prompt_tool')
    m = re.search(r'def _build_image_content.*?(?=\ndef )', pt, re.S)
    ok(m, '找不到 _build_image_content')
    body = m.group()
    ok("'type': 'text'" in body and 'label_prefix' in body,
       '_build_image_content 必须插入文字标记（[参考图N]）')
    ok('len(images) > 1' in body,
       '标记应仅在多图时插入（单图没有编号歧义，插了是噪声）')
    # 规划轮与改写轮都要带图
    ok('images=images' in pt, '规划轮 _call_planner 必须收到参考图')


@case
def test_warnings_visible_when_some_reference_image_fails():
    """单张参考图读取失败时必须警告并点名。

    静默丢一张会让编号错位：用户说「图2」指的是第 2 张，模型看到的却是第 3 张 ——
    错位是静默的，改出来的提示词看起来完全正常。
    """
    pt = src_of('prompt_tool')
    ok(re.search(r'参考图 \{?\w+\}? ?读取失败|读取失败.*未送模型', pt),
       '参考图读取失败必须写进 warnings 并说明未送模型')


@case
def test_session_storage_does_not_use_current_app():
    """会话存储模块不得依赖 Flask 的应用上下文。

    `_save_result_to_session` 在 **SSE 生成器内部**执行（complete 事件之前），
    那时请求上下文已拆 —— 读 `current_app.config` 会抛
    `RuntimeError: Working outside of application context`。后果很隐蔽：
    模型跑完了、结果也 yield 给前端了，**只是没存下来**，用户下次进来发现会话是空的。
    实测踩到过，故用模块级 configure() 定目录。
    """
    ws = src_of('prompt_workspace')
    import ast
    # 必须用 **AST** 判定，不能按字面查 'current_app'：文件里的注释正解释着
    # 「为什么不用 current_app.config」，文本匹配会把解释文字本身判成违规。
    # 实测写错两次：先是 `'current_app' not in ws` 被注释命中，
    # 改成 `current_app\s*\.` 后注释里的 `` `current_app.config` `` 又被命中。
    names = {n.id for n in ast.walk(ast.parse(ws)) if isinstance(n, ast.Name)}
    ok('current_app' not in names,
       'prompt_workspace 不该引用 current_app（SSE 生成器里没有应用上下文）')
    ok('def configure(' in ws, '应提供模块级 configure(path) 来定目录')
    ok('_workspace_base' in ws, '目录应由模块级变量持有，不依赖请求上下文')


@case
def test_js_comment_stripper_survives_regex_literals():
    """剥注释的工具本身也要有断言 —— 它坏掉时是**静默**的。

    样本用的就是 `escapeHtml` 里那一行真代码。`replace(/"/g,'&quot;')` 让朴素的
    引号状态机以为字符串开始，而紧跟的 `replace(/'/g,'&#39;')` 又把它「闭合」，
    于是**从这一行起整个文件相位错开**、一半注释漏剥。漏剥的后果正是这个工具要
    防的事：断言去匹配注释里的解释文字，而且不报错，只在别处莫名其妙地失败。
    实测踩到，排查花了很久 —— 所以这里把那行真代码连同一个「行注释里提到代码」
    的陷阱一起喂进来，工具的回归不该只靠人眼。
    """
    sample = (
        ".replace(/&/g,'&amp;').replace(/\"/g,'&quot;').replace(/'/g,'&#39;')\n"
        "var half = total / 2;                 // 注释里提到 onCaptionInput()\n"
        "var url = 'http://x/';                // 字符串里的 // 不是注释\n"
        "var re = /'/g;                        // 同一行：正则里的引号后面还跟着注释\n"
        "/* 块注释里也提到 _ptFinalDirty */\n"
        "var t = `a${half}b`;                  // 模板串\n"
        "return /[/]/g.test(url);              // 字符类里的斜杠\n"
    )
    got = strip_js_comments(sample)
    # 注释必须全部消失（含正则所在行之后的每一行）
    ok('onCaptionInput()' not in got, '正则字面量之后的按行注释没被剥掉（状态机被带偏）')
    ok('_ptFinalDirty' not in got, '块注释没被剥掉')
    ok('不是注释' not in got, '字符串后面的行注释没被剥掉')
    ok('同一行' not in got, '与正则同一行的注释没被剥掉')
    # 代码必须原样保留
    ok(".replace(/'/g,'&#39;')" in got, '正则字面量被吞掉了')
    ok("var re = /'/g;" in got, '含引号的正则字面量被吞掉了')
    ok('total / 2' in got, '除号被误判成正则开头')
    ok("'http://x/'" in got, '字符串里的 // 被误判成注释')
    ok('`a${half}b`' in got, '模板字符串被吞掉了')
    ok('/[/]/g' in got, '字符类里的斜杠被误判成正则结束')
    ok('return /[/]/g' in got, '`return` 后面的正则被误判成除号')


@case
def test_js_comment_stripper_bounds_a_misjudgement_to_one_line():
    """启发式判错正则时，误判必须**到此为止**，不能污染后面的每一行。

    `/` 是正则还是除号靠前一个词判断，这个判断**必然有判错的时候**（`}` 后的
    正则、我没想到的语法）。所以剥注释按行复位：`'` `"` 不能跨行，遇到换行一律
    复位。代价是判错那**一行**的余下部分被当成字符串（该行注释会漏），换来的是
    误判不会顺着文件蔓延 —— 原先那个 bug 之所以难查，正是因为它蔓延了：

        `escapeHtml` 里 `/"/g` 与 `/'/g` 一开一合把状态机带偏 → 从该行起
        **整个文件相位错开**，一半注释漏剥 → 断言去匹配注释里的解释文字。

    这里用 `}` **紧跟**正则造一个确定会被判错的输入（`}` 被当作值结尾 = 除号；
    中间隔着 `;` 就不会判错了 —— `;` 之后一定是正则，第一版样本正是错在隔了分号，
    变异体全绿）。断言：第 2、3 行的注释照样剥干净（复位生效）。
    """
    sample = (
        "function f() {} /'/g.test(s);   // 本行注释会漏（故意的，见 docstring）\n"
        "var after = 1;                  // 第二行的注释必须剥掉（说明误判没蔓延）\n"
        "var next = 2;                   // 第三行同理\n"
    )
    got = strip_js_comments(sample)
    ok('第二行' not in got and '第三行' not in got,
       '启发式判错后误判蔓延到了后续行 —— 按行复位失效，注释会大面积漏剥')
    ok('var after = 1;' in got and 'var next = 2;' in got,
       '按行复位把真正的代码也吃掉了')


@case
def test_sse_total_step_count_is_six():
    """prompt_tool 的 total 必须是 6 —— CLAUDE.md:『别把 total 改成 7』。

    代码里是 `total = 6` 赋值后再 `'total': total`，不是字面量 'total': 6。
    """
    src = src_of('prompt_tool')
    m = re.search(r'^\s*total\s*=\s*(\d+)', src, re.M)
    ok(m, '找不到 total 的赋值')
    eq(int(m.group(1)), 6, 'prompt_tool 的 SSE total 应为 6')


@case
def test_upload_next_pages_use_endpoint_names():
    """_UPLOAD_NEXT_PAGES 的值必须是 endpoint 名，写错会 BuildError → 上传后 500。"""
    import app as m
    from tageditor.ops.file_ops import _UPLOAD_NEXT_PAGES
    eps = {r.endpoint for r in m.app.url_map.iter_rules()}
    for key, val in _UPLOAD_NEXT_PAGES.items():
        ok(val in eps, '_UPLOAD_NEXT_PAGES[%r]=%r 不是有效 endpoint' % (key, val))


@case
def test_session_lifecycle_end_to_end():
    """会话全生命周期跑一遍（真请求，真落盘）—— 尤其是**删除只删自己**。

    这条必须行为化，不能靠源码里的正则：`sessions_delete` 执行的是
    `shutil.rmtree`，**不可逆**，而它唯一的防线是 sid 的白名单正则。正则写成
    `.*` 也能让「源码里有 _SID_RE」这类断言通过，但后果是删库。所以这里真的
    发请求：建 → 传图 → 读回 → 清图 → 删，并确认界外的文件**一个都没少**。

    另外顺带守住两个已在别处用文本断言盯着、但只有跑起来才看得见的行为：
      · 往**格式合法但不存在**的 sid 传图 → 404 且**不得把会话建出来**
        （否则它 mtime 最新，会被列表接口当成 current，刷新后进空壳）
      · 切换工作区根目录必须靠模块级 configure()，不能用 current_app
    测试把工作区指到临时目录，**绝不碰真的 prompt_workspace/**。
    """
    import io
    import shutil
    import tempfile

    import app as m
    from tageditor.translate import prompt_workspace as ws

    tmp = tempfile.mkdtemp(prefix='pt_ws_test_')
    prev = ws._workspace_base
    try:
        ws.configure(tmp)
        # 界外文件：删除会话时绝不能碰到它（在 sessions/ 之外，但在同一临时根下）
        outsider = os.path.join(tmp, 'important.txt')
        with open(outsider, 'w', encoding='utf-8') as f:
            f.write('别动我')
        c = m.app.test_client()

        r = c.post('/prompt_sessions/new')
        eq(r.status_code, 200, '新建会话应 200')
        sid = r.get_json()['sid']
        ok(re.match(r'^\d{8}-\d{6}-[0-9a-f]{6}$', sid),
           'sid 应形如 YYYYMMDD-HHMMSS-xxxxxx，实际 %r' % sid)
        sess = os.path.join(tmp, 'sessions', sid)
        ok(os.path.isdir(os.path.join(sess, 'images')), '新会话应带 images/ 目录')
        ok(os.path.isfile(os.path.join(sess, 'session.json')),
           '新会话应写 session.json（不然列表读不到它）')

        # 列表把最新的当 current —— 前端靠它恢复上次现场
        eq(c.get('/prompt_sessions').get_json()['current'], sid,
           '列表的 current 应是最新的会话')

        # 传一张真 PNG（magic 不重要，allowed_file 只看扩展名）
        png = (b'\x89PNG\r\n\x1a\n' + b'\x00' * 64)

        def _upload(*names):
            return c.post('/prompt_sessions/%s/images' % sid,
                          data={'files': [(io.BytesIO(png), n) for n in names]},
                          content_type='multipart/form-data')

        r = _upload('ref1.png')
        eq(r.status_code, 200, '上传参考图应 200')
        eq(r.get_json()['count'], 1, '上传后应有 1 张图')

        r = c.get('/prompt_sessions/%s' % sid)
        imgs = r.get_json()['images']
        eq(len(imgs), 1, '读回应有 1 张图')
        eq(imgs[0]['submit_index'], 1, '首图编号应为 1（编号即提示词里的「图1」）')
        ok(imgs[0]['selected'], '没有勾选记录时默认全选（否则存量会话升级后静默变纯文本）')

        # —— 工作区**不设上限**：一次传 8 张，一张都不该被拒 ——
        r = _upload(*['ref%d.png' % i for i in range(2, 10)])
        d = r.get_json()
        eq(d['count'], 9, '工作区应有 9 张图（工作区不限量）')
        eq(d['rejected'], [], '工作区不该再因为数量拒图')
        eq(d['max_submit'], 5, '单次提交上限应是 5')
        eq(d['selected_count'], 5, '自动勾选只能补到上限 5 张')
        eq(len(d['auto_selected']), 4, '新传的 8 张里只有 4 张补得进（第 1 张已占一格）')

        # —— 勾选：编号必须等于**送模型的顺序**，即勾选的按工作区序连续编 1..K ——
        r = c.post('/prompt_sessions/%s/images/select' % sid,
                   json={'names': ['ref3.png', 'ref7.png', 'ref9.png', 'ref2.png', 'ref5.png']})
        eq(r.status_code, 200, '设置勾选应 200')
        rows = r.get_json()['images']
        picked = [(x['name'], x['submit_index']) for x in rows if x['selected']]
        eq(picked, [('ref2.png', 1), ('ref3.png', 2), ('ref5.png', 3),
                    ('ref7.png', 4), ('ref9.png', 5)],
           '编号应按**工作区自然序**连续编（不是勾选先后序）——用户把「图N」写在了'
           '会持久化的优化要求里')
        eq(sum(1 for x in rows if not x['selected'] and x['submit_index'] != 0), 0,
           '未勾选的图不该带编号')

        # 勾选态必须落盘（刷新/换会话回来还在）
        sel_file = os.path.join(sess, 'selection.json')
        ok(os.path.isfile(sel_file), '勾选态应写进 selection.json')
        rows2 = c.get('/prompt_sessions/%s' % sid).get_json()['images']
        eq([(x['name'], x['submit_index']) for x in rows2 if x['selected']], picked,
           '重新加载会话应还原同一套编号')

        # 超上限：前端会拦，但后端是防线
        r = c.post('/prompt_sessions/%s/images/select' % sid,
                   json={'names': ['ref%d.png' % i for i in range(1, 8)]})
        eq(r.status_code, 400, '一次勾 7 张应被拒（单次上限 5）')

        # 非法名字 / 重复名字：服务端归一（丢弃 + 去重），不往下传
        r = c.post('/prompt_sessions/%s/images/select' % sid,
                   json={'names': ['../../secret', 'ref4.png', 'ref4.png']})
        eq(r.status_code, 200, '含非法名与重复名应被归一而不是报错')
        eq([(x['name'], x['submit_index']) for x in r.get_json()['images'] if x['selected']],
           [('ref4.png', 1)], '非法名要丢弃、重复名要去重')
        eq(c.post('/prompt_sessions/%s/images/select' % sid,
                  json={'names': 'not-a-list'}).status_code, 400,
           'names 不是数组应 400')

        # 删掉一张已勾选的图 → 其余图重新连续编号（不能留下空洞）
        c.post('/prompt_sessions/%s/images/select' % sid,
               json={'names': ['ref2.png', 'ref5.png', 'ref8.png']})
        c.post('/prompt_sessions/%s/images/delete' % sid, json={'name': 'ref2.png'})
        eq([(x['name'], x['submit_index'])
            for x in c.get('/prompt_sessions/%s' % sid).get_json()['images'] if x['selected']],
           [('ref5.png', 1), ('ref8.png', 2)],
           '删掉已勾选的图后，其余图应重新连续编号')

        eq(c.post('/prompt_sessions/%s/images/clear' % sid).get_json()['count'], 0,
           '清空后应为 0 张')
        ok(os.path.isdir(sess), '清空参考图**不该**连会话一起没了')
        eq(c.get('/prompt_sessions/%s' % sid).get_json()['selected_count'], 0,
           '清空后勾选态也该是空的')

        # —— 幽灵会话：格式合法但不存在的 sid 传图，必须 404 且不建目录 ——
        ghost = '20200101-000000-abcdef'
        eq(c.post('/prompt_sessions/%s/images' % ghost,
                  data={'files': (io.BytesIO(png), 'x.png')},
                  content_type='multipart/form-data').status_code, 404,
           '往不存在的会话传图应 404')
        ok(not os.path.exists(os.path.join(tmp, 'sessions', ghost)),
           '传图绝不能替不存在的 sid 把会话建出来（幽灵会话）')

        # —— 路径遍历：白名单正则该在拼路径之前就拒掉 ——
        # （这些靠 is_within_directory 也能兜住，所以单独验它们**测不出**白名单失效）
        for bad in ('..', '../../etc', 'a/../../b', '....//....//x',
                    '20200101-000000-ABCDEF', '20200101-000000-abcde'):
            eq(c.post('/prompt_sessions/%s/delete' % bad).status_code, 404,
               '非法 sid %r 的删除请求应 404' % bad)

        # —— 白名单**真正**载荷的那一半：目录名不是 sid 形状就不该被当会话 ——
        # 上面那批遍历请求即使白名单退化成 `.*` 也仍会被 is_within_directory 拦住，
        # 于是断言全绿、白名单其实已经没了。这里放一个**手工建在 sessions/ 下**的
        # 目录：它路径合法、目录真实存在，只有白名单能拦住删除。list_sessions 的
        # 注释写的「不认识的目录一律不碰（用户手放的东西不该被误删）」就是这个。
        stray = os.path.join(tmp, 'sessions', 'not-a-session')
        os.makedirs(stray, exist_ok=True)
        with open(os.path.join(stray, 'keep.txt'), 'w', encoding='utf-8') as f:
            f.write('手工放的，别删')
        eq(c.post('/prompt_sessions/not-a-session/delete').status_code, 404,
           '名字不是 sid 形状的目录不该被当成会话')
        ok(os.path.isfile(os.path.join(stray, 'keep.txt')),
           '白名单失效：sessions/ 下手工放的目录被 rmtree 删掉了（不可逆）')

        # —— 真删除：只删自己 ——
        eq(c.post('/prompt_sessions/%s/delete' % sid).status_code, 200, '删除应 200')
        ok(not os.path.exists(sess), '删除会话后目录应消失')
        ok(os.path.isfile(outsider), '删除会话**绝不能**碰到界外的文件')
        eq(c.get('/prompt_sessions/%s' % sid).status_code, 404, '删掉后再读应 404')
    finally:
        ws.configure(prev if prev else 'prompt_workspace')
        shutil.rmtree(tmp, ignore_errors=True)


@case
def test_no_print_isms_in_log_calls():
    """log.* 调用不能带 print 专属参数，也不能缺 msg。

    这是 print→logging 机械迁移留下的典型伤：只换了函数名，参数原样保留。
    实际踩到的是 `log.info(f"...", end='')`（进度条想用 \\r 原地刷新）——
    logging 的 `Logger._log()` 不接受 `end`，直接 TypeError。在那个场景里
    它被外层 try/except 吞掉，表现为「下载失败: Logger._log() got an
    unexpected keyword argument 'end'」，看起来像网络问题，实际是日志调用写错。
    `log.info()`（无参）同理。

    这类错误**导入期不报**，只在对应分支被执行时才炸，所以必须靠源码级断言守。
    """
    import ast
    bad = []
    for p in all_source_files():
        src = p.read_text(encoding='utf-8')
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        for n in ast.walk(tree):
            if not isinstance(n, ast.Call):
                continue
            f = n.func
            # 只认 `log.<level>(...)` 形态（各模块的 logger 变量名就是 log）
            if not (isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name)
                    and f.value.id == 'log'
                    and f.attr in ('info', 'warning', 'error', 'debug', 'critical',
                                   'exception')):
                continue
            for kw in n.keywords:
                if kw.arg in ('end', 'sep', 'flush', 'file'):
                    bad.append('%s:%d log.%s(...) 带了 print 专属参数 %s='
                               % (p, n.lineno, f.attr, kw.arg))
            if not n.args and not n.keywords:
                bad.append('%s:%d log.%s() 缺少 msg 参数'
                           % (p, n.lineno, f.attr))
    eq(bad, [], '存在 print 式残留的 log 调用')


@case
def test_bangumi_circuit_breaker():
    """Bangumi 连续失败达阈值后必须停止发包。

    为什么这条重要：Bangumi 服务故障时（实测其对每个请求返 500，因为它的
    Meilisearch 挂了），不改的话每个标签都要撞满 12 个请求 × timeout=10s，
    且 `verify_ssl=False` 那轮还会再来一遍。一批 32 个标签最坏接近 10 分钟，
    而结果必然是空 —— 纯白等。熔断后前 5 个标签之后直接返回空。

    同时守两件事：
      - 5xx 时不该跑 verify_ssl=False 那一轮（那是给证书问题准备的）
      - 200 但无匹配项算「成功」，不能触发熔断（没有匹配是正常结果）
    """
    import time
    from unittest import mock
    import requests
    import tageditor.translate.llm_pipeline as lp

    class R500:
        status_code = 500
        def json(self): return {}

    class R200Empty:
        status_code = 200
        def json(self): return {'data': []}

    def boom(*a, **k):
        raise requests.exceptions.RequestException('too many 500 error responses')

    lp.reset_bangumi_circuit()
    try:
        calls = {'n': 0}

        def counted(*a, **k):
            calls['n'] += 1
            return boom()

        with mock.patch.object(requests.Session, 'post', counted), \
             mock.patch.object(time, 'sleep', lambda *_: None):
            for i in range(lp._BANGUMI_FAIL_THRESHOLD):
                lp._fetch_bangumi_entity('t%d_(series)' % i, 3, 'tok')
            ok(lp._bangumi_circuit_open(), '达阈值后应跳闸')
            before = calls['n']
            for i in range(20):
                lp._fetch_bangumi_entity('after%d_(series)' % i, 3, 'tok')
            eq(calls['n'], before, '跳闸后不应再发包')

        # 5xx 时不该走 verify=False 那一轮
        lp.reset_bangumi_circuit()
        verifies = []

        def rec(self, url, **kw):
            verifies.append(kw.get('verify'))
            raise requests.exceptions.RequestException('boom')

        with mock.patch.object(requests.Session, 'post', rec), \
             mock.patch.object(time, 'sleep', lambda *_: None):
            lp._fetch_bangumi_entity('x_(series)', 3, 'tok')
        ok(False not in verifies or len(set(verifies)) == 1,
           '5xx 时混用了 verify=True/False：%s' % verifies)

        # 空结果不能触发熔断
        lp.reset_bangumi_circuit()
        with mock.patch.object(requests.Session, 'post',
                               lambda *a, **k: R200Empty()), \
             mock.patch.object(time, 'sleep', lambda *_: None):
            for i in range(10):
                lp._fetch_bangumi_entity('nomatch%d' % i, 3, 'tok')
        ok(not lp._bangumi_circuit_open(),
           '200 但无匹配项是正常结果，不该触发熔断')

        # 非 3/4 分类不该发请求
        lp.reset_bangumi_circuit()
        calls['n'] = 0
        with mock.patch.object(requests.Session, 'post', counted):
            for cat in (0, 1, 5, -1):
                lp._fetch_bangumi_entity('some_tag', cat, 'tok')
        eq(calls['n'], 0, 'category 0/1/5 不该发起 Bangumi 请求')
    finally:
        lp.reset_bangumi_circuit()   # 别把跳闸状态留给后续用例


# ═══════════════════════════════════════════════════════════════════════════
# 配置一致性：.env / .env.example / 代码读取 三者必须对得上
# ═══════════════════════════════════════════════════════════════════════════

# 通过变量名间接读取、正则扫不到的键（logging_setup 的 _level(name, ...)）
_INDIRECT_ENV_KEYS = {'LOG_LEVEL', 'LOG_FILE_LEVEL'}


def _env_keys(path):
    """读一个 .env 风格文件的键（含被注释掉的 #KEY= 形式）。

    被注释的也算：.env.example 里刻意用注释保留「可选/默认即可」的项。
    """
    out = set()
    p = ROOT / path
    if not p.exists():
        return out
    import re as _re
    for line in p.read_text(encoding='utf-8').splitlines():
        s = line.strip()
        if not s or '=' not in s:
            continue
        k = s.split('=', 1)[0].strip().lstrip('#').strip()
        if _re.fullmatch(r'[A-Z_][A-Z_0-9]*', k):
            out.add(k)
    return out


def _code_env_keys():
    """代码里真正会读的 env 键。"""
    import re as _re
    keys = set(_INDIRECT_ENV_KEYS)
    for p in all_source_files():
        src = p.read_text(encoding='utf-8')
        for m in _re.finditer(r"os\.environ\.get\(\s*'([A-Z_0-9]+)'", src):
            keys.add(m.group(1))
        for m in _re.finditer(r"os\.environ\[\s*'([A-Z_0-9]+)'\s*\]", src):
            keys.add(m.group(1))
    return keys


@case
def test_env_keys_are_documented_in_example():
    """`.env` 里**实际配置**的每一项都必须在 `.env.example` 里有记录。

    **只查这一个方向**，不要求两边键集合相等：`.env.example` 会用注释列出
    「有默认值、可选」的项（如 LOG_*），而 `.env` 不必把它们写出来 ——
    要求相等会把正常状态判成失败。

    守的是反方向漂移（真实踩过）：LLM_TEXT_THINKING / LLM_TEXT_TIMEOUT 曾
    只加进 `.env`、忘了同步示例，导致「照着示例配的人根本不知道有这两项」。
    """
    real, example = _env_keys('.env'), _env_keys('.env.example')
    if not real:
        return          # 没有 .env（未配置的环境）就跳过
    eq(sorted(real - example), [],
       '.env 已配置但 .env.example 未记录（照示例配的人会漏掉）')


@case
def test_no_dead_config_in_env_example():
    """.env.example 里的每个键都必须**真的被代码读取**。

    这是 CAPTION_USE_TAGS_AS_HINT / CAPTION_SAVE_AS 那对死配置的教训：
    它们写在示例里、README 里还列了表格，但代码从来不读 ——
    用户以为能关掉「描述参考已有标签」，实际关不掉。比没有这个配置更糟。
    """
    code = _code_env_keys()
    example = _env_keys('.env.example')
    dead = sorted(example - code)
    eq(dead, [], '.env.example 里存在「代码根本不读」的死配置')


@case
def test_all_code_env_keys_documented():
    """代码会读的每个 env 键都必须在**某处**有记录。

    判定为「.env.example 或 README 任一提及」。这是刻意的分工，不是放宽：
      - `.env.example` = 照着填就能跑（只列必填/常改的）
      - `README` 的配置说明 = 完整参考（连有默认值不必配的也列）
    README 的配置表还带默认值，比示例更适合查「这项默认是什么」。

    守的是「新加了一个 os.environ.get(...) 却哪都没写」——
    用户既不知道有这项可调，也不知道该配什么。
    """
    import re as _re
    code = _code_env_keys()
    readme = (ROOT / 'README.md').read_text(encoding='utf-8')
    missing = []
    for k in sorted(code):
        if ('`%s`' % k) in readme:
            continue
        if k in _env_keys('.env.example'):
            continue
        missing.append(k)
    eq(missing, [], '代码读取但 .env.example 与 README 都未记录的键')


@case
def test_readme_config_table_covers_env_example():
    """README 的配置说明要覆盖 .env.example 的每个键。

    不要求逐字一致，只要求「用户能在 README 里查到这一项」。
    """
    import re as _re
    example = _env_keys('.env.example')
    readme = (ROOT / 'README.md').read_text(encoding='utf-8')
    missing = []
    for k in sorted(example):
        # README 用 `KEY` 形式提及
        if ('`%s`' % k) not in readme:
            missing.append(k)
    eq(missing, [], 'README 未提及的配置项')


# ═══════════════════════════════════════════════════════════════════════════

@case
def test_alpha_brush_mode_is_exclusive_with_crop_mode():
    """Alpha 修补笔刷与裁剪模式必须互斥（共用同一张覆盖层 canvas）。

    两个模式都在 `#editor-canvas` 上画：不互斥时裁剪框会叠在笔画上、
    或者退出裁剪时 drawCropOverlay 的 clearRect 把未烘焙的笔画抹掉。
    互斥是双向的：进笔刷要退出裁剪（toggleBrushMode），进裁剪要退出笔刷
    （toggleCropMode）。只写一边的话从另一边进入时照样打架。
    """
    src = js_of('image_editor.html')
    body = _fn_body(src, 'toggleBrushMode')
    ok('if (cropMode) toggleCropMode()' in body,
       '进笔刷必须先退出裁剪模式（两者共用覆盖层 canvas）')
    cbody = _fn_body(src, 'toggleCropMode')
    ok('exitBrushMode()' in cbody,
       '进裁剪必须先退出笔刷模式（互斥是双向的，只写一边照样打架）')


@case
def test_brush_restore_has_a_source_snapshot():
    """「恢复」笔刷必须有进模式时的原图快照，且快照在涂抹之前存。

    没有快照的「恢复」没有数据来源，画了也是空的；快照若在首次擦除之后才存，
    涂回来的是已擦除的图（等于没恢复）。同时切图/重置必须清快照——
    留着旧图的快照会在新图上涂错像素。
    """
    src = js_of('image_editor.html')
    ebody = _fn_body(src, 'enterBrushMode')
    # 必须盯「存快照的动作」（drawImage 拷贝），不能只查变量名——
    # `rmbgSourceCanvas = null` 的清空赋值也包含变量名，查名字会被它骗过
    ok("getContext('2d').drawImage(img, 0, 0)" in ebody,
       '进笔刷模式必须把当前 img 拷进快照（drawImage），只声明变量没有数据来源')
    # 快照必须在 brushMode = true 之前存（时序：先有数据来源，再允许涂抹）
    ok(ebody.index('drawImage(img, 0, 0)') < ebody.index('brushMode = true'),
       '快照必须存于进入模式之前（涂抹后存就只能是已擦除的图）')
    nbody = _fn_body(src, 'doNavigate')
    ok('exitBrushMode()' in nbody, '切图必须退出笔刷模式（旧图快照/撤销栈会在新图上涂错像素）')


@case
def test_brush_strokes_are_baked_into_img():
    """每笔结束后必须把笔画烘焙进 img 元素。

    保存（saveImage）导出的是 img 元素——笔画只留在显示层 canvas 上的话，
    用户看着修好了、点保存存下的却是没修过的图（静默丢编辑）。
    烘焙函数必须先绑 onload 再设 src（缓存命中时同步加载会错过 onload，
    与 loadImageIntoEditor 注释里记的是同一个坑）。
    """
    src = js_of('image_editor.html')
    body = _fn_body(src, 'bakeBrushToImg')
    ok('toDataURL' in body, '烘焙必须产出新 dataURL 换掉 img.src')
    ok(body.index('img.onload') < body.index('img.src = dataUrl'),
       '必须先绑 onload 再设 src（否则缓存命中时错过回调，canvas 尺寸失同步）')
    # mouseup 里必须真的调了烘焙
    ups = re.findall(r"document\.addEventListener\('mouseup', function\(\) \{(.*?)\}\);", src, re.S)
    ok(any('bakeBrushToImg()' in u for u in ups),
       '涂抹结束（mouseup）必须调用烘焙，否则保存存下的是没修过的图')


# ═══════════════════════════════════════════════════════════════════════════
# 并发/数据层新约定（线程本地连接、imread_any、FTS 版本判定等——
# 这些约定曾在一次性评审报告中列出，报告删除后以 CLAUDE.md 为准）
# ═══════════════════════════════════════════════════════════════════════════

@case
def test_db_conn_is_thread_local_with_pragmas():
    """两处连接必须是**线程本地**且都调 apply_conn_pragmas。

    进程级单例（check_same_thread=False）在 Flask 每请求一线程下：
    线程 A 开着事务时线程 B 的 commit 会把 A 的半截事务一起提交、
    rollback 会回滚 A 已发出的写 —— 实测复现过最严重的一条：
    _apply_results 的 commit 变 no-op 却不报错，紧随其后的 _save_history
    已把这批记为「已处理」→ 翻译永久丢失且界面显示完成。
    """
    src = src_of('translation')
    ok('_local = _threading.local()' in src,
       'translation.py 的连接必须是线程本地（不得回到进程级单例）')
    ok("check_same_thread=False" not in src.split('def _get_tag_db_conn')[1].split('def ')[0],
       '_get_tag_db_conn 不得用 check_same_thread=False（每请求一线程下事务是连接级的）')
    ok('apply_conn_pragmas(conn' in src,
       'translation.py 的连接必须调 apply_conn_pragmas（PRAGMA 统一在那一层）')
    # build_tag_db.get_conn 也要 PRAGMA
    src2 = src_of('build_tag_db')
    ok('apply_conn_pragmas(conn' in src2 or 'PRAGMA busy_timeout' in src2,
       'build_tag_db.get_conn 必须设 busy_timeout/PRAGMA（跨连接串行化靠它）')


@case
def test_image_reading_uses_imread_any():
    """图像读取必须走 imread_any（cv2.imread 在 Windows 上对中文路径返回 None）。

    实测：中文文件名 cv2.imread → None、imread_any → 正常数组。直接用 cv2.imread
    的功能对中文文件**静默失效**（返回 None 后要么报「无法读取」要么跳过）。
    """
    from tageditor.core.image_io import imread_any   # 存在性：模块被删时这里先炸
    import ast as _ast
    for mod in ('image_editor', 'tagger'):
        src = src_of(mod)
        # 上 AST 判定**真实调用**：注释与 docstring 里正在解释「为什么不用 cv2.imread/
        # imwrite」的解释文字本身含这些名字（image_editor.py 实测 3 处注释 + 2 处
        # docstring），拿整段源码查字符串会把解释判成违规 —— CLAUDE.md 记过四次的坑。
        tree = _ast.parse(src)
        calls = [n for n in _ast.walk(tree) if isinstance(n, _ast.Call)
                 and isinstance(n.func, _ast.Attribute)
                 and n.func.attr in ('imread', 'imwrite')
                 and isinstance(n.func.value, _ast.Name) and n.func.value.id == 'cv2']
        ok(not calls, '%s.py 不得直接用 cv2.imread/imwrite（%d 处真实调用；'
           'imread 中文路径返回 None、imwrite 按扩展名推断写 .tmp 会失败，'
           '必须走 core.image_io.imread_any / _write_image_atomic）' % (mod, len(calls)))


@case
def test_apply_results_returns_written_set():
    """_apply_results 必须返回**真有写入**的标签集合，调用方不得回到按返回条目记 history。

    库里实测 677 条有中文名却无中文 wiki，其中 512 条被旧口径记进 history 后
    永久跳过——「调用了模型」不等于「写了库」（结果为空/名字对不上时没写入）。
    """
    src = src_of('llm_pipeline')
    m = re.search(r'def _apply_results\(.*?\n(?=def |\Z)', src, re.S)
    ok(m, '找不到 _apply_results')
    ok('return updated_names' in m.group(0) or 'return written' in m.group(0),
       '_apply_results 必须返回写入集合（调用方据此记 history，不写库的不记）')


@case
def test_fts_layout_version_gate_survives():
    """_ensure_fts_index 必须保留**版本比较逻辑**（ver < _FTS_LAYOUT_VERSION 触发重建）。

    只查常量名测不到「比较被弱化」：`_FTS_LAYOUT_VERSION = 2` 改成 1、或比较改成
    `!=` 都不会让常量消失，但索引布局升级会静默不再重建 —— 必须盯比较表达式本身。
    """
    src = src_of('build_tag_db')
    m = re.search(r'def _ensure_fts_index\(.*?(?=\ndef |\Z)', src, re.S)
    ok(m, '找不到 _ensure_fts_index')
    body = m.group(0)
    ok('ver < _FTS_LAYOUT_VERSION' in body,
       '必须保留 user_version 与布局版本的比较（删掉后索引布局升级静默失效）')
    ok("PRAGMA user_version" in body, '必须从 user_version 读布局版本')


@case
def test_init_destructive_guard():
    """init 分支必须保留守卫逻辑：`not args.yes` 拒绝 + backup_db 快照。

    只查 '--yes' 子串测不到「参数被改名」——docstring 与报错文案里也含这个词
    （实测 :833 与 :1716 都有）。必须盯**真实判定**（args.yes）与守卫分支。
    """
    src = src_of('build_tag_db')
    ok("p_init.add_argument('--yes'" in src, 'init 必须注册 --yes 参数')
    ok('if not args.yes:' in src, '守卫必须真的判 args.yes（删掉后破坏性操作无守卫）')
    ok('backup_db(db_path)' in src, '库非空时必须先 backup_db（重建前自动快照，留后悔路）')


@case
def test_trim_cooc_does_not_read_full_columns():
    """run_trim_cooc 不得回到整表全列 read_parquet（峰值 937MB 的来源）。

    62.9MB / 434 万行的真实文件：全列 object 指针数组把峰值顶到 937MB，
    且 OOM 落在「长时间爬取刚成功、准备落盘」这个最坏时刻。
    """
    src = src_of('cooc_pipeline')
    m = re.search(r'def run_trim_cooc\(.*?(?=\ndef |\Z)', src, re.S)
    ok(m, '找不到 run_trim_cooc')
    body = m.group(0)
    ok("columns=['source', 'target', 'frequency']" in body,
       'run_trim_cooc 必须只读需要的列（全列读峰值 937MB）')


@case
def test_rebuild_fts_uses_contentless_delete():
    """_rebuild_fts_index 在非空 contentless 表上必须走 'delete-all'，不得回退 DELETE FROM。

    contentless FTS5 表不支持普通 DELETE（会抛异常）——旧写法在「已有数据、
    只想重建索引」的库上直接炸，重建永远失败。
    """
    src = src_of('build_tag_db')
    ok("tags_fts) VALUES('delete-all')" in src,
       "重建必须走 contentless 的 'delete-all'（普通 DELETE FROM 对 contentless 表抛异常）")


def main():
    print('=' * 72)
    print('TagEditor_Web 关键不变量测试')
    print('=' * 72)
    passed = failed = 0
    for fn in _results:
        name = fn.__name__
        try:
            fn()
            passed += 1
            print('  [PASS] %s' % name)
        except Exception as e:
            failed += 1
            print('  [FAIL] %s' % name)
            for line in str(e).splitlines():
                print('         %s' % line)
            if VERBOSE:
                traceback.print_exc()
    print('-' * 72)
    print('%d 通过, %d 失败, 共 %d' % (passed, failed, len(_results)))
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
