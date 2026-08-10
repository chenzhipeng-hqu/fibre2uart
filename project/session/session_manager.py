# -*- coding: utf-8 -*-
"""
session/session_manager.py — 请求-响应配对与超时管理

设计要点（§3.4）：
  - send_request() 分配 seq、编码帧、投递 Transport.send()、注册 PendingRequest
  - on_frame_received() 匹配 seq，校验 connection_generation，resolve Future
  - _timeout_checker() 后台线程每 100ms 扫描超时请求，reject Future
  - on_transport_lost() 自增代际、批量 reject 所有挂起 Future

connection_generation：
  每次 on_transport_lost() 自增；PendingRequest 记录注册时的代际；
  收到响应时若代际不匹配则丢弃，防止重连后旧帧误 resolve 新请求。

weakref：
  PendingRequest 用 weakref 持有 Future，若调用方提前放弃等待（Future 被 GC），
  下次超时扫描时自动清理该条目。
"""
from __future__ import annotations

import threading
import time
import weakref
from concurrent.futures import Future
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional, Tuple

from frame.models import CommandFrame
from protocol.codec import CommandCodec
from protocol.models import PortPath
from session.seq_manager import SeqManager

# 默认请求超时（秒）
DEFAULT_TIMEOUT: float = 0.5


class TransportLostError(Exception):
    """Transport 断连时批量 reject Future 使用的异常类型。"""


@dataclass
class PendingRequest:
    seq: int
    port_path: PortPath
    sent_at: float
    timeout: float
    connection_generation: int
    # weakref 持有 Future，避免强引用阻止 GC
    _future_ref: 'weakref.ref[Future]' = field(repr=False)

    @property
    def future(self) -> Optional[Future]:
        """返回 Future 对象；若已被 GC 则返回 None。"""
        return self._future_ref()

    @classmethod
    def create(cls, seq: int, port_path: PortPath,
               timeout: float, connection_generation: int) -> Tuple['PendingRequest', Future]:
        """工厂方法：同时创建 PendingRequest 与对应的 Future。"""
        fut: Future = Future()
        ref: weakref.ref = weakref.ref(fut)
        pending = cls(
            seq=seq,
            port_path=port_path,
            sent_at=time.monotonic(),
            timeout=timeout,
            connection_generation=connection_generation,
            _future_ref=ref,
        )
        return pending, fut


class SessionManager:
    """
    会话管理器：请求-响应配对、超时管理、断连批量清理。

    使用方式::

        session = SessionManager(transport_send_fn, seq_manager)
        fut = session.send_request(port_path, cmd=0x24, data=b'', timeout=0.5)
        frame = fut.result(timeout=1.0)   # 阻塞等待响应
    """

    def __init__(
        self,
        transport_send: Callable[[bytes], None],
        seq_manager: Optional[SeqManager] = None,
        timeout_interval: float = 0.1,
    ) -> None:
        """
        :param transport_send:    SerialTransport.send，接收字节投递到发送队列
        :param seq_manager:       SeqManager 实例；不传则内部创建
        :param timeout_interval:  超时检查间隔（秒）
        """
        self._transport_send = transport_send
        self._seq_mgr = seq_manager or SeqManager()
        self._lock = threading.Lock()
        self._pending: Dict[int, PendingRequest] = {}   # seq → PendingRequest
        self._connection_generation: int = 0
        self._closed = False

        # 启动超时检查线程
        self._timeout_thread = threading.Thread(
            target=self._timeout_checker,
            name='SessionTimeout',
            daemon=True,
        )
        self._timeout_interval = timeout_interval
        self._timeout_thread.start()

    # ------------------------------------------------------------------
    # 公共接口
    # ------------------------------------------------------------------

    def send_request(
        self,
        port_path: PortPath,
        cmd: int,
        data: bytes = b'',
        timeout: float = DEFAULT_TIMEOUT,
    ) -> Future:
        """
        发送一个请求并返回 Future。

        :param port_path: 路由路径
        :param cmd:       指令类型
        :param data:      载荷
        :param timeout:   等待超时（秒）
        :return:          Future[CommandFrame]，resolve 时值为响应帧
        """
        seq = self._seq_mgr.allocate()
        raw = CommandCodec.encode(seq, cmd, port_path, data)

        pending, fut = PendingRequest.create(
            seq=seq,
            port_path=port_path,
            timeout=timeout,
            connection_generation=self._connection_generation,
        )

        with self._lock:
            if self._closed:
                fut.set_exception(TransportLostError("SessionManager already closed"))
                return fut
            self._pending[seq] = pending

        self._transport_send(raw)
        return fut

    def send_no_reply(self, port_path: PortPath, cmd: int, data: bytes = b'') -> None:
        """发送帧但不等待响应（fire-and-forget）。适用于广播指令。"""
        seq = self._seq_mgr.allocate()
        raw = CommandCodec.encode(seq, cmd, port_path, data)
        self._transport_send(raw)

    def on_frame_received(self, frame: CommandFrame) -> None:
        """
        接收线程回调：匹配 seq，校验代际，resolve Future。

        :param frame: 已解析的响应帧（SOF=0xAB）
        """
        with self._lock:
            pending = self._pending.pop(frame.seq, None)

        if pending is None:
            return  # 无匹配（超时后已被清理，或重复帧）

        # 代际校验：重连前的残留帧丢弃
        if pending.connection_generation != self._connection_generation:
            return

        fut = pending.future
        if fut is None or fut.done():
            return  # Future 已被 GC 或已完成

        fut.set_result(frame)

    def on_transport_lost(self) -> None:
        """
        Transport 断连时调用：自增代际，批量 reject 所有挂起 Future。
        """
        with self._lock:
            self._connection_generation += 1
            pending_list = list(self._pending.values())
            self._pending.clear()

        for p in pending_list:
            fut = p.future
            if fut is not None and not fut.done():
                fut.set_exception(TransportLostError("transport lost"))

    def close(self) -> None:
        """关闭 SessionManager，拒绝新请求并批量 reject 现有请求。"""
        with self._lock:
            self._closed = True
        self.on_transport_lost()

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------

    def _timeout_checker(self) -> None:
        """后台守护线程：每 timeout_interval 扫描一次，清理超时请求。"""
        while True:
            time.sleep(self._timeout_interval)
            now = time.monotonic()
            timed_out = []
            with self._lock:
                for seq, pending in list(self._pending.items()):
                    if now - pending.sent_at >= pending.timeout:
                        timed_out.append(self._pending.pop(seq))

            for p in timed_out:
                fut = p.future
                if fut is not None and not fut.done():
                    fut.set_exception(TimeoutError(
                        f"seq={p.seq} timeout after {p.timeout}s"
                    ))
