"""百度网盘检索：调用开放平台关键词检索接口，统一映射为 SearchResult。

流程（决策见 docs/adr/0002）：
  1. 经 core.baidu_pan 确保用户令牌有效（必要时自动刷新）；
  2. 以文件名关键词检索（全盘，支持目录/类型过滤）；
  3. 原始条目 -> SearchResult（source="baidu"），按展示上限截断。

云端响应可能上千条（实测常见词约 1000 条），故在映射层截断，
与 Everything 侧 count 上限的产品约定一致；截断值由调用方传入。
"""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional, Tuple

from core import baidu_pan
from core.models import SearchResult

# 百度文件类型编码 -> 中文名（开放平台 category 字段：1-7）
CATEGORY_NAMES = {
    1: "视频",
    2: "音频",
    3: "图片",
    4: "文档",
    5: "应用",
    6: "其他",
    7: "种子",
}

# 默认展示上限：云端单次响应可能上千条，界面默认只展示前 100 条
DEFAULT_LIMIT = 100

# 未授权时的引导文案（与 UI 菜单路径一致）
_NOT_AUTHORIZED_MSG = "未授权百度网盘：请经 设置 → 百度网盘授权… 完成授权"


def _to_dt(epoch) -> Optional[datetime]:
    """epoch 秒 -> datetime；非法值返回 None（网盘目录的 mtime 可能为 0）。"""
    try:
        ts = int(epoch)
    except (TypeError, ValueError):
        return None
    if ts <= 0:
        return None
    try:
        return datetime.fromtimestamp(ts)
    except (OverflowError, OSError, ValueError):
        return None


def _to_item(raw: dict) -> Optional[SearchResult]:
    """单条网盘条目 -> SearchResult；缺关键字段时返回 None 跳过。"""
    fsid = raw.get("fsid")
    path = (raw.get("path") or "").strip()
    if fsid is None or not path:
        return None
    is_dir = str(raw.get("isdir")) == "1"
    size_raw = raw.get("size")
    try:
        size = int(size_raw)
    except (TypeError, ValueError):
        size = 0
    return SearchResult(
        source="baidu",
        title=(raw.get("server_filename") or path.rsplit("/", 1)[-1]
               or "(未命名)"),
        path=path,
        kind="folder" if is_dir else "",
        ref_id=str(fsid),
        size=size if size > 0 else None,  # 目录 size 为 0，按未知处理
        modified=_to_dt(raw.get("server_mtime") or raw.get("local_mtime")),
        snippet="",
    )


def search_baidu(
    query: str,
    limit: int = DEFAULT_LIMIT,
    dir: str = "/",
    category: Optional[int] = None,
    token: Optional[dict] = None,
) -> Tuple[List[SearchResult], List[str]]:
    """检索百度网盘，返回 (结果列表, 错误信息列表)。

    - query：文件名关键词（自动截断到开放平台 30 字符上限）；
    - limit：展示上限（客户端截断，默认 100）；
    - dir：检索目录，默认全盘；category：文件类型 1-7，None 不限；
    - token：显式传入令牌（测试用）；缺省读本机令牌文件。
    未授权 / 网络失败 / 接口错误时返回空结果 + 一条中文错误（不抛异常），
    与 ima_search 的返回约定一致，便于 UI 统一处理。
    """
    if not query or not query.strip():
        return [], []
    if not (token or baidu_pan.has_authorization()):
        return [], [_NOT_AUTHORIZED_MSG]

    items, err = baidu_pan.search_files(
        query, dir=dir, category=category, token=token)
    if err:
        return [], [err]

    results: List[SearchResult] = []
    seen = set()
    for raw in items:
        r = _to_item(raw)
        if r is None or r.ref_id in seen:
            continue
        seen.add(r.ref_id)
        results.append(r)
        if len(results) >= max(1, int(limit)):
            break
    return results, []


def category_name(category) -> str:
    """文件类型编码 -> 中文名（详情面板展示用）；未知返回空串。"""
    try:
        return CATEGORY_NAMES.get(int(category), "")
    except (TypeError, ValueError):
        return ""
