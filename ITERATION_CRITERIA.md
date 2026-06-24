# 迭代验收判据（Iteration Acceptance Criteria）

本文件为开放式目标「对项目前后端迭代优化 修复潜在bug 美化前端 提高性能 加强体验」
定义**可逐条核对**的客观判据。每轮迭代需满足全部「基线回归」+ 至少满足「性能阈值」
与「功能场景」中尚未达标的新增项，方可视为该轮迭代完成。

> 「迭代优化」本身无终点，但每**一轮**迭代以「本文件全部判据通过 + 本轮新增目标达成」
> 作为可验证的收尾标准。

---

## 一、基线回归（每轮必须全通过）

每轮迭代交付前，必须通过以下回归测试（已有 `_regression.py` 套件覆盖）：

| 编号 | 判据 | 验证方式 |
|---|---|---|
| R1 | 8 个 Python 文件 `python -m py_compile` 全通过 | 语法检查 |
| R2 | `import app` + 5 个 blueprint 模块导入无异常 | 导入测试 |
| R3 | `PRAGMA journal_mode` = `wal` | DB 配置 |
| R4 | FTS5 表 `tags_fts` 存在且行数 == tags 行数 | DB 索引 |
| R5 | FTS5 搜索结果与旧 LIKE 实现**逐例一致**（≥5 用例含空格/连字符/中文） | 对照测试 |
| R6 | 单次 FTS5 搜索耗时 < 500ms | 性能测试 |
| R6.1 | **搜索 P95 延迟 < 200ms**（本轮新增基线，10 次采样取 P95） | 性能阈值 |
| R7 | 带空格标签翻译能查到（`on bed` → `在床上`） | key 一致性 |
| R8 | 带连字符标签翻译能查到（`side-tie_panties`） | key 一致性 |
| R9 | 3 个工作线程各得**独立** requests.Session | 线程隔离 |
| R10 | 16 个核心端点全部注册 + `/editor` 已重命名为 `/img_editor` | 路由 |
| R11 | `batch_translate` 返回 HTTP 200（无 500） | 回归 bug 守卫 |
| R12 | 全项目无原生 `confirm(` / `alert(` / `prompt(` | 体验统一 |
| R13 | 事务原子性：INSERT 中途失败后旧数据完整保留（`test_rollback_on_failure ... ok`） | 事务测试 |

---

## 二、性能阈值（本轮已达标，后续不得回退）

| 编号 | 指标 | 阈值 | 本轮实测 | 达标 |
|---|---|---|---|---|
| P1 | 单次标签搜索（FTS5） | < 200ms（P95） | ~130ms | ✅ |
| P2 | 并发读 + 写事务（WAL）读延迟 | < 100ms | 15ms | ✅ |
| P3 | 全量重建 init_from_files（10万级）事务回滚正确 | 失败时旧数据 0 丢失 | 100/100 保留 | ✅ |
| P4 | 批量翻译并发吞吐 | 并发度 2 时吞吐 ≈ 串行 2x | 默认 concurrency=2 | ✅ |
| P5 | **批量翻译 500 标签端到端** | **< 60s**（LLM API 可用时） | 待实测（需 LLM） | ⏳ |

---

## 三、功能场景（必须通过的端到端流程）

| 编号 | 场景 | 通过判据 |
|---|---|---|
| F1 | 搜索 `on bed` 返回 ≥6 条且含 `on_bed` | `/danbooru_search` |
| F2 | 搜索 `side tie` 命中连字符标签 `side-tie_bikini` 等 | 连字符兼容 |
| F3 | 反向查询 `在床上` → `on_bed` | zh→en |
| F4 | `lookup_cache ['on bed','1girl']` → `['在床上','1个女孩']` | en→zh |
| F5 | `/tag_detail/on_bed` 返回 `cn_name=在床上` 且 `en_wiki` 非空 | 详情完整 |
| F6 | `/tag_stats` 返回统计数组 + total_files | 统计 |
| F7 | 快速「保存 + 切图」不写错文件（saveCaption 快照守卫） | 竞态保护 |
| F8 | 放大进行中导航不覆盖新图（runUpscale 守卫） | 竞态保护 |
| F9 | 查找替换后统计弹窗刷新（reloadTagStats） | 数据同步 |
| F10 | 删除/清空/未保存切换弹自定义弹窗（非原生 confirm） | 体验统一 |

---

## 四、Bug 清单（本轮已修复，后续不得复现）

| 编号 | Bug | 修复证据 |
|---|---|---|
| B1 | saveCaption/saveImage 异步保存中导航 → 写错文件 | 快照守卫 |
| B2 | runUpscale/runRemoveBg/runAlphaSingle 处理中导航 → 覆盖新图 | 索引守卫 |
| B3 | batch_translate 500（os 作用域 UnboundLocalError） | import os 移顶部 |
| B4 | init_from_files 无事务 → 半成品空库 | BEGIN/ROLLBACK |
| B5 | saveCaption 保存失败仍导航（数据丢失） | reject + .catch |
| B6 | SSE 生成器异常逃逸 → 流卡死 | 四处 try/except fatal |
| B7 | SSE 读取器无 .catch + 提前 done | finishStream 兜底 |
| B8 | executeTagStatsReplace 替换后统计不刷新 | reloadTagStats |
| B9 | data.total='?' → NaN 进度条 | 类型守卫 |
| B10 | onTagChanged/详情/搜索 响应竞态 | 序列守卫 |

---

## 五、使用方式

每轮迭代收尾时：
1. 运行 `_regression.py`（或等价套件），**第一、二节全部 ✓**
2. 核对本文件「性能阈值」「功能场景」**无回退**
3. 本轮新增优化项的目标达成后，**追加到对应表格**作为下轮基线
4. git commit + 合并 + push

满足以上即视为**该轮迭代可验证完成**。
