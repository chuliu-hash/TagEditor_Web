# -*- coding: utf-8 -*-
"""批量标签操作路由：触发词追加 / 全局查找替换。

这两个接口都会**遍历整个 uploads 目录并改写每一个 .txt**，是全项目唯一
「一次点击改所有文件」的入口，所以对输入的校验和写入的原子性要求最高。
"""
import os
from flask import Blueprint, request, jsonify, current_app

from config import write_text_atomic
import logging


log = logging.getLogger(__name__)

tag_ops_bp = Blueprint('tag_operations', __name__)


def _tag_files(upload_dir):
    """列出目录下所有标签文件（排除 .nl.txt 描述文件与子目录）。"""
    out = []
    for filename in os.listdir(upload_dir):
        if not (filename.endswith('.txt') and not filename.endswith('.nl.txt')):
            continue
        path = os.path.join(upload_dir, filename)
        if os.path.isfile(path):     # 跳过硬链接/子目录，避免 open() 抛 IsADirectoryError
            out.append(path)
    return out


def _json_body():
    """取请求体并保证是 dict。非法时返回 (None, 错误响应)。

    `request.get_json()` 对**非 JSON** 的 body 会由 Flask 兜成 400，
    但对**合法 JSON 的非对象**（如 `[1,2]`、`"abc"`）会原样返回
    list/str，随后 `.get(...)` 直接抛 AttributeError → 500。
    本模块两个路由都是「一改全改」，绝不能带着未校验的参数往下走。
    """
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return None, (jsonify({'error': '请求体必须是 JSON 对象'}), 400)
    return data, None


@tag_ops_bp.route('/prepend_tags', methods=['POST'])
def prepend_tags():
    """为所有标签文件添加触发词（开头或末尾）。

    幂等：已经以该触发词开头（或结尾）的文件会跳过，重复点击不会累积。
    返回 {updated, skipped, errors}。
    """
    data, err = _json_body()
    if err:
        return err
    triggers = data.get('triggers')
    if not isinstance(triggers, str):
        return jsonify({'error': 'triggers 必须是字符串'}), 400
    triggers = triggers.strip().strip(',')
    if not triggers:
        return jsonify({'error': '触发词不能为空'}), 400

    position = data.get('position', 'start')
    if position not in ('start', 'end'):
        return jsonify({'error': "position 只能是 'start' 或 'end'"}), 400

    upload_dir = current_app.config['UPLOAD_FOLDER']
    updated = skipped = errors = 0
    for txt_path in _tag_files(upload_dir):
        try:
            with open(txt_path, 'r', encoding='utf-8') as f:
                content = f.read().strip()

            # 幂等判定：按「标签项」精确比对，不做子串匹配 ——
            # 触发词 'my' 不该让已含 'my_char' 的文件被判定为「已添加」。
            existing = [t.strip() for t in content.split(',') if t.strip()]
            if triggers.lower() in [t.lower() for t in existing]:
                skipped += 1
                continue

            if position == 'end':
                new_content = (content + ', ' + triggers) if content else triggers
            else:
                new_content = (triggers + ', ' + content) if content else triggers

            write_text_atomic(txt_path, new_content)
            updated += 1
        except Exception as e:
            # 单个文件失败不影响其余：全目录批量操作里，一个坏文件不该让整批中断，
            # 但要计数并向用户汇报，不能像原先那样静默吞掉。
            errors += 1
            log.error(f"[prepend_tags] 处理失败 {os.path.basename(txt_path)}: {e}")

    return jsonify({'updated': updated, 'skipped': skipped, 'errors': errors})


@tag_ops_bp.route('/find_replace', methods=['POST'])
def find_replace():
    """在所有标签文件中查找并替换标签（排除 .nl.txt 描述文件）。

    匹配是**整项精确匹配**（`solo` 不会命中 `solo_focus`）。
    replace 为空 = 删除该标签（原有行为，前端会提示确认）。
    preview=True 时只统计不落盘。
    """
    data, err = _json_body()
    if err:
        return err
    find_text = data.get('find')
    if not isinstance(find_text, str) or not find_text.strip():
        return jsonify({'error': '查找内容不能为空'}), 400
    find_text = find_text.strip()

    replace_raw = data.get('replace', '')
    if not isinstance(replace_raw, str):
        return jsonify({'error': 'replace 必须是字符串'}), 400
    replace_text = replace_raw.strip()

    preview = bool(data.get('preview', False))

    # 替换文本里含逗号会把一个标签拆成多个（如 'a, b'）。这是用户可能真的想要的，
    # 但静默发生会让人以为只是改了个名，所以显式回报，由前端决定是否提示。
    replace_into_multiple = ',' in replace_text or '，' in replace_text

    upload_dir = current_app.config['UPLOAD_FOLDER']
    updated_files = replaced_count = errors = 0
    for txt_path in _tag_files(upload_dir):
        try:
            with open(txt_path, 'r', encoding='utf-8') as f:
                content = f.read()
            tags = [t.strip() for t in content.split(',') if t.strip()]

            new_tags = []
            changed = False
            for tag in tags:
                # 精确匹配（大小写敏感，与前端统计表的展示口径一致）
                if tag == find_text:
                    replaced_count += 1
                    changed = True
                    if replace_text:
                        # 替换文本本身可能含逗号 → 展开成多个标签
                        new_tags.extend(t.strip() for t in
                                        replace_text.replace('，', ',').split(',') if t.strip())
                    # 空 replace：该项直接丢弃（= 删除标签）
                else:
                    new_tags.append(tag)
            if not changed:
                continue
            updated_files += 1
            if not preview:
                write_text_atomic(txt_path, ', '.join(new_tags))
        except Exception as e:
            errors += 1
            log.error(f"[find_replace] 处理失败 {os.path.basename(txt_path)}: {e}")

    return jsonify({'updated_files': updated_files, 'replaced': replaced_count,
                    'errors': errors, 'preview': preview,
                    'replace_into_multiple': replace_into_multiple})
