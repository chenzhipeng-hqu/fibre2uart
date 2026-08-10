# -*- coding: utf-8 -*-
"""
virtual_serial/test_virtual_serial.py — VirtualSerialPort / VirtualSerialManager 单元测试

注意：PTY 测试只在 Linux 上运行（os.openpty 存在），Windows 跳过。

测试项：
  1. create() 返回非空 device_path，port is_open=True
  2. dispatch() 转发消息帧到对应端口（write_to_pty 被调用）
  3. dispatch() 遇到无匹配端口时不崩溃（guard）
  4. close() 后 is_open=False
  5. close_all() 关闭所有端口
  6. _on_pty_data → encode → transport_send 调用链（注入 on_pty_data 回调）
  7. write_to_pty BlockingIOError → overflow_count 递增
"""
import os
import sys
import unittest
import select
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from frame.models import CommandFrame, SOF_RX
from protocol.models import PortPath, PortType
from routing.table import PCLogicRoutingTable
from virtual_serial.port import VirtualSerialPort
from virtual_serial.manager import VirtualSerialManager

IS_LINUX = sys.platform != 'win32'


# ──────────────────────────────────────────────
# 测试辅助
# ──────────────────────────────────────────────

class _MockTransport:
    def __init__(self):
        self.sent: list = []

    def send(self, data: bytes) -> None:
        self.sent.append(data)


def _make_manager(logic_table=None) -> tuple:
    """返回 (manager, logic_table, mock_transport)"""
    if logic_table is None:
        logic_table = PCLogicRoutingTable()
    transport = _MockTransport()

    def encode_fn(seq, cmd, port_path, data):
        return data  # 透传，不真正编码

    def seq_allocate():
        return 42

    mgr = VirtualSerialManager(
        logic_table=logic_table,
        transport_send=transport.send,
        encode_fn=encode_fn,
        seq_allocate=seq_allocate,
    )
    return mgr, logic_table, transport


# ──────────────────────────────────────────────
# 测试：VirtualSerialManager
# ──────────────────────────────────────────────

class TestVirtualSerialManager(unittest.TestCase):

    def setUp(self):
        self.mgr, self.logic_table, self.transport = _make_manager()
        self.logic_table.add(0x001, PortPath([1, 2]))

    def tearDown(self):
        self.mgr.close_all()

    # ── 1. create 返回 device_path，is_open=True ─

    @unittest.skipUnless(IS_LINUX, "PTY only on Linux")
    def test_create_returns_device_path(self):
        """create() 返回 /dev/ttyVcom_* 路径且文件存在"""
        path = self.mgr.create(0x001, PortType.PORT_485, 115200)
        self.assertTrue(path, "device_path 不应为空")
        port = self.mgr.get(0x001)
        self.assertIsNotNone(port)
        self.assertTrue(port.is_open)

    # ── 2. dispatch 转发消息帧 ─────────────────

    @unittest.skipUnless(IS_LINUX, "PTY only on Linux")
    def test_dispatch_calls_write_to_pty(self):
        """dispatch(frame) 将 frame.data 写入对应 PTY"""
        written = []
        # 注入自定义 write_to_pty
        self.mgr.create(0x001, PortType.PORT_485)
        port = self.mgr.get(0x001)
        original_write = port.write_to_pty
        port.write_to_pty = lambda data: written.append(data)

        frame = CommandFrame(
            sof=SOF_RX, check=0, seq=1, cmd=0x02,   # cmd<0x10 = 消息
            port_len=2, ports=[1, 2],
            data=b'\xDE\xAD\xBE\xEF',
        )
        self.mgr.dispatch(frame)
        self.assertEqual(written, [b'\xDE\xAD\xBE\xEF'])

    # ── 3. dispatch 无匹配端口不崩溃 ───────────

    def test_dispatch_no_match_no_crash(self):
        """dispatch 找不到匹配端口时不崩溃"""
        frame = CommandFrame(
            sof=SOF_RX, check=0, seq=1, cmd=0x01,
            port_len=3, ports=[9, 9, 9],
            data=b'\x00',
        )
        try:
            self.mgr.dispatch(frame)   # 无端口，应静默丢弃
        except Exception as e:
            self.fail(f"dispatch 不应抛出异常: {e}")

    # ── 4. close 后 is_open=False ──────────────

    @unittest.skipUnless(IS_LINUX, "PTY only on Linux")
    def test_close_sets_is_open_false(self):
        """close() 后 is_open 变为 False"""
        self.mgr.create(0x001, PortType.PORT_485)
        self.mgr.close(0x001)
        port = self.mgr.get(0x001)
        self.assertIsNone(port, "close 后不应再 get 到")

    # ── 5. close_all ───────────────────────────

    @unittest.skipUnless(IS_LINUX, "PTY only on Linux")
    def test_close_all(self):
        """close_all() 关闭所有已创建的虚拟串口"""
        self.logic_table.add(0x002, PortPath([1, 3]))
        self.mgr.create(0x001, PortType.PORT_485)
        self.mgr.create(0x002, PortType.PORT_232)
        self.mgr.close_all()
        self.assertEqual(len(self.mgr.list_all()), 0)


# ──────────────────────────────────────────────
# 测试：VirtualSerialPort._on_pty_data 路由链
# ──────────────────────────────────────────────

class TestVirtualSerialPort(unittest.TestCase):

    # ── 6. _on_pty_data → transport_send ───────

    def test_on_pty_data_routes_via_on_pty_data_callback(self):
        """PTY数据通过on_pty_data回调正确路由"""
        received = []

        port = VirtualSerialPort(
            logical_addr=0x001,
            port_type=PortType.PORT_485,
        )
        port.on_pty_data = lambda data: received.append(data)
        # 不 open PTY，直接调用内部方法
        port._on_pty_data(b'\xAA\xBB\xCC')
        self.assertEqual(received, [b'\xAA\xBB\xCC'])

    # ── 7. write_to_pty BlockingIOError → overflow_count ─

    @unittest.skipUnless(IS_LINUX, "PTY only on Linux")
    def test_overflow_count_increments_on_blocking_io(self):
        """写PTY阻塞时overflow_count递增"""
        import builtins

        port = VirtualSerialPort(
            logical_addr=0x001,
            port_type=PortType.PORT_485,
        )
        port._master_fd = 99   # 假 fd
        port.is_open = True
        port._closing = False

        original_write = os.write

        def mock_write(fd, data):
            raise BlockingIOError("buffer full")

        os.write = mock_write
        try:
            port.write_to_pty(b'\x01' * 100)
        finally:
            os.write = original_write

        self.assertEqual(port.overflow_count, 1)


# ──────────────────────────────────────────────
# 测试：端到端串口收发（PTY 双向数据路径）
# ──────────────────────────────────────────────

class TestVirtualSerialReadWrite(unittest.TestCase):
    """
    端到端收发测试，验证 PTY master/slave 双向数据路径：
      - 下行：write_to_pty(data) → 第三方从 device_path 读取
      - 上行：第三方写入 device_path → on_pty_data 回调 / transport_send
    """

    def setUp(self):
        self._ports: list = []
        self._fds: list = []
        self._managers: list = []

    def tearDown(self):
        for fd in self._fds:
            try:
                os.close(fd)
            except OSError:
                pass
        for port in self._ports:
            try:
                port.close()
            except Exception:
                pass
        for mgr in self._managers:
            try:
                mgr.close_all()
            except Exception:
                pass

    def _open_raw(self, device_path: str) -> int:
        """以原始模式打开虚拟串口 slave 端，禁用 echo 和行规律转换。"""
        fd = os.open(device_path, os.O_RDWR | os.O_NOCTTY)
        try:
            import tty
            tty.setraw(fd)
        except Exception:
            pass
        self._fds.append(fd)
        return fd

    def _read_exactly(self, fd: int, n: int, timeout: float = 1.0) -> bytes:
        """从 fd 循环读取，直到凑够 n 字节或超时，返回已读字节。"""
        buf = bytearray()
        deadline = time.time() + timeout
        while len(buf) < n and time.time() < deadline:
            rlist, _, _ = select.select([fd], [], [], 0.05)
            if rlist:
                buf += os.read(fd, n - len(buf))
        return bytes(buf)

    # ── 8. 下行：write_to_pty → 第三方读取 ─────────────

    @unittest.skipUnless(IS_LINUX, "PTY only on Linux")
    def test_downstream_single_frame(self):
        """单帧下行：write_to_pty后第三方可读到相同数据"""
        port = VirtualSerialPort(logical_addr=0x010, port_type=PortType.PORT_485)
        self._ports.append(port)
        device_path = port.open()

        fd = self._open_raw(device_path)
        payload = b'\x01\x02\x03\x04\x05'
        port.write_to_pty(payload)

        data = self._read_exactly(fd, len(payload))
        self.assertEqual(data, payload, "下行单帧：读取内容与写入不符")

    # ── 9. 上行：第三方写入 → on_pty_data 回调 ──────────

    @unittest.skipUnless(IS_LINUX, "PTY only on Linux")
    def test_upstream_single_frame_callback(self):
        """单帧上行：第三方写入后on_pty_data收到相同数据"""
        received: list = []
        done = threading.Event()

        port = VirtualSerialPort(logical_addr=0x011, port_type=PortType.PORT_485)
        port.on_pty_data = lambda d: (received.append(d), done.set())
        self._ports.append(port)
        device_path = port.open()

        fd = self._open_raw(device_path)
        payload = b'\xAA\xBB\xCC'
        os.write(fd, payload)

        self.assertTrue(done.wait(timeout=1.0), "超时：1 秒内未触发 on_pty_data")
        self.assertEqual(b''.join(received), payload, "上行单帧：回调内容与写入不符")

    # ── 10. 下行：多帧连续收发 ──────────────────────────

    @unittest.skipUnless(IS_LINUX, "PTY only on Linux")
    def test_downstream_multi_frame(self):
        """多帧连续下行：字节无损按序到达"""
        port = VirtualSerialPort(logical_addr=0x012, port_type=PortType.PORT_485)
        self._ports.append(port)
        device_path = port.open()

        frames = [bytes([i] * (i + 1)) for i in range(1, 6)]  # 各 1~5 字节
        total = b''.join(frames)

        fd = self._open_raw(device_path)
        for frame in frames:
            port.write_to_pty(frame)

        data = self._read_exactly(fd, len(total))
        self.assertEqual(data, total, "多帧下行：合并内容不匹配")

    # ── 11. 上行：多帧连续写入 → 回调收齐全部内容 ──────

    @unittest.skipUnless(IS_LINUX, "PTY only on Linux")
    def test_upstream_multi_frame_callback(self):
        """多次上行写入：on_pty_data累计收到全部字节"""
        received: list = []
        total_expected = 0
        done = threading.Event()

        def _collect(data: bytes):
            received.append(data)
            if sum(len(d) for d in received) >= total_expected:
                done.set()

        port = VirtualSerialPort(logical_addr=0x013, port_type=PortType.PORT_485)
        port.on_pty_data = _collect
        self._ports.append(port)
        device_path = port.open()

        frames = [bytes([i] * (i + 1)) for i in range(1, 5)]  # 各 1~4 字节
        total_expected = sum(len(f) for f in frames)  # 10 字节

        fd = self._open_raw(device_path)
        for frame in frames:
            os.write(fd, frame)
            time.sleep(0.02)   # 给读取线程处理间隙

        self.assertTrue(done.wait(timeout=2.0), "超时：2 秒内回调未收齐全部字节")
        self.assertEqual(b''.join(received), b''.join(frames), "多帧上行：内容不匹配")

    # ── 12. Manager dispatch → 第三方读取（端到端下行）─

    @unittest.skipUnless(IS_LINUX, "PTY only on Linux")
    def test_manager_dispatch_to_pty(self):
        """Manager.dispatch()→第三方从device_path读到frame.data"""
        mgr, logic_table, _ = _make_manager()
        self._managers.append(mgr)
        logic_table.add(0x020, PortPath([1, 2]))
        device_path = mgr.create(0x020, PortType.PORT_485)

        fd = self._open_raw(device_path)
        payload = b'\xDE\xAD\xBE\xEF'
        frame = CommandFrame(
            sof=SOF_RX, check=0, seq=1, cmd=0x02,
            port_len=2, ports=[1, 2],
            data=payload,
        )
        mgr.dispatch(frame)

        data = self._read_exactly(fd, len(payload))
        self.assertEqual(data, payload, "Manager dispatch：读取内容与 frame.data 不符")

    # ── 13. 第三方写入 → transport_send（完整上行路径）──

    @unittest.skipUnless(IS_LINUX, "PTY only on Linux")
    def test_pty_upstream_to_transport(self):
        """第三方写入→PTY读取线程→on_pty_data→encode→transport_send"""
        mgr, logic_table, transport = _make_manager()
        self._managers.append(mgr)
        logic_table.add(0x021, PortPath([1, 3]))
        device_path = mgr.create(0x021, PortType.PORT_485)

        fd = self._open_raw(device_path)
        payload = b'\x11\x22\x33'
        os.write(fd, payload)

        # 等待读取线程处理（最多 1 秒）
        deadline = time.time() + 1.0
        while not transport.sent and time.time() < deadline:
            time.sleep(0.05)

        self.assertTrue(transport.sent, "transport_send 未被调用")
        # encode_fn 透传，sent 内容应等于写入的 payload
        self.assertEqual(b''.join(transport.sent), payload,
                         "上行完整路径：transport 收到的内容与写入不符")


if __name__ == '__main__':
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for cls in (TestVirtualSerialManager, TestVirtualSerialPort,
                TestVirtualSerialReadWrite):
        suite.addTests(loader.loadTestsFromTestCase(cls))
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
