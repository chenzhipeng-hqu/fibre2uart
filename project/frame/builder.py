# -*- coding: utf-8 -*-
"""
frame/builder.py — 高层参数 → 字节流

帧结构：SOF(1) | check(1) | seq(1) | len(1) | cmd(1) | portLen(1) | port[0~N] | data[0~N]

  len   = 6 + portLen + dataLen   （SOF 到 dataN 整帧字节数，与协议文档"SOF-dataN"一致）
  check = SUM(seq + len + cmd + portLen + sum(ports) + sum(data)) & 0xFF
"""
from typing import List

from frame.models import SOF_TX, SOF_RX


class FrameBuilder:
    """
    构建协议帧字节流。

    - ``build_tx`` — PC → MCU（SOF=0xAA），正常发送使用
    - ``build_rx`` — MCU → PC（SOF=0xAB），仿真/自测使用
    """

    @staticmethod
    def _calc_check(seq: int, length: int, cmd: int,
                    ports: List[int], data: bytes) -> int:
        port_len = len(ports)
        return (
            seq + length + cmd + port_len
            + sum(ports) + sum(data)
        ) & 0xFF

    @classmethod
    def build_tx(cls, seq: int, cmd: int,
                 ports: List[int], data: bytes) -> bytes:
        """SOF=0xAA，PC→MCU 正常发送。"""
        return cls._build(SOF_TX, seq, cmd, ports, data)

    @classmethod
    def build_rx(cls, seq: int, cmd: int,
                 ports: List[int], data: bytes) -> bytes:
        """SOF=0xAB，MCU→PC 仿真/自测使用。"""
        return cls._build(SOF_RX, seq, cmd, ports, data)

    @classmethod
    def _build(cls, sof: int, seq: int, cmd: int,
               ports: List[int], data: bytes) -> bytes:
        port_len = len(ports)
        data_len = len(data)
        length = 6 + port_len + data_len        # len = SOF(1)+check(1)+seq(1)+len(1)+cmd(1)+portLen(1)+ports+data
        check = cls._calc_check(seq, length, cmd, ports, data)

        buf = bytearray()
        buf.append(sof & 0xFF)
        buf.append(check & 0xFF)
        buf.append(seq & 0xFF)
        buf.append(length & 0xFF)
        buf.append(cmd & 0xFF)
        buf.append(port_len & 0xFF)
        buf.extend(ports)
        buf.extend(data)
        return bytes(buf)
