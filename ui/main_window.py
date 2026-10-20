"""主窗口 UI：合并展示本地（Everything）与 ima 知识库检索结果。

布局：
  - 顶部：搜索框 + 搜索按钮 + 来源勾选（本地 / ima）
  - 中部：结果表格（来源标签 / 标题 / 位置 / 大小 / 修改时间）
  - 底部：来源筛选下拉 + 状态栏 + 选中详情（ima 高亮摘要）
检索在后台线程进行，避免界面卡顿。
"""
from __future__ import annotations

import os
import subprocess
from datetime import datetime
from typing import List

from PySide6.QtCore import QThread, Signal, QFileInfo, QSize, Qt
from PySide6.QtGui import QDesktopServices, QColor, QAction
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
from core.models import SearchResult
from search import local_search, ima_search


# 来源标签配色
COLOR_LOCAL = QColor("#1565C0")  # 蓝
COLOR_IMA = QColor("#2E7D32")    # 绿

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


class SearchWorker(QThread):
    result_ready = Signal(list)   # List[SearchResult]
    error_ready = Signal(str)     # 错误信息
    progress = Signal(str)        # 状态提示（文本）
    progress_pct = Signal(int)    # 检索进度（0-100，百分比）

    def __init__(self, query: str, ev_cfg: EverythingConfig, ima_cfg: ImaConfig,
                 kb_ids, do_local: bool, do_ima: bool, ima_max_workers: int = 4,
                 ima_req_per_sec: float = 4.0, ima_burst: int = 2):
        super().__init__()
        self.query = query
        self.ev_cfg = ev_cfg
        self.ima_cfg = ima_cfg
        self.kb_ids = kb_ids
        self.do_local = do_local
        self.do_ima = do_ima
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
            try:
                results.extend(local_search.search_local(self.query, self.ev_cfg))
            except Exception as e:  # noqa: BLE001
                self.error_ready.emit(f"本地检索失败：{e}")
            if has_ima:
                self.progress_pct.emit(20)
            else:
                self.progress_pct.emit(100)

        if has_ima:
            self.progress.emit("正在检索 ima 知识库…")

            def _on_ima_progress(done: int, total: int):
                if total <= 0:
                    return
                frac = done / total
                # 本地占 20%，ima 占剩余 80%
                pct = (20 + frac * 80) if has_local else (frac * 100)
                self.progress_pct.emit(int(round(pct)))

            try:
                ima_res, ima_errs = ima_search.search_ima(
                    self.query, self.ima_cfg, self.kb_ids,
                    progress_callback=_on_ima_progress,
                    req_per_sec=self.ima_req_per_sec,
                    burst=self.ima_burst,
                    max_workers=self.ima_max_workers,
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
        self._init_ui()
        self._check_backends()

    def _icon_for(self, r: SearchResult):
        """返回与 Everything 一致的系统图标。

        - 本地文件夹：文件夹图标
        - 本地文件：按扩展名取系统关联图标（路径不存在时回退通用文件图标）
        - ima 文档：通用文档图标
        """
        if r.source == "local":
            if r.kind == "folder":
                return self.icon_provider.icon(QFileIconProvider.IconType.Folder)
            info = QFileInfo(r.path)
            ic = self.icon_provider.icon(info)
            if ic.isNull():
                ic = self.icon_provider.icon(QFileIconProvider.IconType.File)
            return ic
        # ima 知识库文档：通用文档图标
        return self.icon_provider.icon(QFileIconProvider.IconType.File)

    def _init_ui(self):
        self.setWindowTitle("统一检索：本地文件 + ima 知识库")
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
        self.chk_local = QCheckBox("本地(Everything)")
        self.chk_local.setChecked(True)
        self.chk_ima = QCheckBox("ima 知识库")
        self.chk_ima.setChecked(True)
        top.addWidget(self.input, 3)
        top.addWidget(self.btn_search)
        top.addWidget(self.chk_local)
        top.addWidget(self.chk_ima)

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
            folder = os.path.dirname(r.path)
            try:
                if os.path.isdir(r.path):
                    QDesktopServices.openUrl(QUrl.fromLocalFile(r.path))
                    return
                if folder and os.path.isdir(folder):
                    # Windows：资源管理器打开并选中该文件
                    if os.name == "nt":
                        subprocess.Popen(["explorer", "/select," + r.path])
                    else:
                        QDesktopServices.openUrl(QUrl.fromLocalFile(folder))
                else:
                    QDesktopServices.openUrl(QUrl.fromLocalFile(folder or r.path))
            except Exception:  # noqa: BLE001
                pass
        elif r.source == "ima":
            self._open_ima_kb(r)

    def _open_ima_kb(self, r: SearchResult):
        """ima 打开路径：解析数字 knowledgeBaseId 并打开知识库网页。

        网页地址需要数字型 knowledgeBaseId，OpenAPI 只给 base64 kb_id（无法换算），
        故用 get_knowledge_list 根目录的 current_path[0].folder_id 解析，并按 kb_id 缓存。
        """
        numeric = self._kb_numeric_cache.get(r.kb_id) if r.kb_id else None
        if numeric is None and r.kb_id and self.ima_cfg.has_credentials:
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            try:
                numeric = ima_search.get_kb_numeric_id(self.ima_cfg, r.kb_id)
            except Exception:  # noqa: BLE001
                numeric = None
            finally:
                QApplication.restoreOverrideCursor()
            if numeric:
                self._kb_numeric_cache[r.kb_id] = numeric
        if numeric:
            url = QUrl("https://ima.qq.com/wikis")
            query = QUrlQuery()
            query.addQueryItem("knowledgeBaseId", numeric)
            url.setQuery(query)
            QDesktopServices.openUrl(url)
            self.status.showMessage(f"已打开知识库：{r.kb_name or r.kb_id}")
        else:
            QDesktopServices.openUrl(QUrl("https://ima.qq.com/"))
            self.status.showMessage("未能获取知识库数字 ID，已打开 ima 网页")

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
        """双击 ima 结果：取其真实直链并用默认浏览器打开。

        通过 get_media_info(media_id) 拿 data.url_info.url（COS 预签名直链）：
        PDF/图片会在浏览器直接预览，Office 等文件浏览器会下载到本地。
        失败或无直链时回退为打开 ima 网页，供手动定位。
        """
        if not self.ima_cfg.has_credentials or not r.ref_id:
            QDesktopServices.openUrl(QUrl("https://ima.qq.com/"))
            self.status.showMessage("未配置 ima 凭证或无文档 ID，已打开 ima 网页")
            return
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


def main():
    app = QApplication([])
    win = MainWindow()
    win.show()
    app.exec()


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
