"""无界面验证：在 offscreen 平台下构造主窗口，确认 UI 代码可导入且可实例化。

运行：
  QT_QPA_PLATFORM=offscreen venv/Scripts/python.exe tests/verify_gui.py
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication  # noqa: E402
from PySide6.QtWidgets import QHeaderView  # noqa: E402

from ui.main_window import MainWindow  # noqa: E402


def main():
    app = QApplication([])
    win = MainWindow()
    win.show()
    win.hide()
    # 触发一次空检索的逻辑分支（不联网）
    win.input.setText("")

    # 断言：所有列均为可手动调整的 Interactive 模式（非 Stretch）
    hdr = win.table.horizontalHeader()
    for c in range(win.table.columnCount()):
        assert hdr.sectionResizeMode(c) == QHeaderView.Interactive, f"列 {c} 未设为 Interactive"
    # 断言：各列有合理初始宽度
    for c in range(win.table.columnCount()):
        assert win.table.columnWidth(c) > 0, f"列 {c} 初始宽度为 0"

    print("GUI construct OK; window title =", win.windowTitle())
    print("all columns Interactive + initial widths set: OK")


if __name__ == "__main__":
    main()
