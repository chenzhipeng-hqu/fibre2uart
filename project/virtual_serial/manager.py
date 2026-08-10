# -*- coding: utf-8 -*-
"""
virtual_serial/manager.py — 虚拟串口管理器

VirtualSerialManager 职责：
  - create(logical_addr, port_type, baud) → VirtualSerialPort（开口 + 注册）
  - close(logical_addr)                   → 关闭并注销
  - dispatch(frame: CommandFrame)         → cmd<0x10 消息帧 → write_to_pty
  - on_device_offline(uid)               → 关闭对应虚拟串口

线程安全：所有写操作通过 threading.Lock 保护，读操作（dispatch）直接 snapshot 不持锁。
"""
from __future__ import annotations

import logging
import threading
from typing import Callable, Dict, List, Optional

from frame.models import CommandFrame
from protocol.models import PortPath, PortType
from virtual_serial.port import VirtualSerialPort

logger = logging.getLogger(__name__)


class VirtualSerialManager:
    """
    虚拟串口生命周期管理。

    典型用法::

        mgr = VirtualSerialManager(logic_table, transport.send,
                                   CommandCodec.encode, seq_mgr.allocate)
        path = mgr.create(0x001, PortType.PORT_485, 115200)
        print(f"第三方软件打开: {path}")

        # 收到下行消息帧
        mgr.dispatch(frame)

        # 设备离线
        mgr.close(0x001)
    """

    def __init__(
        self,
        logic_table,                          # PCLogicRoutingTable
        transport_send: Callable[[bytes], None],
        encode_fn: Callable,                  # CommandCodec.encode
        seq_allocate: Callable[[], int],      # SeqManager.allocate
    ) -> None:
        self._logic_table = logic_table
        self._transport_send = transport_send
        self._encode_fn = encode_fn
        self._seq_allocate = seq_allocate

        self._lock = threading.Lock()
        self._ports: Dict[int, VirtualSerialPort] = {}  # logical_addr → port

    # ──────────────────────────────────────────
    # 公共接口
    # ──────────────────────────────────────────

    def create(self, logical_addr: int,
               port_type: PortType,
               baud_rate: int = 115200) -> str:
        """
        创建并开启虚拟串口，返回 device_path。
        若同一逻辑地址已存在则直接返回已有 device_path（幂等）。
        """
        with self._lock:
            existing = self._ports.get(logical_addr)
            if existing is not None and existing.is_open:
                return existing.device_path

        port = VirtualSerialPort(
            logical_addr=logical_addr,
            port_type=port_type,
            baud_rate=baud_rate,
            logic_table=self._logic_table,
            transport_send=self._transport_send,
            encode_fn=self._encode_fn,
            seq_allocate=self._seq_allocate,
        )
        device_path = port.open()
        with self._lock:
            self._ports[logical_addr] = port

        logger.info("VSerialManager: created %s → 0x%04X", device_path, logical_addr)
        return device_path

    def close(self, logical_addr: int) -> None:
        """关闭指定逻辑地址的虚拟串口。"""
        with self._lock:
            port = self._ports.pop(logical_addr, None)
        if port is not None:
            port.close()

    def close_all(self) -> None:
        """关闭所有虚拟串口（程序退出时调用）。"""
        with self._lock:
            ports = list(self._ports.values())
            self._ports.clear()
        for port in ports:
            try:
                port.close()
            except Exception:
                pass

    def get(self, logical_addr: int) -> Optional[VirtualSerialPort]:
        """获取虚拟串口对象（用于读 overflow_count 等属性）。"""
        with self._lock:
            return self._ports.get(logical_addr)

    def list_all(self) -> List[VirtualSerialPort]:
        """返回所有虚拟串口快照列表。"""
        with self._lock:
            return list(self._ports.values())

    # ──────────────────────────────────────────
    # 消息分发（设备 → 第三方）
    # ──────────────────────────────────────────

    def dispatch(self, frame: CommandFrame) -> None:
        """
        将 cmd<0x10 的消息帧分发到对应虚拟串口。

        逻辑：
          1. 根据 frame.ports 反查 PCLogicRoutingTable（逆向：PortPath → logicAddr）
             ——框架中 PortPath 直接编码在帧 ports 字段，
               需遍历 self._ports 找匹配 port_path.ports == frame.ports
          2. 调用 VirtualSerialPort.write_to_pty(frame.data)
        """
        if frame.cmd >= 0x10:
            return   # 非消息帧，忽略

        # 快速路径：ports 字段即路由路径，遍历已注册端口寻找匹配
        snapshot = self.list_all()
        frame_ports = frame.ports

        for port in snapshot:
            if not port.is_open:
                continue
            route = self._logic_table.lookup(port.logical_addr)
            if route is None:
                continue
            if list(route.ports) == frame_ports:
                port.write_to_pty(frame.data)
                return

        # 未找到匹配端口：记录为 UnclaimedData（上层可订阅 UnclaimedDataEvent）
        logger.debug("VSerialManager.dispatch: no port for ports=%s, dropping %d bytes",
                     frame_ports, len(frame.data))
