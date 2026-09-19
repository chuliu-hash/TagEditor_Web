# -*- coding: utf-8 -*-
"""sync_tags — 从上游 GitHub SQLite 同步新标签到本地数据库。

从 ffdkj/ffdkj-Danbooru_Tag-Chinese-English-Translation-Table 自动下载最新
tag.sqlite，筛选 post_count≥100 且 category∈{0,3,4} 的标签写入本地 SQLite。
"""
import os
import sqlite3
import requests
import time
from pathlib import Path
from config import get_tag_db_config
import logging



log = logging.getLogger(__name__)

def _download_sqlite(save_path: str, cancel_check=None) -> bool:
    """从 GitHub 下载 tag.sqlite，返回是否成功。复用 DANBOORU_PROXY 代理配置。

    先下载到临时文件（save_path + '.tmp'），下载完成后再重命名覆盖目标路径。
    这样下载中断时不会留下破损的 .sqlite 文件，下次可重新下载。

    cancel_check: 可选可调用，返回 True 表示取消下载（清理临时文件后返回 False）。"""
    url = "https://github.com/ffdkj/ffdkj-Danbooru_Tag-Chinese-English-Translation-Table/raw/main/tag.sqlite"
    log.info(f"[SyncTags] 正在下载: {url}")
    proxies = None
    proxy_env = os.environ.get('DANBOORU_PROXY', '').strip()
    if proxy_env:
        proxies = {'http': proxy_env, 'https': proxy_env}
        log.info(f"[SyncTags] 使用代理: {proxy_env}")
    tmp_path = save_path + '.tmp'

    def _cleanup():
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass

    if cancel_check and cancel_check():
        log.info("[SyncTags] 取消下载（下载前）")
        _cleanup()
        return False

    try:
        resp = requests.get(url, stream=True, timeout=120, proxies=proxies)
        resp.raise_for_status()
        total = int(resp.headers.get('content-length', 0))
        downloaded = 0
        _dl_cancelled = False
        with open(tmp_path, 'wb') as f:
            for chunk in resp.iter_content(chunk_size=8192):
                if cancel_check and cancel_check():
                    _dl_cancelled = True
                    break
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total:
                        pct = downloaded / total * 100
                        log.info(f"\r[SyncTags] 下载: {pct:.1f}% ({downloaded}/{total})", end='')
        if _dl_cancelled:
            log.info("\n[SyncTags] 取消下载（下载中）")
            _cleanup()
            return False
        log.info()
        # 下载完成，重命名覆盖
        import os as _os
        if _os.path.exists(save_path):
            _os.remove(save_path)
        _os.rename(tmp_path, save_path)
        log.info(f"[SyncTags] 下载完成: {os.path.abspath(save_path)}")
        return True
    except Exception as e:
        log.error(f"[SyncTags] 下载失败: {e}")
        _cleanup()
        return False


def _get_upstream_conn(sqlite_path: str) -> sqlite3.Connection | None:
    """打开上游 SQLite 连接。"""
    if not os.path.exists(sqlite_path):
        log.info(f"[SyncTags] 找不到文件: {sqlite_path}")
        return None
    try:
        conn = sqlite3.connect(sqlite_path)
        conn.row_factory = sqlite3.Row
        return conn
    except Exception as e:
        log.error(f"[SyncTags] 打开 SQLite 失败: {e}")
        return None


def run(db_path: str = None, download: bool = True, cancel_check=None):
    if db_path is None:
        db_path = get_tag_db_config()['db_path']

    base_dir = Path(db_path).parent
    sqlite_path = str(base_dir / 'raw' / 'tag.sqlite')
    Path(sqlite_path).parent.mkdir(parents=True, exist_ok=True)

    # 下载
    if download and not os.path.exists(sqlite_path):
        ok = _download_sqlite(sqlite_path, cancel_check=cancel_check)
        if not ok:
            return
    elif download:
        log.info(f"[SyncTags] tag.sqlite 已存在: {sqlite_path}")
        # 选择：每次都重下？保持简单，检查文件大小，太小就重下
        size_mb = os.path.getsize(sqlite_path) / 1024 / 1024
        if size_mb < 1:
            log.info(f"[SyncTags] tag.sqlite 过小({size_mb:.1f}MB)，重新下载")
            ok = _download_sqlite(sqlite_path, cancel_check=cancel_check)
            if not ok:
                return

    up_conn = _get_upstream_conn(sqlite_path)
    if up_conn is None:
        return

    # 读取上游数据
    try:
        rows = up_conn.execute(
            "SELECT name, category, cn_name, post_count FROM tags"
        ).fetchall()
    except Exception as e:
        log.error(f"[SyncTags] 读取上游失败: {e}")
        up_conn.close()
        return
    up_conn.close()

    log.info(f"[SyncTags] 上游共有 {len(rows)} 条标签")

    # 连接到本地 DB。
    # 走 build_tag_db.get_conn 而不是裸 sqlite3.connect：后者不带 journal_mode=WAL、
    # 不跑 SCHEMA、不做旧库迁移，也无法与 translation.py 的进程级连接共享锁语义
    # ——本函数与 Web 进程同时运行时裸连接更容易撞 database is locked。
    from build_tag_db import get_conn
    conn = get_conn(db_path)

    try:
        return _sync_into(conn, rows, sqlite_path)
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _sync_into(conn, rows, sqlite_path):
    """把上游 rows 合并进本地 tags 表。连接由调用方负责关闭。"""
    # 获取本地已有标签
    local_names = {r[0] for r in conn.execute("SELECT name FROM tags").fetchall()}
    log.info(f"[SyncTags] 本地现有 {len(local_names)} 条标签")

    # 筛选：post_count≥100, category∈{0,3,4}, 本地不存在
    new_tags = []
    for r in rows:
        name = r['name']
        cat = int(r['category']) if r['category'] is not None else -1
        pc = int(r['post_count']) if r['post_count'] is not None else 0
        cn = (r['cn_name'] or '').strip()
        if pc >= 100 and cat in (0, 3, 4) and name not in local_names:
            new_tags.append((name, cn, cat, pc))

    if not new_tags:
        log.info(f"[SyncTags] 没有符合条件的新标签")
    else:
        log.info(f"[SyncTags] 发现 {len(new_tags)} 个新标签")
        try:
            conn.executemany(
                "INSERT OR IGNORE INTO tags (name, cn_name, category, post_count) VALUES (?, ?, ?, ?)",
                new_tags
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        log.info(f"[SyncTags] 已写入 {len(new_tags)} 条新标签")

    # 更新已有标签的 post_count 和 category（不覆盖 cn_name）
    # 用 dict 加速上游查询
    up_map = {r['name']: r for r in rows}
    upd_params = []
    for name in local_names:
        r = up_map.get(name)
        if r is None:
            continue
        cat = int(r['category']) if r['category'] is not None else -1
        pc = int(r['post_count']) if r['post_count'] is not None else 0
        upd_params.append((cat, cat, pc, pc, name))
    update_count = len(upd_params)
    if upd_params:
        try:
            conn.executemany(
                "UPDATE tags SET category = CASE WHEN ? >= 0 THEN ? ELSE category END, "
                "post_count = CASE WHEN ? > 0 THEN ? ELSE post_count END WHERE name = ?",
                upd_params
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    log.warning(f"[SyncTags] 已更新 {update_count} 条已有标签的 category/post_count")

    # 清理下载的临时文件
    if os.path.exists(sqlite_path):
        os.remove(sqlite_path)
        log.info(f"[SyncTags] 已删除临时文件: {sqlite_path}")
