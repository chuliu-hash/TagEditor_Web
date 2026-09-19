# CLAUDE.md

This file provides guidance to Claude Code when working with code in this repository.

## Project Overview

标签批量编辑工具 — 基于 Flask 的 Web 应用，用于批量上传图片及对应文本标签，支持中英文双向翻译（OpenAI 兼容大模型 API）和在线编辑保存。
额外提供：
- 图片编辑器（裁剪、旋转、透明转色底、Real-ESRGAN 超清放大、ToonOut 背景移除）
- WD14 本地自动打标（ONNX CUDA）生成 `.txt` 标签文件
- VLM API 自然语言描述生成（将结构化标签"翻译"为连贯英文描述，保存为 `.nl.txt`）
- 提示词优化器（图片 + 提示词 + 中文**优化要求** → 模型自决意图并点名检索工具 → 以本地标签库为词表与校验器重调提示词。**只产出到页面，不写入任何文件**）

## Commands

```bash
# 安装依赖（需在 tageditor conda 环境中，Python 3.11）
conda activate tageditor
pip install -r requirements.txt

# 启动开发服务器（默认 debug 关闭；开发时用 FLASK_DEBUG=1 python app.py 开启，访问 http://127.0.0.1:8001）
python app.py
```

无构建步骤，无第三方测试框架。唯一的测试文件是 `test_invariants.py`：

```bash
python test_invariants.py        # 25 个用例，只用标准库
python test_invariants.py -v     # 失败时打印 traceback
```

它**不是**覆盖率测试，而是把 CLAUDE.md 里那些「别退回」的约定固化成断言——因为本项目多数 bug 是静默的（翻译列空白、徽标错、描述被炸成假标签、数据写坏），没有异常也没有日志，只有人眼能发现。用例分三类：纯函数行为（`_combine_cn` / `_split_prompt_entries` / `resolve_api_key`）、数据库语义（`lookup_tags` 只查主表、`user_tags` 独立）、源码级约定（`OpenAI()` 必须过 `resolve_api_key`、翻译格必须走 `_setTranslationCell`、`GeneratorExit` 守卫、`total=6`、`_UPLOAD_NEXT_PAGES` 用 endpoint 名）。改这些地方时先跑它；加新的「别退回」约定时往里面加一条。

已用变异测试验证过：14 个故意注入的缺陷全部被对应用例抓到（`_combine_cn` 不拆全角、提示词切分退回按逗号切、`lookup_tags` 塞回落、`resolve_api_key` 不归一化、原子写退回直接覆盖、`prepend_tags` 去掉幂等、`find_replace` 改子串匹配、批量路由不校验 body、`clear_all` 恢复宽松归一化、翻译格绕过写入口、弹窗守卫改手写 id 清单、`currentImgName` 拼写复活、`GeneratorExit` 被删、`total` 改成 7、`_UPLOAD_NEXT_PAGES` 写错名）。

## Architecture

后端使用 Flask Blueprint 拆分为模块，前端为四个页面。所有 Blueprint 通过 `current_app.config['UPLOAD_FOLDER']` 获取上传目录。翻译数据存储在 `data/danbooru_tags.db`（SQLite）。

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
| 提示词优化 | `prompt_tool.py` | 规划轮（模型自决意图 + 点名工具）+ 本地工具执行（四工具、零 LLM）+ 带图改写 + 未收录修补 + `/prompt_adjust` 路由 |
| 工具类 | `realesrgan_utils.py` | RealESRGANer 推理 |
| 工具类 | `birefnet_utils.py` | BiRefNet/ToonOut 背景移除推理 |
| 工具类 | `sse_utils.py` | SSE 事件格式化（`sse_event(type, data)`） |

### 前端页面

- `tag_editor.html` — 标签编辑主页面：三栏布局（文件列表 / 图片预览 / 标签编辑器 + 自然语言描述面板）
- `image_editor.html` — 图片编辑器：Canvas 裁剪、旋转、缩放、透明转色底、超清放大、背景移除
- `danbooru_wiki.html` — Danbooru 标签查询页：搜索（FTS5）+ 标签详情 + 增量更新（SSE 流式）
- `prompt_tool.html` — 提示词优化器：三栏（图片 / 输入+优化要求 / diff 结果 + 描述 + 检索计划面板）

`danbooru_wiki.html` 的**浏览记录**（纯前端，`localStorage` 键 `danbooru_tag_history_v1`，新→旧、上限 200 条，`{name, cn, ts}`）：
埋点只有一处 —— `renderDetail(data)` 首行的 `historyAdd(...)`（该函数唯一调用者是 `showTagDetail`，覆盖搜索/回车/随机推荐/DText/共现/标签组全部入口）；
展示两处 —— 头部「浏览记录」按钮 + `#history-modal`，以及搜索下拉里独立的 `#history-matches` 容器（在 `#search-results` 之上，**不参与** `_searchResults`/`appendSearchResults` 那套增量分页）；
`renderHistoryMatches` 挂在 `searchTags()` 开头，`clearHistoryMatches` 挂在 `liveSearch()` 的空输入分支（下拉只加 `hidden` 类不清内容，不主动清会残留）；
行内 `onclick` **一律传数组下标**（标签名可能含引号，拼串会拼坏 JS）。读写全包 `try/catch` —— `localStorage` 在隐私模式/配额满时会抛异常，埋点绝不能把 `renderDetail` 打挂。
标签编辑页**没有**浏览记录（刻意不加）。

搜索下拉的**键盘导航**（`onSearchKeydown` / `_highlightSearchItem` / `_searchSel`）：`↑`/`↓` 选、`Enter` 打开选中项（无选中时沿用原「打开输入框内容」）、`Esc` 关下拉。
关键约束：结果项带 `data-idx`（= 在 `_searchResults` 里的下标，搜索结果存进去时打 `__idx`），**不能用 DOM 顺序定位** —— 增量分页只先渲染前 50 条，DOM 顺序与结果下标不等价；`_highlightSearchItem` 遇到未渲染的目标会先 `appendSearchResults()` 补齐。选中态只用 CSS 类 `.sr-item.is-sel` 一套机制（不要叠 Tailwind 的 `bg-primary/10`，两套并存时移除只删一个 class 会留下半截高亮）；鼠标 `mousemove` 到别的条目上会清掉键盘选中态，避免两条同时看起来被选中。

四页面通过 URL hash 互相跳转并保持当前图片位置。`/#newtag=<标签名>` 是新增的 hash 动作：Danbooru 页 `_applyHashAction()` 认这个前缀，自动打开新标签弹窗并预填标签名，随后用 `history.replaceState` 清掉 hash（否则关掉弹窗再刷新会重复弹出）。

## Key Patterns

### 日志（logging_setup.py）

改造前全项目 0 处 logging、246 处 print。问题不在「print 不能用」，而是没有级别、不落盘（SSE 里的报错只到 stdout，换个窗口就永久丢了）、没有时间戳与来源（几十分钟的抓取/翻译管线无法定位到时刻）。

现在：

- 各模块用 `log = logging.getLogger(__name__)`（迁移时由脚本统一注入，位置在 import 块之后）
- 配置集中在 `logging_setup.py`，`app.py` 启动时调 `setup_logging()`；`build_tag_db.py main()` 也调（CLI 长跑命令同样要能回溯）
- 控制台与文件**默认都是 INFO**，所以 level 分错只是「调成 WARNING 时可见性不同」，不会丢信息；拿不准时报高一级
- 落盘 `logs/tageditor.log`，5MB × 5 份轮转。**`delay=True` 是必需的**：Windows 上不延迟打开会让轮转时 `os.rename` 撞 WinError 32（文件被占用），结果是**丢弃日志记录**并往 stderr 刷 Logging error。实测不开必失败
- 环境变量 `LOG_LEVEL` / `LOG_FILE_LEVEL` / `LOG_DIR` / `LOG_TO_FILE` 可调，都有默认值
- 目录不可写时自动退回「仅控制台」，日志系统绝不把应用拖垮

**CLI 程序输出仍用 print，不要迁移**：`show_stats` 的统计表、`cooc_pipeline` 的 PMI/NPMI 对齐表格、`tag_groups` 的进度行是「程序本身的结果」而非诊断信息，加时间戳前缀会毁掉对齐也不利于管道过滤。判断标准是「这是给终端用户看的结果，还是给排查问题看的痕迹」。

### SSE 流式模式

自动打标、VLM 描述生成、批量翻译、批量透明转色底使用 SSE 流式推送进度：

- 后端：`Response(generator(), mimetype='text/event-stream')`，使用 `sse_utils.sse_event()` 格式化
- 前端：`fetch()` + `ReadableStream` 消费 POST 流（非 EventSource，因需要 POST）
- 事件类型：`progress`（进度）、`error`（单项失败）、`complete`（全部完成）、`fatal`（致命错误）
- 致命错误后必须 `return` 终止 generator，否则继续 yield 会报 `RuntimeError`
- 前置校验（如无内容可处理）仍返回普通 JSON，前端通过 Content-Type 区分
- SSE 响应统一带 `Cache-Control: no-cache` 与 `X-Accel-Buffering: no` 头

### 翻译数据库（SQLite）

`data/danbooru_tags.db`，schema：`tags(name PK, cn_name, en_wiki, cn_wiki, other_names, category, post_count, updated_at, nsfw, cn_name_locked, cn_wiki_locked)`，外加 FTS5 全文索引表 `tags_fts`，以及用户新标签表 `user_tags`。

关键约定：
- **查询优先级**：SQLite（cn_name）→ LLM（未命中时）→ 回写 SQLite
- **回写函数不受 updated_at 守卫限制**（纯 UPDATE + ON CONFLICT）
- **进程级连接**：`_get_tag_db_conn()` 懒加载，DB 不存在时返回 None
- **锁定保护**：`cn_name_locked` / `cn_wiki_locked` 通过 SQL `CASE WHEN locked=1 THEN old ELSE new END` 实现

#### 用户新标签（`user_tags`）

`user_tags(name PK, cn_name, cn_wiki, created_at, updated_at)` — 打标过程中遇到、主库未收录的标签，用户手动维护（Danbooru 查询页「新标签」入口），**与爬取的 `tags` 表独立**：同步/重建 `tags` 不影响本表；翻译复用 `llm_pipeline.translate_one_tag` 三层管线（新标签无 wiki 数据 → 自动走 fallback 层），结果存本表自己的字段，不写回 `tags`。

- **主表优先**：两表存在同名标签时以 `tags` 为准。新增时若主库已收录则拒绝（400）；翻译时主库已有中文名则直接返回主库数据（`source='main_db'`）；列表返回 `in_main_db` 标注供前端展示「主库已收录」徽标
- **中文名不手动设置**：弹窗只输入标签名，添加后前端自动调 `/user_tags/translate`；结果区在等待期间显示「翻译中」（`_utPending` 集合驱动，同一标签并发只发一次请求），翻译完成后展示中文名 + 中文 wiki（`_utRows` 缓存最近列表以便重渲染）
- **手动编辑只作用于主库已收录标签**：`/update_cn_name`、`/update_tag_wiki` 先查 `tags` 是否存在，未收录直接 404。原因是底层 `update_translation` / `update_cn_wiki` 是 `INSERT ... ON CONFLICT DO UPDATE`，未收录标签会被插进 `tags`（其余字段全是默认值）——用户新标签的编辑必须走 `user_tags`。`/tag_detail` 返回 `in_main_db` 供前端隐藏这些按钮（前端隐藏只是体验，后端 404 才是防线）
- **显示回落**：标签编辑页的翻译列不直接查 `lookup_tags`，而是走 `translation._lookup_cn_from_db` —— 主表有中文名用主表，主表未收录或中文名为空时回落 `user_tags`（否则用户自己翻译过的新标签在编辑页/统计页显示为空）。`/tag_detail` 同样回落
- 标签名按 `normalize_tag_key`（小写+空格→下划线）规范化存储与匹配
- 代码：`build_tag_db.py` 的 `normalize_tag_key` / `list_user_tags` / `lookup_user_tags` / `upsert_user_tag` / `delete_user_tag`；路由在 `translation.py`（`/user_tags` 系列）

> 注意：`lookup_tags` 保持「只查主表」的语义，`/user_tags` 列表的 `in_main_db` 徽标与翻译的 `source='main_db'` 判定都依赖它；需要含回落语义时用 `lookup_user_tags` 显式补齐，勿把回落塞进 `lookup_tags`。

### 懒加载

WD14（onnxruntime/cv2/pandas）、Real-ESRGAN（torch/basicsr）、BiRefNet（torch/transformers/kornia/einops/timm）在函数内按需 import，未安装时仅禁用对应功能。

### 模型缓存

三套模型首次加载后常驻内存，按各自配置 key 失效（`_wd14_model_cache`、`_realesrgan_cache`、`birefnet_utils._birefnet_cache`）。单 worker 运行时无并发竞争问题。

### 配置热更新

所有配置通过 `.env` 文件管理。每次 API 调用触发 `config.load_env()`，通过 mtime 检测按需重读。

### API Key 可空（本地部署）

本地部署的 OpenAI 兼容端点（Ollama / LM Studio / vLLM / llama.cpp）不校验 Key，故 `LLM_TEXT_API_KEY` / `LLM_VISION_API_KEY` **留空即可**。但 openai SDK 2.x 在 `api_key` 为空字符串时同样抛 `OpenAIError: Missing credentials`（`_client.py` 判的是 `not self.api_key`，不只是 `None`），所以**所有 `OpenAI()` 构造点必须过 `config.resolve_api_key()`**——它把空值/空白归一化为占位串 `not-needed`，真实 key 原样透传。

- `get_llm_config()` / `get_vision_config()` 已在配置层归一化，消费方（`tagger.py` 的 VLM 路径、`prompt_tool.py` 的 `get_prompt_tool_config()`）直接取用即可
- `llm_pipeline.py` / `translation.py` 直接读 `os.environ`，各自显式调用 `resolve_api_key()`
- **该守的是端点地址不是 key**：缺 `LLM_TEXT_API_URL` 仍报错（`ValueError` → 路由 400，SSE 路径发 `fatal`），缺 key 不再拦

### 提示词管理（prompts/）

所有 LLM/VLM 提示词统一存放在 `prompts/` 目录，每个提示词一个 `.txt` 文件（代码和 `.env` 中不存提示词，缺失即报错，无内置默认）：

- `vlm_caption.txt` — VLM 自然语言描述提示词（必填，缺失时 `/auto_caption_vlm` 返回 400）
- `llm_entity.txt` / `llm_general.txt` / `llm_fallback.txt` — LLM 深度翻译三层系统提示词
- `rules_tag_groups.txt` / `rules_cooc.txt` — 占位符 `{TAG_GROUPS_RULE}` / `{COOC_RULE}` 注入的规则片段
- `prompt_planner.txt` — 提示词优化器第 1 轮（规划：判意图 + 点名工具，纯文本不带图）
- `prompt_adjust.txt` — 提示词优化器第 2 轮（带图综合改写：标签 diff + 描述改写契约）
- `prompt_repair.txt` — 提示词优化器第 3 轮（未收录标签修补，纯文本不重发图片）

读取：`config.get_prompt(key)`（文件名去 `.txt` 为 key），目录 mtime + 各文件 (name, mtime, size) 签名检测热更新，修改保存即生效。LLM 提示词经 `llm_pipeline.get_system_prompt(key)` 读取并注入规则占位符，文件缺失抛 `ValueError`。文件内容整读（含 `#` 开头行，无注释语法）。

### 路径安全

`safe_filename()` 保留中文字符但移除危险字符；路径验证用 `config.is_within_directory()`（基于 `os.path.commonpath()` 逐段比较，非 `startswith`）。

### VLM 自然语言描述生成（tagger.py — `/auto_caption_vlm`）

- 读取已有 `.txt` 标签作为参考标签送入 VLM
- 无标签的图片纯靠 VLM 看图描述
- **仅处理无 `.nl.txt` 的图片**，已有描述的自动跳过（`skipped`）
- 保存到 `.nl.txt`，不覆盖原 `.txt` 标签
- **提示词分工（`prompts/vlm_caption.txt`，改这个文件时别退回）**：人物特征/外貌（发色、瞳色、五官、身材、服装、配饰及其颜色）**全部由结构化标签负责**，NL 描述**禁止**再写（连标签漏掉的外貌细节也不补）；描述只补标签表达不了的三件事——**动作细节**（重量落在哪、躯干怎么扭、哪个肢体做什么、视线落点）、**构图**（景别、机位角度、主体在画面中的位置、前后景层次、景深）、**背景氛围**（光线方向与质感、天气、情绪）。四肢只能作为动作的施动者出现（"one arm reaches back"），不得描述身体长什么样。反面教训：旧版 A 段 + `[complex action]` 示例通篇是身体部位细节（"ears and tail still lifted"），模型照抄示例去写外貌；示例本身必须干净
- 提示词：从 `prompts/vlm_caption.txt` 读取（必填），提示词引导 VLM 扮演"翻译官"而非"创作者"
- VLM 配置（`.env`）：`LLM_VISION_API_URL` / `LLM_VISION_API_KEY` / `LLM_VISION_MODEL` / `LLM_VISION_MAX_TOKENS`（默认 1024）/ `LLM_VISION_THINKING`（默认 `off`）
- **`max_tokens` 是 `reasoning_content` + `content` 的共享额度**，不是只算正式回答。推理模型（DeepSeek 等）思考模式默认开启，额度被思考吃光后 `content` 为空、`finish_reason=length`，日志表现为「模型在思考中，未生成正式描述」。故默认关思考（`extra_body={'thinking': {'type': 'disabled'}}`）——本任务只要 2~3 个短句，思考纯烧时间和钱
- 思考模式下服务端会**静默忽略 `temperature`**（不报错、不生效），关掉思考后 `temperature=0.7` 才真正起作用

### 提示词优化器（prompt_tool.py — `/prompt_tool`）

输入：一张图（可空）+ 用户当前提示词（可空）+ 中文**优化要求**（必填）→ 输出：标签 diff + 改写后的自然语言描述。

**用户描述的不一定是「想要的效果」**，也可能是对提示词本身的元操作：精炼（砍到 N 条）、清理（去掉查不到的假标签）、按图校正、调整动作、去重/规范格式。所以**意图由模型自己判断**（不写死中文关键词表去猜），再由它点名要用哪些工具。目标不是让模型自由创作，而是**以本地标签库当词表与校验器**，压住模型编造标签的倾向。

**本页不写入任何文件**（`/save_prompt_result` 已删除，产出只用于页面复制）——与标签编辑功能独立。

#### 核心实测约束：提示词是「标签行 + 换行 + 描述段」两段式

真实文件 `uploads/HRdo_aAbQAIZrm6..txt` = 1808 字符 / **只有 1 个换行** / 50 个逗号块。块 0~35 是真标签，**换行落在第 36 块中间**，第 37~49 块是英文散文段（句读的逗号被逗号切分炸成 14 个假标签）。所以：

- **切分原语 = 首个换行**（`_split_prompt_entries`）：换行前按 `,` `，` 切标签（保留 `(tag:1.2)` / `[tag]` 权重语法），换行后**整段保留，绝不按逗号切**
- 无换行时的回退才用启发式逐块判散文（`_looks_like_prose`：>80 字符 / >8 词 / 含 `. ` / 中文长句 + `_PROSE_MARKERS`），命中的块收拢成一整段描述
- 改这里时别退回「全文按逗号切」——那正是把描述炸成假标签的原因

#### 三次 LLM 调用链

```
校验（request 必填）→ 解析提示词 → 输入知识 → ★规划轮 → 本地执行工具 → ★带图改写 → ★未收录修补
                                                (LLM#1)    (零 LLM)      (LLM#2)      (LLM#3，条件触发)
```

- **LLM#1 `_call_planner`（`prompts/prompt_planner.txt`）**：纯文本**不带图**（它只决定「改什么、查什么」，看图无增益），输出 `{intent, understanding, plan, tools}`。`tools` 为空是**合法计划**（纯格式类要求不需要检索）
  - **规划器失败只降级不 fatal**：抛错/解析不出 JSON → `warnings` 记一句 + 走**兜底检索**（`_extract_terms` 抽词直喂 `search_tags`）后继续。没有工具结果仍可改写，毁掉整轮是净损失
  - **提示词文件缺失**在进 generator 之前就和另两个一起校验 → 普通 JSON 400
- **工具执行（零 LLM）**：见下节
- **LLM#2 `_call_vlm_json`**：带图，`response_format` 两段式（不支持则降级 + 提示词补「只输出 JSON」），解析三级回退（`json.loads` → `json_repair` → 正则抽 `{...}`）。`content` 为空且 `finish_reason=='length'` 抛 `_PromptFatal`（与 `llm_pipeline._OutputTruncated` 同口径，**不静默返回空 diff**）
- **LLM#3 `_call_llm_repair`**：**仅当存在 `uncollected` 时触发**，纯文本不重发图片。要求「被换掉/删掉的标签若在描述里被提到才改描述，没提到就**原样回传 caption**」——避免二次改写漂移
- `_call_json_text(prompt_key, payload, cfg, label)` 是 #1/#3 的公共骨架（#2 因为要发图单独一份）
- `_apply_repair` 是**合并而非替换**（按 `_repair_key` 对齐，`dropped` 处理删掉的新增项）；修补后重新 `validate_tags`，仍未收录的留在 diff 里标红由用户决定
- 无图时的提示「本次没有图片」写在**路由的共享 `warnings` 列表**里，不要在 `_call_vlm_json` 内 `dict(payload, warnings=...)`——那是复制一份，模型看得到而前端看不到

#### 工具层（`_TOOL_SPECS` + `execute_tool_calls`，零 LLM）

四个工具，每个只吃**一个列表参数**（`param`），返回结构都为进模型而瘦过身。`_TOOL_SPECS` 是**唯一出处**：规划提示词里注入的 `available_tools`、参数上限 `bound`、`_run_tool` 的分派全从它来（加工具只改这一处）：

| 工具 | 参数（上限） | 实现 | 返回 |
|---|---|---|---|
| `search_tags` | `keywords`(6) | 见下方「双路检索」 | ≤48 条 `{name, cn_name, category, post_count}` |
| `tag_detail` | `names`(40) | `classify_tags(..., with_wiki=True)` | ≤40 条 `{name, status, cn_name, category, post_count, wiki_excerpt, suggestions}` |
| `cooc` | `seeds`(30) | `cooc_recommendations` | ≤16 条 `{name, lift, seed_hits}`（**不带 cn_name/count**，对模型判断无用，白占 token） |
| `tag_groups` | `names`(40) | 读 `data/tag_groups.json` | ≤20 条 `{name, groups:[{id, cn_name, member_total, members}]}`，成员总预算 500 |

- **双路检索（`_tool_search_tags`）**：含 CJK → `_batch_cn_first_segment` + `_search_cn_fts`；纯 ASCII → `build_tag_db.search_tags`。**别合并成一条**：`search_tags` OR 了 `other_names`，中文会被别名污染（`'白发'` 首位是 `red_eyes`、`'和服'` 混进 `leotard`），而英文正需要别名命中。排序 `(命中关键词数 DESC, post_count DESC)`——「被多个关键词命中」是免费的强相关信号；过滤 `post_count < 20`
- **候选默认只取 category 0**（`_PROMPT_CANDIDATE_CATEGORIES`）：22445 条角色标签会淹没风格/光影类候选
- **共现按 lift 排序而非裸 count**：裸 count 让 `1girl` 引向 `three-tone_hair`(778670 次，与自身 `post_count=237` 差 3000 倍)，是 parquet 与 `tags.post_count` 的口径不一致。`_PROMPT_COOC_MIN_POST=500` + `count > pc_b` 直接丢弃这类行 + 发警告
- **`tag_groups` 走 `translation._load_tag_groups_cache()`**（`data/tag_groups.json`，不是 SQLite），白拿进程级缓存与爬取后的失效。`groups: []` **只说明库里没收录它的分组**，不是「有冗余」——提示词两条都写死了这条口径
- **三道防线**（模型乱点也不会 500）：白名单（名字不在 `_TOOL_SPECS` 或不在本次 `allowed` → 丢弃 + warning）/ 参数清洗（非列表、非标量、空串、重复 → 清掉；超 `bound` 截断）/ 单项 try/except（一个工具炸了其余照常）。**同一工具多次调用合并**执行一次；**空结果不落 key**（与「没被调用」对模型是一回事，省 token）
- 工具开关 = 前端两个复选框：`use_db` gate `search_tags`/`tag_detail`/`tag_groups`（连带 `tag_groups` 的 `conn is None` 也照跑，它只读 JSON），`use_cooc` gate `cooc`。**被关掉的工具不进规划器的 `available_tools`** —— 模型不知道它存在，自然不会点
- 整体超 `_TOOL_RESULT_CHARS` 时按块丢弃（先丢最大块）+ warning，保证改写轮输入不失控

#### `_extract_terms`：降级为兜底（别当主路径）

`_extract_terms` / `_CN_STOPWORDS` 保留，但语义是**规划器失败时的保险**。改注意两点：

- **短词优先（2~4 字）、英文词先占名额**。抽出的词是拿去和 `cn_name` 做**首段/子串**匹配的，5 字以上的长 gram 在中文标签名里几乎不存在，长词优先只会把 limit 名额占满 —— 实测「白发少女在窗边逆光」长词优先时 12 个名额全被 5~6 字 gram 吃掉、命中 **0** 条；短词优先能拿到 `白发 / 少女 / 窗边 / 逆光`，命中 `white_hair` + `backlighting`
- 它抽不出词时（如「去掉虚假不合规提示词」）兜底就真的没有词表，此时改写轮只能靠 `input_prompt` 已有标签 —— 这是可接受的降级，不是 bug

#### `status` 五分类

（`in_db`/`user_tag`/`quality_meta`/`uncollected`/缺失）：**「库里没有 ≠ 编造」**——`_META_TAGS`（masterpiece 等）库不收但绝不是编造，提示词硬规则禁止因「查不到」而删。`prompt_adjust.txt` 与 `prompt_repair.txt` 各有一条**例外**：用户明确要求清理假标签时，按用户要求 `remove` 或换 `suggestions` 里的真实项。

#### 配置（`.env`）

`.env` 里**只有三个**（会真的按需调整的）：`PROMPT_TOOL_MAX_TOKENS`（8192，与思考共享额度，截断报错文案直接指向它）/ `PROMPT_TOOL_THINKING`（off）/ `PROMPT_TOOL_TIMEOUT`（180，**`tagger.py` 的 VLM 路径没传 timeout，这里必须显式传**）。端点复用 `LLM_VISION_API_URL` / `LLM_VISION_API_KEY` / `LLM_VISION_MODEL`。

其余检索口径与图片编码上限（`temperature` 0.4 / `max_input_tags` 60 / `wiki_chars` 240 / `cooc_topk` 8 / `cooc_nsfw` hide / `cooc_min_post` 500 / `candidate_categories` {0} / `image_max_bytes` 4MB / `image_max_side` 1536）是**写死在 `config.py` 的 `_PROMPT_*` 常量**——它们是「调好就不动」的实现细节（比如 `cooc_min_post` 是数据口径防线的一部分，不是可调旋钮），放进 `.env` 只会让人以为调了有意义。**工具侧的 `bound`/`limit`/字符预算写死在 `prompt_tool.py` 的 `_TOOL_SPECS` 与 `_TOOL_RESULT_CHARS`**，同样不进 `.env`。`get_prompt_tool_config()` 仍返回完整 dict，加参数时先问「用户真的会改它吗」。

#### SSE 阶段编号（`total = 6`）

`current` 序列 `0,1,2,2,3,4,5,6` —— 第 6 步（未收录修补）条件触发。改造时「候选检索 + 共现推荐」被「**规划** + **执行工具**」1:1 替换，所以 `total` 与编号一个都没动，只换了语义：`3` 让模型判断优化意图与检索计划 / `4` 执行检索计划（文案带 `_tool_brief` 的命中摘要）/ `5` 调用视觉模型。**别把 `total` 改成 7**。

#### 前端（prompt_tool.html）

- **diff 状态存在 JS 的 `_ptDiff` 数组里**（不是 DOM）：勾选态在对象上，`renderDiff()` 从数据重建，suggestion 点击原地改数据后重渲染
- `effectiveTag(d)` 是勾选语义的唯一出处：未勾选 = 反向操作（keep 移出 / add 不加 / remove 保留原名 / modify 保留 `from`）；`renderTags()` 两趟遍历保证**原提示词顺序在前、新增追加在后**（不依赖后端补全顺序）
- `renderFinalPrompt`（`composeFinal()`）的触发点：勾选变化 / 描述输入 / 结果渲染完成；用户手改 `#pt-final-prompt` 后置 `_ptFinalDirty`，重算前先 `showConfirm`
- `executeWithProgress` 在中断/失败时也会回调 `onComplete({})`，`renderResult()` 靠 `'diff' in data` 挡住这种空回调，否则结果面板会被清空
- `/upload` 表单带 `next=prompt_tool`；`file_ops._UPLOAD_NEXT_PAGES` 的值必须是 **endpoint 名**（新页面是 `prompt_tool_page`，写 `prompt_tool` 会 BuildError → 上传后 500）
- **本页不提供保存**：结果区只有「复制」，没有 `saveResult` / `updateSaveHint` / 保存路径提示。删这些时漏摘 `updateSaveHint()` 的调用点（`selectImage` / `onCaptionInput` / `init`）会直接 ReferenceError —— 尤其是 `init()` 里 `_images` 为空的那条分支
- **检索计划面板**（`#pt-candidates-panel` / `toggleCandidates` / `renderPlan(plan, toolResults)`）数据**随 `complete` 事件回来**（`plan` + `tool_results`），不再单独打一个接口拉。`/prompt_candidates` 路由已删除
- 「优化要求」`#pt-request-input` 必填，`runAdjust()` 空则本地拦下；后端同样 400（两道）

> 共现数据首次加载 3~6s，`app.py` 启动后用 daemon 线程无条件预热（与首次请求竞争只是重复读一次，无正确性问题）。取消复用 `translation._register_cancel('prompt_tool')`，前端「中断」走 `/danbooru_cancel`；**只在阶段边界生效，无法中断已发出的 HTTP 请求**。

### 前端脏状态追踪（tag_editor.html）

- **双层脏状态**：`savedTags`（标签文本）+ `savedNlCaption`（NL 描述）
- `isDirty()` — 检查标签是否有未保存修改
- `isNlDirty()` — 检查自然语言描述是否有未保存修改
- 切换图片/离开页面/重命名/刷新关闭时同时检查两者，弹窗提示"标签和自然语言描述有未保存的修改"
- `loadSeq` 序列号丢弃过期的 `loadCaption` 响应，防止异步竞态
- 自然语言描述面板可折叠（`toggleNlSection`），有内容时自动展开，无内容时隐藏
- **翻译列刷新点共四处**：切图（`loadCaption`）、进度弹窗关闭（`refreshCurrentImage`）、标签被编辑（`onTagChanged`）、保存成功（`refillTranslations`）。`refillTranslations` 只补「标签非空且翻译为空」的行、只打 `/lookup_cache`（纯查库，不触发 LLM，也没有收录/翻译新标签的能力），响应到达后二次校验行标签未变且仍为空才写 DOM——否则会覆盖用户刚编辑出来的结果。**翻译列空白 ≠ 显示 bug**：主库未收录（如 meta 类 `masterpiece`/`best quality`，或非 Danbooru 标签）的标签在任何地方都没有翻译可显示。（历史注：本条曾写「编辑页没有给它们留出路」，现已补上，见下条）
- **翻译格的两种结构与统一写入口**：单元格有翻译时是纯文本 `div.flex-1`，无翻译时是「`div.flex-1` + 可点击占位 span」（点它开详情弹窗）。**所有写这一格的地方必须走 `_setTranslationCell(index, tr)`**，不要 `div.textContent = tr`——那会把占位 span 连同它的 `onclick` 一起抹掉，补出翻译后再清空标签，这一格就永久失去点击入口。`_setTranslationCell` 按有无翻译重建整格（class / title / onclick / 内容）
- **`_tagChangeSeq` 是按行的 Map，不是全局计数**：早先是单个全局 `_tagChangeSeq`，「最后一次修改获胜」的语义会让快速编辑第 2、3 行时第 2 行的响应被丢弃、翻译列永久空白。改成 `Map<行号, seq>` 后只丢弃同一行的过期响应；**切图时 `_tagChangeSeq.clear()`**（键是行号，跨图复用会误判「同一行」）
- **未收录标签的提示与出路**：`/tag_detail` 返回 `in_main_db=false` 时，详情弹窗显示 `#td-not-collected` 提示（说明主库未收录 → 没有翻译是必然的、在主库上翻译会 404），并给两条出路。链接指向 `/#newtag=<标签名>`，Danbooru 页的 `_applyHashAction()` 会据此自动打开新标签弹窗并预填标签名，打开后清掉 hash（否则关掉弹窗再刷新会重新弹出）。跳转走 `navigateAway` 而非直接赋 `location.href`，保证有未保存修改时先弹确认框。**主库未收录与原「编辑页没有出路」的矛盾已在代码层解决**，不是只有文案
- **键盘快捷键**：`A` 新增 / `S` 保存 / `J`/`K` 与 `←`/`→` 切图 / `↑`/`↓` 在标签行间移动（输入框内也可用）/ `Enter` 在输入框里另起一条 / 输入框为空时 `Backspace` 删掉该行退回上一行 / `?` 打开快捷键帮助（`#shortcut-modal`）。加新快捷键时注意两条守卫：**弹窗打开时不接管按键**（`_anyModalOpen()` 按 `div.fixed.inset-0` 是否带 `hidden` 判定，不要手写 id 清单——漏一个就会在被遮挡的页面上触发），**输入元素里只放行行内导航**（`_isTextEntry` / `_inTagRowInput` 必须在 `switch (e.key)` 之前 return，否则字母键会被抢走打不出字）。修饰键组合（Ctrl/Cmd/Alt）一律放行给浏览器
- `image_editor.html` 的 keydown 同样有 `_anyModalOpen()` 守卫：**旧版没有它，导致「未保存修改」确认框弹出时按 → 会在遮罩底下真的切图**，用户点「取消」已来不及，那张图的编辑就丢了

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
- `/` (GET) — Danbooru 标签查询（`index()`，注意不是标签编辑页）
- `/tag_editor` (GET) — 标签编辑主页
- `/img_editor` (GET) — 图片编辑器（endpoint 名是 `editor`）
- `/prompt_tool` (GET) — 提示词优化器（endpoint 名是 `prompt_tool_page`）

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
- `/prompt_adjust` — 提示词优化：规划轮 + 本地工具执行 + 带图改写 + 未收录修补（前置校验失败仍返回 JSON 400）

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
- `/export_zip` — 导出图片+标签为 ZIP（可选 `.txt` 或 `.nl.txt`，文件统一放入 `train/` 文件夹）
- `/user_tags` — 用户新标签新增/更新（`user_tags` 表，主库已收录则拒绝）
- `/user_tags/delete` — 删除用户新标签
- `/user_tags/translate` — 单条 LLM 翻译用户新标签（结果存 `user_tags`，主库优先）

### 常规 JSON 路由（GET）
- `/uploads/<filename>` — 静态文件访问
- `/get_caption/<name>` — 获取图片标签 + 翻译 + 自然语言描述（`nl_caption`）
- `/tag_detail/<tag>` — 标签完整信息
- `/tag_cooc/<tag>` — 共现推荐列表
- `/tag_stats` — 标签统计
- `/user_tags` — 用户新标签列表（附 `in_main_db` 主库收录标注）

## LLM 深度翻译管线（llm_pipeline.py）

三层处理顺序：
1. **entity**（category 3/4 — 版权/角色标签，有 wiki 页）
2. **general**（有 wiki 页的 general 等标签）
3. **fallback**（无 wiki 页的标签）

每层按 `batch_size=8` 分批，每批有独立 `history` 保存，防止单批失败丢失进度。OpenAI 调用设 `timeout=30`，重试 5 次后抛出异常。JSON 解析失败抛出 `ValueError`（含 LLM 响应预览）。`_combine_cn(base_cn, ext_cn)` 合并基础名与扩展名：按半角/全角逗号切分 → strip → 保序去重 → 半角逗号拼接。**必须去重**——LLM 常把 base 在 `extended_cn_name` 里重复一遍（base=透明衣物 / ext=透明衣物,透视装），直接拼接会得到 `透明衣物,透明衣物,透视装`。**必须拆全角**——前端 `cn_name.split(',')` 只认半角，不拆全角会把「彩虹社，Anycolor」粘成一段显示。改这里时别退回 `",".join([base, ext])`。

`_call_llm` 显式传 `max_tokens`（`.env` 的 `LLM_TEXT_MAX_TOKENS`，默认 8192）——思考与正式回答共享该额度，省略则走服务端默认值，思考占满后返回空 `content`。`content` 为空且 `finish_reason='length'` 时抛 `_OutputTruncated`（`ValueError` 子类，路由的 `except ValueError` 能接住转成 400），与上下文超限一样走**拆半递归重试**，仍失败才显式报错——**不要退回静默返回 `[]`**，那会让调用方以为「这批本来就没结果」，整批白跑、不记历史、一轮轮重试且日志里看不出异常。

**单条翻译公共入口** `translate_one_tag(tag_data, db_path)`：按 `tag_data` 自动判层级（category 3/4 → entity 0.1；有 en_wiki → general 0.4；否则 fallback 0.5），返回 `{cn_name, cn_wiki, nsfw}`，不写库（由调用方决定写回目标）。两处共用：
- `/translate_single_tag`（主表标签）→ 结果经 `_update_tag` 写回 `tags`（受 `cn_name_locked`/`cn_wiki_locked` 守卫）
- `/user_tags/translate`（用户新标签）→ 结果写回 `user_tags`（无 wiki 数据 → 自动走 fallback 层）

BiRefNet 加载：`AutoModelForImageSegmentation.from_pretrained` 加载 base 结构 → `load_state_dict` 覆盖 ToonOut 权重（清洗 `module.`/`module._orig_mod.` 前缀）→ `model.float()` 转 FP32。
