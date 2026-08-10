# -*- coding: utf-8 -*-
"""
transport/test_serial_transport.py — SerialTransport 单元测试

使用 MockSerial（内存管道）注入，不需要真实串口。

测试项：
  1. open() + send() → 数据写入 MockSerial
  2. recv loop → on_data_received 被调用
  3. close() → 发布 TransportLostEvent
  4. tx_count / rx_count 在 send/recv 后递增
  5. 发送队列满时丢弃并递增 err_count
  6. 被动断连（MockSerial.read 抛异常）→ 发布 TransportLostEvent
"""
import queue
import sys
import os
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from events.bus import EventBus, TransportLostEvent
from transport.serial_transport import SerialTransport


# ──────────────────────────────────────────────
# MockSerial
# ──────────────────────────────────────────────

class MockSerial:
    """
    内存模拟串口，用于 SerialTransport 注入测试。

    - write(data)  → 追加到 _sent 缓冲区
    - read(size)   → 从 _recv_queue 取数据（阻塞 100ms）
    - close()      → 标记 is_open=False
    - inject(data) → 测试辅助：将数据放入接收队列（模拟设备发来数据）
    - raise_on_read → 设置后下次 read() 抛 OSError（模拟断连）
    """

    def __init__(self, port=None, baudrate=None):
        self._recv_queue: queue.Queue = queue.Queue()
        self._sent = bytearray()
        self._lock = threading.Lock()
        self.is_open = True
        self._raise_on_read = False

    def write(self, data: bytes) -> int:
        with self._lock:
            self._sent.extend(data)
        return len(data)

    def read(self, size: int = 256) -> bytes:
        if self._raise_on_read:
            raise OSError("simulated disconnect")
        try:
            return self._recv_queue.get(timeout=0.1)
        except queue.Empty:
            return b''

    def close(self) -> None:
        self.is_open = False

    # 测试辅助
    def inject(self, data: bytes) -> None:
        self._recv_queue.put(data)

    def get_sent(self) -> bytes:
        with self._lock:
            return bytes(self._sent)

    def trigger_disconnect(self) -> None:
        """下次 read() 将抛 OSError。"""
        self._raise_on_read = True
        
    def flush(self) -> None:
        pass


# ──────────────────────────────────────────────
# 工厂函数
# ──────────────────────────────────────────────

def _mock_factory(port, baudrate):
    return MockSerial(port, baudrate)


# ──────────────────────────────────────────────
# 测试用例
# ──────────────────────────────────────────────

class TestSerialTransport(unittest.TestCase):

    def setUp(self):
        self.bus = EventBus()
        self.bus.mark_ready()
        self.transport = SerialTransport(self.bus)
        self._mock: MockSerial = None

    def _open(self, raise_on_read: bool = False) -> MockSerial:
        mock = MockSerial()
        mock._raise_on_read = raise_on_read

        def factory(port, baudrate):
            return mock

        self.transport.open('/dev/ttyFAKE', 115200,
                            _serial_factory=factory)
        self._mock = mock
        return mock

    def tearDown(self):
        if self.transport.is_open:
            self.transport.close()

    # ── 1. send() 数据写入 MockSerial ──────────

    def test_send_data_written_to_serial(self):
        """send() 发送的数据写入底层串口"""
        mock = self._open()
        payload = b'\x01\x02\x03\x04'
        self.transport.send(payload)
        time.sleep(0.2)   # 等待发送线程处理
        self.assertEqual(mock.get_sent(), payload)

    # ── 2. recv loop → on_data_received ────────

    def test_recv_loop_calls_on_data_received(self):
        """接收线程收到数据后调用 on_data_received 回调"""
        received = []
        mock = self._open()
        self.transport.on_data_received = lambda data: received.append(data)

        time.sleep(0.2)
        mock.inject(b'\xAB\xCD')
        time.sleep(0.2)

        self.assertIn(b'\xAB\xCD', received)

    # ── 3. close() → TransportLostEvent ────────

    def test_close_publishes_transport_lost_event(self):
        """close() 发布 TransportLostEvent(reason='close')"""
        events = []
        token = self.bus.subscribe(TransportLostEvent,
                                   lambda e: events.append(e))
        self._open()
        self.transport.close()
        time.sleep(0.1)

        self.assertTrue(len(events) >= 1, "关闭时应发布 TransportLostEvent")
        self.assertEqual(events[0].reason, 'close')
        del token

    # ── 4. tx_count / rx_count 递增 ────────────

    def test_tx_rx_count_increments(self):
        """发送/接收后 tx_count/rx_count 递增"""
        mock = self._open()
        self.transport.on_data_received = lambda _: None

        # 确保发送线程已启动
        time.sleep(0.1)
        
        self.transport.send(b'\x01')
        time.sleep(0.1)  # 等待第一个发送完成
        self.transport.send(b'\x02')
        time.sleep(0.3)  # 等待第二个发送完成

        self.assertEqual(self.transport.tx_count, 2)

        mock.inject(b'\xAA')
        time.sleep(0.2)
        mock.inject(b'\xBB')
        time.sleep(0.3)

        self.assertEqual(self.transport.rx_count, 2)

    # ── 5. 队列满时丢弃并递增 err_count ─────────

    def test_queue_full_increments_err_count(self):
        """发送队列满时丢包并递增 err_count"""
        # 创建一个极小队列的 transport
        bus2 = EventBus()
        bus2.mark_ready()
        t = SerialTransport(bus2)
        t._send_queue = __import__('queue').Queue(maxsize=1)

        mock = MockSerial()
        # 不启动发送线程，直接往满队列投数据
        # 先填满
        import queue as _q
        t._send_queue.put(b'\x00')  # 占满（maxsize=1）
        t._running = True           # 让 send() 不直接返回

        t.send(b'\xFF\xFF')         # 队列满，应丢弃

        self.assertEqual(t.err_count, 1)
        t._running = False

    # ── 6. 被动断连 → TransportLostEvent ────────

    def test_passive_disconnect_publishes_event(self):
        """底层读异常时发布 TransportLostEvent 被动断连"""
        events = []
        token = self.bus.subscribe(TransportLostEvent,
                                   lambda e: events.append(e))
        mock = MockSerial()
        mock._raise_on_read = True   # 立即断连

        def factory(port, baudrate):
            return mock

        self.transport.open('/dev/ttyFAKE', 115200,
                            _serial_factory=factory)
        # recv_loop 应立即发现异常并发布事件
        time.sleep(0.5)

        self.assertTrue(len(events) >= 1, "被动断连应发布 TransportLostEvent")
        del token


if __name__ == '__main__':
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromTestCase(TestSerialTransport)
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
