# -*- coding: utf-8 -*-
import os
import re
from pathlib import Path


_env_mtime = None


def load_env(env_path='.env'):
    """读取 .env 到 os.environ。通过 mtime 检测避免每次调用都读磁盘（API 热更新仍生效）。"""
    global _env_mtime
    env_file = Path(__file__).parent / env_path
    try:
        mtime = env_file.stat().st_mtime
    except OSError:
        mtime = None
    # .env 未变化则跳过磁盘读取
    if _env_mtime is not None and _env_mtime == mtime:
        return
    if env_file.exists():
        with open(env_file, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, _, value = line.partition('=')
                    key = key.strip()
                    value = value.strip().strip('"').strip("'")
                    os.environ[key] = value
    _env_mtime = mtime


def get_llm_config():
    """每次调用时重新读取 .env 配置"""
    load_env()
    return {
        'api_url': os.environ.get('LLM_API_URL', 'http://localhost:8080/v1/chat/completions'),
        'api_key': os.environ.get('LLM_API_KEY', 'ollama'),
        'model': os.environ.get('LLM_MODEL', 'qwen2.5:7b'),
        'prompt': os.environ.get('LLM_TAG_TRANSLATE_PROMPT', '').replace('\\n', '\n'),
    }


def get_vision_config():
    """每次调用时重新读取视觉模型配置"""
    load_env()
    return {
        'api_url': os.environ.get('VISION_API_URL', ''),
        'api_key': os.environ.get('VISION_API_KEY', ''),
        'model': os.environ.get('VISION_MODEL', ''),
        'prompt': os.environ.get('VISION_SYSTEM_PROMPT', '').replace('\\n', '\n'),
    }


def get_wd14_config():
    """每次调用时重新读取 WD14 配置"""
    load_env()
    model_path = os.environ.get('WD14_MODEL_PATH', 'models/wd-swinv2-tagger-v3')
    if not os.path.isabs(model_path):
        model_path = str(Path(__file__).parent / model_path)
    return {
        'model_path': model_path,
        'general_threshold': float(os.environ.get('WD14_GENERAL_THRESHOLD', '0.35')),
        'character_threshold': float(os.environ.get('WD14_CHARACTER_THRESHOLD', '0.1')),
        'excluded_tags': [t.strip() for t in os.environ.get('WD14_EXCLUDED_TAGS', '').split(',') if t.strip()],
    }


def get_realesrgan_config():
    """每次调用时重新读取 Real-ESRGAN 配置。
    使用 anime_6B 动漫模型（4x 放大），权重路径由 .env 的 REALESRGAN_MODEL_PATH 配置。
    tile=0 表示不切瓦片（整图推理），显存不足时设为 400/512 等分块。"""
    load_env()
    model_path = os.environ.get('REALESRGAN_MODEL_PATH', 'models/RealESRGAN_x4plus_anime_6B.pth')
    if not os.path.isabs(model_path):
        model_path = str(Path(__file__).parent / model_path)
    return {
        'model_path': model_path,
        'tile': int(os.environ.get('REALESRGAN_TILE', '0')),
        'tile_pad': int(os.environ.get('REALESRGAN_TILE_PAD', '10')),
    }


ALLOWED_IMAGE_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
ALLOWED_TEXT_EXTENSIONS = {'txt'}


def allowed_file(filename, file_type='image'):
    """检查文件是否允许上传"""
    allowed = ALLOWED_IMAGE_EXTENSIONS if file_type == 'image' else ALLOWED_TEXT_EXTENSIONS
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in allowed


def get_image_files(upload_folder):
    """获取所有图片文件，按文件名自然排序"""
    files = []
    for filename in os.listdir(upload_folder):
        if allowed_file(filename, 'image'):
            files.append(filename)

    def natural_sort_key(s):
        return [int(c) if c.isdigit() else c.lower() for c in re.split(r'(\d+)', s)]
    files.sort(key=natural_sort_key)
    return files


def safe_filename(filename):
    """保留原始文件名中的字符（包括中文），只移除路径分隔符等危险字符"""
    filename = filename.replace('\x00', '')
    filename = os.path.basename(filename)
    filename = filename.strip(' .')
    return filename if filename else 'unnamed'


def is_within_directory(path, base_dir):
    """校验 path 是否词法上位于 base_dir 之内（防止路径遍历）。

    用 os.path.commonpath 逐段比较，避免 startswith 把 'uploads_evil'
    这类同前缀目录误判为合法。path/base_dir 都会被规范化为绝对路径。
    """
    abs_path = os.path.abspath(path)
    abs_base = os.path.abspath(base_dir)
    return os.path.commonpath([abs_path, abs_base]) == abs_base
