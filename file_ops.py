# -*- coding: utf-8 -*-
import os
from flask import Blueprint, request, redirect, url_for, jsonify, send_from_directory, current_app
from config import allowed_file, safe_filename, is_within_directory
from translation import load_cache, save_cache

file_ops_bp = Blueprint('file_ops', __name__)


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
    """获取图片对应的标签，同时返回翻译缓存"""
    from translation import load_cache, lookup_tag
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

    # 一次性返回标签 + 翻译
    tags = [t.strip() for t in caption.split(',') if t.strip()] if caption else []
    cache = load_cache()
    translations = []
    for tag in tags:
        translations.append(lookup_tag(tag.lower(), cache))

    return jsonify({'caption': caption, 'translations': translations})


@file_ops_bp.route('/save_caption/<image_name>', methods=['POST'])
def save_caption(image_name):
    """保存标签到文件，同时将翻译列的变动更新到翻译缓存"""
    data = request.get_json()
    content = data.get('content', '')
    translations = data.get('translations', {})

    # 大小写不敏感去重（与前端逻辑一致），防止非浏览器客户端写入重复标签
    seen = set()
    deduped = []
    for t in content.split(','):
        t = t.strip()
        if t:
            key = t.lower()
            if key not in seen:
                seen.add(key)
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

    # 更新翻译缓存：对比前端传来的翻译与缓存，有变化则更新
    if translations:
        cache = load_cache()
        changed = False
        for tag, tr in translations.items():
            tag = tag.lower().strip()
            tr = tr.strip()
            if tag and tr and cache.get(tag) != tr:
                cache[tag] = tr
                changed = True
        if changed:
            save_cache(cache)

    return jsonify({'success': True})


@file_ops_bp.route('/tag_stats')
def tag_stats():
    """统计所有标签出现次数，附翻译缓存"""
    from collections import Counter
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

    cache = load_cache()
    stats = []
    for tag, count in counter.most_common():
        entry = {'tag': tag, 'count': count}
        tr = cache.get(tag.lower()) or ''  # 翻译缓存 key 全小写，用原 tag 的小写形式查
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
