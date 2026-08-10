# -*- coding: utf-8 -*-
"""
ui/device_tree.py — 设备节点树组件

DeviceTreeView(QTreeWidget) 特性：
  - 订阅 EventBus.DeviceFoundEvent / DeviceOfflineEvent，动态增删节点
  - 每节点显示：型号 / 逻辑地址 / UUID(前8字节) / tx/err 计数
  - 点击节点发射 nodeSelected(Device) 信号，联动右侧详情面板与 TopologyView
  - 所有 EventBus 回调通过 Signal → 主线程执行，禁止跨线程操作 Widget

自测（__main__）：注入模拟设备树，验证节点渲染与点击联动。
"""
from __future__ import annotations

import sys
import threading
from typing import Callable, Dict, Optional

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QHBoxLayout, QPushButton,
    QTreeWidget, QTreeWidgetItem, QWidget,
)

# 延迟导入，避免在无设备管理器时崩溃
try:
    from device.models import Device
    from events.bus import DeviceFoundEvent, DeviceOfflineEvent, NodeDiscoveredEvent
except ImportError:
    Device = None  # type: ignore


class DeviceTreeView(QTreeWidget):
    """
    设备节点树，动态响应 NodeDiscoveredEvent / DeviceFoundEvent / DeviceOfflineEvent。

    信号：
        nodeSelected(Device)  — 用户点击节点时发射

    节点显示两个阶段：
      Phase A 发现后：「[发现中] uuid...」
      Phase C 分配地址后：「0xXXXX uuid...」
    """

    # 主线程信号（来自 EventBus 回调桥接）
    _node_discovered_signal:  Signal = Signal(object)       # NodeDiscoveredEvent
    _device_found_signal:     Signal = Signal(object)       # Device
    _device_offline_signal:   Signal = Signal(bytes)        # uid
    _update_version_signal:   Signal = Signal(object, str)  # (uid, version_str)
    _vserial_opened_signal:   Signal = Signal(object, str)  # (uid, port_name)

    nodeSelected: Signal = Signal(object)   # Device

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setHeaderLabels(["逻辑地址", "UUID", "型号", "路由路径", "版本号/波特率", "升级/开启"])
        self.setAlternatingRowColors(True)
        self.setAnimated(True)
        self.header().setStretchLastSection(False)
        self.setColumnWidth(0, 150)    # 逻辑地址
        self.setColumnWidth(1, 180)   # UUID
        self.setColumnWidth(2, 130)   # 型号列
        self.setColumnWidth(3, 100)   # 路由路径
        self.setColumnWidth(4, 100)   # 版本号/波特率
        self.setColumnWidth(5, 30)   # 升级勾选框/开启按钮

        # 回调函数（由 MainWindow 在连接后注入）
        self._set_baudrate_fn: Optional[Callable] = None
        self._open_vserial_fn: Optional[Callable] = None
        self._close_vserial_fn: Optional[Callable] = None

        self._baud_list = ["1200", "2400", "4800", "9600", "19200", "38400",
                           "57600", "115200", "230400", "460800", "921600", "1000000"]
        self._open_btns: Dict[bytes, QPushButton] = {}
        self._upgrade_checks: Dict[bytes, QCheckBox] = {}

        # 提升“升级”列勾选框在深色背景下的可见性
        self.setStyleSheet("""
            QTreeWidget::indicator {
                width: 16px;
                height: 16px;
                border-radius: 3px;
            }
            QTreeWidget::indicator:unchecked {
                background: #303030;
                border: 2px solid #BDBDBD;
            }
            QTreeWidget::indicator:checked {
                background: #4CAF50;
                border: 2px solid #E8F5E9;
            }
        """)

        # uid → QTreeWidgetItem（仅真实设备，uuid != bytes(12)）
        self._items: Dict[bytes, QTreeWidgetItem] = {}
        # str(ports) → QTreeWidgetItem（所有节点，含终端口 stub）
        self._items_by_path: Dict[str, QTreeWidgetItem] = {}

        self._node_discovered_signal.connect(self._on_node_discovered)
        self._device_found_signal.connect(self._on_device_found)
        self._device_offline_signal.connect(self._on_device_offline)
        self._update_version_signal.connect(self._on_update_version)
        self._vserial_opened_signal.connect(self._on_vserial_opened)
        self.itemClicked.connect(self._on_item_clicked)

    # ──────────────────────────────────────────
    # EventBus 回调（任意线程）
    # ──────────────────────────────────────────

    @staticmethod
    def _model_label(model, is_terminal: bool, port_type=None) -> str:
        """生成型号列显示文本。
        - 中继节点：'中继 BRIDGE_FIBRE'
        - 终端口：'终端口 PORT_485'（用 port_type，而非 model，避免显示 UNKNOWN）
        """
        if is_terminal:
            pt_name = port_type.name if (port_type is not None and hasattr(port_type, 'name')) else str(port_type)
            return f"终端口 {pt_name}"
        name = model.name if hasattr(model, 'name') else str(model)
        return f"中继 {name}"

    def handle_node_discovered(self, event) -> None:
        """订阅 NodeDiscoveredEvent 后由 EventBus 调用。"""
        self._node_discovered_signal.emit(event)

    def handle_device_found(self, event) -> None:
        """订阅 DeviceFoundEvent 后由 EventBus 调用。"""
        self._device_found_signal.emit(event.device)

    def handle_device_offline(self, event) -> None:
        """订阅 DeviceOfflineEvent 后由 EventBus 调用。"""
        self._device_offline_signal.emit(event.uid)

    # ──────────────────────────────────────────
    # 主线程 Slot
    # ──────────────────────────────────────────

    @Slot(object)
    def _on_node_discovered(self, event) -> None:
        """Phase A 发现节点后立即在树中插入占位行。"""
        uid = event.uuid
        path_key = str(list(event.port_path.ports))

        # 幂等
        if uid in self._items or path_key in self._items_by_path:
            return

        item = QTreeWidgetItem()
        uuid_str = uid.hex() if uid != bytes(12) else "-"
        item.setText(0, "[发现中]")
        item.setText(1, uuid_str)  # 非终端口显示 UUID
        item.setText(2, f"{self._model_label(event.model, False)}  发现中...")
        item.setText(3, str(list(event.port_path.ports)))
        item.setText(4, "-")                              # 版本号占位
        item.setData(0, Qt.UserRole, None)   # 无 Device 对象，点击无响应
        self._set_upgrade_checkbox(item, uid)

        parent_item = self._find_parent_item(event.port_path)
        if parent_item is not None:
            parent_item.addChild(item)
            parent_item.setExpanded(True)
        else:
            self.addTopLevelItem(item)

        self._items[uid] = item
        self._items_by_path[path_key] = item

    @Slot(object)
    def _on_device_found(self, device) -> None:
        """Phase C 分配 logicAddr 后更新占位行，或新增终端口 stub 行。"""
        uid = device.uid
        path_key = str(list(device.port_path.ports))
        addr_str = f"0x{device.logical_addr:04X}"
        uuid_str = uid.hex() if uid != bytes(12) else "-"

        existing = self._items.get(uid)
        if existing is not None:
            # 更新 Phase A 占位行
            existing.setText(0, addr_str)
            existing.setText(1, "-" if device.is_terminal else uuid_str)
            existing.setText(2, self._model_label(device.model, device.is_terminal, device.port_type))
            existing.setText(3, str(list(device.port_path.ports)))
            if device.is_terminal:
                self._set_terminal_widgets(existing, device)
            else:
                existing.setText(4, "-")   # 非终端口：待 update_version 填充
            existing.setData(0, Qt.UserRole, device)
            self._items_by_path[path_key] = existing
            return

        # 终端口 stub（uuid=bytes(12)）或 Phase A 未覆盖的情形
        if path_key in self._items_by_path:
            # 已在树中（通过 path 命中），仅补充 Device 引用
            item = self._items_by_path[path_key]
            item.setText(0, addr_str)
            item.setText(1, "-" if device.is_terminal else uuid_str)
            item.setText(2, self._model_label(device.model, device.is_terminal, device.port_type))
            item.setText(3, str(list(device.port_path.ports)))
            if device.is_terminal:
                self._set_terminal_widgets(item, device)
            else:
                item.setText(4, "-")
            item.setData(0, Qt.UserRole, device)
            if uid != bytes(12):
                self._items[uid] = item
            return

        # 全新节点（stub）：直接插入
        item = QTreeWidgetItem()
        item.setText(0, addr_str)
        item.setText(1, "-" if device.is_terminal else uuid_str)
        item.setText(2, self._model_label(device.model, device.is_terminal, device.port_type))
        item.setText(3, str(list(device.port_path.ports)))
        if not device.is_terminal:
            item.setText(4, "-")   # 非终端口：待 update_version 填充
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(5, Qt.Unchecked)   # 升级勾选框
        item.setData(0, Qt.UserRole, device)

        # 优先用 device.parent.uid（Phase C 已建立链接），次用 path 推算
        parent_item = None
        if device.parent is not None:
            parent_item = self._items.get(device.parent.uid)
        if parent_item is None:
            parent_item = self._find_parent_item(device.port_path)

        if parent_item is not None:
            parent_item.addChild(item)
            parent_item.setExpanded(True)
        else:
            self.addTopLevelItem(item)

        if device.is_terminal:
            self._set_terminal_widgets(item, device)

        if uid != bytes(12):
            self._items[uid] = item
        self._items_by_path[path_key] = item

    def _set_terminal_widgets(self, item: QTreeWidgetItem, device) -> None:
        """为终端口行的第4列设置波特率下拉+应用，第5列设置开启按钮。"""
        uid = device.uid
        addr = device.logical_addr

        # 第4列：波特率下拉（选中即发送）
        baud_combo = QComboBox()
        baud_combo.setFixedWidth(90)
        for b in self._baud_list:
            baud_combo.addItem(b)
        baud_combo.setCurrentText("115200")
        baud_combo.currentIndexChanged.connect(
            lambda _idx, a=addr, c=baud_combo: self._on_apply_baud(a, c))
        self.setItemWidget(item, 4, baud_combo)

        # 第5列：开启/关闭虚拟串口按钮
        btn_open = QPushButton("开启")
        btn_open.setFixedWidth(35)
        btn_open.clicked.connect(
            lambda checked=False, a=addr, u=uid, b=btn_open: self._on_open_btn(a, u, b))
        self.setItemWidget(item, 5, btn_open)
        if uid != bytes(12):
            self._open_btns[uid] = btn_open

    def _on_apply_baud(self, logical_addr: int, combo: QComboBox) -> None:
        """应用波特率：在子线程发 0x33。"""
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

    @Slot(object, str)
    def _on_vserial_opened(self, uid: object, port_name: str) -> None:
        """虚拟串口开启后，在第1列显示串口名。"""
        item = self._items.get(uid)  # type: ignore[arg-type]
        if item is not None:
            item.setText(1, port_name if port_name else "-")

    def _on_open_btn(self, logical_addr: int, uid: bytes, btn: QPushButton) -> None:
        """开启/关闭虚拟串口。"""
        if btn.text() == "开启":
            if self._open_vserial_fn is None:
                return
            def _do_open():
                port_name = self._open_vserial_fn(logical_addr)
                # 回到主线程更新 UI
                self._vserial_opened_signal.emit(uid, port_name or "")
            threading.Thread(target=_do_open, daemon=True).start()
            btn.setText("关闭")
        else:
            if self._close_vserial_fn is None:
                return
            threading.Thread(
                target=self._close_vserial_fn,
                args=(logical_addr,),
                daemon=True,
            ).start()
            btn.setText("开启")
            # 清空虚拟串口名
            item = self._items.get(uid)
            if item is not None:
                item.setText(1, "-")

    def _find_parent_item(self, port_path) -> Optional[QTreeWidgetItem]:
        """从 port_path 推导父节点 item。
        
        - 普通节点：父路径 = ports[:-1]
        - RS485 设备（has485=True 且 ports[:-1] 不是真实设备路径）：
          先试 ports[:-1]，未找到再试 ports[:-2]
        """
        ports = list(port_path.ports)
        if not ports:
            return None  # 根节点，无父

        # 第一优先：直接上一级
        parent_key = str(ports[:-1])
        item = self._items_by_path.get(parent_key)
        if item is not None:
            return item

        # RS485 设备：ports[:-1] 是 485 端口路径（非设备），再往上一级
        if getattr(port_path, 'has485', False) and len(ports) >= 2:
            parent_key2 = str(ports[:-2])
            return self._items_by_path.get(parent_key2)

        return None

    @Slot(bytes)
    def _on_device_offline(self, uid: bytes) -> None:
        item = self._items.pop(uid, None)
        if item is None:
            return
        # 同步从 _items_by_path 移除
        keys_to_del = [k for k, v in self._items_by_path.items() if v is item]
        for k in keys_to_del:
            del self._items_by_path[k]
        parent = item.parent()
        if parent is not None:
            parent.removeChild(item)
        else:
            idx = self.indexOfTopLevelItem(item)
            if idx >= 0:
                self.takeTopLevelItem(idx)

    @Slot(object, int)
    def _on_item_clicked(self, item: QTreeWidgetItem, _col: int) -> None:
        device = item.data(0, Qt.UserRole)
        if device is not None:   # 跳过 Phase A 占位行（无 Device 对象）
            self.nodeSelected.emit(device)

    # ──────────────────────────────────────────
    # 公共接口
    # ──────────────────────────────────────────

    def clear_tree(self) -> None:
        self.clear()
        self._items.clear()
        self._items_by_path.clear()

    def select_device(self, uid: bytes) -> None:
        """从外部（TopologyView 点击）联动广播节点。"""
        item = self._items.get(uid)
        if item:
            self.setCurrentItem(item)
            self.scrollToItem(item)

    def update_version(self, uid: bytes, version: str) -> None:
        """从任意线程更新指定设备的版本号（通过 Signal 到主线程）。

        version 格式示例：'20260307_APP' 或 '20260307_BOOT'
        """
        self._update_version_signal.emit(uid, version)

    @Slot(object, str)
    def _on_update_version(self, uid: bytes, version: str) -> None:
        item = self._items.get(uid)
        if item is not None:
            item.setText(4, version)

    def get_upgrade_uids(self) -> list:
        """返回所有勾选了"升级"框的非终端口设备 uid 列表。"""
        result = []
        for uid, item in self._items.items():
            if item.checkState(5) == Qt.Checked:
                device = item.data(0, Qt.UserRole)
                if device is not None and not device.is_terminal:
                    result.append(uid)
        return result


# ──────────────────────────────────────────────
# 自测
# ──────────────────────────────────────────────

if __name__ == '__main__':
    from dataclasses import dataclass, field
    from typing import List

    @dataclass
    class _MockPortPath:
        ports: List
        has485: bool = False

    @dataclass
    class _MockDevice:
        uid: bytes
        logical_addr: int
        is_terminal: bool
        port_path: '_MockPortPath' = field(default_factory=lambda: _MockPortPath([]))
        parent: '_MockDevice | None' = None
        children: List = field(default_factory=list)

        class _M:
            name = "BRIDGE_FIBRE"
        model = _M()

    app = QApplication(sys.argv)
    tree = DeviceTreeView()
    tree.setWindowTitle("DeviceTreeView 自测")
    tree.resize(800, 400)
    tree.show()

    root  = _MockDevice(uid=bytes(12),     logical_addr=0x801, is_terminal=False,
                        port_path=_MockPortPath([1]))
    node1 = _MockDevice(uid=bytes([1]*12), logical_addr=0x802, is_terminal=False, parent=root,
                        port_path=_MockPortPath([1, 2]))
    term1 = _MockDevice(uid=bytes([2]*12), logical_addr=0x001, is_terminal=True,  parent=node1,
                        port_path=_MockPortPath([1, 2, 3]))

    class _Ev:
        def __init__(self, d): self.device = d
    class _OffEv:
        def __init__(self, u): self.uid = u

    tree.handle_device_found(_Ev(root))
    tree.handle_device_found(_Ev(node1))
    tree.handle_device_found(_Ev(term1))

    tree.nodeSelected.connect(lambda d: print(f"选中: 0x{d.logical_addr:04X}"))

    sys.exit(app.exec())
