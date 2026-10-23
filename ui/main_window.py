"""主窗口 UI：合并展示本地（Everything）与 ima 知识库检索结果。

布局：
  - 顶部：搜索框 + 搜索按钮 + 来源勾选（本地 / ima）；勾选「本地」后展开盘符勾选区（C盘 / E盘…，自动探测）；勾选「ima 知识库」后展开知识库类型勾选区（个人 / 共享 / 订阅）
  - 中部：结果表格（来源标签 / 标题 / 位置 / 大小 / 修改时间）
  - 底部：来源筛选下拉 + 状态栏 + 选中详情（ima 高亮摘要）
检索在后台线程进行，避免界面卡顿。
"""
from __future__ import annotations

import os
import sys
from datetime import datetime
from typing import List, Optional

from PySide6.QtCore import QThread, Signal, QFileInfo, QSize, Qt, QDir
from PySide6.QtGui import QDesktopServices, QColor, QAction, QIcon, QPixmap
from PySide6.QtCore import QUrl, QUrlQuery
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileIconProvider,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QFormLayout,
    QDoubleSpinBox,
    QSpinBox,
    QMenu,
    QMenuBar,
    QMessageBox,
    QPushButton,
    QProgressBar,
    QStatusBar,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
    QSystemTrayIcon,
)

from core.config import (
    EverythingConfig,
    ImaConfig,
    load_everything_config,
    load_ima_config,
    load_ima_kb_ids,
    save_ima_credentials,
    Settings,
    load_settings,
    save_settings,
)
from core import format_sort
from core import ev_bundler
from core.models import SearchResult
from search import local_search, ima_search


# 来源标签配色
COLOR_LOCAL = QColor("#1565C0")  # 蓝
COLOR_IMA = QColor("#2E7D32")    # 绿

def _detect_drives() -> List[str]:
    """检测本机存在的盘符字母（Windows）。非 Windows 返回 ["C"] 兜底。"""
    import string
    drives = []
    if os.name == "nt":
        for letter in string.ascii_uppercase:
            # 用 os.path.exists 探测盘符是否挂载（含可移动/网络盘）
            if os.path.exists(f"{letter}:\\"):
                drives.append(letter)
    else:
        drives = ["C"]
    return drives


# 排序选项：标签 -> (Everything sort 字段, 是否升序)
# 字段取值须与 Everything HTTP 的 sort 参数一致：name/path/date_modified/size
SORT_OPTIONS = [
    ("名称 ↑", "name", True),
    ("名称 ↓", "name", False),
    ("路径 ↑", "path", True),
    ("路径 ↓", "path", False),
    ("大小 ↑", "size", True),
    ("大小 ↓", "size", False),
    ("修改时间 ↑", "date_modified", True),
    ("修改时间 ↓", "date_modified", False),
]


def _fmt_size(n: int | None) -> str:
    if n is None:
        return ""
    units = ["B", "KB", "MB", "GB", "TB"]
    f = float(n)
    i = 0
    while f >= 1024 and i < len(units) - 1:
        f /= 1024.0
        i += 1
    return f"{f:.1f} {units[i]}"


def _fmt_dt(dt) -> str:
    if dt is None:
        return ""
    if isinstance(dt, datetime):
        return dt.strftime("%Y-%m-%d %H:%M")
    return str(dt)


def _app_icon_path() -> Optional[str]:
    """定位应用图标（.ico）：打包后从 PyInstaller 解压目录(_MEIPASS)取，开发环境从仓库 assets 取。"""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        p = os.path.join(meipass, "SuperSearch.ico")
        if os.path.isfile(p):
            return p
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    p = os.path.join(repo, "assets", "SuperSearch.ico")
    return p if os.path.isfile(p) else None


class SearchWorker(QThread):
    result_ready = Signal(list)   # List[SearchResult]
    error_ready = Signal(str)     # 错误信息
    progress = Signal(str)        # 状态提示（文本）
    progress_pct = Signal(int)    # 检索进度（0-100，百分比）

    def __init__(self, query: str, ev_cfg: EverythingConfig, ima_cfg: ImaConfig,
                 kb_ids, do_local: bool, do_ima: bool, ima_max_workers: int = 4,
                 ima_req_per_sec: float = 4.0, ima_burst: int = 2,
                 local_drives: List[str] = None, ima_kb_types: List[str] = None):
        super().__init__()
        self.query = query
        self.ev_cfg = ev_cfg
        self.ima_cfg = ima_cfg
        self.kb_ids = kb_ids
        self.do_local = do_local
        self.do_ima = do_ima
        # 本地检索限定的盘符（None/空 = 检索全部盘符）
        self.local_drives = local_drives
        # ima 检索限定的知识库类型（None = 检索全部类型）
        self.ima_kb_types = ima_kb_types
        # ima 并发/限速配置（来自用户设置，可在「设置 → ima 提速」中调整）
        self.ima_max_workers = ima_max_workers
        self.ima_req_per_sec = ima_req_per_sec
        self.ima_burst = ima_burst

    def run(self):
        results: List[SearchResult] = []
        has_local = self.do_local
        has_ima = self.do_ima

        # 阶段权重：本地检索（快）占前 20%，ima 跨库检索（慢）占后 80%，
        # 以便状态栏百分比能平滑反映整体进度。
        if has_local:
            self.progress.emit("正在检索本地文件（Everything）…")
            self.progress_pct.emit(0)
            # 确保 Everything HTTP 可用；不可达时自动启动内置便携版（等待就绪，最多 30s）
            self.progress.emit("正在检查本地检索服务（Everything）…")
            ev_cfg, ev_note, ev_ok = ev_bundler.ensure_everything_running(
                self.ev_cfg, wait_seconds=30,
                # HTTP 就绪后继续等待索引可查询：冷启动重建索引实测约 80s，
                # 期间 HTTP 虽通但直接检索会静默返回空结果
                index_wait_seconds=120,
                progress_cb=self.progress.emit)
            if ev_note:
                self.progress.emit(ev_note)
            if ev_ok:
                try:
                    results.extend(local_search.search_local(
                        self.query, ev_cfg, drives=self.local_drives))
                except Exception as e:  # noqa: BLE001
                    self.error_ready.emit(f"本地检索失败：{e}")
            else:
                self.error_ready.emit(ev_note or "本地检索不可用")
            if has_ima:
                self.progress_pct.emit(20)
            else:
                self.progress_pct.emit(100)

        if has_ima:
            self.progress.emit("正在检索 ima 知识库…")

            def _on_ima_progress(done: int, total: int, result_count: int):
                if total <= 0:
                    return
                frac = done / total
                # 本地占 20%，ima 占剩余 80%
                pct = (20 + frac * 80) if has_local else (frac * 100)
                self.progress_pct.emit(int(round(pct)))
                # 额外给出可读进度：已检索 N/Y 个库，已得 M 条结果
                self.progress.emit(
                    f"正在检索 ima 知识库… 已检索 {done}/{total} 个库，已得 {result_count} 条结果")

            try:
                ima_res, ima_errs = ima_search.search_ima(
                    self.query, self.ima_cfg, self.kb_ids,
                    progress_callback=_on_ima_progress,
                    req_per_sec=self.ima_req_per_sec,
                    burst=self.ima_burst,
                    max_workers=self.ima_max_workers,
                    kb_types=self.ima_kb_types,
                )
                results.extend(ima_res)
                for e in ima_errs:
                    self.error_ready.emit(e)
            except Exception as e:  # noqa: BLE001
                self.error_ready.emit(f"ima 检索失败：{e}")
            self.progress_pct.emit(100)

        self.result_ready.emit(results)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.ev_cfg = load_everything_config()
        self.ima_cfg = load_ima_config()
        self.kb_ids = load_ima_kb_ids()
        self.settings = load_settings()  # 文件格式优先级设置
        self.all_results: List[SearchResult] = []
        self._cols_initialized = False  # 列宽是否已完成首次按内容自适应
        self.icon_provider = QFileIconProvider()  # 系统 shell 图标（与资源管理器/Everything 同源）
        self._kb_numeric_cache: dict = {}  # ima kb_id(base64) -> 数字 knowledgeBaseId 缓存
        self._kb_numeric_failed: set = set()  # 已知解析不出数字 ID 的 kb_id（负面缓存）
        self._ima_client_cache = None  # ima 电脑端是否可用（None=未探测）
        self._ext_icon_cache: dict = {}  # 扩展名 -> 系统图标缓存（ima 结果按类型显示）
        self._init_ui()
        # 提前拉起本地检索服务：应用启动即启动内置 Everything 便携版（其配置已启用
        # HTTP 服务），不阻塞界面；真正的 HTTP 就绪等待放到首次检索时再处理。
        # retry_on_fail=False：启动阶段只负责拉起，不在此触发 8180 端口重启。
        self.ev_cfg, _ev_note, _ev_ok = ev_bundler.ensure_everything_running(
            self.ev_cfg, wait_seconds=0.5, retry_on_fail=False)
        if _ev_note:
            self.status.showMessage(_ev_note)
        self._check_backends()
        # 系统托盘：关闭窗口时最小化到托盘而非退出；托盘菜单可「显示主窗口 / 退出」
        self._force_quit = False
        self._setup_tray()

    def _icon_for(self, r: SearchResult):
        """返回与 Everything 一致的系统图标。

        - 本地文件夹：文件夹图标
        - 本地文件：按扩展名取系统关联图标（路径不存在时回退通用文件图标）
        - ima 文档：按标题扩展名取系统图标（与本地同名文件一致，如 PDF 显示 PDF 图标）；
          无扩展名（如知识库条目）回退通用文档图标
        """
        if r.source == "local":
            if r.kind == "folder":
                return self.icon_provider.icon(QFileIconProvider.IconType.Folder)
            info = QFileInfo(r.path)
            ic = self.icon_provider.icon(info)
            if ic.isNull():
                ic = self.icon_provider.icon(QFileIconProvider.IconType.File)
            return ic
        # ima 结果：
        #  - 文件夹 -> 文件夹图标（与本地一致）
        #  - 文档 -> 按标题扩展名取系统图标；无扩展名回退通用文档图标
        if r.kind == "folder":
            return self.icon_provider.icon(QFileIconProvider.IconType.Folder)
        ext = QFileInfo(r.title).suffix().lower()
        if ext:
            ic = self._ext_icon(ext)
            if not ic.isNull():
                return ic
        return self.icon_provider.icon(QFileIconProvider.IconType.File)

    def _ext_icon(self, ext: str):
        """按扩展名取系统关联图标（与资源管理器中同扩展名文件一致），按扩展名缓存。

        QFileIconProvider 对不存在的路径会走 SHGFI_USEFILEATTRIBUTES，
        仅凭扩展名即可拿到关联图标，因此用临时目录下的虚拟探测路径即可。
        """
        ic = self._ext_icon_cache.get(ext)
        if ic is None:
            info = QFileInfo(os.path.join(QDir.tempPath(), f"icon_probe.{ext}"))
            ic = self.icon_provider.icon(info)
            if ic.isNull():
                ic = self.icon_provider.icon(QFileIconProvider.IconType.File)
            self._ext_icon_cache[ext] = ic
        return ic

    def _init_ui(self):
        self.setWindowTitle("SuperSearch")
        self.resize(1100, 720)

        # 菜单栏：设置
        mb = self.menuBar()
        settings_menu = mb.addMenu("设置")
        act_format = QAction("文件格式排序…", self)
        act_format.triggered.connect(self.open_settings)
        settings_menu.addAction(act_format)
        act_ima = QAction("ima 凭证…", self)
        act_ima.triggered.connect(self.open_ima_creds)
        settings_menu.addAction(act_ima)
        act_speed = QAction("ima 提速（并发）…", self)
        act_speed.triggered.connect(self.open_concurrency)
        settings_menu.addAction(act_speed)

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        # 搜索栏
        top = QHBoxLayout()
        self.input = QLineEdit()
        self.input.setPlaceholderText("输入关键词，回车检索（同时搜本地与 ima）")
        self.input.returnPressed.connect(self.on_search)
        self.btn_search = QPushButton("检索")
        self.btn_search.clicked.connect(self.on_search)
        self.chk_local = QCheckBox("本地")
        self.chk_local.setChecked(True)
        self.chk_ima = QCheckBox("ima 知识库")
        self.chk_ima.setChecked(True)
        top.addWidget(self.input, 3)
        top.addWidget(self.btn_search)
        top.addWidget(self.chk_local)
        top.addWidget(self.chk_ima)

        # 本地盘符勾选面板：勾选「本地」后出现，限定本地检索范围（C盘 / E盘 等）。
        # 默认全部勾选（等价于检索所有盘符）；取消某些盘符即只搜剩余盘符。
        self.drive_panel = QWidget()
        self.drive_row = QHBoxLayout(self.drive_panel)
        self.drive_row.setContentsMargins(0, 0, 0, 0)
        self.drive_row.addWidget(QLabel("盘符:"))
        self.drive_chks: dict = {}
        for d in _detect_drives():
            cb = QCheckBox(f"{d}盘")
            cb.setChecked(True)
            cb.setProperty("drive", d)
            self.drive_chks[d] = cb
            self.drive_row.addWidget(cb)
        self.chk_local.toggled.connect(self._on_local_toggled)
        self.drive_panel.setVisible(self.chk_local.isChecked())
        root.addWidget(self.drive_panel)

        # 知识库类型勾选面板：勾选「ima 知识库」后出现，限定只检索指定类型的知识库。
        # 默认三类全勾选（等价于检索全部类型）；取消某些类型即只搜剩余类型。
        self.kbtype_panel = QWidget()
        self.kbtype_row = QHBoxLayout(self.kbtype_panel)
        self.kbtype_row.setContentsMargins(0, 0, 0, 0)
        self.kbtype_row.addWidget(QLabel("知识库类型:"))
        self.kbtype_chks: dict = {}
        for label in ima_search.KB_TYPE_LABELS:
            cb = QCheckBox(label)
            cb.setChecked(True)
            cb.setProperty("kbtype", label)
            self.kbtype_chks[label] = cb
            self.kbtype_row.addWidget(cb)
        self.chk_ima.toggled.connect(self._on_ima_toggled)
        self.kbtype_panel.setVisible(self.chk_ima.isChecked())
        root.addWidget(self.kbtype_panel)

        # 排序选择器：对齐 Everything GUI 的排序列与方向
        top.addWidget(QLabel("排序："))
        self.sort_combo = QComboBox()
        for label, _, _ in SORT_OPTIONS:
            self.sort_combo.addItem(label)
        self.sort_combo.setCurrentIndex(0)  # 默认 名称↑，与 Everything 默认一致
        self.sort_combo.currentIndexChanged.connect(self.on_sort_changed)
        top.addWidget(self.sort_combo)
        root.addLayout(top)

        # 结果表格
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["来源", "标题", "位置 / 知识库", "大小", "修改时间"])
        # 所有列均可由用户拖动表头边界手动调整宽度（Interactive，而非 Stretch）
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(QHeaderView.Interactive)
        hdr.setSectionsMovable(True)  # 允许拖动列头改变列顺序
        # 初始列宽（仅首次填充内容时再按内容微调，见 apply_filter）
        self.table.setColumnWidth(0, 70)    # 来源
        self.table.setColumnWidth(1, 360)   # 标题
        self.table.setColumnWidth(2, 360)   # 位置 / 知识库
        self.table.setColumnWidth(3, 90)    # 大小
        self.table.setColumnWidth(4, 150)   # 修改时间
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setIconSize(QSize(16, 16))  # 小图标，与 Everything 一致
        self.table.doubleClicked.connect(self.on_open)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._on_table_context_menu)
        self.table.itemSelectionChanged.connect(self.on_select)
        root.addWidget(self.table, 1)

        # 来源筛选
        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("筛选："))
        self.filter_combo = QComboBox()
        self.filter_combo.addItems(["全部", "本地", "ima"])
        self.filter_combo.currentTextChanged.connect(self.apply_filter)
        filter_row.addWidget(self.filter_combo)
        filter_row.addStretch(1)
        root.addLayout(filter_row)

        # 详情
        self.detail = QTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setMaximumHeight(120)
        self.detail.setPlaceholderText("选中结果后显示位置 / ima 高亮摘要")
        root.addWidget(self.detail)

        # 状态栏：左侧文本提示，右侧百分比进度条
        self.status = QStatusBar()
        self.setStatusBar(self.status)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setFormat("%p%")  # 显示百分比，如 45%
        self.progress_bar.setFixedWidth(160)
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setVisible(False)  # 空闲时隐藏
        self.status.addPermanentWidget(self.progress_bar)
        self.status.showMessage("就绪")

    def _check_backends(self):
        msgs = []
        if self.chk_local.isChecked():
            if not local_search.is_everything_reachable(self.ev_cfg):
                if ev_bundler.bundled_everything_exe():
                    # 已自动启动内置 Everything 便携版（正在建立索引），
                    # 首次检索时会自动等待其就绪，不必打扰用户
                    self.status.showMessage("内置 Everything 正在启动（首次需建立索引）…")
                else:
                    msgs.append(
                        f"Everything HTTP Server 不可达（{self.ev_cfg.base_url}），"
                        "请在 Everything -> 选项 -> HTTP 服务器 中启用"
                    )
        if self.chk_ima.isChecked() and not self.ima_cfg.has_credentials:
            msgs.append("未配置 ima 凭证（client_id / api_key）")
        if msgs:
            for m in msgs:
                self.status.showMessage("⚠ " + m)
            QMessageBox.warning(self, "后端未就绪", "\n".join(msgs))

    def _apply_sort_from_combo(self):
        """把排序下拉框的当前选择同步到 EverythingConfig。"""
        label = self.sort_combo.currentText()
        for opt_label, sort, asc in SORT_OPTIONS:
            if opt_label == label:
                self.ev_cfg.sort = sort
                self.ev_cfg.ascending = asc
                break

    def on_sort_changed(self, _idx: int):
        # 切换排序后，若已有检索词则重新查询，确保从 Everything 取回新顺序的结果
        # （名称排序依赖 Everything 服务端自然序，不能在客户端重排）。
        self._apply_sort_from_combo()
        if self.input.text().strip():
            self.on_search()

    def open_settings(self):
        """打开「文件格式排序」设置对话框；确认后保存并在已有结果上即时重排。

        只更新格式相关字段，保留 ima 并发等其它设置，避免两个设置组互相重置。
        """
        dlg = SettingsDialog(self.settings, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            s = dlg.get_settings()
            self.settings.format_sort_enabled = s.format_sort_enabled
            self.settings.folders_first = s.folders_first
            self.settings.format_order = s.format_order
            self.settings.group_by_name = s.group_by_name
            save_settings(self.settings)
            # 若已有检索结果，按新设置重排并刷新（无需重新查询 Everything）
            if self.all_results:
                self.all_results = format_sort.reorder_by_format(self.all_results, self.settings)
                self.apply_filter(self.filter_combo.currentText())
            self.status.showMessage("设置已保存")

    def open_ima_creds(self):
        """打开 ima API 凭证配置对话框；确认后写入 ~/.config/ima 并即时刷新内存凭证。"""
        dlg = ImaCredsDialog(self.ima_cfg, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            cid, key = dlg.get_credentials()
            save_ima_credentials(cid, key)
            # 即时刷新，下一次检索即用新凭证（无需重启）
            self.ima_cfg = load_ima_config()
            self.status.showMessage("ima 凭证已保存")

    def open_concurrency(self):
        """打开 ima 提速（并发/限速）设置对话框；确认后保存，下一次检索即时生效。"""
        dlg = ConcurrencyDialog(self.settings, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            s = dlg.get_settings()
            # 只更新并发相关字段，保留文件格式排序等其它设置
            self.settings.ima_max_workers = s.ima_max_workers
            self.settings.ima_req_per_sec = s.ima_req_per_sec
            self.settings.ima_burst = s.ima_burst
            save_settings(self.settings)
            self.status.showMessage(
                f"提速设置已保存（并发上限 {self.settings.ima_burst} / "
                f"{self.settings.ima_req_per_sec:g} 请求/秒）"
            )

    def on_search(self):
        q = self.input.text().strip()
        if not q:
            return
        self._apply_sort_from_combo()
        self.table.setRowCount(0)
        self.all_results = []
        self.detail.clear()
        self.btn_search.setEnabled(False)
        self.worker = SearchWorker(
            q, self.ev_cfg, self.ima_cfg, self.kb_ids,
            self.chk_local.isChecked(), self.chk_ima.isChecked(),
            ima_max_workers=self.settings.ima_max_workers,
            ima_req_per_sec=self.settings.ima_req_per_sec,
            ima_burst=self.settings.ima_burst,
            local_drives=self._selected_drives(),
            ima_kb_types=self._selected_kb_types(),
        )
        self.worker.progress.connect(self.status.showMessage)
        self.worker.progress_pct.connect(self._on_progress_pct)
        self.worker.error_ready.connect(self._on_worker_error)
        self.worker.result_ready.connect(self.on_results)
        self.worker.finished.connect(lambda: self.btn_search.setEnabled(True))
        # 开始检索：显示进度条并归零
        self.progress_bar.setValue(0)
        self.progress_bar.setVisible(True)
        self.worker.start()

    def _on_progress_pct(self, pct: int):
        """更新状态栏百分比进度条。"""
        self.progress_bar.setVisible(True)
        self.progress_bar.setValue(max(0, min(100, pct)))

    def _on_local_toggled(self, checked: bool):
        """勾选/取消「本地」时显隐盘符选择面板。"""
        self.drive_panel.setVisible(checked)

    def _selected_drives(self) -> List[str]:
        """返回当前勾选的盘符列表（如 ["C", "E"]）；全空时返回 []（搜全部）。"""
        if not self.chk_local.isChecked():
            return []
        return [d for d, cb in self.drive_chks.items() if cb.isChecked()]

    def _on_ima_toggled(self, checked: bool):
        """勾选/取消「ima 知识库」时显隐知识库类型选择面板。"""
        self.kbtype_panel.setVisible(checked)

    def _selected_kb_types(self) -> Optional[List[str]]:
        """返回当前勾选的知识库类型标签列表（如 ["个人知识库", "订阅知识库"]）。

        - 未勾「ima 知识库」：返回 None（整块不检索）。
        - 三类全选：返回 None，表示"检索全部类型"——保持原行为，且不会因出现
          未知 base_type 的库而被误排除。
        - 部分勾选 或 全不选([])：返回所勾列表（全不选即 []，表示不检索任何类型）。
        """
        if not self.chk_ima.isChecked():
            return None
        selected = [label for label, cb in self.kbtype_chks.items() if cb.isChecked()]
        if set(selected) == set(ima_search.KB_TYPE_LABELS):
            return None
        return selected

    def _on_worker_error(self, msg: str):
        self.status.showMessage("⚠ " + msg)
        # 单库限流等软错误不隐藏进度条（检索仍在继续）；真正的致命错误由 on_results 收尾

    def on_results(self, results: List[SearchResult]):
        # 若启用「按文件格式优先级排序」，在客户端以格式为PrimaryKey重排
        self.all_results = format_sort.reorder_by_format(results, self.settings)
        self.apply_filter(self.filter_combo.currentText())
        self.progress_bar.setValue(100)
        self.progress_bar.setVisible(False)  # 完成后隐藏进度条
        self.status.showMessage(f"共 {len(self.all_results)} 条结果")

    def _filtered(self, src_text: str) -> List[SearchResult]:
        if src_text == "本地":
            return [r for r in self.all_results if r.source == "local"]
        if src_text == "ima":
            return [r for r in self.all_results if r.source == "ima"]
        return self.all_results

    def apply_filter(self, src_text: str):
        data = self._filtered(src_text)
        self.table.setRowCount(len(data))
        for i, r in enumerate(data):
            src_item = QTableWidgetItem(r.source_label())
            src_item.setForeground(COLOR_LOCAL if r.source == "local" else COLOR_IMA)
            self.table.setItem(i, 0, src_item)
            title_item = QTableWidgetItem(r.title)
            title_item.setIcon(self._icon_for(r))  # 系统文件/文件夹图标，与 Everything 一致
            self.table.setItem(i, 1, title_item)
            loc = r.path if r.source == "local" else (r.kb_name or "ima")
            self.table.setItem(i, 2, QTableWidgetItem(loc))
            self.table.setItem(i, 3, QTableWidgetItem(_fmt_size(r.size)))
            self.table.setItem(i, 4, QTableWidgetItem(_fmt_dt(r.modified)))
        # 仅在首次填充时按内容自适应列宽；之后尊重用户的手动调整，不再覆盖
        if not self._cols_initialized:
            self.table.resizeColumnsToContents()
            self._cols_initialized = True

    def on_select(self):
        row = self.table.currentRow()
        if row < 0 or row >= len(self._filtered(self.filter_combo.currentText())):
            self.detail.clear()
            return
        r = self._filtered(self.filter_combo.currentText())[row]
        lines = []
        if r.source == "local":
            lines.append(f"类型：本地文件")
            lines.append(f"路径：{r.path}")
            lines.append(f"大小：{_fmt_size(r.size)}")
            lines.append(f"修改时间：{_fmt_dt(r.modified)}")
        else:
            lines.append(f"类型：ima 知识库文档")
            lines.append(f"知识库：{r.kb_name}")
            lines.append(f"标题：{r.title}")
            if r.snippet:
                lines.append(f"匹配摘要：{r.snippet}")
        self.detail.setPlainText("\n".join(lines))

    def on_open(self):
        r = self._current_result()
        if r is not None:
            self._open_result(r)

    def _current_result(self):
        """取当前选中行对应的 SearchResult；无有效行返回 None。"""
        row = self.table.currentRow()
        data = self._filtered(self.filter_combo.currentText())
        if 0 <= row < len(data):
            return data[row]
        return None

    def _open_result(self, r: SearchResult):
        """【打开】：与双击一致——本地打开文件；ima 取其直链用浏览器打开。"""
        if r.source == "local" and r.path:
            try:
                if os.path.isfile(r.path):
                    QDesktopServices.openUrl(QUrl.fromLocalFile(r.path))
                else:
                    QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(r.path)))
            except Exception:  # noqa: BLE001
                pass
        elif r.source == "ima":
            self._open_ima(r)

    def _open_result_path(self, r: SearchResult):
        """【打开路径】：本地打开所在文件夹（并选中文件）；ima 打开 ima 网页到对应知识库。"""
        if r.source == "local" and r.path:
            # 打开所在文件夹：文件夹结果打开其本身；文件结果打开其父目录（即"路径"列的目录）。
            # 用 os.startfile 直接打开该目录——比 `explorer /select,<path>` 稳定，
            # 后者在部分环境会退回打开默认"文档"库。
            target = (
                r.path if (r.kind == "folder" and os.path.isdir(r.path))
                else (os.path.dirname(r.path) or r.path)
            )
            try:
                if target and os.path.isdir(target):
                    if os.name == "nt":
                        os.startfile(target)
                    else:
                        QDesktopServices.openUrl(QUrl.fromLocalFile(target))
                elif target:
                    QDesktopServices.openUrl(QUrl.fromLocalFile(target))
            except Exception:  # noqa: BLE001
                pass
        elif r.source == "ima":
            self._open_ima_kb(r)

    def _ima_client_available(self) -> bool:
        """检测是否安装了 ima 电脑端（是否注册了 imacopilot / ima.copilot 协议）。"""
        if os.name != "nt":
            return False
        if self._ima_client_cache is None:
            ok = False
            try:
                import winreg
                for scheme in ("imacopilot", "ima.copilot"):
                    try:
                        winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, scheme)
                        ok = True
                        break
                    except OSError:
                        continue
            except Exception:  # noqa: BLE001
                ok = False
            self._ima_client_cache = ok
        return self._ima_client_cache

    def _open_ima_link(self, deeplink: str, browser_url: str) -> bool:
        """优先用 ima 电脑端深链打开；未装客户端则回退默认浏览器打开 browser_url。

        返回 True 表示走了 ima 客户端。深链形如
        imacopilot://open?url=chrome-extension://<KB扩展>/index.html?action=autoOpenMedia
            &knowledgeBaseId=..&mediaId=..（精准定位到库内该篇文档）。
        """
        if self._ima_client_available():
            self._open_ima_deeplink(deeplink)
            return True
        QDesktopServices.openUrl(QUrl(browser_url))
        return False

    @staticmethod
    def _open_ima_deeplink(deeplink: str):
        """调起 ima 客户端打开深链。

        用 os.startfile 走 ShellExecute，保留 imacopilot://open?url=... 的原始大小写
        （QUrl 会把 host 归一为小写，可能影响客户端匹配）。
        """
        if os.name == "nt":
            os.startfile(deeplink)
        else:
            QDesktopServices.openUrl(QUrl(deeplink))

    def _open_ima_kb(self, r: SearchResult):
        """ima 打开路径：解析数字 knowledgeBaseId，优先用 ima 电脑端精准打开该知识库。

        网页/客户端深链都需要数字型 knowledgeBaseId，OpenAPI 只给 base64 kb_id
        （无法换算），故用 get_knowledge_list 根目录的 current_path[0].folder_id
        解析，并按 kb_id 缓存。
        """
        numeric = self._kb_numeric_cache.get(r.kb_id) if r.kb_id else None
        if numeric is None and r.kb_id and self.ima_cfg.has_credentials:
            numeric = self._resolve_kb_numeric(r.kb_id)
        if numeric:
            deeplink = ima_search.ima_client_kb_url(numeric)
            web = f"https://ima.qq.com/wikis?knowledgeBaseId={numeric}"
            used = self._open_ima_link(deeplink, web)
            self.status.showMessage(
                ("已在 ima 客户端打开知识库：" if used else "已打开知识库网页：")
                + (r.kb_name or numeric))
        else:
            QDesktopServices.openUrl(QUrl("https://ima.qq.com/"))
            self.status.showMessage("未能获取知识库数字 ID，已打开 ima 网页")

    def _resolve_kb_numeric(self, kb_id: str):
        """解析并缓存 kb_id(base64) -> 数字 knowledgeBaseId；失败返回 None。

        负面缓存：已知解析不出数字 ID 的 kb_id 记入 _kb_numeric_failed，
        避免每次打开都白跑一次 get_knowledge_list 请求（OpenAPI 当前不暴露
        数字 ID，见 ima_search.get_kb_numeric_id 的背景说明）。
        """
        numeric = self._kb_numeric_cache.get(kb_id)
        if numeric:
            return numeric
        if kb_id in self._kb_numeric_failed:
            return None
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            numeric = ima_search.get_kb_numeric_id(self.ima_cfg, kb_id)
        except Exception:  # noqa: BLE001
            numeric = None
        finally:
            QApplication.restoreOverrideCursor()
        if numeric:
            self._kb_numeric_cache[kb_id] = numeric
        else:
            self._kb_numeric_failed.add(kb_id)
        return numeric

    def _on_table_context_menu(self, pos):
        """结果表格右键菜单：【打开】/【打开路径】。"""
        item = self.table.itemAt(pos)
        row = item.row() if item is not None else self.table.currentRow()
        data = self._filtered(self.filter_combo.currentText())
        if not (0 <= row < len(data)):
            return
        self.table.selectRow(row)  # 同步选中被右键的行
        r = data[row]
        menu = QMenu(self)
        act_open = menu.addAction("打开")
        act_path = menu.addAction("打开路径")
        chosen = menu.exec(self.table.viewport().mapToGlobal(pos))
        if chosen == act_open:
            self._open_result(r)
        elif chosen == act_path:
            self._open_result_path(r)

    def _open_ima(self, r: SearchResult):
        """双击/【打开】ima 结果：优先用 ima 电脑端精准打开该篇文档。

        深链：imacopilot://open?url=chrome-extension://<KB扩展>/index.html
            ?action=autoOpenMedia&knowledgeBaseId=<数字ID>&mediaId=<媒体ID>
        （数字 ID 由 get_kb_numeric_id 解析）。action=autoOpenMedia 是客户端“知识库页”
        真正打开文档的开关；未装电脑端则回退：用 get_media_info 的 COS 直链在默认浏览器
        打开（PDF/图片预览，Office 下载）。失败回退 ima 网页。

        2026-10-23 修正：OpenAPI 不暴露数字 knowledgeBaseId（其 ID 空间只有 base64 的
        ima_kb_XXX），get_kb_numeric_id 增加纯数字校验后对现有账号恒返回 None，
        因此正常情况下走 COS 直链回退（实测可用）；深链路径保留，待 OpenAPI 暴露
        数字 ID 时自动恢复。
        """
        if not self.ima_cfg.has_credentials or not r.ref_id:
            QDesktopServices.openUrl(QUrl("https://ima.qq.com/"))
            self.status.showMessage("未配置 ima 凭证或无文档 ID，已打开 ima 网页")
            return

        # 装了电脑端：用 open?url= 桥 + action=autoOpenMedia 精准打开该篇
        if self._ima_client_available() and r.kb_id:
            numeric = self._resolve_kb_numeric(r.kb_id)
            if numeric:
                self._open_ima_deeplink(ima_search.ima_client_doc_url(numeric, r.ref_id))
                self.status.showMessage(f"已在 ima 客户端打开文档：{r.title}")
                return

        # 回退：取 COS 直链用默认浏览器打开
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        url = None
        err = ""
        try:
            url = ima_search.get_media_url(self.ima_cfg, r.ref_id)
        except ima_search.ImaApiError as e:
            err = e.msg
        except Exception as e:  # noqa: BLE001
            err = str(e)
        finally:
            QApplication.restoreOverrideCursor()
        if url:
            QDesktopServices.openUrl(QUrl(url))
            self.status.showMessage("已在默认浏览器打开 ima 文档")
        else:
            QDesktopServices.openUrl(QUrl("https://ima.qq.com/"))
            self.status.showMessage(f"未取到直链（{err or '无 url'}），已打开 ima 网页")


    def closeEvent(self, event):
        # 关闭窗口 -> 最小化到托盘（不真正退出）；仅当用户从托盘菜单「退出」时才真正退出。
        # 必须在 MainWindow 类体内定义，PySide6 才能将其注册为 Qt 虚函数覆盖（setattr 无效）。
        if self._tray_available and self.tray.isVisible() and not self._force_quit:
            self.hide()
            event.ignore()
            self.status.showMessage("SuperSearch 已最小化到系统托盘，右键托盘图标可退出")
        else:
            event.accept()
            # 无托盘环境下关闭窗口即真正退出：退出内置 Everything 实例并
            # 等待其保存数据库，下次启动可快速冷加载（见 quit_app 注释）。
            # 此处阻塞 UI 线程数秒无妨——窗口本就要关闭。
            ev_bundler.quit_our_instance_and_wait_save()


def _quit_bundled_everything() -> int:
    """退出内置 Everything 独立实例（供卸载清理调用，避免残留进程/锁文件）。

    优先用稳定目录里正在运行的副本；失败再回退到随包内置的 everything.exe。
    返回 0 表示已尝试退出（无论目标是否原本就在运行），非 0 表示找不到 exe。
    """
    import subprocess
    candidates = []
    stable = ev_bundler._stable_exe_path()
    if os.path.isfile(stable):
        candidates.append(stable)
    bundled = ev_bundler.bundled_everything_exe()
    if bundled:
        candidates.append(bundled)
    for exe in candidates:
        try:
            # -instance 限定只退出 SuperSearch 实例；-quit 触发退出
            subprocess.run(
                [exe, "-instance", ev_bundler.INSTANCE_NAME, "-quit"],
                timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            # 等数据库保存落盘后再返回，安装器才会继续删文件，
            # 避免删到半截 .db / 残留 .db.tmp 导致下次安装后索引异常
            ev_bundler._wait_db_save_complete()
            return 0
        except Exception:  # noqa: BLE001
            continue
    return 1


def main():
    import sys
    # 卸载清理钩子：仅退出内置 Everything 实例并立即退出，不启动 GUI
    if "--quit-everything" in sys.argv:
        sys.exit(_quit_bundled_everything())
    # 安装器（已提权）调用：安装 Everything 服务后立即退出，不启动 GUI。
    # 装了服务，标准用户客户端即可经服务索引 NTFS，从此不弹“需要管理员权限”
    # 对话框、也不触发 UAC。
    if "--install-everything-service" in sys.argv:
        ok, err = ev_bundler.install_service()
        if not ok and err:
            print(err, file=sys.stderr)
        sys.exit(0 if ok else 1)
    # 卸载清理钩子：卸载 Everything 服务后立即退出（-uninstall-service 按服务名
    # 经 SCM 移除，与用哪个副本执行无关）。
    if "--uninstall-everything-service" in sys.argv:
        ev_bundler.uninstall_service()
        sys.exit(0)

    app = QApplication([])
    # 统一设置应用图标（窗口标题栏 / 任务栏 / 托盘共用），从 .ico 加载
    icon_path = _app_icon_path()
    if icon_path:
        app.setWindowIcon(QIcon(icon_path))
    win = MainWindow()
    win.show()
    app.exec()


def _setup_tray(self):
    """初始化系统托盘图标（Windows 通知区域）。

    不依赖 QSystemTrayIcon.isSystemTrayAvailable()（部分环境会误报 False 导致托盘
    整体不创建）；直接创建并 show()。若系统确实无托盘，show() 为 no-op，关闭时由
    closeEvent 优雅退出。图标缺失时用纯色方块兜底，确保托盘图标永不可见缺失。
    """
    self._tray_available = True
    self.tray = QSystemTrayIcon(self)
    icon = QApplication.windowIcon()
    if icon.isNull():
        icon = QIcon(_app_icon_path() or "")
    if icon.isNull():
        pm = QPixmap(32, 32)
        pm.fill(QColor(45, 108, 223))
        icon = QIcon(pm)
    self.tray.setIcon(icon)
    self.tray.setToolTip("SuperSearch")
    menu = QMenu(self)
    act_show = menu.addAction("显示主窗口")
    act_show.triggered.connect(self.show_normal)
    menu.addSeparator()
    act_quit = menu.addAction("退出 SuperSearch")
    act_quit.triggered.connect(self.quit_app)
    self.tray.setContextMenu(menu)
    self.tray.activated.connect(self.on_tray_activated)
    self.tray.show()


def on_tray_activated(self, reason):
    # 双击 / 单击托盘图标：切换主窗口显隐
    if reason in (QSystemTrayIcon.DoubleClick, QSystemTrayIcon.Trigger):
        if self.isVisible() and not self.isMinimized():
            self.hide()
        else:
            self.show_normal()


def show_normal(self):
    self.show()
    self.setWindowState(self.windowState() & ~Qt.WindowState.WindowMinimized)
    self.raise_()
    self.activateWindow()
    if self._tray_available:
        self.tray.show()


def quit_app(self):
    # 真正退出：先收起托盘图标，再结束事件循环
    self._force_quit = True
    if self._tray_available:
        self.tray.hide()
    # 退出内置 Everything 实例并等待其保存数据库：实例退出时会写盘 .db
    # （233MB 约需 2-5s），等落盘完成再退出应用，避免"重启应用时新实例与
    # 旧实例存盘竞争"导致 HTTP 绑定延迟数十秒；同时让库保持新鲜，下次冷启动
    # 数秒即可就绪（实测 4.5s，陈旧库则要约 80s 全量重建）。
    ev_bundler.quit_our_instance_and_wait_save()
    QApplication.quit()


# 将托盘方法绑定到 MainWindow（保持 MainWindow 类体整洁）
# 注：closeEvent 必须在 MainWindow 类体内定义（见上方），PySide6 才能将其注册为
#     Qt 虚函数覆盖；其余方法经信号连接，setattr 绑定即可正常工作。
for _m in ("_setup_tray", "on_tray_activated", "show_normal", "quit_app"):
    setattr(MainWindow, _m, globals()[_m])


class SettingsDialog(QDialog):
    """「文件格式排序」设置对话框。

    用户可定义文件扩展名的优先级顺序（越靠前越优先），并决定是否启用、
    以及文件夹是否置顶。确认后由调用方持久化并即时重排结果。
    """

    def __init__(self, settings: Settings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("设置 - 文件格式排序")
        self.resize(380, 420)

        layout = QVBoxLayout(self)

        self.chk_enable = QCheckBox("启用按文件格式优先级排序（主键）")
        self.chk_enable.setChecked(settings.format_sort_enabled)
        layout.addWidget(self.chk_enable)

        self.chk_folders = QCheckBox("文件夹置顶")
        self.chk_folders.setChecked(settings.folders_first)
        layout.addWidget(self.chk_folders)

        self.chk_group = QCheckBox("同名结果归组（本地 / ima 混排相邻）")
        self.chk_group.setChecked(getattr(settings, "group_by_name", True))
        self.chk_group.setToolTip(
            "把本地与 ima 中同名的结果排在一起，不再分成“本地一堆 / ima 一堆”。"
        )
        layout.addWidget(self.chk_group)

        layout.addWidget(
            QLabel("格式优先级（从上到下，越靠上越优先；不含点，如 pdf）。"
                   "ima 文档与本地文件按扩展名混排；把 folder 加入列表可控制文件夹位置。")
        )

        self.listw = QListWidget()
        self.listw.addItems(settings.format_order)
        layout.addWidget(self.listw)

        btn_row = QHBoxLayout()
        b_add = QPushButton("添加")
        b_folder = QPushButton("＋文件夹(folder)")
        b_del = QPushButton("删除")
        b_up = QPushButton("上移")
        b_down = QPushButton("下移")
        btn_row.addWidget(b_add)
        btn_row.addWidget(b_folder)
        btn_row.addWidget(b_del)
        btn_row.addWidget(b_up)
        btn_row.addWidget(b_down)
        layout.addLayout(btn_row)

        ok_row = QHBoxLayout()
        b_ok = QPushButton("确定")
        b_cancel = QPushButton("取消")
        ok_row.addStretch(1)
        ok_row.addWidget(b_ok)
        ok_row.addWidget(b_cancel)
        layout.addLayout(ok_row)

        b_add.clicked.connect(self._on_add)
        b_folder.clicked.connect(self._on_add_folder)
        b_del.clicked.connect(self._on_del)
        b_up.clicked.connect(lambda: self._move(-1))
        b_down.clicked.connect(lambda: self._move(1))
        b_ok.clicked.connect(self.accept)
        b_cancel.clicked.connect(self.reject)

    def _on_add(self):
        ext, ok = QInputDialog.getText(
            self, "添加格式", "输入文件扩展名（不含点，如 pdf）；文件夹用 folder：")
        if ok and ext.strip():
            self._add_unique(_normalize_ext(ext))

    def _on_add_folder(self):
        # 一键把"文件夹"作为一种格式加入列表（去重）
        self._add_unique("folder")

    def _add_unique(self, text: str):
        if not text:
            return
        existing = [self.listw.item(i).text() for i in range(self.listw.count())]
        if text in existing:
            return
        self.listw.addItem(text)
        self.listw.setCurrentRow(self.listw.count() - 1)

    def _on_del(self):
        row = self.listw.currentRow()
        if row >= 0:
            self.listw.takeItem(row)

    def _move(self, delta: int):
        row = self.listw.currentRow()
        if row < 0:
            return
        new = row + delta
        if 0 <= new < self.listw.count():
            item = self.listw.takeItem(row)
            self.listw.insertItem(new, item)
            self.listw.setCurrentRow(new)

    def get_settings(self) -> Settings:
        return Settings(
            format_sort_enabled=self.chk_enable.isChecked(),
            folders_first=self.chk_folders.isChecked(),
            format_order=[
                self.listw.item(i).text() for i in range(self.listw.count())
            ],
            group_by_name=self.chk_group.isChecked(),
        )


def _normalize_ext(e: str) -> str:
    return str(e).strip().lower().lstrip(".")


class ImaCredsDialog(QDialog):
    """ima OpenAPI 凭证配置对话框。

    输入 Client ID 与 API Key（后者以密码形式掩码显示），确认后由调用方
    写入 ~/.config/ima（client_id / api_key），与 ima 官方 skill 约定一致。
    也可用环境变量 IMA_OPENAPI_CLIENTID / IMA_OPENAPI_APIKEY 覆盖。
    """

    def __init__(self, ima_cfg: ImaConfig, parent=None):
        super().__init__(parent)
        self.setWindowTitle("设置 - ima 凭证")
        self.resize(420, 180)

        layout = QVBoxLayout(self)

        form = QVBoxLayout()
        self.edit_cid = QLineEdit(ima_cfg.client_id)
        self.edit_cid.setPlaceholderText("ima OpenAPI Client ID")
        self.edit_key = QLineEdit(ima_cfg.api_key)
        self.edit_key.setPlaceholderText("ima OpenAPI API Key")
        self.edit_key.setEchoMode(QLineEdit.EchoMode.Password)  # 掩码显示密钥

        form.addWidget(QLabel("Client ID："))
        form.addWidget(self.edit_cid)
        form.addWidget(QLabel("API Key："))
        form.addWidget(self.edit_key)
        layout.addLayout(form)

        layout.addWidget(
            QLabel("保存到 ~/.config/ima（client_id / api_key）。也可通过设置环境变量 "
                   "IMA_OPENAPI_CLIENTID / IMA_OPENAPI_APIKEY 覆盖。")
        )

        ok_row = QHBoxLayout()
        b_ok = QPushButton("确定")
        b_cancel = QPushButton("取消")
        ok_row.addStretch(1)
        ok_row.addWidget(b_ok)
        ok_row.addWidget(b_cancel)
        layout.addLayout(ok_row)

        b_ok.clicked.connect(self.accept)
        b_cancel.clicked.connect(self.reject)

    def get_credentials(self) -> tuple:
        return (self.edit_cid.text().strip(), self.edit_key.text().strip())


class ConcurrencyDialog(QDialog):
    """ima 提速（并发 / 限速）设置对话框。

    暴露三个旋钮，让用户在不改代码 / 环境变量的前提下，直接调高并发以加快检索：
      - 并发上限（令牌桶突发容量）：同一时刻最多多少个请求在飞；
      - 每秒请求数上限：令牌桶速率（请求/秒）；
      - 并发线程数：工作线程数（≥ 并发上限即可，多余线程在令牌桶处排队）。
    默认取值偏保守以避免触发 ima 的账户级限流（code 200001）。若账号更宽松可调大；
    若频繁出现 200001 则调小。注意：ima 另有每日调取次数配额，与并发无关。
    """

    def __init__(self, settings: Settings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("设置 - ima 提速（并发）")
        self.resize(420, 220)

        layout = QVBoxLayout(self)

        form = QFormLayout()

        # 并发上限（令牌桶突发）
        self.spin_burst = QSpinBox()
        self.spin_burst.setRange(1, 16)
        self.spin_burst.setValue(settings.ima_burst)
        self.spin_burst.setToolTip(
            "同一时刻最多多少个请求并发在飞。调大提速，但更可能触发 ima 频率限流（200001）。"
        )
        form.addRow("并发上限（瞬时）:", self.spin_burst)

        # 每秒请求数上限
        self.spin_rate = QDoubleSpinBox()
        self.spin_rate.setRange(0.5, 50.0)
        self.spin_rate.setSingleStep(0.5)
        self.spin_rate.setValue(settings.ima_req_per_sec)
        self.spin_rate.setSuffix(" 请求/秒")
        self.spin_rate.setToolTip("全局请求速率上限（令牌桶速率）。与并发上限共同决定实际速度。")
        form.addRow("每秒请求数上限:", self.spin_rate)

        # 并发线程数
        self.spin_workers = QSpinBox()
        self.spin_workers.setRange(1, 32)
        self.spin_workers.setValue(settings.ima_max_workers)
        self.spin_workers.setToolTip(
            "工作线程数，建议 ≥ 并发上限；多余的线程会在令牌桶处排队，不会额外增快也不会更慢。"
        )
        form.addRow("并发线程数:", self.spin_workers)

        layout.addLayout(form)

        layout.addWidget(
            QLabel("默认偏保守以避免限流。若账号更宽松可调大；若频繁出现 "
                   "「请求频率超限 (200001)」则调小。注意并发不会突破 ima 的每日调取次数配额。")
        )

        ok_row = QHBoxLayout()
        b_ok = QPushButton("确定")
        b_cancel = QPushButton("取消")
        ok_row.addStretch(1)
        ok_row.addWidget(b_ok)
        ok_row.addWidget(b_cancel)
        layout.addLayout(ok_row)

        b_ok.clicked.connect(self.accept)
        b_cancel.clicked.connect(self.reject)

    def get_settings(self) -> Settings:
        # 返回仅含并发字段的临时 Settings（调用方只取这三个字段）
        return Settings(
            ima_max_workers=self.spin_workers.value(),
            ima_req_per_sec=self.spin_rate.value(),
            ima_burst=self.spin_burst.value(),
        )


if __name__ == "__main__":
    main()
