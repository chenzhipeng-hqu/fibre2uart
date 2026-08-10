# -*- coding: utf-8 -*-
"""
session/test_session.py — SeqManager / SessionManager 单元测试

测试项：
  SeqManager:
    1.  单线程循环分配 0~255，第 257 次回到 0
    2.  多线程并发分配不碰撞（4线程×64次=256次）
    3.  is_duplicate 同路径同 seq → True
    4.  is_duplicate 不同路径相同 seq → False
    5.  reset_seen 清空指定路径历史
    6.  reset_all 清空全部历史
    7.  滑动窗口超过 128 时旧 seq 被淘汰（不无限增长）
  SessionManager:
    8.  send_request → Future，on_frame_received 后 resolve
    9.  seq 不匹配的响应帧被丢弃
    10. 超时后 Future 被 reject 为 TimeoutError
    11. on_transport_lost 批量 reject 所有挂起 Future
    12. connection_generation 代际校验（旧帧不 resolve 新请求）
    13. close 后新请求立即 reject
    14. Future 被 GC 后超时扫描不崩溃
    15. 多请求并发，各自独立 resolve
"""
from __future__ import annotations

import sys
import os
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from protocol.models import PortPath
from frame.builder import FrameBuilder
from frame.parser import FrameParser
from frame.models import CommandFrame
from session.seq_manager import SeqManager
from session.session_manager import SessionManager, TransportLostError


def _make_frame(seq: int, cmd: int = 0x24, ports: list[int] | None = None,
                data: bytes = b'') -> CommandFrame:
    raw = FrameBuilder.build_rx(seq, cmd, ports or [], data)
    return list(FrameParser().feed(raw))[0]


def _make_session(send_fn=None) -> SessionManager:
    return SessionManager(
        transport_send=send_fn or (lambda _: None),
        timeout_interval=0.05,
    )


class TestSeqManager(unittest.TestCase):

    # 1
    def test_cycle_0_to_255(self):
        """seq 循环分配 0~255，第257次回绕到 0"""
        mgr = SeqManager()
        seqs = [mgr.allocate() for _ in range(256)]
        self.assertEqual(seqs, list(range(256)))
        self.assertEqual(mgr.allocate(), 0)   # 回绕

    # 2
    def test_concurrent_no_collision(self):
        """4线程并发分配256次 seq，无重复碰撞"""
        mgr = SeqManager()
        results: list[int] = []
        lock = threading.Lock()

        def worker():
            for _ in range(64):
                s = mgr.allocate()
                with lock:
                    results.append(s)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(results), 256)
        from collections import Counter
        counts = Counter(results)
        self.assertTrue(all(v == 1 for v in counts.values()),
                        f"碰撞: {counts.most_common(3)}")

    # 3
    def test_is_duplicate_same_path_same_seq(self):
        """相同路径相同seq第二次出现判定为重复帧"""
        mgr = SeqManager()
        pp = PortPath([1, 2])
        self.assertFalse(mgr.is_duplicate(pp, 42))
        self.assertTrue(mgr.is_duplicate(pp, 42))

    # 4
    def test_is_duplicate_different_path(self):
        """不同路径相同seq不视为重复"""
        mgr = SeqManager()
        pp1 = PortPath([1, 2])
        pp2 = PortPath([1, 3])
        mgr.is_duplicate(pp1, 10)
        self.assertFalse(mgr.is_duplicate(pp2, 10))   # 不同路径不重复

    # 5
    def test_reset_seen_clears_path(self):
        """reset_seen 清空指定路径去重历史"""
        mgr = SeqManager()
        pp = PortPath([1])
        mgr.is_duplicate(pp, 5)
        self.assertTrue(mgr.is_duplicate(pp, 5))
        mgr.reset_seen(pp)
        self.assertFalse(mgr.is_duplicate(pp, 5))  # 清空后不再重复

    # 6
    def test_reset_all(self):
        """reset_all 清空全部路径去重历史"""
        mgr = SeqManager()
        pp = PortPath([1])
        mgr.is_duplicate(pp, 10)
        mgr.reset_all()
        self.assertFalse(mgr.is_duplicate(pp, 10))

    # 7
    def test_window_does_not_grow_unbounded(self):
        """去重滑动窗口不超过128条上限"""
        mgr = SeqManager()
        pp = PortPath([1])
        for i in range(200):
            mgr.is_duplicate(pp, i % 256)
        key = (tuple(pp.ports), pp.has485)
        with mgr._lock:
            size = len(mgr._seen.get(key, set()))
        self.assertLessEqual(size, mgr._WINDOW + 1)


class TestSessionManager(unittest.TestCase):

    # 8
    def test_resolve_on_frame_received(self):
        """收到匹配响应帧后 Future 正常 resolve"""
        session = _make_session()
        pp = PortPath([1])
        fut = session.send_request(pp, cmd=0x24, data=b'')
        self.assertFalse(fut.done())

        session.on_frame_received(_make_frame(seq=0, cmd=0x24))
        self.assertTrue(fut.done())
        self.assertEqual(fut.result(timeout=0.1).seq, 0)

    # 9
    def test_unmatched_seq_ignored(self):
        """seq不匹配的响应帧被丢弃，Future不变"""
        session = _make_session()
        pp = PortPath([1])
        fut = session.send_request(pp, cmd=0x24, timeout=0.2)
        session.on_frame_received(_make_frame(seq=99))  # 不匹配
        self.assertFalse(fut.done())

    # 10
    def test_timeout_rejects_future(self):
        """超时后 Future 被 reject 为 TimeoutError"""
        session = _make_session()
        pp = PortPath([1])
        fut = session.send_request(pp, cmd=0x24, timeout=0.1)
        with self.assertRaises(TimeoutError):
            fut.result(timeout=0.5)

    # 11
    def test_transport_lost_batch_reject(self):
        """on_transport_lost 批量 reject 所有挂起 Future"""
        session = _make_session()
        pp = PortPath([1])
        futs = [session.send_request(pp, cmd=0x24, timeout=5.0) for _ in range(3)]
        session.on_transport_lost()
        time.sleep(0.05)
        for fut in futs:
            self.assertTrue(fut.done())
            with self.assertRaises(TransportLostError):
                fut.result()

    # 12
    def test_connection_generation_stale_frame_discarded(self):
        """重连后旧代际帧不会 resolve 新请求"""
        session = _make_session()
        pp = PortPath([1])
        fut_old = session.send_request(pp, cmd=0x24, timeout=5.0)
        session.on_transport_lost()  # 代际+1，fut_old 被 reject

        fut_new = session.send_request(pp, cmd=0x24, timeout=5.0)
        # 旧帧 seq=0 到达（代际已过期）
        session.on_frame_received(_make_frame(seq=0))
        self.assertFalse(fut_new.done())
        # 新帧 seq=1 到达
        session.on_frame_received(_make_frame(seq=1))
        self.assertTrue(fut_new.done())

    # 13
    def test_close_rejects_new_request(self):
        """close 后新请求立即被 reject"""
        session = _make_session()
        session.close()
        pp = PortPath([1])
        fut = session.send_request(pp, cmd=0x24, timeout=5.0)
        self.assertTrue(fut.done())
        with self.assertRaises(TransportLostError):
            fut.result()

    # 14
    def test_future_gc_no_crash(self):
        """Future 被 GC 后超时扫描不崩溃"""
        session = _make_session()
        pp = PortPath([1])
        fut = session.send_request(pp, cmd=0x24, timeout=0.1)
        del fut
        time.sleep(0.3)   # 让超时扫描运行，不应崩溃

    # 15
    def test_multiple_concurrent_requests(self):
        """多个并发请求各自独立 resolve，seq互不干扰"""
        sent: list[bytes] = []
        session = _make_session(send_fn=sent.append)
        pp = PortPath([1])

        futs = [session.send_request(pp, cmd=0x24, timeout=1.0) for _ in range(5)]
        self.assertEqual(len(sent), 5)

        # 逐一 resolve（seq 0~4）
        for i in range(5):
            session.on_frame_received(_make_frame(seq=i))

        for i, fut in enumerate(futs):
            self.assertTrue(fut.done(), f"fut[{i}] 未完成")
            self.assertEqual(fut.result(timeout=0.1).seq, i)


if __name__ == '__main__':
    unittest.main(verbosity=2)
