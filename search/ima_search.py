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

# 知识库类型（UI 勾选项）与其在 API 返回 base_type 字段里的匹配关键字。
# 经真实接口确认（2026-10-20）：
#   个人知识库 -> base_type == "个人知识库"
#   订阅知识库 -> base_type == "我加入的订阅知识库"
# 共享知识库 当前账号无样本，按关键字 "共享" 做子串匹配；若官方实际字面值不同，
# 仅调整此处 _KB_TYPE_KEYWORDS 即可，无需改动调用方。
KB_TYPE_LABELS = ["个人知识库", "共享知识库", "订阅知识库"]
_KB_TYPE_KEYWORDS = {
    "个人知识库": "个人",
    "共享知识库": "共享",
    "订阅知识库": "订阅",
}

# ima 对订阅知识库「资料获取」的每日额度限制错误码（与个人库无关）。
# 触发后当日无法对订阅库做全文检索，需次日额度恢复；不属于限流(200001)，不应重试。
KB_QUOTA_EXCEEDED_CODE = 220021


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


def get_kb_numeric_id(cfg: ImaConfig, kb_id: str) -> Optional[str]:
    """尝试获取知识库的数字型 knowledgeBaseId（雪花 ID）；拿不到返回 None。

    背景（2026-10-23 实测修正，勿再回退旧逻辑）：ima 客户端/网页端的深链要求
    数字型 knowledgeBaseId（如 7516872677265288），而 OpenAPI 的知识库 ID 空间
    只有 base64 形态的 ima_kb_XXX——search_knowledge_base / get_knowledge_base
    （{"ids": [...]}）/ get_knowledge_list 三个接口均不暴露数字 ID。旧实现直接把
    current_path[0].folder_id 当数字 ID 返回，实测该值就是 ima_kb_XXX 本身；
    拼进深链后客户端报"参数错误"（根因是 ID 空间不同，非格式问题）。
    因此本函数增加纯数字校验：候选中确为数字才返回，否则 None（调用方应走
    浏览器直链回退，见 ui/main_window._open_ima）。将来若 OpenAPI 暴露数字 ID，
    本函数自动恢复可用，无需改动调用方。
    """
    if not kb_id:
        return None
    try:
        data = _post(
            cfg, "get_knowledge_list",
            {"knowledge_base_id": kb_id, "cursor": "", "limit": 1},
        )
    except ImaApiError:
        return None
    candidates = []
    cp = data.get("current_path") or []
    if cp and cp[0].get("folder_id"):
        candidates.append(cp[0]["folder_id"])
    for it in data.get("knowledge_list", []):
        pf = it.get("parent_folder_id")
        if pf:
            candidates.append(pf)
    for c in candidates:
        if str(c).isdigit():
            return str(c)
    return None


# 客户端内置前端的"知识库页"扩展 ID（REA 其前端常量 Yt.KnowledgeBase / ua.KnowledgeBase 得到）。
# 客户端自己的 web→电脑端 桥 TQ 用它构造：
#   imacopilot://open?url=<chrome-extension://<ext>/index.html?knowledgeBaseId=数字ID>
_IMA_KB_EXT_ID = "nkohmbngmopdajidckglcoehlaeepeoi"


def ima_client_kb_url(numeric_kb_id: str) -> str:
    """ima 电脑端「打开知识库」深链（精准定位到该库）。

    复刻客户端内置前端 TQ 桥：
        imacopilot://open?url=chrome-extension://<KB扩展>/index.html?knowledgeBaseId=<数字ID>
    numeric_kb_id 为数字型 knowledgeBaseId。REA 客户端本地缓存确认：客户端内部用
    数字 knowledgeBaseId（如 7516872677265288），与网页 ima.qq.com/wikis?
    knowledgeBaseId=<数字> 一致，而非 OpenAPI 的 base64 kb_id。
    ⚠ 调用方必须确保持有纯数字 ID：OpenAPI 不提供该 ID（见 get_kb_numeric_id），
    传入 ima_kb_XXX 会被客户端以"参数错误"拒绝。
    """
    return (
        f"imacopilot://open?url="
        f"chrome-extension://{_IMA_KB_EXT_ID}/index.html?knowledgeBaseId={numeric_kb_id}"
    )


def ima_client_doc_url(numeric_kb_id: str, media_id: str) -> str:
    """ima 电脑端「打开知识库内某篇文档」深链（精准定位到该篇）。

    复刻客户端内置前端「知识库页」(扩展 nkohmbngmopdajidckglcoehlaeepeoi) 的自动打开逻辑：
    该页在挂载时会读取 URL 中的 action / knowledgeBaseId / mediaId，且仅当
        action === "autoOpenMedia" 且 knowledgeBaseId 与 mediaId 同时存在
    时才会真正调 openKnowledgeMedia 打开对应文档
    （REA 其 index-Bdu-6Zli.js 的 k() 函数确认：「if(!(wr()===it.AutoOpenMedia&&!!H&&!!y))return」）。

    因此文档深链必须复用与「打开知识库」一致的 open?url= 桥（透明加载该扩展页），
    并在内层地址上显式带上 action=autoOpenMedia：
        imacopilot://open?url=chrome-extension://<KB扩展>/index.html
            ?action=autoOpenMedia&knowledgeBaseId=<数字ID>&mediaId=<媒体ID>
    注意：不能走 knowledgeDetail 协议——它不会注入 action=autoOpenMedia，
    页面只会定位到知识库而停留在“未打开文档”状态（即此前实测失败的根因）。
    numeric_kb_id 为数字型 knowledgeBaseId；media_id 为文档的 media_id（search 结果里的 ref_id）。
    ⚠ 调用方必须确保持有纯数字 knowledgeBaseId：OpenAPI 不提供该 ID
    （见 get_kb_numeric_id），传入 ima_kb_XXX 会被客户端以"参数错误"拒绝。
    """
    return (
        f"imacopilot://open?url="
        f"chrome-extension://{_IMA_KB_EXT_ID}/index.html"
        f"?action=autoOpenMedia&knowledgeBaseId={numeric_kb_id}&mediaId={media_id}"
    )


def list_knowledge_bases(cfg: ImaConfig, query: str = "") -> List[Dict[str, str]]:
    """列出（按名称匹配或为空时列出全部）知识库，返回 [{id, name, base_type}]。

    兼容 ima 不同接口版本：优先用 kb_id / kb_name，回退到 id / name。
    base_type 为知识库类型（个人知识库 / 共享知识库 / 订阅知识库），用于按类型筛选。
    """
    out: List[Dict[str, str]] = []
    cursor = ""
    while True:
        data = _post(cfg, "search_knowledge_base", {"query": query, "cursor": cursor, "limit": 20})
        for it in data.get("info_list", []):
            out.append({
                "id": it.get("kb_id") or it.get("id", ""),
                "name": it.get("kb_name") or it.get("name", ""),
                "base_type": it.get("base_type") or "",
            })
        if data.get("is_end") or not data.get("next_cursor"):
            break
        cursor = data.get("next_cursor", "")
        if not cursor:
            break
    return out


def _kb_matches_types(kb: Dict[str, str], kb_types) -> bool:
    """判断知识库是否落在所选类型内。

    kb_types 语义：
      - None  -> 不限类型（匹配全部）；
      - []    -> 未勾选任何类型（匹配 none，即不检索任何知识库）；
      - 列表  -> 只匹配 base_type 命中其中任一标签关键字的库。
    匹配方式：取各标签对应的关键字（_KB_TYPE_KEYWORDS）做 base_type 子串匹配。
    """
    if kb_types is None:
        return True
    if not kb_types:
        return False
    bt = (kb.get("base_type") or "")
    for label in kb_types:
        kw = _KB_TYPE_KEYWORDS.get(label)
        if kw and kw in bt:
            return True
    return False


def _ima_item_is_folder(it: dict, media_id: str) -> bool:
    """判断搜索结果是否为文件夹（而非文档）。

    经真实接口逆向确认（get_knowledge_list / search_knowledge）：
      - 文件夹项的 media_type == 99，且 media_id 以 "folder_" 开头；
      - 文档（PDF）media_type == 1、media_id 以 "pdf_" 开头；笔记 media_type == 11。
    因此以 media_type==99 或 media_id 前缀 "folder_" 为主判据，空 media_id 作兜底。
    """
    mt = it.get("media_type")
    if mt is not None and str(mt) == "99":
        return True
    if media_id and media_id.startswith("folder_"):
        return True
    if not media_id:
        return True
    return False


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
            is_folder = _ima_item_is_folder(it, media_id)
            # 文件夹无 media_id，用标题去重避免重复
            key = media_id or it.get("title", "")
            if key in seen:
                continue
            seen.add(key)
            results.append(
                SearchResult(
                    source="ima",
                    title=it.get("title", "") or "(无标题)",
                    path="",
                    kind="folder" if is_folder else "",
                    ref_id=media_id,
                    size=None,
                    modified=None,
                    snippet=it.get("highlight_content", ""),
                    kb_name=kb_name,
                    kb_id=kb_id,
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
    kb_types: Optional[List[str]] = None,
) -> Tuple[List[SearchResult], List[str]]:
    """检索 ima 知识库，返回 (结果列表, 错误信息列表)。

    kb_ids 为显式知识库 ID 列表；为空时按名称匹配自动发现知识库。
    kb_types 为所选知识库类型标签（如 ["个人知识库"]）；为 None 或覆盖全部类型时
    不做类型限制；否则只检索 base_type 命中所选类型的知识库。
    注意：kb_types 仅在自动发现（kb_ids 为空）时生效；显式指定 kb_ids 时以列表为准。
    progress_callback(done, total, result_count) 在每个知识库检索完成后回调，用于汇报进度：
    已完成库数 / 总库数 / 已得结果数（便于 UI 显示"已检索 N/Y 个库，已得 M 条结果"）。
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

        # 按所选知识库类型过滤（仅在自动发现时生效）：
        # kb_types 为 None（全选）或覆盖了全部类型 -> 不限制；
        # 否则只保留 base_type 命中所选类型的知识库（[] 表示全不选 -> 不检索任何库）。
        if kb_types is not None and set(kb_types) != set(KB_TYPE_LABELS):
            kb_targets = [t for t in kb_targets if _kb_matches_types(t, kb_types)]

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
    raw_errors = []  # (kb_name, msg, code) 原始错误，便于按 code 归类合并
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futures = {
            ex.submit(_search_one_kb_with_retry, cfg, t["id"], t["name"], query,
                     4, 0.5, limiter): t
            for t in kb_targets
        }
        for f in as_completed(futures):
            name = futures[f]["name"]
            try:
                all_results.extend(f.result())
            except ImaApiError as e:
                raw_errors.append((name, e.msg, e.code))
            except requests.RequestException as e:
                raw_errors.append((name, f"连接失败：{e}", -1))
            with lock:
                done += 1
                cur = done
                nres = len(all_results)
            # 回调带"已完成库数 / 总库数 / 已得结果数"，便于 UI 显示可读进度
            if progress_callback:
                progress_callback(cur, total, nres)

    # 整理错误信息：订阅库「资料获取」每日额度(220021)耗尽时，把 N 条重复报错合并为一条明确提示，
    # 避免刷屏；个人库等其它真实错误仍逐条显示。
    quota_errs = [r for r in raw_errors if r[2] == KB_QUOTA_EXCEEDED_CODE]
    other_errs = [r for r in raw_errors if r[2] != KB_QUOTA_EXCEEDED_CODE]
    if quota_errs:
        names = "、".join(r[0] for r in quota_errs[:5])
        more = "" if len(quota_errs) <= 5 else f" 等共 {len(quota_errs)} 个"
        errors.append(
            f"订阅知识库（{names}{more}）因 ima 每日「资料获取」额度已用尽，今日暂不可全文检索；"
            f"个人知识库不受影响。明天额度恢复后可正常检索。"
        )
    for name, msg, _ in other_errs:
        errors.append(f"知识库 {name} 检索失败：{msg}")

    return all_results, errors
