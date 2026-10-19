"""按用户自定义文件格式优先级对搜索结果排序。

Nothing 服务端仅支持 name/path/size/date_modified 等固定字段排序，
不支持「按自定义扩展名优先级」排序。因此本模块把"文件格式优先级"作为
**主键**在客户端完成，并以 Everything 返回的次级顺序（name/path/size/date）
作为**次级键**——依靠 Python 稳定排序保证：同格式内的相对顺序
保持 Everything 的次级排序结果（含名称自然序 file2 < file10）。
"""
from __future__ import annotations

import os
from typing import List, Tuple

from core.config import Settings
from core.models import SearchResult


def format_rank(r: SearchResult, settings: Settings) -> Tuple[int, int]:
    """返回 (分组, 组内序号)：分组 0=文件夹(置顶) / 1=文件 / 2=ima；组内序号越小越靠前。

    - 文件夹且 folders_first：分组 0；
    - 否则按扩展名在 format_order 中的下标；未列入的格式排在最后（下标 = len）。
    """
    fmt_index = {ext: i for i, ext in enumerate(settings.format_order)}
    n = len(settings.format_order)

    if r.source != "local":
        return (2, 0)  # ima 文档始终排在本地文件之后

    if r.kind == "folder":
        if settings.folders_first:
            return (0, 0)
        return (1, fmt_index.get("folder", n))

    ext = os.path.splitext(r.title)[1].lower().lstrip(".")
    return (1, fmt_index.get(ext, n))


def reorder_by_format(
    results: List[SearchResult], settings: Settings
) -> List[SearchResult]:
    """按文件格式优先级重排；未启用时原样返回。

    输入中本地结果已按 Everything 的次级排序排列；稳定排序后，
    同 (分组, 组内序号) 内保持该次级顺序。
    """
    if not settings.format_sort_enabled:
        return results
    return sorted(results, key=lambda r: format_rank(r, settings))
