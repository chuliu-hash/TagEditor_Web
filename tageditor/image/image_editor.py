# -*- coding: utf-8 -*-
import os
import numpy as np
from io import BytesIO
from flask import Blueprint, request, jsonify, Response, current_app, send_file, stream_with_context
from tageditor.core.config import (safe_filename, is_within_directory, get_realesrgan_config,
                                   get_birefnet_config, get_sam2_config)
from tageditor.core.sse_utils import sse_event
import logging


log = logging.getLogger(__name__)

image_editor_bp = Blueprint('image_editor', __name__)

# Real-ESRGAN upsampler 缓存（首次加载后常驻内存，与 _wd14_model_cache 同理）。
# 按 model_path + tile 作为 key 失效，避免热加载 .env 切换模型后仍用旧实例。
_realesrgan_cache = {'upsampler': None, 'model_key': None}


def _write_image_atomic(path, img, compression=3):
    """原子写图片：按**目标扩展名**编码到内存 → 唯一临时文件 → os.replace。

    三处都与旧实现不同，每一处都对应一个实测过的问题：

    1) **不用 cv2.imwrite(目标)**：它的编码器按扩展名推断，写 `x.png.tmp` 会直接失败，
       没法做临时文件；而且直接覆写目标时若中途失败（磁盘满/进程被杀），原图已被截断。
    2) **临时名唯一 + 按目标路径加锁**：旧实现是 `path + '.tmp'` 固定名且无锁 —— 与
       `config.write_text_atomic` 注释里记的那个坑完全同形（文本侧实测「4 线程写同一
       文件 120 次失败 14 次」）。两个请求（双击保存，或批量转色底正跑着时又保存同一张图）
       会共用一个 .tmp 互相截断 → **用户原图被覆盖成半截 PNG**。现在复用
       `config.write_bytes_atomic` 的两件套。
    3) **按扩展名编码**：旧实现无论原名是什么都 `imencode('.png')`，于是
       `/batch_alpha_to_white` 把 PNG 字节写进了 `x.jpg`，而 `send_from_directory`
       仍按扩展名声明 `Content-Type: image/jpeg`。浏览器靠内容嗅探多半还能显示，
       但依赖扩展名的下游（训练脚本按后缀选解码器、图库做格式校验）会误判，
       体积语义也变了。

    返回 True/False（与 cv2.imwrite 口径一致）。
    """
    import cv2
    ext = os.path.splitext(path)[1].lower()
    params = []
    if ext in ('.jpg', '.jpeg'):
        enc = '.jpg'
        params = [cv2.IMWRITE_JPEG_QUALITY, 95]
    elif ext == '.webp':
        enc = '.webp'
    else:
        enc = '.png'
        params = [cv2.IMWRITE_PNG_COMPRESSION, compression]
    ok, buf = cv2.imencode(enc, img, params)
    if not ok:
        return False
    from tageditor.core.config import write_bytes_atomic
    write_bytes_atomic(path, buf.tobytes())
    return True


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

    # 已有同名 .png 时直接覆写，不要先删——删了再写失败就是「原图没了、新图也没成」。
    # 仅当「源文件名 ≠ 目标文件名」（扩展名变化，如 .jpg → .png）时才在**写成功之后**删除旧文件。
    is_same_path = os.path.abspath(os.path.join(upload_dir, filename)) == os.path.abspath(save_path)

    try:
        ret = _write_image_atomic(save_path, img, compression=3)
        if not ret:
            return jsonify({'success': False, 'error': '图片写入失败（编码失败）'}), 500
    except Exception as e:
        return jsonify({'success': False, 'error': f'保存失败: {str(e)}'}), 500

    # 新图已安全落盘，此时删除扩展名不同的旧文件才是安全的
    if not is_same_path and original_ext != '.png':
        try:
            os.unlink(os.path.join(upload_dir, filename))
        except OSError:
            pass

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
            from tageditor.core.image_io import imread_any
            img = imread_any(fpath, cv2.IMREAD_UNCHANGED)
            if img is None:
                return jsonify({'success': False, 'error': '无法读取图片'}), 400
            if not (img.ndim == 3 and img.shape[2] == 4):
                return jsonify({'success': True, 'message': 'no_alpha'})
            alpha = img[:, :, 3:4] / 255.0
            result = (img[:, :, :3] * alpha + bg_rgb * (1.0 - alpha)).astype(np.uint8)
            if not _write_image_atomic(fpath, result):
                return jsonify({'success': False, 'error': '图片写入失败'}), 500
            return jsonify({'success': True, 'converted': 1, 'item': filename})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500

    # 全部处理：先筛选有 alpha 通道的图片。
    #
    # **只看文件头，不整图解码**：旧实现为判断 `shape[2] == 4` 对每张图
    # `cv2.imread(IMREAD_UNCHANGED)` 整图解码一遍，之后 generator 里再解一遍 ——
    # 几千张图的目录里，用户点下去后第一个 SSE 字节要等「全库解码一遍」（分钟级），
    # 期间界面只有「正在扫描图片...」，看起来像卡死；CPU/IO 还翻倍。
    # has_alpha 三态：True / False / None（判不了）—— None 时退回解码判定，
    # 不能当 False 跳过，否则真有透明通道的图会被静默漏掉（「完成，转换 0 张」）。
    from tageditor.core.config import get_image_files
    from tageditor.core.image_io import has_alpha, imread_any
    images = get_image_files(upload_dir)
    alpha_images = []
    for fname in images:
        ext = os.path.splitext(fname)[1].lstrip('.').lower()
        if ext == 'gif':
            continue
        fpath = os.path.join(upload_dir, fname)
        known = has_alpha(fpath)
        if known is True:
            alpha_images.append((fname, fpath))
        elif known is None:
            img = imread_any(fpath, cv2.IMREAD_UNCHANGED)
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
                    img = imread_any(fpath, cv2.IMREAD_UNCHANGED)
                    if img is None:
                        errors += 1
                        yield sse_event('error', {'item': fname, 'error': '无法读取图片'})
                        continue
                    # 透明像素与底色按 alpha 混合（bg_rgb 在路由层解析，闭包捕获）
                    alpha = img[:, :, 3:4] / 255.0
                    result = img[:, :, :3] * alpha + bg_rgb * (1.0 - alpha)
                    result = result.astype(np.uint8)

                    if not _write_image_atomic(fpath, result):
                        raise RuntimeError('图片写入失败 (编码失败)')
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

    return Response(stream_with_context(generate()), mimetype='text/event-stream',
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
    from tageditor.image.realesrgan_utils import RealESRGANer
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
    log.info(f"[RealESRGAN] 模型加载完成: {cfg['model_path']}, tile={cfg['tile']}, "
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
    # imread_any：cv2.imread 读不了中文路径（本项目刻意保留中文文件名）
    from tageditor.core.image_io import imread_any
    img = imread_any(fpath, cv2.IMREAD_UNCHANGED)
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
    from tageditor.image.birefnet_utils import load_birefnet_model
    return load_birefnet_model(cfg['base_model_dir'], cfg['toonout_weights'])


def _resolve_upload_image(target):
    """把 query 里的 target 解析为 (filename, abs_path, img_bgr)，失败返回 (None, None, None)。

    单张图片路由的公共前置：GIF 拒绝 → safe_filename → 目录校验 → imread_any →
    统一成 3 通道 BGR。抽出来是因为 SAM2 的两个路由与 /remove_background 都要它，
    各写一遍迟早有一处漏掉 `is_within_directory`（那是路径校验的防线）。
    """
    import cv2
    target = (target or '').strip()
    if not target:
        return None, None, None
    filename = safe_filename(target)
    if os.path.splitext(filename)[1].lstrip('.').lower() == 'gif':
        return None, None, None
    upload_dir = os.path.abspath(current_app.config['UPLOAD_FOLDER'])
    fpath = os.path.abspath(os.path.join(upload_dir, filename))
    if not is_within_directory(fpath, upload_dir) or not os.path.isfile(fpath):
        return None, None, None
    from tageditor.core.image_io import imread_any
    img = imread_any(fpath, cv2.IMREAD_UNCHANGED)
    if img is None:
        return None, None, None
    # 统一为 3 通道 BGR（BiRefNet / SAM2 都只处理 RGB 内容，alpha 在此丢弃）
    if img.ndim == 2:
        img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    elif img.ndim == 3 and img.shape[2] == 4:
        img = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
    return filename, fpath, img


def _load_sam2(cfg):
    """加载 SAM2 predictor 并把当前图片送入（embedding 按文件 mtime 缓存）。"""
    from tageditor.image.sam2_utils import load_sam2_model, set_image
    predictor = load_sam2_model(cfg['config'], cfg['checkpoint'])
    return predictor


def _sam2_image_key(fpath):
    """embedding 缓存键：(路径, mtime_ns, 大小)。保存覆盖后 mtime 变 → 自动失效。"""
    try:
        st = os.stat(fpath)
        return (os.path.abspath(fpath), st.st_mtime_ns, st.st_size)
    except OSError:
        return None


@image_editor_bp.route('/sam2_load', methods=['POST'])
def sam2_load():
    """进入描点模式：加载 SAM2 + 把当前图片的 embedding 算好（缓存）。

    query: ?target=<filename>
    返回 {success, width, height}；SAM2 未安装或权重缺失时返回 400 + 明确文案
    （前端据此把「SAM2 描点」选项置灰——不能只让按钮点了没反应）。
    """
    cfg = get_sam2_config()
    if not os.path.isfile(cfg['checkpoint']):
        return jsonify({'success': False,
                        'error': f"SAM2 权重不存在：{cfg['checkpoint']}\n"
                                 f"下载 https://dl.fbaipublicfiles.com/segment_anything_2/092824/"
                                 f"sam2.1_hiera_base_plus.pt 放到 models/"}), 400

    filename, fpath, img = _resolve_upload_image(request.args.get('target'))
    if img is None:
        return jsonify({'success': False, 'error': '文件不存在或非法路径'}), 400

    try:
        predictor = _load_sam2(cfg)
        from tageditor.image.sam2_utils import set_image
        set_image(predictor, img, cache_key=_sam2_image_key(fpath))
    except ImportError as e:
        return jsonify({'success': False,
                        'error': f'SAM2 未安装（pip install sam2）：{e}'}), 400
    except Exception as e:
        log.error('[SAM2] 加载失败: %s', e)
        return jsonify({'success': False, 'error': f'SAM2 加载失败: {e}'}), 500

    return jsonify({'success': True,
                    'width': int(img.shape[1]), 'height': int(img.shape[0]),
                    'multimask': True})


@image_editor_bp.route('/sam2_predict', methods=['POST'])
def sam2_predict():
    """按锚点预测 mask，返回 PNG 灰度图（255 = 前景）供前端叠加预览。

    body JSON: {target, points: [[x,y],...], labels: [1,0,...], index: 0}
      index: multimask 候选下标（0 = score 最高的那个，默认）
    成功返回 image/png 二进制；失败返回 JSON（前端按 Content-Type 分流）。

    **只收点坐标，不收 mask**：mask 是数 MB 的图，传给前端再传回来既慢又给了
    「前后端各算一份、两边不一致」的空间；而点坐标只有几十字节，解码只要 ~30ms。
    """
    from tageditor.image.sam2_utils import predict_mask, mask_to_png_bytes

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({'success': False, 'error': '请求体必须是 JSON 对象'}), 400
    points = data.get('points')
    labels = data.get('labels')
    if not isinstance(points, list) or not points:
        return jsonify({'success': False, 'error': '缺少 points'}), 400
    if not isinstance(labels, list) or len(labels) != len(points):
        return jsonify({'success': False, 'error': 'labels 必须与 points 等长'}), 400

    cfg = get_sam2_config()
    filename, fpath, img = _resolve_upload_image(data.get('target'))
    if img is None:
        return jsonify({'success': False, 'error': '文件不存在或非法路径'}), 400

    try:
        predictor = _load_sam2(cfg)
        from tageditor.image.sam2_utils import set_image
        set_image(predictor, img, cache_key=_sam2_image_key(fpath))
        cands = predict_mask(predictor, points, labels)
    except ImportError as e:
        return jsonify({'success': False, 'error': f'SAM2 未安装：{e}'}), 400
    except Exception as e:
        log.error('[SAM2] 预测失败: %s', e)
        return jsonify({'success': False, 'error': f'SAM2 预测失败: {e}'}), 500

    if not cands:
        return jsonify({'success': False, 'error': '未预测出掩码（试试换个位置点）'}), 400

    idx = data.get('index', 0)
    try:
        idx = max(0, min(int(idx), len(cands) - 1))
    except (TypeError, ValueError):
        idx = 0
    mask, score = cands[idx]
    png = mask_to_png_bytes(mask)
    return send_file(BytesIO(png), mimetype='image/png',
                     download_name='sam2_mask.png')


@image_editor_bp.route('/remove_background', methods=['POST'])
def remove_background():
    """BiRefNet（ToonOut）背景移除（单张）。

    query: ?target=<filename>&bg_color=<hex|transparent>
           &gate_mode=off|include|exclude&points=<json>&labels=<json>
           &gate_index=<int>
      gate_mode != off 时为 SAM2 门控模式（描点选目标）：
      服务端用 points/labels **重新预测** mask（不信任前端传来的 mask），
      再门控到 ToonOut 的 alpha 上。gate_mode=off 时行为与旧路径**逐像素一致**。

    bg_color=transparent 或缺省：输出透明背景 RGBA PNG。
    bg_color=<hex>（如 #ffffff）：前景与该底色混合，输出 RGB PNG。
    成功返回 PNG 二进制（image/png），失败返回 JSON。前端按 Content-Type 区分。
    后端不写盘——落盘交给既有 /process_image（保存）流程。
    """
    import cv2
    import json as _json

    filename, fpath, img = _resolve_upload_image(request.args.get('target'))
    if img is None:
        return jsonify({'success': False, 'error': '文件不存在或非法路径'}), 400

    # 解析输出模式：transparent 透明，否则 hex 底色
    bg_raw = (request.args.get('bg_color') or 'transparent').strip()
    if bg_raw.lower() == 'transparent':
        bg_color = None  # 透明背景
    else:
        bg_color = _parse_bg_color(bg_raw)  # (R,G,B) float32

    # SAM2 门控（可选）：gate_mode=off（默认）时 gate_mask 保持 None，
    # birefnet_utils.remove_background 走与原实现完全相同的分支。
    gate_mode = (request.args.get('gate_mode') or 'off').strip().lower()
    if gate_mode not in ('off', 'include', 'exclude'):
        return jsonify({'success': False, 'error': "gate_mode 只能是 off/include/exclude"}), 400

    gate_mask = None
    cfg_sam2 = get_sam2_config()
    if gate_mode != 'off':
        try:
            points = _json.loads(request.args.get('points') or '[]')
            labels = _json.loads(request.args.get('labels') or '[]')
        except ValueError as e:
            return jsonify({'success': False, 'error': f'points/labels 不是合法 JSON：{e}'}), 400
        if not points:
            return jsonify({'success': False, 'error': '门控模式必须提供 points'}), 400
        try:
            from tageditor.image.sam2_utils import predict_mask, set_image
            predictor = _load_sam2(cfg_sam2)
            set_image(predictor, img, cache_key=_sam2_image_key(fpath))
            cands = predict_mask(predictor, points, labels)
        except ImportError as e:
            return jsonify({'success': False, 'error': f'SAM2 未安装：{e}'}), 400
        except Exception as e:
            log.error('[SAM2] 门控预测失败: %s', e)
            return jsonify({'success': False, 'error': f'SAM2 门控预测失败: {e}'}), 500
        if not cands:
            return jsonify({'success': False, 'error': '未预测出掩码，无法门控'}), 400
        try:
            gi = max(0, min(int(request.args.get('gate_index', 0)), len(cands) - 1))
        except (TypeError, ValueError):
            gi = 0
        gate_mask = cands[gi][0]

    # 加载模型（带缓存）
    cfg = get_birefnet_config()
    try:
        model = _load_birefnet_model(cfg)
    except Exception as e:
        return jsonify({'success': False, 'error': f'模型加载失败: {str(e)}'}), 500

    # 背景移除推理（gate_mask 为 None 时 = 旧路径）
    try:
        from tageditor.image.birefnet_utils import remove_background as _remove_bg
        output = _remove_bg(model, img, bg_color=bg_color, gate_mask=gate_mask,
                            gate_mode=gate_mode if gate_mask is not None else 'include',
                            dilate_px=cfg_sam2['gate_dilate_px'],
                            feather_px=cfg_sam2['gate_feather_px'])
    except Exception as e:
        log.error('[背景移除] 推理失败: %s', e)
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
