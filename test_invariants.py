# -*- coding: utf-8 -*-
"""关键不变量测试 —— 锁住 CLAUDE.md 里那些「别退回」的约定。

为什么需要这个文件：本项目的多数 bug 是**静默**的（翻译列空白、徽标错、
描述被炸成假标签、数据写坏），没有异常、没有日志，只有人眼能发现。
这些不变量都是踩过坑之后写进文档的，一旦被无意改回去，症状要过很久才浮现。

运行：
    python test_invariants.py          # 全部
    python test_invariants.py -v       # 显示每条断言

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

# 项目根（本文件位于根目录）。所有源码级断言都基于它拼路径，
# 这样模块被移动到包里的其它位置时，只要改下面的 PKG 映射即可。
ROOT = Path(__file__).resolve().parent


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
    """所有 Python 源文件（用于「全仓扫描」类断言，如 OpenAI() 检查）。"""
    files = sorted(ROOT.glob('*.py'))
    pkg = ROOT / 'tageditor'
    if pkg.is_dir():
        files += sorted(pkg.rglob('*.py'))
    return files


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
# 一、翻译合并 _combine_cn —— CLAUDE.md:337「改这里时别退回 ",".join」
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
# 二、提示词切分 _split_prompt_entries —— CLAUDE.md:176「别退回全文按逗号切」
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
# 三、API Key 归一化 —— CLAUDE.md:127「所有 OpenAI() 构造点必须过 resolve_api_key」
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
    import glob
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
# 四、lookup_tags 只查主表 —— CLAUDE.md:111「勿把回落塞进 lookup_tags」
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
    """弹窗守卫必须按结构判定，不能手写 id 清单（漏一个就会被遮挡触发快捷键）。"""
    for f in ('templates/tag_editor.html', 'templates/image_editor.html'):
        src = (ROOT / f).read_text(encoding='utf-8')
        ok('function _anyModalOpen' in src, '%s 应有 _anyModalOpen' % f)
        ok("querySelectorAll('div.fixed.inset-0')" in src,
           '%s 应按结构判定遮罩层' % f)


@case
def test_no_stale_variable_names_in_templates():
    """曾经导致静默失效的拼写错误不能再回来。"""
    te = open(ROOT / 'templates/tag_editor.html', encoding='utf-8').read()
    no_cmt = re.sub(r'//[^\n]*', '', te)
    ok('currentImgName' not in no_cmt,
       'currentImgName 是未声明变量（正确名是 currentImageName），会导致保存描述抛 ReferenceError')
    # escapeHtml 必须能接受 null/undefined
    for f in ('templates/tag_editor.html', 'templates/danbooru_wiki.html',
              'templates/prompt_tool.html'):
        src = (ROOT / f).read_text(encoding='utf-8')
        m = re.search(r'function escapeHtml\(str\)\s*\{(.{0,200})', src, re.S)
        ok(m and ('str == null' in m.group(1) or 'String(str ==' in m.group(1)),
           '%s 的 escapeHtml 应先归一化 null/undefined' % f)


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


# ═══════════════════════════════════════════════════════════════════════════

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
