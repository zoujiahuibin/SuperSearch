"""本地文件检索：通过 Everything HTTP Server 查询。

Everything 1.4 自带 HTTP Server（Options -> HTTP Server 启用）。
查询格式（见官方文档 sort / ascending 参数）：
  http://127.0.0.1:PORT/?search=Q&json=1&count=N&sort=name&ascending=1
      &path_column=1&size_column=1&date_modified_column=1
sort 取值：name（名称）/ path（路径）/ date_modified（修改时间）/ size（大小）；
ascending 非零为升序。默认 sort=name、ascending=1，与 Everything GUI 默认排序一致。
注意：名称排序必须由 Everything 服务端完成（其"自然序" file2 在 file10 前），
不可在客户端用普通字符串排序替代，否则会与 Everything 不一致。

返回 JSON（Everything 1.4 HTTP Server 实测结构）：
  {"totalResults": N, "results": [
      {"type": "file|folder",
       "name": "真实条目名（含关键词）",
       "path": "父目录路径（不含条目名本身）",
       "size": 字节数（文件夹为 '' 空串）,
       "date_modified": "Windows FILETIME 字符串，如 '134332406219547572'"}
  ]}

注意：真实返回中 `name` 才是含关键词的条目名，`path` 是父目录。
（个别旧版本/自定义脚本可能返回 `path` 即完整路径、无 `name` 字段，
  此处两种格式均兼容。）
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import PureWindowsPath
from typing import List

import requests

from core.config import EverythingConfig
from core.models import SearchResult

# Windows FILETIME 起点（1601-01-01）到 Unix 纪元（1970-01-01）的秒数差
_FILETIME_EPOCH_OFFSET = 11644473600


def _filetime_to_datetime(ft) -> datetime:
    """将 Windows FILETIME 转换为 UTC datetime。

    入参可为 int 或字符串（Everything HTTP Server 实测为字符串）。
    无法解析时返回 None。
    """
    try:
        ft = int(ft)
    except (ValueError, TypeError):
        return None
    if ft <= 0:
        return None
    try:
        ts = (ft / 10_000_000.0) - _FILETIME_EPOCH_OFFSET
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    except (ValueError, OSError, OverflowError):
        return None


def _build_drive_filter(drives) -> str:
    """把勾选的盘符拼成 Everything 的 path: 过滤表达式。

    实测结论（2026-10-20，Everything HTTP Server 1.4）：
      - 必须是 `path:C:`（带冒号、**不带**结尾反斜杠），`path:C:\\` 会匹配 0 条；
      - 多盘用 `path:C: | path:E:`（空格分隔、**不要**外层括号），
        写成 `(path:C: | path:E:)` 反而会被 Everything 整体忽略（返回全部盘符）。
    因此这里：盘符统一规范成 `X:`（接受 "C" 或 "C:" 两种输入），
    多盘直接用空格 + `|` 连接，不套括号。

    drives 形如 ["C", "E"] 或 ["C:", "E:"]；为空则返回 ""（不限定，搜全部盘符）。
    """
    if not drives:
        return ""
    parts = []
    for d in drives:
        d = str(d).strip().upper()
        if not d:
            continue
        if not d.endswith(":"):
            d += ":"
        parts.append(f"path:{d}")
    if not parts:
        return ""
    return " | ".join(parts)


def search_local(query: str, cfg: EverythingConfig, drives: List[str] = None) -> List[SearchResult]:
    """查询 Everything HTTP Server，返回本地文件结果列表。

    drives 为勾选的盘符列表（如 ["C:", "E:]），非空时仅检索这些盘符；
    为空则检索全部盘符（默认行为）。失败时抛出 requests 异常，由调用方捕获。
    """
    if not query or not query.strip():
        return []

    drive_filter = _build_drive_filter(drives)
    search_query = f"{query.strip()} {drive_filter}".strip() if drive_filter else query.strip()

    params = {
        "search": search_query,
        "json": 1,
        "count": cfg.count,
        "sort": cfg.sort_key,
        "ascending": cfg.ascending_int,
        "path_column": 1,
        "size_column": 1,
        "date_modified_column": 1,
    }
    url = f"{cfg.base_url}/"
    # Everything 始终在本地；环境可能配置了 HTTP 代理，必须把 localhost 排除，
    # 否则请求会被代理转发而连不上本地服务（返回 502）。
    try:
        resp = requests.get(url, params=params, timeout=cfg.timeout, proxies={"http": None})
        resp.raise_for_status()
    except requests.ConnectionError as e:
        raise ConnectionError(
            f"无法连接 Everything HTTP Server（{cfg.base_url}）。"
            "请确认已在 Everything → 工具 → 选项 → HTTP 服务器 中勾选启用，并保持 Everything 正在运行。"
        ) from e
    except requests.Timeout as e:
        raise TimeoutError(
            f"连接 Everything HTTP Server（{cfg.base_url}）超时，请确认其已启用且端口正确。"
        ) from e

    data = resp.json()
    results: List[SearchResult] = []
    for item in data.get("results", []):
        name = item.get("name")
        parent = item.get("path") or ""

        # Everything 两种返回格式兼容：
        # 1) 新版（实测）：name=条目名（含关键词），path=父目录
        # 2) 旧版/兜底：无 name，path=完整路径
        if name:
            title = name
            if parent and not parent.rstrip("\\/").endswith(name):
                full_path = os.path.join(parent, name)
            else:
                full_path = parent or name
        else:
            full_path = parent
            title = PureWindowsPath(full_path).name if full_path else ""

        if not full_path:
            continue

        size = item.get("size")
        # Everything 将 size 以字符串返回（如 '15722453382'），文件夹为 '' 空串
        if size in (None, ""):
            size = None
        else:
            try:
                size = int(size)
            except (ValueError, TypeError):
                size = None
        raw_ft = item.get("date_modified")
        dt = _filetime_to_datetime(raw_ft) if raw_ft else None

        results.append(
            SearchResult(
                source="local",
                title=title,
                path=full_path,
                kind=item.get("type") or "",
                size=int(size) if isinstance(size, (int, float)) else None,
                modified=dt,
            )
        )
    return results


def is_everything_reachable(cfg: EverythingConfig) -> bool:
    """探测 Everything HTTP Server 是否可达（绕过本地代理）。"""
    try:
        requests.get(f"{cfg.base_url}/", params={"json": 1}, timeout=cfg.timeout, proxies={"http": None})
        return True
    except requests.RequestException:
        return False
