# -*- coding: utf-8 -*-
import os
import re
import json
import requests
from flask import Blueprint, request, jsonify, current_app, Response
from pathlib import Path
from config import load_env, get_llm_config
from sse_utils import sse_event

translation_bp = Blueprint('translation', __name__)

CACHE_FILE = Path(__file__).parent / 'translation_cache.json'

# 内存缓存 + 反向索引，避免每次请求读磁盘和线性扫描
_cache_memory = None
_reverse_index = None


def load_cache():
    global _cache_memory, _reverse_index
    if _cache_memory is not None:
        return _cache_memory
    if CACHE_FILE.exists():
        try:
            with open(CACHE_FILE, 'r', encoding='utf-8') as f:
                _cache_memory = json.load(f)
        except Exception:
            _cache_memory = {}
    else:
        _cache_memory = {}
    # 构建反向索引：value.lower() → key
    _reverse_index = {}
    for k, v in _cache_memory.items():
        _reverse_index[v.lower()] = k
    return _cache_memory


def save_cache(cache):
    global _cache_memory, _reverse_index
    _cache_memory = cache
    _reverse_index = {}
    for k, v in cache.items():
        _reverse_index[v.lower()] = k
    with open(CACHE_FILE, 'w', encoding='utf-8') as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


def lookup_tag(tag_lower, cache):
    """O(1) 双向查找标签"""
    if tag_lower in cache:
        return cache[tag_lower]
    if _reverse_index is not None and tag_lower in _reverse_index:
        return _reverse_index[tag_lower]
    return ''


@translation_bp.route('/lookup_cache', methods=['POST'])
def lookup_cache():
    """根据标签列表查找翻译缓存（单向存储，双向查找）"""
    data = request.get_json()
    tags = data.get('tags', [])
    cache = load_cache()

    translations = []
    for tag in tags:
        key = tag.lower().strip()
        translations.append(lookup_tag(key, cache))

    return jsonify({'translations': translations})


@translation_bp.route('/translate_tags', methods=['POST'])
def translate_tags():
    """批量翻译标签，带本地缓存"""
    data = request.get_json()
    tags = data.get('tags', [])
    src = data.get('src', 'en')
    dst = data.get('dst', 'zh')
    if not tags:
        return jsonify({'translations': []})

    isEn2Zh = (src == 'en' and dst == 'zh')
    cache = load_cache()

    results = [None] * len(tags)
    uncached = []
    uncached_indices = []
    for i, tag in enumerate(tags):
        tag_lower = tag.lower().strip()
        if tag_lower in cache:
            results[i] = cache[tag_lower]
        else:
            results[i] = lookup_tag(tag_lower, cache)
            if not results[i]:
                uncached.append(tag)
                uncached_indices.append(i)

    if not uncached:
        return jsonify({'translations': [r or '' for r in results]})

    cfg = get_llm_config()
    lang_map = {'zh': '中文', 'en': '英文'}
    src_name = lang_map.get(src, src)
    dst_name = lang_map.get(dst, dst)

    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {cfg["api_key"]}',
    }

    if not cfg['prompt']:
        return jsonify({'error': '未配置翻译系统提示（LLM_TAG_TRANSLATE_PROMPT）'}), 400
    tag_system_content = cfg['prompt'].format(src_name=src_name, dst_name=dst_name)

    payload = {
        'model': cfg['model'],
        'messages': [
            {'role': 'system', 'content': tag_system_content},
            {'role': 'user', 'content': json.dumps(uncached, ensure_ascii=False)}
        ],
        'temperature': 0.3,
    }

    try:
        response = requests.post(cfg['api_url'], headers=headers, json=payload, timeout=120)
        result = response.json()

        if 'error' in result:
            return jsonify({'error': result['error'].get('message', '未知错误')}), 500

        content = result['choices'][0]['message']['content'].strip()
        match = re.search(r'\[.*?\]', content, re.DOTALL)
        if match:
            translations = json.loads(match.group())
        else:
            translations = [content] * len(uncached)

        while len(translations) < len(uncached):
            translations.append('')
        translations = translations[:len(uncached)]

        changed = False
        for j, idx in enumerate(uncached_indices):
            translated = translations[j]
            results[idx] = translated
            tag_lower = uncached[j].lower().strip()
            if tag_lower and translated:
                translated_lower = translated.lower().strip()
                if isEn2Zh:
                    cache[tag_lower] = translated
                else:
                    cache[translated_lower] = tag_lower
                changed = True

        if changed:
            save_cache(cache)

        return jsonify({'translations': [r or '' for r in results]})

    except requests.exceptions.Timeout:
        return jsonify({'error': '翻译超时'}), 504
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@translation_bp.route('/batch_translate', methods=['POST'])
def batch_translate():
    """扫描所有标签文件，将未翻译的标签批量翻译并写回（SSE 流式）"""
    data = request.get_json()
    src = data.get('src', 'en')
    dst = data.get('dst', 'zh')
    upload_dir = current_app.config['UPLOAD_FOLDER']

    cache = load_cache()
    all_tags = set()
    file_tags = {}
    for filename in os.listdir(upload_dir):
        if not filename.endswith('.txt'):
            continue
        txt_path = os.path.join(upload_dir, filename)
        with open(txt_path, 'r', encoding='utf-8') as f:
            content = f.read().strip()
        if not content:
            continue
        tags = [t.strip() for t in content.split(',') if t.strip()]
        file_tags[filename] = tags
        for tag in tags:
            tag_lower = tag.lower()
            if tag_lower not in cache:
                if not lookup_tag(tag_lower, cache):
                    all_tags.add(tag)

    if not all_tags:
        return jsonify({'translated': 0, 'files': len(file_tags), 'message': '所有标签已翻译'})

    tag_list = list(all_tags)
    cfg = get_llm_config()
    if not cfg['prompt']:
        return jsonify({'error': '未配置翻译系统提示（LLM_TAG_TRANSLATE_PROMPT）'}), 400

    lang_map = {'zh': '中文', 'en': '英文'}
    src_name = lang_map.get(src, src)
    dst_name = lang_map.get(dst, dst)
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {cfg["api_key"]}',
    }

    batch_size = 50
    total_batches = (len(tag_list) + batch_size - 1) // batch_size

    def generate():
        translated_map = {}
        error_count = 0

        for batch_idx, i in enumerate(range(0, len(tag_list), batch_size), start=1):
            batch = tag_list[i:i+batch_size]
            batch_desc = f"标签 {i+1}-{min(i+batch_size, len(tag_list))}"

            yield sse_event('progress', {'current': batch_idx, 'total': total_batches, 'item': batch_desc})

            payload = {
                'model': cfg['model'],
                'messages': [
                    {'role': 'system', 'content': cfg['prompt'].format(src_name=src_name, dst_name=dst_name)},
                    {'role': 'user', 'content': json.dumps(batch, ensure_ascii=False)}
                ],
                'temperature': 0.3,
            }
            try:
                response = requests.post(cfg['api_url'], headers=headers, json=payload, timeout=120)
                result = response.json()
                if 'error' in result:
                    error_count += 1
                    yield sse_event('error', {'item': batch_desc, 'error': result['error'].get('message', '未知错误')})
                    continue
                content = result['choices'][0]['message']['content'].strip()
                match = re.search(r'\[.*?\]', content, re.DOTALL)
                if match:
                    translations = json.loads(match.group())
                else:
                    translations = [content] * len(batch)
                while len(translations) < len(batch):
                    translations.append('')
                translations = translations[:len(batch)]
                for j, tag in enumerate(batch):
                    translated_map[tag] = translations[j]
                    tag_lower = tag.lower().strip()
                    if tag_lower and translations[j].strip():
                        cache[tag_lower] = translations[j]
            except Exception as e:
                error_count += 1
                yield sse_event('error', {'item': batch_desc, 'error': str(e)})

        save_cache(cache)
        yield sse_event('complete', {'translated': len(translated_map), 'files': len(file_tags), 'errors': error_count})

    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})
