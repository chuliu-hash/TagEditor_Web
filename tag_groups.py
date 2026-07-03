# -*- coding: utf-8 -*-
"""tag_groups — 从 Danbooru Wiki 爬取标签组（tag_group）体系。

从 tag_groups 目录页解析所有子组，逐个抓取每组详情页提取成员标签。
支持中断续传：取消时保存断点（.tag_groups_progress.json），下次重跑跳过已爬取 group。
输出: data/tag_groups.json — tag_to_groups/group_to_tags/group_cn_names
"""
import re
import json
import requests
import time
import os
from pathlib import Path
from config import get_tag_db_config, get_danbooru_config


def _fetch_wiki_page(title: str, auth: dict, proxies: dict | None, headers: dict) -> dict | None:
    """请求 wiki_pages.json 精确查询单页。"""
    api_url = "https://danbooru.donmai.us/wiki_pages.json"
    for attempt in range(3):
        try:
            resp = requests.get(
                api_url,
                params={**auth, 'search[title]': title, 'limit': 1},
                headers=headers,
                proxies=proxies,
                timeout=30
            )
            if resp.status_code == 429:
                print(f"[TagGroups] 429 限流，休眠 30 秒...")
                time.sleep(30)
                continue
            resp.raise_for_status()
            data = resp.json()
            return data[0] if data else None
        except requests.exceptions.RequestException as e:
            if attempt < 2:
                time.sleep(1)
            else:
                print(f"[TagGroups] 请求失败 ({title}): {e}")
    return None


def _parse_group_titles(index_body: str) -> list[str]:
    """从 tag_groups 主目录页 body 解析所有子 group 的 wiki title。"""
    titles = []
    for m in re.finditer(r'\[\[Tag [Gg]roup:([^\]|]+?)(?:\|[^\]]*)?\]\]', index_body, re.IGNORECASE):
        raw = m.group(1).strip()
        title = "tag_group:" + raw.lower().replace(' ', '_')
        titles.append(title)
    return list(dict.fromkeys(titles))


def _parse_group_members(body: str) -> list[str]:
    """从 tag group 页 body 提取成员标签名。"""
    tags = []
    for m in re.finditer(r'\[\[([^\]|]+?)(?:\|[^\]]*)?\]\]', body):
        raw = m.group(1).strip()
        normalized = raw.lower().replace(' ', '_')
        if any(normalized.startswith(p) for p in (
            'tag_group:', 'list_of_', 'help:', 'pool_', 'tag_groups'
        )):
            continue
        tags.append(normalized)
    return list(dict.fromkeys(tags))


def _save_checkpoint(path: Path, completed_titles: set, tag_to_groups: dict, group_to_tags: dict):
    """保存断点到临时文件后重命名覆盖，保证不写坏。"""
    tmp = str(path) + '.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump({
                'completed_titles': sorted(completed_titles),
                'tag_to_groups': tag_to_groups,
                'group_to_tags': group_to_tags,
            }, f, ensure_ascii=False)
        if path.exists():
            os.remove(str(path))
        os.rename(tmp, str(path))
    except Exception as e:
        print(f"[TagGroups] 保存断点失败: {e}")


def run(db_path: str = None, progress_callback=None, cancel_check=None):
    """爬取 Danbooru 标签组体系。

    Args:
        db_path: 数据库路径
        progress_callback: 进度回调，接收 dict 事件
        cancel_check: 可选可调用，返回 True 表示请求取消
    """
    if db_path is None:
        db_path = get_tag_db_config()['db_path']

    def _emit(event):
        if progress_callback:
            progress_callback(event)

    def _cancelled():
        return cancel_check and cancel_check()

    dan_cfg = get_danbooru_config()
    proxy = dan_cfg.get('proxy', '')
    proxies = {'http': proxy, 'https': proxy} if proxy else None

    USER_NAME = os.environ.get('DANBOORU_USER_NAME', '')
    API_KEY = os.environ.get('DANBOORU_API_KEY', '')
    if not USER_NAME or not API_KEY:
        msg = "[TagGroups] 未配置 DANBOORU_USER_NAME 或 DANBOORU_API_KEY"
        print(msg)
        _emit({'type': 'fatal', 'error': msg})
        return

    auth = {'login': USER_NAME, 'api_key': API_KEY}
    headers = {"User-Agent": "TagEditorWeb/1.0", "Accept": "application/json"}

    base_dir = Path(db_path).parent
    out_path = base_dir / 'tag_groups.json'
    progress_file = base_dir / '.tag_groups_progress.json'

    # Step 1: 抓取主目录页
    if _cancelled():
        _emit({'type': 'cancelled', 'item': '已取消'})
        return
    print("[TagGroups] 正在抓取主目录页 tag_groups...")
    _emit({'type': 'progress', 'page': 0, 'total': '?', 'item': '正在抓取主目录页 tag_groups...'})
    index_page = _fetch_wiki_page('tag_groups', auth, proxies, headers)
    if not index_page:
        msg = "[TagGroups] 无法获取主目录页"
        print(msg)
        _emit({'type': 'fatal', 'error': msg})
        return

    group_titles = _parse_group_titles(index_page.get('body', ''))
    msg = f"[TagGroups] 目录页解析到 {len(group_titles)} 个 group"
    print(msg)
    _emit({'type': 'progress', 'page': 0, 'total': len(group_titles), 'item': msg})
    if not group_titles:
        msg = "[TagGroups] 未解析到任何 group，终止"
        print(msg)
        _emit({'type': 'fatal', 'error': msg})
        return

    # Step 2: 加载断点数据（上次中断时保存的部分结果）
    completed_titles: set = set()
    tag_to_groups: dict[str, list[str]] = {}
    group_to_tags: dict[str, list[str]] = {}
    if progress_file.exists():
        try:
            saved = json.loads(progress_file.read_text(encoding='utf-8'))
            completed_titles = set(saved.get('completed_titles', []))
            tag_to_groups = saved.get('tag_to_groups', {})
            group_to_tags = saved.get('group_to_tags', {})
            print(f"[TagGroups] 从断点恢复：跳过 {len(completed_titles)} 个已爬取的 group")
            _emit({'type': 'progress', 'page': len(completed_titles), 'total': len(group_titles),
                   'item': f'断点恢复，跳过 {len(completed_titles)} 个已爬取的 group'})
        except Exception as e:
            print(f"[TagGroups] 断点文件读取失败，从头开始: {e}")

    # Step 3: 逐个查询每个 tag group 页，提取成员
    for i, title in enumerate(group_titles):
        if title in completed_titles:
            continue
        if _cancelled():
            _save_checkpoint(progress_file, completed_titles, tag_to_groups, group_to_tags)
            _emit({'type': 'cancelled', 'new_count': len(completed_titles), 'item': '用户取消，断点已保存，下次可续传'})
            return
        item_text = f"[{i + 1}/{len(group_titles)}] {title}"
        print(f"  {item_text}")
        _emit({'type': 'progress', 'page': i + 1, 'total': len(group_titles), 'item': item_text})
        page = _fetch_wiki_page(title, auth, proxies, headers)
        if not page:
            print(f"  跳过（页面不存在或请求失败）")
            time.sleep(0.3)
            continue
        members = _parse_group_members(page.get('body', ''))
        group_to_tags[title] = members
        for tag in members:
            tag_to_groups.setdefault(tag, []).append(title)
        completed_titles.add(title)
        time.sleep(0.3)
        # 每爬 5 个 group 存一次断点
        if len(completed_titles) % 5 == 0:
            _save_checkpoint(progress_file, completed_titles, tag_to_groups, group_to_tags)

    # Step 4: 全部完成，合并中文名后写入最终文件
    existing_cn_names: dict[str, str] = {}
    if out_path.exists():
        try:
            with open(out_path, 'r', encoding='utf-8') as f:
                existing_cn_names = json.load(f).get('group_cn_names', {})
        except Exception:
            pass

    group_cn_names: dict[str, str] = {}
    for gid in group_to_tags:
        group_cn_names[gid] = existing_cn_names.get(gid, "")

    result = {
        'tag_to_groups': tag_to_groups,
        'group_to_tags': group_to_tags,
        'group_cn_names': group_cn_names,
    }

    # 先写临时文件再重命名覆盖
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = out_path.with_suffix('.tmp')
    with open(tmp_path, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    if out_path.exists():
        os.remove(str(out_path))
    os.rename(str(tmp_path), str(out_path))

    # 清理断点文件
    if progress_file.exists():
        progress_file.unlink()

    msg = (f"[TagGroups] 完成：{len(group_to_tags)} 个 group，覆盖 {len(tag_to_groups)} 个标签，"
           f"{sum(1 for v in group_cn_names.values() if v)} 个已有中文名")
    print(msg)
    _emit({'type': 'complete', 'groups': len(group_to_tags), 'tags': len(tag_to_groups), 'item': msg})
