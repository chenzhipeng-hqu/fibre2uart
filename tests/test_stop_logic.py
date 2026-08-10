# -*- coding: utf-8 -*-
"""
tests/test_stop_logic.py — 通信测试停止语义 / 快速放行 自测

验证 throughput_panel 新的停止逻辑（替换旧的"连续 3 次超时无条件停止"）：
  1. blast 模式（等待回环确认 关 + 出错自动停止 关）+ 回环失效：不会自动停止，
     持续发送（本次需求的核心）。
  2. 出错自动停止 开 + 回环失效：首个漏帧即停。
  3. 等待回环确认 开 + 回环失效：首个漏帧即停（宽）。
  4. blast 模式连续漏帧达阈值：进入"快速放行"模式。
  5. 健康回环 + 等待回环确认：ack 信号不丢失、不虚假超时停机（fix A 回归测试）。

运行：PYTHONPATH=project python3 tests/test_stop_logic.py
"""
import os
import sys
import threading
import time
import unittest

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)
_PROJ = os.path.join(_BASE, 'project')
if _PROJ not in sys.path:
    sys.path.insert(0, _PROJ)

import serial as _realserial
from ui.throughput_panel import ThroughputTestWorker


class _DeadSerial:
    """模拟回环失效：永远收不到数据。"""
    def __init__(self, *a, **k):
        self.is_open = True

    @property
    def in_waiting(self):
        return 0

    def read(self, n=1):
        time.sleep(0.02)  # 模拟串口读超时，避免空转烧 CPU
        return b''

    def write(self, data):
        return len(data)

    def close(self):
        self.is_open = False


class _EchoSerial:
    """模拟健康回环：通过类级共享缓冲区，把"设备回送"的数据暴露给 worker 打开的实例读取。"""
    _feed = bytearray()
    _lock = threading.Lock()

    @classmethod
    def reset(cls):
        with cls._lock:
            cls._feed = bytearray()

    @classmethod
    def feed(cls, data):
        with cls._lock:
            cls._feed.extend(data)

    def __init__(self, *a, **k):
        self.is_open = True

    @property
    def in_waiting(self):
        with _EchoSerial._lock:
            return len(_EchoSerial._feed)

    def read(self, n=1):
        time.sleep(0.003)  # 模拟 ~3ms RTT
        with _EchoSerial._lock:
            if not _EchoSerial._feed:
                return b''
            chunk = bytes(_EchoSerial._feed[:n])
            del _EchoSerial._feed[:n]
            return chunk

    def write(self, data):
        _EchoSerial.feed(data)
        return len(data)

    def close(self):
        self.is_open = False


class _MockClient:
    """send_cmd 立即成功。echo=True 时把数据回送到 _EchoSerial 共享缓冲。"""
    def __init__(self, echo=False):
        self._session = None
        self._echo = echo

    def send_cmd(self, addr, cmd, data):
        if self._echo:
            _EchoSerial.feed(data)
        return b'\x00'


def _wait_worker_exit(w, timeout):
    end = time.time() + timeout
    while time.time() < end:
        if not w.is_alive():
            return True
        time.sleep(0.05)
    w.join(timeout=0.5)
    return not w.is_alive()


def _wait_mode(w, mode, timeout):
    end = time.time() + timeout
    while time.time() < end:
        if w.get_metrics().get('loopback_mode') == mode:
            return mode
        time.sleep(0.05)
    return w.get_metrics().get('loopback_mode')


def _stop_quietly(w):
    try:
        w.stop()
    except Exception:
        pass
    w.join(timeout=3.0)


class TestStopLogic(unittest.TestCase):
    def setUp(self):
        self._orig_serial = _realserial.Serial
        _realserial.Serial = _DeadSerial

    def tearDown(self):
        _realserial.Serial = self._orig_serial

    def test_blast_mode_keeps_sending_on_timeout(self):
        """两开关都关：回环失效时不自动停止，持续发送（核心需求）。"""
        w = ThroughputTestWorker(
            _MockClient(), target_logical_addr=0x001, loopback_port='/dev/fake',
            loopback_baudrate=1000000, duration=600, max_count=0,
            interval_ms=20, wait_for_ack=False, stop_on_error=False,
        )
        w.start()
        try:
            time.sleep(4.0)  # 超过旧逻辑"3 次漏帧(~3s)即停"的窗口
            alive = w.is_alive()
            tx1 = w.get_metrics()['tx_count']
            time.sleep(1.5)
            tx2 = w.get_metrics()['tx_count']
            self.assertTrue(alive, "blast 模式不应在回环失效时自动停止")
            self.assertGreater(tx1, 20, "应已发送多包")
            self.assertGreater(tx2, tx1 + 10, "应持续发送，tx_count 持续增长")
        finally:
            _stop_quietly(w)

    def test_stop_on_error_stops_on_first_miss(self):
        """出错自动停止：首个回环漏帧即停。"""
        w = ThroughputTestWorker(
            _MockClient(), target_logical_addr=0x001, loopback_port='/dev/fake',
            loopback_baudrate=1000000, duration=600, max_count=0,
            interval_ms=20, wait_for_ack=False, stop_on_error=True,
        )
        w.start()
        stopped = _wait_worker_exit(w, timeout=4.0)  # 首个漏帧约 1s 后触发
        self.assertTrue(stopped, "stop_on_error 应在首个漏帧后停止")

    def test_wait_for_ack_stops_on_first_miss(self):
        """等待回环确认（宽）：首个回环漏帧即停。"""
        w = ThroughputTestWorker(
            _MockClient(), target_logical_addr=0x001, loopback_port='/dev/fake',
            loopback_baudrate=1000000, duration=600, max_count=0,
            interval_ms=20, wait_for_ack=True, stop_on_error=False,
        )
        w.start()
        stopped = _wait_worker_exit(w, timeout=4.0)
        self.assertTrue(stopped, "wait_for_ack 应在首个漏帧后停止")

    def test_fast_drain_entry(self):
        """blast 模式连续漏帧达阈值后进入快速放行。"""
        class _FastDrainWorker(ThroughputTestWorker):
            FAST_DRAIN_THRESHOLD = 3  # 降低阈值加速测试

        w = _FastDrainWorker(
            _MockClient(), target_logical_addr=0x001, loopback_port='/dev/fake',
            loopback_baudrate=1000000, duration=600, max_count=0,
            interval_ms=20, wait_for_ack=False, stop_on_error=False,
        )
        w.start()
        try:
            mode = _wait_mode(w, 'fast_drain', timeout=8.0)  # 3 次漏帧(~3s)后进入
            self.assertEqual(mode, 'fast_drain', "应进入快速放行模式")
        finally:
            _stop_quietly(w)


class TestAckRaceRegression(unittest.TestCase):
    """fix A 回归：健康快回环 + wait_for_ack 时 ack 信号不丢失、不虚假停机。"""
    def setUp(self):
        self._orig_serial = _realserial.Serial
        _realserial.Serial = _EchoSerial
        _EchoSerial.reset()

    def tearDown(self):
        _realserial.Serial = self._orig_serial

    def test_healthy_loopback_no_false_stop(self):
        w = ThroughputTestWorker(
            _MockClient(echo=True), target_logical_addr=0x001, loopback_port='/dev/fake',
            loopback_baudrate=1000000, duration=600, max_count=0,
            interval_ms=20, wait_for_ack=True, stop_on_error=False,
        )
        w.start()
        try:
            time.sleep(3.0)
            m = w.get_metrics()
            self.assertTrue(w.is_alive(), "健康回环不应虚假停机（ack 不应丢失）")
            self.assertGreater(m['rx_count'], 10, "应成功收到多包，证明 ack 信号正常传递")
        finally:
            _stop_quietly(w)


if __name__ == '__main__':
    unittest.main(verbosity=2)
