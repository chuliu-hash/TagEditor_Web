# -*- coding: utf-8 -*-
import os
import re
from pathlib import Path


_env_mtime = None

_prompts_cache = None  # (签名, prompts dict)；签名变化时重新读取


def load_prompts(prompts_dir='prompts'):
    """按需读取提示词目录：每个 .txt 文件对应一个提示词，文件名（去 .txt）为 key。

    通过「目录 mtime + 各文件 (名称, mtime, size)」签名检测变化：
    - 新增/删除文件或修改文件内容都会改变签名，触发重新读取（热更新）
    - 文件内容整读（含 # 开头行，不设注释语法），.strip() 后作为提示词
    目录不存在时返回空 dict，由调用方回退内置默认提示词。
    """
    global _prompts_cache
    pdir = Path(__file__).parent / prompts_dir
    try:
        dir_mtime = pdir.stat().st_mtime
    except OSError:
        dir_mtime = None
    files = []
    sig_entries = []
    if pdir.is_dir():
        for f in sorted(pdir.glob('*.txt')):
            try:
                st = f.stat()
            except OSError:
                continue
            files.append(f)
            sig_entries.append((f.name, st.st_mtime, st.st_size))
    signature = (dir_mtime, tuple(sig_entries))
    # 签名未变化则跳过磁盘读取
    if _prompts_cache is not None and _prompts_cache[0] == signature:
        return _prompts_cache[1]
    prompts = {}
    for f in files:
        prompts[f.stem] = f.read_text(encoding='utf-8').strip()
    _prompts_cache = (signature, prompts)
    return prompts


def get_prompt(key, default=''):
    """获取提示词目录中 key（文件名去 .txt）对应的提示词；缺失时返回 default"""
    prompts = load_prompts()
    if key in prompts and prompts[key]:
        return prompts[key]
    return default


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


# 本地部署的 OpenAI 兼容端点（Ollama / LM Studio / vLLM / llama.cpp 等）不校验 Key，
# 但 openai SDK 2.x 在 api_key 为空字符串时同样抛 OpenAIError（_client.py 判定的是
# `not self.api_key`，不只是 None），故统一喂一个占位串。需要鉴权的云端端点照旧在
# .env 里填 LLM_API_KEY / VISION_API_KEY。
API_KEY_PLACEHOLDER = 'not-needed'


def resolve_api_key(raw_key):
    """把空 API Key 归一化为占位串。本地端点无需配置 key，云端端点照旧透传。"""
    return (raw_key or '').strip() or API_KEY_PLACEHOLDER


def get_llm_config():
    """每次调用时重新读取 .env 配置"""
    load_env()
    return {
        'api_url': os.environ.get('LLM_API_URL', 'http://localhost:8080/v1'),
        'api_key': resolve_api_key(os.environ.get('LLM_API_KEY', '')),
        'model': os.environ.get('LLM_MODEL', 'qwen2.5:7b'),
    }


def get_vision_config():
    """每次调用时重新读取视觉模型配置（用于 VLM 自然语言描述生成）

    api_key 经 resolve_api_key 归一化：本地部署（Ollama 等）不配 VISION_API_KEY
    也能直接用，云端端点填了照旧透传。消费方（tagger.py / prompt_tool.py）
    直接把该值喂给 OpenAI() 即可。
    """
    load_env()
    return {
        'api_url': os.environ.get('VISION_API_URL', ''),
        'api_key': resolve_api_key(os.environ.get('VISION_API_KEY', '')),
        'model': os.environ.get('VISION_MODEL', ''),
        # max_tokens 是 reasoning_content + content 的共享额度，不是只算正式回答。
        # 推理模型（DeepSeek 等）思考模式默认开启，512 会被思考吃光 → content 为空。
        'max_tokens': int(os.environ.get('VISION_MAX_TOKENS', '1024')),
        # 'off' = 关闭思考（本任务只需 2~3 个短句，思考纯烧时间和钱；
        # 且思考模式下服务端会静默忽略 temperature，关掉后 temperature 才真正生效）
        'thinking': os.environ.get('VISION_THINKING', 'off').strip().lower(),
    }


def get_caption_config():
    """每次调用时重新读取 VLM 自然语言描述生成配置"""
    load_env()
    return {
        'reference_tags': os.environ.get('CAPTION_REFERENCE_TAGS', 'true').strip().lower() in ('true', '1', 'yes'),
        'save_format': os.environ.get('CAPTION_SAVE_FORMAT', 'txt'),  # txt 覆盖标签 / separate 另存 .caption.txt
    }


# ── 提示词优化器（/prompt_tool）的内置参数 ──────────────────────────────────
# 这些是「调好就不动」的实现细节（检索口径、token 预算、图片编码上限），不是用户配置项，
# 故写死在代码里。.env 只留三个会真的按需调整的：PROMPT_MAX_TOKENS / THINKING / TIMEOUT。
_PROMPT_TEMPERATURE = 0.4        # 结构化改写求稳，不要 0.7 的发散
_PROMPT_MAX_INPUT_TAGS = 60      # 输入标签注入上限
_PROMPT_WIKI_CHARS = 240         # 每条标签注入的 en_wiki 字符数（实测均值 546、最大 30393，必须截断）
_PROMPT_COOC_TOPK = 8            # 共现推荐条数
_PROMPT_COOC_NSFW = 'hide'       # 过滤 tags.nsfw=1 的推荐词（hide | show）
# 候选自身 post_count 下限：太冷门的标签 lift 虚高（实测 three-tone_hair 只有 237 条却霸榜）
_PROMPT_COOC_MIN_POST = 500
# 候选只取通用类：22445 条角色标签会淹没风格/光影类候选
_PROMPT_CANDIDATE_CATEGORIES = {0}
_PROMPT_IMAGE_MAX_BYTES = 4194304   # 超过则用 Pillow 缩到最长边 _PROMPT_IMAGE_MAX_SIDE 并重编码
_PROMPT_IMAGE_MAX_SIDE = 1536


def get_prompt_tool_config():
    """每次调用时重新读取提示词优化器（/prompt_tool）配置。

    模型端点复用 VISION_API_URL/KEY/MODEL（多模态），只有三个参数走 .env：
    - max_tokens：输出是逐条 diff JSON + 一段自然语言描述，条目多；且与思考共享额度
    - thinking：结构化 JSON 任务，思考纯烧钱且会静默吃掉 temperature
    - timeout：单次调用超时（tagger.py 的 VLM 路径没传 timeout，这里必须显式传）
    其余检索/编码参数见上方 _PROMPT_* 常量。
    """
    load_env()
    vision = get_vision_config()
    return {
        'api_url': vision['api_url'],
        'api_key': vision['api_key'],
        'model': vision['model'],
        'max_tokens': int(os.environ.get('PROMPT_MAX_TOKENS', '8192')),
        'thinking': os.environ.get('PROMPT_THINKING', 'off').strip().lower(),
        'timeout': int(os.environ.get('PROMPT_TIMEOUT', '180')),
        'temperature': _PROMPT_TEMPERATURE,
        'max_input_tags': _PROMPT_MAX_INPUT_TAGS,
        'wiki_chars': _PROMPT_WIKI_CHARS,
        'cooc_topk': _PROMPT_COOC_TOPK,
        'cooc_nsfw': _PROMPT_COOC_NSFW,
        'cooc_min_post': _PROMPT_COOC_MIN_POST,
        'candidate_categories': _PROMPT_CANDIDATE_CATEGORIES,
        'image_max_bytes': _PROMPT_IMAGE_MAX_BYTES,
        'image_max_side': _PROMPT_IMAGE_MAX_SIDE,
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
