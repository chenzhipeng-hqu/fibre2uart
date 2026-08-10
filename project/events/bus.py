# -*- coding: utf-8 -*-
"""
events/bus.py — 发布-订阅事件总线

设计要点（§3.8）：
  - bound method     → weakref.WeakMethod，实例被 GC 后订阅自动移除
  - lambda / partial / 普通函数 → _FuncWrapper（强持有），再 weakref.ref(_FuncWrapper)
    subscribe() 返回 _FuncWrapper token；调用方必须持有 token，
    token 被 GC 后订阅自动移除（允许外部精确控制生命周期）
  - _startup_buffer  → 发现完成前缓冲所有事件，保证零丢失
  - mark_ready()     → 原子切换 _ready + 快照 buffer + 锁外 dispatch，防止乱序
"""
from __future__ import annotations

import inspect
import threading
import weakref
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Type


# ──────────────────────────────────────────────
# 事件基类与内置事件
# ──────────────────────────────────────────────

class EventBase:
    """所有事件的公共基类（可选，不强制继承）。"""


@dataclass
class NodeDiscoveredEvent(EventBase):
    """Phase A 发现节点后立即发布（logicAddr 尚未分配）。"""
    port_path: Any        # PortPath
    uuid: bytes
    model: Any            # ModelType
    port_types: Any       # List[PortType]


@dataclass
class DeviceFoundEvent(EventBase):
    device: Any                    # Device（避免循环导入，类型用 Any）


@dataclass
class DeviceOfflineEvent(EventBase):
    uid: bytes


@dataclass
class UpgradeProgressEvent(EventBase):
    uid: bytes
    progress: int                  # 0~100


@dataclass
class UpgradeErrorEvent(EventBase):
    uid: bytes
    stage: str
    error_code: int
    retry_count: int


@dataclass
class TransportLostEvent(EventBase):
    reason: str = ''


@dataclass
class TransportRestoredEvent(EventBase):
    pass


@dataclass
class RouteTableUpdatedEvent(EventBase):
    pass


@dataclass
class DiscoveryFailedEvent(EventBase):
    reason: str


@dataclass
class PortDiscoveryFailedEvent(EventBase):
    port_path: Any                 # PortPath


@dataclass
class FrameErrorEvent(EventBase):
    raw: bytes
    reason: str


@dataclass
class UnclaimedDataEvent(EventBase):
    port_path: Any
    data: bytes


# ──────────────────────────────────────────────
# 弱引用包装器
# ──────────────────────────────────────────────

class _FuncWrapper:
    """
    将非绑定方法（lambda / partial / 普通函数）包装成可 weakref 的对象。

    - 内部持有对原始 callable 的**强**引用
    - subscribe() 返回此对象给调用方；调用方持有 token 则订阅有效，
      token 被释放后 weakref 失效，订阅自动移除
    """
    __slots__ = ('_func', '__weakref__')   # __weakref__ 允许对此对象创建弱引用

    def __init__(self, func: Callable) -> None:
        self._func = func

    def __call__(self, event: Any) -> None:
        self._func(event)


# ──────────────────────────────────────────────
# EventBus
# ──────────────────────────────────────────────

class EventBus:
    """
    全局发布-订阅事件总线。

    典型用法::

        bus = EventBus()

        # bound method — 对象被 GC 后自动移除，无需手动 unsubscribe
        bus.subscribe(MyEvent, self.handle)

        # lambda / function — 必须保持 token 存活
        token = bus.subscribe(MyEvent, lambda e: print(e))

        bus.publish(MyEvent())
        bus.mark_ready()   # DiscoveryWorker finally 块调用，replay startup_buffer
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # event_type → list of (weakref, original_id)
        self._subscribers: Dict[Type, List[weakref.ref]] = {}
        self._startup_buffer: deque = deque()
        self._ready: bool = False

    # ------------------------------------------------------------------
    # 订阅 / 取消订阅
    # ------------------------------------------------------------------

    def subscribe(self, event_type: Type, callback: Callable) -> Any:
        """
        订阅事件。

        :param event_type: 事件类型（class）
        :param callback:   回调函数
        :return:           对于非绑定方法，返回 _FuncWrapper token（调用方须持有）；
                           对于绑定方法，返回 None（WeakMethod 自动管理生命周期）
        """
        if inspect.ismethod(callback):
            ref: weakref.ref = weakref.WeakMethod(callback)
            token = None
        else:
            wrapper = _FuncWrapper(callback)
            ref = weakref.ref(wrapper)
            token = wrapper

        with self._lock:
            self._subscribers.setdefault(event_type, []).append(ref)

        return token

    def unsubscribe(self, event_type: Type, callback: Callable) -> None:
        """
        取消订阅。对于绑定方法，会搜索匹配的 WeakMethod；
        对于函数/lambda，需要传入 subscribe() 返回的 _FuncWrapper token。
        """
        with self._lock:
            subs = self._subscribers.get(event_type)
            if not subs:
                return
            to_remove = []
            for ref in subs:
                cb = ref()
                if cb is None or cb == callback:
                    to_remove.append(ref)
            for r in to_remove:
                try:
                    subs.remove(r)
                except ValueError:
                    pass

    # ------------------------------------------------------------------
    # 发布 / 标记就绪
    # ------------------------------------------------------------------

    def publish(self, event: Any) -> None:
        """
        发布事件。未 mark_ready 前缓入 startup_buffer；之后直接 dispatch。
        """
        with self._lock:
            if not self._ready:
                self._startup_buffer.append(event)
                return
        # _ready=True，直接在锁外 dispatch
        self._dispatch(event)

    def mark_ready(self) -> None:
        """
        原子切换就绪状态，锁外逐条 replay startup_buffer，防止乱序。
        DiscoveryWorker.finally 块保证此方法一定被调用。
        """
        with self._lock:
            if self._ready:
                return           # 防止重复调用
            self._ready = True
            buffer = list(self._startup_buffer)
            self._startup_buffer.clear()

        for event in buffer:
            self._dispatch(event)   # 锁外 dispatch，回调内允许再次 publish

    def clear_startup_buffer(self) -> None:
        """Transport 关闭时清空积压缓冲（未 mark_ready 的场景）。"""
        with self._lock:
            self._startup_buffer.clear()

    # ------------------------------------------------------------------
    # 内部分发
    # ------------------------------------------------------------------

    def _dispatch(self, event: Any) -> None:
        """向所有订阅者分发事件；过期弱引用自动清理。"""
        event_type = type(event)
        dead_refs: List[weakref.ref] = []
        callbacks: List[Callable] = []

        with self._lock:
            refs = list(self._subscribers.get(event_type, []))

        for ref in refs:
            cb = ref()
            if cb is None:
                dead_refs.append(ref)
            else:
                callbacks.append(cb)

        # 清理失效引用
        if dead_refs:
            with self._lock:
                subs = self._subscribers.get(event_type, [])
                for d in dead_refs:
                    try:
                        subs.remove(d)
                    except ValueError:
                        pass

        # 锁外调用，允许回调内再次 publish
        for cb in callbacks:
            try:
                cb(event)
            except Exception:
                pass   # 单个回调异常不影响其他订阅者
