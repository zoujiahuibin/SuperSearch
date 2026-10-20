"""内置 Everything 便携版管理：让安装包"开箱即用"，无需用户单独安装 Everything。

原理：
  安装包把 Everything 便携版（everything.exe，来自 voidtools 官方 zip，未修改）放到
  {app}\\Everything\\。应用启动/检索时探测 Everything HTTP 是否可达：
    - 可达（用户本机已装并开启了 HTTP 服务）→ 直接使用；
    - 不可达 → 选一个空闲端口（先 80，被占则 8180），在
      %LOCALAPPDATA%\\SuperSearch\\Everything\\ 下生成 Everything 配置
      （http_server=1），以 `-instance SuperSearch -config <ini>` 启动内置便携版，
      并等待其 HTTP 服务就绪后使用该端口。

Everything 命令行（1.4 官方支持）：
  -instance <name>  使用独立实例（独立窗口类/互斥体，避免与用户已装的 Everything 冲突）
  -config <path>    使用指定 ini（数据库等数据文件默认写在该 ini 同目录，普通用户可写）
  -startup          后台运行，不显示任何搜索窗口（voidtools 官方选项）；配合 ini 的
                    show_tray_icon=0 可做到完全无界面，HTTP 服务由 ini 的 http_server=1 启用。

NTFS 索引需要管理员权限或 Everything 服务。为避免每次启动都弹
“需要管理员权限索引 NTFS 卷”对话框、也不触发 UAC，做法是：
  1) 安装一次 Everything 服务（`everything.exe -install-service`，需管理员；安装器在
     提权环境下静默完成，见 SuperSearch.iss）。服务以 SYSTEM 后台运行、无界面，负责
     读取 NTFS MFT/USN。
  2) 应用以标准用户 + `run_as_admin=0` 启动 `-startup` 客户端：经服务取得 NTFS 索引，
     既不弹管理员对话框，也不显示窗口。
服务缺失时（如便携/免安装运行）由 ensure_everything_running 尝试补装一次
（标准用户会触发一次性 UAC，装好后即永久生效）。
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from dataclasses import replace
from typing import Optional, Tuple

import requests

from core.config import EverythingConfig

# 内置 Everything 相对 {app} 的目录与文件名（官方 zip 解压后的原始文件名）
BUNDLED_DIR_NAME = "Everything"
BUNDLED_EXE_NAME = "everything.exe"

# 独立实例名：避免与用户本机已安装的 Everything（默认实例）互斥/抢配置
INSTANCE_NAME = "SuperSearch"

# 内置实例候选端口：优先 80（与程序默认一致），被占用（如 IIS/http.sys 保留）则退 8180
BUNDLED_PORTS = (80, 8180)

# 启动后等待 HTTP 就绪的默认时长（首次启动需建索引，HTTP 服务通常数秒内可用）
DEFAULT_WAIT_SECONDS = 20.0

# 防止重复启动内置实例（模块级守卫；Everything 对同一 instance 本身也有互斥，双保险）
_launch_lock = threading.Lock()
_launched = False

# 同一进程内是否已尝试过安装 Everything 服务（避免一次启动内重复触发 UAC）
_service_install_attempted = False


def appdata_dir() -> str:
    """内置 Everything 配置/数据目录（普通用户可写，避免写入 Program Files 失败）。"""
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "SuperSearch", "Everything")


def bundled_everything_exe() -> Optional[str]:
    """返回内置 everything.exe 的路径；不存在返回 None。

    查找顺序（按打包形态自适应）：
      1. 打包后（PyInstaller onefile / onedir）：内置 Everything 作为 data 文件随包
         解压，运行时位于 sys._MEIPASS/Everything/everything.exe（onefile 解到临时
         目录，onedir 即 {app}/Everything）。这是最可靠的定位方式。
      2. 兜底：sys.executable 同级 Everything/ 目录（onedir 或用户手动放置）。
      3. 开发环境：仓库 vendor/Everything/everything.exe（便于本地联调）。

    注意：onefile 下 sys.executable 指向临时目录里的副本，不能用它定位安装目录。
    """
    candidates = []
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidates.append(os.path.join(meipass, BUNDLED_DIR_NAME, BUNDLED_EXE_NAME))
        candidates.append(
            os.path.join(os.path.dirname(os.path.abspath(sys.executable)),
                         BUNDLED_DIR_NAME, BUNDLED_EXE_NAME))
    # 开发环境：core/ 的上一级是仓库根目录
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    candidates.append(os.path.join(repo, "vendor", BUNDLED_DIR_NAME, BUNDLED_EXE_NAME))
    for c in candidates:
        try:
            if os.path.isfile(c):
                return c
        except OSError:
            continue
    return None


def _stable_exe_path() -> str:
    """内置 Everything 的稳定落盘路径（%LOCALAPPDATA%\\SuperSearch\\Everything）。

    onefile 模式下 PyInstaller 会把内置 exe 解到临时目录并在退出时清理，若直接
    从该临时路径拉起 Everything，SuperSearch 关闭后临时目录被删，运行中的
    Everything 进程会被拽入“文件被占用”的尴尬状态。因此先把它复制到用户可写的
    稳定目录再启动。
    """
    return os.path.join(appdata_dir(), BUNDLED_EXE_NAME)


def _ensure_stable_exe(bundled: str) -> str:
    """若内置 exe 不在稳定目录，则复制过去；返回实际使用（启动）的 exe 路径。"""
    stable = _stable_exe_path()
    if os.path.abspath(bundled) == os.path.abspath(stable):
        return stable
    try:
        os.makedirs(appdata_dir(), exist_ok=True)
        shutil.copyfile(bundled, stable)
        return stable
    except OSError:
        # 复制失败（极端权限问题）→ 退而求其次就地启动
        return bundled


def _port_free(port: int, host: str = "127.0.0.1") -> bool:
    """探测本机端口是否可绑定（即没有其它进程/保留占用）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def pick_bundled_port(preferred: int = 80) -> Optional[int]:
    """为内置实例挑一个可用端口：优先 preferred，再依次尝试 BUNDLED_PORTS；全占用返回 None。"""
    for p in (preferred, *BUNDLED_PORTS):
        if _port_free(p):
            return p
    return None


def write_bundled_ini(port: int, base_dir: Optional[str] = None) -> str:
    """生成内置实例的 Everything 配置（启用 HTTP 服务），返回 ini 路径。

    只写必要键：Everything 读取时对缺失键用默认值；未知键会被忽略（无害）。
    数据（数据库等）默认写在 ini 同目录（%LOCALAPPDATA%，普通用户可写）。

    以后台/标准用户方式运行（配合 -startup 与 Everything 服务）：
      run_as_admin=0            不自动提权重启 → 不触发 UAC / “以管理员身份运行”
      run_in_background=1       关闭搜索窗口后仍驻留后台（持续提供 HTTP 服务）
      show_tray_icon=0          不显示通知区域托盘图标（配合 -startup 完全无界面）
      auto_include_fixed_volumes=1  自动纳入固定 NTFS 卷（经服务读取 MFT/USN）
      check_for_updates_on_startup=0  关闭启动更新检查，避免额外弹窗
    """
    d = base_dir or appdata_dir()
    os.makedirs(d, exist_ok=True)
    ini = os.path.join(d, f"Everything-{INSTANCE_NAME}.ini")
    content = (
        "// SuperSearch built-in Everything instance config\n"
        f"instance={INSTANCE_NAME}\n"
        "http_server=1\n"
        f"http_server_port={port}\n"
        "http_server_username=\n"
        "http_server_password=\n"
        f"db_path={os.path.join(d, f'Everything-{INSTANCE_NAME}.db')}\n"
        "run_as_admin=0\n"
        "run_in_background=1\n"
        "show_tray_icon=0\n"
        "auto_include_fixed_volumes=1\n"
        "check_for_updates_on_startup=0\n"
    )
    with open(ini, "w", encoding="utf-8") as f:
        f.write(content)
    return ini


def launch_bundled(port: int, exe_path: Optional[str] = None,
                   base_dir: Optional[str] = None) -> Tuple[Optional[subprocess.Popen], Optional[str]]:
    """启动内置 Everything 便携版（独立实例，后台无界面）。

    返回 (Popen 或 None, 错误信息或 None)。

    以 `-startup` 方式启动：Everything 在后台运行、不显示任何搜索窗口
    （voidtools 官方选项：“Run Everything in the background without showing any
    search windows”）。配合 ini 的 show_tray_icon=0，做到完全无界面；HTTP 服务
    由 ini 的 http_server=1 启用，与窗口模式无关。进程仍保有消息循环，HTTP 服务
    线程照常初始化（此前用 STARTUPINFO 最小化窗口也能起 HTTP，可证明窗口消息循环
    并非 HTTP 的前提；-startup 只是不再显示那个窗口而已）。
    """
    global _launched
    exe = exe_path or bundled_everything_exe()
    if not exe:
        return None, "未找到内置 everything.exe"
    ini = write_bundled_ini(port, base_dir)
    try:
        proc = subprocess.Popen(
            [exe, "-instance", INSTANCE_NAME, "-config", ini, "-startup"],
            creationflags=0, close_fds=True,
        )
        with _launch_lock:
            _launched = True
        return proc, None
    except OSError as e:
        return None, f"启动内置 Everything 失败：{e}"


def _reachable(cfg: EverythingConfig, timeout: float = 1.0) -> bool:
    """快速探测 Everything HTTP 是否就绪（绕过系统代理，与 local_search 一致）。"""
    try:
        requests.get(f"{cfg.base_url}/", params={"json": 1},
                     timeout=timeout, proxies={"http": None})
        return True
    except requests.RequestException:
        return False


def is_everything_reachable(cfg: EverythingConfig) -> bool:
    return _reachable(cfg, timeout=1.5)


def wait_reachable(cfg: EverythingConfig, wait_seconds: float) -> bool:
    """在 wait_seconds 内轮询等待 HTTP 就绪。"""
    deadline = time.monotonic() + max(0.0, wait_seconds)
    while True:
        if _reachable(cfg, timeout=1.0):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.5)


def ensure_everything_running(
    cfg: EverythingConfig,
    wait_seconds: float = DEFAULT_WAIT_SECONDS,
    base_dir: Optional[str] = None,
    exe_path: Optional[str] = None,
    retry_on_fail: bool = True,
) -> Tuple[EverythingConfig, Optional[str], bool]:
    """确保 Everything HTTP 可用；必要时自动启动内置便携版。

    返回 (可能改端口的 cfg, 提示/错误消息 or None, 是否可继续本地检索)。
      - 已可达（用户自有 Everything 或内置已就绪）→ (cfg, None, True)
      - 内置启动并就绪 → (改端口后的 cfg, None, True)
      - 内置已启动但未就绪（首次建索引中）→ (cfg', 提示, False)
      - 无内置、不可达 → (cfg, 错误, False)
    """
    global _launched
    if _reachable(cfg, timeout=1.5):
        return cfg, None, True

    exe = exe_path or bundled_everything_exe()
    if not exe:
        return cfg, (
            "本地检索不可用：未检测到正在运行的 Everything HTTP 服务，"
            "且程序目录中也没有内置 Everything。"
        ), False

    # 确保 Everything 服务已安装：有了服务，标准用户客户端即可经服务索引 NTFS，
    # 不再弹“需要管理员权限索引 NTFS”对话框，也不触发 UAC。安装器已在提权环境
    # 装好；若缺失（便携/免安装运行），这里尝试补装一次（标准用户会触发一次性
    # UAC，装好后即永久生效；同一进程内最多尝试一次）。
    global _service_install_attempted
    if os.name == "nt" and not _service_install_attempted and not service_installed():
        _service_install_attempted = True
        install_service(exe)

    # 同一进程内已启动过 → 只等待，不再重复拉起
    with _launch_lock:
        already = _launched

    port = pick_bundled_port(cfg.port)
    if port is None:
        return cfg, (
            f"本地检索不可用：无法为内置 Everything 找到可用端口"
            f"（{', '.join(str(p) for p in (cfg.port, *BUNDLED_PORTS))} 均被占用）。"
        ), False

    new_cfg = replace(cfg, port=port)

    # 已启动（如应用启动时已拉起）：只等待 HTTP 就绪
    if already:
        ok = wait_reachable(new_cfg, wait_seconds)
        if ok:
            return new_cfg, None, True
        # 80 端口超时未就绪，尝试换 8180 重启一次（先退出旧实例）
        if retry_on_fail and port == 80:
            quit_our_instance(exe)
            _launched = False
            return ensure_everything_running(
                replace(new_cfg, port=8180), wait_seconds, base_dir, exe_path)
        return new_cfg, "内置 Everything 正在建立索引，请稍后重试本地检索。", False

    # 复制到稳定目录（避免 onefile 临时目录被清理），再从稳定目录启动
    run_exe = _ensure_stable_exe(exe)
    proc, err = launch_bundled(port, exe_path=run_exe, base_dir=base_dir)
    if proc is None and run_exe != exe:
        # 稳定目录副本启动失败，回退到随包内置 exe 就地启动
        proc, err = launch_bundled(port, exe_path=exe, base_dir=base_dir)
    if proc is None:
        return new_cfg, (
            f"本地检索不可用：内置 Everything 启动失败（{err or '未知错误'}）。"
            "请确认程序有写入权限，或手动安装 Everything 后重试。"
        ), False
    ok = wait_reachable(new_cfg, wait_seconds)
    if ok:
        return new_cfg, None, True
    # 首次启动 HTTP 未就绪：若用的是 80，换 8180 重启一次
    if retry_on_fail and port == 80:
        quit_our_instance(exe)
        _launched = False
        return ensure_everything_running(
            replace(new_cfg, port=8180), wait_seconds, base_dir, exe_path)
    return new_cfg, (
        "内置 Everything 已启动，但 HTTP 服务暂未就绪（首次启动需要建立索引，"
        "可能需要几十秒），请稍后重新检索。"
    ), False


def quit_our_instance(exe_path: Optional[str] = None) -> None:
    """退出内置 Everything 的 SuperSearch 独立实例（best-effort，用于端口切换时重启）。"""
    exe = exe_path or bundled_everything_exe()
    if not exe:
        return
    try:
        subprocess.run(
            [exe, "-instance", INSTANCE_NAME, "-quit"], timeout=10,
            creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0),
        )
    except Exception:  # noqa: BLE001
        pass


def service_installed() -> bool:
    """检测 Windows 是否已安装 Everything 服务（只读，无需管理员）。

    用 `sc query Everything` 探测服务是否存在：returncode==0 表示服务已安装
    （无论是否正在运行），非 0（如 1060 不存在）表示未安装。sc 不可用或查询
    异常时返回 False（此时由调用方决定是否补装；安装本身是幂等的）。
    """
    if os.name != "nt":
        return False
    try:
        out = subprocess.run(
            ["sc", "query", "Everything"],
            capture_output=True, text=True, timeout=5,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        return out.returncode == 0
    except Exception:  # noqa: BLE001
        return False


def install_service(exe_path: Optional[str] = None,
                    wait_seconds: int = 20) -> Tuple[bool, Optional[str]]:
    """安装并启动 Everything Windows 服务（需要管理员权限）。

    装了服务后，标准用户的 everything.exe 客户端即可经服务读取 NTFS MFT/USN，
    不再弹“需要管理员权限索引 NTFS”对话框，也不触发 UAC。服务以 SYSTEM 后台
    运行、无界面。

    - 安装器（已提权）调用：直接安装，无额外提示。
    - 应用运行时（标准用户）调用：everything.exe 会自动以管理员身份重启一次
      （一次性 UAC），完成后即永久生效。

    服务会把启动路径记录下来，因此必须从**持久化副本**（%LOCALAPPDATA% 稳定目录）
    执行安装——不能从 PyInstaller onefile 的 sys._MEIPASS 临时目录（退出即被清理）。

    返回 (是否成功, 错误信息或 None)。
    """
    exe = exe_path or bundled_everything_exe()
    if not exe:
        return False, "未找到内置 everything.exe"
    run_exe = _ensure_stable_exe(exe)  # 服务需指向持久化路径
    try:
        subprocess.run(
            [run_exe, "-install-service"],
            timeout=wait_seconds,
            creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0),
        )
        return True, None
    except Exception as e:  # noqa: BLE001
        return False, f"安装 Everything 服务失败：{e}"


def uninstall_service(exe_path: Optional[str] = None) -> None:
    """卸载 Everything 服务（best-effort，供卸载清理调用；需要管理员权限）。

    `-uninstall-service` 按服务名经 SCM 移除服务，与用哪个 everything.exe 副本
    执行无关，因此即便稳定目录已被清理，用随包内置副本执行也能卸载。
    """
    exe = exe_path or bundled_everything_exe()
    if not exe:
        return
    try:
        subprocess.run(
            [exe, "-uninstall-service"], timeout=20,
            creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0),
        )
    except Exception:  # noqa: BLE001
        pass
