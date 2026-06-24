# -*- coding: utf-8 -*-
import os
import numpy as np
from io import BytesIO
from flask import Blueprint, request, jsonify, Response, current_app, send_file
from config import safe_filename, is_within_directory, get_realesrgan_config, get_birefnet_config
from sse_utils import sse_event

image_editor_bp = Blueprint('image_editor', __name__)

# Real-ESRGAN upsampler 缓存（首次加载后常驻内存，与 _wd14_model_cache 同理）。
# 按 model_path + tile 作为 key 失效，避免热加载 .env 切换模型后仍用旧实例。
_realesrgan_cache = {'upsampler': None, 'model_key': None}


def _parse_bg_color(raw):
    """解析 hex 颜色字符串 -> (R,G,B) float32 数组，非法或缺失返回白色。

    支持 #rgb / #rrggbb / rrggbb 形式，用于透明转色底的 alpha 混合。
    """
    white = np.array([255.0, 255.0, 255.0], dtype=np.float32)
    if not raw:
        return white
    s = raw.strip().lstrip('#')
    if len(s) == 3:  # #rgb 简写展开
        s = ''.join(c * 2 for c in s)
    if len(s) != 6:
        return white
    try:
        r, g, b = int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)
        return np.array([float(r), float(g), float(b)], dtype=np.float32)
    except ValueError:
        return white


@image_editor_bp.route('/process_image', methods=['POST'])
def process_image():
    """保存编辑后的图片（覆盖原图），前端 multipart 直传二进制"""
    filename = request.form.get('filename', '')
    file = request.files.get('image')

    if not filename or not file:
        return jsonify({'success': False, 'error': '参数缺失'}), 400

    # 不支持 GIF
    ext = os.path.splitext(filename)[1].lstrip('.').lower()
    if ext == 'gif':
        return jsonify({'success': False, 'error': '不支持编辑 GIF 图片'}), 400

    filename = safe_filename(filename)
    upload_dir = os.path.abspath(current_app.config['UPLOAD_FOLDER'])
    file_path = os.path.abspath(os.path.join(upload_dir, filename))
    if not is_within_directory(file_path, upload_dir):
        return jsonify({'success': False, 'error': '非法路径'}), 400

    # 读取上传的二进制
    img_bytes = file.read()

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
    """透明转色底：?color=hex 自定义底色；?target=<文件名> 仅处理单张（返回 JSON），
    缺省或 ?target=all 处理全部含 alpha 图片（SSE 流式）"""
    import cv2
    upload_dir = os.path.abspath(current_app.config['UPLOAD_FOLDER'])
    bg_rgb = _parse_bg_color(request.args.get('color'))
    target = (request.args.get('target') or 'all').strip()

    # 单张处理：直接转色并返回 JSON
    if target and target != 'all':
        filename = safe_filename(target)
        fpath = os.path.abspath(os.path.join(upload_dir, filename))
        if not is_within_directory(fpath, upload_dir) or not os.path.isfile(fpath):
            return jsonify({'success': False, 'error': '文件不存在或非法路径'}), 400
        if os.path.splitext(filename)[1].lstrip('.').lower() == 'gif':
            return jsonify({'success': False, 'error': '不支持 GIF 图片'}), 400
        try:
            img = cv2.imread(fpath, cv2.IMREAD_UNCHANGED)
            if img is None:
                return jsonify({'success': False, 'error': '无法读取图片'}), 400
            if not (img.ndim == 3 and img.shape[2] == 4):
                return jsonify({'success': True, 'message': 'no_alpha'})
            alpha = img[:, :, 3:4] / 255.0
            result = (img[:, :, :3] * alpha + bg_rgb * (1.0 - alpha)).astype(np.uint8)
            cv2.imwrite(fpath, result, [cv2.IMWRITE_PNG_COMPRESSION, 3])
            return jsonify({'success': True, 'converted': 1, 'item': filename})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500

    # 全部处理：先筛选有 alpha 通道的图片（只存文件名，避免内存占用）
    from config import get_image_files
    images = get_image_files(upload_dir)
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
        try:
            # 先发一个总数预告事件（current:0）：让前端立即显示「0 / N」与预估总数，
            # 而非停留在「正在扫描图片...」无进度信息。扫描已在路由层完成，此处 total 已知。
            yield sse_event('progress', {'current': 0, 'total': total, 'item': '准备开始转换 ' + str(total) + ' 张图片...'})
            for i, (fname, fpath) in enumerate(alpha_images):
                try:
                    img = cv2.imread(fpath, cv2.IMREAD_UNCHANGED)
                    if img is None:
                        errors += 1
                        yield sse_event('error', {'item': fname, 'error': '无法读取图片'})
                        continue
                    # 透明像素与底色按 alpha 混合（bg_rgb 在路由层解析，闭包捕获）
                    alpha = img[:, :, 3:4] / 255.0
                    result = img[:, :, :3] * alpha + bg_rgb * (1.0 - alpha)
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
        except Exception as e:
            # 生成器级别的未预期异常：发 fatal，前端能正常收尾
            yield sse_event('fatal', {'error': f'批量转换异常终止: {e}'})

    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


def _load_realesrgan_upsampler(cfg):
    """加载 Real-ESRGAN upsampler，结果缓存到模块级全局变量。

    重依赖（torch/basicsr）在此懒加载，避免缺失依赖时整个 blueprint 加载失败。
    RealESRGANer 类已集成进本项目 realesrgan_utils.py；RRDBNet 网络结构定义在 basicsr 包内。
    CPU 环境强制 fp32（否则报 slow_conv2d_cpu not implemented for 'Half'）。
    固定使用 anime_6B 模型结构（6 个 RRDB 残差块，4x 放大）。
    """
    global _realesrgan_cache
    model_key = (cfg['model_path'], cfg['tile'])
    if _realesrgan_cache['upsampler'] is not None and _realesrgan_cache['model_key'] == model_key:
        return _realesrgan_cache['upsampler']

    import torch
    # 先 import realesrgan_utils：它会注入 torchvision.transforms.functional_tensor
    # 兼容垫片，使后续 import basicsr 不报 ModuleNotFoundError（basicsr 1.4.2 兼容性问题）
    from realesrgan_utils import RealESRGANer
    from basicsr.archs.rrdbnet_arch import RRDBNet

    if not os.path.isfile(cfg['model_path']):
        raise FileNotFoundError(f"模型文件不存在: {cfg['model_path']}")

    # anime_6B: 6 个 RRDB 残差块，4x 放大，针对动漫图像优化
    model = RRDBNet(num_in_ch=3, num_out_ch=3, num_feat=64,
                    num_block=6, num_grow_ch=32, scale=4)
    half = torch.cuda.is_available()  # CPU 必须 fp32
    upsampler = RealESRGANer(
        scale=4,
        model_path=cfg['model_path'],
        model=model,
        tile=cfg['tile'],
        tile_pad=cfg['tile_pad'],
        half=half,
    )

    _realesrgan_cache = {'upsampler': upsampler, 'model_key': model_key}
    print(f"[RealESRGAN] 模型加载完成: {cfg['model_path']}, tile={cfg['tile']}, "
          f"half={half}, device={upsampler.device}")
    return upsampler


@image_editor_bp.route('/upscale_realesrgan', methods=['POST'])
def upscale_realesrgan():
    """Real-ESRGAN 超清放大（单张）。

    query: ?target=<filename>&w=<int>&h=<int>
    固定 anime_6B 4x 超分，再缩放到 (w,h)。w/h 必须在 [原图, 原图*4] 范围内。
    成功返回 PNG 二进制（image/png），失败返回 JSON。
    前端按 Content-Type 区分。后端不写盘——落盘交给既有 /process_image（保存）流程。
    """
    import cv2

    target = (request.args.get('target') or '').strip()
    if not target:
        return jsonify({'success': False, 'error': '缺少 target 参数'}), 400

    # 尺寸参数解析与范围校验
    try:
        w = int(request.args.get('w', '0'))
        h = int(request.args.get('h', '0'))
    except ValueError:
        return jsonify({'success': False, 'error': 'w/h 必须是整数'}), 400
    if w <= 0 or h <= 0:
        return jsonify({'success': False, 'error': 'w/h 必须为正整数'}), 400

    filename = safe_filename(target)
    ext = os.path.splitext(filename)[1].lstrip('.').lower()
    if ext == 'gif':
        return jsonify({'success': False, 'error': '不支持 GIF 图片'}), 400

    upload_dir = os.path.abspath(current_app.config['UPLOAD_FOLDER'])
    fpath = os.path.abspath(os.path.join(upload_dir, filename))
    if not is_within_directory(fpath, upload_dir) or not os.path.isfile(fpath):
        return jsonify({'success': False, 'error': '文件不存在或非法路径'}), 400

    # 读取原图，校验目标尺寸范围 [原图, 原图*4]
    img = cv2.imread(fpath, cv2.IMREAD_UNCHANGED)
    if img is None:
        return jsonify({'success': False, 'error': '无法读取图片'}), 400
    oh, ow = img.shape[:2]
    if not (ow <= w <= ow * 4) or not (oh <= h <= oh * 4):
        return jsonify({'success': False, 'error': f'目标尺寸必须在 [{ow}x{oh}] 到 [{ow*4}x{oh*4}] 之间'}), 400

    # 加载模型（带缓存）
    cfg = get_realesrgan_config()
    try:
        upsampler = _load_realesrgan_upsampler(cfg)
    except Exception as e:
        return jsonify({'success': False, 'error': f'模型加载失败: {str(e)}'}), 500

    # 4x 超分
    try:
        output, _ = upsampler.enhance(img, outscale=4)
    except Exception as e:
        return jsonify({'success': False, 'error': f'超分推理失败: {str(e)}'}), 500

    # 若目标尺寸 ≠ 4x，用 LANCZOS4 缩回（高质量下采样）
    if output.shape[:2] != (h, w):
        output = cv2.resize(output, (w, h), interpolation=cv2.INTER_LANCZOS4)

    # 编码为 PNG 二进制返回（不写盘）
    try:
        ok, buf = cv2.imencode('.png', output, [cv2.IMWRITE_PNG_COMPRESSION, 3])
        if not ok:
            return jsonify({'success': False, 'error': 'PNG 编码失败'}), 500
    except Exception as e:
        return jsonify({'success': False, 'error': f'PNG 编码失败: {str(e)}'}), 500

    return send_file(BytesIO(buf.tobytes()), mimetype='image/png',
                     download_name=os.path.splitext(filename)[0] + '_upscaled.png')


def _load_birefnet_model(cfg):
    """加载 BiRefNet（ToonOut 权重），结果缓存到模块级全局变量。
    重依赖（torch/transformers）在 birefnet_utils 内懒加载。"""
    from birefnet_utils import load_birefnet_model
    return load_birefnet_model(cfg['base_model_dir'], cfg['toonout_weights'])


@image_editor_bp.route('/remove_background', methods=['POST'])
def remove_background():
    """BiRefNet（ToonOut）背景移除（单张）。

    query: ?target=<filename>&bg_color=<hex|transparent>
    bg_color=transparent 或缺省：输出透明背景 RGBA PNG。
    bg_color=<hex>（如 #ffffff）：前景与该底色混合，输出 RGB PNG。
    成功返回 PNG 二进制（image/png），失败返回 JSON。前端按 Content-Type 区分。
    后端不写盘——落盘交给既有 /process_image（保存）流程。
    """
    import cv2

    target = (request.args.get('target') or '').strip()
    if not target:
        return jsonify({'success': False, 'error': '缺少 target 参数'}), 400

    filename = safe_filename(target)
    ext = os.path.splitext(filename)[1].lstrip('.').lower()
    if ext == 'gif':
        return jsonify({'success': False, 'error': '不支持 GIF 图片'}), 400

    upload_dir = os.path.abspath(current_app.config['UPLOAD_FOLDER'])
    fpath = os.path.abspath(os.path.join(upload_dir, filename))
    if not is_within_directory(fpath, upload_dir) or not os.path.isfile(fpath):
        return jsonify({'success': False, 'error': '文件不存在或非法路径'}), 400

    # 解析输出模式：transparent 透明，否则 hex 底色
    bg_raw = (request.args.get('bg_color') or 'transparent').strip()
    if bg_raw.lower() == 'transparent':
        bg_color = None  # 透明背景
    else:
        bg_color = _parse_bg_color(bg_raw)  # (R,G,B) float32

    # 读取原图
    img = cv2.imread(fpath, cv2.IMREAD_UNCHANGED)
    if img is None:
        return jsonify({'success': False, 'error': '无法读取图片'}), 400
    # 统一为 3 通道 BGR（BiRefNet 只处理 RGB 内容，alpha 通道在此丢弃）
    if img.ndim == 2:
        img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    elif img.ndim == 3 and img.shape[2] == 4:
        img = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)

    # 加载模型（带缓存）
    cfg = get_birefnet_config()
    try:
        model = _load_birefnet_model(cfg)
    except Exception as e:
        return jsonify({'success': False, 'error': f'模型加载失败: {str(e)}'}), 500

    # 背景移除推理
    try:
        from birefnet_utils import remove_background as _remove_bg
        output = _remove_bg(model, img, bg_color=bg_color)
    except Exception as e:
        return jsonify({'success': False, 'error': f'背景移除失败: {str(e)}'}), 500

    # 编码为 PNG 二进制返回（不写盘）
    try:
        ok, buf = cv2.imencode('.png', output, [cv2.IMWRITE_PNG_COMPRESSION, 3])
        if not ok:
            return jsonify({'success': False, 'error': 'PNG 编码失败'}), 500
    except Exception as e:
        return jsonify({'success': False, 'error': f'PNG 编码失败: {str(e)}'}), 500

    return send_file(BytesIO(buf.tobytes()), mimetype='image/png',
                     download_name=os.path.splitext(filename)[0] + '_nobg.png')
