# -*- coding: utf-8 -*-
import os
from flask import Blueprint, request, jsonify, current_app

tag_ops_bp = Blueprint('tag_operations', __name__)


@tag_ops_bp.route('/prepend_tags', methods=['POST'])
def prepend_tags():
    """为所有标签文件添加触发词（开头或末尾）"""
    data = request.get_json()
    triggers = data.get('triggers', '').strip()
    position = data.get('position', 'start')
    if not triggers:
        return jsonify({'error': '触发词不能为空'}), 400

    upload_dir = current_app.config['UPLOAD_FOLDER']
    updated = 0
    for filename in os.listdir(upload_dir):
        if not filename.endswith('.txt'):
            continue
        txt_path = os.path.join(upload_dir, filename)
        with open(txt_path, 'r', encoding='utf-8') as f:
            content = f.read().strip()
        if position == 'end':
            new_content = content + ', ' + triggers if content else triggers
        else:
            new_content = triggers + ', ' + content if content else triggers
        with open(txt_path, 'w', encoding='utf-8') as f:
            f.write(new_content)
        updated += 1
    return jsonify({'updated': updated})


@tag_ops_bp.route('/find_replace', methods=['POST'])
def find_replace():
    """在所有标签文件中查找并替换标签"""
    data = request.get_json()
    find_text = data.get('find', '').strip()
    replace_text = data.get('replace', '').strip()
    preview = data.get('preview', False)
    if not find_text:
        return jsonify({'error': '查找内容不能为空'}), 400

    upload_dir = current_app.config['UPLOAD_FOLDER']
    updated_files = 0
    replaced_count = 0
    for filename in os.listdir(upload_dir):
        if not filename.endswith('.txt'):
            continue
        txt_path = os.path.join(upload_dir, filename)
        with open(txt_path, 'r', encoding='utf-8') as f:
            content = f.read()
        tags = [t.strip() for t in content.split(',') if t.strip()]
        new_tags = []
        changed = False
        for tag in tags:
            if tag == find_text:
                new_tags.append(replace_text if not preview else tag)
                replaced_count += 1
                changed = True
            else:
                new_tags.append(tag)
        if changed and not preview:
            new_tags = [t for t in new_tags if t.strip()]
            with open(txt_path, 'w', encoding='utf-8') as f:
                f.write(', '.join(new_tags))
            updated_files += 1
        elif changed:
            updated_files += 1
    return jsonify({'updated_files': updated_files, 'replaced': replaced_count})
