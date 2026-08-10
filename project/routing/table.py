# -*- coding: utf-8 -*-
"""
routing/table.py — PC 端路由表

PCLogicRoutingTable : logicAddr(uint16) → PortPath  （主路由表，发送时寻址）
PCUIDRoutingTable   : UID(bytes) → PortPath          （备用表，设备唯一标识寻址）

两张表均线程安全（读写锁语义用 threading.Lock 实现）。
"""
from __future__ import annotations

import threading
from typing import Dict, List, Optional, Tuple

from protocol.models import PortPath


class PCLogicRoutingTable:
    """
    logicAddr → PortPath 路由表。

    - 节点设备地址范围: 0x801~
    - 终端口地址范围  : 0x001~0x7FF
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._table: Dict[int, PortPath] = {}

    def add(self, logical_addr: int, port_path: PortPath) -> None:
        """添加或更新路由条目。"""
        with self._lock:
            self._table[logical_addr] = port_path

    def lookup(self, logical_addr: int) -> Optional[PortPath]:
        """按逻辑地址查找路径；未找到返回 None。"""
        with self._lock:
            return self._table.get(logical_addr)

    def remove(self, logical_addr: int) -> None:
        """移除路由条目（设备离线时调用）。"""
        with self._lock:
            self._table.pop(logical_addr, None)

    def clear(self) -> None:
        """清空路由表（重新发现时调用）。"""
        with self._lock:
            self._table.clear()

    def all_entries(self) -> List[Tuple[int, PortPath]]:
        """返回所有 (logical_addr, port_path) 条目快照（用于序列化/UI 展示）。"""
        with self._lock:
            return list(self._table.items())

    def __len__(self) -> int:
        with self._lock:
            return len(self._table)

    def __contains__(self, logical_addr: int) -> bool:
        with self._lock:
            return logical_addr in self._table


class PCUIDRoutingTable:
    """
    UID(bytes) → PortPath 备用路由表。

    UID 为 12 字节设备唯一标识，在设备发现时同步建表，
    用于 logicAddr 被重新分配后仍能通过 UUID 定位设备。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._table: Dict[bytes, PortPath] = {}

    def add(self, uid: bytes, port_path: PortPath) -> None:
        """添加或更新 UID 路由条目。"""
        with self._lock:
            self._table[uid] = port_path

    def lookup(self, uid: bytes) -> Optional[PortPath]:
        """按 UID 查找路径；未找到返回 None。"""
        with self._lock:
            return self._table.get(uid)

    def remove(self, uid: bytes) -> None:
        """移除 UID 路由条目。"""
        with self._lock:
            self._table.pop(uid, None)

    def clear(self) -> None:
        """清空表。"""
        with self._lock:
            self._table.clear()

    def all_entries(self) -> List[Tuple[bytes, PortPath]]:
        """返回所有 (uid, port_path) 条目快照。"""
        with self._lock:
            return list(self._table.items())

    def __len__(self) -> int:
        with self._lock:
            return len(self._table)
