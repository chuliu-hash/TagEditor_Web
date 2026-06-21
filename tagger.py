# -*- coding: utf-8 -*-
import os
import json
import base64
import numpy as np
import requests
from flask import Blueprint, request, jsonify, current_app, Response
from config import get_vision_config, get_wd14_config, get_image_files
from sse_utils import sse_event

tagger_bp = Blueprint('tagger', __name__)

# WD14 模型缓存（首次加载后常驻内存）。只读缓存，无并发一致性问题；
# 但多进程部署时每个 worker 会各自加载一份模型到显存，建议单 worker 运行。
_wd14_model_cache = {'session': None, 'label_df': None, 'model_name': None}

# WD14 批量推理的 batch 大小：攒多张一次 session.run，显著减少单张推理的固定开销
WD14_BATCH_SIZE = 8


def wd14_preprocess_image(image_path):
    """WD14 图像预处理：读取 → RGBA转白底BGR → 填充正方形 → 缩放448x448 → float32"""
    import cv2
    img = cv2.imread(image_path, cv2.IMREAD_UNCHANGED)
    if img is None:
        raise ValueError(f"无法读取图像: {image_path}")

    if img.ndim == 3 and img.shape[2] == 4:
        alpha = img[:, :, 3:4] / 255.0
        img = img[:, :, :3] * alpha + 255.0 * (1.0 - alpha)
        img = img.astype(np.uint8)
    elif img.ndim == 2:
        img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)

    h, w = img.shape[:2]
    size = max(h, w, 448)
    pad_h = (size - h) // 2
    pad_w = (size - w) // 2
    canvas = np.ones((size, size, 3), dtype=np.uint8) * 255
    canvas[pad_h:pad_h+h, pad_w:pad_w+w] = img

    interp = cv2.INTER_AREA if size > 448 else cv2.INTER_CUBIC
    canvas = cv2.resize(canvas, (448, 448), interpolation=interp)
    return canvas.astype(np.float32)[np.newaxis, ...]


def wd14_load_model(model_path):
    """加载本地 WD14 ONNX 模型和标签文件，结果缓存到全局变量"""
    global _wd14_model_cache
    if _wd14_model_cache['session'] is not None and _wd14_model_cache['model_name'] == model_path:
        return _wd14_model_cache

    import onnxruntime
    import pandas as pd

    onnx_path = os.path.join(model_path, 'model.onnx')
    csv_path = os.path.join(model_path, 'selected_tags.csv')
    if not os.path.exists(onnx_path):
        raise FileNotFoundError(f"模型文件不存在: {onnx_path}")
    if not os.path.exists(csv_path):
        raise FileNotFoundError(f"标签文件不存在: {csv_path}")

    providers = ['CUDAExecutionProvider', 'CPUExecutionProvider']
    session = onnxruntime.InferenceSession(onnx_path, providers=providers)
    label_df = pd.read_csv(csv_path)

    _wd14_model_cache = {'session': session, 'label_df': label_df, 'model_name': model_path}
    print(f"[WD14] 模型加载完成: {model_path}, providers: {session.get_providers()}")
    return _wd14_model_cache


def wd14_filter_tags(label_df, probabilities, general_threshold, character_threshold, excluded_tags):
    """按阈值过滤标签，返回逗号分隔的标签字符串"""
    tags = []
    for i, row in label_df.iterrows():
        category = row['category']
        if category == 9:
            continue
        prob = float(probabilities[i])
        threshold = character_threshold if category == 4 else general_threshold
        if prob >= threshold:
            tag_name = row['name'].replace('_', ' ')
            if tag_name not in excluded_tags:
                tags.append(tag_name)
    return ', '.join(tags)


def _collect_untagged(upload_dir):
    """收集无标签的图片列表"""
    all_images = get_image_files(upload_dir)
    to_tag = []
    for filename in all_images:
        base_name = os.path.splitext(filename)[0]
        txt_path = os.path.join(upload_dir, f"{base_name}.txt")
        if not os.path.exists(txt_path) or os.path.getsize(txt_path) == 0:
            to_tag.append(filename)
    return all_images, to_tag


@tagger_bp.route('/auto_tag', methods=['POST'])
def auto_tag():
    """批量自动打标（SSE 流式）：对无标签的图片调用视觉模型生成标签"""
    vcfg = get_vision_config()
    if not vcfg['api_url'] or not vcfg['model']:
        return jsonify({'error': '未配置视觉模型（VISION_API_URL / VISION_MODEL）'}), 400
    if not vcfg['prompt']:
        return jsonify({'error': '未配置视觉模型系统提示（VISION_SYSTEM_PROMPT）'}), 400

    upload_dir = current_app.config['UPLOAD_FOLDER']
    all_images, to_tag = _collect_untagged(upload_dir)
    skipped = len(all_images) - len(to_tag)

    if not to_tag:
        return jsonify({'tagged': 0, 'skipped': skipped, 'errors': [], 'message': '所有图片已有标签'})

    total = len(to_tag)
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {vcfg["api_key"]}',
    }

    def generate():
        tagged = 0
        error_count = 0

        for idx, filename in enumerate(to_tag, start=1):
            yield sse_event('progress', {'current': idx, 'total': total, 'item': filename})

            file_path = os.path.join(upload_dir, filename)
            ext = os.path.splitext(filename)[1].lstrip('.')
            mime_map = {'jpg': 'jpeg', 'jpeg': 'jpeg', 'png': 'png', 'gif': 'gif', 'webp': 'webp'}
            mime = f"image/{mime_map.get(ext, 'jpeg')}"

            try:
                with open(file_path, 'rb') as f:
                    img_b64 = base64.b64encode(f.read()).decode('utf-8')

                payload = {
                    'model': vcfg['model'],
                    'messages': [
                        {'role': 'system', 'content': vcfg['prompt']},
                        {'role': 'user', 'content': [
                            {'type': 'image_url', 'image_url': {'url': f'data:{mime};base64,{img_b64}'}},
                            {'type': 'text', 'text': '开始分析并输出标签：'}
                        ]}
                    ],
                    'temperature': 0.3,
                }

                response = requests.post(vcfg['api_url'], headers=headers, json=payload, timeout=120)
                result = response.json()

                if 'error' in result:
                    error_count += 1
                    yield sse_event('error', {'item': filename, 'error': result['error'].get('message', '未知错误')})
                    continue

                tags = result['choices'][0]['message']['content'].strip()
                txt_path = os.path.join(upload_dir, f"{os.path.splitext(filename)[0]}.txt")
                with open(txt_path, 'w', encoding='utf-8') as f:
                    f.write(tags)
                tagged += 1

            except Exception as e:
                error_count += 1
                yield sse_event('error', {'item': filename, 'error': str(e)})

        yield sse_event('complete', {'tagged': tagged, 'skipped': skipped, 'errors': error_count})

    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@tagger_bp.route('/auto_tag_wd14', methods=['POST'])
def auto_tag_wd14():
    """批量自动打标（WD14 本地模型，SSE 流式）：对无标签的图片使用 WD14 tagger 生成标签"""
    cfg = get_wd14_config()
    upload_dir = current_app.config['UPLOAD_FOLDER']

    all_images, to_tag = _collect_untagged(upload_dir)
    skipped = len(all_images) - len(to_tag)

    if not to_tag:
        return jsonify({'tagged': 0, 'skipped': skipped, 'errors': [], 'message': '所有图片已有标签'})

    total = len(to_tag)

    def generate():
        tagged = 0
        error_count = 0
        done = 0  # 已处理张数（含失败），用于进度推送

        yield sse_event('progress', {'current': 0, 'total': total, 'item': '正在加载 WD14 模型...'})

        try:
            model_cache = wd14_load_model(cfg['model_path'])
        except Exception as e:
            yield sse_event('fatal', {'error': f'WD14 模型加载失败: {str(e)}'})
            return

        session = model_cache['session']
        label_df = model_cache['label_df']
        input_name = session.get_inputs()[0].name

        for batch_start in range(0, total, WD14_BATCH_SIZE):
            batch_files = to_tag[batch_start:batch_start + WD14_BATCH_SIZE]

            # 预处理阶段：逐张解码+缩放，失败的单独记错并跳过（不进 batch）
            imgs = []
            ok_files = []
            for filename in batch_files:
                try:
                    imgs.append(wd14_preprocess_image(os.path.join(upload_dir, filename)))
                    ok_files.append(filename)
                except Exception as e:
                    done += 1
                    error_count += 1
                    yield sse_event('progress', {'current': done, 'total': total, 'item': filename})
                    yield sse_event('error', {'item': filename, 'error': str(e)})

            if not imgs:
                continue

            # 批量推理：(N,448,448,3) -> (N, num_tags)
            try:
                batch_input = np.concatenate(imgs, axis=0)
                probs_batch = session.run(None, {input_name: batch_input})[0]
            except Exception as e:
                # 整批推理失败，逐张报错
                for filename in ok_files:
                    done += 1
                    error_count += 1
                    yield sse_event('progress', {'current': done, 'total': total, 'item': filename})
                    yield sse_event('error', {'item': filename, 'error': str(e)})
                continue

            # 逐张过滤+写标签（进度仍逐张推送）
            for k, filename in enumerate(ok_files):
                done += 1
                yield sse_event('progress', {'current': done, 'total': total, 'item': filename})
                try:
                    tags = wd14_filter_tags(
                        label_df, probs_batch[k],
                        cfg['general_threshold'], cfg['character_threshold'],
                        cfg['excluded_tags']
                    )
                    if tags:
                        txt_path = os.path.join(upload_dir, f"{os.path.splitext(filename)[0]}.txt")
                        with open(txt_path, 'w', encoding='utf-8') as f:
                            f.write(tags)
                        tagged += 1
                    else:
                        error_count += 1
                        yield sse_event('error', {'item': filename, 'error': '未产生有效标签'})
                except Exception as e:
                    error_count += 1
                    yield sse_event('error', {'item': filename, 'error': str(e)})

        yield sse_event('complete', {'tagged': tagged, 'skipped': skipped, 'errors': error_count})

    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})
