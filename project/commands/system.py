# -*- coding: utf-8 -*-
"""
commands/system.py — 系统指令处理器（0x21~0x26）

数据格式约定（对应协议文档 §3.6）：
  0x21 reset          : 请求 data=[]；响应 data=[]
  0x22 get/set date   : get: data=[]；set: data=8字节 ASCII（YYYYMMDD）
                        响应 data=8字节日期
  0x23 scan_nodes     : 请求/响应 data=[]
  0x24 get_node_info  : 请求 data=[]；响应 data= uuid(12)+model(1)+portTypes(6)
  0x25 status         : 请求 data=clear(1)；响应 data=status bytes
  0x26 control_io     : 请求 data=io_mask(1)+mode(1)+value(1)；响应 data=[]
"""
from __future__ import annotations

import struct
from typing import List, Optional

from device.models import BoardInfo, NodeInfo
from protocol.models import ModelType, PortPath, PortType
from session.session_manager import SessionManager

# 默认超时（秒）
_TIMEOUT = 1.0


def _parse_port_types(raw: bytes) -> List[PortType]:
    """将 6 字节端口类型原始数据解析为 PortType 列表。"""
    result = []
    for b in raw[:6]:
        try:
            result.append(PortType(b))
        except ValueError:
            result.append(PortType.NONE)
    return result


class SysCmdHandler:
    """
    系统指令处理器，封装 0x21~0x26 指令的编解码。

    所有方法均同步阻塞（内部通过 Future.result() 等待响应）。
    """

    def __init__(self, session_manager: SessionManager,
                 timeout: float = _TIMEOUT) -> None:
        self._session = session_manager
        self._timeout = timeout

    def clone_with_timeout(self, timeout: float) -> "SysCmdHandler":
        """返回共享同一 SessionManager 但不同超时的新实例。"""
        return SysCmdHandler(self._session, timeout=timeout)

    def reset(self, port_path: PortPath) -> None:
        """0x21 系统复位（无响应数据）。"""
        fut = self._session.send_request(port_path, 0x21, b'', self._timeout)
        fut.result(timeout=self._timeout + 0.5)

    def get_mfg_date(self, port_path: PortPath) -> str:
        """0x22 读取出厂日期。
        响应格式：data0=day(1B), data1=month(1B), data2-3=year(2B, LSB)
        返回 'YYYYMMDD' 字符串。
        """
        fut = self._session.send_request(port_path, 0x22, b'', self._timeout)
        frame = fut.result(timeout=self._timeout + 0.5)
        data = frame.data
        if len(data) < 4:
            raise ValueError(f"get_mfg_date: short response {len(data)} bytes")
        day   = data[0]
        month = data[1]
        year  = int.from_bytes(data[2:4], 'little')
        return f"{year:04d}{month:02d}{day:02d}"

    def set_mfg_date(self, port_path: PortPath, date: str) -> None:
        """0x22 设置出厂日期。
        date 格式 'YYYYMMDD'。
        发送格式：data0=day(1B), data1=month(1B), data2-3=year(2B, LSB)
        """
        year  = int(date[0:4])
        month = int(date[4:6])
        day   = int(date[6:8])
        data  = bytes([day, month]) + year.to_bytes(2, 'little')
        fut = self._session.send_request(port_path, 0x22, data, self._timeout)
        fut.result(timeout=self._timeout + 0.5)

    def scan_nodes(self, port_path: PortPath) -> None:
        """0x23 触发节点扫描（通常不需要直接调用，发现时用 0x24）。"""
        fut = self._session.send_request(port_path, 0x23, b'', self._timeout)
        fut.result(timeout=self._timeout + 0.5)

    def get_node_info(self, port_path: PortPath) -> NodeInfo:
        """
        0x24 获取节点信息。

        响应数据：uuid(12) + model(1) + portTypes(6) = 19 字节
        """
        fut = self._session.send_request(port_path, 0x24, b'', self._timeout)
        frame = fut.result(timeout=self._timeout + 0.5)
        data = frame.data
        if len(data) < 19:
            raise ValueError(f"get_node_info: short response {len(data)} bytes")
        uuid = data[:12]
        model_byte = data[12]
        try:
            model = ModelType(model_byte)
        except ValueError:
            model = ModelType.UNKNOWN
        port_types = _parse_port_types(data[13:19])
        return NodeInfo(uuid=uuid, model=model, port_types=port_types)

    def get_clear_status(self, port_path: PortPath,
                         clear: bool = False) -> bytes:
        """0x25 查询/清除状态，返回原始状态字节。"""
        data = bytes([0x01 if clear else 0x00])
        fut = self._session.send_request(port_path, 0x25, data, self._timeout)
        frame = fut.result(timeout=self._timeout + 0.5)
        return frame.data

    def control_io(self, port_path: PortPath,
                   io_mask: int, mode: int, value: int) -> None:
        """0x26 控制 IO。"""
        data = bytes([io_mask & 0xFF, mode & 0xFF, value & 0xFF])
        fut = self._session.send_request(port_path, 0x26, data, self._timeout)
        fut.result(timeout=self._timeout + 0.5)
