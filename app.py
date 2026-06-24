# -*- coding: utf-8 -*-
import os
from flask import Flask, render_template
from config import load_env, get_image_files
from translation import translation_bp
from tagger import tagger_bp
from file_ops import file_ops_bp
from tag_operations import tag_ops_bp
from image_editor import image_editor_bp

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


@app.route('/')
def index():
    """主页"""
    images = get_image_files(app.config['UPLOAD_FOLDER'])
    return render_template('tag_editor.html', images=images, image_count=len(images))


@app.route('/img_editor')
def editor():
    """图片编辑器页面"""
    images = get_image_files(app.config['UPLOAD_FOLDER'])
    return render_template('image_editor.html', images=images, image_count=len(images))


@app.route('/danbooru')
def danbooru():
    """Danbooru 标签查询页面（wiki 风格，搜索本地标签数据库）"""
    return render_template('danbooru_wiki.html')


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
                print(f"[预热] WD14 模型不存在（{onnx_path}），跳过，首次打标时按懒加载处理")
                return
            print(f"[预热] 后台加载 WD14 模型...")
            wd14_load_model(cfg['model_path'])
            print("[预热] WD14 模型预热完成，首次打标将即时响应")
        except Exception as e:
            print(f"[预热] WD14 预热失败（不影响应用，首次打标会重试）: {e}")

    threading.Thread(target=warmup, daemon=True).start()


if __name__ == '__main__':
    # 可选后台预热：MODEL_PRELOAD=true 时启动后预热轻量模型（默认关，避免改变默认行为）
    # 考虑到 GPU 显存与启动资源，重型模型始终懒加载。
    if os.environ.get('MODEL_PRELOAD', 'false').strip().lower() in ('true', '1', 'yes'):
        _preheat_models()
    # debug 默认关闭（生产避免暴露 Werkzeug 调试器）；通过 FLASK_DEBUG=1 显式开启
    app.run(debug=os.environ.get('FLASK_DEBUG', '0') == '1', port=8001)
