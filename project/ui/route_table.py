# -*- coding: utf-8 -*-
"""
ui/route_table.py — 路由表视图组件

RouteTableView(QTableWidget) 特性：
  - 展示 logicAddr → PortPath 路由表（3 列：逻辑地址 / 路由路径 / has485）
  - 订阅 RouteTableUpdatedEvent，触发全量刷新
  - 刷新通过 Signal 投递到主线程

自测（__main__）：注入模拟路由数据，验证表格渲染。
"""
from __future__ import annotations

import sys
from typing import List, Optional, Tuple

from PySide6.QtCore import Signal, Slot
from PySide6.QtWidgets import (
    QApplication, QTableWidget, QTableWidgetItem, QWidget,
    QHeaderView,
)


class RouteTableView(QTableWidget):
    """
    路由表格组件，支持全量刷新。
    """

    _refresh_signal: Signal = Signal(list)   # List[Tuple[int, PortPath]]

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(0, 3, parent)
        self.setHorizontalHeaderLabels(["逻辑地址", "路由路径", "has485"])
        self.horizontalHeader().setStretchLastSection(True)
        self.setAlternatingRowColors(True)
        self.setEditTriggers(QTableWidget.NoEditTriggers)
        self.setSelectionBehavior(QTableWidget.SelectRows)

        self._refresh_signal.connect(self._on_refresh)

    # ──────────────────────────────────────────
    # EventBus 回调（任意线程）
    # ──────────────────────────────────────────

    def refresh_from_table(self, logic_table) -> None:
        """
        由 RouteTableUpdatedEvent 处理器调用，
        将 PCLogicRoutingTable 所有条目推送到主线程刷新。
        """
        entries = logic_table.all_entries()   # snapshot，不持锁
        self._refresh_signal.emit(entries)

    # ──────────────────────────────────────────
    # 主线程 Slot
    # ──────────────────────────────────────────

    @Slot(list)
    def _on_refresh(self, entries: List[Tuple]) -> None:
        self.setRowCount(0)
        for logical_addr, port_path in entries:
            row = self.rowCount()
            self.insertRow(row)
            self.setItem(row, 0, QTableWidgetItem(f"0x{logical_addr:04X}"))
            self.setItem(row, 1, QTableWidgetItem(str(list(port_path.ports))))
            self.setItem(row, 2, QTableWidgetItem(
                "是" if port_path.has485 else "否"))


# ──────────────────────────────────────────────
# 自测
# ──────────────────────────────────────────────

if __name__ == '__main__':
    from dataclasses import dataclass, field
    from typing import List as TList

    @dataclass
    class _PortPath:
        ports: TList[int]
        has485: bool = False

    app = QApplication(sys.argv)
    view = RouteTableView()
    view.setWindowTitle("RouteTableView 自测")
    view.resize(500, 300)
    view.show()

    entries = [
        (0x801, _PortPath([])),
        (0x802, _PortPath([1])),
        (0x803, _PortPath([1, 2])),
        (0x001, _PortPath([1, 2, 3])),
        (0x002, _PortPath([1, 3, 5, 2], has485=True)),
    ]
    view._on_refresh(entries)

    sys.exit(app.exec())
