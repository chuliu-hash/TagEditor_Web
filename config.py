# -*- coding: utf-8 -*-
import os
import re
from pathlib import Path


def load_env(env_path='.env'):
    env_file = Path(__file__).parent / env_path
    if env_file.exists():
        with open(env_file, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, _, value = line.partition('=')
                    key = key.strip()
                    value = value.strip().strip('"').strip("'")
                    os.environ[key] = value


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


ALLOWED_IMAGE_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif'}
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
