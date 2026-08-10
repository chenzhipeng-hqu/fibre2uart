# -*- coding: utf-8 -*-
"""
commands/port.py — 端口指令处理器（0x31~0x37）

数据格式：
  0x31 port_reset          : data=port_no(1)
  0x32 port_power          : data=port_no(1)+on(1)
  0x33 port_config (读取)   : data=port_no(1)
                             响应   data=port_no(1)+baud(4,LSB)+parity(1)+stopbit(1)
  0x33 port_config (设置)   : data=port_no(1)+baud(4,LSB)+parity(1)+stopbit(1)
  0x34 set_uart_filter     : data=port_no(1)+enable(1)+filter_bytes(N)
  0x35 set_can_filter      : data=port_no(1)+filter_id(1)+can_id(4,LSB)
  0x36 set_logical_addr    : data=port_no(1)+addr(2,MSB)
  0x37 set_terminal_port   : data=port_no(1)+is_terminal(1)  0=非终端口 1=终端口
"""
from __future__ import annotations

import struct
from typing import Tuple

from protocol.models import PortPath
from session.session_manager import SessionManager

_TIMEOUT = 1.0


class PortCmdHandler:
    """
    端口指令处理器，封装 0x31~0x36 指令的编解码。
    """

    def __init__(self, session_manager: SessionManager,
                 timeout: float = _TIMEOUT) -> None:
        self._session = session_manager
        self._timeout = timeout

    def port_reset(self, port_path: PortPath, port_no: int) -> None:
        """0x31 端口复位。"""
        fut = self._session.send_request(
            port_path, 0x31, bytes([port_no & 0xFF]), self._timeout)
        fut.result(timeout=self._timeout + 0.5)

    def port_power(self, port_path: PortPath, port_no: int, on: bool) -> None:
        """0x32 端口供电控制。"""
        data = bytes([port_no & 0xFF, 0x01 if on else 0x00])
        fut = self._session.send_request(port_path, 0x32, data, self._timeout)
        fut.result(timeout=self._timeout + 0.5)

    def get_port_config(self, port_path: PortPath,
                        port_no: int) -> Tuple[int, int, int]:
        """0x33 读取端口配置，返回 (baud, parity, stopbit)。

        发送仅含 port_no 的 1 字节请求，MCU 响应 7 字节：
          port_no(1) + baud(4, LSB) + parity(1) + stopbit(1)
        """
        fut = self._session.send_request(
            port_path, 0x33, bytes([port_no & 0xFF]), self._timeout)
        frame = fut.result(timeout=self._timeout + 0.5)
        data = frame.data
        if len(data) < 7:
            raise ValueError(f"get_port_config: short response {len(data)} bytes")
        baud = struct.unpack('<I', data[1:5])[0]
        parity = data[5]
        stopbit = data[6]
        return baud, parity, stopbit

    def port_config(self, port_path: PortPath, port_no: int,
                    baud: int, parity: int, stopbit: int) -> None:
        """0x33 设置端口波特率/校验/停止位（波特率为 LSB 小端序）。"""
        data = (bytes([port_no & 0xFF])
                + struct.pack('<I', baud)
                + bytes([parity & 0xFF, stopbit & 0xFF]))
        fut = self._session.send_request(port_path, 0x33, data, self._timeout)
        fut.result(timeout=self._timeout + 0.5)

    def set_uart_filter(self, port_path: PortPath, port_no: int,
                        enable: bool, filter_data: bytes = b'') -> None:
        """0x34 设置串口过滤器。"""
        data = bytes([port_no & 0xFF, 0x01 if enable else 0x00]) + filter_data
        fut = self._session.send_request(port_path, 0x34, data, self._timeout)
        fut.result(timeout=self._timeout + 0.5)

    def set_can_filter(self, port_path: PortPath, port_no: int,
                       filter_id: int, can_id: int) -> None:
        """0x35 设置 CAN 过滤器（can_id 为 LSB 小端序）。"""
        data = bytes([port_no & 0xFF, filter_id & 0xFF]) + struct.pack('<I', can_id)
        fut = self._session.send_request(port_path, 0x35, data, self._timeout)
        fut.result(timeout=self._timeout + 0.5)

    def set_logical_addr(self, port_path: PortPath,
                         port_no: int, addr: int) -> None:
        """0x36 写入端口逻辑地址（发现阶段 Phase C 调用）。"""
        data = bytes([port_no & 0xFF]) + struct.pack('>H', addr)
        fut = self._session.send_request(port_path, 0x36, data, self._timeout)
        fut.result(timeout=self._timeout + 0.5)

    def set_terminal_port(self, port_path: PortPath,
                          port_no: int, is_terminal: bool) -> None:
        """0x37 设置终端口标记（发现阶段 Phase C，紧跟 set_logical_addr 调用）。

        :param port_path:    父节点路径（同 0x36）
        :param port_no:      端口编号（1-based）
        :param is_terminal:  True=终端口，False=中继节点
        """
        data = bytes([port_no & 0xFF, 0x01 if is_terminal else 0x00])
        fut = self._session.send_request(port_path, 0x37, data, self._timeout)
        fut.result(timeout=self._timeout + 0.5)
