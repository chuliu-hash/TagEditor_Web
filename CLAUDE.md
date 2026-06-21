# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

标签批量编辑工具 — 基于 Flask 的 Web 应用，用于批量上传图片及对应文本标签，支持中英文双向翻译（OpenAI 兼容大模型 API）和在线编辑保存。额外提供图片编辑器页面（裁剪、旋转、透明转白底、Real-ESRGAN 超清放大）。

## Commands

```bash
# 安装依赖（需在 tageditor conda 环境中，Python 3.11）
conda activate tageditor
pip install -r requirements.txt

# 启动开发服务器（默认 debug 关闭；开发时用 FLASK_DEBUG=1 python app.py 开启，访问 http://127.0.0.1:8001）
python app.py
```

无测试框架、无构建步骤。

## Architecture

后端使用 Flask Blueprint 拆分为多个模块，前端为两个页面（标签编辑 + 图片编辑）。

### 后端模块

| 文件 | 职责 |
|------|------|
| `app.py` | 入口，注册 Blueprint + 首页/编辑器路由 |
| `config.py` | .env 加载、各模型配置读取、文件工具函数（`allowed_file`/`get_image_files`/`safe_filename`） |
| `translation.py` | 翻译缓存（`load_cache`/`save_cache`/`lookup_tag`）+ 翻译路由 |
| `tagger.py` | WD14 预处理/模型加载/过滤 + API 打标 + WD14 本地打标路由 |
| `file_ops.py` | 上传/删除/清空/标签读写/静态文件/标签统计路由 |
| `tag_operations.py` | 触发词/查找替换路由 |
| `image_editor.py` | 图片编辑：保存编辑后图片（multipart 二进制直传转存 PNG）、批量透明转色底、Real-ESRGAN 超清放大 |
| `realesrgan_utils.py` | RealESRGANer 推理类（从 Real-ESRGAN 项目提取，仅推理部分，去掉训练用线程类和网络下载逻辑） |
| `sse_utils.py` | 共享 SSE 事件格式化工具（`sse_event(type, data)`） |

所有 Blueprint 通过 `current_app.config['UPLOAD_FOLDER']` 获取上传目录，无数据库，数据全部以文件形式存储在 `uploads/` 目录下。

### 前端页面

| 文件 | 功能 |
|------|------|
| `templates/tag_editor.html` | 标签编辑主页面：三栏布局（文件列表 → 图片预览 → 标签编辑器） |
| `templates/image_editor.html` | 图片编辑器：Canvas 裁剪（点「裁剪」按钮进入模式，锁宽高比框可拖动/缩放，框大小即输出分辨率，参数条右侧「确定裁剪」执行）、旋转、整图等比缩放（宽高联动+长边预设，重采样有损）、透明转色底（底色可选，单张/全部）、超清放大（Real-ESRGAN anime_6B 4x 超分+自定义尺寸 1~4x，预设/宽高联动，前端暂存→保存覆盖）、文件列表删除、保存 |

两页面通过 URL hash 互相跳转并保持当前图片位置。

### SSE 流式模式

自动打标（API/WD14）、批量翻译、批量透明转白底使用 SSE 流式推送进度：

- 后端：`Response(generator(), mimetype='text/event-stream')`，使用 `sse_utils.sse_event()` 格式化
- 前端：`fetch()` + `ReadableStream` 消费 POST 流（非 EventSource，因为需要 POST）
- 事件类型：`progress`（进度）、`error`（单项失败）、`complete`（全部完成）、`fatal`（致命错误）
- 前置校验（如无内容可处理）仍返回普通 JSON，前端通过 Content-Type 区分

### Key Routes

| 路由 | 方法 | 功能 |
|------|------|------|
| `/` | GET | 标签编辑主页面 |
| `/editor` | GET | 图片编辑器页面 |
| `/upload` | POST | 批量上传图片和 txt 文件 |
| `/uploads/<filename>` | GET | 静态文件访问（图片） |
| `/get_caption/<name>` | GET | 获取图片对应的标签 + 翻译缓存（合并返回） |
| `/save_caption/<name>` | POST | 保存编辑后的标签 + 翻译缓存更新 |
| `/translate_tags` | POST | 批量标签翻译，带缓存写入 |
| `/lookup_cache` | POST | 查询翻译缓存 |
| `/batch_translate` | POST | SSE 流式批量翻译未缓存标签 |
| `/auto_tag` | POST | SSE 流式 API 视觉模型自动打标（仅处理无标签图片） |
| `/auto_tag_wd14` | POST | SSE 流式 WD14 本地 ONNX 自动打标（仅处理无标签图片） |
| `/process_image` | POST | 保存编辑后图片（前端 multipart 二进制直传，始终转存为 PNG，非 PNG 时删旧文件，不支持 GIF） |
| `/batch_alpha_to_white` | POST | 透明转色底（`?color=hex` 自定义底色；`?target=<文件名>` 单张返回 JSON，缺省处理全部含 alpha 图片走 SSE 流式） |
| `/upscale_realesrgan` | POST | Real-ESRGAN 超清放大（单张，`?target=<文件名>&w=<int>&h=<int>`，固定 anime_6B 4x 超分再 LANCZOS4 缩放到目标尺寸，w/h 范围 [原图, 原图×4]，成功返回 PNG 二进制不写盘，失败返回 JSON） |
| `/prepend_tags` | POST | 为所有标签文件添加触发词（位置 `start`/`end`） |
| `/find_replace` | POST | 全局查找替换标签（完整标签精确匹配，支持 `preview` 预览模式） |
| `/tag_stats` | GET | 所有标签统计（数量 + 翻译，降序） |
| `/delete/<image_name>` | POST | 删除图片及对应 txt |
| `/clear_all` | POST | 清空所有文件 |

### 数据模型

- 图片与标签通过文件名关联：`photo.png` 对应 `photo.txt`。
- 标签以**逗号分隔**存储在 txt 文件中（如 `1girl, blue hair, smile`），前端拆分为标签列表逐行编辑。
- 保存时自动去重（大小写不敏感）；标签统计与查找替换**保留原始大小写**（完整标签精确匹配，区分大小写）。
- 文件按自然排序显示。支持 PNG/JPG/JPEG/GIF/WEBP 格式（见 `config.ALLOWED_IMAGE_EXTENSIONS`），总上传上限 256MB。

### 翻译缓存

`translation_cache.json`（项目根目录）存储标签级翻译映射（key 全部 lowercase），统一为**英文 key → 中文 value**（双向翻译都会归一到英文 key：en→zh 直接用英文 key；zh→en 用翻译出的英文结果作 key）。

- 内存缓存：`load_cache()` 首次读磁盘后缓存在 `_cache_memory`，通过缓存文件 mtime 检测失效（多进程下其他进程改了文件会重读）；`save_cache()` 临时文件 + `os.replace` 原子写入，同步更新内存与磁盘。
- 反向索引：`_reverse_index` 字典实现 O(1) 反向查找。
- 保存标签时，前端收集 tag→translation 对并提交，后端与缓存对比后更新。

### 前端关键机制

- **脏状态追踪**：`savedTags` 记录上次保存/加载的标签文本，`isDirty()` 对比当前状态。切换图片、关闭页面时触发确认。
- **快速切换防护**：`loadSeq` 序列号丢弃过期的 `loadCaption` 响应，防止异步竞态。`doNavigate` 同步重置状态防止误报脏状态。
- **拖拽排序**：标签行支持拖拽重新排序。
- **面板折叠**：预览面板和编辑面板可独立隐藏/显示。
- **图片预览自适应**：小图放大填满预览区，保持宽高比。
- **标签统计弹窗**：合并了查找替换功能，查找用字符匹配筛选，替换用完整标签精确匹配。
- **添加行定位**：添加标签时插入到当前聚焦行的下方。

### 后端模式

- **错误处理**：所有端点失败时返回 JSON `{ "error": "..." }`，附带合适的 HTTP 状态码。
- **安全**：`safe_filename()` 保留中文字符但移除危险字符；删除/保存图片路径通过 `config.is_within_directory()`（基于 `os.path.commonpath()` 逐段比较，避免 `startswith` 把 `uploads_evil` 这类同前缀目录误判为合法）防止路径遍历。
- **懒加载**：WD14 相关依赖（onnxruntime、cv2、pandas）在函数内按需 import，未安装时仅禁用 WD14 功能。
- **配置热更新**：每次 API 调用触发 `config.load_env()`，通过 `.env` 文件 mtime 检测按需重读磁盘（未变化则跳过）。
- **自动打标范围**：`/auto_tag`、`/auto_tag_wd14` 仅处理**无标签或空标签**的图片（`tagger._collect_untagged`），已有标签的跳过；`/batch_alpha_to_white` 仅处理含 alpha 通道的图片并跳过 GIF。
- **SSE 禁缓冲**：所有 SSE 响应统一带 `Cache-Control: no-cache` 与 `X-Accel-Buffering: no` 头，防止 Nginx/代理缓冲导致前端收不到流。
- **WD14 设备**：ONNX session 优先 `CUDAExecutionProvider`，回退 `CPUExecutionProvider`；模型按 `model_path` 缓存到全局 `_wd14_model_cache`，切换路径才重新加载。

## Configuration

所有配置通过 `.env` 文件管理。翻译和视觉的系统提示词无硬编码默认值，但 API 地址、密钥、模型名等有默认值。每次 API 调用时重新读取 `.env`，支持热更新。

### 翻译模型

- `LLM_API_URL`：API 地址（默认 `http://localhost:8080/v1/chat/completions`）
- `LLM_API_KEY`：API 密钥
- `LLM_MODEL`：模型名称（默认 `qwen2.5:7b`）
- `LLM_TAG_TRANSLATE_PROMPT`：翻译系统提示词，支持 `{src_name}` 和 `{dst_name}` 占位符

### 视觉模型（API 打标）

- `VISION_API_URL`：API 地址（需包含完整路径如 `/v1/chat/completions`）
- `VISION_API_KEY`：API 密钥
- `VISION_MODEL`：模型名称
- `VISION_SYSTEM_PROMPT`：打标系统提示词（`\n` 表示换行）

### WD14 本地打标

- `WD14_MODEL_PATH`：本地模型目录路径（需包含 `model.onnx` 和 `selected_tags.csv`）
- `WD14_GENERAL_THRESHOLD`：general 标签置信度阈值（默认 0.35）
- `WD14_CHARACTER_THRESHOLD`：character 标签置信度阈值（默认 0.1）
- `WD14_EXCLUDED_TAGS`：逗号分隔的排除标签列表

模型缓存为全局变量 `_wd14_model_cache`，首次加载后常驻内存。预处理流程：RGBA→白底转换 → 等比缩放+填充为 448×448 正方形。推理按 `WD14_BATCH_SIZE=8` 攒批一次 `session.run`（减少单张固定开销），预处理失败的单张跳过、不进 batch，进度与错误仍逐张推送。
