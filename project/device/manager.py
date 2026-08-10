# -*- coding: utf-8 -*-
"""
device/manager.py — 设备管理器

DeviceManager：
  - 维护设备集合（uid → Device 和 logical_addr → Device 双索引）
  - 管理设备树根节点
  - 线程安全读写（threading.Lock）
"""
from __future__ import annotations

import threading
from typing import Dict, List, Optional

from device.models import Device


class DeviceManager:
    """
    设备管理器：维护设备对象的双索引（uid 和 logical_addr）。

    - 发现阶段调用 add_device() 注册设备
    - 设备离线时调用 remove_device() 清理
    - UI/路由层通过 find_by_uid() / find_by_logical_addr() 查询
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_uid: Dict[bytes, Device] = {}
        self._by_addr: Dict[int, Device] = {}
        self._root: Optional[Device] = None

    # ------------------------------------------------------------------
    # 写操作
    # ------------------------------------------------------------------

    def add_device(self, device: Device) -> None:
        """
        注册设备。若 logical_addr > 0x800 且当前无根节点则设为根节点。
        """
        with self._lock:
            self._by_uid[device.uid] = device
            if device.logical_addr != 0:
                self._by_addr[device.logical_addr] = device
            # 根节点：PC_FIBRE 型号或 logical_addr == 0x801
            if self._root is None and device.logical_addr == 0x801:
                self._root = device

    def remove_device(self, uid: bytes) -> Optional[Device]:
        """
        移除设备及其子树（递归），返回被移除的根设备。
        """
        with self._lock:
            device = self._by_uid.pop(uid, None)
            if device is None:
                return None
            # 递归移除子树
            self._remove_subtree(device)
            # 从父节点摘除
            if device.parent is not None:
                device.parent.remove_child(uid)
            return device

    def _remove_subtree(self, device: Device) -> None:
        """递归移除子树（调用时应已持锁）。"""
        for child in list(device.children):
            self._by_uid.pop(child.uid, None)
            if child.logical_addr != 0:
                self._by_addr.pop(child.logical_addr, None)
            self._remove_subtree(child)
        if device.logical_addr != 0:
            self._by_addr.pop(device.logical_addr, None)

    def update_logical_addr(self, uid: bytes, new_addr: int) -> None:
        """更新设备的 logicAddr（发现阶段阶段 C 写入后调用）。"""
        with self._lock:
            device = self._by_uid.get(uid)
            if device is None:
                return
            # 移除旧地址索引
            if device.logical_addr != 0:
                self._by_addr.pop(device.logical_addr, None)
            device.logical_addr = new_addr
            self._by_addr[new_addr] = device

    def clear(self) -> None:
        """清空全部设备（重新发现前调用）。"""
        with self._lock:
            self._by_uid.clear()
            self._by_addr.clear()
            self._root = None

    # ------------------------------------------------------------------
    # 读操作
    # ------------------------------------------------------------------

    def find_by_uid(self, uid: bytes) -> Optional[Device]:
        with self._lock:
            return self._by_uid.get(uid)

    def find_by_logical_addr(self, addr: int) -> Optional[Device]:
        with self._lock:
            return self._by_addr.get(addr)

    def get_root(self) -> Optional[Device]:
        """返回树的根节点（PC_FIBRE，logical_addr=0x801）。"""
        with self._lock:
            return self._root

    def set_root(self, device: Device) -> None:
        """手动设置根节点（发现阶段调用）。"""
        with self._lock:
            self._root = device

    def all_devices(self) -> List[Device]:
        """返回所有已注册设备的快照列表。"""
        with self._lock:
            return list(self._by_uid.values())

    def terminal_devices(self) -> List[Device]:
        """返回所有终端口设备快照。"""
        with self._lock:
            return [d for d in self._by_uid.values() if d.is_terminal]

    def __len__(self) -> int:
        with self._lock:
            return len(self._by_uid)
