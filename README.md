# TagEditor Web

标签批量编辑工具 — 基于 Flask 的 Web 应用，用于批量上传图片及对应文本标签，支持中英文双向翻译（OpenAI 兼容大模型 API）、自动打标、在线编辑保存，并内置图片编辑器（裁剪、旋转、缩放、透明转色底、Real-ESRGAN 超清放大、ToonOut 背景移除）、本地 WD14 自动打标、VLM 自然语言描述生成，以及以本地标签库为词表的提示词优化器。

## 功能

### 标签编辑（`/tag_editor`）

- **批量上传**：支持上传图片（PNG/JPG/JPEG/GIF/WEBP）及同名 txt 标签文件，总上传上限 256MB
- **在线编辑**：三栏布局（文件列表 / 图片预览 / 标签编辑器），标签可上下移动排序
- **键盘快捷键**（页内按 `?` 查看）：`A` 新增标签、`S` 保存、`J`/`K` 或 `←`/`→` 切图、`↑`/`↓` 在标签行间移动（输入框内也能用）、输入框内 `Enter` 另起一条、输入框为空时 `Backspace` 删掉该行并退回上一条
- **翻译列可点击**：无翻译的行显示「无翻译，点击处理」，点开标签详情；主库未收录的标签会说明原因并给出一键跳转到「新标签」录入入口
- **在线翻译**：通过 OpenAI 兼容 API 进行英文→中文标签翻译，结果持久化到 SQLite 显示为只读
- **自动打标**：
  - WD14 本地打标：使用 ONNX 模型离线推理（CUDA 优先）
  - VLM 自然语言描述：将结构化标签"翻译"为连贯英文描述，存为 `.nl.txt`
- **批量操作**：全局查找替换、触发词添加（开头/末尾）、批量重命名（`{名称}-{宽}x{高}-{编号}`）、标签统计、按模式清空

每张图片关联两个文本文件：

- `{name}.txt` — WD14 标签（结构化标签，逗号分隔）
- `{name}.nl.txt` — VLM 自然语言描述（连贯英文句子）

### 图片编辑器（`/img_editor`）

- **图片导航**：`←`/`→` 或 `K`/`J` 切换图片（有未保存编辑时会先确认）
- **Canvas 裁剪**：锁定宽高比的裁剪框，拖角缩放、拖体移动，框大小即输出分辨率
- **旋转**：顺时针旋转，自动保存覆盖原图
- **整图缩放**：保持宽高比缩放（宽高联动 + 长边预设）
- **透明转色底**：将透明背景转为指定纯色（单张或全部，自定义底色）
- **超清放大**：基于 Real-ESRGAN（anime_6B 模型）4x 超分，支持 1~4x 自定义尺寸，前端暂存→保存覆盖
- **背景移除**：基于 ToonOut（BiRefNet 动漫微调）移除背景，输出透明 PNG 或合成纯色底，前端暂存→保存覆盖

> 旋转与透明转色底会自动落盘，与「暂存→保存」冲突，因此在有暂存修改时被禁用。

### Danbooru 标签查询（`/`）

- **标签搜索**：FTS5 全文索引（trigram），支持空格/下划线/连字符兼容搜索；下拉结果支持 `↑`/`↓` 选择、`Enter` 打开、`Esc` 关闭
- **标签详情**：中文名/英文名/别名/分组/使用次数、英文 wiki（只读）、中文 wiki（内联编辑）
- **深度翻译**：单条标签的 LLM 三层深度翻译（中文名 + 中文 wiki + NSFW 标记）
- **锁定保护**：中文名和中文 wiki 可独立锁定，锁定后深度翻译和手动编辑均无法覆盖
- **共现推荐**：显示与当前标签关联度高的其他标签
- **标签组**：爬取并浏览 Danbooru 标签组体系
- **浏览记录**：纯前端（`localStorage`），新→旧最多 200 条
- **增量同步**：从 Danbooru 上游同步新标签和 wiki 更新
- **随机推荐**：首页显示约 60 条随机热门标签，可点击查看详情
- **用户新标签**：打标中遇到、主库未收录的标签在此手动维护与翻译

### 提示词优化器（`/prompt_tool`）

图片（可空）+ 当前提示词（可空）+ 中文优化要求（必填）→ 标签 diff + 改写后的自然语言描述。

- **意图由模型自决**：用户的要求可能是精炼、清理假标签、按图校正、调整动作等，不写死关键词表去猜
- **本地标签库当词表与校验器**：四工具零 LLM 检索（`search_tags` / `tag_detail` / `cooc` / `tag_groups`），压住模型编造标签的倾向
- **只产出到页面**，不写入任何文件

## 测试

```bash
python test_invariants.py
```

25 个用例，只用标准库、不需要网络或标签库。覆盖的是**静默失败**类的不变量（翻译合并去重/拆全角、提示词切分不炸假标签、API Key 归一化、`lookup_tags` 只查主表、原子写、批量操作幂等与精确匹配、`clear_all` 危险默认值防线，以及若干源码级约定）。改动相关模块后跑一遍即可。

## 快速开始

### 环境要求

- Python 3.11（conda 环境 `tageditor`）
- GPU（可选，推荐）：超清放大 / 背景移除 / WD14 在 CUDA 上推理，CPU 也可运行但较慢

### 一键脚本（Windows）

```
setup.bat    安装依赖（可重复执行，已装的会跳过）
run.bat      启动服务并自动打开浏览器
```

`setup.bat` 除了跑 `pip install -r requirements.txt`，还处理三个 pip 单独搞不定的包：`torch`（走 CUDA 专用源）、`basicsr`（依赖已下架的 `tb-nightly`，需 `--no-deps` 再补运行时依赖）、`onnxruntime-gpu`（需要额外的 index-url）。它**不会降级**你已经装好的包 —— 比如 `requirements.txt` 钉的是 `onnxruntime-gpu==1.18.0`，若你装的更高版本且能用，脚本会跳过而不是为了对齐版本降级。

装完会跑一次 `setup_check.py` 自检，逐项报告哪个功能可用、哪个会降级（例如 onnxruntime 拿不到 cuDNN 时会回落到 CPU，WD14 会慢约 10 倍）。自检**只报告不修改环境**：

```bash
python setup_check.py
```

### 手动安装

```bash
conda activate tageditor
pip install -r requirements.txt
```

> Real-ESRGAN / BiRefNet 依赖 `torch`/`torchvision`（GPU 版需按 CUDA 版本从 [PyTorch 官方源](https://pytorch.org/) 安装）：
> ```bash
> # CUDA 12.1 示例
> pip install torch==2.5.1 torchvision==0.20.1 --index-url https://download.pytorch.org/whl/cu121
> ```
> `basicsr==1.4.2` 因依赖已下架的 `tb-nightly`，需用 `--no-deps` 安装并手动补齐运行时依赖（addict/future/lmdb/scipy/scikit-image/tqdm/yapf）。
> 装完后 `import basicsr` 仍可能报 `No module named 'torchvision.transforms.functional_tensor'` —— 这是 basicsr 1.4.2 与 torchvision 0.20+ 的已知不兼容，应用侧已在 `realesrgan_utils.py` 顶部注入兼容垫片，**不影响运行**（自检脚本也复现了同样的导入顺序，所以不会误报）。

### 配置

复制 `.env.example` 为 `.env` 并填入实际值：

```bash
cp .env.example .env      # Windows: copy .env.example .env
```

模型文件需自行准备：

| 功能 | 模型 | 下载地址 |
|------|------|----------|
| WD14 打标 | `model.onnx` + `selected_tags.csv` | https://huggingface.co/SmilingWolf/wd-eva02-large-tagger-v3 |
| Real-ESRGAN 超清放大 | `RealESRGAN_x4plus_anime_6B.pth` | https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.2.4/RealESRGAN_x4plus_anime_6B.pth |
| BiRefNet 背景移除 base | `birefnet.py` + `config.json` + 权重 | https://huggingface.co/ZhengPeng7/BiRefNet |
| ToonOut 微调权重 | `.pth` | https://huggingface.co/joelseytre/toonout |

下载后放到 `.env` 对应变量指定的路径（默认在 `models/` 下）。

提示词统一放在 `prompts/` 目录，每个提示词一个 `.txt` 文件，**缺失即报错、无内置默认**：

| 文件 | 用途 |
|------|------|
| `vlm_caption.txt` | VLM 自然语言描述（必填） |
| `llm_entity.txt` / `llm_general.txt` / `llm_fallback.txt` | LLM 深度翻译三层系统提示词 |
| `rules_tag_groups.txt` / `rules_cooc.txt` | 注入的规则片段 |
| `prompt_planner.txt` / `prompt_adjust.txt` / `prompt_repair.txt` | 提示词优化器三轮调用 |

### 启动

```
run.bat              # Windows 一键启动（自动开浏览器）
```

或手动：

```bash
python app.py
```

访问 http://127.0.0.1:8001 即可使用。开发调试时可用 `FLASK_DEBUG=1 python app.py` 开启热重载。第一次启动若没有 `.env`，`run.bat` 会从 `.env.example` 复制一份并打开记事本让你填。

### 构建标签数据库

```bash
# 1. 首次构建（从 danbooru-tag-pipeline 项目的 csv + parquet 导入）
python build_tag_db.py init --csv <tags_enhanced.csv> --parquet <wiki_pages.parquet>

# 2. 从上游 GitHub SQLite 同步新标签（--no-download 用本地已下载的 tag.sqlite）
python build_tag_db.py sync-tags

# 3. 增量抓取 Danbooru wiki（需配置 DANBOORU_USER_NAME / DANBOORU_API_KEY）
python build_tag_db.py update

# 4. 查看统计
python build_tag_db.py stats
```

## 配置说明

所有配置通过 `.env` 文件管理，修改后**无需重启**（按 mtime 热更新）。完整清单见 `.env.example`，以下是分组摘要。

### 翻译模型（LLM）

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `LLM_TEXT_API_URL` | API 地址 | — |
| `LLM_TEXT_API_KEY` | API 密钥（本地部署可留空） | — |
| `LLM_TEXT_MODEL` | 模型名称 | — |
| `LLM_TEXT_MAX_TOKENS` | 单次输出上限 | `8192` |

### 视觉模型（VLM 描述生成 / 提示词优化器）

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `LLM_VISION_API_URL` | API 地址 | — |
| `LLM_VISION_API_KEY` | API 密钥（本地部署可留空） | — |
| `LLM_VISION_MODEL` | 模型名称 | — |
| `LLM_VISION_MAX_TOKENS` | 单次输出上限（思考与正式回答共享） | `1024` |
| `LLM_VISION_THINKING` | 思考模式开关 | `off` |
| `LLM_VISION_TIMEOUT` | 单次请求超时（秒） | `180` |

> **API Key 可空**：本地部署的 OpenAI 兼容端点（Ollama / LM Studio / vLLM / llama.cpp）不校验 Key，留空即可 —— 空值由 `config.resolve_api_key()` 归一化为占位串。缺 `API_URL` 仍会报错。

### 提示词优化器（`/prompt_tool`）

端点复用上面的 `LLM_VISION_*`。只有三个会真的按需调整：`PROMPT_TOOL_MAX_TOKENS`（`8192`）、`PROMPT_TOOL_THINKING`（`off`）、`PROMPT_TOOL_TIMEOUT`（`180`）。其余检索口径与图片编码上限是写死在 `config.py` 的 `_PROMPT_*` 常量。

### WD14 / Real-ESRGAN / BiRefNet

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `TAGGER_MODEL_PATH` | 模型目录（需含 `model.onnx` + `selected_tags.csv`） | `models/wd-eva02-large-tagger-v3` |
| `TAGGER_GENERAL_THRESHOLD` | general 标签置信度阈值（调高更准更少，调低更多更全） | `0.3` |
| `TAGGER_CHARACTER_THRESHOLD` | character 标签置信度阈值（同上；角色标签通常比 general 给得低） | `0.1` |
| `REALESRGAN_MODEL_PATH` | 模型权重 `.pth` 路径 | `models/RealESRGAN_x4plus_anime_6B.pth` |
| `REALESRGAN_TILE` | 分块推理尺寸（显存不足时设置，如 `400`） | — |
| `BIREFNET_BASE_DIR` | base 模型目录 | `models/birefnet-base` |
| `BIREFNET_WEIGHTS` | ToonOut 微调权重 `.pth` | `models/toonout.pth` |

### Danbooru 抓取

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `DANBOORU_USER_NAME` / `DANBOORU_API_KEY` | API 凭证 | — |
| `DANBOORU_PROXY` | 代理地址（网络问题时用） | — |
| `DANBOORU_API_URL` | API 基址 | `https://danbooru.donmai.us` |
| `DANBOORU_ENABLED` | 抓取总开关 | `true` |
| `DANBOORU_USER_AGENT` | 自定义 UA | `TagEditorWeb/1.0` |
| `DANBOORU_DELAY` / `DANBOORU_DELAY_JITTER` | 请求间隔与抖动（秒） | `0.15` / `0.3` |
| `DANBOORU_PAGE_LIMIT` | 每页条数（上限 200） | `200` |
| `DANBOORU_TIMEOUT` | 请求超时（秒） | `15` |
| `DANBOORU_PAUSE_EVERY_PAGES` / `DANBOORU_PAUSE_SECONDS` | 每 N 页暂停 M 秒 | `100` / `5` |

### 其它

| 变量 | 说明 |
|------|------|
| `TAG_DB_PATH` | 标签数据库路径 | `data/danbooru_tags.db` |
| `BANGUMI_ACCESS_TOKEN` | Bangumi API 令牌，深度翻译「角色/作品」时查证中文名用。**留空不会跳过查询**，只是不带认证头发请求、必然失败并白等超时；不做这类翻译就不用配 | — |
| `CAPTION_USE_TAGS_AS_HINT` | VLM 描述是否参考已有 `.txt` 标签 | `true` |
| `CAPTION_SAVE_AS` | `txt` 覆盖标签 / `separate` 另存 `.caption.txt` | `txt` |
| `REALESRGAN_TILE_PAD` | 分块推理边距 | `10` |
| `PRELOAD_MODELS` | 启动时预热轻量模型 | `false` |
| `FLASK_DEBUG` | 设为 `1` 开启调试热重载 | `0` |

## 日志

运行日志写到 `logs/tageditor.log`（单文件 5MB，自动轮转保留 5 份），同时输出到控制台。排查抓取/翻译这类长任务的失败原因时看这个文件。

```bash
# 只看错误和警告
grep -E '\[(ERROR|WARNING)\]' logs/tageditor.log

# 太吵就把控制台调安静（文件仍记全部）
LOG_LEVEL=WARNING python app.py
```

| 变量 | 说明 | 默认 |
|------|------|------|
| `LOG_LEVEL` | 控制台级别 | `INFO` |
| `LOG_FILE_LEVEL` | 文件级别 | `INFO` |
| `LOG_DIR` | 日志目录 | `logs` |
| `LOG_TO_FILE` | 设为 `false` 则只输出控制台 | `true` |

`build_tag_db.py stats` 这类命令的**统计表格仍直接打印到终端**，不写日志文件——那是命令的输出结果，不是诊断信息。

## 数据存储

- `uploads/` — 图片及对应 txt / nl.txt 文件
- `data/danbooru_tags.db` — Danbooru 标签本地数据库（SQLite）
- `data/cooc/` — 共现矩阵（parquet）
- `data/tag_groups.json` — 标签组体系
- `models/` — WD14、Real-ESRGAN、BiRefNet 模型文件（需自行准备，不入库）

图片与标签通过文件名关联，标签以**逗号分隔**存储在 txt 中（如 `1girl, blue_hair, smile`）。保存时自动统一为小写并去重。

### 标签数据库（SQLite）

`data/danbooru_tags.db`，主表 `tags` 为 11 列：

| 列 | 含义 | 来源 |
|----|------|------|
| `name` | 英文标签名（主键，如 `blue_hair`） | 上游 SQLite + Danbooru 增量 |
| `cn_name` | 中文翻译（逗号分隔多词，如 `蓝发,蓝色头发`） | 上游 SQLite + LLM 深度翻译 + 手动编辑 |
| `en_wiki` | 英文 wiki 正文（Danbooru DText 格式，只读） | Danbooru 增量抓取 |
| `cn_wiki` | 中文 wiki（LLM 翻译或手写） | LLM 深度翻译 + 手动编辑 |
| `other_names` | 多语言别名（JSON 数组，如 `["蓝发","蓝毛","青髪"]`） | Danbooru 增量抓取 |
| `category` | 标签分类（0=通用/1=艺术家/3=版权/4=角色/5=元数据） | 上游 SQLite |
| `post_count` | 热门度（Danbooru 使用该标签的图片数） | 上游 SQLite |
| `updated_at` | 最后更新时间（增量抓取的时间锚点） | Danbooru 增量抓取 |
| `nsfw` | NSFW 标记（0=安全 1=不安全） | LLM 深度翻译 |
| `cn_name_locked` | 中文名锁定（0=未锁定 1=锁定） | 手动设置 |
| `cn_wiki_locked` | 中文 wiki 锁定（0=未锁定 1=锁定） | 手动设置 |

外加 FTS5 全文索引表 `tags_fts`（contentless，trigram 分词）、抓取状态表 `fetch_state`，以及**独立的**用户新标签表 `user_tags`（`name/cn_name/cn_wiki/created_at/updated_at`，同步或重建 `tags` 不影响它）。

翻译查询优先级：**SQLite（cn_name）→ LLM（未命中时）→ 回写 SQLite**。

## 项目结构

```
.
├── app.py                  # 入口，注册 Blueprint + 页面路由 + 共现预热
├── config.py               # .env 热加载、prompts/ 读取、各模型配置、文件工具函数
├── translation.py          # 翻译 + 标签数据库读写 + 标签详情/wiki 编辑 + SSE 管线路由
├── build_tag_db.py         # 标签数据库构建与查询（init/update/merge/sync-tags/FTS5）
├── sync_tags.py            # 从上游 GitHub SQLite 同步新标签
├── cooc_pipeline.py        # 共现矩阵管线（抓取 / PMI 裁剪 / 画师共现）
├── llm_pipeline.py         # LLM 三层深度翻译管线（entity/general/fallback）
├── tag_groups.py           # 爬取 Danbooru 标签组体系
├── tagger.py               # WD14 预处理/加载/过滤 + 自动打标 + VLM 描述生成
├── prompt_tool.py          # 提示词优化器（规划轮 + 本地工具 + 带图改写 + 未收录修补）
├── file_ops.py             # 上传/删除/清空/标签读写/静态文件/标签统计/批量重命名/ZIP 导出
├── tag_operations.py       # 触发词/查找替换路由
├── image_editor.py         # 图片编辑路由
├── realesrgan_utils.py     # RealESRGANer 推理类
├── birefnet_utils.py       # BiRefNet/ToonOut 背景移除推理
├── sse_utils.py            # SSE 事件格式化工具
├── prompts/                # LLM/VLM 提示词（.txt，热更新）
├── templates/
│   ├── tag_editor.html     # 标签编辑主页（三栏布局）
│   ├── image_editor.html   # 图片编辑器
│   ├── danbooru_wiki.html  # Danbooru 标签查询页
│   └── prompt_tool.html    # 提示词优化器
├── data/                   # 标签数据库 + 共现数据（运行时生成）
├── uploads/                # 图片 + 标签（运行时生成）
├── models/                 # 模型权重（需自行下载）
└── .env                    # 配置文件（不入库）
```

## 架构要点

- **Blueprint 拆分**：6 个 Blueprint、46 条路由；所有 Blueprint 通过 `current_app.config['UPLOAD_FOLDER']` 获取上传目录
- **SSE 流式**：打标 / 描述生成 / 批量翻译 / 同步 / 爬取等长任务用 SSE 推送进度。后端 `Response(generator(), mimetype='text/event-stream')`，前端 `fetch()` + `ReadableStream` 消费 POST 流（因需 POST，不能用 EventSource）。事件类型 `progress` / `error` / `complete` / `fatal`
- **取消机制**：`GeneratorExit` 继承 `BaseException`，绕过 `except Exception`，所以每个 SSE generator 必须显式 `except GeneratorExit: cancel_evt.set(); raise` 否则取消标志永不置位
- **进程级连接**：SQLite 单例连接（`check_same_thread=False` + `busy_timeout=5000` + `journal_mode=WAL`）；单 worker 运行时无并发竞争
- **原子写入**：parquet / PNG / 数据库迁移一律「写临时文件 → `os.replace`」，避免中途失败留下截断文件
- **懒加载**：WD14（onnxruntime/cv2/pandas）、Real-ESRGAN（torch/basicsr）、BiRefNet（torch/transformers）在函数内按需 import，未安装时仅禁用对应功能
- **模型缓存**：三套模型首次加载后常驻内存，按各自配置 key 失效

## 性能参考

RTX 4060 Laptop + torch 2.5.1+cu121（FP32）下的单张推理耗时：

| 操作 | 耗时 |
|------|------|
| Real-ESRGAN 4x 超分（1024×576 → 4096×2304） | ~4 秒 |
| ToonOut 背景移除（1024×576） | ~2 秒 |

CPU 推理会慢得多（背景移除可达数十秒），建议有 GPU 时启用。

共现数据（`cooccurrence_clean.parquet`）首次加载 3~6 秒，启动后用 daemon 线程无条件预热。

## 备注

- 所有 API 均为 OpenAI 兼容格式，支持 Ollama、DeepSeek 等本地或远程服务
- WD14 自动打标仅处理**无标签或空 `.txt`** 的图片
- VLM 描述生成仅处理**无 `.nl.txt`** 的图片
- 透明转色底仅处理含 alpha 通道的图片，并跳过 GIF
- 四个页面通过 URL hash 互相跳转并保持当前图片位置
