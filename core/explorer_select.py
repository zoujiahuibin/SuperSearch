"""在资源管理器中打开所在文件夹并选中指定文件。

【打开路径】对本地文件结果的诉求：不只是打开父目录，还要让资源管理器选中该文件
（类似 Everything/Listary 的 "Open Path"）。实现优先调用 Win32 Shell API
``SHOpenFolderAndSelectItems``——资源管理器"在文件夹中显示"的原生语义，
对不存在文件、含空格/逗号/中文的路径都稳；失败时逐级静默降级：
``explorer /select,<path>`` → 仅打开父目录（与历史行为一致）。

设计约定（Q2=A）：
  - 文件夹结果：直接打开该文件夹本身（无"选中"语义）；
  - 文件结果且文件存在：打开父目录并选中该文件；
  - 文件已不存在（陈旧索引命中已删文件）：无法选中，仅打开父目录；
  - 父目录也不存在（如网络盘掉线）：不做任何动作——避免 ``explorer /select``
    对不存在目标"退回打开默认'文档'库"的经典行为；
  - 所有降级均静默完成，不弹窗、不报错。
"""
from __future__ import annotations

import ctypes
import os
import subprocess

# Win32 常量
_S_OK = 0
_COINIT_APARTMENTTHREADED = 0x2
_COINIT_DISABLE_OLE1DDE = 0x4


def _open_folder_only(folder: str) -> bool:
    """仅打开文件夹（不选中任何项）。返回是否成功打开。"""
    if not folder or not os.path.isdir(folder):
        return False
    try:
        if os.name == "nt":
            os.startfile(folder)
        else:
            # 非 Windows：退回 Qt 的通用打开目录能力
            from PySide6.QtCore import QUrl
            from PySide6.QtGui import QDesktopServices

            QDesktopServices.openUrl(QUrl.fromLocalFile(folder))
        return True
    except Exception:  # noqa: BLE001
        return False


def _co_initialize() -> None:
    """确保调用线程已初始化 COM（STA）。

    与 Qt 在 GUI 线程上的做法一致：只初始化、不配对 CoUninitialize——
    GUI 线程的公寓存活于整个进程生命周期；S_FALSE（已初始化）与
    RPC_E_CHANGED_MODE（已以别的人格初始化）均视为可用。
    """
    ole32 = ctypes.windll.ole32  # type: ignore[attr-defined]
    ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    ole32.CoInitializeEx.restype = ctypes.c_long
    ole32.CoInitializeEx(
        None, _COINIT_APARTMENTTHREADED | _COINIT_DISABLE_OLE1DDE
    )


def _sh_parse_display_name(path: str) -> ctypes.c_void_p:
    """把路径解析为 PIDL；失败抛 OSError。调用方负责 CoTaskMemFree。"""
    shell32 = ctypes.windll.shell32  # type: ignore[attr-defined]
    shell32.SHParseDisplayName.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_uint,
        ctypes.POINTER(ctypes.c_ulong),
    ]
    shell32.SHParseDisplayName.restype = ctypes.c_long
    pidl = ctypes.c_void_p()
    hr = shell32.SHParseDisplayName(path, None, ctypes.byref(pidl), 0, None)
    if hr != _S_OK or not pidl.value:
        raise OSError(f"SHParseDisplayName 失败：{path}（hr={hr}）")
    return pidl


def _co_task_mem_free(pidl: ctypes.c_void_p) -> None:
    """释放 SHParseDisplayName 产出的 PIDL（失败静默）。"""
    try:
        ole32 = ctypes.windll.ole32  # type: ignore[attr-defined]
        ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
        ole32.CoTaskMemFree.restype = None
        ole32.CoTaskMemFree(pidl)
    except Exception:  # noqa: BLE001
        pass


def _call_sh_open_and_select(
    pidl_folder: ctypes.c_void_p, pidl_item: ctypes.c_void_p
) -> None:
    """调 SHOpenFolderAndSelectItems 打开 pidl_folder 并选中 pidl_item。失败抛 OSError。"""
    shell32 = ctypes.windll.shell32  # type: ignore[attr-defined]
    shell32.SHOpenFolderAndSelectItems.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_ulong,
    ]
    shell32.SHOpenFolderAndSelectItems.restype = ctypes.c_long
    items = (ctypes.c_void_p * 1)(pidl_item)
    hr = shell32.SHOpenFolderAndSelectItems(
        pidl_folder, ctypes.c_uint(1), items, 0
    )
    if hr != _S_OK:
        raise OSError(f"SHOpenFolderAndSelectItems 失败（hr={hr}）")


def _sh_open_and_select(file_path: str) -> None:
    """Win32 主路径：打开 file_path 的父目录并选中 file_path。失败抛异常。"""
    _co_initialize()
    pidl_folder = _sh_parse_display_name(os.path.dirname(file_path))
    try:
        pidl_item = _sh_parse_display_name(file_path)
        try:
            _call_sh_open_and_select(pidl_folder, pidl_item)
        finally:
            _co_task_mem_free(pidl_item)
    finally:
        _co_task_mem_free(pidl_folder)


def _explorer_select(file_path: str) -> None:
    """降级路径：``explorer /select,"<path>"``。启动失败抛异常。"""
    subprocess.Popen(f'explorer /select,"{file_path}"')


def open_folder_and_select(path: str) -> bool:
    """打开所在文件夹，并尽可能选中目标。

    返回是否至少完成了"打开文件夹"这一动作；静默降级，不弹窗。
    """
    if not path:
        return False
    if os.path.isdir(path):
        # 文件夹结果：打开其本身（无选中语义）
        return _open_folder_only(path)
    folder = os.path.dirname(path) or path
    if not os.path.isdir(folder):
        # 父目录也不存在（网络盘掉线等）：不动资源管理器，
        # 否则 explorer /select 对不存在目标会退回打开默认"文档"库
        return False
    if os.path.isfile(path) and os.name == "nt":
        try:
            _sh_open_and_select(path)
            return True
        except Exception:  # noqa: BLE001
            pass
        try:
            _explorer_select(path)
            return True
        except Exception:  # noqa: BLE001
            pass
    # 文件不存在（陈旧索引）或选中失败：仅打开所在文件夹
    return _open_folder_only(folder)
