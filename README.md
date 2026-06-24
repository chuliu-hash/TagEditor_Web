# TagEditor Web

标签批量编辑工具 — 基于 Flask 的 Web 应用，用于批量上传图片及对应文本标签，支持中英文双向翻译、自动打标、在线编辑保存，并内置图片编辑器（裁剪、旋转、缩放、透明转色底、Real-ESRGAN 超清放大、ToonOut 背景移除）。

## 功能

### 标签编辑（主页 `/`）

- **批量上传**：支持上传图片（PNG/JPG/JPEG/GIF/WEBP）及同名 txt 标签文件，总上传上限 256MB
- **在线编辑**：三栏布局（文件列表 / 图片预览 / 标签编辑器），支持键盘左右键导航、标签上下移动排序
- **双向翻译**：通过 OpenAI 兼容 API 进行中英标签翻译，结果持久化到 SQLite，避免重复翻译
- **自动打标**：
  - API 打标：调用视觉模型为图片生成 Danbooru 格式标签
  - WD14 本地打标：使用 ONNX 模型离线推理
- **批量操作**：全局查找替换、触发词添加（开头/末尾）、批量翻译所有未缓存标签、批量重命名（`{名称}-{宽}x{高}-{编号}`）、标签统计

### 图片编辑器（`/img_editor`）

- **Canvas 裁剪**：锁定宽高比的裁剪框，拖角缩放、拖体移动，框大小即输出分辨率
- **旋转**：顺时针旋转，自动保存覆盖原图
- **整图缩放**：保持宽高比缩放（宽高联动 + 长边预设）
- **透明转色底**：将透明背景转为指定纯色（单张或全部，自定义底色）
- **超清放大**：基于 Real-ESRGAN（anime_6B 模型）4x 超分，支持 1~4x 自定义尺寸，前端暂存→保存覆盖
- **背景移除**：基于 ToonOut（BiRefNet 动漫微调）移除背景，输出透明 PNG 或合成纯色底，前端暂存→保存覆盖

> 编辑器状态机制：裁剪/缩放/超清放大/背景移除会把结果暂存在前端（需点「保存」才覆盖原图）；旋转为自动保存；暂存态下会禁用旋转与透明转色底（避免冲突），需先保存或重置。

## 快速开始

### 环境要求

- Python 3.11
- GPU（可选，推荐）：超清放大 / 背景移除在 CUDA 上推理，CPU 也可运行但较慢

### 安装依赖

```bash
pip install -r requirements.txt
```

> Real-ESRGAN / BiRefNet 依赖 `torch`/`torchvision`（GPU 版需按 CUDA 版本从 [PyTorch 官方源](https://pytorch.org/) 安装）：
> ```bash
> # CUDA 12.1 示例
> pip install torch==2.5.1 torchvision==0.20.1 --index-url https://download.pytorch.org/whl/cu121
> ```
> `basicsr==1.4.2` 因依赖已下架的 `tb-nightly`，需用 `--no-deps` 安装并手动补齐运行时依赖（addict/future/lmdb/scipy/scikit-image/tqdm/yapf）。

### 配置

所有配置通过 `.env` 文件管理（见下方[配置说明](#配置说明)）。模型文件需自行准备：

| 功能 | 模型 | 下载地址 |
|------|------|----------|
| WD14 打标 | `model.onnx` + `selected_tags.csv` | https://huggingface.co/SmilingWolf/wd-eva02-large-tagger-v3 |
| Real-ESRGAN 超清放大 | `RealESRGAN_x4plus_anime_6B.pth` | https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.2.4/RealESRGAN_x4plus_anime_6B.pth |
| BiRefNet 背景移除 base | `birefnet.py` + `config.json` + 权重 | https://huggingface.co/ZhengPeng7/birefnet |
| ToonOut 微调权重 | `.pth` | https://huggingface.co/joelseytre/toonout |

下载后放到 `.env` 对应变量指定的路径（默认在 `models/` 下）。

### 启动

```bash
python app.py
```

访问 http://127.0.0.1:8001 即可使用。开发调试时可用 `FLASK_DEBUG=1 python app.py` 开启热重载。

## 配置说明

所有配置通过 `.env` 文件管理，修改后无需重启（热更新）。

### 翻译模型

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `LLM_API_URL` | API 地址 | `http://localhost:8080/v1/chat/completions` |
| `LLM_API_KEY` | API 密钥 | `ollama` |
| `LLM_MODEL` | 模型名称 | `qwen2.5:7b` |
| `LLM_TAG_TRANSLATE_PROMPT` | 翻译系统提示词，支持 `{src_name}`/`{dst_name}` 占位符 | — |

### 视觉模型（API 打标）

| 变量 | 说明 |
|------|------|
| `VISION_API_URL` | API 地址（需含完整路径如 `/v1/chat/completions`） |
| `VISION_API_KEY` | API 密钥 |
| `VISION_MODEL` | 模型名称 |
| `VISION_SYSTEM_PROMPT` | 打标系统提示词（`\n` 表示换行） |

### WD14 本地打标

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `WD14_MODEL_PATH` | 模型目录（需含 `model.onnx` 和 `selected_tags.csv`） | `models/wd-eva02-large-tagger-v3` |
| `WD14_GENERAL_THRESHOLD` | general 标签置信度阈值 | `0.3` |
| `WD14_CHARACTER_THRESHOLD` | character 标签置信度阈值 | `0.1` |

### Real-ESRGAN 超清放大

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `REALESRGAN_MODEL_PATH` | 模型权重 `.pth` 路径（相对路径基于项目根目录） | `models/RealESRGAN_x4plus_anime_6B.pth` |
| `REALESRGAN_TILE` | 瓦片大小（显存不足时分块推理，`0`=整图） | `0` |
| `REALESRGAN_TILE_PAD` | 瓦片边界 padding（消除拼接伪影） | `10` |

> 显存不足时（CUDA OOM），将 `REALESRGAN_TILE` 设为 `200`/`400`/`512` 启用分块推理。

### BiRefNet 背景移除（ToonOut）

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `BIREFNET_BASE_MODEL_DIR` | base 模型目录（含 `birefnet.py` + `config.json` + 权重） | `models/birefnet-base` |
| `BIREFNET_TOONOUT_WEIGHTS` | ToonOut 微调权重 `.pth` | `models/toonout.pth` |

## 数据存储

- `uploads/` — 图片及对应 txt 标签文件（如 `photo.png` ↔ `photo.txt`）
- `data/danbooru_tags.db` — Danbooru 标签本地数据库（SQLite，含英文标签/中文翻译/英文 wiki/中文 wiki/多语言别名）。由 `build_tag_db.py` 构建
- `models/` — WD14、Real-ESRGAN、BiRefNet 模型文件（需自行准备，不入库）

图片与标签通过文件名关联，标签以**逗号分隔**存储在 txt 中（如 `1girl, blue_hair, smile`）。保存时自动统一为小写并去重。

### 标签数据库（SQLite）

翻译数据统一存储在 `data/danbooru_tags.db`，schema 为 6 列：

| 列 | 含义 | 来源 |
|----|------|------|
| `name` | 英文标签名（主键，如 `blue_hair`） | CSV + parquet + Danbooru 增量 |
| `cn_name` | 中文翻译（逗号分隔多词，如 `蓝发,蓝色头发`） | CSV（LLM 翻译产物）+ 手动编辑 |
| `en_wiki` | 英文 wiki 正文（Danbooru DText 格式） | parquet + Danbooru 增量 + 手动编辑 |
| `cn_wiki` | 中文 wiki（LLM 翻译或手写） | LLM 翻译 + 手动编辑 |
| `other_names` | 多语言别名（JSON 数组，如 `["蓝发","蓝毛","青髪"]`） | parquet + Danbooru 增量 |
| `updated_at` | 最后更新时间（**增量抓取的时间锚点**，不展示） | parquet + Danbooru 增量 |

由 `build_tag_db.py` 管理：

```bash
# 1. 首次构建（从 danbooru-tag-pipeline 项目的 csv + parquet 导入）
python build_tag_db.py init --csv <tags_enhanced.csv> --parquet <wiki_pages.parquet>

# 2. 增量更新（从 Danbooru 抓取最新 wiki，需配置 DANBOORU_USER_NAME/API_KEY）
python build_tag_db.py update

# 3. 查看统计
python build_tag_db.py stats
```

翻译查询优先级：**SQLite（cn_name）→ LLM（未命中时）→ 回写 SQLite**。用户手动编辑翻译也会回写 SQLite。每个标签行右侧有「详情」按钮，可查看英文/中文 wiki：英文 wiki 和中文 wiki 均支持内联编辑保存，中文 wiki 不存在时还可一键翻译并保存。

## 项目结构

```
.
├── app.py                  # 入口，注册 Blueprint + 页面路由
├── config.py               # .env 加载、各模型配置读取、文件工具函数
├── translation.py          # 翻译 + 标签数据库读写 + 标签详情/wiki 编辑路由
├── build_tag_db.py         # 标签数据库构建与查询（init 全量 / update 增量 / stats 统计）
├── tagger.py               # WD14 预处理/加载/过滤 + API/WD14 打标路由
├── file_ops.py             # 上传/删除/清空/标签读写/静态文件/标签统计路由
├── tag_operations.py       # 触发词/查找替换路由
├── image_editor.py         # 图片编辑路由（保存/透明转色底/超清放大/背景移除）
├── realesrgan_utils.py     # RealESRGANer 推理类（从 Real-ESRGAN 提取）
├── birefnet_utils.py       # BiRefNet/ToonOut 背景移除推理
├── sse_utils.py            # SSE 事件格式化工具
├── templates/
│   ├── tag_editor.html     # 标签编辑主页（三栏布局）
│   ├── image_editor.html   # 图片编辑器
│   └── danbooru_wiki.html  # Danbooru 标签查询页（搜索/详情/wiki 内联编辑）
├── data/                   # 标签数据库 danbooru_tags.db（运行时生成）
├── uploads/                # 图片 + 标签（运行时生成）
├── models/                 # 模型权重（需自行下载）
└── .env                    # 配置文件（不入库）
```

## 性能参考

RTX 4060 Laptop + torch 2.5.1+cu121（FP32）下的单张推理耗时：

| 操作 | 耗时 |
|------|------|
| Real-ESRGAN 4x 超分（1024×576 → 4096×2304） | ~4 秒 |
| ToonOut 背景移除（1024×576） | ~2 秒 |

CPU 推理会慢得多（背景移除可达数十秒），建议有 GPU 时启用。

## 备注

- 所有 API 均为 OpenAI 兼容格式，支持 Ollama、DeepSeek 等本地或远程服务
- 自动打标仅处理**无标签或空标签**的图片，已有标签的跳过
- 透明转色底仅处理含 alpha 通道的图片，并跳过 GIF
- WD14、Real-ESRGAN、BiRefNet 的重依赖按需懒加载，未安装相关库时仅禁用对应功能，不影响其余功能
- 两个页面通过 URL hash 互相跳转并保持当前图片位置
