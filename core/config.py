"""配置模块：读取 ima 凭证、Everything HTTP 端口等。

优先级：
  1. 环境变量（IMA_OPENAPI_CLIENTID / IMA_OPENAPI_APIKEY / EVERYTHING_HTTP_PORT / EVERYTHING_HTTP_HOST）
  2. 配置文件 ~/.config/ima/client_id 与 api_key
  3. 内置默认值
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

# ima OpenAPI 基础地址
IMA_OPENAPI_BASE = os.environ.get(
    "IMA_OPENAPI_BASE", "https://ima.qq.com/openapi"
)
# ima OpenAPI 凭证存放目录（与 ima 官方 skill 保持一致）；在调用时按环境变量解析，便于测试与覆盖
def ima_config_dir() -> Path:
    return Path(os.environ.get("IMA_CONFIG_DIR", os.path.expanduser("~/.config/ima")))


@dataclass
class ImaConfig:
    client_id: str
    api_key: str
    base_url: str = IMA_OPENAPI_BASE

    @property
    def has_credentials(self) -> bool:
        return bool(self.client_id) and bool(self.api_key)


# Everything HTTP Server 支持的排序字段（见官方文档 sort 参数）
EVERYTHING_SORT_VALUES = ("name", "path", "date_modified", "size")


@dataclass
class EverythingConfig:
    host: str = "127.0.0.1"
    port: int = 80
    timeout: float = 5.0
    count: int = 200  # 单次最多返回结果数
    sort: str = "name"  # 排序字段：name/path/date_modified/size
    ascending: bool = True  # 是否升序（与 Everything GUI 默认一致）

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def ascending_int(self) -> int:
        return 1 if self.ascending else 0

    @property
    def sort_key(self) -> str:
        """归一化的排序字段，确保落在 Everything 允许的取值内。"""
        return self.sort if self.sort in EVERYTHING_SORT_VALUES else "name"


def load_ima_config() -> ImaConfig:
    """读取 ima 凭证：先环境变量，再配置文件。"""
    client_id = os.environ.get("IMA_OPENAPI_CLIENTID", "") or os.environ.get("IMA_CLIENT_ID", "")
    api_key = os.environ.get("IMA_OPENAPI_APIKEY", "") or os.environ.get("IMA_API_KEY", "")

    if not client_id:
        p = ima_config_dir() / "client_id"
        if p.exists():
            client_id = p.read_text(encoding="utf-8").strip()
    if not api_key:
        p = ima_config_dir() / "api_key"
        if p.exists():
            api_key = p.read_text(encoding="utf-8").strip()

    return ImaConfig(client_id=client_id, api_key=api_key)


def save_ima_credentials(client_id: str, api_key: str) -> None:
    """将 ima OpenAPI 凭证写入 ima_config_dir()（默认 ~/.config/ima）。

    目录不存在时自动创建；与 ima 官方 skill 约定一致（client_id / api_key 各占一文件）。
    """
    d = ima_config_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / "client_id").write_text(client_id.strip(), encoding="utf-8")
    (d / "api_key").write_text(api_key.strip(), encoding="utf-8")


def load_ima_kb_ids() -> Optional[List[str]]:
    """返回显式指定的知识库 ID 列表；未配置则返回 None（按名称自动发现）。"""
    raw = os.environ.get("IMA_KB_IDS", "")
    if not raw:
        p = ima_config_dir() / "kb_ids"
        if p.exists():
            raw = p.read_text(encoding="utf-8").strip()
    if not raw:
        return None
    ids = [x.strip() for x in raw.replace("\n", ",").split(",") if x.strip()]
    return ids or None


def load_everything_config() -> EverythingConfig:
    """读取 Everything HTTP Server 配置。"""
    host = os.environ.get("EVERYTHING_HTTP_HOST", "127.0.0.1")
    try:
        port = int(os.environ.get("EVERYTHING_HTTP_PORT", "80"))
    except ValueError:
        port = 80
    try:
        timeout = float(os.environ.get("EVERYTHING_HTTP_TIMEOUT", "5.0"))
    except ValueError:
        timeout = 5.0
    sort = os.environ.get("EVERYTHING_SORT", "name")
    if sort not in EVERYTHING_SORT_VALUES:
        sort = "name"
    asc_raw = os.environ.get("EVERYTHING_SORT_ASC", "1")
    ascending = asc_raw not in ("0", "false", "False", "no", "off")
    return EverythingConfig(
        host=host, port=port, timeout=timeout, sort=sort, ascending=ascending
    )


# ---------------------------------------------------------------------------
# 用户设置（文件格式优先级排序）
# ---------------------------------------------------------------------------

# 默认文件格式优先级（仅供参考，用户可在「设置」中修改）。
# "folder" 是文件夹的伪格式，列入列表即可把文件夹排到任意位置与各格式混排；
# 未列入时由 folders_first（文件夹置顶）决定。
DEFAULT_FORMAT_ORDER = ["folder", "pdf", "docx", "xlsx", "pptx", "txt", "md"]


@dataclass
class Settings:
    # 是否启用「按文件格式优先级排序」（主键），关闭时走 Everything 原有排序
    format_sort_enabled: bool = False
    # 文件夹是否始终置顶（仅 format_sort_enabled 时生效）
    folders_first: bool = True
    # 文件格式优先级（扩展名，不含点，小写），越靠前越优先
    format_order: List[str] = field(default_factory=lambda: list(DEFAULT_FORMAT_ORDER))
    # ima 跨库检索并发/提速配置（与 search/ima_search 的 env 默认值一致）
    ima_max_workers: int = 4     # 最大并发线程数
    ima_req_per_sec: float = 4.0  # 全局请求速率上限（令牌桶，请求/秒）
    ima_burst: int = 2           # 令牌桶突发容量（最大瞬时并发）
    # 同名结果归组：把本地与 ima 中同名的结果排在一起（混排，不再分本地/ima 两部分）
    group_by_name: bool = True


def settings_path() -> Path:
    """设置文件存放位置。

    - 打包后（PyInstaller onefile）：temp 包目录不可写且易失，改存用户可写的
      %APPDATA%\\SuperSearch\\settings.json，确保首选项跨运行持久化。
    - 开发环境：仍放在仓库根目录，便于调试与纳入版本控制（已被 .gitignore 忽略）。
    """
    if getattr(sys, "frozen", False):
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        return Path(base) / "SuperSearch" / "settings.json"
    return Path(__file__).resolve().parent.parent / "settings.json"


def _normalize_ext(e: str) -> str:
    return str(e).strip().lower().lstrip(".")


def _clamp_int(v, lo, hi, default):
    try:
        return max(lo, min(hi, int(v)))
    except (ValueError, TypeError):
        return default


def _clamp_float(v, lo, hi, default):
    try:
        return max(float(lo), min(float(hi), float(v)))
    except (ValueError, TypeError):
        return default


def load_settings() -> Settings:
    """读取用户设置；文件不存在或损坏时返回默认设置。"""
    p = settings_path()
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            return Settings(
                format_sort_enabled=bool(data.get("format_sort_enabled", False)),
                folders_first=bool(data.get("folders_first", True)),
                format_order=[
                    _normalize_ext(x)
                    for x in data.get("format_order", DEFAULT_FORMAT_ORDER)
                    if str(x).strip()
                ],
                ima_max_workers=_clamp_int(data.get("ima_max_workers", 4), 1, 32, 4),
                ima_req_per_sec=_clamp_float(data.get("ima_req_per_sec", 4.0), 0.5, 50.0, 4.0),
                ima_burst=_clamp_int(data.get("ima_burst", 2), 1, 16, 2),
                group_by_name=bool(data.get("group_by_name", True)),
            )
        except (json.JSONDecodeError, OSError, ValueError):
            pass
    return Settings()


def save_settings(s: Settings) -> None:
    """将用户设置写入 settings.json。"""
    p = settings_path()
    payload = {
        "format_sort_enabled": s.format_sort_enabled,
        "folders_first": s.folders_first,
        "format_order": s.format_order,
        "ima_max_workers": s.ima_max_workers,
        "ima_req_per_sec": s.ima_req_per_sec,
        "ima_burst": s.ima_burst,
        "group_by_name": s.group_by_name,
    }
    p.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
