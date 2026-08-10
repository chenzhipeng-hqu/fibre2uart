# -*- coding: utf-8 -*-
"""
transport/serial_transport.py — 物理串口收发

设计要点（§3.1）：
  - 单一发送线程（send_queue）：所有发送请求入队，串行消费，无多线程竞争
  - send() → None：只投队列，立即返回；不等待响应
  - 独立接收线程：持续 read()，数据通过 on_data_received 回调向上传递
  - tx_count / rx_count / err_count：原子计数（threading.Lock 保护）
  - close() 批量清理：发布 TransportLostEvent → 触发 SessionManager 批量 reject
  - 支持 _serial_factory 注入（测试时注入 MockSerial，生产环境用 pyserial.Serial）
"""
from __future__ import annotations

import logging
import queue
import threading
from typing import Callable, Optional

_logger = logging.getLogger(__name__)

# 运行时按需 import serial，允许在没有 pyserial 的测试环境中加载此模块
try:
    import serial as _pyserial
    _HAS_PYSERIAL = True
except ImportError:
    _HAS_PYSERIAL = False

from events.bus import EventBus, TransportLostEvent, TransportRestoredEvent

_SENTINEL = None   # 发送队列停止哨兵


class SerialTransport:
    """
    串口传输层。

    :param event_bus: 用于发布 TransportLostEvent / TransportRestoredEvent
    """

    def __init__(self, event_bus: Optional[EventBus] = None) -> None:
        self._event_bus = event_bus
        self._serial = None
        self._port: Optional[str] = None
        self._baudrate: int = 115200

        self._send_queue: queue.Queue = queue.Queue(maxsize=256)
        self._send_thread: Optional[threading.Thread] = None
        self._recv_thread: Optional[threading.Thread] = None
        self._running: bool = False
        self._send_interval_ms: int = 1   # 帧间最小间隔，可通过 open() 配置

        # 通信计数（原子递增，用 lock 保护）
        self._count_lock = threading.Lock()
        self.tx_count: int = 0
        self.rx_count: int = 0
        self.err_count: int = 0

        # 上层注册回调：收到原始字节后调用（通常是 FrameParser.feed）
        self.on_data_received: Optional[Callable[[bytes], None]] = None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def open(self, port: str, baudrate: int = 115200,
             send_interval_ms: int = 1,
             _serial_factory=None) -> None:
        """
        打开串口并启动收发线程。

        :param port:             串口路径，如 '/dev/ttyUSB0' 或 'COM3'
        :param baudrate:         波特率，默认 115200
        :param send_interval_ms: 每帧发送后的最小间隔（毫秒），0=不限制，默认 1ms
        :param _serial_factory:  可注入的串口工厂函数 (port, baudrate) → serial-like；
                                  为 None 时使用 pyserial.Serial
        """
        if self._running:
            return

        self._port = port
        self._baudrate = baudrate
        self._send_interval_ms = max(0, send_interval_ms)

        if _serial_factory is not None:
            self._serial = _serial_factory(port, baudrate)
        elif _HAS_PYSERIAL:
            self._serial = _pyserial.Serial(
                port,
                baudrate,
                timeout=0.05,
                # stopbits=_pyserial.STOPBITS_ONE  # 设置停止位为1位
                stopbits=_pyserial.STOPBITS_TWO  # 设置停止位为2位
            )
        else:
            raise RuntimeError("pyserial not installed and no _serial_factory provided")

        self._running = True

        self._send_thread = threading.Thread(
            target=self._send_loop, daemon=True, name='SerialSend')
        self._recv_thread = threading.Thread(
            target=self._recv_loop, daemon=True, name='SerialRecv')
        self._send_thread.start()
        self._recv_thread.start()

    def close(self, emit_event: bool = True) -> None:
        """
        关闭串口：
        1. 停止发送线程（投入哨兵）
        2. 停止接收线程
        3. 关闭物理串口
        4. emit_event=True 时发布 TransportLostEvent（触发上层批量清理）
           主动断开时传 emit_event=False，避免触发自动重连逻辑
        """
        if not self._running:
            return
        self._running = False
        # 唤醒发送线程（可能阻塞在 queue.get()）
        try:
            self._send_queue.put_nowait(_SENTINEL)
        except queue.Full:
            pass

        if self._serial is not None:
            try:
                self._serial.close()
            except Exception:
                pass
            self._serial = None

        if emit_event and self._event_bus is not None:
            self._event_bus.publish(TransportLostEvent(reason='close'))

    def send(self, data: bytes) -> None:
        """
        将字节数据投入发送队列，立即返回（非阻塞）。
        队列满时丢弃并递增 err_count。
        """
        try:
            self._send_queue.put_nowait(data)
        except queue.Full:
            with self._count_lock:
                self.err_count += 1

    # ------------------------------------------------------------------
    # 内部线程
    # ------------------------------------------------------------------

    def _send_loop(self) -> None:
        """发送线程：串行从队列取数据写入串口。"""
        import time as _time
        while True:
            data = self._send_queue.get()
            if data is _SENTINEL or not self._running:
                break
            try:
                self._serial.write(data)
                self._serial.flush()  # 强制刷出 USB 缓冲，尽量避免多帧合并或断包
                if self._send_interval_ms > 0:
                    _time.sleep(self._send_interval_ms / 1000.0)
                with self._count_lock:
                    self.tx_count += 1
                if len(data) > 30:
                    preview = data[:20].hex(' ').upper() + ' ... ' + data[-8:].hex(' ').upper()
                    _logger.debug("Tx[%d]: %s", len(data), preview)
                else:
                    _logger.debug("Tx[%d]: %s", len(data), data.hex(' ').upper())
            except Exception:
                with self._count_lock:
                    self.err_count += 1
                self._on_lost("send error")
                break

    def _recv_loop(self) -> None:
        """接收线程：持续读取串口数据，回调上层。"""
        while self._running:
            try:
                data = self._serial.read(256)
                if data:
                    with self._count_lock:
                        self.rx_count += 1
                    if len(data) > 30:
                        preview = data[:20].hex(' ').upper() + ' ... ' + data[-8:].hex(' ').upper()
                        _logger.debug("Rx[%d]: %s", len(data), preview)
                    else:
                        _logger.debug("Rx[%d]: %s", len(data), data.hex(' ').upper())
                    if self.on_data_received is not None:
                        self.on_data_received(data)
            except Exception:
                if self._running:
                    self._on_lost("recv error")
                break

    def _on_lost(self, reason: str) -> None:
        """被动断连处理：停止运行，发布事件。"""
        if not self._running:
            return
        self._running = False
        try:
            self._send_queue.put_nowait(_SENTINEL)
        except queue.Full:
            pass
        if self._serial is not None:
            try:
                self._serial.close()
            except Exception:
                pass
            self._serial = None
        if self._event_bus is not None:
            self._event_bus.publish(TransportLostEvent(reason=reason))

    # ------------------------------------------------------------------
    # 属性
    # ------------------------------------------------------------------

    @property
    def is_open(self) -> bool:
        return self._running
