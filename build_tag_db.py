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
    updated_at  TEXT NOT NULL DEFAULT ''
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

# 已废弃的旧列名（用于迁移检测）。迁移时若 tags 表含任一此列，则重建为精简结构。
_LEGACY_COLUMNS = ('category', 'post_count', 'nsfw')


def _has_legacy_columns(conn):
    """检测 tags 表是否含已废弃列（旧 schema）。返回 bool。"""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(tags)").fetchall()}
    return any(c in cols for c in _LEGACY_COLUMNS)


def _migrate_to_target_schema(conn):
    """把任意旧 schema 迁移为当前目标结构（name/cn_name/en_wiki/cn_wiki/other_names/updated_at）。
    保留这些列里已有的数据，丢弃其他列；缺失的列补默认值。无事务包裹：调用方负责 commit。
    若已是目标 schema 则无操作。"""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(tags)").fetchall()}
    target = {'name', 'cn_name', 'en_wiki', 'cn_wiki', 'other_names', 'updated_at'}
    if cols == target:
        return False
    # 重建：仅 SELECT 目标列（缺失列用 NULL→COALESCE 补默认）。旧索引随旧表 DROP。
    select_cols = []
    for c in ['name', 'cn_name', 'en_wiki', 'cn_wiki', 'updated_at']:
        select_cols.append(c if c in cols else "'' AS " + c)
    # other_names 缺失时补 '[]'
    on_expr = 'other_names' if 'other_names' in cols else "'[]' AS other_names"
    select_cols.insert(4, on_expr)
    conn.executescript(f"""
        CREATE TABLE tags_new (
            name        TEXT PRIMARY KEY,
            cn_name     TEXT NOT NULL DEFAULT '',
            en_wiki     TEXT NOT NULL DEFAULT '',
            cn_wiki     TEXT NOT NULL DEFAULT '',
            other_names TEXT NOT NULL DEFAULT '[]',
            updated_at  TEXT NOT NULL DEFAULT ''
        );
        INSERT INTO tags_new (name, cn_name, en_wiki, cn_wiki, other_names, updated_at)
        SELECT {', '.join(select_cols)} FROM tags;
        DROP TABLE tags;
        ALTER TABLE tags_new RENAME TO tags;
    """)
    # 迁移后 tags 的 rowid 重排，FTS 索引（若存在）的 rowid 已失效，清空待 get_conn 重建。
    # FTS 表可能尚未创建（首次升级的旧库），用 IF EXISTS 防御。
    if _table_exists(conn, 'tags_fts'):
        conn.execute("DELETE FROM tags_fts")
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
            f"SELECT name, cn_name, en_wiki, cn_wiki, other_names "
            f"FROM tags WHERE name IN ({placeholders})",
            chunk
        ).fetchall()
        for r in rows:
            result[r[0]] = {
                'cn_name': r[1], 'en_wiki': r[2], 'cn_wiki': r[3], 'other_names': r[4],
            }
    return result


def lookup_tag_en_wiki(conn, tag):
    """单标签查英文 wiki（翻译时取 body 注入 LLM 用）"""
    row = conn.execute("SELECT en_wiki FROM tags WHERE name = ?", (tag.strip().replace(' ', '_').lower(),)).fetchone()
    return row[0] if row else ''


def update_translation(conn, name, cn_name, commit=True):
    """更新标签的中文翻译（cn_name 列）。标签不存在时自动插入空记录。
    用于：LLM 翻译回写、用户手动编辑翻译回写。不受 updated_at 时间戳守卫限制。
    commit=False 时延迟提交（批量场景由调用方统一 commit，避免逐条磁盘同步拖慢）。"""
    name = name.strip().replace(' ', '_').lower()
    if not name:
        return
    conn.execute("""
        INSERT INTO tags (name, cn_name) VALUES (?, ?)
        ON CONFLICT(name) DO UPDATE SET cn_name = excluded.cn_name
    """, (name, cn_name.strip()))
    if commit:
        conn.commit()


def update_cn_wiki(conn, name, cn_wiki, commit=True):
    """更新标签的中文 wiki（cn_wiki 列）。标签不存在时自动插入空记录。
    用于：详情弹窗翻译英文 wiki 后回写、用户手动编辑中文 wiki 后回写。
    不受 updated_at 时间戳守卫限制。"""
    name = name.strip().replace(' ', '_').lower()
    if not name:
        return
    conn.execute("""
        INSERT INTO tags (name, cn_wiki) VALUES (?, ?)
        ON CONFLICT(name) DO UPDATE SET cn_wiki = excluded.cn_wiki
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
    用于 update_from_danbooru。WHERE 守卫保证只接受比本地更新的 updated_at，
    避免把已存在的翻译/wiki 覆盖。other_names 来自 wiki_pages.json 的同一次抓取，
    不增加额外请求。"""
    conn.execute("""
        INSERT INTO tags (name, en_wiki, other_names, updated_at) VALUES (?, ?, ?, ?)
        ON CONFLICT(name) DO UPDATE SET
            en_wiki=excluded.en_wiki,
            other_names=excluded.other_names,
            updated_at=excluded.updated_at
        WHERE excluded.updated_at > tags.updated_at OR tags.updated_at = ''
    """, (name.strip().replace(' ', '_').lower(), en_wiki, other_names, updated_at))


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
            SELECT name, cn_name, en_wiki, cn_wiki, other_names FROM tags WHERE rowid IN (
                SELECT rowid FROM tags_fts WHERE tags_fts MATCH ?
            )
            ORDER BY length(name), name
            LIMIT ?
            """,
            (match_expr, limit)
        ).fetchall()
    else:
        # 短 keyword（<3 字符，trigram 无效）或 FTS 未建：回退全表 LIKE
        rows = conn.execute(
            """
            SELECT name, cn_name, en_wiki, cn_wiki, other_names
            FROM tags
            WHERE REPLACE(name, '-', '_') LIKE ? ESCAPE '\\'
               OR cn_name LIKE ? ESCAPE '\\'
               OR REPLACE(other_names, '-', '_') LIKE ? ESCAPE '\\'
            ORDER BY length(name), name
            LIMIT ?
            """,
            (name_pat, cn_pat, name_pat, limit)
        ).fetchall()
    return [{
        'name': r[0], 'cn_name': r[1], 'en_wiki': r[2], 'cn_wiki': r[3], 'other_names': r[4],
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


def update_from_danbooru(db_path, verbose=True, progress_callback=None):
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
    consecutive_429 = 0  # 连续 429 次数，用于指数退避（60→120→240s）
    reached_end = False

    # 3. 主循环
    conn = get_conn(db_path)
    try:
        while not reached_end:
            print(f'[BuildTagDB] 抓取第 {current_page} 页...')
            params = {'limit': page_limit, 'page': current_page}
            if current_upper_bound:
                params['search[updated_at]'] = '..' + current_upper_bound

            try:
                resp = session.get(api_url, params=params, timeout=danbooru_cfg['timeout'])
            except Exception as e:
                print(f'[BuildTagDB] 网络异常: {e}，60s 后重试')
                time.sleep(60)
                continue

            if resp.status_code == 429:
                consecutive_429 += 1
                backoff = min(60 * (2 ** (consecutive_429 - 1)), 300)  # 60→120→240→封顶 300s
                print(f'[BuildTagDB] 触发频率限制（第 {consecutive_429} 次），指数退避 {backoff}s')
                time.sleep(backoff)
                continue
            # 成功响应重置 429 计数
            consecutive_429 = 0
            if resp.status_code == 403:
                msg = '403 错误，凭证可能失效或被限流'
                print(f'[BuildTagDB] {msg}，停止')
                _emit({'type': 'error', 'message': msg})
                break
            if resp.status_code == 500:
                time.sleep(60); continue
            if resp.status_code != 200:
                print(f'[BuildTagDB] HTTP {resp.status_code}，60s 后重试')
                time.sleep(60); continue

            data = resp.json()
            if not data:
                print('[BuildTagDB] 已到服务器最后一页')
                break

            # 落库 + 检测与本地时间线衔接
            page_latest = None
            for entry in data:
                entry_time = date_parser.parse(entry['updated_at'])
                if entry_time <= last_update_time:
                    print(f'[BuildTagDB] 与本地时间线衔接（{entry_time}），增量完成')
                    reached_end = True
                    break
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
                if page_latest is None:
                    page_latest = entry['updated_at']
            conn.commit()

            # 每页完成上报进度
            _emit({'type': 'progress', 'page': current_page, 'new_count': new_count})

            if reached_end:
                break

            current_page += 1
            time.sleep(delay_base + random.random() * delay_jitter)

            # 千页突破：到 900 页重置时间轴
            if current_page > 900:
                if page_latest:
                    current_upper_bound = page_latest
                    print(f'[BuildTagDB] 时间轴重置至 {current_upper_bound}')
                current_page = 1

            # 每页计数用本页抓取数；每 pause_every 页存检查点并休息 pause_secs
            pages_done = current_page - 1
            if pages_done > 0 and pages_done % pause_every == 0:
                with open(progress_file, 'w') as f:
                    f.write(f'{pages_done}\n')
                    if current_upper_bound:
                        f.write(f'{current_upper_bound}\n')
                print(f'[BuildTagDB] 已抓 {pages_done} 页，检查点已保存，休息 {pause_secs}s')
                time.sleep(pause_secs)

        # 清理断点文件
        if progress_file.exists():
            progress_file.unlink()
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

    sub.add_parser('stats', help='显示数据库统计')

    args = parser.parse_args()
    db_path = args.db if getattr(args, 'db', None) else get_tag_db_config()['db_path']

    if args.cmd == 'init':
        init_from_files(db_path, args.csv, args.parquet)
        show_stats(db_path)
    elif args.cmd == 'update':
        update_from_danbooru(db_path)
        show_stats(db_path)
    elif args.cmd == 'stats':
        show_stats(db_path)


if __name__ == '__main__':
    main()
