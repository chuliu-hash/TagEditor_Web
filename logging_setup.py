# -*- coding: utf-8 -*-
"""日志配置 —— 统一控制台与文件输出。

为什么需要它：改造前全项目 0 处 logging、246 处 print。问题不是「print 不能用」，
而是：
  - **没有级别**，无法只看错误，也无法按需打开调试输出
  - **没有落盘**，SSE 流里的报错只到 stdout，用户关掉终端或换个窗口就永远丢了，
    事后无法回溯「刚才那次批量翻译为什么失败」
  - **没有时间戳与来源**，长跑管线（抓取/翻译几十分钟）的日志无法定位到时刻

设计取舍：
  - **不动 CLI 的程序输出**。`build_tag_db.py stats` 的统计表、`cooc_pipeline`
    的 PMI/NPMI 表格、`tag_groups` 的进度行是「程序本身的结果」，不是诊断信息；
    加了时间戳前缀反而毁掉对齐、也不利于 `| grep`。那些继续用 print。
  - 控制台输出**默认全开**（保持改造前的观感，用户不会觉得「日志不见了」）；
    觉得吵可以把 `LOG_LEVEL` 调成 `WARNING`。
  - 文件默认 `INFO`，因为落盘的目的是事后回溯，WARNING 会漏掉「做到哪一步失败」。

环境变量：
    LOG_LEVEL       控制台级别（默认 INFO；设为 WARNING 可安静）
    LOG_FILE_LEVEL  文件级别（默认 INFO）
    LOG_DIR         日志目录（默认 logs）
    LOG_TO_FILE     设为 false/0/no 可完全关闭文件输出
"""
import logging
import os
import sys
from logging.handlers import RotatingFileHandler

_configured = False

# 控制台格式刻意短：一行内看到「谁 + 什么级别 + 内容」即可，长跑时刷屏不炸眼
_CONSOLE_FMT = '%(asctime)s [%(levelname)s] %(message)s'
_CONSOLE_DATEFMT = '%H:%M:%S'
# 文件格式带模块名：回溯时第一件事是「哪个文件打的」
_FILE_FMT = '%(asctime)s [%(levelname)s] %(name)s:%(lineno)d - %(message)s'

_MAX_BYTES = 5 * 1024 * 1024   # 单文件 5MB
_BACKUP_COUNT = 5              # 保留 5 份 → 总量上限约 25MB


def _level(name, default):
    v = (os.environ.get(name) or '').strip().upper()
    if not v:
        return default
    return getattr(logging, v, default)


def setup_logging(log_dir=None, force=False):
    """配置根 logger。重复调用是幂等的（除非 force=True）。

    不加 force 的原因是：Flask debug 重载或脚本内多次 import 会重复调用，
    每次加一个 handler 会导致日志成倍重复。
    """
    global _configured
    if _configured and not force:
        return logging.getLogger('tageditor')

    log_dir = log_dir or os.environ.get('LOG_DIR') or 'logs'
    console_level = _level('LOG_LEVEL', logging.INFO)
    file_level = _level('LOG_FILE_LEVEL', logging.INFO)
    to_file = (os.environ.get('LOG_TO_FILE', 'true').strip().lower()
               not in ('false', '0', 'no', 'off'))

    root = logging.getLogger()
    # 清掉已有 handler（force 重配或宿主环境如 gunicorn 预置的）
    for h in list(root.handlers):
        root.removeHandler(h)
    root.setLevel(min(console_level, file_level))

    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(console_level)
    ch.setFormatter(logging.Formatter(_CONSOLE_FMT, _CONSOLE_DATEFMT))
    root.addHandler(ch)

    if to_file:
        try:
            os.makedirs(log_dir, exist_ok=True)
            # delay=True 是必需的，不是优化：Windows 上 RotatingFileHandler 默认
            # 长期持有文件句柄，轮转时 os.rename 会撞 WinError 32（文件被占用），
            # 结果**丢弃日志记录**并往 stderr 打一堆 Logging error。实测不开 delay
            # 必然轮转失败，开了就正常。
            fh = RotatingFileHandler(
                os.path.join(log_dir, 'tageditor.log'),
                maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT,
                encoding='utf-8', delay=True)
            fh.setLevel(file_level)
            fh.setFormatter(logging.Formatter(_FILE_FMT))
            root.addHandler(fh)
        except Exception as e:
            # 日志系统本身不能把应用拖垮：目录不可写就退回「只输出控制台」
            root.warning('日志文件不可用，仅输出到控制台: %s', e)

    # 收敛第三方库的噪音（openai/urllib3 在 HTTP 重试时会刷屏）
    for noisy in ('urllib3', 'openai', 'httpx', 'httpcore', 'PIL', 'matplotlib'):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _configured = True
    root.info('日志已初始化（控制台=%s，文件=%s，目录=%s）',
              logging.getLevelName(console_level),
              logging.getLevelName(file_level) if to_file else '关闭', log_dir)
    return logging.getLogger('tageditor')


def get_logger(name):
    """取 logger。名字用 __name__ 传入即可，文件格式里会显示它。"""
    if not _configured:
        # 没显式初始化时给一个最小可用配置，避免日志石沉大海
        setup_logging()
    return logging.getLogger(name)
