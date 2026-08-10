# -*- coding: utf-8 -*-
"""
protocol/models.py — 协议枚举与路径抽象

PortType：端口类型（§1.4）
ModelType：设备型号（§1.5）
PortPath：路由路径封装，携带 has485 标识
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import List


class PortType(IntEnum):
    NONE = 0
    PORT_PC = 1
    PORT_UART = 2
    PORT_FIBRE = 3
    PORT_232 = 4
    PORT_485 = 5
    PORT_CAN = 6


class ModelType(IntEnum):
    BRIDGE_485 = 0
    FIBRE_485 = 1
    UNKNOWN = 2
    BRIDGE_FIBRE = 3
    PC_FIBRE = 0x13
    TERMINAL = 0x80



@dataclass
class PortPath:
    """
    路由路径封装。

    ports   : 路由跳序列，每字节 0x01~0x7F（端口号或 RS485 地址）；0x00 = 广播
    has485  : 是否含 RS485 中间节点（倒数第二位为 rs485Addr 时为 True），
              序列化到 nodes.json，无需遍历元素即可快速判断路径类型

    示例::

        PortPath([1, 2, 3])             # 无 485 节点
        PortPath([1, 2, 5, 3], True)    # 有 485 节点，port[2]=5 为 rs485Addr
        PortPath.broadcast()            # 广播 [0x00]
        PortPath.local()                # 本机 []
    """

    ports: List[int] = field(default_factory=list)
    has485: bool = False

    # ------------------------------------------------------------------
    # 工厂方法
    # ------------------------------------------------------------------

    @staticmethod
    def broadcast() -> PortPath:
        """广播路径 [0x00]。"""
        return PortPath(ports=[0x00])

    @staticmethod
    def local() -> PortPath:
        """本机（空路径）。"""
        return PortPath(ports=[])

    # ------------------------------------------------------------------
    # 容器协议
    # ------------------------------------------------------------------

    def __len__(self) -> int:
        return len(self.ports)

    def __iter__(self):
        return iter(self.ports)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, PortPath):
            return self.ports == other.ports and self.has485 == other.has485
        return NotImplemented

    def __repr__(self) -> str:
        return f"PortPath({self.ports}, has485={self.has485})"

    # ------------------------------------------------------------------
    # 扩展方法
    # ------------------------------------------------------------------

    def append(self, *port_nos: int) -> PortPath:
        """返回追加端口号后的新 PortPath（不修改原对象）。"""
        return PortPath(ports=self.ports + list(port_nos), has485=self.has485)

    def is_broadcast(self) -> bool:
        return len(self.ports) == 1 and self.ports[0] == 0x00

    def is_local(self) -> bool:
        return len(self.ports) == 0
