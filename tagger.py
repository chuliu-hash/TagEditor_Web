# -*- coding: utf-8 -*-
import os
import base64
import numpy as np
from flask import Blueprint, jsonify, current_app, Response
from config import get_vision_config, get_wd14_config, get_image_files, get_prompt
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


def wd14_filter_tags(label_df, probabilities, general_threshold, character_threshold):
    """按阈值过滤标签，返回逗号分隔的标签字符串。"""
    tags = []
    for i, row in label_df.iterrows():
        category = row['category']
        if category == 9:
            continue
        prob = float(probabilities[i])
        threshold = character_threshold if category == 4 else general_threshold
        if prob >= threshold:
            tag_name = row['name'].replace('_', ' ')
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
    print(f"\n[WD14] 开始自动打标: 共 {total} 张待处理, {skipped} 张已有标签跳过")

    def generate():
        tagged = 0
        error_count = 0
        done = 0  # 已处理张数（含失败），用于进度推送

        try:
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
                batch_idx = batch_start // WD14_BATCH_SIZE + 1
                total_batches = (total + WD14_BATCH_SIZE - 1) // WD14_BATCH_SIZE
                print(f"[WD14] 批次 {batch_idx}/{total_batches} ({len(batch_files)} 张)")

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
                        print(f"[WD14] ✗ {filename}: 预处理失败 — {e}")
                        yield sse_event('progress', {'current': done, 'total': total, 'item': filename})
                        yield sse_event('error', {'item': filename, 'error': str(e)})

                if not imgs:
                    print(f"[WD14] 批次 {batch_idx}: 全部预处理失败，跳过")
                    continue

                # 批量推理：(N,448,448,3) -> (N, num_tags)
                try:
                    batch_input = np.concatenate(imgs, axis=0)
                    probs_batch = session.run(None, {input_name: batch_input})[0]
                    print(f"[WD14] 批次 {batch_idx}: 推理完成 ({len(ok_files)} 张)")
                except Exception as e:
                    # 整批推理失败，逐张报错
                    print(f"[WD14] 批次 {batch_idx}: 批量推理失败 — {e}")
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
                            cfg['general_threshold'], cfg['character_threshold']
                        )
                        if tags:
                            txt_path = os.path.join(upload_dir, f"{os.path.splitext(filename)[0]}.txt")
                            with open(txt_path, 'w', encoding='utf-8') as f:
                                f.write(tags)
                            tag_count = len([t for t in tags.split(',') if t.strip()])
                            print(f"[WD14] ✓ {filename} → {tag_count} 个标签")
                            tagged += 1
                        else:
                            error_count += 1
                            print(f"[WD14] ✗ {filename}: 未产生有效标签")
                            yield sse_event('error', {'item': filename, 'error': '未产生有效标签'})
                    except Exception as e:
                        error_count += 1
                        print(f"[WD14] ✗ {filename}: {e}")
                        yield sse_event('error', {'item': filename, 'error': str(e)})

            print(f"[WD14] 完成: {tagged} 张成功, {skipped} 张跳过, {error_count} 张失败")
            yield sse_event('complete', {'tagged': tagged, 'skipped': skipped, 'errors': error_count})
        except Exception as e:
            # 生成器级别的未预期异常：发 fatal，前端能正常收尾
            yield sse_event('fatal', {'error': f'WD14 自动打标异常终止: {e}'})

    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@tagger_bp.route('/auto_caption_vlm', methods=['POST'])
def auto_caption_vlm():
    """批量生成自然语言描述（SSE 流式）：先读取已有 txt 标签作为参考，
    送入 VLM 将结构化标签"翻译"为连贯的自然语言描述。

    核心思路：让 VLM 扮演"翻译官"而非"创作者"——把已有的结构化标签翻译成自然语言。
    无 txt 标签的图片仅靠 VLM 看图直接描述。
    """
    vcfg = get_vision_config()
    if not vcfg['api_url'] or not vcfg['model']:
        return jsonify({'error': '未配置视觉模型（VISION_API_URL / VISION_MODEL）'}), 400

    # 描述提示词：统一从 prompts/ 目录读取（vlm_caption.txt），必填
    caption_prompt = get_prompt('vlm_caption')
    if not caption_prompt:
        return jsonify({'error': '未配置 VLM 描述提示词，请创建 prompts/vlm_caption.txt'}), 400

    upload_dir = current_app.config['UPLOAD_FOLDER']
    all_images = get_image_files(upload_dir)

    # 过滤：只处理没有 .nl.txt 的图片（已有描述的跳过）
    to_process = []
    for filename in all_images:
        base = os.path.splitext(filename)[0]
        nl_path = os.path.join(upload_dir, f"{base}.nl.txt")
        if not os.path.exists(nl_path):
            to_process.append(filename)

    skipped = len(all_images) - len(to_process)

    if not to_process:
        return jsonify({'tagged': 0, 'skipped': skipped, 'errors': [],
                        'message': '所有图片已有自然语言描述'})

    total = len(to_process)
    print(f"\n[VLM] 开始生成自然语言描述: 共 {total} 张待处理, {skipped} 张已有描述跳过")

    from openai import OpenAI
    client = OpenAI(base_url=vcfg['api_url'], api_key=vcfg['api_key'])

    def generate():
        tagged = 0
        skipped = 0
        error_count = 0
        try:
            yield sse_event('progress', {'current': 0, 'total': total,
                            'item': '正在准备处理...'})

            for idx, filename in enumerate(to_process, start=1):
                yield sse_event('progress', {'current': idx, 'total': total, 'item': filename})
                file_path = os.path.join(upload_dir, filename)

                try:
                    # Step 1: 读取已有 txt 标签作为参考（有则用，无则跳过）
                    ref_tags = ''
                    base = os.path.splitext(filename)[0]
                    txt_path = os.path.join(upload_dir, f"{base}.txt")
                    if os.path.exists(txt_path) and os.path.getsize(txt_path) > 0:
                        with open(txt_path, 'r', encoding='utf-8') as f:
                            content = f.read().strip()
                        if content:
                            ref_tags = content

                    # Step 2: VLM 生成自然语言描述
                    with open(file_path, 'rb') as f:
                        img_b64 = base64.b64encode(f.read()).decode('utf-8')

                    user_content = [
                        {'type': 'image_url',
                         'image_url': {'url': f'data:image/{os.path.splitext(filename)[1].lstrip(".")};base64,{img_b64}'}},
                    ]

                    if ref_tags:
                        user_content.append({
                            'type': 'text',
                            'text': 'Reference tags:\n' + ref_tags
                        })

                    response = client.chat.completions.create(
                        model=vcfg['model'],
                        messages=[
                            {'role': 'system', 'content': caption_prompt},
                            {'role': 'user', 'content': user_content}
                        ],
                        temperature=0.7,
                        max_tokens=512,
                    )

                    description = (response.choices[0].message.content or '').strip()
                    if not description:
                        finish = response.choices[0].finish_reason
                        # 推理模型（如 Qwen3.5 Vision）可能把 token 全花在思考上，未输出正式回答
                        has_reasoning = hasattr(response.choices[0].message, 'reasoning_content') and response.choices[0].message.reasoning_content
                        if has_reasoning:
                            print(f"[VLM] △ {filename}: 模型在思考中，未生成正式描述, finish={finish}")
                        else:
                            print(f"[VLM] △ {filename}: 描述为空, finish={finish}")
                        skipped += 1
                        continue

                    # 保存到新的 .nl.txt，不覆盖原标签
                    out_path = os.path.join(upload_dir, f"{base}.nl.txt")
                    with open(out_path, 'w', encoding='utf-8') as f:
                        f.write(description)
                    print(f"[VLM] ✓ {filename} → {len(description)} 字符")
                    tagged += 1

                except Exception as e:
                    error_count += 1
                    print(f"[VLM] ✗ {filename}: {e}")
                    yield sse_event('error', {'item': filename, 'error': str(e)})

            print(f"[VLM] 完成: {tagged} 张成功, {skipped} 张跳过, {error_count} 张失败")
            yield sse_event('complete', {'tagged': tagged, 'skipped': skipped,
                            'errors': error_count})
        except Exception as e:
            yield sse_event('fatal', {'error': f'描述生成异常终止: {e}'})

    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

