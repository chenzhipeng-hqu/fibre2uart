# -*- coding: utf-8 -*-
"""
ui/command_panel.py — 单指令发送组件

CommandPanel(QWidget) 特性：
  - 地址模式：逻辑地址 / 路由路径 / 广播
  - 广播模式：地址框固定显示 0x00（禁用输入），fire-and-forget 发送
  - 切换回逻辑地址/路由路径时，从 config.ini 恢复对应地址
  - 发送间隔：每次发送成功/发出后等待指定毫秒再发下一条（0=不等待）
    使用 cancel_event.wait() 实现，支持立即中断
  - 最后一条发送后不等待间隔
  - 广播模式应用 max(user_interval, broadcast_min_interval_ms)
  - 200ms QTimer 防重入（输入错误等快速返回场景）
  - 点击发送后自动保存配置到 config.ini（内容相同则跳过写入）
  - 程序启动时从 config.ini 恢复上次配置
  - set_target() 在广播模式下忽略

自测（__main__）：弹出窗口，注入 MockClient 验证收发流程。
"""
from __future__ import annotations

import os
import sys
import threading
from typing import Any, Optional

from PySide6.QtCore import QTimer, Signal, Slot
from PySide6.QtWidgets import (
    QApplication, QComboBox, QFormLayout, QHBoxLayout, QLabel,
    QLineEdit, QPushButton, QSpinBox, QTextEdit, QVBoxLayout, QWidget,
)


class CommandPanel(QWidget):
    """
    单指令发送面板。

    注入::
        panel.set_client(client)   # FibreNetworkClient 实例
    """

    _result_signal: Signal = Signal(str, str)   # (tag, text)  tag=OK/ERR/INFO
    _unlock_signal: Signal = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._client: Any = None
        self._busy = False
        self._cancel_event = threading.Event()

        self._build_ui()
        self._result_signal.connect(self._on_result)
        self._unlock_signal.connect(self._unlock)

        # 200ms 防重入计时器（仅用于输入错误等快速返回场景）
        self._cooldown = QTimer(self)
        self._cooldown.setSingleShot(True)
        self._cooldown.setInterval(200)
        self._cooldown.timeout.connect(self._unlock)

        self._load_config()

    # ──────────────────────────────────────────
    # 公共接口
    # ──────────────────────────────────────────

    def set_client(self, client) -> None:
        """注入 FibreNetworkClient（connect 后调用）。"""
        self._client = client

    def set_target(self, logical_addr: int) -> None:
        """DeviceTreeView 点击节点后同步逻辑地址；广播模式下忽略。"""
        if self._combo_mode.currentText() == "广播":
            return
        self._edit_addr.setText(f"0x{logical_addr:04X}")

    # ──────────────────────────────────────────
    # 内部 UI 构建
    # ──────────────────────────────────────────

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        form = QFormLayout()

        # 地址模式选择
        addr_row = QHBoxLayout()
        self._combo_mode = QComboBox()
        self._combo_mode.addItems(["逻辑地址", "路由路径", "广播"])
        self._combo_mode.setMaximumWidth(100)
        self._combo_mode.currentTextChanged.connect(self._on_mode_changed)
        addr_row.addWidget(self._combo_mode)

        self._edit_addr = QLineEdit()
        self._edit_addr.setPlaceholderText("0x0801")
        addr_row.addWidget(self._edit_addr)

        form.addRow(addr_row)

        self._edit_cmd = QLineEdit()
        self._edit_cmd.setPlaceholderText("25")
        self._edit_cmd.setToolTip("转发消息：CMD < 0x10\n指令命令：CMD > 0x10")
        self._edit_data = QLineEdit()
        self._edit_data.setPlaceholderText("00  (空格分隔)")
        form.addRow("CMD (hex):", self._edit_cmd)
        form.addRow("Data (hex):", self._edit_data)

        self._spin_count = QSpinBox()
        self._spin_count.setMinimum(1)
        self._spin_count.setMaximum(9999)
        self._spin_count.setValue(1)
        self._spin_count.setToolTip("连续发送次数，适用于压力测试")
        form.addRow("发送次数:", self._spin_count)

        self._spin_interval = QSpinBox()
        self._spin_interval.setMinimum(0)
        self._spin_interval.setMaximum(60000)
        self._spin_interval.setValue(0)
        self._spin_interval.setSuffix(" ms")
        self._spin_interval.setToolTip("每次发送后的等待间隔（毫秒），0=不等待；支持立即中断")
        form.addRow("发送间隔:", self._spin_interval)

        self._btn_send = QPushButton("发 送")
        self._btn_send.clicked.connect(self._on_send)

        self._result_box = QTextEdit()
        self._result_box.setReadOnly(True)
        self._result_box.setPlaceholderText("响应结果将显示在此...")
        self._result_box.setMaximumHeight(200)

        layout.addLayout(form)
        layout.addWidget(self._btn_send)
        layout.addWidget(self._result_box)
        layout.addStretch()

    # ──────────────────────────────────────────
    # 配置持久化
    # ──────────────────────────────────────────

    def _load_config(self) -> None:
        """从 config.ini 恢复上次配置。"""
        try:
            sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            from config import get_config
            cfg = get_config()
            # 地址模式
            idx = self._combo_mode.findText(cfg.command_addr_mode)
            if idx >= 0:
                self._combo_mode.setCurrentIndex(idx)
            # 地址：根据当前模式恢复对应字段
            mode_now = cfg.command_addr_mode
            if mode_now == "逻辑地址" and cfg.command_logical_addr:
                self._edit_addr.setText(cfg.command_logical_addr)
            elif mode_now == "路由路径" and cfg.command_path_addr:
                self._edit_addr.setText(cfg.command_path_addr)
            # 广播模式由 _on_mode_changed 固定显示 0x00，不从 ini 恢复
            # CMD
            if cfg.command_cmd:
                self._edit_cmd.setText(cfg.command_cmd)
            # Data
            self._edit_data.setText(cfg.command_data)
            # 发送次数
            self._spin_count.setValue(cfg.command_count)
            # 发送间隔
            self._spin_interval.setValue(cfg.command_interval_ms)
        except Exception:
            pass  # 配置加载失败不影响程序运行

    def _save_config(self) -> None:
        """将当前配置写入 config.ini（内容相同则跳过）。"""
        try:
            from config import get_config
            cfg = get_config()
            mode = self._combo_mode.currentText()
            cur_text = self._edit_addr.text().strip()
            # 仅更新当前模式对应的地址字段，其余保留已有值
            logical_addr = cfg.command_logical_addr
            path_addr = cfg.command_path_addr
            broadcast_addr = cfg.command_broadcast_addr
            if mode == "逻辑地址":
                logical_addr = cur_text
            elif mode == "路由路径":
                path_addr = cur_text
            # 广播地址固定为 0x00，不修改
            cfg.set_command_config(
                addr_mode=mode,
                logical_addr=logical_addr,
                path_addr=path_addr,
                broadcast_addr=broadcast_addr,
                cmd=self._edit_cmd.text().strip(),
                data=self._edit_data.text().strip(),
                count=self._spin_count.value(),
                interval_ms=self._spin_interval.value(),
            )
        except Exception:
            pass

    # ──────────────────────────────────────────
    # 事件处理
    # ──────────────────────────────────────────

    @Slot(str)
    def _on_mode_changed(self, mode: str) -> None:
        """模式切换时更新地址输入框。"""
        if mode == "逻辑地址":
            self._edit_addr.setEnabled(True)
            self._edit_addr.setPlaceholderText("0x0801")
            try:
                from config import get_config
                val = get_config().command_logical_addr
                self._edit_addr.setText(val if val else "")
            except Exception:
                self._edit_addr.clear()
        elif mode == "路由路径":
            self._edit_addr.setEnabled(True)
            self._edit_addr.setPlaceholderText("1 2 3 (空格分割)")
            try:
                from config import get_config
                val = get_config().command_path_addr
                self._edit_addr.setText(val if val else "")
            except Exception:
                self._edit_addr.clear()
        else:  # 广播
            self._edit_addr.setEnabled(False)
            self._edit_addr.setText("0x00")

    def _on_send(self) -> None:
        if self._client is None:
            return

        # 发送中：点击中断，停止发送
        if self._busy:
            self._cancel_event.set()
            self._result_signal.emit("INFO", "--- 已中断 ---")
            return

        self._cancel_event.clear()
        self._busy = True
        self._btn_send.setText("中 断")

        try:
            mode = self._combo_mode.currentText()
            cmd = int(self._edit_cmd.text().strip(), 16)
            raw = self._edit_data.text().strip()
            data = bytes.fromhex(raw.replace(" ", "")) if raw else b''
            count = self._spin_count.value()
            user_interval_s = self._spin_interval.value() / 1000.0

            if mode == "逻辑地址":
                addr_text = self._edit_addr.text().strip()
                addr = int(addr_text, 16)
                port_path = None
            elif mode == "路由路径":
                addr_text = self._edit_addr.text().strip()
                from protocol.models import PortPath
                parts = addr_text.replace(' ', '.').split('.')
                port_path = PortPath([int(p.strip()) for p in parts if p.strip()])
                addr = None
            else:  # 广播
                from protocol.models import PortPath
                port_path = PortPath.broadcast()
                addr = None
                # 广播应用最小间隔
                try:
                    from config import get_config
                    min_ms = get_config().broadcast_min_interval_ms
                except Exception:
                    min_ms = 10
                user_interval_s = max(user_interval_s, min_ms / 1000.0)
        except ValueError as e:
            self._result_signal.emit("ERR", f"输入格式错误: {e}")
            self._unlock()
            return

        # 保存配置
        self._save_config()

        # 异步发送，不阻塞 UI
        cancel = self._cancel_event
        client = self._client  # 局部引用，类型窄化为非 None

        def _send():
            try:
                for i in range(count):
                    if cancel.is_set():
                        break
                    try:
                        if mode == "广播":
                            # fire-and-forget：不等响应
                            client.send_broadcast(cmd, data)
                            prefix = f"[{i+1}/{count}] " if count > 1 else ""
                            self._result_signal.emit("OK", prefix + "广播已发送")
                        elif mode == "逻辑地址":
                            fut = client.send_cmd(addr, cmd, data)
                            frame = fut.result(timeout=2.0)
                            if cancel.is_set():
                                break
                            prefix = f"[{i+1}/{count}] " if count > 1 else ""
                            self._result_signal.emit("OK", prefix + (frame.data.hex() or "(空)"))
                        else:  # 路由路径
                            fut = client._session.send_request(port_path, cmd, data)
                            frame = fut.result(timeout=2.0)
                            if cancel.is_set():
                                break
                            prefix = f"[{i+1}/{count}] " if count > 1 else ""
                            self._result_signal.emit("OK", prefix + (frame.data.hex() or "(空)"))
                    except Exception as ex:
                        if cancel.is_set():
                            break
                        prefix = f"[{i+1}/{count}] " if count > 1 else ""
                        self._result_signal.emit("ERR", prefix + str(ex))

                    # 发送间隔：非最后一条时等待，支持立即中断
                    if user_interval_s > 0 and i < count - 1:
                        cancel.wait(user_interval_s)
            finally:
                self._unlock_signal.emit()

        threading.Thread(target=_send, daemon=True).start()

    @Slot(str, str)
    def _on_result(self, tag: str, text: str) -> None:
        if tag == "INFO":
            color = "#9E9E9E"
        elif tag == "OK":
            color = "#4CAF50"
        else:
            color = "#F44336"
        html = (f'<span style="color:{color};">[{tag}]</span> '
                f'<span style="font-family:monospace;">{text}</span>')
        self._result_box.append(html)

    @Slot()
    def _unlock(self) -> None:
        self._busy = False
        self._btn_send.setText("发 送")
        self._btn_send.setEnabled(True)


# ──────────────────────────────────────────────
# 自测
# ──────────────────────────────────────────────

if __name__ == '__main__':
    from concurrent.futures import Future
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from frame.models import CommandFrame, SOF_RX

    class _MockTransport:
        def send(self, raw: bytes) -> None:
            print(f"[MockTransport] send {raw.hex()}")

    class _MockSession:
        _transport = _MockTransport()

        def _build_frame(self, port_path, cmd, data) -> bytes:
            return b'\xAA' + bytes([cmd]) + data

        def send_request(self, port_path, cmd, data, timeout=2.0):
            fut = Future()
            frame = CommandFrame(SOF_RX, 0, 0, cmd, 0, [], b'\xDE\xAD')
            fut.set_result(frame)
            return fut

    class _MockClient:
        _session = _MockSession()

        def send_cmd(self, addr, cmd, data):
            fut = Future()
            frame = CommandFrame(SOF_RX, 0, 0, cmd, 0, [], b'\xDE\xAD')
            fut.set_result(frame)
            return fut

    app = QApplication(sys.argv)
    panel = CommandPanel()
    panel.set_client(_MockClient())
    panel.setWindowTitle("CommandPanel 自测")
    panel.resize(400, 350)
    panel.show()
    sys.exit(app.exec())
