# CLAUDE.md

This file provides guidance to Claude Code when working with code in this repository.

## Project Overview

标签批量编辑工具 — 基于 Flask 的 Web 应用，用于批量上传图片及对应文本标签，支持中英文双向翻译（OpenAI 兼容大模型 API）和在线编辑保存。
额外提供：
- 图片编辑器（裁剪、旋转、透明转色底、Real-ESRGAN 超清放大、ToonOut 背景移除）
- WD14 本地自动打标（ONNX CUDA）生成 `.txt` 标签文件
- VLM API 自然语言描述生成（将结构化标签"翻译"为连贯英文描述，保存为 `.nl.txt`）

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

后端使用 Flask Blueprint 拆分为模块，前端为三个页面。所有 Blueprint 通过 `current_app.config['UPLOAD_FOLDER']` 获取上传目录。翻译数据存储在 `data/danbooru_tags.db`（SQLite）。

### 文件命名约定

每张图片关联两个文本文件：
- `{name}.txt` — WD14 标签（结构化标签，逗号分隔）
- `{name}.nl.txt` — VLM 自然语言描述（连贯英文句子，由 VLM 基于图片+标签生成）

### 后端模块关系

| 层 | 文件 | 职责 |
|----|------|------|
| 入口 | `app.py` | 注册 Blueprint + 页面路由 |
| 配置 | `config.py` | `.env` 热加载、prompts/ 提示词文件读取（`get_prompt`）、模型配置、文件工具函数 |
| 翻译 | `translation.py` | 标签翻译查询/回写、标签详情/wiki 编辑路由、深度翻译单条 |
| 翻译管线 | `llm_pipeline.py` | 三层深度翻译（entity/general/fallback），回写 cn_name/cn_wiki/nsfw |
| 数据库 | `build_tag_db.py` | 标签库构建/增量更新/FTS5 搜索 |
| 同步 | `sync_tags.py` | 从上游 GitHub SQLite 同步新标签（`post_count≥100`，`category∈{0,3,4}`） |
| 打标 | `tagger.py` | WD14 预处理/模型加载/过滤 + WD14 打标 + VLM 自然语言描述生成 |
| 文件 | `file_ops.py` | 上传/删除/清空/标签读写/NL 描述读写/静态文件/标签统计/批量重命名/ZIP 导出 |
| 标签操作 | `tag_operations.py` | 触发词/查找替换路由 |
| 图片编辑 | `image_editor.py` | 保存/透明转色底/超清放大/背景移除路由 |
| 共现 | `cooc_pipeline.py` | 从 Danbooru 拉取共现频率数据（parquet） |
| 标签组 | `tag_groups.py` | 爬取 Danbooru 标签组体系 |
| 工具类 | `realesrgan_utils.py` | RealESRGANer 推理 |
| 工具类 | `birefnet_utils.py` | BiRefNet/ToonOut 背景移除推理 |
| 工具类 | `sse_utils.py` | SSE 事件格式化（`sse_event(type, data)`） |

### 前端页面

- `tag_editor.html` — 标签编辑主页面：三栏布局（文件列表 / 图片预览 / 标签编辑器 + 自然语言描述面板）
- `image_editor.html` — 图片编辑器：Canvas 裁剪、旋转、缩放、透明转色底、超清放大、背景移除
- `danbooru_wiki.html` — Danbooru 标签查询页：搜索（FTS5）+ 标签详情 + 增量更新（SSE 流式）

三页面通过 URL hash 互相跳转并保持当前图片位置。

## Key Patterns

### SSE 流式模式

自动打标、VLM 描述生成、批量翻译、批量透明转色底使用 SSE 流式推送进度：

- 后端：`Response(generator(), mimetype='text/event-stream')`，使用 `sse_utils.sse_event()` 格式化
- 前端：`fetch()` + `ReadableStream` 消费 POST 流（非 EventSource，因需要 POST）
- 事件类型：`progress`（进度）、`error`（单项失败）、`complete`（全部完成）、`fatal`（致命错误）
- 致命错误后必须 `return` 终止 generator，否则继续 yield 会报 `RuntimeError`
- 前置校验（如无内容可处理）仍返回普通 JSON，前端通过 Content-Type 区分
- SSE 响应统一带 `Cache-Control: no-cache` 与 `X-Accel-Buffering: no` 头

### 翻译数据库（SQLite）

`data/danbooru_tags.db`，schema：`tags(name PK, cn_name, en_wiki, cn_wiki, other_names, category, post_count, updated_at, nsfw, cn_name_locked, cn_wiki_locked)`，外加 FTS5 全文索引表 `tags_fts`。

关键约定：
- **查询优先级**：SQLite（cn_name）→ LLM（未命中时）→ 回写 SQLite
- **回写函数不受 updated_at 守卫限制**（纯 UPDATE + ON CONFLICT）
- **进程级连接**：`_get_tag_db_conn()` 懒加载，DB 不存在时返回 None
- **锁定保护**：`cn_name_locked` / `cn_wiki_locked` 通过 SQL `CASE WHEN locked=1 THEN old ELSE new END` 实现

### 懒加载

WD14（onnxruntime/cv2/pandas）、Real-ESRGAN（torch/basicsr）、BiRefNet（torch/transformers/kornia/einops/timm）在函数内按需 import，未安装时仅禁用对应功能。

### 模型缓存

三套模型首次加载后常驻内存，按各自配置 key 失效（`_wd14_model_cache`、`_realesrgan_cache`、`birefnet_utils._birefnet_cache`）。单 worker 运行时无并发竞争问题。

### 配置热更新

所有配置通过 `.env` 文件管理。每次 API 调用触发 `config.load_env()`，通过 mtime 检测按需重读。

### 提示词管理（prompts/）

所有 LLM/VLM 提示词统一存放在 `prompts/` 目录，每个提示词一个 `.txt` 文件（代码和 `.env` 中不存提示词，缺失即报错，无内置默认）：

- `vlm_caption.txt` — VLM 自然语言描述提示词（必填，缺失时 `/auto_caption_vlm` 返回 400）
- `llm_entity.txt` / `llm_general.txt` / `llm_fallback.txt` — LLM 深度翻译三层系统提示词
- `rules_tag_groups.txt` / `rules_cooc.txt` — 占位符 `{TAG_GROUPS_RULE}` / `{COOC_RULE}` 注入的规则片段

读取：`config.get_prompt(key)`（文件名去 `.txt` 为 key），目录 mtime + 各文件 (name, mtime, size) 签名检测热更新，修改保存即生效。LLM 提示词经 `llm_pipeline.get_system_prompt(key)` 读取并注入规则占位符，文件缺失抛 `ValueError`。文件内容整读（含 `#` 开头行，无注释语法）。

### 路径安全

`safe_filename()` 保留中文字符但移除危险字符；路径验证用 `config.is_within_directory()`（基于 `os.path.commonpath()` 逐段比较，非 `startswith`）。

### VLM 自然语言描述生成（tagger.py — `/auto_caption_vlm`）

- 读取已有 `.txt` 标签作为参考标签送入 VLM
- 无标签的图片纯靠 VLM 看图描述
- **仅处理无 `.nl.txt` 的图片**，已有描述的自动跳过（`skipped`）
- 保存到 `.nl.txt`，不覆盖原 `.txt` 标签
- 提示词：从 `prompts/vlm_caption.txt` 读取（必填），提示词引导 VLM 扮演"翻译官"而非"创作者"
- VLM 配置（`.env`）：`VISION_API_URL` / `VISION_API_KEY` / `VISION_MODEL`

### 前端脏状态追踪（tag_editor.html）

- **双层脏状态**：`savedTags`（标签文本）+ `savedNlCaption`（NL 描述）
- `isDirty()` — 检查标签是否有未保存修改
- `isNlDirty()` — 检查自然语言描述是否有未保存修改
- 切换图片/离开页面/重命名/刷新关闭时同时检查两者，弹窗提示"标签和自然语言描述有未保存的修改"
- `loadSeq` 序列号丢弃过期的 `loadCaption` 响应，防止异步竞态
- 自然语言描述面板可折叠（`toggleNlSection`），有内容时自动展开，无内容时隐藏

### 图片编辑器暂存态（image_editor.html）

- `editorDirty` 标志：裁剪/缩放/超清放大/背景移除暂存前端，需点「保存」才覆盖原图
- `editorDirty=true` 时禁用旋转和透明转色底（这两者自动落盘，与暂存态冲突）
- `_upscaleBusy`/`_removebgBusy` 异步 busy 标志控制按钮 spinner
- `_displayObjectUrl` 跟踪 objectURL 生命周期，切图时 `revokeObjectURL` 清理

### 自动打标/描述范围

- `/auto_tag_wd14` — 仅处理无标签或空 `.txt` 的图片
- `/auto_caption_vlm` — 仅处理无 `.nl.txt` 的图片
- `/batch_alpha_to_white` — 仅处理含 alpha 通道的图片并跳过 GIF

## Route 分类

### 页面路由
- `/` (GET) — 标签编辑主页
- `/img_editor` (GET) — 图片编辑器
- `/danbooru` (GET) — Danbooru 标签查询

### SSE 流式路由（均 POST）
- `/auto_tag_wd14` — WD14 本地自动打标
- `/auto_caption_vlm` — VLM 自然语言描述生成（仅处理无 `.nl.txt` 的图片）
- `/batch_alpha_to_white` — 批量透明转色底
- `/llm_process_db` — 批量深度翻译管线
- `/sync_tags_db` — 从上游同步新标签
- `/crawl_tag_groups` — 爬取 Danbooru 标签组
- `/fetch_cooc` — 拉取共现数据
- `/trim_cooc` — 裁剪共现数据
- `/danbooru_update` — 增量更新 wiki

### 二进制返回路由
- `/upscale_realesrgan` (POST) — 返回 PNG 二进制或 JSON
- `/remove_background` (POST) — 返回 PNG 二进制或 JSON

### 常规 JSON 路由（POST）
- `/upload` — 批量上传
- `/save_caption/<name>` — 保存标签到 `.txt`
- `/save_nl_caption/<name>` — 保存自然语言描述到 `.nl.txt`
- `/lookup_cache` — 查询翻译（双向 en↔zh）
- `/translate_single_tag` — 单条深度翻译
- `/update_cn_name` — 手动编辑中文名
- `/update_tag_wiki` — 手动编辑中文 wiki（lang=zh 只允许中文）
- `/toggle_cn_lock` — 切换锁定状态（name/wiki）
- `/prepend_tags` — 添加触发词
- `/find_replace` — 全局查找替换
- `/rename_files` — 批量重命名
- `/process_image` — 保存编辑后图片
- `/danbooru_search` — FTS5 全文搜索
- `/danbooru_cancel` — 取消 Danbooru 操作
- `/delete/<image_name>` — 删除图片及对应 `.txt` + `.nl.txt`
- `/clear_all` — 清空所有文件
- `/export_zip` — 导出图片+标签为 ZIP（可选 `.txt` 或 `.nl.txt`）

### 常规 JSON 路由（GET）
- `/uploads/<filename>` — 静态文件访问
- `/get_caption/<name>` — 获取图片标签 + 翻译 + 自然语言描述（`nl_caption`）
- `/tag_detail/<tag>` — 标签完整信息
- `/tag_cooc/<tag>` — 共现推荐列表
- `/tag_stats` — 标签统计

## LLM 深度翻译管线（llm_pipeline.py）

三层处理顺序：
1. **entity**（category 3/4 — 版权/角色标签，有 wiki 页）
2. **general**（有 wiki 页的 general 等标签）
3. **fallback**（无 wiki 页的标签）

每层按 `batch_size=8` 分批，每批有独立 `history` 保存，防止单批失败丢失进度。OpenAI 调用设 `timeout=30`，重试 5 次后抛出异常。JSON 解析失败抛出 `ValueError`（含 LLM 响应预览）。`_combine_cn(base_cn, ext_cn)` 将基础名与扩展名用逗号合并。

BiRefNet 加载：`AutoModelForImageSegmentation.from_pretrained` 加载 base 结构 → `load_state_dict` 覆盖 ToonOut 权重（清洗 `module.`/`module._orig_mod.` 前缀）→ `model.float()` 转 FP32。
