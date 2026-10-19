"""按用户自定义文件格式优先级对搜索结果排序。

Everything 服务端仅支持 name/path/size/date_modified 等固定字段排序，
不支持「按自定义扩展名优先级」排序。因此本模块把"文件格式优先级"作为
**主键**在客户端完成，并以结果列表的既有顺序作为**次级键**——依靠 Python
稳定排序保证：同格式内的相对顺序保持不变（本地保持 Everything 次级序、
ima 保持接口返回序；本地与 ima 同为某格式时相邻混排，本地在前）。

格式键（_format_key）：
- 本地文件夹 -> "folder"（可作为普通格式列入 format_order，从而把文件夹
  排到任意位置，与各文件格式混排）；
- 其余（本地文件 / ima 文档）取文件名或标题的扩展名（小写、不含点）；
- ima 文档与本地文件一样按扩展名参与混排，而不再固定排在最后。
"""
from __future__ import annotations

import os
from typing import List, Tuple

from core.config import Settings
from core.models import SearchResult

# 文件夹的伪格式键
FOLDER_KEY = "folder"


def _format_key(r: SearchResult) -> str:
    """提取用于格式排序的键：本地文件夹->'folder'；其余取标题/路径扩展名。"""
    if r.source == "local" and r.kind == "folder":
        return FOLDER_KEY
    name = r.title or (r.path if r.source == "local" else "") or ""
    return os.path.splitext(name)[1].lower().lstrip(".")


def format_rank(r: SearchResult, settings: Settings) -> Tuple[int, int]:
    """返回 (分组, 组内序号)，序号越小越靠前。

    - 若 format_order 中含 'folder'，文件夹按其所列位置与各格式混排；
    - 否则文件夹遵循 folders_first：置顶为分组 0（排在所有格式之前）；
      不置顶则按"未列入格式"处理（排在所有已列格式之后）；
    - 文件与 ima 文档一律按其扩展名在 format_order 中的下标排序，
      未列入的排最后（下标 = len）；同格式的本地与 ima 相邻混排。
    """
    order = settings.format_order
    n = len(order)
    key = _format_key(r)

    if key == FOLDER_KEY and FOLDER_KEY not in order and settings.folders_first:
        return (0, 0)
    idx = order.index(key) if key in order else n
    return (1, idx)


def reorder_by_format(
    results: List[SearchResult], settings: Settings
) -> List[SearchResult]:
    """重排搜索结果；未做任何重排时原样返回。

    两步（均可独立开关）：
    1. 若 format_sort_enabled：按格式优先级排序（主键为格式）；
    2. 若 group_by_name：把**同名**结果归组到该名称首次出现处——本地与 ima 的
       同名项因此排在一起（混排，不再分"本地一堆 / ima 一堆"）。用稳定排序 +
       "名称首次出现下标"作键，整体顺序保持首次出现顺序（本地 Everything 顺序为骨架，
       ima 副本贴到同名本地项旁，ima 独有项跟在后面）。
    """
    out = results
    if settings.format_sort_enabled:
        out = sorted(out, key=lambda r: format_rank(r, settings))
    if getattr(settings, "group_by_name", True):
        out = _group_by_name_stable(out)
    return out


def _name_key(r: SearchResult) -> str:
    """同名归组的比较键：标题去空白、忽略大小写。"""
    return (r.title or "").strip().casefold()


def _group_by_name_stable(items: List[SearchResult]) -> List[SearchResult]:
    """把同名项归组到其首次出现位置（稳定排序，保留组内与组间既有顺序）。"""
    first_index: dict = {}
    for i, it in enumerate(items):
        first_index.setdefault(_name_key(it), i)
    return sorted(items, key=lambda it: first_index[_name_key(it)])
