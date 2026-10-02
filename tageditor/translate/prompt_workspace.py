# -*- coding: utf-8 -*-
"""提示词优化器的**会话存储**（独立于标签编辑的 uploads/）。

一个会话是一个**自包含的整体**：它引用的参考图 + 输入提示词 + 优化要求 +
产出结果，全部装在自己的目录里。因此：

    新开会话 = 一块空工作台（新目录）
    加载会话 = 图、输入、结果一起还原
    删除会话 = 连同它目录里的图一起删 —— 图属于这个会话，不是共享素材

目录结构：
    prompt_workspace/sessions/<sid>/
        session.json     输入与产出（提示词/要求/结果/diff/时间戳）
        selection.json   工作台当前**勾选**了哪几张（送模型的清单）—— 可选文件
        images/          该会话的参考图，顺序即提示词里的「图1 / 图2」

与 uploads/ 的区别（**本模块不碰 uploads/**）：
  1. 目录独立，可用 PROMPT_WORKSPACE_DIR 覆盖，不进版本控制
  2. 不产生任何伴生标签文件（.txt / .nl.txt 一概不写）—— 本页与标签编辑无关
  3. 图片顺序即语义，所以排序用自然序（ref2 排在 ref10 前）

`session.json` 只有**两个写入点**，别再加第三个：
  1. `create_session()` —— 建会话时写 `created_at`
  2. `prompt_tool._save_result_to_session()` —— 一轮跑完写 `prompt` / `request` / `result`
（曾有一个 `POST /prompt_sessions/<sid>/save` 路由，全仓无调用方，已删 —— 它会让
「谁在写会话」有两个答案。）

**会话标题不落盘**，由 `session_title()` 从 `prompt` 读时现算：存一份就会与
prompt 脱节（改了提示词重跑，落盘的还是上一轮的标题），列表显示算出来的、
磁盘存着旧的，又是一个「两个真相来源」。

**勾选态刻意不写进 `session.json`，而是单独一个 `selection.json`。** 两者语义不同：
`session.json` 是「这一轮跑的输入与产出」（管线的账单），勾选是「工作台当前的挑选」
（UI 的工作状态）—— 写入者不同、生命周期不同、丢了的后果也不同（丢结果要重跑，
丢勾选重勾即可）。而且勾选框可以在一轮 LLM 跑着的 10~60 秒里任意时刻落盘，与
`_save_result_to_session` 的 read-modify-write 挤同一个文件没有好处。
`selection.json` 是**可选文件**：没有它就按「全选（截到上限）」处理，所以新建会话与
本次改动之前的老会话都不需要迁移，也不必提前造一个空文件出来。

安全：sid 由服务端生成，格式固定（`YYYYMMDD-HHMMSS-xxxxxx`），用**白名单正则**
校验后再拼路径。比黑名单过滤强：任何不在该格式内的输入（含 `..`、绝对路径、
URL 编码）在第一步就被拒，不依赖后续的路径检查兜底。
"""
import json
import os
import re
import shutil
import time

from flask import Blueprint, jsonify, request, send_from_directory

from tageditor.core.config import allowed_file, is_within_directory, write_text_atomic
import logging


log = logging.getLogger(__name__)

prompt_workspace_bp = Blueprint('prompt_workspace', __name__)

# **单次提交**给模型的参考图上限。注意它**不是**工作区的容量上限 ——
# 工作区（会话的 images/）放多少张都行，用户勾选其中最多 5 张送模型。
#
# 早先这里叫 MAX_IMAGES 且兼管上传，是「一个数字管两件事」。现在拆开：素材随便攒，
# 花钱的那一步（每张压缩后约 1~2k token，1536px 上限）才设限。5 张 ≈ 5~10k token，
# 叠加工具结果、标签注入与输出额度，本地常见 32k 上下文端点仍有余量。
# 调大它要同步考虑 `_PROMPT_IMAGE_MAX_SIDE` 与 `PROMPT_TOOL_MAX_TOKENS` ——
# 否则表现是请求报错或输出被截断，而不是一条「图太多」的明确提示。
MAX_SUBMIT_IMAGES = 5

# 会话 ID 白名单：服务端生成的长这样，校验时也只认这个形状。
_SID_RE = re.compile(r'^\d{8}-\d{6}-[0-9a-f]{6}$')


# 会话根目录的**进程级**缓存。
#
# 为什么不用 `current_app.config`：本模块的函数会在 **SSE 生成器内部**被调用
# （`_save_result_to_session` 在 `complete` 事件之前落盘），而生成器恢复执行时
# Flask 的请求上下文已经拆掉了 —— `current_app` 直接抛
# `RuntimeError: Working outside of application context`，结果是「模型跑完了、
# 结果却没存下来」。实测踩到过。
#
# 改用模块级变量：`app.py` 启动时调一次 `configure()` 定下来。
# 测试里想换目录也调它（比塞 config key 更直接，且不依赖应用上下文）。
_workspace_base = None


def configure(path):
    """设定会话存储的根目录（进程级）。app.py 启动时调用；测试用它指向临时目录。"""
    global _workspace_base
    _workspace_base = os.path.abspath(path)
    return _workspace_base


def sessions_root() -> str:
    """会话根目录（绝对路径），不存在则创建。"""
    base = _workspace_base or os.path.abspath('prompt_workspace')
    d = os.path.join(base, 'sessions')
    os.makedirs(d, exist_ok=True)
    return d


def _new_sid() -> str:
    """生成会话 ID：时间戳 + 随机后缀。

    带时间戳是为了默认排序即「新→旧」，且出问题时从目录名就能看出是什么时候的；
    随机后缀防同一秒内连续新建撞名（只靠时间戳在快速点击「新开会话」时会重名，
    而 shutil.rmtree 一旦作用到别人的目录就是毁灭性的）。
    """
    return '%s-%s' % (time.strftime('%Y%m%d-%H%M%S'), os.urandom(3).hex())


def session_dir(sid: str, must_exist: bool = True):
    """把 sid 解析为会话目录绝对路径；不合法/不存在时返回 None。

    三重校验：白名单正则 → 拼出的路径必须仍在 sessions 根内 → 存在性。
    第二重看似冗余（正则已排除 `..` 与分隔符），但保留它是因为**根目录本身**
    可能被配置成符号链接，逐段比较是唯一与配置无关的保证。
    """
    if not isinstance(sid, str) or not _SID_RE.match(sid):
        return None
    root = sessions_root()
    path = os.path.abspath(os.path.join(root, sid))
    if not is_within_directory(path, root):
        return None
    if must_exist and not os.path.isdir(path):
        return None
    return path


def images_dir(sid: str):
    """会话的图片目录；会话不存在（或 images/ 缺失）时返回 None。

    **刻意没有 `create` 参数**。早先有一个，用来「顺手把目录建出来」，唯一调用方
    是上传接口 —— 结果往一个**格式合法但不存在**的 sid 传图会凭空造出一个没有
    `session.json` 的幽灵会话；又因为它目录 mtime 最新，列表接口会把它当作
    `current`，用户刷新后直接被带进这个空壳里（实测复现过）。

    「上传到一个不存在的会话」正确语义是 404，不是替它开一个。建目录是
    `create_session` 自己的事（它显式 makedirs），不靠这里代劳。
    """
    d = session_dir(sid)
    if d is None:
        return None
    p = os.path.join(d, 'images')
    if not os.path.isdir(p):
        return None
    return p


def list_images(sid: str) -> list:
    """会话内的参考图文件名，自然序（= 提示词里的「图1/图2」编号）。"""
    d = images_dir(sid)
    if d is None:
        return []
    files = [f for f in os.listdir(d)
             if allowed_file(f, 'image') and os.path.isfile(os.path.join(d, f))]

    def natural_key(s):
        return [int(c) if c.isdigit() else c.lower() for c in re.split(r'(\d+)', s)]
    files.sort(key=natural_key)
    return files


def resolve_selection(all_names: list, stored) -> list:
    """把存储的勾选态解析为**最终送模型的清单**（纯函数，编号口径的唯一出处）。

    三条规则，每一条都对应一个实测过的错法：

    1. **顺序 = 工作区的自然序，不是勾选的先后序。** 用户是按缩略图的排列读「图1/图2」
       的，若按勾选顺序编号，同样的勾选在刷新/重勾后会得到不同编号 —— 而用户把
       「用图1 的人物配图2 的动作」写在了**会持久化的**优化要求里。
    2. **与 `all_names` 求交，引用已删文件的项自然消失。** 只在写入时清理是不够的：
       删除是另一个请求，两次写入之间隔着的不确定性正是悬空引用的来源。
    3. **`stored` 为 None 时默认全选（截到上限）。** 这条覆盖三种「没有勾选记录」的
       情况：本次改动之前的老会话、刚建的新会话、`selection.json` 被手工删掉。
       默认成「一张不选」会让所有存量会话升级后**静默变成纯文本模式** ——
       模型看不到图，用户只看到「跑完了、结果有点怪」。
    """
    names = [n for n in all_names if isinstance(n, str)]
    if stored is None:
        return names[:MAX_SUBMIT_IMAGES]
    if not isinstance(stored, (list, tuple, set)):
        return names[:MAX_SUBMIT_IMAGES]
    wanted = {n for n in stored if isinstance(n, str)}
    # 用 all_names 的顺序遍历（不是遍历 wanted）—— 第 1 条规则就落在这个循环的方向上
    return [n for n in names if n in wanted][:MAX_SUBMIT_IMAGES]


def selection_file(sid: str):
    """`selection.json` 的绝对路径；会话不存在时返回 None。"""
    d = session_dir(sid)
    if d is None:
        return None
    return os.path.join(d, 'selection.json')


def read_selection(sid: str):
    """读勾选态。**文件不存在返回 None**（= 从没勾过，由 resolve_selection 兜底成全选），
    与「勾了但一张都没勾」（空列表）是两种不同语义，别把前者也返回 []。
    """
    p = selection_file(sid)
    if p is None or not os.path.isfile(p):
        return None
    try:
        with open(p, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception as e:
        log.error('[会话 %s] 勾选态读取失败（按默认全选处理）: %s', sid, e)
        return None
    if not isinstance(data, dict):
        return None
    return [n for n in (data.get('names') or []) if isinstance(n, str)]


def write_selection(sid: str, names: list) -> bool:
    """原子写勾选态。失败只记日志 —— 勾选丢了顶多重勾一次，不该打断用户操作。"""
    p = selection_file(sid)
    if p is None:
        return False
    try:
        write_text_atomic(p, json.dumps({'names': list(names)}, ensure_ascii=False, indent=1))
        return True
    except Exception as e:
        log.error('[会话 %s] 勾选态保存失败: %s', sid, e)
        return False


def selected_images(sid: str) -> list:
    """本会话本次该送模型的图（工作区顺序，≤ MAX_SUBMIT_IMAGES）。"""
    return resolve_selection(list_images(sid), read_selection(sid))


def image_rows(sid: str) -> list:
    """图清单的统一负载：每张带 `selected` 与 `submit_index`。

    `submit_index` 是**送模型的序号**（勾选的按工作区序连续编 1..K），未勾选为 0。
    它必须与 `_build_image_content` 的 `[参考图N]` 标记同源，否则用户说的「图2」
    会指到别的图上（静默错位）。所以在服务端算好下发，前端只负责显示。

    五个图路由共用它 —— 各拼一遍的话，迟早有一个忘了跟着改。
    """
    all_names = list_images(sid)
    chosen = resolve_selection(all_names, read_selection(sid))
    rank = {n: i + 1 for i, n in enumerate(chosen)}
    return [{'name': n, 'selected': n in rank, 'submit_index': rank.get(n, 0)}
            for n in all_names]


def image_payload(sid: str) -> dict:
    """图路由的完整响应体（清单 + 两个计数），与 image_rows 一起保证形状一致。"""
    rows = image_rows(sid)
    return {
        'images': rows,
        'count': len(rows),
        'selected_count': sum(1 for r in rows if r['selected']),
        'max_submit': MAX_SUBMIT_IMAGES,
    }


def resolve_image(sid: str, filename: str):
    """把「会话 + 文件名」解析为绝对路径；不合法/不存在时返回 None。"""
    if not filename or os.path.basename(filename) != filename:
        return None
    d = images_dir(sid)
    if d is None:
        return None
    path = os.path.abspath(os.path.join(d, filename))
    if not is_within_directory(path, d):
        return None
    if not os.path.isfile(path):
        return None
    return path


# ── 会话读写的公共部分 ──────────────────────────────────────────────────────

def read_session(sid: str):
    """读 session.json；不存在/损坏时返回 None（不抛，调用方按「空会话」处理）。"""
    d = session_dir(sid)
    if d is None:
        return None
    p = os.path.join(d, 'session.json')
    if not os.path.isfile(p):
        return None
    try:
        with open(p, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception as e:
        log.error('[会话] 读取失败 %s: %s', sid, e)
        return None


def write_session(sid: str, data: dict) -> bool:
    """原子写 session.json。失败返回 False（调用方据此提示，不静默丢弃）。"""
    d = session_dir(sid)
    if d is None:
        return False
    data = dict(data)
    data['sid'] = sid
    data['updated_at'] = time.strftime('%Y-%m-%d %H:%M:%S')
    try:
        write_text_atomic(os.path.join(d, 'session.json'),
                          json.dumps(data, ensure_ascii=False, indent=1))
        return True
    except Exception as e:
        log.error('[会话] 保存失败 %s: %s', sid, e)
        return False


def session_title(data: dict) -> str:
    """会话标题 = **提示词的标签行**，不是优化要求。

    会话里两样都记着，但只有一样回答得了「这是哪份提示词」：要求说的是
    「这轮让它干什么」（精简到 30 条 / 去掉查不到的 / 按图2 改动作），同一份
    提示词迭代三轮，列表里就并排出现三条互不相干的标题 —— 用户认不出它们是
    同一个角色的图。提示词才是这个会话的主题，而它开头恰好长得就像个名字。

    只取**首个换行之前**：提示词是「标签行 + 换行 + 描述段」两段式（同
    `prompt_tool._split_prompt_entries` 的口径），描述段进标题会被截成半句散文。

    **读的时候现算，不落盘**：用户改了提示词重跑，存下来的标题还是上一轮的，
    而列表显示的是算出来的 —— 那就是两个真相来源。老会话 session.json 里残留的
    `title` 字段不再被读取，无害。
    """
    head = (data.get('prompt') or '').strip().split('\n', 1)[0].strip()
    if head:
        return head[:40] + ('…' if len(head) > 40 else '')
    # 还没跑过（新建的空会话）：退到要求，再退到「空会话」
    req = (data.get('request') or '').strip().replace('\n', ' ')
    return req[:40] + ('…' if len(req) > 40 else '') if req else '空会话'


def session_summary(sid: str) -> dict:
    """会话列表项：不含结果正文（列表不需要，回传会让响应很大）。"""
    data = read_session(sid) or {}
    imgs = list_images(sid)
    req = (data.get('request') or '').strip().replace('\n', ' ')
    return {
        'sid': sid,
        'title': session_title(data),
        # 要求单独回传：它与标题互补 —— 标题答「这是哪份提示词」，
        # 要求答「这轮让它干什么」，前端把它显示在列表项第二行
        'request': req[:60] + ('…' if len(req) > 60 else ''),
        'updated_at': data.get('updated_at') or '',
        'created_at': data.get('created_at') or '',
        'image_count': len(imgs),
        'has_result': bool(data.get('result')),
        'thumb': imgs[0] if imgs else '',
    }


def list_sessions() -> list:
    """所有会话，按最后修改时间新→旧。

    用目录 mtime 而不是 session.json 里的 updated_at：后者在文件根本没写过
    （新会话还没跑过）时是空的，会导致它排到列表最末，用户找不到刚建的会话。
    """
    root = sessions_root()
    out = []
    for name in os.listdir(root):
        if not _SID_RE.match(name):
            continue          # 不认识的目录一律不碰（用户手放的东西不该被误删）
        p = os.path.join(root, name)
        if not os.path.isdir(p):
            continue
        try:
            mtime = os.path.getmtime(p)
        except OSError:
            mtime = 0
        s = session_summary(name)
        s['_mtime'] = mtime
        out.append(s)
    out.sort(key=lambda x: x['_mtime'], reverse=True)
    for s in out:
        s.pop('_mtime', None)
    return out


def create_session() -> str:
    """新建空会话，返回 sid。

    **必须传 must_exist=False**：目录还没建，默认的存在性校验会返回 None，
    接着 os.path.join(None, 'images') 直接 TypeError（实测踩过）。
    """
    sid = _new_sid()
    d = session_dir(sid, must_exist=False)
    os.makedirs(os.path.join(d, 'images'), exist_ok=True)
    # 只写 created_at，**不写 title**：标题由 `session_title()` 读时现算，
    # 存一份就与 prompt 脱节了（见该函数的 docstring）
    write_session(sid, {'created_at': time.strftime('%Y-%m-%d %H:%M:%S')})
    log.info('[会话] 新建 %s', sid)
    return sid


# ── 路由：会话 ──────────────────────────────────────────────────────────────

@prompt_workspace_bp.route('/prompt_sessions', methods=['GET'])
def sessions_list():
    """会话列表 + 最近一个（前端据此恢复上次的工作现场，刷新不丢）。"""
    # 刻意不带 max_submit：这一路只用来渲染会话列表，上限由 /prompt_sessions/<sid>
    # 与各图路由（都走 image_payload）下发。早先这里挂过一个 max_images，前端从未读过
    # —— 留着就是「有个字段看起来该用」，下一个人会以为它是真相来源。
    items = list_sessions()
    return jsonify({'sessions': items, 'current': items[0]['sid'] if items else None})


@prompt_workspace_bp.route('/prompt_sessions/new', methods=['POST'])
def sessions_new():
    """新建会话（空工作台）。"""
    sid = create_session()
    out = {'sid': sid, 'session': session_summary(sid)}
    out.update(image_payload(sid))
    return jsonify(out)


@prompt_workspace_bp.route('/prompt_sessions/<sid>', methods=['GET'])
def sessions_get(sid):
    """加载会话：输入 + 结果 + 图片清单，一并还原。"""
    if session_dir(sid) is None:
        return jsonify({'error': '会话不存在'}), 404
    data = read_session(sid) or {}
    out = {'sid': sid, 'data': data}
    out.update(image_payload(sid))
    return jsonify(out)


@prompt_workspace_bp.route('/prompt_sessions/<sid>/delete', methods=['POST'])
def sessions_delete(sid):
    """删除会话 —— **连同它的图片**。会话是整体，图不独立于它存在。"""
    d = session_dir(sid)
    if d is None:
        return jsonify({'error': '会话不存在'}), 404
    try:
        shutil.rmtree(d)
    except Exception as e:
        log.error('[会话] 删除失败 %s: %s', sid, e)
        return jsonify({'error': f'删除失败：{e}'}), 500
    log.info('[会话] 已删除 %s（含其参考图）', sid)
    return jsonify({'ok': True, 'sessions': list_sessions()})


# ── 路由：会话内的参考图 ────────────────────────────────────────────────────

@prompt_workspace_bp.route('/prompt_sessions/<sid>/images', methods=['POST'])
def images_upload(sid):
    """上传参考图（可多张）。

    不做重定向、不回跳到整页：参考图是会话内的增删，不该让整页刷新、
    丢掉用户已经写好的提示词与优化要求。

    先确认会话存在再建 `images/` —— 弄反了就会替一个不存在的 sid 把会话建出来
    （见 `images_dir` 的说明）。
    """
    d = session_dir(sid)
    if d is None:
        return jsonify({'error': '会话不存在'}), 404
    d = os.path.join(d, 'images')
    # images/ 被手工删掉时自愈：会话本身还在，不该逼用户重建整个会话
    os.makedirs(d, exist_ok=True)
    files = [f for f in request.files.getlist('files') if f and f.filename]
    if not files:
        return jsonify({'error': '没有选择文件'}), 400

    existing = set(list_images(sid))
    added, rejected = [], []
    for f in files:
        filename = os.path.basename(f.filename.replace('\\', '/')).strip(' .')
        if not filename:
            continue
        if not allowed_file(filename, 'image'):
            rejected.append(f'{filename}（不是支持的图片格式）')
            continue
        # 这里**不再有数量上限**：工作区是素材库，随用户攒。
        # 限制只落在「送模型」那一步（MAX_SUBMIT_IMAGES），见 resolve_selection。
        target = os.path.join(d, filename)
        # 重名不覆盖：静默替换会让用户刚传的那张凭空消失
        if filename in existing or os.path.exists(target):
            stem, ext = os.path.splitext(filename)
            n = 2
            while os.path.exists(os.path.join(d, f'{stem}_{n}{ext}')):
                n += 1
            filename = f'{stem}_{n}{ext}'
            target = os.path.join(d, filename)
        try:
            f.save(target)
            added.append(filename)
            log.info('[会话 %s] 参考图已上传: %s', sid, filename)
        except Exception as e:
            log.error('[会话 %s] 保存失败 %s: %s', sid, filename, e)
            rejected.append(f'{filename}（保存失败：{e}）')

    # 新传的图自动补勾到上限 —— 常见用法是「加图就是为了用」，不补勾用户会以为坏了。
    # 补不满时说清楚为什么（下面的 auto_selected 让前端能提示），别让它静默发生：
    # 「传了 3 张却只勾上 1 张」若不解释，用户会以为是随机挑的。
    auto_selected, auto_skipped = [], 0
    if added:
        cur = selected_images(sid)
        room = MAX_SUBMIT_IMAGES - len(cur)
        for nm in added:
            if room > 0 and nm not in cur:
                cur.append(nm)
                auto_selected.append(nm)
                room -= 1
            elif nm not in cur:
                auto_skipped += 1
        # 按工作区自然序存（resolve_selection 自己会再排一次，这里存序只为文件可读）
        write_selection(sid, [n for n in list_images(sid) if n in set(cur)])

    out = {'added': added, 'rejected': rejected,
           'auto_selected': auto_selected, 'auto_skipped': auto_skipped}
    out.update(image_payload(sid))
    return jsonify(out)


@prompt_workspace_bp.route('/prompt_sessions/<sid>/images/delete', methods=['POST'])
def images_delete(sid):
    """删除会话内的单张参考图。"""
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({'error': '请求体必须是 JSON 对象'}), 400
    name = (body.get('name') or '').strip()
    path = resolve_image(sid, name)
    if path is None:
        return jsonify({'error': '文件不存在或名字非法'}), 400
    try:
        os.unlink(path)
    except Exception as e:
        return jsonify({'error': f'删除失败：{e}'}), 500
    # 不必清理 selection.json：resolve_selection 每次求交，已删名字自然消失，
    # 其余图的编号会重新连续。写时清理只是优化，只做它就会留下悬空引用的窗口。
    return jsonify(image_payload(sid))


@prompt_workspace_bp.route('/prompt_sessions/<sid>/images/select', methods=['POST'])
def images_select(sid):
    """设置本次要送模型的参考图（勾选态）。

    body: `{"names": ["ref2.png", "ref4.png"]}`
    服务端把名字**过滤 + 去重 + 按工作区序重排**后落盘，前端只负责显示：
    名字是路径的来源，所以任何不在会话目录里的名字在这里就被丢掉了，不往下传。

    上限在这里也校验一次。前端已拦（勾第 6 张直接拒绝、不发请求），但这里是防线 ——
    `/prompt_adjust` 才是真正花 token 的地方，它自己还会再校验一次。
    """
    if session_dir(sid) is None:
        return jsonify({'error': '会话不存在'}), 404
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        body = {}
    raw = body.get('names')
    if not isinstance(raw, (list, tuple)):
        return jsonify({'error': 'names 必须是数组'}), 400
    wanted = {n for n in raw if isinstance(n, str)}
    chosen = [n for n in list_images(sid) if n in wanted]
    if len(chosen) > MAX_SUBMIT_IMAGES:
        return jsonify({'error': f'单次最多提交 {MAX_SUBMIT_IMAGES} 张参考图，'
                                 f'当前选了 {len(chosen)} 张'}), 400
    write_selection(sid, chosen)
    log.info('[会话 %s] 勾选参考图 %d 张: %s', sid, len(chosen), ', '.join(chosen) or '（无）')
    return jsonify(image_payload(sid))


@prompt_workspace_bp.route('/prompt_sessions/<sid>/images/clear', methods=['POST'])
def images_clear(sid):
    """清空会话内的参考图（**只删图，保留会话与文字结果**）。

    刻意不接受任何 mode 参数 —— `/clear_all` 曾因把 `" ALL "` 归一化成 `'all'`
    而误清空上传目录，这里不给调用方任何「换个写法就清别的东西」的机会。
    作用目录由本模块从 sid 解析，永远落在该会话的 images/ 内。
    """
    d = images_dir(sid)
    if d is None:
        return jsonify({'error': '会话不存在'}), 404
    removed, failed = 0, []
    for f in list_images(sid):
        try:
            os.unlink(os.path.join(d, f))
            removed += 1
        except Exception as e:
            failed.append(f'{f}（{e}）')
    # 勾选态顺手清掉。`resolve_selection` 本来就会把已删名字过滤掉（结果同样是空），
    # 但这里显式写一次是因为「图没了、勾选记录还留着旧名字」在排查时会误导人。
    # **必须放在删除循环之后** —— 清空逻辑与删图在同一处，写反了就成了「图还在、
    # 勾选先没了」，用户会看到所有图突然变成未勾选。
    write_selection(sid, [])
    log.info('[会话 %s] 已清空参考图：删除 %d 张', sid, removed)
    out = {'removed': removed, 'failed': failed}
    out.update(image_payload(sid))
    return jsonify(out)


@prompt_workspace_bp.route('/prompt_sessions/<sid>/images/<path:filename>')
def images_file(sid, filename):
    """访问会话内的参考图（send_from_directory 自身会挡路径遍历）。"""
    d = images_dir(sid)
    if d is None:
        return jsonify({'error': '会话不存在'}), 404
    return send_from_directory(d, filename)
