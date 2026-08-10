# -*- coding: utf-8 -*-
"""
frame/models.py — 帧数据模型

帧结构：SOF(1) | check(1) | seq(1) | len(1) | cmd(1) | portLen(1) | port[0~N] | data[0~N]

  len     = 6 + portLen + dataLen   （整帧总字节数，SOF 到最后一个 data 字节）
  check   = SUM(seq + len + cmd + portLen + ports + data) & 0xFF
"""
from dataclasses import dataclass, field
from typing import List

SOF_TX = 0xAA  # PC → MCU
SOF_RX = 0xAB  # MCU → PC


@dataclass
class CommandFrame:
    sof: int                            # 0xAA 或 0xAB
    check: int                          # SUM 校验值
    seq: int                            # 序列号 0~255
    cmd: int                            # 指令类型
    port_len: int                       # ports 字段字节数
    ports: List[int] = field(default_factory=list)   # 路由路径（0x01~0x7F；0x00=广播）
    data: bytes = b''                   # 载荷

    def __post_init__(self):
        if isinstance(self.data, (list, bytearray)):
            self.data = bytes(self.data)
        if isinstance(self.ports, tuple):
            self.ports = list(self.ports)

    def __repr__(self) -> str:
        return (
            f"CommandFrame(sof=0x{self.sof:02X}, seq={self.seq}, "
            f"cmd=0x{self.cmd:02X}, ports={self.ports}, "
            f"data={self.data.hex() if self.data else ''})"
        )
