# TagEditor Web

标签批量编辑工具 — 基于 Flask 的 Web 应用，用于批量上传图片及对应文本标签，支持中英文双向翻译和在线编辑保存。

## 功能

- **批量上传**：支持上传图片（PNG/JPG/JPEG/GIF）及同名 txt 标签文件，总上传上限 256MB
- **在线编辑**：三栏布局（文件列表 / 图片预览 / 标签编辑器），支持键盘左右键导航、标签拖拽排序
- **双向翻译**：通过 OpenAI 兼容 API 进行中英标签翻译，带本地 JSON 缓存，避免重复翻译
- **自动打标**：
  - API 打标：调用视觉模型（如 Qwen3-VL）为图片生成 Danbooru 格式标签
  - WD14 本地打标：使用 ONNX 模型离线推理
- **批量操作**：全局查找替换、触发词添加（开头/末尾）、批量翻译所有未缓存标签

## 快速开始

### 安装依赖

```bash
pip install -r requirements.txt
```

### 配置

在 .env 文件中填写翻译及打标配置。

### 启动

```bash
python app.py
```

访问 http://127.0.0.1:5000 即可使用。

## 数据存储

- `uploads/` — 图片及对应 txt 标签文件（如 `photo.png`-`photo.txt`）
- `translation_cache.json` — 标签翻译缓存（英文 key → 中文 value）

## 环境要求

- Python 3.11
- 所有 API 均为 OpenAI 兼容格式，支持 Ollama、DeepSeek 等本地或远程服务
- WD14 模型需单独下载.onnx和.csv文件，放到 `WD14_MODEL_PATH` 指定目录
