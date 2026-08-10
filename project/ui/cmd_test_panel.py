# -*- coding: utf-8 -*-
"""
ui/cmd_test_panel.py — 指令测试面板
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime
from typing import Callable, List, Optional, Tuple

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

logger = logging.getLogger(__name__)


class SkipCommand(Exception):
    """用于标记跳过执行的高风险指令。"""


class CmdResultRow(QWidget):
    """每条指令展示 4 行信息。"""

    send_clicked = Signal(int)  # 发送按钮点击信号，传递行索引

    def __init__(self, cmd_name: str, index: int, parent=None):
        super().__init__(parent)
        self._cmd_name = cmd_name
        self._index = index
        self._board_info_mode = False
        self._date_mode = False
        self._date_tx_mode = False
        self._node_info_mode = False
        self._terminal_port_tx_mode = False
        self._terminal_port_expect_mode = False
        self._jump_program_tx_mode = False
        self._jump_program_expect_mode = False
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(1)

        # 第1行：勾选框 + 指令名称 + 发送按钮 + 测试结论
        h1 = QHBoxLayout()
        self._chk_enable = QCheckBox()
        self._chk_enable.setChecked(True)
        h1.addWidget(self._chk_enable)
        self._lbl_name = QLabel(f"<b>{self._cmd_name}</b>")
        h1.addWidget(self._lbl_name)
        h1.addStretch()
        self._btn_send = QPushButton("发送")
        self._btn_send.setMaximumWidth(60)
        self._btn_send.clicked.connect(lambda: self.send_clicked.emit(self._index))
        h1.addWidget(self._btn_send)
        self._lbl_result = QLabel("未开始")
        self._lbl_result.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self._lbl_result.setMinimumWidth(90)
        h1.addWidget(self._lbl_result)
        layout.addLayout(h1)

        # 第2行：发送指令输入框
        self._h2 = QHBoxLayout()
        self._h2.setContentsMargins(15, 0, 0, 0)
        self._h2.addWidget(QLabel("发送:"))
        self._edit_tx = QLineEdit()
        self._edit_tx.setPlaceholderText("hex格式，如: 01 02 03")
        self._edit_tx.setStyleSheet("font-family: monospace;")
        self._h2.addWidget(self._edit_tx, stretch=1)
        layout.addLayout(self._h2)

        # 第3行：期望输入框
        self._h3 = QHBoxLayout()
        self._h3.setContentsMargins(15, 0, 0, 0)
        self._h3.addWidget(QLabel("期望:"))
        self._edit_expect = QLineEdit()
        self._edit_expect.setPlaceholderText("hex格式，如: 01 xx 03 (xx表示不关心)")
        self._edit_expect.setStyleSheet("font-family: monospace;")
        self._h3.addWidget(self._edit_expect, stretch=1)
        layout.addLayout(self._h3)

        # 第4行：回复数据
        h4 = QHBoxLayout()
        h4.setContentsMargins(15, 0, 0, 0)
        h4.addWidget(QLabel("回复:"))
        self._lbl_rx = QLabel("—")
        self._lbl_rx.setStyleSheet("font-family: monospace; color: #888;")
        self._lbl_rx.setWordWrap(True)
        h4.addWidget(self._lbl_rx, stretch=1)
        layout.addLayout(h4)

        sep = QLabel()
        sep.setFixedHeight(1)
        sep.setStyleSheet("background: #444;")
        layout.addWidget(sep)

    def is_enabled(self) -> bool:
        """返回是否勾选。"""
        return self._chk_enable.isChecked()

    def set_enabled(self, enabled: bool):
        """设置是否勾选。"""
        self._chk_enable.setChecked(enabled)

    def get_tx_data(self) -> str:
        """获取发送数据。"""
        if self._date_tx_mode:
            return self._edit_tx_date.text().strip()
        if self._terminal_port_tx_mode:
            port_no = self._combo_tx_port.currentData()
            is_terminal = self._combo_tx_is_terminal.currentData()
            return f"{port_no:02X} {'01' if is_terminal else '00'}"
        if self._jump_program_tx_mode:
            mode = self._combo_jump_mode.currentData()
            level = self._combo_jump_level.currentData()
            return f"{mode:02X} {level:02X}"
        return self._edit_tx.text().strip()

    def set_tx_data(self, data: str):
        """设置发送数据。"""
        if self._date_tx_mode:
            self._edit_tx_date.setText(data)
        elif self._terminal_port_tx_mode:
            pass  # 由下拉框控制，不支持文本回填
        elif self._jump_program_tx_mode:
            # 格式: "01 00" → mode=0x01, level=0x00
            parts = data.strip().split()
            if len(parts) >= 1:
                try:
                    mode_val = int(parts[0], 16)
                    for i in range(self._combo_jump_mode.count()):
                        if self._combo_jump_mode.itemData(i) == mode_val:
                            self._combo_jump_mode.setCurrentIndex(i)
                            break
                except ValueError:
                    pass
            if len(parts) >= 2:
                try:
                    level_val = int(parts[1], 16)
                    for i in range(self._combo_jump_level.count()):
                        if self._combo_jump_level.itemData(i) == level_val:
                            self._combo_jump_level.setCurrentIndex(i)
                            break
                except ValueError:
                    pass
        else:
            self._edit_tx.setText(data)

    def set_date_tx_mode(self):
        """将发送行替换为日期输入框。"""
        self._date_tx_mode = True
        # 隐藏原输入框
        self._edit_tx.hide()

        # 日期标签和输入框
        self._h2.addWidget(QLabel("测试日期:"))
        self._edit_tx_date = QLineEdit()
        self._edit_tx_date.setPlaceholderText("YYYYMMDD")
        self._edit_tx_date.setMaximumWidth(100)
        self._h2.addWidget(self._edit_tx_date)
        self._h2.addStretch()

    def set_terminal_port_tx_mode(self, port_count: int = 6):
        """将发送行替换为端口下拉框 + 终端口类型下拉框。"""
        self._terminal_port_tx_mode = True
        self._edit_tx.hide()

        self._combo_tx_port = QComboBox()
        for i in range(1, port_count + 1):
            self._combo_tx_port.addItem(f"端口 {i}", i)
        self._combo_tx_port.setMaximumWidth(90)
        self._h2.addWidget(self._combo_tx_port)

        self._combo_tx_is_terminal = QComboBox()
        self._combo_tx_is_terminal.addItem("非终端口", False)
        self._combo_tx_is_terminal.addItem("终端口", True)
        self._combo_tx_is_terminal.setMaximumWidth(90)
        self._h2.addWidget(self._combo_tx_is_terminal)
        self._h2.addStretch()

    def set_jump_program_tx_mode(self):
        """将发送行替换为 boot/app 下拉框 + log level 下拉框。"""
        self._jump_program_tx_mode = True
        self._edit_tx.hide()

        self._combo_jump_mode = QComboBox()
        self._combo_jump_mode.addItem("BOOT", 0x01)
        self._combo_jump_mode.addItem("APP", 0x02)
        self._combo_jump_mode.setMaximumWidth(80)
        self._h2.addWidget(self._combo_jump_mode)

        self._combo_jump_level = QComboBox()
        self._combo_jump_level.addItem("NONE", 0x00)
        self._combo_jump_level.addItem("WARNING", 0x02)
        self._combo_jump_level.addItem("INFO", 0x03)
        self._combo_jump_level.addItem("DEBUG", 0x04)
        self._combo_jump_level.setMaximumWidth(90)
        self._h2.addWidget(self._combo_jump_level)
        self._h2.addStretch()

    def set_jump_program_expect_mode(self):
        """将期望行替换为 boot/app 下拉框 + log level 下拉框（同发送行）。"""
        self._jump_program_expect_mode = True
        self._edit_expect.hide()

        self._combo_exp_jump_mode = QComboBox()
        self._combo_exp_jump_mode.addItem("BOOT", 0x01)
        self._combo_exp_jump_mode.addItem("APP", 0x02)
        self._combo_exp_jump_mode.setMaximumWidth(80)
        self._h3.addWidget(self._combo_exp_jump_mode)

        self._combo_exp_jump_level = QComboBox()
        self._combo_exp_jump_level.addItem("NONE", 0x00)
        self._combo_exp_jump_level.addItem("WARNING", 0x02)
        self._combo_exp_jump_level.addItem("INFO", 0x03)
        self._combo_exp_jump_level.addItem("DEBUG", 0x04)
        self._combo_exp_jump_level.setMaximumWidth(90)
        self._h3.addWidget(self._combo_exp_jump_level)
        self._h3.addStretch()

    def set_date_expect_mode(self):
        """将期望行替换为日期输入框。"""
        self._date_mode = True
        # 隐藏原输入框
        self._edit_expect.hide()

        # 日期标签和输入框
        self._h3.addWidget(QLabel("日期:"))
        self._edit_date = QLineEdit()
        self._edit_date.setPlaceholderText("YYYYMMDD")
        self._edit_date.setMaximumWidth(100)
        self._h3.addWidget(self._edit_date)
        self._h3.addStretch()

    def set_terminal_port_expect_mode(self, port_count: int = 6):
        """将期望行替换为端口下拉框 + 终端口类型下拉框。"""
        self._terminal_port_expect_mode = True
        self._edit_expect.hide()

        self._combo_exp_port = QComboBox()
        for i in range(1, port_count + 1):
            self._combo_exp_port.addItem(f"端口 {i}", i)
        self._combo_exp_port.setMaximumWidth(90)
        self._h3.addWidget(self._combo_exp_port)

        self._combo_exp_is_terminal = QComboBox()
        self._combo_exp_is_terminal.addItem("非终端口", False)
        self._combo_exp_is_terminal.addItem("终端口", True)
        self._combo_exp_is_terminal.setMaximumWidth(90)
        self._h3.addWidget(self._combo_exp_is_terminal)
        self._h3.addStretch()

    def set_board_info_expect_mode(self, model_names: list):
        """将期望行替换为 board_info 专用控件：程序空间+日期+型号。"""
        self._board_info_mode = True
        # 隐藏原输入框
        self._edit_expect.hide()

        # 程序空间下拉框
        self._combo_location = QComboBox()
        self._combo_location.addItem("(不检查)", None)
        self._combo_location.addItem("1 BOOTLOADER", 1)
        self._combo_location.addItem("2 APP", 2)
        self._h3.addWidget(self._combo_location)

        # 日期输入框
        self._edit_date = QLineEdit()
        self._edit_date.setPlaceholderText("日期 YYYYMMDD")
        self._edit_date.setMaximumWidth(100)
        self._h3.addWidget(self._edit_date)

        # 型号下拉框
        self._combo_model = QComboBox()
        self._combo_model.addItem("(不检查)", None)
        for name, val in model_names:
            self._combo_model.addItem(name, val)
        self._h3.addWidget(self._combo_model)
        self._h3.addStretch()

    def set_node_info_expect_mode(self, model_names: list, port_types: list):
        """将期望行替换为 node_info 专用控件：型号+6个端口。"""
        self._node_info_mode = True
        # 隐藏原输入框
        self._edit_expect.hide()

        # 型号下拉框
        self._combo_node_model = QComboBox()
        self._combo_node_model.addItem("(不检查)", None)
        for name, val in model_names:
            self._combo_node_model.addItem(name, val)
        self._h3.addWidget(self._combo_node_model)

        # 6个端口下拉框
        self._combo_ports = []
        for i in range(6):
            combo = QComboBox()
            combo.addItem("(不检查)", None)
            for name, val in port_types:
                combo.addItem(name, val)
            combo.setMaximumWidth(120)
            self._combo_ports.append(combo)
            self._h3.addWidget(combo)
        self._h3.addStretch()

    def get_expect_data(self) -> str:
        """获取期望数据。"""
        if self._date_mode:
            return self._edit_date.text().strip()
        if self._terminal_port_expect_mode:
            port_no = self._combo_exp_port.currentData()
            is_terminal = self._combo_exp_is_terminal.currentData()
            return f"port_no={port_no} is_terminal={int(is_terminal)}"
        if self._jump_program_expect_mode:
            mode = self._combo_exp_jump_mode.currentData()
            level = self._combo_exp_jump_level.currentData()
            return f"mode={mode} level={level}"
        if self._board_info_mode:
            parts = []
            loc = self._combo_location.currentData()
            if loc is not None:
                parts.append(f"location={loc}")
            date = self._edit_date.text().strip()
            if date:
                parts.append(f"date={date}")
            model = self._combo_model.currentData()
            if model is not None:
                parts.append(f"model={model}")
            return " ".join(parts)
        if self._node_info_mode:
            parts = []
            model = self._combo_node_model.currentData()
            if model is not None:
                parts.append(f"model={model}")
            for i, combo in enumerate(self._combo_ports):
                port = combo.currentData()
                if port is not None:
                    parts.append(f"port{i}={port}")
            return " ".join(parts)
        return self._edit_expect.text().strip()

    def set_expect_data(self, data: str):
        """设置期望数据。"""
        if self._jump_program_expect_mode:
            # 格式: "mode=1 level=0"
            for token in data.split():
                if token.startswith("mode="):
                    try:
                        val = int(token.split("=", 1)[1])
                        for i in range(self._combo_exp_jump_mode.count()):
                            if self._combo_exp_jump_mode.itemData(i) == val:
                                self._combo_exp_jump_mode.setCurrentIndex(i)
                                break
                    except ValueError:
                        pass
                elif token.startswith("level="):
                    try:
                        val = int(token.split("=", 1)[1])
                        for i in range(self._combo_exp_jump_level.count()):
                            if self._combo_exp_jump_level.itemData(i) == val:
                                self._combo_exp_jump_level.setCurrentIndex(i)
                                break
                    except ValueError:
                        pass
            return
        if self._date_mode:
            self._edit_date.setText(data)
            return
        if self._board_info_mode:
            # 解析 'location=N date=YYYYMMDD model=NAME' 格式
            for token in data.split():
                if token.startswith("location="):
                    val = int(token.split("=", 1)[1])
                    for i in range(self._combo_location.count()):
                        if self._combo_location.itemData(i) == val:
                            self._combo_location.setCurrentIndex(i)
                            break
                elif token.startswith("date="):
                    self._edit_date.setText(token.split("=", 1)[1])
                elif token.startswith("model="):
                    name = token.split("=", 1)[1]
                    for i in range(self._combo_model.count()):
                        if self._combo_model.itemText(i) == name:
                            self._combo_model.setCurrentIndex(i)
                            break
            return
        if self._node_info_mode:
            # 解析 'model=N port0=N port1=N ...' 格式
            for token in data.split():
                if token.startswith("model="):
                    val = int(token.split("=", 1)[1])
                    for i in range(self._combo_node_model.count()):
                        if self._combo_node_model.itemData(i) == val:
                            self._combo_node_model.setCurrentIndex(i)
                            break
                elif token.startswith("port"):
                    # 解析 port0=N, port1=N, ...
                    port_idx = int(token[4])  # 提取端口索引
                    val = int(token.split("=", 1)[1])
                    for i in range(self._combo_ports[port_idx].count()):
                        if self._combo_ports[port_idx].itemData(i) == val:
                            self._combo_ports[port_idx].setCurrentIndex(i)
                            break
            return
        self._edit_expect.setText(data)

    def is_board_info_mode(self) -> bool:
        return self._board_info_mode

    def is_date_mode(self) -> bool:
        return self._date_mode

    def is_node_info_mode(self) -> bool:
        return self._node_info_mode

    def is_terminal_port_mode(self) -> bool:
        return self._terminal_port_expect_mode

    def is_jump_program_mode(self) -> bool:
        return self._jump_program_expect_mode

    def check_node_info(self, rx: str) -> Optional[str]:
        """对比 rx 字符串与期望值，返回不匹配描述，全部通过返回 None。"""
        mismatches = []
        model_val = self._combo_node_model.currentData()
        if model_val is not None:
            import re
            m = re.search(r'model=(\w+)', rx)
            actual_name = m.group(1) if m else "?"
            model_name = self._combo_node_model.currentText()
            if actual_name != model_name:
                mismatches.append(f"model: 期望={model_name} 实际={actual_name}")
        for i, combo in enumerate(self._combo_ports):
            port_val = combo.currentData()
            if port_val is not None:
                import re
                m = re.search(rf'port{i}=(\w+)', rx)
                actual_name = m.group(1) if m else "?"
                port_name = combo.currentText()
                if actual_name != port_name:
                    mismatches.append(f"port{i}: 期望={port_name} 实际={actual_name}")
        return "\n".join(mismatches) if mismatches else None

    def check_board_info(self, rx: str) -> Optional[str]:
        """对比 rx 字符串与期望值，返回不匹配描述，全部通过返回 None。"""
        mismatches = []
        loc = self._combo_location.currentData()
        if loc is not None:
            import re
            m = re.search(r'location=(\d+)', rx)
            if not m or int(m.group(1)) != loc:
                actual = m.group(1) if m else "?"
                mismatches.append(f"location: 期望={loc} 实际={actual}")
        date = self._edit_date.text().strip()
        if date:
            import re
            m = re.search(r'date=(\d+)', rx)
            if not m or m.group(1) != date:
                actual = m.group(1) if m else "?"
                mismatches.append(f"date: 期望={date} 实际={actual}")
        model_val = self._combo_model.currentData()
        if model_val is not None:
            import re
            m = re.search(r'model=(\w+)', rx)
            actual_name = m.group(1) if m else "?"
            # model_val 是枚举值，需要对比名称
            model_name = self._combo_model.currentText()
            if actual_name != model_name:
                mismatches.append(f"model: 期望={model_name} 实际={actual_name}")
        return "\n".join(mismatches) if mismatches else None

    def reset(self):
        self._lbl_result.setText("未开始")
        self._lbl_result.setStyleSheet("color: #888;")
        self._lbl_rx.setText("—")

    def set_pending(self):
        self._lbl_result.setText("测试中")
        self._lbl_result.setStyleSheet("color: #888;")
        self._lbl_rx.setText("—")

    def set_ok(self, rx: str):
        self._lbl_result.setText("✅ 通过")
        self._lbl_result.setStyleSheet("color: #4CAF50;")
        self._lbl_rx.setText(rx or "(空)")

    def set_err(self, err: str):
        self._lbl_result.setText("❌ 失败")
        self._lbl_result.setStyleSheet("color: #F44336;")
        self._lbl_rx.setText(err)

    def set_skip(self, reason: str):
        self._lbl_result.setText("⏭ 跳过")
        self._lbl_result.setStyleSheet("color: #FFC107;")
        self._lbl_rx.setText(reason)

    @staticmethod
    def compare_hex(expect: str, actual: str) -> bool:
        """
        对比期望和实际的hex数据。
        expect中的'xx'表示不关心该位。
        """
        if not expect:
            return True  # 没有期望值，认为通过

        # 移除空格，转换为大写
        expect_parts = expect.upper().replace(" ", "").split()
        actual_parts = actual.upper().replace(" ", "").split()

        # 如果没有空格分隔，按两个字符一组分割
        if len(expect_parts) == 1 and len(expect) > 2:
            expect_parts = [expect[i:i+2] for i in range(0, len(expect), 2)]
        if len(actual_parts) == 1 and len(actual) > 2:
            actual_parts = [actual[i:i+2] for i in range(0, len(actual), 2)]

        # 长度不匹配
        if len(expect_parts) != len(actual_parts):
            return False

        # 逐字节对比
        for exp, act in zip(expect_parts, actual_parts):
            if exp.upper() != "XX" and exp != act:
                return False

        return True


class CmdTestPanel(QWidget):
    """指令测试面板。"""

    _row_signal: Signal = Signal(int, str, str, str)  # idx, status(ok/err/skip), tx, rx
    _done_signal: Signal = Signal()
    _dev_found_signal: Signal = Signal(object)
    _dev_offline_signal: Signal = Signal(bytes)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._client = None
        self._known_addrs: set = set()
        self._uid_to_addr: dict = {}
        self._pending_node: str = ""
        self._cmd_specs: List[Tuple[str, Callable]] = self._build_cmd_specs()

        self._build_ui()
        self._load_config()

        self._row_signal.connect(self._on_row_result)
        self._done_signal.connect(self._on_done)
        self._dev_found_signal.connect(self._on_dev_found)
        self._dev_offline_signal.connect(self._on_dev_offline)

    def set_client(self, client) -> None:
        self._client = client

    def clear_targets(self) -> None:
        self._combo_node.clear()
        self._edit_path.clear()
        self._known_addrs.clear()
        self._uid_to_addr.clear()

    def handle_device_found(self, event) -> None:
        self._dev_found_signal.emit(event.device)

    def handle_device_offline(self, event) -> None:
        self._dev_offline_signal.emit(event.uid)

    def _load_config(self) -> None:
        """从cmd.json加载指令配置。"""
        config_path = os.path.join(os.path.dirname(__file__), "..", "..", "datas", "cmd.json")
        logger.info("加载指令: %s", os.path.abspath(config_path))
        if not os.path.exists(config_path):
            return

        try:
            with open(config_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)

            # 恢复 测试节点/路由路径 模式及对应值
            target_cfg = data.get("__target__", {})
            mode = target_cfg.get("mode", "测试节点")
            path_value = target_cfg.get("path", "")
            idx_mode = self._combo_target_mode.findText(mode)
            if idx_mode >= 0:
                self._combo_target_mode.setCurrentIndex(idx_mode)
            # 路由路径文本直接恢复
            self._edit_path.setText(path_value)
            # 测试节点：尝试匹配已有项，否则暂存待设备发现后再选中
            node_value = target_cfg.get("node", "")
            self._pending_node = node_value
            if node_value:
                idx_node = self._combo_node.findText(node_value)
                if idx_node >= 0:
                    self._combo_node.setCurrentIndex(idx_node)

            for idx, row in enumerate(self._rows):
                key = str(idx)
                if key in data:
                    tx = data[key].get("tx", "")
                    expect = data[key].get("expect", "")
                    enabled = data[key].get("enabled", True)
                    row.set_tx_data(tx)
                    row.set_expect_data(expect)
                    row.set_enabled(enabled)
        except Exception as e:
            logger.error("加载指令配置失败: %s", e)

    def _save_config(self) -> None:
        """保存指令配置到cmd.json。"""
        config_path = os.path.join(os.path.dirname(__file__), "..", "..", "datas", "cmd.json")

        # 读取现有配置
        data = {}
        if os.path.exists(config_path):
            try:
                with open(config_path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
            except Exception as e:
                logger.warning("读取现有配置失败: %s", e)

        # 保存 测试节点/路由路径 模式及对应值（两者分别保存，互不覆盖）
        mode = self._combo_target_mode.currentText()
        existing_target = data.get("__target__", {})
        node_value = self._combo_node.currentText()
        path_value = self._edit_path.text().strip()
        data["__target__"] = {
            "mode": mode,
            "node": node_value if node_value else existing_target.get("node", ""),
            "path": path_value if path_value else existing_target.get("path", ""),
        }

        # 保存每条指令的配置
        for idx, row in enumerate(self._rows):
            key = str(idx)
            data[key] = {
                "enabled": row.is_enabled(),
                "tx": row.get_tx_data(),
                "expect": row.get_expect_data()
            }

        # 写入文件
        try:
            with open(config_path, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
            logger.debug("指令测试配置已保存: %s", config_path)
        except Exception as e:
            logger.error("保存指令配置失败: %s", e)

    def _build_cmd_specs(self) -> List[Tuple[str, Callable]]:
        return [
            # Upgrade 0x11~0x15
            ("0x11 jump_program", self._run_0x11),
            ("0x12 get_board_info", self._run_0x12),
            ("0x13 send_upgrade_info", self._run_0x13),
            ("0x14 send_upgrade_data", self._run_0x14),
            ("0x15 query_upgrade_progress", self._run_0x15),
            # System 0x21~0x26
            ("0x21 reset", self._run_0x21),
            ("0x22 get_mfg_date", self._run_0x22_get),
            ("0x22 set_mfg_date", self._run_0x22_set),
            ("0x23 scan_nodes", self._run_0x23),
            ("0x24 get_node_info", self._run_0x24),
            ("0x25 get_clear_status", self._run_0x25),
            ("0x26 control_io", self._run_0x26),
            # Port 0x31~0x37
            ("0x31 port_reset", self._run_0x31),
            ("0x32 port_power", self._run_0x32),
            ("0x33 get_port_config", self._run_0x33_get),
            ("0x33 port_config", self._run_0x33_set),
            ("0x34 set_uart_filter", self._run_0x34),
            ("0x35 set_can_filter", self._run_0x35),
            ("0x36 set_logical_addr", self._run_0x36),
            ("0x37 set_terminal_port", self._run_0x37),
        ]

    # -----------------------
    # 各指令 runner
    # -----------------------

    def _run_0x11(self, client, port_path, tx_input=""):
        """0x11 jump_program: mode(01=BOOT/02=APP) + level(00=NONE/02=WARNING/03=INFO/04=DEBUG)"""
        mode = 2
        level = 2
        if tx_input:
            try:
                data = bytes.fromhex(tx_input.replace(" ", ""))
                if len(data) >= 1:
                    mode = data[0]
                if len(data) >= 2:
                    level = data[1]
            except (ValueError, IndexError):
                pass
        tx = f"{mode:02X} {level:02X}"
        frame = client.upgrade_cmd.jump_program(port_path, mode, level)
        rx = frame.data.hex(' ').upper() if getattr(frame, 'data', b'') else ""
        return tx, rx

    def _run_0x12(self, client, port_path, tx_input=""):
        """0x12 get_board_info: 无参数"""
        tx = tx_input if tx_input else ""
        info = client.upgrade_cmd.get_board_info(port_path)
        rx = (f"location={info.location} date={info.mfg_date} "
              f"hw={info.hardware} model={info.model.name} uuid={info.uuid.hex()}")
        return tx, rx

    def _run_0x13(self, client, port_path, tx_input=""):
        # 解析输入框数据获取包数量
        if tx_input:
            try:
                hex_str = tx_input.replace(" ", "")
                data = bytes.fromhex(hex_str)
                if len(data) >= 4:
                    pkg_count = int.from_bytes(data[0:4], 'little')
                else:
                    pkg_count = 16
            except (ValueError, IndexError):
                pkg_count = 16
            tx = tx_input
        else:
            pkg_count = 16
            tx = "10 00 00 00"

        frame = client.upgrade_cmd.send_upgrade_info(port_path, pkg_count)
        rx = frame.data.hex(' ').upper() if getattr(frame, 'data', b'') else ""
        return tx, rx

    def _run_0x14(self, client, port_path, tx_input=""):
        # 解析输入框数据获取序号和payload
        if tx_input:
            try:
                hex_str = tx_input.replace(" ", "")
                data = bytes.fromhex(hex_str)
                if len(data) >= 2:
                    seq = int.from_bytes(data[0:2], 'little')
                    payload = data[2:] if len(data) > 2 else b"\x00" * 8
                else:
                    seq = 1
                    payload = b"\x00" * 8
            except (ValueError, IndexError):
                seq = 1
                payload = b"\x00" * 8
            tx = tx_input
        else:
            seq = 1
            payload = b"\x00" * 8
            tx = "01 00 " + payload.hex(' ').upper()

        frame = client.upgrade_cmd.send_upgrade_data(port_path, seq, payload)
        rx = frame.data.hex(' ').upper() if getattr(frame, 'data', b'') else ""
        return tx, rx

    def _run_0x15(self, client, port_path, tx_input=""):
        frame = client.upgrade_cmd.query_upgrade_progress(port_path)
        tx = tx_input if tx_input else ""
        rx = frame.data.hex(' ').upper() if getattr(frame, 'data', b'') else ""
        return tx, rx

    def _run_0x21(self, client, port_path, tx_input=""):
        frame = client.sys_cmd.reset(port_path)
        tx = tx_input if tx_input else ""
        rx = frame.data.hex(' ').upper() if getattr(frame, 'data', b'') else ""
        return tx, rx

    def _run_0x22_get(self, client, port_path, tx_input=""):
        # get_mfg_date 返回 'YYYYMMDD' 字符串
        date_str = client.sys_cmd.get_mfg_date(port_path)
        tx = tx_input if tx_input else ""
        # 同时以 hex 形式展示原始二进制（day, month, year LE）
        if len(date_str) == 8 and date_str.isdigit():
            year  = int(date_str[0:4])
            month = int(date_str[4:6])
            day   = int(date_str[6:8])
            raw   = bytes([day, month]) + year.to_bytes(2, 'little')
            rx = f"{date_str}  ({raw.hex(' ').upper()})"
        else:
            rx = date_str
        return tx, rx

    def _run_0x22_set(self, client, port_path, tx_input=""):
        # 解析输入框数据：'YYYYMMDD' ASCII 字符串 或 hex格式
        if tx_input:
            try:
                # 先尝试作为 YYYYMMDD 字符串解析
                stripped = tx_input.replace(" ", "")
                if len(stripped) == 8 and stripped.isdigit():
                    date = stripped
                else:
                    # 当作 hex 解析：data0=day, data1=month, data2-3(LSB)=year
                    raw = bytes.fromhex(stripped)
                    day   = raw[0]
                    month = raw[1]
                    year  = int.from_bytes(raw[2:4], 'little')
                    date  = f"{year:04d}{month:02d}{day:02d}"
            except (ValueError, IndexError):
                date = datetime.now().strftime("%Y%m%d")
            tx = tx_input
        else:
            date = datetime.now().strftime("%Y%m%d")
            year  = int(date[0:4])
            month = int(date[4:6])
            day   = int(date[6:8])
            raw   = bytes([day, month]) + year.to_bytes(2, 'little')
            tx = raw.hex(' ').upper()

        frame = client.sys_cmd.set_mfg_date(port_path, date)
        rx = frame.data.hex(' ').upper() if getattr(frame, 'data', b'') else ""
        return tx, rx

    def _run_0x23(self, client, port_path, tx_input=""):
        frame = client.sys_cmd.scan_nodes(port_path)
        tx = tx_input if tx_input else ""
        rx = frame.data.hex(' ').upper() if getattr(frame, 'data', b'') else ""
        return tx, rx

    def _run_0x24(self, client, port_path, tx_input=""):
        node_info = client.sys_cmd.get_node_info(port_path)
        tx = tx_input if tx_input else ""

        # 直接使用 NodeInfo 结构体的属性
        if node_info:
            # 格式化为结构化字符串
            rx = f"model={node_info.model.name}"
            for i, port_type in enumerate(node_info.port_types):
                rx += f" port{i}={port_type.name}"
        else:
            rx = ""

        return tx, rx

    def _run_0x25(self, client, port_path, tx_input=""):
        # 解析输入框数据获取clear标志
        if tx_input:
            try:
                hex_str = tx_input.replace(" ", "")
                data = bytes.fromhex(hex_str)
                clear = bool(data[0]) if len(data) >= 1 else False
            except (ValueError, IndexError):
                clear = False
            tx = tx_input
        else:
            clear = False
            tx = "00"

        frame = client.sys_cmd.get_clear_status(port_path, clear=clear)
        rx = frame.data.hex(' ').upper() if getattr(frame, 'data', b'') else ""
        return tx, rx

    def _run_0x26(self, client, port_path, tx_input=""):
        # 解析输入框数据获取IO控制参数
        if tx_input:
            try:
                hex_str = tx_input.replace(" ", "")
                data = bytes.fromhex(hex_str)
                if len(data) >= 3:
                    io_mask = data[0]
                    mode = data[1]
                    value = data[2]
                else:
                    io_mask, mode, value = 0x01, 0x00, 0x00
            except (ValueError, IndexError):
                io_mask, mode, value = 0x01, 0x00, 0x00
            tx = tx_input
        else:
            io_mask, mode, value = 0x01, 0x00, 0x00
            tx = "01 00 00"

        frame = client.sys_cmd.control_io(port_path, io_mask=io_mask, mode=mode, value=value)
        rx = frame.data.hex(' ').upper() if getattr(frame, 'data', b'') else ""
        return tx, rx

    def _run_0x31(self, client, port_path, tx_input=""):
        # 解析输入框数据获取端口号
        if tx_input:
            try:
                hex_str = tx_input.replace(" ", "")
                data = bytes.fromhex(hex_str)
                port_num = data[0] if len(data) >= 1 else 1
            except (ValueError, IndexError):
                port_num = 1
            tx = tx_input
        else:
            port_num = 1
            tx = "01"

        frame = client.port_cmd.port_reset(port_path, port_num)
        rx = frame.data.hex(' ').upper() if getattr(frame, 'data', b'') else ""
        return tx, rx

    def _run_0x32(self, client, port_path, tx_input=""):
        # 解析输入框数据获取端口号和电源状态
        if tx_input:
            try:
                hex_str = tx_input.replace(" ", "")
                data = bytes.fromhex(hex_str)
                if len(data) >= 2:
                    port_num = data[0]
                    power_on = bool(data[1])
                else:
                    port_num, power_on = 1, True
            except (ValueError, IndexError):
                port_num, power_on = 1, True
            tx = tx_input
        else:
            port_num, power_on = 1, True
            tx = "01 01"

        frame = client.port_cmd.port_power(port_path, port_num, power_on)
        rx = frame.data.hex(' ').upper() if getattr(frame, 'data', b'') else ""
        return tx, rx

    def _run_0x33_get(self, client, port_path, tx_input=""):
        # 解析输入框数据获取端口号
        if tx_input:
            try:
                hex_str = tx_input.replace(" ", "")
                data = bytes.fromhex(hex_str)
                port_num = data[0] if len(data) >= 1 else 1
            except (ValueError, IndexError):
                port_num = 1
            tx = tx_input
        else:
            port_num = 1
            tx = "01"

        frame = client.port_cmd.get_port_config(port_path, port_num)
        rx = frame.data.hex(' ').upper() if getattr(frame, 'data', b'') else ""
        return tx, rx

    def _run_0x33_set(self, client, port_path, tx_input=""):
        # 解析输入框数据获取端口配置参数
        if tx_input:
            try:
                hex_str = tx_input.replace(" ", "")
                data = bytes.fromhex(hex_str)
                if len(data) >= 7:
                    port_num = data[0]
                    baud = int.from_bytes(data[1:5], 'little')
                    parity = data[5]
                    stopbit = data[6]
                else:
                    port_num, baud, parity, stopbit = 1, 115200, 0, 1
            except (ValueError, IndexError):
                port_num, baud, parity, stopbit = 1, 115200, 0, 1
            tx = tx_input
        else:
            port_num, baud, parity, stopbit = 1, 115200, 0, 1
            tx = "01 00 C2 01 00 00 01"

        frame = client.port_cmd.port_config(port_path, port_num, baud, parity, stopbit)
        rx = frame.data.hex(' ').upper() if getattr(frame, 'data', b'') else ""
        return tx, rx

    def _run_0x34(self, client, port_path, tx_input=""):
        # 解析输入框数据获取UART过滤参数
        if tx_input:
            try:
                hex_str = tx_input.replace(" ", "")
                data = bytes.fromhex(hex_str)
                if len(data) >= 2:
                    port_num = data[0]
                    enable = bool(data[1])
                    filter_data = data[2:] if len(data) > 2 else b""
                else:
                    port_num, enable, filter_data = 1, False, b""
            except (ValueError, IndexError):
                port_num, enable, filter_data = 1, False, b""
            tx = tx_input
        else:
            port_num, enable, filter_data = 1, False, b""
            tx = "01 00"

        frame = client.port_cmd.set_uart_filter(port_path, port_num, enable, filter_data)
        rx = frame.data.hex(' ').upper() if getattr(frame, 'data', b'') else ""
        return tx, rx

    def _run_0x35(self, client, port_path, tx_input=""):
        # 解析输入框数据获取CAN过滤参数
        if tx_input:
            try:
                hex_str = tx_input.replace(" ", "")
                data = bytes.fromhex(hex_str)
                if len(data) >= 6:
                    port_num = data[0]
                    filter_id = int.from_bytes(data[1:3], 'little')
                    mask = int.from_bytes(data[3:6], 'little')
                else:
                    port_num, filter_id, mask = 1, 0, 0
            except (ValueError, IndexError):
                port_num, filter_id, mask = 1, 0, 0
            tx = tx_input
        else:
            port_num, filter_id, mask = 1, 0, 0
            tx = "01 00 00 00 00 00"

        frame = client.port_cmd.set_can_filter(port_path, port_num, filter_id, mask)
        rx = frame.data.hex(' ').upper() if getattr(frame, 'data', b'') else ""
        return tx, rx

    def _run_0x36(self, client, port_path, tx_input=""):
        """0x36 set_logical_addr: port_no(1) + addr(2,MSB)，默认不勾选"""
        port_no = 1
        addr = 0x0001
        if tx_input:
            try:
                parts = bytes.fromhex(tx_input.replace(" ", ""))
                if len(parts) >= 1:
                    port_no = parts[0]
                if len(parts) >= 3:
                    addr = (parts[1] << 8) | parts[2]
            except ValueError:
                pass
        tx = f"{port_no:02X} {addr:04X}"
        client.port_cmd.set_logical_addr(port_path, port_no, addr)
        return tx, "OK"

    def _run_0x37(self, client, port_path, tx_input=""):
        """0x37 set_terminal_port: port_no(1) + is_terminal(1)，默认不勾选"""
        port_no = 1
        is_terminal = True
        if tx_input:
            try:
                parts = bytes.fromhex(tx_input.replace(" ", ""))
                if len(parts) >= 1:
                    port_no = parts[0]
                if len(parts) >= 2:
                    is_terminal = bool(parts[1])
            except ValueError:
                pass
        tx = f"{port_no:02X} {'01' if is_terminal else '00'}"
        data = bytes([port_no & 0xFF, 0x01 if is_terminal else 0x00])
        fut = client._session.send_request(port_path, 0x37, data, client.port_cmd._timeout)
        frame = fut.result(timeout=client.port_cmd._timeout + 0.5)
        rx = frame.data.hex(' ').upper() if getattr(frame, 'data', None) else ""
        return tx, rx

    # -----------------------
    # UI
    # -----------------------

    def _build_ui(self):
        layout = QVBoxLayout(self)

        hTop = QHBoxLayout()
        # 模式选择：测试节点 / 路由路径
        self._combo_target_mode = QComboBox()
        self._combo_target_mode.addItems(["测试节点", "路由路径"])
        self._combo_target_mode.setMaximumWidth(100)
        self._combo_target_mode.currentTextChanged.connect(self._on_target_mode_changed)
        hTop.addWidget(self._combo_target_mode)

        # 节点下拉框（测试节点模式显示）
        self._combo_node = QComboBox()
        self._combo_node.setMinimumWidth(200)
        hTop.addWidget(self._combo_node)

        # 路由路径输入框（路由路径模式显示）
        self._edit_path = QLineEdit()
        self._edit_path.setMinimumWidth(200)
        self._edit_path.setPlaceholderText("端口序列，空格分隔，如: 01 02 03")
        self._edit_path.setToolTip(
            "路由路径：用空格分隔的端口号（hex）\n"
            "如单节点 01，多跳 01 02 03，广播 00"
        )
        self._edit_path.setStyleSheet("font-family: monospace;")
        self._edit_path.setVisible(False)
        hTop.addWidget(self._edit_path)

        self._btn_start = QPushButton("开始测试")
        self._btn_start.setMinimumWidth(100)
        self._btn_start.clicked.connect(self._on_start)
        hTop.addWidget(self._btn_start)
        hTop.addStretch()
        layout.addLayout(hTop)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        container = QWidget()
        self._list_layout = QVBoxLayout(container)
        self._list_layout.setContentsMargins(0, 0, 0, 0)
        self._list_layout.setSpacing(0)

        self._rows: List[CmdResultRow] = []
        for idx, (name, _) in enumerate(self._cmd_specs):
            row = CmdResultRow(name, idx, container)
            row.send_clicked.connect(self._on_send_single)
            self._rows.append(row)
            self._list_layout.addWidget(row)

        # 为 0x12 get_board_info 行启用专用期望模式
        idx_0x12 = next((i for i, (n, _) in enumerate(self._cmd_specs) if '0x12' in n), None)
        if idx_0x12 is not None:
            from protocol.models import ModelType
            model_names = [(m.name, m.value) for m in ModelType]
            self._rows[idx_0x12].set_board_info_expect_mode(model_names)

        # 为 0x11 jump_program 行启用 boot/app + log level 下拉
        idx_0x11 = next((i for i, (n, _) in enumerate(self._cmd_specs) if n.startswith('0x11 ')), None)
        if idx_0x11 is not None:
            self._rows[idx_0x11].set_jump_program_tx_mode()
            self._rows[idx_0x11].set_jump_program_expect_mode()

        # 为 0x22 get_mfg_date 行启用日期期望模式
        idx_0x22_get = next((i for i, (n, _) in enumerate(self._cmd_specs) if '0x22 get_mfg_date' in n), None)
        if idx_0x22_get is not None:
            self._rows[idx_0x22_get].set_date_expect_mode()

        # 为 0x22 set_mfg_date 行启用日期发送模式
        idx_0x22_set = next((i for i, (n, _) in enumerate(self._cmd_specs) if '0x22 set_mfg_date' in n), None)
        if idx_0x22_set is not None:
            self._rows[idx_0x22_set].set_date_tx_mode()

        # 为 0x24 get_node_info 行启用节点信息期望模式
        idx_0x24 = next((i for i, (n, _) in enumerate(self._cmd_specs) if '0x24 get_node_info' in n), None)
        if idx_0x24 is not None:
            from protocol.models import ModelType, PortType
            model_names = [(m.name, m.value) for m in ModelType]
            port_types = [(p.name, p.value) for p in PortType]
            self._rows[idx_0x24].set_node_info_expect_mode(model_names, port_types)

        # 0x36 / 0x37 默认不勾选（高风险，但允许用户手动启用）
        for prefix in ('0x36 ', '0x37 '):
            idx = next((i for i, (n, _) in enumerate(self._cmd_specs) if n.startswith(prefix)), None)
            if idx is not None:
                self._rows[idx].set_enabled(False)

        # 0x37 发送行和期望行使用端口+终端口类型下拉框
        idx_0x37 = next((i for i, (n, _) in enumerate(self._cmd_specs) if n.startswith('0x37 ')), None)
        if idx_0x37 is not None:
            self._rows[idx_0x37].set_terminal_port_tx_mode()
            self._rows[idx_0x37].set_terminal_port_expect_mode()

        self._list_layout.addStretch()
        scroll.setWidget(container)
        layout.addWidget(scroll)

    @Slot(str)
    def _on_target_mode_changed(self, mode: str) -> None:
        is_node = (mode == "测试节点")
        self._combo_node.setVisible(is_node)
        self._edit_path.setVisible(not is_node)

    def _get_port_path(self):
        """根据当前模式解析并返回 PortPath，失败抛 ValueError。"""
        from protocol.models import PortPath
        mode = self._combo_target_mode.currentText()
        if mode == "测试节点":
            logical_addr = self._combo_node.currentData()
            if not logical_addr:
                raise ValueError("请先在下拉框中选择测试节点")
            try:
                return self._client._resolve(logical_addr)
            except Exception as e:
                raise ValueError(f"解析节点路径失败: {e}") from e
        else:
            text = self._edit_path.text().strip()
            try:
                ports = [int(x, 16) for x in text.split() if x]
            except ValueError:
                raise ValueError(f"路径格式错误: '{text}'，请使用空格分隔的十六进制端口号")
            return PortPath(ports=ports)

    @Slot(object)
    def _on_dev_found(self, device) -> None:
        addr = device.logical_addr
        if addr == 0 or addr in self._known_addrs or device.is_terminal:
            return
        self._known_addrs.add(addr)
        self._uid_to_addr[device.uid] = addr
        model_str = device.model.name if hasattr(device.model, "name") else str(device.model)
        item_text = f"{model_str} (0x{addr:04X})"
        self._combo_node.addItem(item_text, userData=addr)
        # 如果有待恢复的节点文本，匹配则自动选中
        if self._pending_node and item_text == self._pending_node:
            self._combo_node.setCurrentIndex(self._combo_node.count() - 1)
            self._pending_node = ""

    @Slot(bytes)
    def _on_dev_offline(self, uid: bytes) -> None:
        addr = self._uid_to_addr.pop(uid, None)
        if addr is None:
            return
        self._known_addrs.discard(addr)
        for i in range(self._combo_node.count()):
            if self._combo_node.itemData(i) == addr:
                self._combo_node.removeItem(i)
                break

    @Slot()
    @Slot()
    def _on_start(self):
        if not self._client:
            QMessageBox.warning(self, "提示", "请先连接设备")
            return

        try:
            port_path = self._get_port_path()
        except ValueError as e:
            QMessageBox.warning(self, "提示", str(e))
            return

        # 先清空所有行的测试状态
        for row in self._rows:
            row.reset()

        # 只测试勾选的指令
        enabled_indices = [i for i, row in enumerate(self._rows) if row.is_enabled()]
        if not enabled_indices:
            QMessageBox.warning(self, "提示", "请至少勾选一条指令")
            return

        for i in enabled_indices:
            self._rows[i].set_pending()

        self._btn_start.setEnabled(False)
        threading.Thread(
            target=self._run_all,
            args=(port_path, enabled_indices),
            daemon=True,
            name="CmdTestWorker",
        ).start()

    @Slot(int)
    def _on_send_single(self, idx: int):
        """单独发送某条指令。"""
        if not self._client:
            QMessageBox.warning(self, "提示", "请先连接设备")
            return

        try:
            port_path = self._get_port_path()
        except ValueError as e:
            QMessageBox.warning(self, "提示", str(e))
            return

        self._rows[idx].set_pending()
        threading.Thread(
            target=self._run_single,
            args=(port_path, idx),
            daemon=True,
            name=f"CmdTestWorker-{idx}",
        ).start()

    def _run_all(self, port_path, enabled_indices: List[int]):
        for idx in enabled_indices:
            self._execute_command(idx, port_path)

        # 保存配置
        self._save_config()
        self._done_signal.emit()

    def _run_single(self, port_path, idx: int):
        """执行单条指令。"""
        self._execute_command(idx, port_path)
        # 保存配置
        self._save_config()

    def _execute_command(self, idx: int, port_path):
        """执行指定索引的指令。"""
        row = self._rows[idx]
        _, runner = self._cmd_specs[idx]

        try:
            tx_input = row.get_tx_data()
            tx, rx = runner(self._client, port_path, tx_input)

            if row.is_board_info_mode():
                # 0x12 专用对比：结构化期望
                mismatch = row.check_board_info(rx)
                if mismatch:
                    self._row_signal.emit(idx, "err", tx, f"不匹配: {mismatch}\n实际: {rx}")
                else:
                    self._row_signal.emit(idx, "ok", tx, rx)
            elif row.is_node_info_mode():
                # 0x24 专用对比：结构化期望
                mismatch = row.check_node_info(rx)
                if mismatch:
                    self._row_signal.emit(idx, "err", tx, f"不匹配: {mismatch}\n实际: {rx}")
                else:
                    self._row_signal.emit(idx, "ok", tx, rx)
            elif row.is_terminal_port_mode():
                # 0x37 专用：对比响应帧数据与发送内容
                expect = row.get_expect_data()  # "port_no=N is_terminal=0/1"
                exp_parts = dict(kv.split('=') for kv in expect.split() if '=' in kv)
                exp_port_no = int(exp_parts.get('port_no', 1))
                exp_is_terminal = int(exp_parts.get('is_terminal', 1))
                exp_hex = f"{exp_port_no:02X} {'01' if exp_is_terminal else '00'}"
                if rx and not row.compare_hex(exp_hex, rx):
                    self._row_signal.emit(idx, "err", tx, f"期望: {exp_hex}\n实际: {rx}")
                else:
                    self._row_signal.emit(idx, "ok", tx, rx or "(空)")
            elif row.is_jump_program_mode():
                # 0x11 专用：对比 mode + level
                expect = row.get_expect_data()  # "mode=N level=N"
                exp_parts = dict(kv.split('=') for kv in expect.split() if '=' in kv)
                exp_mode = int(exp_parts.get('mode', 2))
                exp_level = int(exp_parts.get('level', 0))
                exp_hex = f"{exp_mode:02X} {exp_level:02X}"
                if rx and not row.compare_hex(exp_hex, rx):
                    self._row_signal.emit(idx, "err", tx, f"期望: {exp_hex}\n实际: {rx}")
                else:
                    self._row_signal.emit(idx, "ok", tx, rx or "(空)")
            elif row.is_date_mode():
                # 0x22 get_mfg_date 专用对比：日期字符串
                expect = row.get_expect_data()
                if expect:
                    # rx 是 hex 格式，需要转换为 ASCII 字符串
                    try:
                        rx_bytes = bytes.fromhex(rx.replace(" ", ""))
                        rx_date = rx_bytes.decode('ascii')
                        if expect != rx_date:
                            self._row_signal.emit(idx, "err", tx, f"期望: {expect}\n实际: {rx_date}")
                        else:
                            self._row_signal.emit(idx, "ok", tx, rx_date)
                    except (ValueError, UnicodeDecodeError):
                        self._row_signal.emit(idx, "err", tx, f"无法解析日期: {rx}")
                else:
                    # 没有期望值，直接显示结果
                    try:
                        rx_bytes = bytes.fromhex(rx.replace(" ", ""))
                        rx_date = rx_bytes.decode('ascii')
                        self._row_signal.emit(idx, "ok", tx, rx_date)
                    except (ValueError, UnicodeDecodeError):
                        self._row_signal.emit(idx, "ok", tx, rx)
            else:
                expect = row.get_expect_data()
                if expect and not row.compare_hex(expect, rx):
                    self._row_signal.emit(idx, "err", tx, f"期望: {expect}\n实际: {rx}")
                else:
                    self._row_signal.emit(idx, "ok", tx, rx)

        except SkipCommand as e:
            self._row_signal.emit(idx, "skip", "", str(e))
        except Exception as e:
            self._row_signal.emit(idx, "err", "", str(e))

    @Slot(int, str, str, str)
    def _on_row_result(self, idx: int, status: str, tx: str, rx: str):
        if idx >= len(self._rows):
            return
        row = self._rows[idx]
        # 在主线程中更新输入框（如果为空则填充默认值）
        if tx and not row.get_tx_data():
            row.set_tx_data(tx)
        if status == "ok":
            row.set_ok(rx)
        elif status == "skip":
            row.set_skip(rx)
        else:
            row.set_err(rx)

    @Slot()
    def _on_done(self):
        self._btn_start.setEnabled(True)


if __name__ == "__main__":
    import sys

    app = QApplication(sys.argv)
    panel = CmdTestPanel()
    panel.setWindowTitle("指令测试 - 自测")
    panel.resize(680, 800)
    panel.show()
    sys.exit(app.exec())
