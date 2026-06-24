# -*- coding: utf-8 -*-
import os
import struct
from flask import Blueprint, request, redirect, url_for, jsonify, send_from_directory, current_app
from config import allowed_file, safe_filename, is_within_directory

file_ops_bp = Blueprint('file_ops', __name__)


def _get_image_size(file_path):
    """读取图片尺寸 (width, height)。

    优先用 cv2（覆盖 PNG/JPEG/WEBP/BMP 等多数格式），失败时回退到纯头部解析：
      - PNG/JPEG/GIF：按格式头部分段读取，仅解码元数据、不解码像素，最快且不依赖 cv2。
      - WEBP：通过 RIFF 块解析（VP8/VP8L/VP8X 三种位图 chunk 的尺寸字段）。
    任一失败返回 (0, 0)。
    """
    ext = os.path.splitext(file_path)[1].lstrip('.').lower()
    # 优先 cv2（最稳，支持任意编码）
    try:
        import cv2
        img = cv2.imread(file_path, cv2.IMREAD_UNCHANGED)
        if img is not None and img.ndim >= 2:
            return img.shape[1], img.shape[0]
    except Exception:
        pass

    # 回退：纯头部解析
    try:
        with open(file_path, 'rb') as f:
            head = f.read(32)

        # PNG: bytes 16-24 为 IHDR 的 width/height（大端 32 位）
        if head.startswith(b'\x89PNG\r\n\x1a\n'):
            if len(head) >= 24:
                w = struct.unpack('>I', head[16:20])[0]
                h = struct.unpack('>I', head[20:24])[0]
                return w, h

        # JPEG: 逐段扫描 SOFx 标记（FFC0-FFCF，不含 FFCC0=JPG/JPEG-LS）取尺寸
        if head.startswith(b'\xff\xd8'):
            with open(file_path, 'rb') as f:
                f.read(2)  # 跳过 SOI
                while True:
                    marker = f.read(2)
                    if len(marker) < 2 or marker[0] != 0xff:
                        break
                    code = marker[1]
                    # SOFx（FFC0-FFCF，排除 FFC4=C0 标记错误范围；实际 SOF 为 C0-C3/C5-C7/C9-CB/CD-CF）
                    if 0xc0 <= code <= 0xcf and code not in (0xc4, 0xc8, 0xcc):
                        seg = f.read(7)  # 长度(2) + 精度(1) + 高(2) + 宽(2)
                        if len(seg) >= 7:
                            h = struct.unpack('>H', seg[3:5])[0]
                            w = struct.unpack('>H', seg[5:7])[0]
                            return w, h
                    elif code in (0xd8, 0xd9):  # SOI/EOI
                        break
                    else:
                        seg_len_bytes = f.read(2)
                        if len(seg_len_bytes) < 2:
                            break
                        seg_len = struct.unpack('>H', seg_len_bytes)[0]
                        f.seek(seg_len - 2, 1)  # 跳过本段剩余字节

        # GIF: bytes 6-10 为逻辑屏宽高（小端 16 位）
        if head[:6] in (b'GIF87a', b'GIF89a'):
            if len(head) >= 10:
                w = struct.unpack('<H', head[6:8])[0]
                h = struct.unpack('<H', head[8:10])[0]
                return w, h

        # WEBP: RIFF + WebP，按 VP8/VP8L/VP8X chunk 解析
        if head[:4] == b'RIFF' and head[8:12] == b'WEBP':
            chunk = head[12:16]
            if chunk == b'VP8 ' and len(head) >= 26:  # 有损
                w = struct.unpack('<H', head[26:28])[0] & 0x3fff
                h = struct.unpack('<H', head[28:30])[0] & 0x3fff
                return w, h
            if chunk == b'VP8L' and len(head) >= 25:  # 无损
                bits = head[21:25]
                b0 = bits[0]
                w = 1 + (((b0 & 0x3f) << 8) | bits[1])
                h = 1 + (((bits[2] & 0x0f) << 10) | (bits[3] << 2) | (bits[2] >> 6))
                return w, h
            if chunk == b'VP8X' and len(head) >= 30:  # 扩展（含 alpha/动画）
                w = 1 + (head[24] | (head[25] << 8) | (head[26] << 16))
                h = 1 + (head[27] | (head[28] << 8) | (head[29] << 16))
                return w, h
    except Exception:
        pass

    return 0, 0


def _sanitize_rename_name(name):
    """清洗用户输入的 name 部分（用于 {name}-{w}x{h}-编号 模板）。

    移除路径分隔符、控制字符，把 Windows/Linux 文件名非法字符替换为下划线。
    保留中文等任意 Unicode 文本，与 safe_filename 风格一致。
    """
    if name is None:
        return ''
    name = str(name).replace('\x00', '')
    # 替换文件系统非法字符（Windows: \ / : * ? " < > |，Linux/macOS: /）
    for ch in '\\/:*?"<>|':
        name = name.replace(ch, '_')
    return name.strip()


@file_ops_bp.route('/upload', methods=['POST'])
def upload_files():
    """上传文件"""
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
            print(f"[上传] {filename} -> {save_path}")
            file.save(save_path)

    return redirect(url_for('index'))


@file_ops_bp.route('/get_caption/<image_name>')
def get_caption(image_name):
    """获取图片对应的标签，同时从 SQLite 查翻译返回"""
    from translation import _lookup_cn_from_db
    base_name = os.path.splitext(image_name)[0]
    caption_file = f"{base_name}.txt"
    caption_path = os.path.join(current_app.config['UPLOAD_FOLDER'], caption_file)

    caption = ""
    if os.path.exists(caption_path):
        try:
            with open(caption_path, 'r', encoding='utf-8') as f:
                caption = f.read()
        except Exception as e:
            print(f"读取标签文件失败: {str(e)}")

    # 一次性返回标签 + 翻译（从 SQLite cn_name 查）
    tags = [t.strip() for t in caption.split(',') if t.strip()] if caption else []
    hits = _lookup_cn_from_db(tags)
    translations = [hits.get(tag, '') for tag in tags]

    return jsonify({'caption': caption, 'translations': translations})


@file_ops_bp.route('/save_caption/<image_name>', methods=['POST'])
def save_caption(image_name):
    """保存标签到文件。用户编辑的翻译回写到 SQLite cn_name 列"""
    data = request.get_json()
    content = data.get('content', '')
    translations = data.get('translations', {})

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

    base_name = os.path.splitext(image_name)[0]
    caption_file = f"{base_name}.txt"
    caption_path = os.path.join(current_app.config['UPLOAD_FOLDER'], caption_file)

    try:
        with open(caption_path, 'w', encoding='utf-8') as f:
            f.write(content)
    except Exception as e:
        print(f"保存标签文件失败: {str(e)}")
        return jsonify({'success': False, 'error': str(e)})

    # 用户手动编辑的翻译回写 SQLite（覆盖 LLM/Danbooru 的结果）
    if translations:
        try:
            from translation import _get_tag_db_conn
            from build_tag_db import update_translation
            conn = _get_tag_db_conn()
            if conn is not None:
                for tag, tr in translations.items():
                    tag = tag.strip()
                    tr = tr.strip()
                    if tag and tr:
                        update_translation(conn, tag, tr)
        except Exception as e:
            print(f"翻译回写 SQLite 失败: {e}")

    return jsonify({'success': True})


@file_ops_bp.route('/tag_stats')
def tag_stats():
    """统计所有标签出现次数，附翻译（从 SQLite 查）"""
    from collections import Counter
    from translation import _lookup_cn_from_db
    upload_dir = current_app.config['UPLOAD_FOLDER']
    counter = Counter()
    for filename in os.listdir(upload_dir):
        if not filename.endswith('.txt'):
            continue
        with open(os.path.join(upload_dir, filename), 'r', encoding='utf-8') as f:
            content = f.read().strip()
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

    return jsonify({'stats': stats, 'total_files': len([f for f in os.listdir(upload_dir) if f.endswith('.txt')])})


@file_ops_bp.route('/clear_all', methods=['POST'])
def clear_all():
    """清空所有上传的文件"""
    upload_dir = current_app.config['UPLOAD_FOLDER']
    for filename in os.listdir(upload_dir):
        file_path = os.path.join(upload_dir, filename)
        try:
            if os.path.isfile(file_path):
                os.unlink(file_path)
        except Exception as e:
            print(f"删除文件失败: {str(e)}")

    return redirect(url_for('index'))


@file_ops_bp.route('/delete/<image_name>', methods=['POST'])
def delete_image(image_name):
    """删除图片及对应的txt文件"""
    filename = safe_filename(image_name)
    upload_dir = os.path.abspath(current_app.config['UPLOAD_FOLDER'])
    file_path = os.path.abspath(os.path.join(upload_dir, filename))
    if not is_within_directory(file_path, upload_dir):
        return jsonify({'success': False, 'error': '非法路径'}), 400
    if os.path.exists(file_path):
        os.unlink(file_path)
    base_name = os.path.splitext(filename)[0]
    txt_path = os.path.join(upload_dir, f"{base_name}.txt")
    if os.path.exists(txt_path):
        os.unlink(txt_path)
    return jsonify({'success': True})


@file_ops_bp.route('/uploads/<filename>')
def uploaded_file(filename):
    """提供上传文件的访问（带浏览器缓存，减少切换图片时的重复请求）"""
    resp = send_from_directory(current_app.config['UPLOAD_FOLDER'], filename)
    # ETag/Last-Modified 默认存在，文件变更时浏览器会重新拉取；max-age 省去 304 往返
    resp.headers['Cache-Control'] = 'public, max-age=3600'
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
    from config import get_image_files

    data = request.get_json() or {}
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
    # 阶段1：old_base.<图片后缀> 和 old_base.txt → __tageditor_rename_tmp_{i}.<后缀>
    # 阶段2：__tageditor_rename_tmp_{i}.<后缀> → new_base.<后缀>
    # 图片用原图扩展名（保留格式），标签固定 .txt。
    tmp_prefix = '__tageditor_rename_tmp_'
    renamed = 0
    errors = 0
    for i, (old_base, new_base, ext, _, _) in enumerate(plan):
        # 该图的两个待重命名后缀：图片原后缀 + 标签 .txt
        exts = (ext, '.txt')
        # 阶段1：old → tmp
        for e in exts:
            old_path = os.path.join(upload_dir, old_base + e)
            tmp_path = os.path.join(upload_dir, f"{tmp_prefix}{i}{e}")
            if os.path.exists(old_path):
                try:
                    os.rename(old_path, tmp_path)
                except Exception as ex:
                    print(f"[重命名] 阶段1失败 {old_path} -> {tmp_path}: {ex}")
                    errors += 1
        # 阶段2：tmp → new
        moved_any = False
        for e in exts:
            tmp_path = os.path.join(upload_dir, f"{tmp_prefix}{i}{e}")
            new_path = os.path.join(upload_dir, new_base + e)
            if os.path.exists(tmp_path):
                try:
                    # 防御：若 new_path 已被别的源占用（不该发生），先清理
                    if os.path.abspath(tmp_path) != os.path.abspath(new_path) and os.path.exists(new_path):
                        os.unlink(new_path)
                    os.rename(tmp_path, new_path)
                    moved_any = True
                except Exception as ex:
                    print(f"[重命名] 阶段2失败 {tmp_path} -> {new_path}: {ex}")
                    errors += 1
        if moved_any:
            renamed += 1

    return jsonify({'renamed': renamed, 'total': len(plan), 'errors': errors})
