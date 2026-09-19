# -*- coding: utf-8 -*-
import os
from flask import Flask, render_template
from config import load_env, get_image_files
from translation import translation_bp
from tagger import tagger_bp
from file_ops import file_ops_bp
from tag_operations import tag_ops_bp
from image_editor import image_editor_bp
from prompt_tool import prompt_tool_bp
import logging

# 日志必须在其它模块开始打日志之前配好。放在这里（import 之后、建 app 之前）：
# 各模块的 logger 是模块级 `logging.getLogger(__name__)`，本身不触发输出，
# 所以只要在第一次真正写日志（下面 load_env / 预热线程）之前配置即可。
from logging_setup import setup_logging
setup_logging()

log = logging.getLogger(__name__)

app = Flask(__name__)

UPLOAD_FOLDER = 'uploads'
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 256 * 1024 * 1024  # 256MB

if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

load_env()

app.register_blueprint(translation_bp)
app.register_blueprint(tagger_bp)
app.register_blueprint(file_ops_bp)
app.register_blueprint(tag_ops_bp)
app.register_blueprint(image_editor_bp)
app.register_blueprint(prompt_tool_bp)


@app.route('/')
def index():
    """Danbooru 标签查询页面（wiki 风格，搜索本地标签数据库）"""
    return render_template('danbooru_wiki.html')


@app.route('/tag_editor')
def tag_editor():
    """标签编辑主页"""
    images = get_image_files(app.config['UPLOAD_FOLDER'])
    return render_template('tag_editor.html', images=images, image_count=len(images))


@app.route('/img_editor')
def editor():
    """图片编辑器页面"""
    images = get_image_files(app.config['UPLOAD_FOLDER'])
    return render_template('image_editor.html', images=images, image_count=len(images))


@app.route('/prompt_tool')
def prompt_tool_page():
    """提示词优化器页面（图片 + 提示词 + 效果描述 → 结合标签库重调提示词）"""
    images = get_image_files(app.config['UPLOAD_FOLDER'])
    return render_template('prompt_tool.html', images=images, image_count=len(images))


def _preheat_cooc():
    """后台预热共现数据（首次 _load_cooc_data 要 3~6s，纯 CPU/磁盘、不占显存）。
    预热与首次请求竞争时只是重复读一次，无正确性问题，故无条件开。"""
    import threading
    import time

    def warmup():
        time.sleep(2)
        try:
            from config import get_tag_db_config
            from llm_pipeline import _load_cooc_data
            log.info("[预热] 后台加载共现数据...")
            data = _load_cooc_data(get_tag_db_config()['db_path'], top_k=8)
            log.info(f"[预热] 共现数据预热完成（{len(data)} 个标签有共现关系）")
        except Exception as e:
            log.error(f"[预热] 共现数据预热失败（不影响应用，首次调用会重试）: {e}")

    threading.Thread(target=warmup, daemon=True).start()


def _preheat_models():
    """后台预热常驻模型，减少用户首次操作等待。
    仅预热轻量模型（WD14 ONNX），重型模型（BiRefNet+ToonOut ~1.3GB 显存、Real-ESRGAN）
    保持懒加载，避免启动即占满显存、且用户未必用到。

    容错：模型文件缺失/损坏时仅打印警告，不影响应用启动与该功能（首次使用时仍会按懒加载报错）。
    线程：daemon=True，主进程退出时自动结束，不阻塞 Flask 启动。"""
    import threading
    import time

    def warmup():
        # 略等 Flask 起来，避免预热与首次请求竞争 GPU/IO
        time.sleep(2)
        try:
            from config import get_wd14_config
            from tagger import wd14_load_model
            cfg = get_wd14_config()
            onnx_path = os.path.join(cfg['model_path'], 'model.onnx')
            if not os.path.exists(onnx_path):
                log.warning(f"[预热] WD14 模型不存在（{onnx_path}），跳过，首次打标时按懒加载处理")
                return
            log.info(f"[预热] 后台加载 WD14 模型...")
            wd14_load_model(cfg['model_path'])
            log.info("[预热] WD14 模型预热完成，首次打标将即时响应")
        except Exception as e:
            log.error(f"[预热] WD14 预热失败（不影响应用，首次打标会重试）: {e}")

    threading.Thread(target=warmup, daemon=True).start()


if __name__ == '__main__':
    # 可选后台预热：PRELOAD_MODELS=true 时启动后预热轻量模型（默认关，避免改变默认行为）
    # 考虑到 GPU 显存与启动资源，重型模型始终懒加载。
    if os.environ.get('PRELOAD_MODELS', 'false').strip().lower() in ('true', '1', 'yes'):
        _preheat_models()
    # 共现数据预热只读 parquet（3~6s，不占显存）——否则提示词优化器首次运行要多等这么久
    _preheat_cooc()
    # debug 默认关闭（生产避免暴露 Werkzeug 调试器）；通过 FLASK_DEBUG=1 显式开启
    app.run(debug=os.environ.get('FLASK_DEBUG', '0') == '1', port=8001)
