# -*- coding: utf-8 -*-
import os
from flask import Flask, render_template
from config import load_env, get_image_files
from translation import translation_bp
from tagger import tagger_bp
from file_ops import file_ops_bp
from tag_operations import tag_ops_bp

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


@app.route('/')
def index():
    """主页"""
    images = get_image_files(app.config['UPLOAD_FOLDER'])
    return render_template('index.html', images=images, image_count=len(images))


if __name__ == '__main__':
    app.run(debug=True)
