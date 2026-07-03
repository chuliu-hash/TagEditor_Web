# -*- coding: utf-8 -*-
"""Danbooru 标签本地数据库构建与查询（SQLite）。

精简 schema，只保留核心字段：
    tags(name PK, cn_name, en_wiki, cn_wiki, other_names, updated_at)

来源数据：
- tags_enhanced.csv：name, cn_name(中文翻译，逗号分隔), wiki(中文描述)
- wiki_pages.parquet：title, body(英文 wiki), other_names(多语言别名), updated_at

用法：
    python build_tag_db.py init --csv D:/Download/data/processed/tags_enhanced.csv \
                                --parquet D:/Download/data/processed/wiki_pages.parquet
    python build_tag_db.py stats
"""
import argparse
import os
import sqlite3
from pathlib import Path

from config import get_tag_db_config


SCHEMA = """
CREATE TABLE IF NOT EXISTS tags (
    name        TEXT PRIMARY KEY,
    cn_name     TEXT NOT NULL DEFAULT '',
    en_wiki     TEXT NOT NULL DEFAULT '',
    cn_wiki     TEXT NOT NULL DEFAULT '',
    other_names TEXT NOT NULL DEFAULT '[]',   -- JSON 数组字符串（多语言别名）
    category    INTEGER NOT NULL DEFAULT -1,  -- Danbooru 标签分类：0=通用/1=艺术家/3=版权/4=角色/5=元数据，-1=未知
    post_count  INTEGER NOT NULL DEFAULT 0,   -- Danbooru 使用该标签的图片数（热门度，用于搜索排序）
    updated_at  TEXT NOT NULL DEFAULT '',
    nsfw        INTEGER NOT NULL DEFAULT 0,   -- NSFW 标记：0=安全 1=不安全
    cn_name_locked INTEGER NOT NULL DEFAULT 0, -- 锁定中文名（禁止深度翻译/手动编辑覆盖）
    cn_wiki_locked INTEGER NOT NULL DEFAULT 0  -- 锁定中文 wiki（禁止深度翻译/手动编辑覆盖）
);
-- 抓取状态表：记录各 API（tags.json/wiki_pages.json）上次抓取的「时间锚点」等增量状态。
-- 锚点不属于某个标签行（是全局抓取进度），故用独立状态表而非给 tags 加列。
-- key 为主键（如 'tags_anchor'），value 存 ISO 时间戳字符串（updated_at）。
CREATE TABLE IF NOT EXISTS fetch_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);
"""

# search_tags 全文索引：FTS5 trigram 虚拟表，对 name(规范化) + other_names + cn_name 做子串匹配。
# trigram 分词器原生支持任意子串（≥3 字符），配合 name_norm（连字符→下划线）实现
# 「on bed / on_bed / side-tie」三种写法互通。cn_name 也纳入 FTS（≥3 字符查询走 FTS，~2ms；
# <3 字符的短中文查询回退 LIKE）。比 LIKE 全表扫描快约 1000 倍（2ms vs 2s），P95 < 200ms。
# 由 _ensure_fts_index 建表 + 触发器，自动随 tags 表增删改同步，无需每个写入点手动维护。
_FTS_INDEX_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS tags_fts USING fts5(
    name_norm,        -- 规范化 name（连字符→下划线），与 keyword 规范化口径一致
    other_names,
    cn_name,          -- 中文翻译（≥3 字符查询走 FTS，避免 cn_name LIKE 全表扫 ~160ms）
    content='',       -- contentless：自身存副本，避免外部内容表的 rowid 对齐负担
    tokenize = "trigram"
);
"""

# 同步触发器：tags 表增删改时，自动维护 tags_fts。
# name_norm 在触发器内对 NEW.name 做 REPLACE('-','_') 计算，保证两侧规范化口径一致。
_FTS_TRIGGERS_SQL = """
CREATE TRIGGER IF NOT EXISTS tags_fts_ai AFTER INSERT ON tags BEGIN
    INSERT INTO tags_fts(rowid, name_norm, other_names, cn_name)
    VALUES (NEW.rowid, REPLACE(NEW.name, '-', '_'), NEW.other_names, NEW.cn_name);
END;
CREATE TRIGGER IF NOT EXISTS tags_fts_ad AFTER DELETE ON tags BEGIN
    INSERT INTO tags_fts(tags_fts, rowid, name_norm, other_names, cn_name)
    VALUES ('delete', OLD.rowid, REPLACE(OLD.name, '-', '_'), OLD.other_names, OLD.cn_name);
END;
CREATE TRIGGER IF NOT EXISTS tags_fts_au AFTER UPDATE OF name, other_names, cn_name ON tags BEGIN
    INSERT INTO tags_fts(tags_fts, rowid, name_norm, other_names, cn_name)
    VALUES ('delete', OLD.rowid, REPLACE(OLD.name, '-', '_'), OLD.other_names, OLD.cn_name);
    INSERT INTO tags_fts(rowid, name_norm, other_names, cn_name)
    VALUES (NEW.rowid, REPLACE(NEW.name, '-', '_'), NEW.other_names, NEW.cn_name);
END;
"""


def _ensure_fts_index(conn):
    """确保 FTS5 索引表与同步触发器存在。
    全量重建场景（init_from_files 先 DELETE 再批量 INSERT）会通过触发器自动填充索引，
    无需手动重建。若 FTS 表已存在但为空（旧库升级），调用 _rebuild_fts_index 补数据。
    若旧版 FTS 表列集合不符（如缺少 cn_name 列），DROP 表 + 旧触发器后重建为新结构。
    CREATE TRIGGER IF NOT EXISTS 不会更新已存在的触发器，故旧版升级时必须先 DROP 再 CREATE。"""
    needs_rebuild = False
    if _table_exists(conn, 'tags_fts'):
        fts_cols = {r[1] for r in conn.execute("PRAGMA table_info(tags_fts)").fetchall()}
        if 'cn_name' not in fts_cols:
            # 旧版 FTS 表：DROP 表 + 三个旧触发器，重新创建带 cn_name 的新版
            conn.executescript("""
                DROP TABLE IF EXISTS tags_fts;
                DROP TRIGGER IF EXISTS tags_fts_ai;
                DROP TRIGGER IF EXISTS tags_fts_ad;
                DROP TRIGGER IF EXISTS tags_fts_au;
            """)
            needs_rebuild = True
    conn.executescript(_FTS_INDEX_SQL)
    conn.executescript(_FTS_TRIGGERS_SQL)
    return needs_rebuild  # 调用方可据此触发 _rebuild_fts_index 填充数据


def _rebuild_fts_index(conn):
    """全量重建 FTS5 索引内容（清空后从 tags 表重新填充）。
    用于：旧库首次升级到 FTS5（触发器建好后表仍空），或索引损坏修复。"""
    conn.execute("DELETE FROM tags_fts")
    conn.execute("""
        INSERT INTO tags_fts(rowid, name_norm, other_names, cn_name)
        SELECT rowid, REPLACE(name, '-', '_'), other_names, cn_name FROM tags
    """)

# 已废弃的旧列名（用于迁移检测）。迁移时若 tags 表含任一此列，则重建为目标结构。
# 注意：category/post_count 曾在早期版本存在后被移除，现在重新加入。nsfw 仍属废弃。
_LEGACY_COLUMNS = ()


def _has_legacy_columns(conn):
    """检测 tags 表是否含已废弃列（旧 schema）。返回 bool。"""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(tags)").fetchall()}
    return any(c in cols for c in _LEGACY_COLUMNS)


def _migrate_to_target_schema(conn):
    """把任意旧 schema 迁移为当前目标结构
    （name/cn_name/en_wiki/cn_wiki/other_names/category/post_count/updated_at/nsfw）。
    保留已有数据，丢弃其他列；缺失的列补默认值。无事务包裹：调用方负责 commit。
    若已是目标 schema 则无操作。"""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(tags)").fetchall()}
    target = {'name', 'cn_name', 'en_wiki', 'cn_wiki', 'other_names', 'category', 'post_count', 'updated_at', 'nsfw', 'cn_name_locked', 'cn_wiki_locked'}
    if cols == target:
        return False
    # 重建：仅 SELECT 目标列（缺失列用 COALESCE 补默认）。旧索引随旧表 DROP。
    select_parts = []
    for c in ['name', 'cn_name', 'en_wiki', 'cn_wiki']:
        select_parts.append(c if c in cols else "'' AS " + c)
    # other_names 缺失时补 '[]'
    select_parts.append('other_names' if 'other_names' in cols else "'[]' AS other_names")
    # category/post_count/updated_at 缺失时补默认值
    select_parts.append('category' if 'category' in cols else '-1 AS category')
    select_parts.append('post_count' if 'post_count' in cols else '0 AS post_count')
    select_parts.append('updated_at' if 'updated_at' in cols else "'' AS updated_at")
    # nsfw 缺失时补默认值
    select_parts.append('nsfw' if 'nsfw' in cols else '0 AS nsfw')
    # cn_name_locked / cn_wiki_locked 缺失时补默认值
    select_parts.append('cn_name_locked' if 'cn_name_locked' in cols else '0 AS cn_name_locked')
    select_parts.append('cn_wiki_locked' if 'cn_wiki_locked' in cols else '0 AS cn_wiki_locked')
    conn.executescript(f"""
        CREATE TABLE tags_new (
            name        TEXT PRIMARY KEY,
            cn_name     TEXT NOT NULL DEFAULT '',
            en_wiki     TEXT NOT NULL DEFAULT '',
            cn_wiki     TEXT NOT NULL DEFAULT '',
            other_names TEXT NOT NULL DEFAULT '[]',
            category    INTEGER NOT NULL DEFAULT -1,
            post_count  INTEGER NOT NULL DEFAULT 0,
            updated_at  TEXT NOT NULL DEFAULT '',
            nsfw        INTEGER NOT NULL DEFAULT 0,
            cn_name_locked INTEGER NOT NULL DEFAULT 0,
            cn_wiki_locked INTEGER NOT NULL DEFAULT 0
        );
        INSERT INTO tags_new (name, cn_name, en_wiki, cn_wiki, other_names, category, post_count, updated_at, nsfw, cn_name_locked, cn_wiki_locked)
        SELECT {', '.join(select_parts)} FROM tags;
        DROP TABLE tags;
        ALTER TABLE tags_new RENAME TO tags;
    """)
    # 迁移后 tags 的 rowid 重排，FTS 索引（若存在）的 rowid 已失效。
    # contentless FTS5 表不支持 DELETE，直接 DROP 表 + 触发器，由 get_conn → _ensure_fts_index 重建。
    if _table_exists(conn, 'tags_fts'):
        conn.executescript("""
            DROP TABLE IF EXISTS tags_fts;
            DROP TRIGGER IF EXISTS tags_fts_ai;
            DROP TRIGGER IF EXISTS tags_fts_ad;
            DROP TRIGGER IF EXISTS tags_fts_au;
        """)
    return True


def _table_exists(conn, table_name):
    """检查表（含虚拟表）是否存在。"""
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name = ?", (table_name,)
    ).fetchone() is not None


def get_conn(db_path=None):
    """打开 SQLite 连接（自动建父目录）。
    若表是旧 schema（列集合与目标不符），自动迁移为目标结构。
    busy_timeout=5000：写锁竞争时最多等待 5s 再报错。
    journal_mode=WAL：写时用 WAL，允许「增量更新线程写」与「查询线程读」并发不阻塞
        （默认 delete 模式下，长事务写入会锁库，并发查询可能 'database is locked'）。"""
    if db_path is None:
        db_path = get_tag_db_config()['db_path']
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=5)
    conn.execute('PRAGMA busy_timeout = 5000')
    conn.execute('PRAGMA journal_mode = WAL')
    conn.executescript(SCHEMA)
    # 旧库兼容：列集合与目标不符就迁移
    if _migrate_to_target_schema(conn):
        conn.commit()
    # FTS5 全文索引：建表 + 触发器；若索引为空（旧库升级或迁移后）补数据
    fts_rebuilt = _ensure_fts_index(conn)
    fts_count = conn.execute("SELECT count(*) FROM tags_fts").fetchone()[0]
    tag_count = conn.execute("SELECT count(*) FROM tags").fetchone()[0]
    if tag_count > 0 and (fts_count == 0 or fts_rebuilt):
        _rebuild_fts_index(conn)
        conn.commit()
    return conn


def _normalize_other_names(raw) -> str:
    """把 other_names 序列化为标准 JSON 数组字符串。
    兼容多种原始格式：list / 标准 JSON 字符串 / Python list 字面量字符串（单引号，parquet 存储格式）。
    单个裸字符串（非数组形式）会被包成单元素数组，避免数据丢失。"""
    import json
    if isinstance(raw, list):
        cleaned = [str(x).strip() for x in raw if isinstance(x, str) and x.strip()]
        return json.dumps(cleaned, ensure_ascii=False)
    if isinstance(raw, str):
        raw = raw.strip()
        if not raw or raw in ('[]', 'nan', 'None'):
            return '[]'
        # 先尝试标准 JSON（双引号）
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return json.dumps([str(x).strip() for x in parsed if isinstance(x, str) and x.strip()], ensure_ascii=False)
            if isinstance(parsed, str):  # JSON 字符串值，包成数组
                return json.dumps([parsed.strip()], ensure_ascii=False) if parsed.strip() else '[]'
        except Exception:
            pass
        # 再尝试 Python list 字面量（单引号，parquet 存储格式）
        try:
            import ast
            parsed = ast.literal_eval(raw)
            if isinstance(parsed, list):
                return json.dumps([str(x).strip() for x in parsed if isinstance(x, str) and x.strip()], ensure_ascii=False)
        except Exception:
            pass
        # 兜底：无法解析为数组，视为单个别名
        return json.dumps([raw], ensure_ascii=False)
    return '[]'


def upsert_tag(conn, name, cn_name='', en_wiki='', cn_wiki='', other_names='[]', updated_at=''):
    """单条 UPSERT（全量写入，受 updated_at 时间戳守卫）"""
    conn.execute("""
        INSERT INTO tags (name, cn_name, en_wiki, cn_wiki, other_names, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(name) DO UPDATE SET
            cn_name=excluded.cn_name,
            en_wiki=excluded.en_wiki,
            cn_wiki=excluded.cn_wiki,
            other_names=excluded.other_names,
            updated_at=excluded.updated_at
        WHERE excluded.updated_at > tags.updated_at OR tags.updated_at = ''
    """, (name, cn_name, en_wiki, cn_wiki, other_names, updated_at))


def lookup_tags(conn, tags):
    """批量查标签翻译。返回 {name: {cn_name, en_wiki, cn_wiki, other_names}}（key 为规范化 name）。

    分批查询（每批 ≤ 500 个占位符）：SQLite 默认 SQLITE_MAX_VARIABLE_NUMBER=999，
    一次 IN (?,...) 超过限制会抛 OperationalError: too many SQL variables。
    tag_stats 可能传入数千个唯一标签，故分块避免崩溃。
    去重输入减少重复查询（同 tag 多次出现只查一次）。"""
    if not tags:
        return {}
    # 规范化 + 去重，减少查询次数（统一小写+空格→下划线，与保存口径一致）
    norm_map = {}  # name -> 原始 tags 中的引用（无实际用途，仅去重）
    norm_list = []
    for t in tags:
        n = t.strip().replace(' ', '_').lower()
        if n and n not in norm_map:
            norm_map[n] = True
            norm_list.append(n)

    result = {}
    BATCH = 500  # 远低于 999 上限，留余量
    for i in range(0, len(norm_list), BATCH):
        chunk = norm_list[i:i+BATCH]
        placeholders = ','.join('?' * len(chunk))
        rows = conn.execute(
            f"SELECT name, cn_name, en_wiki, cn_wiki, other_names, category, post_count, cn_name_locked, cn_wiki_locked "
            f"FROM tags WHERE name IN ({placeholders})",
            chunk
        ).fetchall()
        for r in rows:
            result[r[0]] = {
                'cn_name': r[1], 'en_wiki': r[2], 'cn_wiki': r[3], 'other_names': r[4],
                'category': r[5], 'post_count': r[6], 'cn_name_locked': r[7], 'cn_wiki_locked': r[8],
            }
    return result


def lookup_tag_en_wiki(conn, tag):
    """单标签查英文 wiki（翻译时取 body 注入 LLM 用）"""
    row = conn.execute("SELECT en_wiki FROM tags WHERE name = ?", (tag.strip().replace(' ', '_').lower(),)).fetchone()
    return row[0] if row else ''


def update_translation(conn, name, cn_name, commit=True):
    """更新标签的中文翻译（cn_name 列）。标签不存在时自动插入空记录。
    用于：LLM 翻译回写、用户手动编辑翻译回写。不受 updated_at 时间戳守卫限制。
    受 cn_name_locked 守卫：锁定后跳过更新。
    commit=False 时延迟提交（批量场景由调用方统一 commit，避免逐条磁盘同步拖慢）。"""
    name = name.strip().replace(' ', '_').lower()
    if not name:
        return
    conn.execute("""
        INSERT INTO tags (name, cn_name) VALUES (?, ?)
        ON CONFLICT(name) DO UPDATE SET cn_name = excluded.cn_name
        WHERE cn_name_locked IS NULL OR cn_name_locked = 0
    """, (name, cn_name.strip()))
    if commit:
        conn.commit()


def update_cn_wiki(conn, name, cn_wiki, commit=True):
    """更新标签的中文 wiki（cn_wiki 列）。标签不存在时自动插入空记录。
    用于：详情弹窗翻译英文 wiki 后回写、用户手动编辑中文 wiki 后回写。
    不受 updated_at 时间戳守卫限制。受 cn_wiki_locked 守卫：锁定后跳过更新。"""
    name = name.strip().replace(' ', '_').lower()
    if not name:
        return
    conn.execute("""
        INSERT INTO tags (name, cn_wiki) VALUES (?, ?)
        ON CONFLICT(name) DO UPDATE SET cn_wiki = excluded.cn_wiki
        WHERE cn_wiki_locked IS NULL OR cn_wiki_locked = 0
    """, (name, cn_wiki.strip()))
    if commit:
        conn.commit()


def update_en_wiki(conn, name, en_wiki, commit=True):
    """更新标签的英文 wiki（en_wiki 列）。标签不存在时自动插入空记录。
    用于：用户手动编辑英文 wiki 后回写。不受 updated_at 时间戳守卫限制，
    因此手动编辑不会被 Danbooru 增量更新误判为时间锚点前移。"""
    name = name.strip().replace(' ', '_').lower()
    if not name:
        return
    conn.execute("""
        INSERT INTO tags (name, en_wiki) VALUES (?, ?)
        ON CONFLICT(name) DO UPDATE SET en_wiki = excluded.en_wiki
    """, (name, en_wiki.strip()))
    if commit:
        conn.commit()


def upsert_wiki_incremental(conn, name, en_wiki, other_names, updated_at):
    """增量更新专用：仅写入 en_wiki/other_names/updated_at 三列，保留 cn_name/cn_wiki 不变。
    用于 update_from_danbooru。仅 UPDATE 已存在于本地的标签（即 sync_tags_db 同步过的热标签），
    不 INSERT 新行，避免非热标签混入数据库。
    WHERE 守卫保证只接受比本地更新的 updated_at，避免把已存在的翻译/wiki 覆盖。"""
    name = name.strip().replace(' ', '_').lower()
    conn.execute("""
        UPDATE tags SET en_wiki = ?, other_names = ?, updated_at = ?
        WHERE name = ? AND (? > updated_at OR updated_at = '')
    """, (en_wiki, other_names, updated_at, name, updated_at))


def update_tag_meta(conn, name, category, post_count, commit=True):
    """更新标签的 category 和 post_count（来自 Danbooru /tags.json）。
    不受 updated_at 守卫限制（与翻译回写同理），category=-1/post_count=0 时不覆盖已有非默认值。
    用于增量更新时从 tags.json 补充元数据。"""
    name = name.strip().replace(' ', '_').lower()
    if not name:
        return
    conn.execute("""
        INSERT INTO tags (name, category, post_count) VALUES (?, ?, ?)
        ON CONFLICT(name) DO UPDATE SET
            category = CASE WHEN excluded.category >= 0 THEN excluded.category ELSE tags.category END,
            post_count = CASE WHEN excluded.post_count > 0 THEN excluded.post_count ELSE tags.post_count END
    """, (name, category, post_count))
    if commit:
        conn.commit()


def get_fetch_state(conn, key, default=''):
    """读取抓取状态（fetch_state 表）。key 不存在时返回 default。"""
    row = conn.execute("SELECT value FROM fetch_state WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def set_fetch_state(conn, key, value, commit=True):
    """写入抓取状态（fetch_state 表）。用于记录 tags.json 增量锚点等全局抓取进度。
    commit=False 时延迟提交（与 update_tag_meta 同语义，批量场景由调用方统一 commit）。"""
    conn.execute("""
        INSERT INTO fetch_state (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
    """, (key, value))
    if commit:
        conn.commit()


def lookup_tag_by_cn(conn, cn_name):
    """反向查找：中文翻译 → 英文标签名（中译英用）。返回英文 name 或空字符串。
    匹配 cn_name 的第一个逗号分隔项（cn_name 可能是"蓝发,蓝色头发"多词形式）。
    无 post_count 后改按 name 字母序取首条。"""
    if not cn_name:
        return ''
    cn_first = cn_name.strip().split(',')[0].strip()
    if not cn_first:
        return ''
    # 在 cn_name 字段里查找：整字段等于、或以"cn_first,"开头（多词形式的第一项）
    row = conn.execute(
        "SELECT name FROM tags WHERE cn_name = ? OR cn_name LIKE ? ORDER BY name LIMIT 1",
        (cn_first, cn_first + ',%')
    ).fetchone()
    return row[0] if row else ''


def search_tags(conn, keyword, limit=20):
    """模糊搜索标签。匹配 name / cn_name / other_names 三列，按 name 字母序。
    keyword 为空时返回空列表。返回 list[dict]（与 lookup_tags 的 info 结构一致）。

    空格/下划线/连字符兼容：Danbooru 标签命名规则是「空格→下划线，连字符保留」
    （如 side-tie_panties、on_bed）。但用户搜索时习惯用空格（on bed、side tie）。
    为兼容三种写法，keyword 规范化为「空格/连字符→下划线」，FTS 索引的 name_norm
    列已预存「连字符→下划线」的规范化 name，两侧口径一致：
        用户输入 "side tie" → 规范化 "side_tie" → FTS 匹配 name_norm "side_tie_panties" ✓
        用户输入 "on bed"  → 规范化 "on_bed"  → FTS 匹配 name_norm "on_bed" ✓

    性能：name/other_names/cn_name 均走 FTS5 trigram 索引（子串匹配，≈2ms）。
    keyword 规范化后 <3 字符时 trigram 无法用，回退全表 LIKE（仅扫 name/cn_name/other_names）。
    cn_name 用原始 keyword 匹配（中文无需空格/连字符规范化）。

    keyword 中的 FTS 特殊字符（" * ( ) 等）会按 FTS5 双引号转义，避免被当查询语法。"""
    kw = (keyword or '').strip()
    if not kw:
        return []

    # 统一小写 + 空格/连字符→下划线，匹配 name_norm 的规范化口径（保存统一小写，搜索也应小写）
    kw_norm = kw.replace(' ', '_').replace('-', '_').lower()

    def _make_like_pattern(text):
        esc = text.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
        return '%' + esc + '%'

    cn_pat = _make_like_pattern(kw)        # cn_name 用原始 keyword（中文）
    name_pat = _make_like_pattern(kw_norm) # name/other_names 用规范化 keyword

    # trigram 要求 keyword ≥3 字符才能命中（<3 时退化为全表 LIKE）
    use_fts = len(kw) >= 3 and _table_exists(conn, 'tags_fts')

    if use_fts:
        # FTS5 trigram MATCH：双引号包裹避免特殊字符被当查询语法。
        # name_norm/other_names 用规范化 keyword；cn_name 用原始 keyword（中文无需规范化）。
        # 三列用列限定语法「col:"q"」+ OR 合并为一个 MATCH（单次 FTS 索引扫描，最快）。
        fts_q_norm = kw_norm.replace('"', '""')
        fts_q_kw = kw.replace('"', '""')
        match_expr = 'name_norm:"{0}" OR other_names:"{0}" OR cn_name:"{1}"'.format(fts_q_norm, fts_q_kw)
        rows = conn.execute(
            """
            SELECT name, cn_name, en_wiki, cn_wiki, other_names, category, post_count FROM tags WHERE rowid IN (
                SELECT rowid FROM tags_fts WHERE tags_fts MATCH ?
            )
            ORDER BY post_count DESC, length(name), name
            LIMIT ?
            """,
            (match_expr, limit)
        ).fetchall()
    else:
        # 短 keyword（<3 字符，trigram 无效）或 FTS 未建：回退全表 LIKE
        rows = conn.execute(
            """
            SELECT name, cn_name, en_wiki, cn_wiki, other_names, category, post_count
            FROM tags
            WHERE REPLACE(name, '-', '_') LIKE ? ESCAPE '\\'
               OR cn_name LIKE ? ESCAPE '\\'
               OR REPLACE(other_names, '-', '_') LIKE ? ESCAPE '\\'
            ORDER BY post_count DESC, length(name), name
            LIMIT ?
            """,
            (name_pat, cn_pat, name_pat, limit)
        ).fetchall()
    return [{
        'name': r[0], 'cn_name': r[1], 'en_wiki': r[2], 'cn_wiki': r[3], 'other_names': r[4],
        'category': r[5], 'post_count': r[6],
    } for r in rows]


def init_from_files(db_path, csv_path, parquet_path, verbose=True):
    """从 tags_enhanced.csv + wiki_pages.parquet 构建本地数据库（全量重建）"""
    import pandas as pd

    if verbose:
        print(f"[BuildTagDB] 读取 CSV: {csv_path}")
    df_csv = pd.read_csv(csv_path, dtype=str).fillna('')

    if verbose:
        print(f"[BuildTagDB] 读取 Parquet: {parquet_path}")
    df_wiki = pd.read_parquet(parquet_path, columns=['title', 'body', 'other_names', 'updated_at'])
    wiki_map = {}
    for _, row in df_wiki.iterrows():
        title = row['title']
        if not isinstance(title, str) or not title.strip():
            continue
        # body 可能为 NaN(float)，统一转 str 后空字符串处理
        body = row.get('body', '')
        body = '' if not isinstance(body, str) else body
        wiki_map[title.strip()] = {
            'body': body,
            'other_names': _normalize_other_names(row.get('other_names')),
            'updated_at': str(row.get('updated_at', '') or ''),
        }

    conn = get_conn(db_path)
    try:
        # 原子性：DELETE + INSERT 包在显式事务里，中途异常（内存不足/Ctrl+C/parquet 损坏等）
        # 一律 ROLLBACK，避免「旧数据已删、新数据未插」的半成品空库。
        conn.execute("BEGIN")
        conn.execute("DELETE FROM tags")  # 全量重建
        batch = []
        for _, r in df_csv.iterrows():
            name = r['name'].strip()
            if not name:
                continue
            w = wiki_map.get(name, {})
            # CSV 的 wiki 列是中文描述，parquet 的 body 是英文 wiki
            cn_wiki = (r.get('wiki') or '').strip()
            en_wiki = (w.get('body') or '').strip()
            other_names = w.get('other_names', '[]')
            updated_at = w.get('updated_at', '')
            batch.append((name, r['cn_name'].strip(), en_wiki, cn_wiki, other_names, updated_at))
        # 补充：parquet 中存在但 csv 中没有的标签（无中文翻译）
        csv_names = {r[0] for r in batch}
        for title, w in wiki_map.items():
            if title in csv_names:
                continue
            batch.append((title, '', w['body'].strip(), '', w['other_names'], w['updated_at']))

        # 去重：同一 name 可能多次出现，保留首次（csv 优先于 parquet-only）
        seen = set()
        deduped = []
        for row in batch:
            if row[0] in seen:
                continue
            seen.add(row[0])
            deduped.append(row)

        conn.executemany(
            "INSERT INTO tags (name, cn_name, en_wiki, cn_wiki, other_names, updated_at) "
            "VALUES (?,?,?,?,?,?)",
            deduped
        )
        conn.commit()
        if verbose:
            print(f"[BuildTagDB] 构建完成：{len(deduped)} 条记录 → {db_path}")
    except Exception:
        conn.rollback()
        if verbose:
            print("[BuildTagDB] 构建失败，已回滚（旧数据保留，未产生半成品库）")
        raise
    finally:
        conn.close()


def merge_local_sources(db_path, sqlite_src=None, wiki_parquet=None, csv_src=None, verbose=True):
    """从本地数据源增量补全数据库（保留现有数据，只填缺失字段）。

    与 init_from_files 的区别：
      · init_from_files：DELETE 清空后全量重建，会丢失现有翻译/wiki
      · merge_local_sources：永不覆盖已有非空字段，仅补缺（安全）

    三个数据源互补，均可选（传 None 跳过）：
      · raw/tag.sqlite（29.6 万条）：name/category/post_count/cn_name
        → 补 category/post_count 缺口（覆盖远超热门前 10 万），cn_name 仅在本地为空时补
      · wiki_pages.parquet（27 万条）：title/body(en_wiki)/other_names/updated_at
        → 补 en_wiki/other_names 缺口
      · tags_enhanced.csv（4.8 万条）：name/cn_name/wiki(中文描述)/post_count/category
        → cn_name 质量最高（逗号分隔多词），但仅在本地为空时补；cn_wiki 仅在为空时补

    覆盖规则（所有字段统一）：
      · category/post_count：本地无值（category=-1 / post_count=0）才补，否则保留
      · cn_name/cn_wiki/en_wiki/other_names：本地为空才补，否则保留
      · updated_at：parquet 的较新，但仅当本地为空或新值更大时更新（守卫不回退）

    用临时内存表批量 upsert，逐条更新 ~30 万行会非常慢，故用 executemany 分批。
    """
    import sqlite3 as sql3

    def _v(msg):
        if verbose:
            print(msg)

    stats = {'category': 0, 'post_count': 0, 'cn_name': 0, 'cn_wiki': 0,
             'en_wiki': 0, 'other_names': 0, 'updated_at': 0}

    conn = get_conn(db_path)
    try:
        # === 1. raw/tag.sqlite：补 category/post_count/cn_name ===
        if sqlite_src:
            _v(f"[Merge] 读取 raw sqlite: {sqlite_src}")
            src = sql3.connect(sqlite_src)
            try:
                # 只取本地缺失 category 或 post_count 的标签名 → 减少写入量
                cur = conn.execute("SELECT name FROM tags WHERE category < 0 OR post_count <= 0")
                need = {row[0] for row in cur.fetchall()}
                src_rows = src.execute(
                    "SELECT name, category, cn_name, post_count FROM tags"
                ).fetchall()
                # 批1：补 category/post_count（本地无值才写，update_tag_meta 的 CASE 守卫已保证）
                meta_batch = []
                # 批2：补 cn_name（本地为空才写）
                # 用一条 SQL 拿到「本地 cn_name 为空」的 name 集合
                empty_cn = {r[0] for r in conn.execute(
                    "SELECT name FROM tags WHERE cn_name = '' OR cn_name IS NULL").fetchall()}
                for name, cat, cn, pc in src_rows:
                    if not name:
                        continue
                    n = name.strip().replace(' ', '_').lower()
                    if not n:
                        continue
                    try:
                        cat_i = int(cat) if cat is not None else -1
                    except (ValueError, TypeError):
                        cat_i = -1
                    try:
                        pc_i = int(pc) if pc is not None else 0
                    except (ValueError, TypeError):
                        pc_i = 0
                    # category/post_count：本地缺失才补（update_tag_meta 的 CASE 守卫）
                    if cat_i >= 0 or pc_i > 0:
                        meta_batch.append((n, cat_i, pc_i))
                    # cn_name：本地为空且源非空才补
                    if n in empty_cn and cn and str(cn).strip():
                        conn.execute(
                            "UPDATE tags SET cn_name = ? WHERE name = ? AND (cn_name = '' OR cn_name IS NULL)",
                            (str(cn).strip(), n)
                        )
                        stats['cn_name'] += 1
                # 批量补 category/post_count
                for i in range(0, len(meta_batch), 500):
                    chunk = meta_batch[i:i+500]
                    conn.executemany(
                        "INSERT INTO tags (name, category, post_count) VALUES (?, ?, ?) "
                        "ON CONFLICT(name) DO UPDATE SET "
                        "category = CASE WHEN excluded.category >= 0 THEN excluded.category ELSE tags.category END, "
                        "post_count = CASE WHEN excluded.post_count > 0 THEN excluded.post_count ELSE tags.post_count END",
                        chunk
                    )
                stats['category'] = len(meta_batch)
                conn.commit()
                _v(f"[Merge] raw sqlite：补 category/post_count {len(meta_batch)} 条，cn_name {stats['cn_name']} 条")
            finally:
                src.close()

        # === 2. wiki_pages.parquet：补 en_wiki/other_names/updated_at ===
        if wiki_parquet:
            import pandas as pd
            _v(f"[Merge] 读取 wiki parquet: {wiki_parquet}")
            df = pd.read_parquet(wiki_parquet, columns=['title', 'body', 'other_names', 'updated_at'])
            # 需补全的标签：en_wiki 或 other_names 任一为空（两者独立判断，不互斥）
            need_wiki = {r[0] for r in conn.execute(
                "SELECT name FROM tags WHERE en_wiki = '' OR en_wiki IS NULL "
                "OR other_names = '' OR other_names = '[]' OR other_names IS NULL").fetchall()}
            wiki_batch = []
            for _, row in df.iterrows():
                title = row['title']
                if not isinstance(title, str) or not title.strip():
                    continue
                n = title.strip().replace(' ', '_').lower()
                body = row.get('body', '')
                body = '' if not isinstance(body, str) else body.strip()
                other = _normalize_other_names(row.get('other_names'))
                ua = str(row.get('updated_at', '') or '')
                if n in need_wiki:
                    # 源数据 body 为空也写入（other_names/updated_at 仍可能补全）
                    wiki_batch.append((n, body, other, ua))
            # 批量 upsert：en_wiki/other_names/updated_at（守卫：本地为空或新值更大）
            for i in range(0, len(wiki_batch), 500):
                chunk = wiki_batch[i:i+500]
                conn.executemany(
                    "INSERT INTO tags (name, en_wiki, other_names, updated_at) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(name) DO UPDATE SET "
                    "en_wiki = CASE WHEN tags.en_wiki = '' OR tags.en_wiki IS NULL THEN excluded.en_wiki ELSE tags.en_wiki END, "
                    "other_names = CASE WHEN tags.other_names = '' OR tags.other_names = '[]' OR tags.other_names IS NULL "
                    "THEN excluded.other_names ELSE tags.other_names END, "
                    "updated_at = CASE WHEN excluded.updated_at > tags.updated_at OR tags.updated_at = '' "
                    "THEN excluded.updated_at ELSE tags.updated_at END",
                    chunk
                )
            stats['en_wiki'] = len(wiki_batch)
            conn.commit()
            _v(f"[Merge] wiki parquet：补 en_wiki/other_names {len(wiki_batch)} 条")

        # === 3. tags_enhanced.csv：补 cn_name/cn_wiki（质量最高，最后补兜底）===
        if csv_src:
            import pandas as pd
            _v(f"[Merge] 读取 csv: {csv_src}")
            df = pd.read_csv(csv_src, dtype=str).fillna('')
            # 重新查本地为空的集合（前面的步骤可能已补部分）
            empty_cn = {r[0] for r in conn.execute(
                "SELECT name FROM tags WHERE cn_name = '' OR cn_name IS NULL").fetchall()}
            empty_cnwiki = {r[0] for r in conn.execute(
                "SELECT name FROM tags WHERE cn_wiki = '' OR cn_wiki IS NULL").fetchall()}
            cn_batch = []
            cnwiki_batch = []
            for _, row in df.iterrows():
                name = (row.get('name') or '').strip()
                if not name:
                    continue
                n = name.replace(' ', '_').lower()
                cn = (row.get('cn_name') or '').strip()
                wiki_cn = (row.get('wiki') or '').strip()
                if n in empty_cn and cn:
                    cn_batch.append((cn, n))
                if n in empty_cnwiki and wiki_cn:
                    cnwiki_batch.append((wiki_cn, n))
            for i in range(0, len(cn_batch), 500):
                chunk = cn_batch[i:i+500]
                conn.executemany(
                    "UPDATE tags SET cn_name = ? WHERE name = ? AND (cn_name = '' OR cn_name IS NULL)",
                    chunk
                )
            for i in range(0, len(cnwiki_batch), 500):
                chunk = cnwiki_batch[i:i+500]
                conn.executemany(
                    "UPDATE tags SET cn_wiki = ? WHERE name = ? AND (cn_wiki = '' OR cn_wiki IS NULL)",
                    chunk
                )
            stats['cn_name'] += len(cn_batch)
            stats['cn_wiki'] = len(cnwiki_batch)
            conn.commit()
            _v(f"[Merge] csv：补 cn_name {len(cn_batch)} 条，cn_wiki {len(cnwiki_batch)} 条")

        _v(f"[Merge] 完成：{stats}")
        return stats
    finally:
        conn.close()


def show_stats(db_path):
    """打印数据库统计"""
    conn = get_conn(db_path)
    try:
        total = conn.execute("SELECT COUNT(*) FROM tags").fetchone()[0]
        with_cn = conn.execute("SELECT COUNT(*) FROM tags WHERE cn_name != ''").fetchone()[0]
        with_en_wiki = conn.execute("SELECT COUNT(*) FROM tags WHERE en_wiki != ''").fetchone()[0]
        with_cn_wiki = conn.execute("SELECT COUNT(*) FROM tags WHERE cn_wiki != ''").fetchone()[0]
        print(f"数据库: {db_path}")
        print(f"总标签数:     {total}")
        print(f"含中文翻译:   {with_cn} ({with_cn*100//total if total else 0}%)")
        print(f"含英文 wiki:  {with_en_wiki} ({with_en_wiki*100//total if total else 0}%)")
        print(f"含中文 wiki:  {with_cn_wiki} ({with_cn_wiki*100//total if total else 0}%)")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 增量爬取 Danbooru wiki_pages（参考 fetch_wiki.py 的工业级设计）
# ---------------------------------------------------------------------------

def _fetch_with_retry(session, url, params, danbooru_cfg, cancel_check=None, label='fetch',
                      on_429=None, on_network_error=None):
    """统一 HTTP 请求 + 429 指数退避 + 5xx/网络重试 + 中断检查。

    wiki / tags 增量 / tags 冷启动 三阶段共用，消除重复逻辑与策略不一致。
    返回 dict:
        {data, cancelled, status}
        - data: 解析后的 JSON（list/dict），失败时为 None
        - cancelled: True 表示收到中断请求（调用方应终止循环）
        - status: HTTP 状态码（成功=200；403=致命，调用方据此终止；410=千页上限，调用方做千页突破）

    策略（三阶段统一）：
        - 429：指数退避 60→120→240→300s 封顶，重试（计数局部化，不跨阶段污染）
          连续 429 达 MAX_429 次后放弃，返回 status=429 让调用方决定（避免持续限流时永久卡住）
        - 网络异常：60s 后重试，连续 MAX_NET_ERRORS 次后放弃，返回 status=0（避免断网时永久卡住）
        - 5xx：直接返回让调用方处理（不再内部重试，避免与调用方重试叠加成无限循环）
        - 403：直接返回（调用方视为致命错误，不重试）
        - 410：直接返回（调用方做千页突破，不重试）
        - 中断：循环顶部检查 cancel_check，立即返回 cancelled=True

    on_429(count, backoff) / on_network_error(msg)：可选回调，用于打印日志（避免本函数依赖 print）。
    """
    import time
    consecutive_429 = 0  # 局部计数，消除跨阶段污染
    net_errors = 0
    MAX_429 = 10         # 连续 429 上限：达此值放弃（≈指数退避累计已数十分钟）
    MAX_NET_ERRORS = 5   # 连续网络异常上限：达此值放弃（5×60s=5min 仍不通）
    timeout = danbooru_cfg['timeout']
    while True:
        # 中断优先：请求前检查，避免发出请求后才中断
        if cancel_check and cancel_check():
            return {'data': None, 'cancelled': True, 'status': 0}
        try:
            resp = session.get(url, params=params, timeout=timeout)
        except Exception as e:
            net_errors += 1
            if net_errors >= MAX_NET_ERRORS:
                msg = f'{label} 连续网络异常 {MAX_NET_ERRORS} 次，放弃'
                if on_network_error:
                    on_network_error(msg)
                return {'data': None, 'cancelled': False, 'status': 0}
            msg = f'{label} 网络异常（第 {net_errors}/{MAX_NET_ERRORS} 次）: {e}，60s 后重试'
            if on_network_error:
                on_network_error(msg)
            time.sleep(60)
            continue
        net_errors = 0  # 收到响应，重置网络异常计数
        if resp.status_code == 429:
            consecutive_429 += 1
            if consecutive_429 >= MAX_429:
                if on_429:
                    on_429(consecutive_429, 0)
                return {'data': None, 'cancelled': False, 'status': 429}
            backoff = min(60 * (2 ** (consecutive_429 - 1)), 300)
            if on_429:
                on_429(consecutive_429, backoff)
            time.sleep(backoff)
            continue
        # 成功或非重试状态码：返回让调用方处理（403/410/5xx/200/其他）
        data = None
        if resp.status_code == 200:
            try:
                data = resp.json()
            except Exception:
                data = None
        return {'data': data, 'cancelled': False, 'status': resp.status_code}

def full_scan_wiki(db_path, verbose=True, progress_callback=None, cancel_check=None):
    """全量抓取 Danbooru wiki_pages.json（所有 wiki 页面），补全本地 en_wiki/other_names/updated_at。

    与 update_from_danbooru 的 wiki 阶段区别：
      · update_from_danbooru：增量模式，只抓「本地最新 updated_at 之后」的新 wiki（几页）
      · full_scan_wiki：全量遍历 Danbooru 所有 wiki 页面（约27万条），补齐历史缺口

    抓取策略：默认排序（updated_at）+ page 翻页 + 千页突破。
      · 不能用 search[order]=id：wiki 的 ID 稀疏（总量~27万但ID跨度到29万4），
        按 ID 排序每页跨大区间，2-3 页就抓完全部，无法稳定分页。
      · 默认排序按 updated_at，page 翻页稳定，但 page>1000 返回 410（平台硬限制），
        故每 900 页做「千页突破」：记录当前窗口最小 updated_at，用 search[updated_at]=..<min 收窄，
        page 重置为 1 继续。
      · 空响应容错：连续 EMPTY_RETRIES 次空才认定遍历结束。
      · 跳过 is_deleted=True 的 wiki 页。

    落库：仅写 en_wiki/other_names/updated_at，保留本地 cn_name/cn_wiki 不变。

    progress_callback 同 update_from_danbooru。
    """
    import random
    import time
    from dateutil import parser as date_parser
    from config import get_danbooru_config

    def _emit(event):
        if progress_callback:
            progress_callback(event)

    db_cfg = get_tag_db_config()
    danbooru_cfg = get_danbooru_config()
    if not danbooru_cfg['enabled']:
        msg = 'Danbooru 抓取已禁用（DANBOORU_ENABLED=false）'
        print(f'[BuildTagDB] {msg}')
        _emit({'type': 'error', 'message': msg})
        return

    delay_base = danbooru_cfg.get('delay', 0.15)
    delay_jitter = danbooru_cfg.get('delay_jitter', 0.3)
    page_limit = min(int(danbooru_cfg.get('page_limit', 200)), 200)
    pause_every = max(1, int(danbooru_cfg.get('pause_every_pages', 100)))
    pause_secs = float(danbooru_cfg.get('pause_seconds', 5.0))

    session = _make_session(db_cfg, danbooru_cfg)
    api_url = danbooru_cfg['api_url'].rstrip('/') + '/wiki_pages.json'

    print('[BuildTagDB] 开始 wiki 全量遍历（默认排序 + page + 千页突破）')
    _emit({'type': 'progress', 'page': 1, 'new_count': 0})

    EMPTY_RETRIES = 3
    empty_streak = 0
    current_page = 1
    current_upper_bound = None  # 千页突破：search[updated_at]=..<upper
    wiki_count = 0
    skipped_deleted = 0

    conn = get_conn(db_path)
    cancelled = False
    try:
        while True:
            params = {'limit': page_limit, 'page': current_page}
            if current_upper_bound:
                params['search[updated_at]'] = '..' + current_upper_bound
            # 统一请求层：429 退避 + 网络重试 + 中断检查（与 update_from_danbooru 同机制）
            r = _fetch_with_retry(
                session, api_url, params, danbooru_cfg,
                cancel_check=cancel_check, label='wiki',
                on_429=lambda c, b: print(f'[BuildTagDB] wiki 429（第 {c} 次），退避 {b}s'),
                on_network_error=lambda m: print(f'[BuildTagDB] {m}'),
            )
            if r['cancelled']:
                print(f'[BuildTagDB] 用户中断 wiki 全量遍历（已抓 {wiki_count} 条）')
                _emit({'type': 'cancelled', 'new_count': wiki_count})
                cancelled = True
                break
            if r['status'] == 403:
                msg = '403 错误，凭证可能失效或被限流'
                print(f'[BuildTagDB] {msg}，停止')
                _emit({'type': 'error', 'message': msg})
                break
            if r['status'] == 410:
                # page 超过 1000 上限，触发千页突破
                if current_upper_bound:
                    print(f'[BuildTagDB] wiki 千页上限(410)，重置时间轴到 updated_at<{current_upper_bound}')
                    current_page = 1
                    continue
                else:
                    print('[BuildTagDB] wiki 首页即 410，停止')
                    break
            if r['status'] != 200:
                print(f'[BuildTagDB] wiki HTTP {r["status"]}，60s 后重试')
                time.sleep(60)
                continue

            data = r['data']
            if not data:
                empty_streak += 1
                if empty_streak >= EMPTY_RETRIES:
                    print(f'[BuildTagDB] wiki 连续 {EMPTY_RETRIES} 次空响应，遍历完成')
                    break
                print(f'[BuildTagDB] wiki 空响应（第 {empty_streak}/{EMPTY_RETRIES} 次），5s 后重试')
                time.sleep(5)
                continue
            empty_streak = 0

            page_oldest_ua = None  # 本页最小 updated_at（默认排序下=最后一条）
            for entry in data:
                ua = entry.get('updated_at') or ''
                if ua and (page_oldest_ua is None or ua < page_oldest_ua):
                    page_oldest_ua = ua
                if entry.get('is_deleted'):
                    skipped_deleted += 1
                    continue
                title = (entry.get('title') or '').strip()
                if not title:
                    continue
                body = entry.get('body') or ''
                body = body if isinstance(body, str) else ''
                other_names = _normalize_other_names(entry.get('other_names'))
                upsert_wiki_incremental(conn, name=title, en_wiki=body, other_names=other_names, updated_at=ua)
                wiki_count += 1
            conn.commit()

            _emit({'type': 'progress', 'page': current_page, 'new_count': wiki_count})
            current_page += 1
            time.sleep(delay_base + random.random() * delay_jitter)

            # 千页突破：到 900 页提前收窄时间窗口，page 重置为 1
            if current_page > 900:
                if page_oldest_ua:
                    current_upper_bound = page_oldest_ua
                    print(f'[BuildTagDB] wiki 千页突破，重置时间轴到 updated_at<{current_upper_bound[:19]}，page 重置为 1')
                current_page = 1
            # 每 pause_every 页打印进度
            if current_page > 1 and (current_page - 1) % pause_every == 0:
                print(f'[BuildTagDB] wiki 全量进行中：{wiki_count} 条，当前 page={current_page}')
                time.sleep(pause_secs)

        if cancelled:
            print(f'[BuildTagDB] wiki 全量遍历已中断：抓取 {wiki_count} 条，跳过已删除 {skipped_deleted} 条')
            # cancelled 事件已在循环中断点发送，此处不再重复
        else:
            print(f'[BuildTagDB] wiki 全量遍历完成：抓取 {wiki_count} 条，跳过已删除 {skipped_deleted} 条')
            _emit({'type': 'complete', 'new_count': wiki_count})
    finally:
        conn.close()


def _make_session(db_cfg, danbooru_cfg):
    """构造带认证/代理/UA 的 requests session"""
    import requests
    s = requests.Session()
    s.headers.update({'User-Agent': danbooru_cfg['user_agent'], 'Accept': 'application/json'})
    # 认证参数（认证用户有更高 API 配额）。匿名时两个值都为空
    if db_cfg['username'] and db_cfg['api_key']:
        s.params = {'login': db_cfg['username'], 'api_key': db_cfg['api_key']}
    if danbooru_cfg['proxy']:
        s.proxies = {'http': danbooru_cfg['proxy'], 'https': danbooru_cfg['proxy']}
    return s


def update_from_danbooru(db_path, verbose=True, progress_callback=None, cancel_check=None):
    """增量抓取 Danbooru wiki_pages.json，更新本地 SQLite 的 en_wiki/other_names/updated_at。

    机制（参考 danbooru-tag-pipeline 的 fetch_wiki.py）：
    - 时间锚点增量：取本地 updated_at 最大值，只抓 search[updated_at]=>该时间 的新数据
    - 断点续传：data/.wiki_fetch_progress 记录页码+时间上限，中断后回退 2 页恢复
    - 频率控制：基于 help:api 官方"读请求 10 req/s 全局上限"设计。
        · 页间延迟 delay + 0~delay_jitter 抖动（默认 0.15+0~0.3 ≈ 0.15~0.45s，≈3 req/s）
        · 每 pause_every_pages 页休眠 pause_seconds（默认每 100 页休 5s，长任务保险）
        · 429：指数退避（60s→120s→240s），500：60s 重试
    - 千页突破：Danbooru 单次翻页上限约 1000，到 900 页时重置时间轴到当前最后一项的 updated_at
    - 中文翻译/中文 wiki 不在此处更新（Danbooru 不提供中文），只更新英文侧字段

    progress_callback(event_dict)：可选回调，用于 SSE 流式上报进度。
        {'type':'progress','page':N,'new_count':M} 每页完成
        {'type':'error','message':str} 致命错误（403/禁用）
        {'type':'complete','new_count':N} 全部完成
        {'type':'cancelled','new_count':N} 用户中断（已爬数据已落库）

    cancel_check()：可选回调，返回 True 表示用户请求中断。三个阶段循环顶部各检查一次，
    中断时正常走 finally（已 commit 数据落库），并发 cancelled 事件。断点文件/state 保留，
    下次重跑从断点处续传，不重复爬。
    """
    import random
    import time
    from dateutil import parser as date_parser
    from config import get_danbooru_config

    def _emit(event):
        """同时支持 print（CLI）和 progress_callback（SSE）"""
        if progress_callback:
            progress_callback(event)

    db_cfg = get_tag_db_config()
    danbooru_cfg = get_danbooru_config()
    if not danbooru_cfg['enabled']:
        msg = 'Danbooru 抓取已禁用（DANBOORU_ENABLED=false）'
        print(f'[BuildTagDB] {msg}')
        _emit({'type': 'error', 'message': msg})
        return

    # 页间延迟：delay 基准 + 0~delay_jitter 随机抖动（基于 help:api 读请求 10 req/s 上限）
    delay_base = danbooru_cfg.get('delay', 0.15)
    delay_jitter = danbooru_cfg.get('delay_jitter', 0.3)
    page_limit = min(int(danbooru_cfg.get('page_limit', 200)), 200)  # wiki_pages.json 官方上限 200
    pause_every = max(1, int(danbooru_cfg.get('pause_every_pages', 100)))
    pause_secs = float(danbooru_cfg.get('pause_seconds', 5.0))

    session = _make_session(db_cfg, danbooru_cfg)
    api_url = danbooru_cfg['api_url'].rstrip('/') + '/wiki_pages.json'
    base_dir = Path(db_path).parent
    progress_file = base_dir / '.wiki_fetch_progress'

    # 1. 时间锚点：本地 updated_at 最大值
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT MAX(updated_at) FROM tags WHERE updated_at != ''").fetchone()
        last_update_time = date_parser.parse(row[0]) if row and row[0] else date_parser.parse('2000-01-01T00:00:00Z')
    finally:
        conn.close()
    if verbose:
        print(f'[BuildTagDB] 本地最新 updated_at: {last_update_time}')

    # 2. 断点续传
    current_page = 1
    current_upper_bound = None
    if progress_file.exists():
        try:
            lines = progress_file.read_text().splitlines()
            if lines:
                current_page = max(1, int(lines[0].strip()) - 2)  # 回退 2 页保险
                if len(lines) > 1 and lines[1].strip():
                    current_upper_bound = lines[1].strip()
                print(f'[BuildTagDB] 检测到中断记录，从第 {current_page} 页恢复')
        except ValueError:
            pass

    new_count = 0
    reached_end = False
    WIKI_EMPTY_RETRIES = 3   # wiki 增量空响应重试次数（与 tags 阶段同机制）
    wiki_empty_streak = 0    # wiki 增量连续空响应计数
    page_latest_seen = None  # 全局最新 updated_at（跨页累积），用于意外 410 时重置时间轴

    # 3. 连接数据库。本地仅包含通过 sync_tags_db 同步的热门标签（post_count≥100, category∈{0,3,4}），
    # 不需要额外过滤——所有标签都有完整 category/post_count。
    conn = get_conn(db_path)
    cancelled = False
    try:
        tag_count = conn.execute("SELECT count(*) FROM tags").fetchone()[0]
        print(f'[BuildTagDB] 本地标签库 {tag_count} 个标签，开始增量抓取 wiki...')

        # 4. 主循环
        while not reached_end:
            print(f'[BuildTagDB] 抓取第 {current_page} 页...')
            params = {'limit': page_limit, 'page': current_page}
            if current_upper_bound:
                params['search[updated_at]'] = '..' + current_upper_bound

            # 统一请求层：429 退避 + 网络重试 + 中断检查（详见 _fetch_with_retry）
            r = _fetch_with_retry(
                session, api_url, params, danbooru_cfg,
                cancel_check=cancel_check, label='wiki',
                on_429=lambda c, b: print(f'[BuildTagDB] wiki 触发频率限制（第 {c} 次），指数退避 {b}s'),
                on_network_error=lambda m: print(f'[BuildTagDB] {m}'),
            )
            if r['cancelled']:
                print(f'[BuildTagDB] 用户中断 wiki 抓取（已抓 {new_count} 条，断点已保存）')
                _emit({'type': 'cancelled', 'new_count': new_count})
                cancelled = True
                break
            if r['status'] == 403:
                msg = '403 错误，凭证可能失效或被限流'
                print(f'[BuildTagDB] {msg}，停止')
                _emit({'type': 'error', 'message': msg})
                break
            if r['status'] == 410:
                # page 超过 Danbooru 1000 页上限。正常不应到这里（900 页提前千页突破），
                # 但若服务端阈值变化提前 410，用已记录的 page_latest 重置时间轴继续。
                if page_latest_seen:
                    current_upper_bound = page_latest_seen
                    print(f'[BuildTagDB] wiki 意外 410，重置时间轴至 {current_upper_bound}，page 重置为 1')
                    current_page = 1
                    continue
                else:
                    print('[BuildTagDB] wiki 410 且无 page_latest 可重置，停止')
                    break
            if r['status'] != 200:
                print(f'[BuildTagDB] wiki HTTP {r["status"]}，60s 后重试')
                time.sleep(60)
                continue
            data = r['data']
            if not data:
                # 空响应容错：可能是网络抖动，连续 WIKI_EMPTY_RETRIES 次空才认定到最后一页。
                # （与 tags 阶段同机制，避免单次空响应误判导致漏抓）
                wiki_empty_streak += 1
                if wiki_empty_streak >= WIKI_EMPTY_RETRIES:
                    print('[BuildTagDB] wiki 连续空响应，已到服务器最后一页')
                    break
                print(f'[BuildTagDB] wiki 空响应（第 {wiki_empty_streak}/{WIKI_EMPTY_RETRIES} 次），5s 后重试')
                time.sleep(5)
                continue
            wiki_empty_streak = 0  # 收到数据，重置空响应计数

            # 落库 + 检测与本地时间线衔接
            page_latest = None
            for entry in data:
                entry_time = date_parser.parse(entry['updated_at'])
                if entry_time <= last_update_time:
                    # 注意：wiki 默认排序按 updated_at，但同一页内不一定严格单调。
                    # 命中衔接点后用 continue 跳过本条（旧于锚点），继续处理同页后续可能更新的条目，
                    # 而非 break（break 会漏掉同页后续比锚点新的条目）。
                    # 标记本页已触及衔接点，页结束后停止翻页。
                    reached_end = True
                    continue
                title = (entry.get('title') or '').strip()
                if not title:
                    continue
                body = entry.get('body') or ''
                other_names = _normalize_other_names(entry.get('other_names'))
                # 仅更新 en_wiki/other_names/updated_at，保留本地翻译与中文 wiki 不被覆盖
                upsert_wiki_incremental(
                    conn,
                    name=title,
                    en_wiki=body if isinstance(body, str) else '',
                    other_names=other_names,
                    updated_at=entry['updated_at'],
                )
                new_count += 1
                # page_latest 取本页最新 updated_at（用于千页突破重置时间轴）
                if page_latest is None or entry['updated_at'] > page_latest:
                    page_latest = entry['updated_at']
            conn.commit()
            # 累积全局最新 updated_at，用于意外 410 时重置时间轴（page_latest 仅本页）
            if page_latest and (page_latest_seen is None or page_latest > page_latest_seen):
                page_latest_seen = page_latest

            # 每页完成上报进度
            _emit({'type': 'progress', 'page': current_page, 'new_count': new_count})

            if reached_end:
                print(f'[BuildTagDB] 与本地时间线衔接，增量完成（本页新增 {new_count} 条）')
                break

            current_page += 1
            time.sleep(delay_base + random.random() * delay_jitter)

            # 千页突破：到 900 页重置时间轴
            if current_page > 900:
                if page_latest:
                    current_upper_bound = page_latest
                    print(f'[BuildTagDB] 时间轴重置至 {current_upper_bound}')
                    current_page = 1
                else:
                    # page_latest 为空（异常：900 页都没拿到新数据），停止避免死循环。
                    # 正常增量远不会到 900 页（几页就衔接锚点），到这里说明数据异常。
                    print('[BuildTagDB] 千页突破但无 page_latest（900 页无新数据），停止避免死循环')
                    break

            # 每页都存检查点（轻量：2 行小文件），保证中断后可从当前页-2 恢复，不丢数据。
            # 原设计每 pause_every 页才存，但 wiki 增量通常只跑几页，中断点可能落在检查点之外。
            pages_done = current_page - 1
            if pages_done > 0:
                with open(progress_file, 'w') as f:
                    f.write(f'{pages_done}\n')
                    if current_upper_bound:
                        f.write(f'{current_upper_bound}\n')
                # 每 pause_every 页额外休息 pause_secs（长任务保险）
                if pages_done % pause_every == 0:
                    print(f'[BuildTagDB] 已抓 {pages_done} 页，检查点已保存，休息 {pause_secs}s')
                    time.sleep(pause_secs)

        # --- 不再需要 tags.json 补查阶段 ---
        # 本地仅包含热标签，所有写入的 wiki 数据都已有完整 category/post_count。

        # 清理断点文件：仅在整个任务正常完成时删除（中断时保留供下次续传）。
        if not cancelled and progress_file.exists():
            progress_file.unlink()
        if cancelled:
            print(f'[BuildTagDB] 更新已中断，本次新增/更新 {new_count} 条（断点已保存，下次续传）')
            # cancelled 事件已在各阶段中断点发送，此处不再重复
        else:
            print(f'[BuildTagDB] 增量更新完成，本次新增/更新 {new_count} 条')
            _emit({'type': 'complete', 'new_count': new_count})
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description='Danbooru 标签数据库工具')
    sub = parser.add_subparsers(dest='cmd', required=True)

    p_init = sub.add_parser('init', help='从 CSV + Parquet 全量构建数据库')
    p_init.add_argument('--csv', required=True, help='tags_enhanced.csv 路径')
    p_init.add_argument('--parquet', required=True, help='wiki_pages.parquet 路径')
    p_init.add_argument('--db', default=None, help='输出 SQLite 路径（默认用 .env 的 TAG_DB_PATH）')

    p_update = sub.add_parser('update', help='增量抓取 Danbooru wiki 更新本地数据库')
    p_update.add_argument('--db', default=None, help='SQLite 路径（默认用 .env 的 TAG_DB_PATH）')

    p_merge = sub.add_parser('merge', help='从本地数据源增量补全数据库（保留现有数据）')
    p_merge.add_argument('--sqlite', default=None, help='raw/tag.sqlite 路径（补 category/post_count/cn_name）')
    p_merge.add_argument('--wiki', default=None, help='wiki_pages.parquet 路径（补 en_wiki/other_names）')
    p_merge.add_argument('--csv', default=None, help='tags_enhanced.csv 路径（补 cn_name/cn_wiki）')
    p_merge.add_argument('--db', default=None, help='目标 SQLite 路径（默认用 .env 的 TAG_DB_PATH）')

    p_wikifull = sub.add_parser('wiki_full', help='全量抓取 Danbooru 所有 wiki 页面，补全 en_wiki/other_names')
    p_wikifull.add_argument('--db', default=None, help='SQLite 路径（默认用 .env 的 TAG_DB_PATH）')

    # ── 新命令：标签工程管线 ──
    p_sync = sub.add_parser('sync-tags', help='从上游 GitHub SQLite 同步新标签')
    p_sync.add_argument('--db', default=None, help='SQLite 路径')
    p_sync.add_argument('--download', action='store_true', default=True, help='是否下载最新 tag.sqlite（默认下载）')

    p_tg = sub.add_parser('tag-groups', help='抓取 Danbooru 标签组（tag_group）体系')
    p_tg.add_argument('--db', default=None, help='SQLite 路径')

    p_llm = sub.add_parser('llm-process', help='LLM 三层翻译增强（general/fallback/entity）+ Bangumi 查证')
    p_llm.add_argument('--db', default=None, help='SQLite 路径')
    p_llm.add_argument('--preview', action='store_true', help='预览模式，仅统计不消耗 API')
    p_llm.add_argument('--debug', action='store_true', help='调试模式')
    p_llm.add_argument('--reprocess-wiki-updates', action='store_true', help='重新处理 Wiki 有更新的标签')

    p_cooc = sub.add_parser('fetch-cooc', help='抓取标签共现矩阵（related_tag API）')
    p_cooc.add_argument('--db', default=None, help='SQLite 路径')
    p_cooc.add_argument('--full', action='store_true', help='全量更新模式')

    p_trim = sub.add_parser('trim-cooc', help='共现矩阵 PMI 降维截断')
    p_trim.add_argument('--db', default=None, help='SQLite 路径')
    p_trim.add_argument('--top-k', type=int, default=50, help='每个标签保留的关联上限')
    p_trim.add_argument('--min-pmi', type=float, default=1.0, help='PMI 最低阈值')
    p_trim.add_argument('--dry-run', action='store_true', help='测试模式，不写盘')

    p_pipe = sub.add_parser('pipeline', help='一键全流程：sync-tags → tag-groups → llm-process → fetch-cooc → trim-cooc')
    p_pipe.add_argument('--db', default=None, help='SQLite 路径')

    sub.add_parser('stats', help='显示数据库统计')

    args = parser.parse_args()
    db_path = args.db if getattr(args, 'db', None) else get_tag_db_config()['db_path']

    if args.cmd == 'init':
        init_from_files(db_path, args.csv, args.parquet)
        show_stats(db_path)
    elif args.cmd == 'update':
        update_from_danbooru(db_path)
        show_stats(db_path)
    elif args.cmd == 'merge':
        merge_local_sources(db_path, sqlite_src=args.sqlite, wiki_parquet=args.wiki, csv_src=args.csv)
        show_stats(db_path)
    elif args.cmd == 'wiki_full':
        full_scan_wiki(db_path)
        show_stats(db_path)
    elif args.cmd == 'stats':
        show_stats(db_path)
    elif args.cmd == 'sync-tags':
        from sync_tags import run as run_sync_tags
        run_sync_tags(db_path, download=args.download)
        show_stats(db_path)
    elif args.cmd == 'tag-groups':
        from tag_groups import run as run_tag_groups
        run_tag_groups(db_path)
    elif args.cmd == 'llm-process':
        from llm_pipeline import run_llm_process
        run_llm_process(db_path, preview=args.preview, debug=args.debug,
                        reprocess_wiki_updates=args.reprocess_wiki_updates)
    elif args.cmd == 'fetch-cooc':
        from cooc_pipeline import run_fetch_cooc
        run_fetch_cooc(db_path, full_update=args.full)
    elif args.cmd == 'trim-cooc':
        from cooc_pipeline import run_trim_cooc
        run_trim_cooc(db_path, top_k=args.top_k, min_pmi=args.min_pmi, dry_run=args.dry_run)
    elif args.cmd == 'pipeline':
        from sync_tags import run as run_sync_tags
        from tag_groups import run as run_tag_groups
        from llm_pipeline import run_llm_process
        from cooc_pipeline import run_fetch_cooc, run_trim_cooc
        print('=' * 60)
        print('[Pipeline] 启动一键全流程')
        print('=' * 60)
        run_sync_tags(db_path)
        run_tag_groups(db_path)
        run_llm_process(db_path)
        run_fetch_cooc(db_path)
        run_trim_cooc(db_path)
        show_stats(db_path)


if __name__ == '__main__':
    main()
