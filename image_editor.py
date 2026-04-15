# -*- coding: utf-8 -*-
import os
import base64
import numpy as np
from flask import Blueprint, request, jsonify, Response, current_app
from config import safe_filename
from sse_utils import sse_event

image_editor_bp = Blueprint('image_editor', __name__)


@image_editor_bp.route('/process_image', methods=['POST'])
def process_image():
    """保存编辑后的图片（覆盖原图），前端传 base64 图片数据"""
    data = request.get_json()
    filename = data.get('filename', '')
    image_data = data.get('image_data', '')

    if not filename or not image_data:
        return jsonify({'success': False, 'error': '参数缺失'}), 400

    # 不支持 GIF
    ext = os.path.splitext(filename)[1].lstrip('.').lower()
    if ext == 'gif':
        return jsonify({'success': False, 'error': '不支持编辑 GIF 图片'}), 400

    filename = safe_filename(filename)
    upload_dir = os.path.abspath(current_app.config['UPLOAD_FOLDER'])
    file_path = os.path.abspath(os.path.join(upload_dir, filename))
    if not file_path.startswith(upload_dir):
        return jsonify({'success': False, 'error': '非法路径'}), 400

    # 解析 base64
    try:
        if ',' in image_data:
            image_data = image_data.split(',', 1)[1]
        img_bytes = base64.b64decode(image_data)
    except Exception:
        return jsonify({'success': False, 'error': '图片数据解析失败'}), 400

    # 解码图片
    import cv2
    try:
        nparr = np.frombuffer(img_bytes, np.uint8)
        img = cv2.imdecode(nparr, cv2.IMREAD_UNCHANGED)
        if img is None:
            return jsonify({'success': False, 'error': '图片解码失败'}), 400
    except Exception as e:
        return jsonify({'success': False, 'error': f'图片处理失败: {str(e)}'}), 500

    # 写入文件（始终保存为 PNG）
    original_ext = os.path.splitext(filename)[1].lower()
    new_filename = os.path.splitext(filename)[0] + '.png'
    save_path = os.path.join(upload_dir, new_filename)

    # 扩展名变化时删除旧文件
    if original_ext != '.png':
        old_path = os.path.join(upload_dir, filename)
        if os.path.exists(old_path) and os.path.abspath(old_path) != os.path.abspath(save_path):
            os.unlink(old_path)

    try:
        cv2.imwrite(save_path, img, [cv2.IMWRITE_PNG_COMPRESSION, 3])
    except Exception as e:
        return jsonify({'success': False, 'error': f'保存失败: {str(e)}'}), 500

    return jsonify({'success': True, 'filename': new_filename,
                    'width': img.shape[1], 'height': img.shape[0]})


@image_editor_bp.route('/batch_alpha_to_white', methods=['POST'])
def batch_alpha_to_white():
    """批量将文件夹中所有透明背景图片转为白底"""
    import cv2
    upload_dir = os.path.abspath(current_app.config['UPLOAD_FOLDER'])
    from config import get_image_files
    images = get_image_files(upload_dir)

    # 先筛选有 alpha 通道的图片（只存文件名，避免内存占用）
    alpha_images = []
    for fname in images:
        ext = os.path.splitext(fname)[1].lstrip('.').lower()
        if ext == 'gif':
            continue
        fpath = os.path.join(upload_dir, fname)
        img = cv2.imread(fpath, cv2.IMREAD_UNCHANGED)
        if img is not None and img.ndim == 3 and img.shape[2] == 4:
            alpha_images.append((fname, fpath))

    if not alpha_images:
        return jsonify({'success': True, 'message': 'no_alpha', 'converted': 0, 'skipped': len(images)})

    def generate():
        converted = 0
        errors = 0
        total = len(alpha_images)
        for i, (fname, fpath) in enumerate(alpha_images):
            try:
                img = cv2.imread(fpath, cv2.IMREAD_UNCHANGED)
                if img is None:
                    errors += 1
                    yield sse_event('error', {'item': fname, 'error': '无法读取图片'})
                    continue
                # 透明转白底
                alpha = img[:, :, 3:4] / 255.0
                result = img[:, :, :3] * alpha + 255.0 * (1.0 - alpha)
                result = result.astype(np.uint8)

                cv2.imwrite(fpath, result, [cv2.IMWRITE_PNG_COMPRESSION, 3])
                converted += 1
                yield sse_event('progress', {
                    'current': i + 1, 'total': total, 'item': fname
                })
            except Exception as e:
                errors += 1
                yield sse_event('error', {
                    'item': fname, 'error': str(e)
                })

        yield sse_event('complete', {
            'converted': converted, 'skipped': len(images) - total, 'errors': errors
        })

    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})
