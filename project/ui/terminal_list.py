# -*- coding: utf-8 -*-
"""
ui/terminal_list.py — 终端口 / 虚拟串口列表

TerminalPortList(QTableWidget) 特性：
  - 7 列：逻辑地址 / 类型 / 波特率(可设置) / TX/RX / 溢出 / 虚拟串口路径 / 操作按钮
  - 波特率列：下拉框选择 + "应用"按钮，应用时调用 set_baudrate_fn 发 0x33 指令
  - TX/RX 合并为 1 列，格式 "<TX>/<RX>"
  - 订阅 DeviceFoundEvent (is_terminal) 自动追加行
  - 订阅 DeviceOfflineEvent 移除对应行
  - 定时刷新 overflow_count（QTimer 每 2 秒刷新一次，避免频繁信号）
  - "开启虚拟串口" 按钮：调用 client.open_virtual_serial()，显示 device_path

自测（__main__）：注入模拟终端口数据，验证表格渲染与按钮交互。
"""
from __future__ import annotations

import sys
import threading
from typing import Callable, Dict, Optional

from PySide6.QtCore import Qt, QTimer, Signal, Slot
from PySide6.QtWidgets import (
    QApplication, QComboBox, QHBoxLayout, QPushButton, QTableWidget,
    QTableWidgetItem, QWidget, QHeaderView,
)

_COL_ADDR   = 0
_COL_TYPE   = 1
_COL_BAUD   = 2   # 波特率下拉框 + 应用按钮（嵌入 widget）
_COL_TXRX   = 3   # TX/RX 合并列
_COL_OVF    = 4
_COL_PATH   = 5
_COL_ACTION = 6

_BAUD_LIST = ["1200", "2400", "4800", "9600", "19200", "38400",
              "57600", "115200", "230400", "460800", "921600", "1000000"]


class TerminalPortList(QTableWidget):
    """
    终端口 / 虚拟串口列表，每行对应一个终端口。
    """

    _device_found_signal:   Signal = Signal(object)   # Device
    _device_offline_signal: Signal = Signal(bytes)    # uid

    def __init__(
        self,
        open_vserial_fn: Optional[Callable[[int], str]] = None,
        close_vserial_fn: Optional[Callable[[int], None]] = None,
        set_baudrate_fn: Optional[Callable[[int, int], None]] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(0, 7, parent)
        self._open_fn = open_vserial_fn
        self._close_fn = close_vserial_fn
        self._set_baudrate_fn = set_baudrate_fn

        self.setHorizontalHeaderLabels(
            ["逻辑地址", "类型", "波特率", "TX/RX", "溢出", "虚拟串口", "操作"])
        hdr = self.horizontalHeader()
        hdr.setStretchLastSection(True)
        hdr.resizeSection(_COL_BAUD, 140)   # 波特率列（下拉框+应用按钮）宽 140
        hdr.resizeSection(_COL_TXRX, 110)   # TX/RX 列宽 110
        hdr.resizeSection(_COL_PATH, 250)   # 虚拟串口路径列宽 250
        self.setEditTriggers(QTableWidget.NoEditTriggers)
        self.setSelectionBehavior(QTableWidget.SelectRows)
        self.setAlternatingRowColors(True)

        # uid → row index
        self._uid_row: Dict[bytes, int] = {}
        # logical_addr → VirtualSerialPort 对象（开启后存入）或 None
        self._vserial_map: Dict[int, object] = {}
        # 统计获取回调（可由外部赋値）：(logical_addr) → Optional[VirtualSerialPort]
        self._get_vport_fn = None

        self._device_found_signal.connect(self._on_device_found)
        self._device_offline_signal.connect(self._on_device_offline)

        # 定时刷新溢出计数
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(1000)
        self._refresh_timer.timeout.connect(self._refresh_overflow)
        self._refresh_timer.start()

    # ──────────────────────────────────────────
    # EventBus 回调（任意线程）
    # ──────────────────────────────────────────

    def handle_device_found(self, event) -> None:
        device = event.device
        if device.is_terminal:
            self._device_found_signal.emit(device)

    def handle_device_offline(self, event) -> None:
        self._device_offline_signal.emit(event.uid)

    # ──────────────────────────────────────────
    # 主线程 Slot
    # ──────────────────────────────────────────

    @Slot(object)
    def _on_device_found(self, device) -> None:
        uid = device.uid
        if uid in self._uid_row:
            return   # 幂等

        row = self.rowCount()
        self.insertRow(row)
        self._uid_row[uid] = row

        addr = device.logical_addr
        type_name = (device.port_type.name
                     if hasattr(device.port_type, 'name') else str(device.port_type))

        self.setItem(row, _COL_ADDR, QTableWidgetItem(f"0x{addr:04X}"))
        self.setItem(row, _COL_TYPE, QTableWidgetItem(type_name))

        # 波特率列：下拉框 + "应用" 按钮
        baud_widget = QWidget()
        baud_layout = QHBoxLayout(baud_widget)
        baud_layout.setContentsMargins(2, 1, 2, 1)
        baud_combo = QComboBox()
        baud_combo.setObjectName(f"baudCombo_{addr}")
        for b in _BAUD_LIST:
            baud_combo.addItem(b)
        baud_combo.setCurrentText("115200")
        btn_apply = QPushButton("应用")
        btn_apply.setFixedWidth(44)
        btn_apply.clicked.connect(
            lambda checked=False, a=addr, c=baud_combo: self._on_apply_baud(a, c))
        baud_layout.addWidget(baud_combo)
        baud_layout.addWidget(btn_apply)
        self.setCellWidget(row, _COL_BAUD, baud_widget)

        txrx_item = QTableWidgetItem("0/0")
        txrx_item.setTextAlignment(Qt.AlignCenter)
        self.setItem(row, _COL_TXRX, txrx_item)
        ovf_item = QTableWidgetItem("0")
        ovf_item.setTextAlignment(Qt.AlignCenter)
        self.setItem(row, _COL_OVF, ovf_item)
        self.setItem(row, _COL_PATH, QTableWidgetItem("—"))

        # 虚拟串口操作按钮
        btn = QPushButton("开启虚拟串口")
        btn.setProperty("logical_addr", addr)
        btn.setProperty("uid", uid)
        btn.clicked.connect(lambda checked=False, a=addr, r=row: self._on_open_btn(a, r))
        self.setCellWidget(row, _COL_ACTION, btn)

    def _on_apply_baud(self, logical_addr: int, combo: QComboBox) -> None:
        """应用按钮：在子线程发 0x33 配置波特率。"""
        if self._set_baudrate_fn is None:
            return
        try:
            baud = int(combo.currentText())
        except ValueError:
            return
        threading.Thread(
            target=self._set_baudrate_fn,
            args=(logical_addr, baud),
            daemon=True,
        ).start()

    @Slot(bytes)
    def _on_device_offline(self, uid: bytes) -> None:
        row = self._uid_row.pop(uid, None)
        if row is None:
            return
        self.removeRow(row)
        # 修正 row 索引
        new_map: Dict[bytes, int] = {}
        for u, r in self._uid_row.items():
            new_map[u] = r if r < row else r - 1
        self._uid_row = new_map

    def clear_all(self) -> None:
        """清空所有终端口行（重新扫描前调用）。"""
        self.setRowCount(0)
        self._uid_row.clear()
        self._vserial_map.clear()

    def _on_open_btn(self, logical_addr: int, row: int) -> None:
        btn: QPushButton = self.cellWidget(row, _COL_ACTION)
        if btn is None:
            return

        existing = self._vserial_map.get(logical_addr)
        if existing is None:
            # 开启
            if self._open_fn is not None:
                path = self._open_fn(logical_addr)
                self.setItem(row, _COL_PATH, QTableWidgetItem(path))
                # 获取 VirtualSerialPort 对象，用于统计刷新
                vport = None
                if self._get_vport_fn is not None:
                    vport = self._get_vport_fn(logical_addr)
                self._vserial_map[logical_addr] = vport
                btn.setText("关闭虚拟串口")
        else:
            # 关闭
            if self._close_fn is not None:
                self._close_fn(logical_addr)
            self._vserial_map.pop(logical_addr, None)
            self.setItem(row, _COL_PATH, QTableWidgetItem("—"))
            # 重置收发统计显示
            txrx_item = QTableWidgetItem("0/0")
            txrx_item.setTextAlignment(Qt.AlignCenter)
            self.setItem(row, _COL_TXRX, txrx_item)
            btn.setText("开启虚拟串口")

    @Slot()
    def _refresh_overflow(self) -> None:
        """2 秒定时刷新：TX/RX 字节数和溢出计数。"""
        for uid, row in list(self._uid_row.items()):
            addr_item = self.item(row, _COL_ADDR)
            if addr_item is None:
                continue
            try:
                addr = int(addr_item.text(), 16)
            except ValueError:
                continue
            vport = self._vserial_map.get(addr)
            if vport is None:
                continue
            # TX/RX 字节数
            tx = getattr(vport, 'tx_bytes', 0)
            rx = getattr(vport, 'rx_bytes', 0)
            txrx_item = QTableWidgetItem(f"{tx}/{rx}")
            txrx_item.setTextAlignment(Qt.AlignCenter)
            self.setItem(row, _COL_TXRX, txrx_item)
            # 溢出计数
            ovf = getattr(vport, 'overflow_count', 0)
            ovf_item = QTableWidgetItem(str(ovf))
            ovf_item.setTextAlignment(Qt.AlignCenter)
            self.setItem(row, _COL_OVF, ovf_item)


# ──────────────────────────────────────────────
# 自测
# ──────────────────────────────────────────────

if __name__ == '__main__':
    from dataclasses import dataclass, field

    @dataclass
    class _Dev:
        uid: bytes
        logical_addr: int
        is_terminal: bool = True

        class _PT:
            name = "PORT_485"
        port_type = _PT()
        parent = None

    class _Ev:
        def __init__(self, d): self.device = d

    app = QApplication(sys.argv)

    def fake_open(addr):
        return f"/dev/ttyVCM_{addr:04x}"

    lst = TerminalPortList(open_vserial_fn=fake_open)
    lst.setWindowTitle("TerminalPortList 自测")
    lst.resize(900, 250)
    lst.show()

    for i in range(5):
        d = _Dev(uid=bytes([i]*12), logical_addr=i+1)
        lst.handle_device_found(_Ev(d))

    sys.exit(app.exec())
