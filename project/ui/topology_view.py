# -*- coding: utf-8 -*-
"""
ui/topology_view.py — 设备拓扑图组件（QGraphicsView）

TopologyView 特性：
  - 以节点-边图形式展示设备拓扑（节点=设备圆形，边=连线）
  - 订阅 DeviceFoundEvent / DeviceOfflineEvent 动态增删节点/边
  - 点击节点发射 nodeSelected(uid) 信号，联动 DeviceTreeView 与右侧详情
  - 支持鼠标拖拽移动节点、滚轮缩放画布
  - 布局：根节点居左，子节点向右递增 X 坐标，同深度按索引分配 Y 坐标

自测（__main__）：注入模拟设备树，验证图形渲染与交互。
"""
from __future__ import annotations

import sys
from typing import Dict, List, Optional, Tuple

from PySide6.QtCore import QRectF, Qt, Signal, Slot
from PySide6.QtGui import (QBrush, QColor, QFont, QPainter, QPen, QWheelEvent)
from PySide6.QtWidgets import (
    QApplication, QGraphicsEllipseItem, QGraphicsItem,
    QGraphicsLineItem, QGraphicsScene, QGraphicsSimpleTextItem,
    QGraphicsView, QWidget,
)

# 节点尺寸与布局间距
NODE_R = 28        # 节点圆半径（px）
LEVEL_W = 160      # 每级间距
SIBLING_H = 70     # 同级节点间距

# 节点颜色
_COLOR_RELAY    = QColor("#1565C0")   # 中继节点（蓝）
_COLOR_TERMINAL = QColor("#2E7D32")   # 终端口（绿）
_COLOR_OFFLINE  = QColor("#555555")   # 离线（灰）
_COLOR_TEXT     = QColor("#FFFFFF")


class _NodeItem(QGraphicsEllipseItem):
    """可交互的节点圆形图元。"""

    def __init__(self, uid: bytes, label: str,
                 is_terminal: bool, scene: QGraphicsScene) -> None:
        super().__init__(-NODE_R, -NODE_R, NODE_R * 2, NODE_R * 2)
        self.uid = uid
        self.setFlag(QGraphicsItem.ItemIsMovable)
        self.setFlag(QGraphicsItem.ItemSendsGeometryChanges)
        self.setAcceptHoverEvents(True)

        color = _COLOR_TERMINAL if is_terminal else _COLOR_RELAY
        self.setBrush(QBrush(color))
        self.setPen(QPen(Qt.white, 1.5))
        self.setZValue(2)

        # 标签
        self._text = QGraphicsSimpleTextItem(label, self)
        self._text.setBrush(QBrush(_COLOR_TEXT))
        br = self._text.boundingRect()
        self._text.setPos(-br.width() / 2, -br.height() / 2)

    def hoverEnterEvent(self, event) -> None:
        self.setPen(QPen(QColor("#FFC107"), 2.5))
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event) -> None:
        self.setPen(QPen(Qt.white, 1.5))
        super().hoverLeaveEvent(event)


class TopologyView(QGraphicsView):
    """
    拓扑图视图。

    信号：
        nodeSelected(uid: bytes)  — 用户点击节点时发射
    """

    _device_found_signal:   Signal = Signal(object)   # Device
    _device_offline_signal: Signal = Signal(bytes)    # uid

    nodeSelected: Signal = Signal(bytes)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self.setRenderHint(QPainter.Antialiasing)
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.setStyleSheet("background-color: #263238;")
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)

        # uid → _NodeItem
        self._nodes: Dict[bytes, _NodeItem] = {}
        # (parent_uid, child_uid) → [QGraphicsLineItem, QGraphicsSimpleTextItem]
        self._edges: Dict[Tuple[bytes, bytes], list] = {}

        # 端口号标签字体（小号）
        self._port_font = QFont()
        self._port_font.setPointSize(7)

        self._device_found_signal.connect(self._on_device_found)
        self._device_offline_signal.connect(self._on_device_offline)

        # 深度 → 已用 Y 偏移槽位数（用于自动布局）
        self._depth_slot: Dict[int, int] = {}

    # ──────────────────────────────────────────
    # EventBus 回调（任意线程）
    # ──────────────────────────────────────────

    def handle_device_found(self, event) -> None:
        self._device_found_signal.emit(event.device)

    def handle_device_offline(self, event) -> None:
        self._device_offline_signal.emit(event.uid)

    # ──────────────────────────────────────────
    # 主线程 Slot
    # ──────────────────────────────────────────

    @Slot(object)
    def _on_device_found(self, device) -> None:
        uid = device.uid
        if uid in self._nodes:
            return

        depth = device.depth() if hasattr(device, 'depth') else 0
        slot = self._depth_slot.get(depth, 0)
        x = depth * LEVEL_W + NODE_R + 20
        y = slot * SIBLING_H + NODE_R + 20
        self._depth_slot[depth] = slot + 1

        label = f"0x{device.logical_addr:04X}"
        node = _NodeItem(uid, label, device.is_terminal, self._scene)
        node.setPos(x, y)
        self._scene.addItem(node)
        self._nodes[uid] = node

        # 父节点连线 + 端口号标签
        if device.parent is not None:
            parent_uid = device.parent.uid
            parent_node = self._nodes.get(parent_uid)
            if parent_node is not None:
                px, py = parent_node.pos().x(), parent_node.pos().y()
                line = QGraphicsLineItem(px, py, x, y)
                line.setPen(QPen(QColor("#78909C"), 1.5))
                line.setZValue(1)
                self._scene.addItem(line)

                # 端口号标签：显示在靠近父节点 25% 处
                ports = device.port_path.ports if hasattr(device, 'port_path') else []
                port_no = ports[-1] if ports else 0
                if port_no:
                    # 判断是 485 地址还是普通端口号
                    conn_type = getattr(device, 'port_type', None)
                    conn_name = conn_type.name if (conn_type and hasattr(conn_type, 'name')) else ''
                    if 'PORT_485' in conn_name:
                        lbl_text = f"485:{port_no}"
                    else:
                        lbl_text = f"P{port_no}"
                    lx = px + (x - px) * 0.22 - 10
                    ly = py + (y - py) * 0.22 - 8
                    port_lbl = QGraphicsSimpleTextItem(lbl_text)
                    port_lbl.setFont(self._port_font)
                    port_lbl.setBrush(QBrush(QColor("#FFC107")))
                    port_lbl.setPos(lx, ly)
                    port_lbl.setZValue(3)
                    self._scene.addItem(port_lbl)
                    self._edges[(parent_uid, uid)] = [line, port_lbl]
                else:
                    self._edges[(parent_uid, uid)] = [line]

    @Slot(bytes)
    def _on_device_offline(self, uid: bytes) -> None:
        node = self._nodes.pop(uid, None)
        if node:
            node.setBrush(QBrush(_COLOR_OFFLINE))

    # ──────────────────────────────────────────
    # 鼠标事件
    # ──────────────────────────────────────────

    def mousePressEvent(self, event) -> None:
        item = self.itemAt(event.pos())
        if isinstance(item, (_NodeItem, QGraphicsSimpleTextItem)):
            target = item if isinstance(item, _NodeItem) else item.parentItem()
            if isinstance(target, _NodeItem):
                self.nodeSelected.emit(target.uid)
        super().mousePressEvent(event)

    def wheelEvent(self, event: QWheelEvent) -> None:
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        self.scale(factor, factor)

    # ──────────────────────────────────────────
    # 外部联动
    # ──────────────────────────────────────────

    def select_node(self, uid: bytes) -> None:
        """DeviceTreeView 点击后联动高亮拓扑图节点。"""
        for u, node in self._nodes.items():
            node.setPen(QPen(QColor("#FFC107") if u == uid else Qt.white,
                             2.5 if u == uid else 1.5))

    def clear_topology(self) -> None:
        self._scene.clear()
        self._nodes.clear()
        self._edges.clear()
        self._depth_slot.clear()


# ──────────────────────────────────────────────
# 自测
# ──────────────────────────────────────────────

if __name__ == '__main__':
    from dataclasses import dataclass, field
    from typing import List as TList

    @dataclass
    class _Dev:
        uid: bytes
        logical_addr: int
        is_terminal: bool
        parent: '_Dev | None' = None
        children: TList = field(default_factory=list)
        port_types: TList = field(default_factory=list)

        class _M: name = "BRIDGE_FIBRE"
        model = _M()

        class _PT:
            name = "PORT_FIBRE"
            value = 3
        port_type = _PT()

        # 每实例通过构造后赋值（见下方创建代码）
        port_path: object = None

        def depth(self):
            d, n = 0, self.parent
            while n: d += 1; n = n.parent
            return d

    class _PP:
        """极简 PortPath stub。"""
        def __init__(self, ports, has485=False):
            self.ports = ports
            self.has485 = has485

    class _Ev:
        def __init__(self, d): self.device = d

    app = QApplication(sys.argv)
    view = TopologyView()
    view.setWindowTitle("TopologyView 自测")
    view.resize(800, 500)
    view.show()

    root  = _Dev(uid=bytes(12),       logical_addr=0x801, is_terminal=False)
    n1    = _Dev(uid=bytes([1]*12),   logical_addr=0x802, is_terminal=False, parent=root)
    n2    = _Dev(uid=bytes([2]*12),   logical_addr=0x803, is_terminal=False, parent=root)
    t1    = _Dev(uid=bytes([3]*12),   logical_addr=0x001, is_terminal=True, parent=n1)
    t2    = _Dev(uid=bytes([4]*12),   logical_addr=0x002, is_terminal=True, parent=n2)

    root.port_path = _PP([])
    n1.port_path   = _PP([1])
    n2.port_path   = _PP([2])
    t1.port_path   = _PP([1, 3])
    t2.port_path   = _PP([2, 1])

    for dev in (root, n1, n2, t1, t2):
        view.handle_device_found(_Ev(dev))

    view.nodeSelected.connect(lambda uid: print(f"拓扑点击: {uid.hex()[:8]}..."))
    sys.exit(app.exec())
