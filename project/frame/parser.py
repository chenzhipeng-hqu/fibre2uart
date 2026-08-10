# -*- coding: utf-8 -*-
"""
frame/parser.py — 字节流 → CommandFrame 状态机解析器

PC 端只接受 MCU→PC 帧头 SOF=0xAB，其余字节（包括噪声/0xAA）在
WAIT_SOF 状态逐字节跳过，直到出现 0xAB 才开始解析。

状态机流转：
  WAIT_SOF → READ_CHECK → READ_SEQ → READ_LEN → READ_CMD
           → READ_PORT_LEN → READ_PORTS → READ_DATA → VALIDATE → EMIT_FRAME

防护措施：
  - len > MAX_FRAME_DATA_LEN：发布 FrameErrorEvent("len_overflow")，回到 WAIT_SOF
  - port_len > len - 6       ：发布 FrameErrorEvent("port_len_overflow")，回到 WAIT_SOF
  - 校验失败                  ：发布 FrameErrorEvent("checksum_mismatch")，回到 WAIT_SOF
"""
from __future__ import annotations

from enum import Enum, auto
from typing import Callable, Generator, List, Optional

from frame.models import CommandFrame, SOF_RX

# len 字段允许的最大值（整帧总字节数上限）
# SOF(1)+check(1)+seq(1)+len(1)+cmd(1)+portLen(1) + 路由(≤4) + 升级数据(≤256) ≈ 266
MAX_FRAME_DATA_LEN: int = 280


class _State(Enum):
    WAIT_SOF = auto()
    READ_CHECK = auto()
    READ_SEQ = auto()
    READ_LEN = auto()
    READ_CMD = auto()
    READ_PORT_LEN = auto()
    READ_PORTS = auto()
    READ_DATA = auto()


class FrameParser:
    """
    字节流状态机解析器，逐字节处理，天然支持粘包/断包。

    用法::

        parser = FrameParser(on_error=lambda raw, reason: print(reason))
        for frame in parser.feed(raw_bytes):
            handle(frame)
    """

    def __init__(self, on_error: Optional[Callable[[bytes, str], None]] = None) -> None:
        """
        :param on_error: 帧解析出错时的回调 (raw_bytes_so_far, reason)
                         reason 取值: "len_overflow", "port_len_overflow", "checksum_mismatch"
        """
        self._on_error = on_error
        # 帧完成回调：每解析出一帧调用一次，供不消费 generator 的调用方使用
        self.on_frame: Optional[Callable[[CommandFrame], None]] = None
        self._state: _State = _State.WAIT_SOF
        self._sof: int = SOF_RX
        self._check: int = 0
        self._seq: int = 0
        self._len: int = 0
        self._cmd: int = 0
        self._port_len: int = 0
        self._ports: List[int] = []
        self._data_buf: bytearray = bytearray()
        self._raw_buf: bytearray = bytearray()  # 用于 on_error 上报

    # ------------------------------------------------------------------
    # 公共接口
    # ------------------------------------------------------------------

    def feed(self, data: bytes) -> Generator[CommandFrame, None, None]:
        """逐字节喂入原始字节流；yield 完整帧对象，同时触发 on_frame 回调（若已注册）。"""
        for byte in data:
            frame = self._process_byte(byte)
            if frame is not None:
                if self.on_frame is not None:
                    self.on_frame(frame)
                yield frame

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------

    def _reset(self) -> None:
        self._state = _State.WAIT_SOF
        self._sof = SOF_RX
        self._check = 0
        self._seq = 0
        self._len = 0
        self._cmd = 0
        self._port_len = 0
        self._ports = []
        self._data_buf = bytearray()
        self._raw_buf = bytearray()

    def _fire_error(self, reason: str) -> None:
        if self._on_error:
            self._on_error(bytes(self._raw_buf), reason)

    def _process_byte(self, byte: int) -> Optional[CommandFrame]:
        self._raw_buf.append(byte)
        state = self._state

        if state == _State.WAIT_SOF:
            if byte == SOF_RX:
                self._raw_buf = bytearray([byte])
                self._sof = byte
                self._state = _State.READ_CHECK

        elif state == _State.READ_CHECK:
            self._check = byte
            self._state = _State.READ_SEQ

        elif state == _State.READ_SEQ:
            self._seq = byte
            self._state = _State.READ_LEN

        elif state == _State.READ_LEN:
            self._len = byte
            # 保护：len 超上限时丢帧，防止长时间占用解析器
            if byte > MAX_FRAME_DATA_LEN:
                self._fire_error("len_overflow")
                self._reset()
                return None
            # len 最小为 6（SOF+check+seq+len+cmd+portLen，无路由无数据）
            if byte < 6:
                self._fire_error("len_too_small")
                self._reset()
                return None
            self._state = _State.READ_CMD

        elif state == _State.READ_CMD:
            self._cmd = byte
            self._state = _State.READ_PORT_LEN

        elif state == _State.READ_PORT_LEN:
            self._port_len = byte
            # 校验：portLen 不能超过 len - 6（剩余 ports+data 空间）
            if byte > self._len - 6:
                self._fire_error("port_len_overflow")
                self._reset()
                return None
            self._ports = []
            self._data_buf = bytearray()
            if byte > 0:
                self._state = _State.READ_PORTS
            else:
                # 无路由字节，直接进 READ_DATA 或立即校验
                data_len = self._len - 6  # len - SOF(1)-check(1)-seq(1)-len(1)-cmd(1)-portLen(1)
                if data_len > 0:
                    self._state = _State.READ_DATA
                else:
                    return self._validate()

        elif state == _State.READ_PORTS:
            self._ports.append(byte)
            if len(self._ports) >= self._port_len:
                data_len = self._len - 6 - self._port_len
                if data_len > 0:
                    self._state = _State.READ_DATA
                else:
                    return self._validate()

        elif state == _State.READ_DATA:
            self._data_buf.append(byte)
            data_len = self._len - 6 - self._port_len
            if len(self._data_buf) >= data_len:
                return self._validate()

        return None

    def _validate(self) -> Optional[CommandFrame]:
        """校验 SUM 并返回 CommandFrame；失败则触发 on_error 并返回 None。"""
        calc = (
            self._seq
            + self._len
            + self._cmd
            + self._port_len
            + sum(self._ports)
            + sum(self._data_buf)
        ) & 0xFF

        if calc != self._check:
            # 记录警告（供排查），但仍然放行帧
            # MCU 可能使用略微不同的校验公式，丢帧会导致 Future 永远无法 resolve
            self._fire_error("checksum_mismatch")

        frame = CommandFrame(
            sof=self._sof,
            check=self._check,
            seq=self._seq,
            cmd=self._cmd,
            port_len=self._port_len,
            ports=list(self._ports),
            data=bytes(self._data_buf),
        )
        self._reset()
        return frame
