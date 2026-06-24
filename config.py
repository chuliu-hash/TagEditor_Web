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
    model_path = os.environ.get('WD14_MODEL_PATH', 'models/wd-eva02-large-tagger-v3')
    if not os.path.isabs(model_path):
        model_path = str(Path(__file__).parent / model_path)
    return {
        'model_path': model_path,
        'general_threshold': float(os.environ.get('WD14_GENERAL_THRESHOLD', '0.3')),
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


def get_birefnet_config():
    """每次调用时重新读取 BiRefNet（ToonOut）背景移除配置。
    base_model_dir：本地 base 模型目录（含 birefnet.py + config.json + 权重），
                    trust_remote_code 加载所需，从 https://huggingface.co/ZhengPeng7/birefnet 下载。
    toonout_weights：ToonOut 动漫微调权重 .pth，从 https://huggingface.co/joelseytre/toonout 下载。"""
    load_env()
    base_model_dir = os.environ.get('BIREFNET_BASE_MODEL_DIR', 'models/birefnet-base')
    if not os.path.isabs(base_model_dir):
        base_model_dir = str(Path(__file__).parent / base_model_dir)
    toonout_weights = os.environ.get('BIREFNET_TOONOUT_WEIGHTS', 'models/toonout.pth')
    if not os.path.isabs(toonout_weights):
        toonout_weights = str(Path(__file__).parent / toonout_weights)
    return {
        'base_model_dir': base_model_dir,
        'toonout_weights': toonout_weights,
    }


def get_danbooru_config():
    """每次调用时重新读取 Danbooru wiki 抓取配置（用于标签翻译时获取英文释义作参考）。
    抓取 https://danbooru.donmai.us/wiki_pages.json?search[title]=<tag> 取 body 字段。
    国内访问需配置代理（DANBOORU_PROXY）。enabled=False 时跳过抓取，回退为纯标签名翻译。

    速率控制说明（基于 help:api 官方文档）：
    - Danbooru 读请求全局上限 10 req/s，与账号无关（认证不提高额度）。
    - delay 为主请求间隔基准，叠加 delay_jitter 范围的随机抖动。
      delay=0.15 + jitter 0~0.3 → 平均约 0.3s/请求（≈3 req/s，远低于 10 req/s 上限）。
    - page_limit 为每页请求数（wiki_pages.json 官方上限 200）。
    - pause_every_pages / pause_seconds：连续抓取多少页后强制休息，避免长任务累积风险。"""
    load_env()
    return {
        'enabled': os.environ.get('DANBOORU_ENABLED', 'true').strip().lower() in ('true', '1', 'yes'),
        'api_url': os.environ.get('DANBOORU_API_URL', 'https://danbooru.donmai.us'),
        'proxy': os.environ.get('DANBOORU_PROXY', ''),  # 如 http://127.0.0.1:7897，空表示直连
        'user_agent': os.environ.get('DANBOORU_USER_AGENT', 'TagEditorWeb/1.0'),
        'timeout': int(os.environ.get('DANBOORU_TIMEOUT', '15')),
        'delay': float(os.environ.get('DANBOORU_DELAY', '0.15')),  # 主请求间隔（秒），默认 0.15
        'delay_jitter': float(os.environ.get('DANBOORU_DELAY_JITTER', '0.3')),  # 随机抖动上限（秒）
        'page_limit': int(os.environ.get('DANBOORU_PAGE_LIMIT', '200')),  # 每页条数（wiki_pages.json 上限 200）
        'pause_every_pages': int(os.environ.get('DANBOORU_PAUSE_EVERY_PAGES', '100')),  # 每多少页休息一次
        'pause_seconds': float(os.environ.get('DANBOORU_PAUSE_SECONDS', '5')),  # 休息秒数
    }


def get_tag_db_config():
    """Danbooru 标签本地数据库配置（SQLite）。
    schema：tags(name PK, cn_name, en_wiki, cn_wiki, other_names, updated_at)
    由 build_tag_db.py 从 tags_enhanced.csv + wiki_pages.parquet 构建并增量更新。"""
    load_env()
    db_path = os.environ.get('TAG_DB_PATH', 'data/danbooru_tags.db')
    if not os.path.isabs(db_path):
        db_path = str(Path(__file__).parent / db_path)
    return {
        'db_path': db_path,
        # Danbooru 账号（认证用户有更高 API 配额，匿名受限）。留空则匿名抓取
        'username': os.environ.get('DANBOORU_USER_NAME', ''),
        'api_key': os.environ.get('DANBOORU_API_KEY', ''),
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
