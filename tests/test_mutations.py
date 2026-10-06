# -*- coding: utf-8 -*-
"""变异测试：往**真源码**里注入缺陷，看对应断言是否真的会失败。

    python tests/test_mutations.py        # 全部跑一遍，每条都还原

「测过」不等于「测得动」。断言写错方向时（查调用点而非实现、锚点落在注释上、
子串匹配太宽、`in` 只要求「出现过」而实际有两处），把它要守的那行代码删掉它照样
全绿 —— 而它唯一的作用就是在那行代码被改坏时报警。所以每条断言都拿一条**真实
退化过**的缺陷验一遍。

和 `test_invariants.py` 的分工：那个是跑给「改代码的人」看的（绿的=没退回），
这个是跑给「改断言的人」看的（绿的=断言真的测得动）。加断言时在这里加一条。

**本脚本会原地改写源文件**（断言读的是磁盘内容，改内存没用）。为此：
  1. 每次改写前把原文存成 `<文件名>.mutbak`
  2. `finally` 里还原
  3. 启动时若发现残留的 `.mutbak`（上次被强杀），**先还原再继续** —— 否则
     仓库会带着一份被注入缺陷的源码，而 `git status` 看起来只是「有点改动」
"""
import os
import shutil
import sys
from pathlib import Path

# `ROOT` 是**项目根**（本文件在 tests/ 下，故上跳一级），变异条目里的路径都相对它；
# 断言文件用 TESTS 定位。搞混这两个会让变异“注入”到一个不存在的路径上。
ROOT = Path(__file__).resolve().parent.parent
TESTS = ROOT / 'tests'
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True     # 见 run_one：别写 .pyc，免得下次读到不同步的缓存

# 排查用：断言失败时打出 traceback（平时只看「抓住/漏网」就够了）。
# 刻意做成常量而不是环境变量 —— `test_all_code_env_keys_documented` 会扫全仓
# `os.environ` 读取并要求写进 .env.example/README，为一个调试开关增加配置面不值。
VERBOSE = False


def _purge_project_modules():
    """把本项目已导入的模块从 `sys.modules` 里踢掉，逼下次 import 重新读盘。

    **不踢会得出「断言漏网」的假结论**：变异改的是磁盘上的 `.py`，而模块一旦进了
    `sys.modules` 就再也不看盘。实测栽过 —— `_SID_RE` 改成 `.*` 的变异体在第一次
    `run_one` 时被导入并缓存，还原后内存里仍是 `.*`，于是三条会话变异全部误报
    「漏网」（`注入后 失败 → 还原后 失败`，两个都是失败的诡异组合就是它的指纹）。

    连 `app` 一起踢是**必需**的，不是图省事：`prompt_workspace` 的蓝图在 `app`
    导入时就 `register_blueprint` 进 url_map 了，只踢子模块的话，测试客户端仍然
    路由到旧模块的视图函数上 —— 换了新模块也白换。
    """
    root = str(ROOT)
    for name, mod in list(sys.modules.items()):
        if name == '__main__':
            continue                  # 脚本自己就在项目根下，踢掉它没有意义
        f = getattr(mod, '__file__', None)
        if f and os.path.abspath(f).startswith(root):
            del sys.modules[name]


def drop_stale_pycache():
    """删掉本仓的 __pycache__。

    `run_one` 走 exec 绕开了 .pyc，但被测模块是 `import` 进来的：Python 按
    「mtime + 文件大小」判断 .pyc 是否有效，而变异**常常不改大小**
    （`max`→`min`、`.*` 之类），写在同一秒内就会被判为有效，于是跑的还是旧字节码。
    """
    for d in sorted(ROOT.rglob('__pycache__')):
        shutil.rmtree(d, ignore_errors=True)


def recover_from_crash():
    """还原上次被强杀时留下的 .mutbak。返回还原了哪些文件。"""
    restored = []
    for bak in sorted(ROOT.rglob('*.mutbak')):
        target = Path(str(bak)[:-len('.mutbak')])
        try:
            target.write_text(bak.read_text(encoding='utf-8'), encoding='utf-8')
            bak.unlink()
            restored.append(target.name)
        except OSError as e:
            print('!! 还原 %s 失败：%s' % (target, e))
    return restored


def run_one(test_name):
    """单独跑一个断言，返回 True=通过，False=失败（AssertionError 或其它异常都算）。

    **用 exec 直接执行源码，不走 import**：走 import 时 Python 会查 `__pycache__`，
    而判据是「mtime + 文件大小」。变异常常**不改大小**（`max`→`min` 正是同长度），
    写在同一秒内时旧 .pyc 会被判为有效 —— 于是变异根本没生效，全绿被误读成
    「断言漏网」。实测栽过一次，白查半天。
    """
    _purge_project_modules()
    src = (TESTS / 'test_invariants.py').read_text(encoding='utf-8')
    ns = {'__name__': 'test_invariants_mut', '__file__': str(TESTS / 'test_invariants.py')}
    exec(compile(src, 'tests/test_invariants.py', 'exec'), ns)
    fn = ns.get(test_name)
    if fn is None:
        raise SystemExit('没有这个断言: %s' % test_name)
    try:
        fn()
        return True
    except AssertionError:
        if VERBOSE:
            import traceback
            traceback.print_exc()
        return False
    except Exception as e:
        # 语法级改动可能让别的断言在导入期炸；这里只关心「有没有被抓到」
        print('       (异常: %s: %s)' % (type(e).__name__, e))
        return False


MUTATIONS = [
    # (说明, 文件, 原文, 改成, 应该失败的断言)
    ('删掉删除会话前的「先关列表弹窗」', 'templates/prompt_tool.html',
     'closeSessionsModal();\n            showConfirm({', 'showConfirm({',
     'test_delete_session_closes_list_modal_first'),

    ('把 _ptFinalDirty 复位挪到 onCaptionInput() 之后', 'templates/prompt_tool.html',
     '            _ptFinalDirty = false;\n            _ptResult = data;',
     '            _ptResult = data;',
     'test_rerun_does_not_pop_the_stale_confirm_dialog'),

    ('把 _ptFinalDirty 复位真的挪到 onCaptionInput() 之后（先删原处再插）',
     'templates/prompt_tool.html',
     [('            _ptFinalDirty = false;\n            _ptResult = data;',
       '            _ptResult = data;'),
      ('            renderDiff();\n            onCaptionInput();',
       '            renderDiff();\n            onCaptionInput();\n            _ptFinalDirty = false;')],
     None,
     'test_rerun_does_not_pop_the_stale_confirm_dialog'),

    ('clearResultPanel 不清检索计划面板', 'templates/prompt_tool.html',
     "            document.getElementById('pt-cand-count').textContent = '';\n"
     "            document.getElementById('pt-candidates-panel').innerHTML = '';\n",
     '',
     'test_clear_result_panel_also_clears_the_plan_panel'),

    ('loadSession 丢掉序列号守卫', 'templates/prompt_tool.html',
     "                    if (seq !== _sessionSeq) return;      // 已有更新的加载发起，丢弃本次\n",
     '',
     'test_session_async_responses_are_guarded'),

    ('loadSession 失败时不回调 onFail（死 sid 会卡死在空会话）', 'templates/prompt_tool.html',
     "                        showNotification(resp.error, 'error');\n"
     "                        if (onFail) onFail();\n",
     "                        showNotification(resp.error, 'error');\n",
     'test_dead_sid_in_url_falls_back_to_a_new_session'),

    # 整段换回**最朴素的**状态机（不认正则、不按行复位）—— 这是这个工具真实退化过
    # 的样子，不是假想的。换法要整段替换，插一小段「朴素版」却在后面接着走原逻辑
    # 是无效变异（实测栽过：变异体自己仍然认正则，于是「全绿」被我误当成漏网）。
    ('strip_js_comments 退回朴素状态机', 'tests/test_invariants.py',
     "        if c == '/' and _prev_allows_regex(prev, word):\n"
     "            j = _skip_js_regex(src, i)\n"
     "            out.append(src[i:j]); prev, word = '/', ''; i = j; continue\n",
     "",
     'test_js_comment_stripper_survives_regex_literals'),

    ('strip_js_comments 去掉按行复位（正则仍认）', 'tests/test_invariants.py',
     "            elif c == '\\n' and quote != '`':   # 单双引号字符串不能跨行 → 状态机复位\n"
     "                quote = None\n",
     "",
     'test_js_comment_stripper_bounds_a_misjudgement_to_one_line'),

    # js_of 用**页面级**断言来验：剥注释的两个断言直接调 strip_js_comments，
    # 拿它们去测 js_of 是测不到的（变异体全绿不代表断言弱，是指错了目标）。
    ('js_of 取最短的 script 块（拿到 tailwind.config）', 'tests/test_invariants.py',
     "    return strip_js_comments(max(blocks, key=len))",
     "    return strip_js_comments(min(blocks, key=len))",
     'test_clear_result_panel_also_clears_the_plan_panel'),

    # —— 以下三条针对会话的 e2e。它们守的是**不可逆**的 rmtree 与幽灵会话 ——
    ('sid 白名单退化成不过滤（.* ）', 'tageditor/translate/prompt_workspace.py',
     "_SID_RE = re.compile(r'^\\d{8}-\\d{6}-[0-9a-f]{6}$')",
     "_SID_RE = re.compile(r'.*')",
     'test_session_lifecycle_end_to_end'),

    ('上传时先建目录再查会话存在（幽灵会话复活）', 'tageditor/translate/prompt_workspace.py',
     "    d = session_dir(sid)\n"
     "    if d is None:\n"
     "        return jsonify({'error': '会话不存在'}), 404\n"
     "    d = os.path.join(d, 'images')\n",
     "    d = session_dir(sid, must_exist=False)\n"
     "    if d is None:\n"
     "        return jsonify({'error': '会话不存在'}), 404\n"
     "    d = os.path.join(d, 'images')\n",
     'test_session_lifecycle_end_to_end'),

    ('清空参考图时连会话目录一起删', 'tageditor/translate/prompt_workspace.py',
     "    d = images_dir(sid)\n"
     "    if d is None:\n"
     "        return jsonify({'error': '会话不存在'}), 404\n"
     "    removed, failed = 0, []",
     "    d = images_dir(sid)\n"
     "    if d is None:\n"
     "        return jsonify({'error': '会话不存在'}), 404\n"
     "    shutil.rmtree(os.path.dirname(d), ignore_errors=True)\n"
     "    removed, failed = 0, []",
     'test_session_lifecycle_end_to_end'),

    # —— 以下三条针对「参考图勾选提交」：编号口径与「工作区不限量」 ——

    # 按**勾选先后序**编号（而不是工作区自然序）。这是最隐蔽的一种错：功能看着完全
    # 正常，只是「图1」的含义会随用户重勾的顺序变化，而「图1」被写在了会持久化的
    # 优化要求里 —— 没有异常，没有日志，只有人物与动作张冠李戴。
    ('resolve_selection 按勾选先后序而不是工作区序', 'tageditor/translate/prompt_workspace.py',
     "    return [n for n in names if n in wanted][:MAX_SUBMIT_IMAGES]",
     "    return [n for n in stored if n in set(names)][:MAX_SUBMIT_IMAGES]",
     'test_selection_resolves_in_workspace_order'),

    # submit_index 用**工作区位置**而不是提交序号：界面上的「图3」与模型收到的
    # [参考图2] 就会对不上，同样是静默错位
    ('image_rows 的 submit_index 用工作区位置', 'tageditor/translate/prompt_workspace.py',
     "    return [{'name': n, 'selected': n in rank, 'submit_index': rank.get(n, 0)}\n"
     "            for n in all_names]",
     "    return [{'name': n, 'selected': n in rank, 'submit_index': (i + 1) if n in rank else 0}\n"
     "            for i, n in enumerate(all_names)]",
     'test_session_lifecycle_end_to_end'),

    # 上传处把数量上限加回来：用户攒到第 6 张就被无声拒绝，而界面写着「数量不限」
    ('上传时按单次上限拒图（工作区又变成有上限）', 'tageditor/translate/prompt_workspace.py',
     "        target = os.path.join(d, filename)\n"
     "        # 重名不覆盖",
     "        if len(existing) + len(added) >= MAX_SUBMIT_IMAGES:\n"
     "            rejected.append(f'{filename}（已达上限 {MAX_SUBMIT_IMAGES} 张）')\n"
     "            continue\n"
     "        target = os.path.join(d, filename)\n"
     "        # 重名不覆盖",
     'test_reference_image_cap_limits_submission_not_workspace'),

    # resolve_selection 的求交退回「只信 stored」：引用已删文件的项会活下来，
    # 被拿去做 resolve_image / 编码，而它们已经不存在了
    ('resolve_selection 不丢弃引用已删文件的项', 'tageditor/translate/prompt_workspace.py',
     "    return [n for n in names if n in wanted][:MAX_SUBMIT_IMAGES]",
     "    return [n for n in stored if isinstance(n, str)][:MAX_SUBMIT_IMAGES]",
     'test_selection_resolves_in_workspace_order'),

    # diff 又渲染出 keep 行：列表被「保留」淹没，真正的增删改看不出
    ('renderDiff 把保留行也渲染出来', 'templates/prompt_tool.html',
     "if (d.op === 'keep') continue;",
     "if (false) continue;",
     'test_diff_renders_only_changed_rows'),

    # complete 的 count 退回用 _lastProgress.current：还是「5 / 6」，与 100% 并存
    ('complete 的 count 沿用 current（仍是 5 / 6）', 'templates/prompt_tool.html',
     "count.textContent = _tn + ' / ' + _tn;",
     "count.textContent = _lastProgress.current + ' / ' + _tn;",
     'test_progress_modal_settles_its_numbers_on_complete'),

    # 自动关窗被删掉：跑完弹窗一直杵着，用户以为还在跑
    ('complete 后不再自动关窗', 'templates/prompt_tool.html',
     "if (seq === _progressSeq) closeProgressModal();",
     ";",
     'test_progress_modal_settles_its_numbers_on_complete'),

    # 图标名换回 FA6 的写法：4.7 里没有 → <i> 渲染成空白，按钮成了一个看得见
    # 却认不出的空方块。这正是本次修的 bug（用户因此以为「没有会话记录功能」）
    ('图标名换成 FA6 写法（空白按钮）', 'templates/prompt_tool.html',
     '<i class="fa fa-times"></i>', '<i class="fa fa-xmark"></i>',
     'test_font_awesome_icons_exist_in_the_loaded_version'),

    # 标题退回取「优化要求」：同一份提示词迭代三轮就出现三条互不相干的标题，
    # 用户认不出是同一个角色的图 —— 这正是本次改掉的旧口径
    ('会话标题退回取优化要求', 'tageditor/translate/prompt_workspace.py',
     "    head = (data.get('prompt') or '').strip().split('\\n', 1)[0].strip()",
     "    head = (data.get('request') or '').strip().replace('\\n', ' ')",
     'test_session_title_comes_from_the_prompt'),

    # title 又被落盘：改提示词重跑后磁盘上还是上一轮的，与现算的标题分叉
    ('create_session 又把 title 落盘', 'tageditor/translate/prompt_workspace.py',
     "    write_session(sid, {'created_at': time.strftime('%Y-%m-%d %H:%M:%S')})",
     "    write_session(sid, {'title': 'x', 'created_at': time.strftime('%Y-%m-%d %H:%M:%S')})",
     'test_session_title_comes_from_the_prompt'),

    # 缩略图退回 object-cover：竖图（实测全是 0.31~0.78 的立绘）被裁成中间一条
    ('缩略图退回 object-cover（裁掉竖图）', 'templates/prompt_tool.html',
     'aspect-[2/3] object-contain', 'aspect-[2/3] object-cover',
     'test_reference_thumbnails_are_not_cropped'),

    # 导航项退回旧文案：同一个 `/` 在两个页面叫两个名字，用户会以为去的是两个功能
    ('导航项退回旧文案「标签查询」', 'templates/prompt_tool.html',
     '<i class="fa fa-book mr-1.5"></i> Danbooru 标签查询',
     '<i class="fa fa-book mr-1.5"></i> 标签查询',
     'test_nav_link_to_danbooru_page_has_one_name'),

    # —— 以下四条针对「Alpha 修补笔刷」——

    # 进笔刷不退出裁剪：两个模式都在覆盖层 canvas 上画，裁剪框会叠在笔画上、
    # 或 drawCropOverlay 的 clearRect 把未烘焙的笔画抹掉
    ('进笔刷不退出裁剪模式（共用 canvas 打架）', 'templates/image_editor.html',
     "                if (cropMode) toggleCropMode();\n"
     "                if (_sam2Mode) exitSam2Mode();\n"
     "                if (!enterBrushMode()) return;",
     "                if (_sam2Mode) exitSam2Mode();\n"
     "                if (!enterBrushMode()) return;",
     'test_alpha_brush_mode_is_exclusive_with_crop_mode'),

    # 「恢复」没有数据来源：画了也是空的。变异把存快照的整块换成清空赋值——
    # 变量名仍在，所以断言必须盯 drawImage 动作而不是变量名（实测第一次只查名字，漏网）
    ('enterBrushMode 不存原图快照（恢复笔刷没有数据来源）', 'templates/image_editor.html',
     """            try {
                rmbgSourceCanvas = document.createElement('canvas');
                rmbgSourceCanvas.width = img.naturalWidth;
                rmbgSourceCanvas.height = img.naturalHeight;
                rmbgSourceCanvas.getContext('2d').drawImage(img, 0, 0);
            } catch (e) {
                rmbgSourceCanvas = null;
                showNotification('无法读取当前图片像素，Alpha 修补不可用', 'error');
                return false;
            }""",
     '            rmbgSourceCanvas = null;',
     'test_brush_restore_has_a_source_snapshot'),

    # 烘焙先设 src 再绑 onload：缓存命中时同步加载会错过回调（与 loadImageIntoEditor
    # 注释里记的是同一个坑），fitImageToEditor 不执行，canvas 尺寸失同步
    ('烘焙先设 src 后绑 onload（缓存命中错过回调）', 'templates/image_editor.html',
     """            img.onload = function() {
                fitImageToEditor();
                updateButtonStates();
            };
            img.src = dataUrl;""",
     """            img.src = dataUrl;
            img.onload = function() {
                fitImageToEditor();
                updateButtonStates();
            };""",
     'test_brush_strokes_are_baked_into_img'),

    # 切图不清笔刷：旧图的快照与撤销栈会在新图上涂错像素
    ('doNavigate 切图不清笔刷状态', 'templates/image_editor.html',
     "            // 切图时退出笔刷/描点模式：快照、撤销栈、锚点都是上一张图的，\n"
     "            // 留着会在新图上涂错像素、或把上一张的 mask 叠到新图上（静默错位）\n"
     "            if (brushMode) exitBrushMode();\n"
     "            if (_sam2Mode) exitSam2Mode();\n"
     "            updateNavUI();",
     '            updateNavUI();',
     'test_brush_restore_has_a_source_snapshot'),

    # —— 以下七条针对并发/数据层新约定（线程本地连接、imread_any、FTS 版本判定等）——

    # 退回进程级单例（check_same_thread=False）：Flask 每请求一线程下，线程 A 开着
    # 事务时线程 B 的 commit 会把 A 的半截事务一起提交 —— 实测复现过「翻译永久丢失」
    ('DB 连接退回进程级单例', 'tageditor/translate/translation.py',
     '_local = _threading.local()',
     '_local = None',
     'test_db_conn_is_thread_local_with_pragmas'),

    # 换回 cv2.imread：中文路径直接返回 None，对中文文件名静默失效（实测复现）
    ('图像读取换回 cv2.imread（中文路径返回 None）', 'tageditor/image/image_editor.py',
     'img = imread_any(fpath, cv2.IMREAD_UNCHANGED)',
     'img = cv2.imread(fpath, cv2.IMREAD_UNCHANGED)',
     'test_image_reading_uses_imread_any'),

    # 删掉版本比较条件：FTS 索引布局升级永远不会落到已存在的库上。
    # 注意变异方向：只改常量值（2→1）是**合法的布局回退**，不是退化——断言守的是
    # 「比较存在」，所以变异必须删比较条件本身（实测改常量值会漏网，那是断言方向错）
    ('FTS 版本比较被删（索引升级静默失效）', 'tageditor/db/build_tag_db.py',
     "if 'cn_name' not in fts_cols or ver < _FTS_LAYOUT_VERSION:",
     "if 'cn_name' not in fts_cols:",
     'test_fts_layout_version_gate_survives'),

    # init 守卫分支被删：库非空时不弹拒绝，一把清空丢掉全部翻译/wiki。
    # 注意变异锚点：**不能**改 --yes 参数名——那属于行为变化，`if not args.yes:` 这行
    # 源码不变，文本级断言天然抓不到（实测漏网）。文本可测的退化是删掉判定本身。
    ('init 守卫分支被删（库非空不再拒绝）', 'tageditor/db/build_tag_db.py',
     "            if not args.yes:",
     "            if False:",
     'test_init_destructive_guard'),

    # 回到整表全列读：峰值 937MB，OOM 落在「长时间爬取刚成功、准备落盘」这个最坏时刻
    ('trim_cooc 回到整表全列读（峰值 937MB）', 'tageditor/db/cooc_pipeline.py',
     "columns=['source', 'target', 'frequency']",
     'columns=None',
     'test_trim_cooc_does_not_read_full_columns'),

    # 重建退回 DELETE FROM：contentless FTS5 表不支持普通 DELETE，直接抛异常
    ('FTS 重建退回 DELETE FROM（contentless 表抛异常）', 'tageditor/db/build_tag_db.py',
     "INSERT INTO tags_fts(tags_fts) VALUES('delete-all')",
     'DELETE FROM tags_fts',
     'test_rebuild_fts_uses_contentless_delete'),

    # —— 以下两条针对「共享 JS 抽取」——

    # 页面又内联定义共享函数：两份实现并存，改 common.js 不生效、改页面又丢共享。
    # 抽取的全部意义就是「修复只落一处」，副本让它静默失效。
    ('页面又内联定义 escapeHtml（副本与 common.js 并存）', 'templates/tag_editor.html',
     "        // escapeHtml 在 /static/js/tageditor-common.js（四页共享）：先 String(...) 归一化\n"
     "        // 再替换，& 必须第一个替换（否则后续插入的实体会被二次转义）。",
     "        function escapeHtml(str) { return String(str == null ? '' : str).replace(/&/g,'&amp;'); }",
     'test_shared_js_is_wired_and_not_copied_back'),

    # 引入标签被删：全部行内 onclick 抛 ReferenceError，页面功能性崩溃。
    # 断言必须盯 <script src> 标签——注释里解释「函数在 common.js」的文字也含路径，
    # 查「路径出现过」会被自己的注释骗过（实测漏网）
    ('common.js 引入标签被删（onclick 全部 ReferenceError）', 'templates/prompt_tool.html',
     '    <script src="/static/js/tageditor-common.js"></script>',
     '    <!-- script tag removed -->',
     'test_shared_js_is_wired_and_not_copied_back'),

    # —— 以下五条针对 SAM2 描点门控 ——

    # 删掉 dilate：程序不报错、结果更「干净」，只是人物边缘少了一圈 ——
    # 与「模型变好了」难以区分。这类改动必须被断言挡住。
    ('门控删掉 dilate（削掉边界外发丝）', 'tageditor/image/sam2_utils.py',
     "    if dilate_px > 0:\n"
     "        k = int(dilate_px) * 2 + 1\n"
     "        g = cv2.dilate(g, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))\n",
     "",
     'test_gate_dilates_before_feathering'),

    # bool → uint8 不乘 255：gate 被压成约 0.004，门控后 alpha 全灭，
    # 看起来像「模型坏了」而日志里什么都没有（实测踩到过）
    ('bool mask 不乘 255（门控后 alpha 全灭）', 'tageditor/image/sam2_utils.py',
     "    g = (g.astype(np.uint8) * 255) if g.dtype == bool else g.astype(np.uint8)",
     "    g = g.astype(np.uint8)",
     'test_gate_bool_mask_is_scaled_to_255'),

    # embedding 缓存键去掉 mtime：用户「抠图 → 保存 → 再抠」拿到上一版图片的
    # embedding，mask 与画面错位且无任何报错
    ('SAM2 embedding 缓存键去掉 mtime', 'tageditor/image/image_editor.py',
     "        return (os.path.abspath(fpath), st.st_mtime_ns, st.st_size)",
     "        return os.path.abspath(fpath)",
     'test_sam2_embedding_cache_key_includes_mtime'),

    # 去掉预测的序号守卫：连点/切图时过期响应覆盖当前画面（静默错位，
    # 只在有网络延迟时复现 —— 本机快得察觉不到，正是最该守的那种）
    ('SAM2 预测去掉序号守卫（过期响应覆盖画面）', 'templates/image_editor.html',
     "                if (seq !== _sam2Seq || currentIndex !== idx) return;",
     "                if (false) return;",
     'test_sam2_predict_has_sequence_guard'),

    # 进描点不退出裁剪：两者共用 #editor-canvas，mask 叠加层会被
    # drawCropOverlay 的 clearRect 抹掉，或裁剪框叠在 mask 上
    ('进描点模式不退出裁剪（共用 canvas 打架）', 'templates/image_editor.html',
     "            if (cropMode) toggleCropMode();\n"
     "            if (brushMode) exitBrushMode();\n"
     "            _sam2Mode = true;",
     "            if (brushMode) exitBrushMode();\n"
     "            _sam2Mode = true;",
     'test_sam2_mode_is_exclusive_with_crop_and_brush'),
]


def main():
    drop_stale_pycache()
    for name in recover_from_crash():
        print('!! 发现上次崩溃留下的备份，已还原：%s' % name)
    print('=' * 78)
    survived = []
    for desc, fname, old, new, tname in MUTATIONS:
        p = ROOT / fname
        orig = p.read_text(encoding='utf-8')
        # old/new 可以是**成对的列表**，按序替换 —— 「把 A 挪到 B 后面」这类变异
        # 必须先删原处再插新处；只插不删等于复制一份，而断言多半查「第一次出现」，
        # 于是复制出来的第二份根本影响不到它，变异体全绿被误读成「断言漏网」。
        # 实测把「挪到 onCaptionInput 之后」写成只插入，得到的正是这个假漏网。
        pairs = old if isinstance(old, list) else [(old, new)]
        if any(o not in orig for o, _ in pairs):
            print('[跳过] %s\n      锚点没匹配上（源码变了？）' % desc)
            survived.append(desc + '（锚点失效）')
            continue
        mutated = orig
        for o, n in pairs:
            mutated = mutated.replace(o, n, 1)
        bak = p.with_suffix(p.suffix + '.mutbak')
        bak.write_text(orig, encoding='utf-8')
        try:
            p.write_text(mutated, encoding='utf-8')
            before = run_one(tname)
        finally:
            p.write_text(orig, encoding='utf-8')
            bak.unlink(missing_ok=True)
        after = run_one(tname)
        caught = (not before) and after
        if not caught:
            survived.append(desc)
        print('[%s] %s' % ('抓住' if caught else '漏网', desc))
        print('      断言 %s：注入后 %s → 还原后 %s'
              % (tname, '通过' if before else '失败', '通过' if after else '失败'))
    print('=' * 78)
    if survived:
        print('%d/%d 未被抓住：' % (len(survived), len(MUTATIONS)))
        for s in survived:
            print('  - %s' % s)
    else:
        print('全部 %d 条变异均被对应用例抓住' % len(MUTATIONS))
    return 1 if survived else 0


if __name__ == '__main__':
    sys.exit(main())
