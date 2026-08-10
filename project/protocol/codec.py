# -*- coding: utf-8 -*-
"""
protocol/codec.py — 帧字段与业务语义之间的编解码

encode : (seq, cmd, PortPath, data) → bytes  （使用 FrameBuilder.build_tx）
decode : CommandFrame → (cmd, PortPath, data)

注意：
  - encode 生成 PC→MCU 帧（SOF=0xAA）
  - has485 字段是路由表元数据，不体现在帧字节流中；decode 不自动推断 has485，
    需要调用方结合路由表查询结果自行填充（PortPath.has485 默认 False）
"""
from typing import Tuple

from frame.builder import FrameBuilder
from frame.models import CommandFrame
from protocol.models import PortPath


class CommandCodec:
    """帧字段与业务语义之间的编解码。"""

    @staticmethod
    def encode(seq: int, cmd: int, port_path: PortPath, data: bytes) -> bytes:
        """
        构建 PC→MCU 字节流（SOF=0xAA）。

        :param seq:       序列号 0~255
        :param cmd:       指令类型
        :param port_path: 路由路径（ports 列表将直接写入帧）
        :param data:      载荷
        :return:          完整帧字节串
        """
        return FrameBuilder.build_tx(seq, cmd, list(port_path.ports), data)

    @staticmethod
    def decode(frame: CommandFrame) -> Tuple[int, PortPath, bytes]:
        """
        从 CommandFrame 解析业务字段。

        :return: (cmd, PortPath, data)
                 PortPath.has485 默认 False，调用方按需从路由表补充
        """
        port_path = PortPath(ports=list(frame.ports), has485=False)
        return frame.cmd, port_path, frame.data
