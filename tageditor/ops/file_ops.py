# -*- coding: utf-8 -*-
import os
import tempfile
import uuid
from flask import Blueprint, request, redirect, url_for, jsonify, send_from_directory, current_app
from tageditor.core.config import allowed_file, safe_filename, is_within_directory, get_image_files, write_text_atomic
import logging


log = logging.getLogger(__name__)

file_ops_bp = Blueprint('file_ops', __name__)


def _get_image_size(file_path):
    """读取图片尺寸 (width, height)。识别不了返回 (0, 0)。

    实现已统一到 core/image_io.image_size（PIL 只读文件头 → 纯头部解析 → cv2 兜底）。

    **顺序是改过的，别退回「cv2 优先」**：`cv2.imread` 会解码整张图，而这里只需要宽高。
    批量重命名会对每张图调一次，1000 张 4K 图就是几十秒到几分钟、每张峰值 `h*w*4` 字节，
    而文件下半部分早就写好了零解码的头部解析，却因为 cv2 排在前面而**永远轮不到**。
    另外 cv2 在 Windows 上还读不了中文路径，而本项目的文件名刻意保留中文。

    cv2 仍保留为**最后**的兜底（对畸形/非常规文件最宽容）：它返回 (0, 0) 时调用方会把
    `0x0` 写进新文件名，那是「静默的错误数据」，比慢严重得多。
    """
    from tageditor.core.image_io import image_size
    return image_size(file_path)


def _sanitize_rename_name(name):
    """清洗用户输入的 name 部分（用于 {name}-{w}x{h}-编号 模板）。

    移除路径分隔符、控制字符，把 Windows/Linux 文件名非法字符替换为下划线。
    保留中文等任意 Unicode 文本，与 safe_filename 风格一致。
    限制最长 128 字符，避免超出文件系统限制。
    """
    if name is None:
        return ''
    name = str(name).replace('\x00', '')
    # 替换文件系统非法字符（Windows: \ / : * ? " < > |，Linux/macOS: /）
    for ch in '\\/:*?"<>|':
        name = name.replace(ch, '_')
    return name.strip()[:128]


# 上传后允许跳回的页面（表单里的 next 字段）。
# 白名单而非直接 redirect(next)：next 来自表单，不校验就是开放重定向漏洞。
_UPLOAD_NEXT_PAGES = {'tag_editor': 'tag_editor', 'image_editor': 'editor',
                      'prompt_tool': 'prompt_tool_page'}   # 值必须是 endpoint 名：新页面的函数是 prompt_tool_page


@file_ops_bp.route('/upload', methods=['POST'])
def upload_files():
    """上传文件。可选表单字段 next 指定上传后回哪个页面（默认标签编辑页）。"""
    if 'files' not in request.files:
        return redirect(request.url)

    files = request.files.getlist('files')
    upload_dir = current_app.config['UPLOAD_FOLDER']

    for file in files:
        if file.filename == '':
            continue
        if file and (allowed_file(file.filename, 'image') or allowed_file(file.filename, 'text')):
            filename = safe_filename(file.filename)
            save_path = os.path.join(upload_dir, filename)
            log.info(f"[上传] {filename} -> {save_path}")
            file.save(save_path)

    endpoint = _UPLOAD_NEXT_PAGES.get((request.form.get('next') or '').strip())
    return redirect(url_for(endpoint or 'tag_editor'))


@file_ops_bp.route('/get_caption/<image_name>')
def get_caption(image_name):
    """获取图片对应的标签，同时从 SQLite 查翻译返回"""
    from tageditor.translate.translation import _lookup_cn_from_db
    filename = safe_filename(image_name)
    upload_dir = os.path.abspath(current_app.config['UPLOAD_FOLDER'])
    file_path = os.path.abspath(os.path.join(upload_dir, filename))
    if not is_within_directory(file_path, upload_dir):
        return jsonify({'caption': '', 'translations': []})
    base_name = os.path.splitext(filename)[0]
    caption_file = f"{base_name}.txt"
    caption_path = os.path.join(upload_dir, caption_file)

    caption = ""
    if os.path.exists(caption_path):
        try:
            with open(caption_path, 'r', encoding='utf-8') as f:
                caption = f.read()
        except Exception as e:
            log.error(f"读取标签文件失败: {str(e)}")

    # 读取自然语言描述（.nl.txt）
    nl_path = os.path.join(upload_dir, f"{base_name}.nl.txt")
    nl_caption = ""
    if os.path.exists(nl_path):
        try:
            with open(nl_path, 'r', encoding='utf-8') as f:
                nl_caption = f.read().strip()
        except Exception as e:
            log.error(f"读取自然语言描述文件失败: {str(e)}")

    # 一次性返回标签 + 翻译（从 SQLite cn_name 查）
    tags = [t.strip() for t in caption.split(',') if t.strip()] if caption else []
    hits = _lookup_cn_from_db(tags)
    translations = [hits.get(tag, '') for tag in tags]

    return jsonify({'caption': caption, 'translations': translations, 'nl_caption': nl_caption})


@file_ops_bp.route('/save_caption/<image_name>', methods=['POST'])
def save_caption(image_name):
    """保存标签到文件。统一小写 + 去重后写入 txt。"""
    filename = safe_filename(image_name)
    upload_dir = os.path.abspath(current_app.config['UPLOAD_FOLDER'])
    file_path = os.path.abspath(os.path.join(upload_dir, filename))
    if not is_within_directory(file_path, upload_dir):
        return jsonify({'success': False, 'error': '非法路径'}), 400

    # silent=True + 类型校验：body 是合法 JSON 数组/字符串时 get_json() 会原样返回，
    # 后面 .get() 抛 AttributeError → 500；这里直接判成 400。
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({'success': False, 'error': '请求体必须是 JSON 对象'}), 400
    content = data.get('content', '')
    if not isinstance(content, str):
        return jsonify({'success': False, 'error': 'content 必须是字符串'}), 400

    # 保存时统一转小写 + 去重（与前端逻辑一致）。
    # 统一小写保证 DB name 列与查询 key 一致，避免翻译查不到；
    # 也防止非浏览器客户端写入重复/混合大小写标签。
    seen = set()
    deduped = []
    for t in content.split(','):
        t = t.strip()
        if t:
            t = t.lower()
            if t not in seen:
                seen.add(t)
                deduped.append(t)
    content = ', '.join(deduped)

    base_name = os.path.splitext(filename)[0]
    caption_file = f"{base_name}.txt"
    caption_path = os.path.join(upload_dir, caption_file)

    try:
        write_text_atomic(caption_path, content)
    except Exception as e:
        log.error(f"保存标签文件失败: {str(e)}")
        return jsonify({'success': False, 'error': str(e)})

    return jsonify({'success': True})


def _write_nl_caption(upload_dir, base_name, content):
    """把自然语言描述写入 {base}.nl.txt。抽出来供 /save_nl_caption 与提示词优化器复用。"""
    nl_path = os.path.join(upload_dir, f"{base_name}.nl.txt")
    write_text_atomic(nl_path, content)


@file_ops_bp.route('/save_nl_caption/<image_name>', methods=['POST'])
def save_nl_caption(image_name):
    """保存自然语言描述到 .nl.txt"""
    filename = safe_filename(image_name)
    upload_dir = os.path.abspath(current_app.config['UPLOAD_FOLDER'])
    file_path = os.path.abspath(os.path.join(upload_dir, filename))
    if not is_within_directory(file_path, upload_dir):
        return jsonify({'success': False, 'error': '非法路径'}), 400

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({'success': False, 'error': '请求体必须是 JSON 对象'}), 400
    content = data.get('content', '')
    if not isinstance(content, str):
        return jsonify({'success': False, 'error': 'content 必须是字符串'}), 400
    content = content.strip()

    base_name = os.path.splitext(filename)[0]
    try:
        _write_nl_caption(upload_dir, base_name, content)
    except Exception as e:
        log.error(f"保存自然语言描述失败: {str(e)}")
        return jsonify({'success': False, 'error': str(e)})

    return jsonify({'success': True})


# /filter_images 的标签内容缓存：{txt 绝对路径: (mtime_ns, size, 小写内容)}
#
# 为什么需要：调用方是标签编辑页的过滤输入框，前置 **250ms debounce**（tag_editor.html），
# 也就是说用户每停顿一下就整目录扫一遍。旧实现每次请求把每个 `.txt` 完整读进来，
# 1 万个文件、平均 1KB 就是每次读 10MB，机械盘上秒级，随图库增长线性劣化。
#
# **失效只靠 mtime+size，不在写路径手动打点**：写路径太多（保存标签、批量替换、
# 添加触发词、上传、删除、清空、重命名…），漏一个就会出现「过滤结果是旧的」这种
# 静默错误；而「按签名自然失效」漏不掉，与 config.load_prompts 是同一个范式。
_filter_cache = {}


def _read_tag_text_cached(txt_path):
    """读标签文件内容（小写），按 (mtime_ns, size) 签名缓存。读不到返回 None。"""
    try:
        st = os.stat(txt_path)
    except OSError:
        _filter_cache.pop(txt_path, None)
        return None
    key = (st.st_mtime_ns, st.st_size)
    hit = _filter_cache.get(txt_path)
    if hit is not None and hit[0] == key:
        return hit[1]
    try:
        with open(txt_path, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read().lower()
    except OSError:
        return None
    _filter_cache[txt_path] = (key, content)
    return content


@file_ops_bp.route('/filter_images', methods=['POST'])
def filter_images():
    """按标签内容子串过滤图片（大小写不敏感）。

    仅匹配 {base}.txt 标签文件内容，忽略 .nl.txt 描述文件；
    无关键词时返回全部图片。结果保持 get_image_files 自然排序，与列表一致。
    """
    data = request.get_json(silent=True)
    # 只读过滤接口：非 dict 一律按「无关键词」处理（返回全部），不 500
    keyword = (data.get('keyword', '') if isinstance(data, dict) else '').strip().lower()
    upload_dir = current_app.config['UPLOAD_FOLDER']
    if not keyword:
        return jsonify({'images': get_image_files(upload_dir)})
    matches = []
    for name in get_image_files(upload_dir):
        base = os.path.splitext(name)[0]
        content = _read_tag_text_cached(os.path.join(upload_dir, base + '.txt'))
        if content is not None and keyword in content:
            matches.append(name)
    return jsonify({'images': matches})


@file_ops_bp.route('/tag_stats')
def tag_stats():
    """统计所有标签出现次数，附翻译（从 SQLite 查）。排除 .nl.txt 描述文件"""
    from collections import Counter
    from tageditor.translate.translation import _lookup_cn_from_db
    upload_dir = current_app.config['UPLOAD_FOLDER']
    counter = Counter()
    for filename in os.listdir(upload_dir):
        if not (filename.endswith('.txt') and not filename.endswith('.nl.txt')):
            continue
        # 单文件失败跳过并记日志：统计页是全目录扫描，一个编码损坏的文件不该让
        # 整个接口 500（同 filter_images 的容错口径；errors='ignore' 兜住 GBK 混入）
        try:
            with open(os.path.join(upload_dir, filename), 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read().strip()
        except OSError as e:
            log.error(f"[tag_stats] 读取标签文件失败 {filename}: {e}")
            continue
        if not content:
            continue
        for tag in content.split(','):
            tag = tag.strip()
            if tag:
                counter[tag] += 1  # 保留原始大小写，使查找/替换可区分大小写

    # 批量从 SQLite 查翻译
    all_tags = list(counter.keys())
    hits = _lookup_cn_from_db(all_tags)
    stats = []
    for tag, count in counter.most_common():
        entry = {'tag': tag, 'count': count}
        tr = hits.get(tag, '')
        if tr:
            entry['translation'] = tr
        stats.append(entry)

    return jsonify({'stats': stats, 'total_files': len([f for f in os.listdir(upload_dir)
                                                        if f.endswith('.txt') and not f.endswith('.nl.txt')])})


@file_ops_bp.route('/clear_all', methods=['POST'])
def clear_all():
    """按模式清空上传目录中的文件。

    请求体 JSON：
        mode: 'all'  全部文件（图片 + .txt + .nl.txt，默认，兼容旧调用）
              'nl'   仅自然语言描述 .nl.txt
              'tags' 所有标签文件 .txt + .nl.txt（保留图片）
    返回 JSON: {removed: N}
    """
    data = request.get_json(silent=True)
    # 非 dict 的请求体（如 `[1]`、`"all"`）一律 400，**不要**回落到默认 mode。
    # 这是本应用唯一的批量删除接口，默认 mode='all' 会清空整个 uploads 目录；
    # 「参数没看懂就执行最危险的默认值」是绝不能被接受的降级路径。
    # 前端调用点只有一个（executeClear），永远发 dict，所以 400 不会误伤正常使用。
    if not isinstance(data, dict):
        return jsonify({'error': '请求体必须是 JSON 对象'}), 400
    mode = data.get('mode', 'all')
    if not isinstance(mode, str):
        return jsonify({'error': f'mode 必须是字符串，收到 {type(mode).__name__}'}), 400
    # 不做 strip/lower 之类的宽松归一化：本接口的失败模式是「删光 uploads」，
    # 归一化会扩大命中 'all' 的输入集合（' ALL ' 会被折叠成破坏性的 'all'）。
    # 只接受精确的三个字面量，其余一律 400 由调用方自己修正。
    if mode not in ('all', 'nl', 'tags'):
        return jsonify({'error': f'未知模式: {mode!r}（只接受 all / nl / tags）'}), 400
    upload_dir = current_app.config['UPLOAD_FOLDER']
    removed = 0
    # mode 已在上面白名单校验过，这里无需再判 else 分支
    for filename in os.listdir(upload_dir):
        if not os.path.isfile(os.path.join(upload_dir, filename)):
            continue  # 跳过子目录（os.unlink 对目录会抛错并计入失败日志）
        if mode == 'all':
            delete = True
        elif mode == 'nl':
            delete = filename.endswith('.nl.txt')
        else:  # 'tags'
            delete = filename.endswith('.txt')  # .nl.txt 同样以 .txt 结尾，一并涵盖
        if delete:
            try:
                os.unlink(os.path.join(upload_dir, filename))
                removed += 1
            except Exception as e:
                log.error(f"删除文件失败 {filename}: {str(e)}")

    return jsonify({'removed': removed})


@file_ops_bp.route('/delete/<image_name>', methods=['POST'])
def delete_image(image_name):
    """删除图片及对应的txt文件"""
    filename = safe_filename(image_name)
    upload_dir = os.path.abspath(current_app.config['UPLOAD_FOLDER'])
    file_path = os.path.abspath(os.path.join(upload_dir, filename))
    if not is_within_directory(file_path, upload_dir):
        return jsonify({'success': False, 'error': '非法路径'}), 400
    # 三个文件逐个容错：图片删掉了但 .txt 被占用（Windows 上预览器持有句柄是常态）
    # 时，裸 unlink 会 500——而图片已经没了，前端重试只会 404。记录失败的文件名返回。
    failed = []
    for p in (file_path, os.path.join(upload_dir, f"{os.path.splitext(filename)[0]}.txt"),
              os.path.join(upload_dir, f"{os.path.splitext(filename)[0]}.nl.txt")):
        if not os.path.exists(p):
            continue
        try:
            os.unlink(p)
        except OSError as e:
            failed.append(f"{os.path.basename(p)}: {e}")
            log.error(f"[删除] 失败 {p}: {e}")
    if failed:
        return jsonify({'success': False, 'error': '；'.join(failed)}), 500
    return jsonify({'success': True})


@file_ops_bp.route('/uploads/<filename>')
def uploaded_file(filename):
    """提供上传文件的访问（带浏览器缓存，减少切换图片时的重复请求）"""
    resp = send_from_directory(current_app.config['UPLOAD_FOLDER'], filename)
    # no-cache + ETag：浏览器每次向服务器校验，文件未变时返回 304（无内容），
    # 文件变化后（如缩放保存）自动返回新内容，无需 ?t= 参数或手动刷新。
    resp.headers['Cache-Control'] = 'no-cache'
    return resp


@file_ops_bp.route('/rename_files', methods=['POST'])
def rename_files():
    """批量重命名图片及其对应标签文件，格式：{name}-{width}x{height}-编号（编号从 1 开始，按文件名自然排序）。

    请求体 JSON：
        name: str   模板中的自定义名称部分（必填，清洗非法字符）
        preview: bool  预览模式：不落盘，只返回每张图的新旧文件名对照（默认 False）

    返回 JSON：
        preview=True → { preview: [{old, new, width, height}], total: N }
        preview=False→ { renamed: N, total: N, errors: 0 }
        错误 → { error: str } (HTTP 400)

    重命名采用两阶段（old → __tmp__{i} → new）避免源/目标名碰撞时互相覆盖，
    图片扩展名保留原图后缀（jpg/jpeg/gif/webp/png 均原样保留，仅改主干名），
    同名 .txt 标签随之联动。
    """
    from tageditor.core.config import get_image_files

    # silent=True + 类型校验：漏 Content-Type 时 Flask 兜成 HTML 错误页（415），
    # 前端按 Content-Type 分流拿不到真实提示；合法 JSON 的非对象会让 .get() 抛 500。
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({'error': '请求体必须是 JSON 对象'}), 400
    name = _sanitize_rename_name(data.get('name', ''))
    if not name:
        return jsonify({'error': '名称不能为空'}), 400

    preview = bool(data.get('preview', False))

    upload_dir = os.path.abspath(current_app.config['UPLOAD_FOLDER'])
    images = get_image_files(upload_dir)  # 已按自然排序

    # 计算每张图的新文件名
    # plan: [(old_base, new_base, ext, width, height)]  ext 含点号（如 '.jpg'），保留原图格式
    plan = []
    used = set()  # 已生成的新文件名（防冲突）
    for i, filename in enumerate(images):
        old_base, ext = os.path.splitext(filename)
        w, h = _get_image_size(os.path.join(upload_dir, filename))
        # 编号从 1 开始
        num = i + 1
        new_base = f"{name}-{w}x{h}-{num}"

        # 新文件名去重：若已存在（理论上编号唯一不会冲突，但 name+尺寸相同时理论可能），
        # 追加序号后缀保证唯一
        candidate = new_base
        suffix = 1
        while candidate in used:
            suffix += 1
            candidate = f"{new_base}_{suffix}"
        new_base = candidate
        used.add(new_base)
        plan.append((old_base, new_base, ext, w, h))

    # 预览模式：不落盘，返回新旧名对照
    if preview:
        return jsonify({
            'preview': [
                {'old': old_base + ext, 'new': new_base + ext,
                 'width': w, 'height': h}
                for old_base, new_base, ext, w, h in plan
            ],
            'total': len(plan)
        })

    # 执行重命名：两阶段（避免源/目标同名覆盖）
    # 阶段1：old_base.<图片后缀> 和 old_base.txt → <本次唯一前缀>{i}.<后缀>
    # 阶段2：<前缀>{i}.<后缀> → new_base.<后缀>
    # 图片用原图扩展名（保留格式），标签固定 .txt。
    #
    # 前缀带随机串而不只是固定字符串：固定前缀的「清理上次残留」会把**上一次
    # 被中断的重命名**留在盘上的 tmp 文件删掉——而那些文件是用户还没落地的新名字，
    # 图直接丢失。而且两个浏览器同时点重命名会共用同名前缀、互相覆盖。
    # 随机后缀保证前缀唯一，只清理本次自己产生的残留。
    tmp_prefix = '__tageditor_rename_' + uuid.uuid4().hex[:8] + '_'

    renamed = 0
    errors = 0
    try:
        for i, (old_base, new_base, ext, _, _) in enumerate(plan):
            # 三个待重命名后缀：图片原后缀 + 标签 .txt + 自然语言描述 .nl.txt
            exts = (ext, '.txt', '.nl.txt')
            # 阶段1：old → tmp
            for e in exts:
                old_path = os.path.join(upload_dir, old_base + e)
                tmp_path = os.path.join(upload_dir, f"{tmp_prefix}{i}{e}")
                if os.path.exists(old_path):
                    try:
                        os.rename(old_path, tmp_path)
                    except Exception as ex:
                        log.error(f"[重命名] 阶段1失败 {old_path} -> {tmp_path}: {ex}")
                        errors += 1
            # 阶段2：tmp → new
            moved_any = False
            for e in exts:
                tmp_path = os.path.join(upload_dir, f"{tmp_prefix}{i}{e}")
                new_path = os.path.join(upload_dir, new_base + e)
                if os.path.exists(tmp_path):
                    try:
                        if os.path.exists(new_path):
                            # 目标已被占用：plan 用 used 集合保证新名互不重复，所以
                            # 这只可能是盘上本来就有的同名文件（与本次 batch 无关）。
                            # 原来的 unlink 会静默删掉用户的一张图换成本次的新图——
                            # 改成报错并保住两边，让用户自己决定。
                            log.warning(f"[重命名] 跳过 {new_path}：目标文件已存在，未覆盖")
                            errors += 1
                            continue
                        os.rename(tmp_path, new_path)
                        moved_any = True
                    except Exception as ex:
                        log.error(f"[重命名] 阶段2失败 {tmp_path} -> {new_path}: {ex}")
                        errors += 1
            if moved_any:
                renamed += 1
    finally:
        # 清理**本次**残留的 tmp 文件（阶段1成功但阶段2失败时遗留）。
        # 前缀唯一，不会碰到其它批次/其它请求留下的文件。
        for f in os.listdir(upload_dir):
            if f.startswith(tmp_prefix):
                try:
                    os.remove(os.path.join(upload_dir, f))
                except Exception:
                    pass

    return jsonify({'renamed': renamed, 'total': len(plan), 'errors': errors})


@file_ops_bp.route('/export_zip', methods=['POST'])
def export_zip():
    """将所有图片及对应的标签文件导出为 ZIP。
    请求体 JSON：
        txt_ext: str   标签后缀，仅接受 'txt'（常规标签）或 'nl'（自然语言描述）
    返回 ZIP 文件下载，标签文件名重置为与图片同名。所有文件放入 zip 内 train/ 文件夹。

    **落盘到临时文件再 send_file，不再在内存里拼整个 ZIP**：旧实现用
    `io.BytesIO` + `buf.getvalue()`，而 getvalue() 返回内部缓冲的**副本** ——
    导出 2GB 的 uploads 峰值内存约 2×ZIP，直接 MemoryError 或整机换页。
    另外图片条目改用 ZIP_STORED：JPEG/PNG 本身已压缩，DEFLATE 几乎不减小体积，
    只是白烧 CPU 和时间。
    """
    import time
    import zipfile

    from flask import after_this_request, send_file
    from tageditor.core.config import get_image_files

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({'error': '请求体必须是 JSON 对象'}), 400
    txt_ext = data.get('txt_ext', 'txt')
    # 白名单：txt_ext 会被拼进 Content-Disposition 的 filename 与 arcname。
    # 不白名单时，带 \r\n 的值会让 Werkzeug 抛
    # `ValueError: Header values must not contain newline characters`
    # （实测）→ 500 而不是 400，用户也看不懂。
    if txt_ext not in ('txt', 'nl'):
        return jsonify({'error': "txt_ext 只能是 'txt' 或 'nl'"}), 400

    upload_dir = os.path.abspath(current_app.config['UPLOAD_FOLDER'])
    images = get_image_files(upload_dir)
    if not images:
        return jsonify({'error': '没有可导出的文件'}), 400

    _t0 = time.perf_counter()
    os.makedirs(_EXPORT_TMP_DIR, exist_ok=True)
    _sweep_export_tmp()  # 顺手清上次残留（见 _EXPORT_TMP_DIR 的说明）
    fd, tmp_zip = tempfile.mkstemp(prefix='tageditor_export_', suffix='.zip',
                                   dir=_EXPORT_TMP_DIR)
    os.close(fd)
    text_count = 0
    try:
        with zipfile.ZipFile(tmp_zip, 'w') as zf:
            for filename in images:
                img_path = os.path.join(upload_dir, filename)
                if not os.path.exists(img_path):
                    continue
                # 图片已压缩，STORED 即可（体积几乎相同，CPU 省一个数量级）
                zf.write(img_path, f"train/{filename}", compress_type=zipfile.ZIP_STORED)

                base = os.path.splitext(filename)[0]

                if txt_ext == 'nl':
                    # nl 模式：合并标签(.txt) + 自然语言描述(.nl.txt) 为一个文件
                    tags_content = ''
                    txt_path = os.path.join(upload_dir, f"{base}.txt")
                    if os.path.exists(txt_path):
                        try:
                            with open(txt_path, 'r', encoding='utf-8') as f:
                                tags_content = f.read().strip()
                        except Exception:
                            pass

                    nl_path = os.path.join(upload_dir, f"{base}.nl.txt")
                    nl_content = ''
                    if os.path.exists(nl_path):
                        try:
                            with open(nl_path, 'r', encoding='utf-8') as f:
                                nl_content = f.read().strip()
                        except Exception:
                            pass

                    if tags_content or nl_content:
                        merged = tags_content + (', ' + nl_content if tags_content and nl_content else nl_content)
                        zf.writestr(f"train/{base}.txt", merged.encode('utf-8'),
                                    compress_type=zipfile.ZIP_DEFLATED)
                        text_count += 1
                else:
                    txt_path = os.path.join(upload_dir, f"{base}.txt")
                    if os.path.exists(txt_path):
                        zf.write(txt_path, f"train/{base}.txt",
                                 compress_type=zipfile.ZIP_DEFLATED)
                        text_count += 1
    except Exception:
        # 失败时别把半截 ZIP 留在临时目录
        try:
            os.unlink(tmp_zip)
        except OSError:
            pass
        raise

    size_mb = os.path.getsize(tmp_zip) / 1048576
    log.info(f"[导出] ZIP 完成: {len(images)} 张图 + {text_count} 个标签文件, "
             f"{size_mb:.1f}MB, 耗时 {time.perf_counter() - _t0:.1f}s")

    # 响应体发完之后删临时文件。用 call_on_close（而非 after_this_request）：
    # 后者在响应**发出之前**执行，删掉文件会把下载打断
    # （Windows 上表现为 PermissionError，Linux 上直接发不出去）。
    @after_this_request
    def _register_cleanup(resp):
        resp.call_on_close(lambda: _safe_unlink(tmp_zip))
        return resp

    return send_file(tmp_zip, mimetype='application/zip', as_attachment=True,
                     download_name=f'tags_export_{txt_ext}.zip')


def _safe_unlink(path):
    try:
        os.unlink(path)
    except OSError:
        # 删不掉就留着（下面的清扫会兜住）；绝不因此让响应报错
        pass


# 导出 ZIP 的临时目录。**不能只靠 call_on_close 清理**：那依赖 WSGI 服务器
# 主动调用响应体的 close()，而实测 Flask 测试客户端就不会调（两个导出请求留下
# 两个 ZIP）；真实部署换服务器也可能变。故加一道不依赖服务器行为的兜底：
# 每次导出前顺手清掉目录里过期的残留，进程被杀/客户端断开留下的文件最多活一小时。
_EXPORT_TMP_DIR = os.path.join(tempfile.gettempdir(), 'tageditor_exports')
_EXPORT_TMP_MAX_AGE = 3600  # 秒


def _sweep_export_tmp(max_age=None):
    """删除导出临时目录里过期的 ZIP，返回删掉的数量（尽力而为，不抛异常）。"""
    import time as _time
    max_age = _EXPORT_TMP_MAX_AGE if max_age is None else max_age
    removed = 0
    try:
        now = _time.time()
        for name in os.listdir(_EXPORT_TMP_DIR):
            if not name.endswith('.zip'):
                continue
            p = os.path.join(_EXPORT_TMP_DIR, name)
            try:
                if now - os.path.getmtime(p) > max_age:
                    os.unlink(p)
                    removed += 1
            except OSError:
                continue
    except OSError:
        pass
    return removed
