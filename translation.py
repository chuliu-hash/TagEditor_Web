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

# 内存缓存 + 反向索引，避免每次请求读磁盘和线性扫描。
# 多进程部署（如 gunicorn 多 worker）下每个进程各持一份内存缓存，
# 通过 _cache_file_mtime 检测磁盘文件被其他进程更新来失效内存缓存；
# save_cache 采用临时文件 + os.replace 原子写入，避免并发写损坏 JSON。
_cache_memory = None
_reverse_index = None
_cache_mtime = None


def _cache_file_mtime():
    """读取缓存文件的 mtime；文件不存在或不可访问时返回 None"""
    try:
        return CACHE_FILE.stat().st_mtime
    except OSError:
        return None


def load_cache():
    global _cache_memory, _reverse_index, _cache_mtime
    current_mtime = _cache_file_mtime()
    # 内存缓存命中且磁盘未被其他进程更新时直接返回
    if _cache_memory is not None and _cache_mtime == current_mtime:
        return _cache_memory
    if CACHE_FILE.exists():
        try:
            with open(CACHE_FILE, 'r', encoding='utf-8') as f:
                _cache_memory = json.load(f)
        except Exception:
            _cache_memory = {}
    else:
        _cache_memory = {}
    _cache_mtime = current_mtime
    # 构建反向索引：value.lower() → key
    _reverse_index = {}
    for k, v in _cache_memory.items():
        _reverse_index[v.lower()] = k
    return _cache_memory


def save_cache(cache):
    global _cache_memory, _reverse_index, _cache_mtime
    _cache_memory = cache
    _reverse_index = {}
    for k, v in cache.items():
        _reverse_index[v.lower()] = k
    # 原子写入：先写临时文件再替换，防止多进程/并发请求写损坏 JSON
    tmp_path = str(CACHE_FILE) + '.tmp'
    with open(tmp_path, 'w', encoding='utf-8') as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, str(CACHE_FILE))
    _cache_mtime = _cache_file_mtime()


def lookup_tag(tag_lower, cache):
    """O(1) 双向查找标签"""
    if tag_lower in cache:
        return cache[tag_lower]
    if _reverse_index is not None and tag_lower in _reverse_index:
        return _reverse_index[tag_lower]
    return ''


def _llm_translate_tags(cfg, tags, src_name, dst_name):
    """调用 LLM 翻译一批标签。返回 (translations, error)。

    成功：translations 为与 tags 等长的列表，error 为 None。
    失败：translations 为 None，error 为错误消息。
    网络异常（如 requests.exceptions.Timeout）向上抛出，由调用方处理。
    """
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {cfg["api_key"]}',
    }
    payload = {
        'model': cfg['model'],
        'messages': [
            {'role': 'system', 'content': cfg['prompt'].format(src_name=src_name, dst_name=dst_name)},
            {'role': 'user', 'content': json.dumps(tags, ensure_ascii=False)},
        ],
        'temperature': 0.3,
    }
    response = requests.post(cfg['api_url'], headers=headers, json=payload, timeout=120)
    result = response.json()
    if 'error' in result:
        return None, result['error'].get('message', '未知错误')
    content = result['choices'][0]['message']['content'].strip()
    match = re.search(r'\[.*?\]', content, re.DOTALL)
    translations = json.loads(match.group()) if match else [content] * len(tags)
    while len(translations) < len(tags):
        translations.append('')
    return translations[:len(tags)], None


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
    if not cfg['prompt']:
        return jsonify({'error': '未配置翻译系统提示（LLM_TAG_TRANSLATE_PROMPT）'}), 400
    lang_map = {'zh': '中文', 'en': '英文'}
    src_name = lang_map.get(src, src)
    dst_name = lang_map.get(dst, dst)

    try:
        translations, err = _llm_translate_tags(cfg, uncached, src_name, dst_name)
        if err:
            return jsonify({'error': err}), 500
    except requests.exceptions.Timeout:
        return jsonify({'error': '翻译超时'}), 504
    except Exception as e:
        return jsonify({'error': str(e)}), 500

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

    batch_size = 50
    total_batches = (len(tag_list) + batch_size - 1) // batch_size

    def generate():
        translated_map = {}
        error_count = 0

        for batch_idx, i in enumerate(range(0, len(tag_list), batch_size), start=1):
            batch = tag_list[i:i+batch_size]
            batch_desc = f"标签 {i+1}-{min(i+batch_size, len(tag_list))}"

            yield sse_event('progress', {'current': batch_idx, 'total': total_batches, 'item': batch_desc})

            try:
                translations, err = _llm_translate_tags(cfg, batch, src_name, dst_name)
                if err:
                    error_count += 1
                    yield sse_event('error', {'item': batch_desc, 'error': err})
                    continue
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
