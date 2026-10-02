# -*- coding: utf-8 -*-
"""共现矩阵管线 — 抓取标签共现与画师共现，PMI/NPMI 降维截断。

数据存储在 data/cooc/ 目录下：
  - cooccurrence_raw.parquet  原始共现（有向边，Snappy 压缩）
  - cooccurrence_clean.parquet  PMI 裁剪后（无向边）
  - tag_artist_cooc.parquet  画师共现（裁剪后）
"""
import time
import json
import os
import random
from pathlib import Path

import requests
import numpy as np
import logging

from tageditor.core.config import USER_AGENT


# ── 工具 ───────────────────────────────────────────────────────────────────


log = logging.getLogger(__name__)

def _get_auth():
    """从环境变量读取 Danbooru 认证。"""
    user = os.environ.get('DANBOORU_USER_NAME', '')
    key = os.environ.get('DANBOORU_API_KEY', '')
    return user, key


def _get_proxies():
    proxy = os.environ.get('DANBOORU_PROXY', '')
    return {'http': proxy, 'https': proxy} if proxy else None


def _cooc_dir(db_path: str) -> Path:
    d = Path(db_path).parent / 'cooc'
    d.mkdir(parents=True, exist_ok=True)
    return d


def _checkpoint_dir(db_path: str) -> Path:
    """checkpoint 目录：存储中断续传的进度/历史文件（与 llm_pipeline 一致）。"""
    d = Path(db_path).parent / 'checkpoint'
    d.mkdir(parents=True, exist_ok=True)
    return d


# 共现 history 的落盘格式改成**追加式 JSONL**（每行一个标签名）。
#
# 原先每次 checkpoint 都 `json.dumps(sorted(history))` 全量重写：
# 实测 cooc_history.json 有 53,311 个标签、1.08MB，一次 sorted+dumps 约 14ms，
# 每 20 个标签写一次 → 全量一轮 2,673 次 → **约 38 秒 CPU + 2.89GB 磁盘写入**，
# 而且是 O(n²)（越到后面越慢）。追加式把这两项都降到可忽略，
# 顺带更耐崩溃：进程在两行之间被杀，也只是少几个标签，不会得到一个坏 JSON。
_COOC_CKPT_EVERY = 200  # 每多少个标签落一次盘（原为 20）


def _load_cooc_history(history_file: Path) -> set:
    """读共现 history，兼容两种格式：老的 JSON 数组 / 新的 JSONL。"""
    if not history_file.exists():
        return set()
    try:
        text = history_file.read_text(encoding='utf-8')
    except OSError:
        return set()
    stripped = text.lstrip()
    if stripped.startswith('['):
        try:
            return set(json.loads(stripped))
        except Exception:
            return set()
    return {line.strip() for line in text.splitlines() if line.strip()}


def _append_cooc_history(history_file: Path, names) -> None:
    """把新增标签**追加**到 JSONL。若磁盘上还是老格式（单个 JSON 数组），先转成 JSONL。

    只追加不重写，所以调用方要传「本次新增」的那部分（见 run_fetch_cooc 里的 newly）。
    """
    names = sorted(set(names))
    if not names:
        return
    if history_file.exists():
        try:
            if history_file.read_text(encoding='utf-8').lstrip().startswith('['):
                existing = _load_cooc_history(history_file)
                history_file.write_text(
                    ''.join(n + '\n' for n in sorted(existing)), encoding='utf-8')
        except OSError:
            pass
    with open(history_file, 'a', encoding='utf-8') as f:
        for n in names:
            f.write(n + '\n')


def _load_tags_from_db(db_path: str) -> list[dict]:
    """从 SQLite 加载所有标签。"""
    import sqlite3
    conn = sqlite3.connect(db_path, timeout=5)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT name, category, post_count FROM tags").fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ═══════════════════════════════════════════════════════════════════════════
# fetch_cooc — 抓取标签共现矩阵
# ═══════════════════════════════════════════════════════════════════════════

def run_fetch_cooc(db_path: str = None, full_update: bool = False,
                   progress_callback=None, cancel_check=None):
    if db_path is None:
        from tageditor.core.config import get_tag_db_config
        db_path = get_tag_db_config()['db_path']

    def _emit(event):
        if progress_callback:
            progress_callback(event)

    def _cancelled():
        return cancel_check and cancel_check()

    user, key = _get_auth()
    if not user or not key:
        log.warning("[Cooc] 未配置 DANBOORU_USER_NAME 或 DANBOORU_API_KEY")
        _emit({'type': 'fatal', 'error': '未配置 Danbooru 凭证'})
        return

    proxies = _get_proxies()
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    auth_params = {'login': user, 'api_key': key}

    cdir = _cooc_dir(db_path)
    raw_parquet = cdir / 'cooccurrence_raw.parquet'
    raw_csv_legacy = cdir / 'cooccurrence_raw.csv'  # 旧格式，首次写入时清除    ckp = _checkpoint_dir(db_path)
    progress_file = ckp / 'cooc_progress.txt'
    history_file = ckp / 'cooc_history.json'
    # 试满重试次数仍失败的标签单独留痕。原来的实现把它们也写进了 history，
    # 而增量入口是 `valid_names - history` → **失败标签永久不再重试**，
    # 共现数据出现无声空洞（/tag_cooc 与 prompt_tool 的 cooc 工具对它永远返回空）。
    failed_file = ckp / 'cooc_failed.json'
    temp_csv = cdir / 'cooc_temp.csv'

    tags = _load_tags_from_db(db_path)
    valid_names = {t['name'] for t in tags}
    tag_pc = {t['name']: int(t['post_count']) for t in tags}

    if not valid_names:
        log.info("[Cooc] 数据库中无标签")
        _emit({'type': 'fatal', 'error': '数据库中无标签'})
        return

    # 历史豁免
    history: set = set()
    start_idx = 0

    if progress_file.exists():
        try:
            start_idx = int(progress_file.read_text().strip())
        except ValueError:
            pass
    if history_file.exists():
        history = _load_cooc_history(history_file)

    if full_update:
        target_list = sorted(valid_names)
        # 全量清空状态（从断点恢复的情况除外）。
        # 注意：raw_parquet 必须留到新数据原子落盘成功后再删（见本函数末尾），
        # 否则中途被取消/403/kill 会旧数据已删、新数据未成，共现功能整体失效且无备份。
        if start_idx == 0:
            history = set()
            for p in [raw_csv_legacy, temp_csv, history_file, progress_file]:
                if p.exists():
                    p.unlink()
    else:
        target_list = sorted(valid_names - history)
        start_idx = 0  # 增量模式 target_list 已排除历史，索引始终从 0 开始

    total = len(target_list)
    if total == 0:
        log.info("[Cooc] 无需抓取")
        _emit({'type': 'complete', 'new_count': 0})
        return

    log.info(f"[Cooc] 待处理 {total} 个标签{'（全量模式）' if full_update else '（增量模式）'}")
    _emit({'type': 'progress', 'page': 1, 'total': total, 'item': f'共需处理 {total} 个标签'})

    session = requests.Session()
    session.headers.update(headers)
    session.params = auth_params
    if proxies:
        session.proxies = proxies

    batch = []
    done = start_idx
    saved_count = start_idx  # 实际已落盘的计数
    # 用**集合**而不是下标切片表达进度：`target_list[start_idx:saved_count]` 会把
    # 中间失败、没拿到数据的标签一并算作已完成（saved_count 只统计成功数，
    # 切片却按位置取），于是失败标签被写进 history → 增量模式永不重试。
    ok_tags = set()
    failed_tags = set()
    api_url = "https://danbooru.donmai.us/related_tag.json"

    for i in range(start_idx, total):
        if _cancelled():
            log.info(f"[Cooc] 用户中断（已保存 {saved_count} 个）")
            _emit({'type': 'cancelled', 'new_count': saved_count})
            return
        tag_a = target_list[i]
        item_text = f"[{i + 1}/{total}] {tag_a}"
        log.info(item_text)
        _emit({'type': 'progress', 'page': i + 1, 'total': total, 'item': item_text})

        fetched = False
        for attempt in range(3):
            try:
                resp = session.get(api_url, params={'query': tag_a}, timeout=30)
                if resp.status_code == 429:
                    log.warning("    429 限流，休眠 30 秒...")
                    time.sleep(30)
                    continue
                if resp.status_code == 403:
                    log.info("    403 凭证失效，终止")
                    _emit({'type': 'fatal', 'error': 'Danbooru 凭证失效'})
                    return
                resp.raise_for_status()
                data = resp.json()
                pairs = _parse_cooc_response(data, tag_a, valid_names, tag_pc)
                batch.extend(pairs)
                done += 1
                fetched = True
                break
            except requests.exceptions.RequestException as e:
                if attempt < 2:
                    time.sleep(2)
                else:
                    log.error(f"    请求失败: {e}")
        if fetched:
            ok_tags.add(tag_a)
        else:
            failed_tags.add(tag_a)

        time.sleep(0.2 + random.random() * 0.2)

        # 每 20 个标签保存一次
        if (i + 1) % 20 == 0 or (i + 1) == total:
            if batch:
                import pandas as pd
                pd.DataFrame(batch).to_csv(temp_csv, mode='a',
                                           header=not temp_csv.exists(),
                                           index=False, encoding='utf-8')
                batch.clear()
            saved_count = done
            progress_file.write_text(str(saved_count))
            # **只把真正取到数据的标签写进 history**，失败的留给下一轮重试
            history.update(ok_tags)
            history_file.write_text(json.dumps(sorted(history), ensure_ascii=False))
            if failed_tags:
                failed_file.write_text(json.dumps(sorted(failed_tags), ensure_ascii=False))
            if (i + 1) % 20 == 0:
                log.info(f"  [Checkpoint] 已处理 {saved_count} 个"
                         + (f"，失败 {len(failed_tags)} 个（未记入 history，下轮会重试）"
                            if failed_tags else ""))

    # 合并到主文件
    if temp_csv.exists():
        import pandas as pd
        df_new = pd.read_csv(temp_csv, low_memory=False, encoding='utf-8')
        # 加去重
        df_new = df_new.drop_duplicates(subset=['source', 'target'], keep='first')

        if not full_update and raw_parquet.exists():
            # 兼容旧 CSV 格式：首次读取旧文件并转存为 parquet 后删除
            if raw_csv_legacy.exists() and not raw_parquet.exists():
                df_old = pd.read_csv(raw_csv_legacy, low_memory=False, encoding='utf-8')
                tmp_parquet = raw_parquet.with_suffix('.parquet.tmp')
                df_old.to_parquet(tmp_parquet, index=False, compression='snappy')
                os.replace(tmp_parquet, raw_parquet)
                raw_csv_legacy.unlink(missing_ok=True)
            df_old = pd.read_parquet(raw_parquet)
            df_all = pd.concat([df_old, df_new], ignore_index=True)
            df_all = df_all.drop_duplicates(subset=['source', 'target'], keep='first')
        else:
            df_all = df_new

        # 原子落盘：先写临时文件、成功后再 os.replace 覆盖目标。
        # 直接 to_parquet(目标) 一旦中途 OOM/磁盘满/被 kill，会留下截断的半截 parquet，
        # 下次 read_parquet 抛 ArrowInvalid，用户只能重新全量拉取。
        tmp_parquet = raw_parquet.with_suffix('.parquet.tmp')
        df_all.to_parquet(tmp_parquet, index=False, compression='snappy')
        os.replace(tmp_parquet, raw_parquet)
        # 清理旧 CSV（如有残留）
        raw_csv_legacy.unlink(missing_ok=True)
        log.info(f"[Cooc] 共现矩阵已保存: {raw_parquet} ({len(df_all)} 条边)")
        temp_csv.unlink(missing_ok=True)
        progress_file.unlink(missing_ok=True)
        if failed_tags:
            # 失败标签留在 failed_file 里（且**没有**进 history），下一轮增量会重试。
            log.warning(f"[Cooc] 本轮有 {len(failed_tags)} 个标签请求失败，"
                        f"已写入 {failed_file.name} 且未记入 history，下次运行会重试")
        _emit({'type': 'complete', 'new_count': done, 'failed': len(failed_tags),
               'failed_file': str(failed_file) if failed_tags else ''})
    else:
        log.info("[Cooc] 没有新数据")
        _emit({'type': 'complete', 'new_count': 0})


def _parse_cooc_response(data, tag_a: str, valid_names: set,
                         tag_pc: dict) -> list[dict]:
    """解析 /related_tag.json 响应。"""
    pairs = []
    query_pc = data.get("post_count", 0) if isinstance(data, dict) else tag_pc.get(tag_a, 0)
    if query_pc <= 0:
        return pairs

    if isinstance(data, dict) and "related_tags" in data:
        for item in data["related_tags"]:
            tag_info = item.get("tag", {}) if isinstance(item.get("tag"), dict) else {}
            tag_b = tag_info.get("name") or item.get("name")
            if not tag_b or tag_b == tag_a or tag_b not in valid_names:
                continue
            freq = float(item.get("frequency", 0.0))
            if freq <= 0:
                continue
            cos_sim = float(item.get("cosine_similarity", 0.0))
            pairs.append({
                "source": tag_a, "target": tag_b,
                "frequency": freq, "cosine_similarity": cos_sim
            })
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, list) and len(item) >= 2:
                tag_b = str(item[0])
                if tag_b not in valid_names or tag_b == tag_a:
                    continue
                try:
                    raw_count = int(item[1])
                    freq = raw_count / query_pc
                except (ValueError, ZeroDivisionError):
                    freq = 0.0
                pairs.append({
                    "source": tag_a, "target": tag_b,
                    "frequency": freq, "cosine_similarity": 0.0
                })
    return pairs


def run_trim_cooc(db_path: str = None, top_k: int = 50,
                  min_pmi: float = 1.0, dry_run: bool = False,
                  progress_callback=None, cancel_check=None):
    if db_path is None:
        from tageditor.core.config import get_tag_db_config
        db_path = get_tag_db_config()['db_path']

    def _emit(event):
        if progress_callback:
            progress_callback(event)

    def _cancelled():
        return cancel_check and cancel_check()

    cdir = _cooc_dir(db_path)
    raw_parquet = cdir / 'cooccurrence_raw.parquet'
    raw_csv_legacy = cdir / 'cooccurrence_raw.csv'
    out_path = cdir / 'cooccurrence_clean.parquet'

    if not raw_parquet.exists():
        if raw_csv_legacy.exists():
            # 旧 CSV 格式自动迁移
            import pandas as _pd
            _tmp = raw_parquet.with_suffix('.parquet.tmp')
            _pd.read_csv(raw_csv_legacy, low_memory=False, encoding='utf-8') \
              .to_parquet(_tmp, index=False, compression='snappy')
            os.replace(_tmp, raw_parquet)
            raw_csv_legacy.unlink(missing_ok=True)
            log.info(f"[TrimCooc] 旧 CSV 已迁移到 Parquet: {raw_parquet}")
        else:
            msg = "[TrimCooc] 找不到原始共现文件，先运行 fetch-cooc"
            log.info(msg)
            _emit({'type': 'error', 'message': msg})
            return

    if _cancelled():
        _emit({'type': 'cancelled'})
        return

    import pandas as pd
    tags = _load_tags_from_db(db_path)
    tag_pc = {t['name']: int(t['post_count']) for t in tags}
    D = float(max(tag_pc.values())) if tag_pc else 1.0

    _trim_step = [0]  # 用 list 包裹便于闭包修改

    def _trim_emit(item: str):
        _trim_step[0] += 1
        _emit({'type': 'progress', 'current': _trim_step[0], 'total': 5, 'item': item})

    _trim_emit('正在加载共现数据...')
    # 只读需要的列 + source/target 用 category dtype：整表全列读实测峰值 937MB
    # （62.9MB 文件 / 434 万行——字符串列在 pandas 里是 object 指针数组，最吃内存），
    # 且 OOM 落在「长时间爬取刚成功、准备落盘」这个最坏时刻。选列 + category +
    # 及时 del 实测把峰值从 937MB 压到 ~550MB（tracemalloc 口径，dry-run 含五档
    # 统计的额外拷贝；真实落盘路径更低），机器能跑就行，不再依赖运气。
    df = pd.read_parquet(raw_parquet, columns=['source', 'target', 'frequency'])
    if 'source' not in df.columns:
        msg = "[TrimCooc] 格式错误"
        log.info(msg)
        _emit({'type': 'error', 'message': msg})
        return
    df['source'] = df['source'].astype('category')
    df['target'] = df['target'].astype('category')

    if _cancelled():
        _emit({'type': 'cancelled'})
        return

    df["frequency"] = pd.to_numeric(df["frequency"], errors="coerce").fillna(0.0)
    orig_len = len(df)
    log.info(f"[TrimCooc] 原始有向边: {orig_len:,}, D={D:,.0f}")
    _trim_emit(f'原始有向边: {orig_len:,}')

    # 过滤有效行
    count_target = df["target"].map(tag_pc)
    count_source = df["source"].map(tag_pc)
    valid = (count_target.notna() & count_source.notna() &
             (count_target > 0) & (count_source > 0) & (df["frequency"] > 0))
    df = df[valid].copy()
    # 过滤后不再需要原始行，及时 del 让 GC 归还（过滤前的 434 万行不归还就白占）
    del valid, count_target, count_source
    # 过滤后重新取 post_count（确保长度一致）
    count_target = df["target"].map(tag_pc).to_numpy()
    count_source = df["source"].map(tag_pc).to_numpy()
    log.info(f"[TrimCooc] 过滤后: {len(df):,}")
    _trim_emit(f'过滤后: {len(df):,} 条边')

    if _cancelled():
        _emit({'type': 'cancelled'})
        return

    # PMI 计算
    pmi_ratio = (df["frequency"].to_numpy() * D) / count_target
    df["pmi"] = np.where(pmi_ratio > 0, np.log2(pmi_ratio), -100.0)
    df["count"] = (df["frequency"] * count_source).round().astype(int)
    del pmi_ratio, count_target, count_source

    if dry_run:
        log.info(f"\n[TrimCooc] Dry-Run (Top-K={top_k})")
        print(f"  {'PMI ≥':<8} {'过滤后边数':<12} {'Top-K后':<12}")
        for t in range(1, 6):
            ft = df[df["pmi"] >= t].copy()
            pmi_kept = len(ft)
            final_kept = 0
            if pmi_kept > 0:
                ft.sort_values(["source", "pmi", "count"], ascending=[True, False, False], inplace=True)
                top_df = ft.groupby("source", sort=False).head(top_k)
                final_kept = _fold_undirected(top_df)
            del ft, top_df    # dry-run 每档都复制一份，不 del 的话峰值被五档叠加抬高
            print(f"  >= {t:<6} {pmi_kept:<12,} {final_kept:<12,}")
        del df
        return

    if _cancelled():
        _emit({'type': 'cancelled'})
        return

    df = df[df["pmi"] >= min_pmi].copy()
    if df.empty:
        msg = "[TrimCooc] PMI 过滤后无数据"
        log.info(msg)
        _emit({'type': 'error', 'message': msg})
        return

    if _cancelled():
        _emit({'type': 'cancelled'})
        return

    _trim_emit(f'PMI ≥ {min_pmi}: {len(df):,} 条边，取 Top-{top_k}...')
    df.sort_values(["source", "pmi", "count"], ascending=[True, False, False], inplace=True)
    top_df = df.groupby("source", sort=False).head(top_k).copy()

    if _cancelled():
        _emit({'type': 'cancelled'})
        return

    # 折叠为无向边
    s = top_df["source"].to_numpy()
    t = top_df["target"].to_numpy()
    mask = s < t
    top_df["tag_a"] = np.where(mask, s, t)
    top_df["tag_b"] = np.where(mask, t, s)

    result = (top_df.groupby(["tag_a", "tag_b"], as_index=False)
              .agg({"count": "max", "pmi": "max"})
              .reset_index(drop=True))
    result.sort_values("pmi", ascending=False, inplace=True)
    _trim_emit(f'折叠为无向边: {len(result):,} 条')
    # 原子落盘：/tag_cooc 每次请求直接读这个文件，截断的半截 parquet 会让共现推荐整体 500
    _tmp_out = out_path.with_suffix('.parquet.tmp')
    result[["tag_a", "tag_b", "count"]].to_parquet(_tmp_out, index=False, compression="snappy")
    os.replace(_tmp_out, out_path)
    msg = f"完成: {len(result):,} 条无向边"
    log.info(f"[TrimCooc] {msg} → {out_path}")
    _emit({'type': 'complete', 'item': msg})


def _fold_undirected(df) -> int:
    s = df["source"].to_numpy()
    t = df["target"].to_numpy()
    mask = s < t
    a = np.where(mask, s, t)
    b = np.where(mask, t, s)
    pairs = set(zip(a, b))
    return len(pairs)


# ═══════════════════════════════════════════════════════════════════════════
# trim_artist_cooc — NPMI 降维截断
# ═══════════════════════════════════════════════════════════════════════════

def run_trim_artist_cooc(db_path: str = None, top_k: int = 50,
                         min_npmi: float = 0.15, dry_run: bool = False):
    if db_path is None:
        from tageditor.core.config import get_tag_db_config
        db_path = get_tag_db_config()['db_path']

    cdir = _cooc_dir(db_path)
    cooc_path = cdir / 'tag_artist_cooc.parquet'
    if not cooc_path.exists():
        log.info("[TrimArtistCooc] 找不到文件，先运行 fetch-artist-cooc")
        return

    import pandas as pd
    tags = _load_tags_from_db(db_path)
    tag_pc = {t['name']: int(t['post_count']) for t in tags}
    D = float(max(tag_pc.values())) if tag_pc else 1.0

    df = pd.read_parquet(cooc_path)
    required = {"tag", "artist", "artist_post_count", "cooc_count", "frequency"}
    if not required.issubset(set(df.columns)):
        log.info(f"[TrimArtistCooc] 缺少列: {required - set(df.columns)}")
        return

    # 清除旧 PMI/NPMI 列
    for c in ["pmi", "npmi"]:
        if c in df.columns:
            df = df.drop(columns=[c])

    df["frequency"] = pd.to_numeric(df["frequency"], errors="coerce")
    df["cooc_count"] = pd.to_numeric(df["cooc_count"], errors="coerce")

    # 映射 tag post_count
    tag_pc_s = df["tag"].map(tag_pc)
    df["tag_post_count"] = tag_pc_s
    valid = (tag_pc_s.notna() & (tag_pc_s > 0) & (df["frequency"] > 0) & (df["cooc_count"] > 0))
    df = df[valid].copy()

    orig_len = len(df)
    log.info(f"[TrimArtistCooc] 原始边数: {orig_len:,}")

    # PMI
    pmi_ratio = (df["frequency"].to_numpy() * D) / df["tag_post_count"].to_numpy()
    df["pmi"] = np.where(pmi_ratio > 0, np.log2(pmi_ratio), -100.0)

    # NPMI
    p_ab = df["cooc_count"].to_numpy() / D
    denom = np.where(p_ab > 0, -np.log2(p_ab), 1.0)
    denom = np.where(denom > 0, denom, 1.0)
    df["npmi"] = df["pmi"].to_numpy() / denom
    df["npmi"] = df["npmi"].clip(-1.0, 1.0)

    if dry_run:
        log.info(f"\n[TrimArtistCooc] Dry-Run (Top-K={top_k})")
        print(f"  {'NPMI ≥':<8} {'过滤后':<12} {'Top-K后':<12} {'画师数':<8}")
        for t in [0.0, 0.1, 0.2, 0.3, 0.4, 0.5]:
            ft = df[df["npmi"] >= t].copy()
            pmi_kept = len(ft)
            final_kept = 0
            n_art = 0
            if pmi_kept > 0:
                ft.sort_values(["artist", "npmi", "cooc_count"], ascending=[True, False, False], inplace=True)
                ft = ft.groupby("artist", sort=False).head(top_k)
                final_kept = len(ft)
                n_art = ft["artist"].nunique()
            print(f"  >= {t:<6} {pmi_kept:<12,} {final_kept:<12,} {n_art:<8,}")
        return

    df = df[df["npmi"] >= min_npmi].copy()
    if df.empty:
        log.warning("[TrimArtistCooc] NPMI 过滤后无数据")
        return

    df.sort_values(["artist", "npmi", "cooc_count"], ascending=[True, False, False], inplace=True)
    df = df.groupby("artist", sort=False).head(top_k).copy()

    out_cols = ["tag", "artist", "artist_post_count", "cooc_count", "frequency", "pmi", "npmi"]
    df_out = df[out_cols].reset_index(drop=True)
    df_out.sort_values(["tag", "npmi"], ascending=[True, False], inplace=True)
    _tmp_artist = cooc_path.with_suffix('.parquet.tmp')
    df_out.to_parquet(_tmp_artist, index=False, compression="snappy")
    os.replace(_tmp_artist, cooc_path)

    n_edges = len(df_out)
    n_art = df_out["artist"].nunique()
    n_tag = df_out["tag"].nunique()
    log.info(f"[TrimArtistCooc] 完成: {n_edges:,} 条边, {n_art} artists, {n_tag} tags")
