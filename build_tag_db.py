# -*- coding: utf-8 -*-
"""兼容入口：让 `python build_tag_db.py <子命令>` 继续可用。

真正的实现在 tageditor/db/build_tag_db.py（重组时模块收进了包）。
保留这个薄壳有两个原因：
  1. README 与 CLAUDE.md 里记录的命令、以及你自己敲熟的命令不必改
  2. 长跑任务（init / update / llm-process）的文档与笔记里有历史命令

等价的另一种写法（推荐在新脚本/文档里用）：
    python -m tageditor.db.build_tag_db stats

若不再需要旧命令形式，删掉本文件即可 —— 不影响任何功能。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tageditor.db.build_tag_db import main  # noqa: E402

if __name__ == '__main__':
    # **必须透传 main() 的返回值**：`init` 的破坏性守卫（库非空且没给 --yes）
    # 靠返回 1 表达「拒绝执行」，薄壳若只写 `main()`，退出码会被吞成 0，
    # 脚本/CI 就分不出「跑成功了」和「被拦下了」。
    sys.exit(main() or 0)
