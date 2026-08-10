# -*- coding: utf-8 -*-
"""
device/models.py — 设备数据模型

Device   : 设备节点（树形结构）
BoardInfo: 0x12 指令返回的板卡信息
NodeInfo : 0x24 指令返回的节点信息（发现阶段临时数据）

终端口判断规则（§零 终端口判断规则）：
  - portType == PORT_232 或 PORT_CAN                  → 终端口
  - portType == PORT_485 且无下级节点（has_children=False） → 终端口
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from protocol.models import ModelType, PortPath, PortType


def is_terminal_port(port_type: PortType, has_children: bool) -> bool:
    """
    判断端口是否为终端口。

    :param port_type:    端口类型
    :param has_children: 是否已发现下级节点
    :return:             True 表示终端口
    """
    if port_type in (PortType.PORT_232, PortType.PORT_CAN):
        return True
    if port_type == PortType.PORT_485 and not has_children:
        return True
    return False


@dataclass
class BoardInfo:
    """
    0x12 get_board_info 返回的板卡信息。

    location : 程序位置 1=bootloader / 2=app
    mfg_date : 出厂日期字符串，如 "20250101"
    hardware : 硬件版本号
    model    : 设备型号
    uuid     : 12 字节唯一标识
    """
    location: int = 0
    mfg_date: str = ''
    hardware: int = 0
    model: ModelType = ModelType.UNKNOWN
    uuid: bytes = b''

    def __repr__(self) -> str:
        return (
            f"BoardInfo(loc={self.location}, date={self.mfg_date!r}, "
            f"hw={self.hardware}, model={self.model.name}, uuid={self.uuid.hex()})"
        )


@dataclass
class NodeInfo:
    """
    0x24 get_node_info 返回的节点信息，发现阶段使用，不持久化。

    uuid       : 12 字节唯一标识
    model      : 设备型号
    port_types : 6 个端口的端口类型列表（index 0~5 对应物理端口 1~6）
    """
    uuid: bytes
    model: ModelType
    port_types: List[PortType] = field(default_factory=list)

    def __repr__(self) -> str:
        return (
            f"NodeInfo(uuid={self.uuid.hex()}, model={self.model.name}, "
            f"port_types={[t.name for t in self.port_types]})"
        )


@dataclass
class Device:
    """
    设备节点（树形结构）。

    uid          : 12 字节 UUID
    model        : 设备型号
    port_type    : 该设备连接父节点时使用的端口类型
    port_path    : 从 PC 到达该设备的完整路由路径
    logical_addr : 逻辑地址（节点 0x801~，终端口 0x001~）
    is_terminal  : 是否为终端口
    children     : 6 个子端口对应的子设备（下级节点或终端口）
    parent       : 父节点（根节点为 None）
    board_info   : 板卡信息（get_board_info 后填充，发现阶段可为 None）
    """
    uid: bytes
    model: ModelType
    port_type: PortType
    port_path: PortPath
    logical_addr: int = 0
    is_terminal: bool = False
    port_types: List[PortType] = field(default_factory=list)   # 本设备 6 个端口的类型（来自 0x24 NodeInfo）
    children: List[Device] = field(default_factory=list)
    parent: Optional[Device] = field(default=None, repr=False, compare=False)
    board_info: Optional[BoardInfo] = None

    def add_child(self, child: Device) -> None:
        """添加子节点，同时设置子节点的 parent 引用。"""
        child.parent = self
        self.children.append(child)

    def remove_child(self, uid: bytes) -> Optional[Device]:
        """按 uid 移除子节点，返回被移除的节点；未找到返回 None。"""
        for i, child in enumerate(self.children):
            if child.uid == uid:
                self.children.pop(i)
                return child
        return None

    def depth(self) -> int:
        """返回节点在树中的深度（根节点为 0）。"""
        d = 0
        node = self.parent
        while node is not None:
            d += 1
            node = node.parent
        return d

    def __repr__(self) -> str:
        return (
            f"Device(uid={self.uid.hex()}, model={self.model.name}, "
            f"logical_addr=0x{self.logical_addr:04X}, "
            f"is_terminal={self.is_terminal}, children={len(self.children)})"
        )
