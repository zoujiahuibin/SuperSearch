"""百度网盘开放平台客户端：OAuth 2.0 设备码授权 + 文件名关键词检索。

设计要点（决策见 docs/adr/0001、0002）：
  - 授权用设备码流：桌面应用无回调地址，用户在浏览器输入 user_code 确认，
    本模块轮询令牌端点直至拿到 access_token / refresh_token；
  - 用户令牌（含应用凭证）只存本机 %APPDATA%\\SuperSearch\\baidu_netdisk.json
    （打包后；开发环境存仓库根，已被 .gitignore 忽略）；
  - access_token 有效期 30 天，剩余不足 REFRESH_MARGIN_SECONDS 时自动用
    refresh_token 续期；检索遇令牌失效错误（errno -6）时刷新重试一次；
  - 关键词检索为 GET /rest/2.0/xpan/file?method=search，全盘覆盖，
    单次响应可能上千条，由调用方按展示上限截断。

接口依据（百度网盘开放平台 union 文档，2026-10 核对外加真实账号实测）：
  - 设备码：POST /oauth/2.0/device/code（client_id, response_type, scope）
  - 轮询/刷新：POST /oauth/2.0/token（grant_type=device_token | refresh_token）
  - 检索：GET /rest/2.0/xpan/file?method=search（key/dir/category/recursion/num）
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import quote

import requests

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

OAUTH_DEVICE_CODE_URL = "https://openapi.baidu.com/oauth/2.0/device/code"
OAUTH_TOKEN_URL = "https://openapi.baidu.com/oauth/2.0/token"
FILE_SEARCH_URL = "https://pan.baidu.com/rest/2.0/xpan/file"
DISK_HOME_URL = "https://pan.baidu.com/disk/home"

# 授权范围：basic（用户信息）+ netdisk（网盘文件）
DEFAULT_SCOPE = "basic,netdisk"

# access_token 有效期 30 天；剩余不足该边际时提前刷新（留足重试余量）
REFRESH_MARGIN_SECONDS = 5 * 24 * 3600

# 关键词检索接口限制：key 最大 30 字符（UTF-8），超出会被服务端拒绝
KEY_MAX_CHARS = 30

# 检索接口的令牌失效错误码（errno -6：未登录 / 令牌无效 / 无权限）
ERRNO_TOKEN_INVALID = -6

# 令牌文件中的字段名（集中定义，避免散落字符串）
_F_CLIENT_ID = "client_id"
_F_CLIENT_SECRET = "client_secret"
_F_ACCESS_TOKEN = "access_token"
_F_REFRESH_TOKEN = "refresh_token"
_F_EXPIRES_AT = "expires_at"
_F_SCOPE = "scope"


class BaiduAuthError(Exception):
    """授权链路错误（设备码过期、被拒绝、刷新失败、未授权等）。"""

    def __init__(self, msg: str, code: str = ""):
        super().__init__(msg)
        self.msg = msg
        self.code = code


# ---------------------------------------------------------------------------
# 令牌存取（%APPDATA%\SuperSearch\baidu_netdisk.json；开发环境存仓库根）
# ---------------------------------------------------------------------------

def token_path() -> Path:
    """令牌文件位置。

    打包后（PyInstaller onefile）temp 目录易失，改存用户可写的
    %APPDATA%\\SuperSearch\\baidu_netdisk.json，与 settings.json 同目录；
    开发环境存仓库根（.gitignore 已忽略，防止误提交敏感数据）。
    测试可用环境变量 BAIDU_TOKEN_FILE 覆盖。
    """
    env = os.environ.get("BAIDU_TOKEN_FILE")
    if env:
        return Path(env)
    if getattr(sys, "frozen", False):
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        return Path(base) / "SuperSearch" / "baidu_netdisk.json"
    return Path(__file__).resolve().parent.parent / "baidu_netdisk.json"


def load_token() -> Optional[dict]:
    """读取令牌文件；不存在或损坏返回 None。"""
    p = token_path()
    try:
        if p.is_file():
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except (OSError, ValueError):
        pass
    return None


def save_token(token: dict) -> None:
    """合并写入令牌文件（空值不覆盖已有字段，避免清掉另一路保存的内容）。"""
    p = token_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    cur = load_token() or {}
    cur.update({k: v for k, v in token.items() if v not in (None, "")})
    p.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")


def save_app_credentials(client_id: str, client_secret: str) -> None:
    """保存应用凭证（client_id / client_secret），不动用户令牌字段。"""
    save_token({
        _F_CLIENT_ID: (client_id or "").strip(),
        _F_CLIENT_SECRET: (client_secret or "").strip(),
    })


def clear_token() -> None:
    """清除用户令牌（保留应用凭证，便于直接重新授权）。

    注意：必须直接写文件，不能走 save_token 的合并路径——合并写会把
    当前文件里的旧令牌字段带回来（pop 掉的键不在入参里即被保留）。
    """
    t = load_token()
    if not t:
        return
    for k in (_F_ACCESS_TOKEN, _F_REFRESH_TOKEN, _F_EXPIRES_AT, _F_SCOPE):
        t.pop(k, None)
    p = token_path()
    if t.get(_F_CLIENT_ID) or t.get(_F_CLIENT_SECRET):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(t, ensure_ascii=False, indent=2),
                     encoding="utf-8")
        return
    try:
        p.unlink()
    except OSError:
        pass


def has_authorization() -> bool:
    """是否持有用户令牌（refresh_token 或 access_token 任一存在）。"""
    t = load_token() or {}
    return bool(t.get(_F_REFRESH_TOKEN) or t.get(_F_ACCESS_TOKEN))


def has_app_credentials() -> bool:
    """是否已配置应用凭证。"""
    t = load_token() or {}
    return bool(t.get(_F_CLIENT_ID) and t.get(_F_CLIENT_SECRET))


def authorization_state() -> str:
    """授权三态：none（无应用凭证）/ configured（有凭证未授权）/ authorized。"""
    if not has_app_credentials():
        return "none"
    if not has_authorization():
        return "configured"
    return "authorized"


# ---------------------------------------------------------------------------
# 设备码授权
# ---------------------------------------------------------------------------

def request_device_code(client_id: str,
                        scope: str = DEFAULT_SCOPE) -> dict:
    """向百度申请设备码；返回含 device_code / user_code / verification_url /
    qrcode_url / expires_in / interval 的字典。失败抛 BaiduAuthError。"""
    try:
        resp = requests.post(OAUTH_DEVICE_CODE_URL, data={
            _F_CLIENT_ID: (client_id or "").strip(),
            "response_type": "device_code",
            "scope": scope,
        }, timeout=10)
        payload = resp.json()
    except ValueError as e:
        raise BaiduAuthError(f"获取设备码失败：响应不是合法 JSON（{e}）")
    except requests.RequestException as e:
        raise BaiduAuthError(f"获取设备码失败：网络错误（{e}）")
    if payload.get("error") or not payload.get("device_code"):
        desc = payload.get("error_description") or payload.get("error") or "未知错误"
        raise BaiduAuthError(f"获取设备码失败：{desc}")
    return payload


def _token_from_payload(payload: dict, client_id: str,
                        client_secret: str) -> dict:
    """把令牌端点响应归一化为令牌文件结构（expires_at 留 60s 安全边际）。"""
    expires_in = float(payload.get("expires_in") or 0)
    return {
        _F_CLIENT_ID: client_id,
        _F_CLIENT_SECRET: client_secret,
        _F_ACCESS_TOKEN: payload.get("access_token", ""),
        _F_REFRESH_TOKEN: payload.get("refresh_token", ""),
        _F_EXPIRES_AT: time.time() + expires_in - 60 if expires_in else 0.0,
        _F_SCOPE: payload.get("scope", ""),
    }


def poll_device_token(
    device_code: str,
    client_id: str,
    client_secret: str,
    interval: float = 5.0,
    timeout: float = 300.0,
    on_status: Optional[Callable[[str], None]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> Optional[dict]:
    """轮询令牌端点直至用户完成授权；成功时保存令牌并返回，取消返回 None。

    - interval：轮询间隔（百度要求 ≥5s；服务端 slow_down 时自动 +5s）；
    - timeout：设备码有效期（一般 300s），超时抛 BaiduAuthError；
    - on_status(文本)：向调用方汇报等待进度（剩余秒数等）；
    - should_stop()：返回 True 时立即放弃（用户点取消），返回 None。
    网络异常不视为失败，继续轮询至超时（授权是低频交互，抖动应容忍）。
    """
    wait = max(5.0, float(interval))
    deadline = time.monotonic() + max(0.0, timeout)
    first = True
    while True:
        if should_stop is not None and should_stop():
            return None
        if not first:
            time.sleep(wait)
        first = False
        try:
            resp = requests.post(OAUTH_TOKEN_URL, data={
                "grant_type": "device_token",
                "code": device_code,
                _F_CLIENT_ID: (client_id or "").strip(),
                _F_CLIENT_SECRET: (client_secret or "").strip(),
            }, timeout=10)
            payload = resp.json()
        except ValueError:
            payload = {}
        except requests.RequestException as e:
            if on_status:
                on_status(f"网络异常，继续等待…（{e}）")
            if time.monotonic() >= deadline:
                raise BaiduAuthError("授权等待超时（网络持续异常）")
            continue
        err = payload.get("error")
        if err == "authorization_pending":
            remaining = max(0, int(deadline - time.monotonic()))
            if on_status:
                on_status(f"等待用户在浏览器中确认…（剩余 {remaining} 秒）")
            if time.monotonic() >= deadline:
                raise BaiduAuthError("设备码已过期，请重新发起授权")
            continue
        if err == "slow_down":
            wait += 5.0
            continue
        if err == "access_denied":
            raise BaiduAuthError("用户拒绝了授权")
        if err == "expired_token":
            raise BaiduAuthError("授权码已过期，请重新发起授权")
        if err:
            desc = payload.get("error_description") or err
            raise BaiduAuthError(f"授权失败：{desc}")
        token = _token_from_payload(payload, client_id, client_secret)
        save_token(token)
        return token


# ---------------------------------------------------------------------------
# 令牌续期
# ---------------------------------------------------------------------------

_refresh_lock = threading.Lock()


def _needs_refresh(token: Optional[dict]) -> bool:
    if not token:
        return False
    if not token.get(_F_REFRESH_TOKEN):
        return False
    exp = float(token.get(_F_EXPIRES_AT) or 0)
    return exp <= 0 or (exp - time.time()) < REFRESH_MARGIN_SECONDS


def refresh_token(token: Optional[dict] = None) -> dict:
    """用 refresh_token 换新的 access_token 并保存；失败抛 BaiduAuthError。

    加锁 + 双重检查：并发检索时多个线程同时发现"即将过期"只会真正刷新一次。
    """
    with _refresh_lock:
        cur = load_token() or token or {}
        if not _needs_refresh(cur):
            return cur
        try:
            resp = requests.post(OAUTH_TOKEN_URL, data={
                "grant_type": "refresh_token",
                "refresh_token": cur.get(_F_REFRESH_TOKEN, ""),
                _F_CLIENT_ID: cur.get(_F_CLIENT_ID, ""),
                _F_CLIENT_SECRET: cur.get(_F_CLIENT_SECRET, ""),
            }, timeout=10)
            payload = resp.json()
        except ValueError as e:
            raise BaiduAuthError(f"刷新令牌失败：响应不是合法 JSON（{e}）")
        except requests.RequestException as e:
            raise BaiduAuthError(f"刷新令牌失败：网络错误（{e}）")
        if payload.get("error"):
            desc = payload.get("error_description") or payload.get("error")
            raise BaiduAuthError(f"刷新令牌失败：{desc}")
        new = dict(cur)
        new.update({
            _F_ACCESS_TOKEN: payload.get("access_token", ""),
            _F_EXPIRES_AT: (
                time.time() + float(payload.get("expires_in") or 0) - 60
                if payload.get("expires_in") else 0.0
            ),
            _F_SCOPE: payload.get("scope") or cur.get(_F_SCOPE, ""),
            # 百度刷新时可能下发新的 refresh_token；缺省保留原值
            _F_REFRESH_TOKEN: payload.get(_F_REFRESH_TOKEN)
                                  or cur.get(_F_REFRESH_TOKEN, ""),
        })
        save_token(new)
        return new


def ensure_token(token: Optional[dict] = None) -> dict:
    """返回持有有效 access_token 的令牌字典；无令牌或刷新失败抛
    BaiduAuthError（调用方应提示用户重新授权）。"""
    cur = token if token is not None else load_token()
    if not cur or not (cur.get(_F_ACCESS_TOKEN) or cur.get(_F_REFRESH_TOKEN)):
        raise BaiduAuthError("未授权百度网盘：请经 设置 → 百度网盘授权… 完成授权")
    if _needs_refresh(cur):
        cur = refresh_token(cur)
    if not cur.get(_F_ACCESS_TOKEN):
        raise BaiduAuthError("百度网盘授权已失效，请重新授权")
    return cur


# ---------------------------------------------------------------------------
# 关键词检索
# ---------------------------------------------------------------------------

def _search_once(key: str, dir: str, category: Optional[int],
                 recursion: int, num: int,
                 access_token: str) -> Tuple[List[dict], Optional[Tuple[int, str]]]:
    """执行一次检索；返回 (items, None) 或 ([], (errno, errmsg))。"""
    params: Dict[str, object] = {
        "method": "search",
        "access_token": access_token,
        "key": key,
        "dir": dir or "/",
        "recursion": recursion,
        "num": num,
    }
    if category:
        params["category"] = category
    resp = requests.get(FILE_SEARCH_URL, params=params, timeout=10)
    payload = resp.json()
    errno = payload.get("errno")
    if errno:
        errmsg = payload.get("errmsg") or payload.get("showMsg") or "未知错误"
        return [], (int(errno), str(errmsg))
    items = payload.get("list") or []
    return [it for it in items if isinstance(it, dict)], None


def search_files(
    key: str,
    dir: str = "/",
    category: Optional[int] = None,
    recursion: int = 1,
    num: int = 500,
    token: Optional[dict] = None,
) -> Tuple[List[dict], Optional[str]]:
    """关键词检索网盘文件，返回 (原始条目列表, 错误信息或 None)。

    - key：文件名关键词（自动截断到 30 字符上限）；
    - dir：检索目录，默认全盘；
    - category：文件类型 1-7（视频/音频/图片/文档/应用/其他/种子），None 不限；
    - recursion：1 递归子目录；
    - num：单次请求条数上限（服务端上限 500）；
    - token：显式传入令牌（测试用）；缺省读本机令牌文件。
    令牌失效（errno -6）时自动刷新并重试一次；仍失败返回可读错误。
    """
    key = (key or "").strip()[:KEY_MAX_CHARS]
    if not key:
        return [], "检索词为空"
    try:
        tok = ensure_token(token)
    except BaiduAuthError as e:
        return [], e.msg
    access_token = tok.get(_F_ACCESS_TOKEN, "")
    try:
        items, err = _search_once(key, dir, category, recursion, num, access_token)
    except requests.RequestException as e:
        return [], f"连接百度网盘失败：{e}"
    except ValueError:
        return [], "百度网盘返回了无法解析的响应"
    if err is None:
        return items, None
    errno, errmsg = err
    if errno == ERRNO_TOKEN_INVALID:
        # 令牌失效：刷新一次再重试；仍失败则引导重新授权
        try:
            tok = refresh_token(tok)
            items, err = _search_once(key, dir, category, recursion, num,
                                      tok.get(_F_ACCESS_TOKEN, ""))
        except (BaiduAuthError, requests.RequestException, ValueError):
            return [], "百度网盘授权已失效，请重新授权（设置 → 百度网盘授权…）"
        if err is None:
            return items, None
        return [], "百度网盘授权已失效，请重新授权（设置 → 百度网盘授权…）"
    return [], f"百度网盘检索失败（错误码 {errno}：{errmsg}）"


def disk_home_url(path: str) -> str:
    """网页版网盘"定位到目录"地址（结果目录定位，见 CONTEXT.md）。

    打开 https://pan.baidu.com/disk/home#/all?path=<URL编码路径>；
    用户在已登录浏览器中查看，不产生分享链接、不改变文件分享状态。
    """
    return f"{DISK_HOME_URL}#/all?path={quote(path or '/', safe='')}"
