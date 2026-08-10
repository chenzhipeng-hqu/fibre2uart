# -*- coding: utf-8 -*-
"""
ui/log_viewer.py — 日志面板组件

LogViewer(QPlainTextEdit) 特性：
  - 最多缓存 MAX_LINES=5000 行，超出后自动滚动丢弃最早行
  - 支持 DEBUG / INFO / WARNING / ERROR 4 级过滤
  - 线程安全：后台线程通过 Signal 投递，禁止直接操作 Widget

用法::
    viewer = LogViewer(parent)
    viewer.append_log("INFO", "连接成功")
    viewer.set_level_filter("WARNING")   # 只显示 WARNING 及以上

自测（__main__）：弹出窗口，注入模拟日志条目验证渲染。
"""
from __future__ import annotations

import logging
import sys
from typing import Optional

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtGui import QColor, QTextCharFormat, QFont, QTextCursor
from PySide6.QtWidgets import QApplication, QPlainTextEdit, QWidget


# 日志级别 → 颜色
_LEVEL_COLORS = {
    "DEBUG":   "#888888",
    "INFO":    "#DDDDDD",
    "WARNING": "#FFC107",
    "ERROR":   "#F44336",
}

_LEVEL_ORDER = {"DEBUG": 0, "INFO": 1, "WARNING": 2, "ERROR": 3}

MAX_LINES = 5000


class LogViewer(QPlainTextEdit):
    """
    线程安全日志面板。

    发布者（后台线程）调用 `append_log(level, message)`，
    内部通过 Signal/Slot 保证在 Qt 主线程中更新 Widget。
    """

    # 后台线程 → 主线程
    _log_signal: Signal = Signal(str, str)   # (level, message)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setReadOnly(True)
        font = QFont("Monospace")
        font.setPointSize(9)
        self.setFont(font)
        self.setMaximumBlockCount(MAX_LINES)

        # 暗色背景
        self.setStyleSheet("background-color: #1E1E1E; color: #DDDDDD;")

        self._min_level: int = _LEVEL_ORDER["DEBUG"]
        self._log_signal.connect(self._on_log_signal)

    # ──────────────────────────────────────────
    # 公共接口（线程安全）
    # ──────────────────────────────────────────

    def append_log(self, level: str, message: str) -> None:
        """
        向日志面板追加一行。可在任意线程调用（通过 Signal 投递）。
        """
        self._log_signal.emit(level.upper(), message)

    def set_level_filter(self, level: str) -> None:
        """设置最低显示级别（DEBUG / INFO / WARNING / ERROR）。"""
        self._min_level = _LEVEL_ORDER.get(level.upper(), 0)

    def clear_log(self) -> None:
        self.clear()

    # ──────────────────────────────────────────
    # 内部（主线程 Slot）
    # ──────────────────────────────────────────

    @Slot(str, str)
    def _on_log_signal(self, level: str, message: str) -> None:
        if _LEVEL_ORDER.get(level, 0) < self._min_level:
            return

        color = _LEVEL_COLORS.get(level, "#DDDDDD")
        line = f"[{level:<7}] {message}"

        # 追加带颜色的文本
        fmt = self.currentCharFormat()
        fmt.setForeground(QColor(color))
        cursor = self.textCursor()
        cursor.movePosition(QTextCursor.End)
        cursor.insertText(line + "\n", fmt)

        # 自动滚动到底部
        self.verticalScrollBar().setValue(self.verticalScrollBar().maximum())


# ──────────────────────────────────────────────
# Qt Logging Handler — 将 Python logging 输出到 LogViewer
# ──────────────────────────────────────────────

class LogViewerHandler(logging.Handler):
    """
    将 Python logging 模块的输出桥接到 LogViewer。

    用法::
        handler = LogViewerHandler(viewer)
        logging.getLogger().addHandler(handler)
    """

    def __init__(self, viewer: LogViewer) -> None:
        super().__init__()
        self._viewer = viewer

    def emit(self, record: logging.LogRecord) -> None:
        level = record.levelname
        msg = self.format(record)
        self._viewer.append_log(level, msg)


# ──────────────────────────────────────────────
# 自测
# ──────────────────────────────────────────────

if __name__ == '__main__':
    import time
    import threading

    app = QApplication(sys.argv)
    viewer = LogViewer()
    viewer.setWindowTitle("LogViewer 自测")
    viewer.resize(800, 400)
    viewer.show()

    def emit_logs():
        levels = ["DEBUG", "INFO", "WARNING", "ERROR"]
        for i in range(30):
            lvl = levels[i % 4]
            viewer.append_log(lvl, f"测试日志 #{i:03d} — level={lvl}")
            time.sleep(0.05)

    t = threading.Thread(target=emit_logs, daemon=True)
    t.start()

    sys.exit(app.exec())
