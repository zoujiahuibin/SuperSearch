"""ima 知识库检索：通过 ima 官方 OpenAPI 查询。

流程：
  1. 确定要检索的知识库列表（显式配置 IMA_KB_IDS，或按名称 search_knowledge_base 发现）。
  2. 对每个知识库并行调用 search_knowledge（游标分页）。
  3. 合并去重，统一为 SearchResult。

鉴权 Header：
  ima-openapi-clientid / ima-openapi-apikey / Content-Type: application/json
统一返回：{code, msg, data}
"""
from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import requests

from core.config import ImaConfig
from core.models import SearchResult

_BASE = "https://ima.qq.com/openapi/wiki/v1"

# 与 ima 官方 skill 保持一致：请求需携带 ima-openapi-ctx（skill_version），否则可能 401
IMA_SKILL_VERSION = os.environ.get("IMA_SKILL_VERSION", "1.1.7")

# 并发 / 限速配置（可通过环境变量覆盖，便于按账号限流阈值调参）。
# ima 为账户级限流（code 200001），纯并行极易触发；令牌桶在"受控并发"下叠加网络延迟，
# 既明显快于串行，又不超阈值。速率单位是"请求/秒"，burst 为允许的最大突发（并发上限）。
IMA_MAX_WORKERS = int(os.environ.get("IMA_MAX_WORKERS", "4"))
IMA_REQ_PER_SEC = float(os.environ.get("IMA_REQ_PER_SEC", "4.0"))
IMA_BURST = int(os.environ.get("IMA_BURST", "2"))


class ImaApiError(Exception):
    def __init__(self, msg: str, code: int = -1):
        super().__init__(msg)
        self.msg = msg
        self.code = code


class _RateLimiter:
    """令牌桶：约束全局请求速率，避免触发 ima 账户级限流（code 200001）。

    - rate：可持续的请求速率（请求/秒）。
    - capacity：桶容量（最大突发/瞬时并发上限）。
    多个线程共享同一实例：每个 HTTP 请求前 acquire 一枚令牌，桶空则阻塞等待补充，
    从而把实际并发与速率都压在阈值内，同时仍能用多线程重叠网络往返延迟。
    """

    def __init__(self, rate: float, capacity: float):
        self.rate = max(rate, 0.1)
        self.capacity = max(capacity, 1.0)
        self._tokens = self.capacity
        self._lock = threading.Lock()
        self._last = time.monotonic()

    def acquire(self):
        while True:
            with self._lock:
                now = time.monotonic()
                elapsed = now - self._last
                self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
                self._last = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                wait = (1.0 - self._tokens) / self.rate
            time.sleep(wait)


def _post(cfg: ImaConfig, path: str, body: dict, timeout: float = 10.0,
          limiter: Optional["_RateLimiter"] = None) -> dict:
    url = f"{_BASE}/{path}"
    headers = {
        "ima-openapi-clientid": cfg.client_id,
        "ima-openapi-apikey": cfg.api_key,
        "ima-openapi-ctx": f"skill_version={IMA_SKILL_VERSION}",
        "Content-Type": "application/json",
    }
    if limiter is not None:
        limiter.acquire()  # 发请求前取令牌，约束全局速率/并发
    resp = requests.post(url, json=body, headers=headers, timeout=timeout)
    if resp.status_code >= 400:
        # ima 在非 2xx 时仍返回 JSON（含 code/msg），读出来透传，避免只抛出模糊的 HTTP 401
        try:
            payload = resp.json()
            code = payload.get("code", resp.status_code)
            msg = payload.get("msg") or resp.reason
        except ValueError:
            code = resp.status_code
            msg = resp.reason or f"HTTP {resp.status_code}"
        raise ImaApiError(f"{msg} (code {code})", code)
    payload = resp.json()
    code = payload.get("code", -1)
    if code != 0:
        raise ImaApiError(payload.get("msg", "未知错误"), code)
    return payload.get("data", {})


def get_media_url(cfg: ImaConfig, media_id: str) -> Optional[str]:
    """通过 get_media_info 获取知识库文档的可打开/下载直链（用于双击打开）。

    ima 的检索接口不返回文档 URL；该接口按 media_id 返回 data.url_info.url
    （通常为 COS 预签名链接，可被浏览器直接打开/下载）。失败或无直链时返回 None。
    """
    if not media_id:
        return None
    data = _post(cfg, "get_media_info", {"media_id": media_id})
    url_info = data.get("url_info") or {}
    url = url_info.get("url") or data.get("url") or data.get("media_url") or ""
    return url or None


def list_knowledge_bases(cfg: ImaConfig, query: str = "") -> List[Dict[str, str]]:
    """列出（按名称匹配或为空时列出全部）知识库，返回 [{id, name}]。

    兼容 ima 不同接口版本：优先用 kb_id / kb_name，回退到 id / name。
    """
    out: List[Dict[str, str]] = []
    cursor = ""
    while True:
        data = _post(cfg, "search_knowledge_base", {"query": query, "cursor": cursor, "limit": 20})
        for it in data.get("info_list", []):
            out.append({
                "id": it.get("kb_id") or it.get("id", ""),
                "name": it.get("kb_name") or it.get("name", ""),
            })
        if data.get("is_end") or not data.get("next_cursor"):
            break
        cursor = data.get("next_cursor", "")
        if not cursor:
            break
    return out


def _search_one_kb(cfg: ImaConfig, kb_id: str, kb_name: str, query: str,
                   limiter: Optional["_RateLimiter"] = None) -> List[SearchResult]:
    results: List[SearchResult] = []
    seen: set = set()
    cursor = ""
    while True:
        data = _post(
            cfg,
            "search_knowledge",
            {"query": query, "cursor": cursor, "knowledge_base_id": kb_id},
            limiter=limiter,
        )
        for it in data.get("info_list", []):
            media_id = it.get("media_id", "")
            if media_id in seen:
                continue
            seen.add(media_id)
            results.append(
                SearchResult(
                    source="ima",
                    title=it.get("title", "") or "(无标题)",
                    path="",
                    ref_id=media_id,
                    size=None,
                    modified=None,
                    snippet=it.get("highlight_content", ""),
                    kb_name=kb_name,
                )
            )
        if data.get("is_end") or not data.get("next_cursor"):
            break
        cursor = data.get("next_cursor", "")
        if not cursor:
            break
    return results


def _search_one_kb_with_retry(
    cfg: ImaConfig,
    kb_id: str,
    kb_name: str,
    query: str,
    max_retries: int = 4,
    base_backoff: float = 0.5,
    limiter: Optional["_RateLimiter"] = None,
) -> List[SearchResult]:
    """包装 _search_one_kb，对限流错误（code 200001）做指数退避重试。

    ima 账户级限流在并发检索多个知识库时极易触发；退避重试可让绝大多数
    限流请求最终成功，避免用户丢失部分知识库的结果。
    """
    last_exc: Optional[Exception] = None
    for attempt in range(max_retries + 1):
        try:
            return _search_one_kb(cfg, kb_id, kb_name, query, limiter=limiter)
        except ImaApiError as e:
            last_exc = e
            if e.code == 200001 and attempt < max_retries:
                time.sleep(base_backoff * (2 ** attempt))
                continue
            raise
        except requests.RequestException as e:
            last_exc = e
            if attempt < max_retries:
                time.sleep(base_backoff * (2 ** attempt))
                continue
            raise
    # 理论上不会到达（循环内已 raise），作为兜底
    assert last_exc is not None
    raise last_exc


def search_ima(
    query: str,
    cfg: ImaConfig,
    kb_ids: Optional[List[str]] = None,
    max_workers: Optional[int] = None,
    progress_callback: Optional[callable] = None,
    req_per_sec: Optional[float] = None,
    burst: Optional[int] = None,
) -> Tuple[List[SearchResult], List[str]]:
    """检索 ima 知识库，返回 (结果列表, 错误信息列表)。

    kb_ids 为显式知识库 ID 列表；为空时按名称匹配自动发现知识库。
    progress_callback(done, total) 在每个知识库检索完成后回调，用于汇报进度（百分比）。
    max_workers / req_per_sec / burst 控制并发与限速，优先取调用方传入，
    缺省时回退到环境变量（IMA_MAX_WORKERS / IMA_REQ_PER_SEC / IMA_BURST）。
    """
    if not cfg.has_credentials:
        return [], ["未配置 ima 凭证（client_id / api_key）"]
    if not query or not query.strip():
        return [], []

    kb_targets: List[Dict[str, str]]
    errors: List[str] = []
    if kb_ids:
        # 显式列表：名称未知时以 id 占位（可后续 get_knowledge_base 补全）
        kb_targets = [{"id": k, "name": k} for k in kb_ids]
    else:
        # 未指定知识库时，列出用户全部知识库并跨库检索文档
        # （而非用检索词去匹配知识库名称——那样文档词往往匹配不到库名而空结果）
        try:
            kb_targets = list_knowledge_bases(cfg, "")
        except ImaApiError as e:
            return [], [f"列举知识库失败：{e.msg}"]
        except requests.RequestException as e:
            return [], [f"连接 ima 失败：{e}"]

    if not kb_targets:
        # 没有按名称命中的知识库，尝试拉取用户全部知识库（名称搜索为空时回退）
        return [], []

    # 受控并发 + 令牌桶限速：ima 为账户级限流（code 200001），纯并行极易触发，
    # 但纯串行（数十个库）又太慢。折中方案——用线程池重叠网络往返延迟，
    # 同时用全局令牌桶把请求速率/突发压在阈值内；限流退避重试作为兜底。
    # 进度按"已完成库数 / 总库数"回调（并发完成顺序不定，done 为累计完成数）。
    workers = max_workers if max_workers else IMA_MAX_WORKERS
    rate = req_per_sec if req_per_sec else IMA_REQ_PER_SEC
    cap = burst if burst else IMA_BURST
    limiter = _RateLimiter(rate=rate, capacity=cap)
    all_results: List[SearchResult] = []
    total = len(kb_targets)
    done = 0
    lock = threading.Lock()
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futures = {
            ex.submit(_search_one_kb_with_retry, cfg, t["id"], t["name"], query,
                     4, 0.5, limiter): t
            for t in kb_targets
        }
        for f in as_completed(futures):
            try:
                all_results.extend(f.result())
            except ImaApiError as e:
                errors.append(f"知识库 {futures[f]['name']} 检索失败：{e.msg}")
            except requests.RequestException as e:
                errors.append(f"知识库 {futures[f]['name']} 连接失败：{e}")
            with lock:
                done += 1
                cur = done
            if progress_callback:
                progress_callback(cur, total)

    return all_results, errors
