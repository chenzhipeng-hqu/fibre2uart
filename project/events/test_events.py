# -*- coding: utf-8 -*-
"""
events/test_events.py — EventBus 单元测试

测试项：
  1. 基本 subscribe/publish
  2. 多种事件类型互不干扰
  3. 多个订阅者同时收到同一事件
  4. WeakMethod：实例被 GC 后订阅自动移除（无崩溃）
  5. lambda token：保持 token → 收到；token GC → 自动移除
  6. startup_buffer：mark_ready 前发布 → 缓冲；mark_ready 后回放（顺序保留）
  7. mark_ready 后 publish → 直接 dispatch（不再缓冲）
  8. 单个回调异常不影响其他订阅者
  9. clear_startup_buffer 清空缓冲
 10. 并发 publish + subscribe 不崩溃
"""
import gc
import sys
import os
import threading
import time
import unittest

# 让测试可以找到 project 包
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from events.bus import EventBus, DeviceFoundEvent, RouteTableUpdatedEvent


class _MyEvent:
    def __init__(self, val: int = 0):
        self.val = val


class _OtherEvent:
    pass


# ──────────────────────────────────────────────
# 测试辅助类
# ──────────────────────────────────────────────

class _Handler:
    def __init__(self):
        self.received = []

    def handle(self, event):
        self.received.append(event)


# ──────────────────────────────────────────────
# 测试用例
# ──────────────────────────────────────────────

class TestEventBus(unittest.TestCase):

    def setUp(self):
        self.bus = EventBus()
        self.bus.mark_ready()   # 大多数测试直接进入 ready 状态

    # ── 1. 基本 subscribe/publish ──────────────

    def test_basic_subscribe_and_publish(self):
        """订阅后发布事件，回调被调用且参数正确"""
        received = []
        handler = _Handler()
        self.bus.subscribe(_MyEvent, handler.handle)
        self.bus.publish(_MyEvent(42))
        self.assertEqual(len(handler.received), 1)
        self.assertEqual(handler.received[0].val, 42)

    # ── 2. 多种事件类型互不干扰 ───────────────

    def test_different_event_types_isolated(self):
        """不同事件类型互相隔离，不触发对方订阅者"""
        my_received = []
        other_received = []

        h_my = _Handler()
        h_other = _Handler()
        self.bus.subscribe(_MyEvent, h_my.handle)
        self.bus.subscribe(_OtherEvent, h_other.handle)

        self.bus.publish(_MyEvent(1))
        self.assertEqual(len(h_my.received), 1)
        self.assertEqual(len(h_other.received), 0)

        self.bus.publish(_OtherEvent())
        self.assertEqual(len(h_my.received), 1)
        self.assertEqual(len(h_other.received), 1)

    # ── 3. 多个订阅者同时收到 ─────────────────

    def test_multiple_subscribers_all_notified(self):
        """同类型多个订阅者均被通知"""
        h1, h2, h3 = _Handler(), _Handler(), _Handler()
        self.bus.subscribe(_MyEvent, h1.handle)
        self.bus.subscribe(_MyEvent, h2.handle)
        self.bus.subscribe(_MyEvent, h3.handle)
        self.bus.publish(_MyEvent(99))
        for h in (h1, h2, h3):
            self.assertEqual(len(h.received), 1)

    # ── 4. WeakMethod：GC 后自动移除 ──────────

    def test_weakmethod_auto_remove_on_gc(self):
        """对象被GC后弱引用方法订阅自动失效"""
        h = _Handler()
        self.bus.subscribe(_MyEvent, h.handle)
        self.bus.publish(_MyEvent(1))
        self.assertEqual(len(h.received), 1)

        del h
        gc.collect()

        # GC 后再发一次，不应崩溃，且死引用已被清理
        self.bus.publish(_MyEvent(2))
        # 无异常即通过

    # ── 5. lambda token：持有 → 有效；GC → 移除 ─

    def test_lambda_token_lifetime(self):
        """持有token期间lambda订阅有效，释放后自动移除"""
        received = []

        # 订阅 lambda，保持 token
        token = self.bus.subscribe(_MyEvent, lambda e: received.append(e.val))
        self.bus.publish(_MyEvent(10))
        self.assertEqual(received, [10])

        # 释放 token，weakref 应失效
        del token
        gc.collect()

        self.bus.publish(_MyEvent(20))
        # 释放后不应再收到
        self.assertEqual(received, [10])

    # ── 6. startup_buffer + mark_ready replay ─

    def test_startup_buffer_replayed_in_order(self):
        """mark_ready前的事件在就绪后按序回放"""
        bus2 = EventBus()      # 新建，未 mark_ready
        h = _Handler()
        bus2.subscribe(_MyEvent, h.handle)

        # 发布 3 个事件 → 全部缓冲
        bus2.publish(_MyEvent(1))
        bus2.publish(_MyEvent(2))
        bus2.publish(_MyEvent(3))
        self.assertEqual(len(h.received), 0, "未 mark_ready 时不应立即 dispatch")

        bus2.mark_ready()

        # mark_ready 后应按顺序 replay
        self.assertEqual(len(h.received), 3)
        self.assertEqual([e.val for e in h.received], [1, 2, 3])

    # ── 7. mark_ready 后 publish → 直接 dispatch ─

    def test_after_mark_ready_direct_dispatch(self):
        """mark_ready后新事件直接分发不经缓冲"""
        h = _Handler()
        self.bus.subscribe(_MyEvent, h.handle)
        self.bus.publish(_MyEvent(100))
        self.bus.publish(_MyEvent(200))
        self.assertEqual([e.val for e in h.received], [100, 200])

    # ── 8. 回调异常不影响其他订阅者 ──────────

    def test_exception_in_one_callback_doesnt_break_others(self):
        """某订阅者抛异常不影响其他订阅者被通知"""
        results = []

        class BrokenHandler:
            def handle(self, event):
                raise RuntimeError("intentional error")

        ok_handler = _Handler()
        broken = BrokenHandler()

        self.bus.subscribe(_MyEvent, broken.handle)
        self.bus.subscribe(_MyEvent, ok_handler.handle)

        # 不应抛出异常
        self.bus.publish(_MyEvent(7))
        self.assertEqual(len(ok_handler.received), 1)

    # ── 9. clear_startup_buffer ───────────────

    def test_clear_startup_buffer(self):
        """clear_startup_buffer 丢弃所有缓冲事件"""
        bus2 = EventBus()
        h = _Handler()
        bus2.subscribe(_MyEvent, h.handle)

        bus2.publish(_MyEvent(1))
        bus2.publish(_MyEvent(2))
        bus2.clear_startup_buffer()
        bus2.mark_ready()

        # buffer 已清空，replay 为空
        self.assertEqual(len(h.received), 0)

    # ── 10. 并发 publish 不崩溃 ───────────────

    def test_concurrent_publish_thread_safety(self):
        """多线程并发发布事件不崩溃无丢失"""
        h = _Handler()
        self.bus.subscribe(_MyEvent, h.handle)
        errors = []

        def worker(start_val):
            try:
                for i in range(50):
                    self.bus.publish(_MyEvent(start_val + i))
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(t * 100,))
                   for t in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], "并发发布不应有异常")
        self.assertEqual(len(h.received), 250)


if __name__ == '__main__':
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromTestCase(TestEventBus)
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
