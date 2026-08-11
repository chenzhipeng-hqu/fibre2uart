# -*- coding: utf-8 -*-
"""
tests/test_throughput.py — 通信性能测试模块自测

测试内容：
1. ThroughputTestWorker 线程启动/停止
2. 发送指标收集（无回环串口，仅验证发送侧计数）
3. 数据包生成（固定包模式长度与帧头）
4. ThroughputTestPanel 面板创建与终端口注入
5. 报告生成完整性

运行：PYTHONPATH=project python3 tests/test_throughput.py
"""
import os
import sys
import struct
import threading
import time
import unittest
from unittest.mock import Mock

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)
_PROJ = os.path.join(_BASE, 'project')
if _PROJ not in sys.path:
    sys.path.insert(0, _PROJ)

from ui.throughput_panel import ThroughputTestWorker, ThroughputTestPanel
from frame.models import CommandFrame, SOF_RX


class MockClient:
    """模拟 FibreNetworkClient：send_cmd 立即成功，不走真实链路。"""
    def __init__(self):
        self._transport = Mock()
        self._transport._tx_queue = Mock()
        self._transport._tx_queue.qsize.return_value = 5
        self._session = Mock()
        self._session._pending = {1: None, 2: None, 3: None}
        self._parser = Mock()
        self._parser.on_frame = None

    def send_cmd(self, logical_addr, cmd, data):
        return b'\x00'


def _make_terminal_device():
    from device.models import Device, ModelType
    from protocol.models import PortPath, PortType
    return Device(
        uid=b'\x01' * 12,
        model=ModelType.FIBRE_485,
        port_type=PortType.PORT_485,
        port_path=PortPath([1, 2]),
        logical_addr=0x0001,
        is_terminal=True,
    )


class TestThroughputWorker(unittest.TestCase):
    def test_worker_start_stop(self):
        """线程启动/停止，发送计数 > 0。"""
        client = MockClient()
        worker = ThroughputTestWorker(
            client, 0x0001, "固定包", packet_size=64, interval_ms=10, duration=5,
        )
        worker.start()
        time.sleep(0.5)
        worker.stop()
        worker.join(timeout=3.0)
        self.assertFalse(worker.is_alive())
        m = worker.get_metrics()
        self.assertGreater(m['tx_count'], 0)

    def test_metrics_collection(self):
        """interval_ms=10 + 即时 send_cmd → 约 100 包/秒。"""
        client = MockClient()
        worker = ThroughputTestWorker(
            client, 0x0001, "固定包", packet_size=64, interval_ms=10, duration=5,
        )
        worker.start()
        time.sleep(1.0)
        m = worker.get_metrics()
        worker.stop()
        worker.join(timeout=3.0)
        # 1 秒约发 100 包；给较宽裕的区间，避免 CI/调度抖动
        self.assertGreater(m['tx_count'], 50)
        self.assertLess(m['tx_count'], 200)

    def test_packet_generation(self):
        """固定包模式：payload 长度 = max(packet_size, 4)，帧头 AA 55 + seq。"""
        client = MockClient()
        worker = ThroughputTestWorker(
            client, 0x0001, "固定包", packet_size=100, interval_ms=10, duration=1,
        )
        worker._packet_size = 10
        self.assertEqual(len(worker._generate_payload(1)), 10)
        worker._packet_size = 240
        payload = worker._generate_payload(100)
        self.assertEqual(len(payload), 240)
        # 帧头：AA 55 + seq(2B 大端)
        self.assertEqual(payload[:2], b'\xAA\x55')
        self.assertEqual(payload[2:4], struct.pack('>H', 100))


class TestThroughputPanel(unittest.TestCase):
    def setUp(self):
        from PySide6.QtWidgets import QApplication
        self.app = QApplication.instance() or QApplication(sys.argv)

    def test_panel_creation(self):
        panel = ThroughputTestPanel()
        self.assertIsNotNone(panel._combo_terminal)
        self.assertIsNotNone(panel._combo_mode)

    def test_client_injection_and_device_found(self):
        """client 注入 + 模拟设备发现后终端口下拉非空。"""
        panel = ThroughputTestPanel()
        client = MockClient()
        panel.set_client(client)
        self.assertEqual(panel._client, client)
        panel._on_dev_found(_make_terminal_device())
        self.assertGreater(panel._combo_terminal.count(), 0)

    def test_report_generation(self):
        """生成报告，包含各必要小节。"""
        panel = ThroughputTestPanel()
        client = MockClient()
        panel.set_client(client)
        panel._on_dev_found(_make_terminal_device())
        panel._combo_terminal.setCurrentIndex(0)  # 报告会读 currentData()

        worker = ThroughputTestWorker(
            client, 0x0001, "固定包", packet_size=64, interval_ms=10, duration=2,
        )
        worker.start()
        time.sleep(0.3)
        worker.stop()
        worker.join(timeout=3.0)
        panel._worker = worker

        report = panel._generate_report(worker.get_metrics())
        self.assertIn("# 通信性能测试报告", report)
        self.assertIn("## 测试配置", report)
        self.assertIn("## 汇总指标", report)
        self.assertIn("## 延迟分布", report)


class TestReversePath(unittest.TestCase):
    """反向链路捕获与校验（#1）：B.TX 回写 + on_frame 拦截 + AA55+seq 反向匹配 + 反向指标。"""

    def _make_worker(self, return_timeout=1.0):
        client = MockClient()
        w = ThroughputTestWorker(
            client, 0x0001, "固定包", packet_size=64, interval_ms=10, duration=5,
            return_timeout=return_timeout,
        )
        return w

    @staticmethod
    def _frame(data: bytes, sof=SOF_RX, cmd=0x01):
        return CommandFrame(sof=sof, check=0, seq=0, cmd=cmd,
                            port_len=0, ports=[], data=data)

    # ── _on_return_frame ──────────────────────────────────────────

    def test_return_match_success(self):
        """已登记的待回程 seq，回程帧 data 与 payload 一致 → 反向成功、无损坏。"""
        w = self._make_worker()
        payload = w._generate_payload(7)
        w._register_pending_return(7, payload)
        w._on_return_frame(self._frame(payload))
        m = w.get_metrics()
        self.assertEqual(m['rx_count_return'], 1)
        self.assertEqual(m['rx_bytes_return'], len(payload))
        self.assertEqual(m['data_corrupt_count_return'], 0)
        self.assertEqual(m['loopback_miss_count_return'], 0)
        self.assertNotIn(7, w._pending_returns)

    def test_return_match_corrupt(self):
        """回程帧 data 与 payload 不一致 → 反向收到但损坏。"""
        w = self._make_worker()
        payload = w._generate_payload(3)
        w._register_pending_return(3, payload)
        bad = bytearray(payload)
        bad[5] ^= 0xFF
        w._on_return_frame(self._frame(bytes(bad)))
        m = w.get_metrics()
        self.assertEqual(m['rx_count_return'], 1)
        self.assertEqual(m['data_corrupt_count_return'], 1)

    def test_return_frame_unknown_seq_ignored(self):
        """回程帧 seq 未在待回程表中 → 忽略（不增任何反向指标）。"""
        w = self._make_worker()
        w._on_return_frame(self._frame(w._generate_payload(11)))
        self.assertEqual(w.get_metrics()['rx_count_return'], 0)

    def test_return_frame_non_passthrough_ignored(self):
        """cmd>=0x10 的帧不进入反向处理（即便 data 含 AA55）。"""
        w = self._make_worker()
        payload = w._generate_payload(4)
        w._register_pending_return(4, payload)
        w._on_return_frame(self._frame(payload, cmd=0x24))
        self.assertEqual(w.get_metrics()['rx_count_return'], 0)

    # ── hook 装卸 / 过滤 / 透传 ───────────────────────────────────

    def test_return_hook_filter_and_passthrough(self):
        """0xAB/cmd<0x10 → 反向处理 + 调原回调；cmd>=0x10 → 仅原回调；卸载还原。"""
        w = self._make_worker()
        original = Mock()
        w._client._parser.on_frame = original
        payload = w._generate_payload(2)
        w._register_pending_return(2, payload)

        w._install_return_hook()
        # 透传帧（cmd>=0x10）：只调原回调，不计反向
        f_high = self._frame(b'\x00', cmd=0x24)
        w._client._parser.on_frame(f_high)
        original.assert_called_once_with(f_high)
        self.assertEqual(w.get_metrics()['rx_count_return'], 0)

        # 回程帧（0xAB/cmd<0x10）：反向处理 + 原回调
        original.reset_mock()
        f_ret = self._frame(payload, cmd=0x01)
        w._client._parser.on_frame(f_ret)
        original.assert_called_once_with(f_ret)
        self.assertEqual(w.get_metrics()['rx_count_return'], 1)

        # 卸载还原
        w._uninstall_return_hook()
        self.assertIs(w._client._parser.on_frame, original)

    # ── B.TX 回写 ─────────────────────────────────────────────────

    def test_b_tx_echo(self):
        """_loopback_reader 正向匹配成功后，把收到的字节 write 到 B.TX。"""
        w = self._make_worker()
        payload = w._generate_payload(9)
        mock_ser = Mock()
        mock_ser.is_open = True
        mock_ser.read.return_value = payload
        mock_ser.in_waiting = len(payload)
        w._loopback_serial = mock_ser
        w._pending_queue.put((9, time.time(), payload))
        # 以线程方式跑读者：处理这一项（read→match→回写 B.TX）。stop_flag 不能预先置，
        # 否则读者内部读循环（while … and not stop_flag）会被跳过、读不到数据。
        reader = threading.Thread(target=w._loopback_reader, daemon=True)
        reader.start()
        _deadline = time.time() + 2.0
        while time.time() < _deadline and not mock_ser.write.called:
            time.sleep(0.02)
        self.assertTrue(mock_ser.write.called, "B.TX 回写未发生")
        w._stop_flag.set()
        reader.join(timeout=2.0)
        mock_ser.write.assert_called_once_with(payload)

    # ── 反向 miss 超时 ────────────────────────────────────────────

    def test_return_miss_timeout(self):
        """待回程项过 deadline 仍未回 → 反向 miss。"""
        w = self._make_worker()
        payload = w._generate_payload(5)
        w._register_pending_return(5, payload, deadline=0.0)  # 已过期
        w._reap_expired_returns()
        self.assertEqual(w.get_metrics()['loopback_miss_count_return'], 1)
        self.assertNotIn(5, w._pending_returns)

    # ── 防御路径 / 端到端 ─────────────────────────────────────────

    def test_return_frame_short_data_ignored(self):
        """回程帧 data < 4 字节 → _extract_seq 返回 None，早期返回，不计反向指标。"""
        w = self._make_worker()
        w._on_return_frame(self._frame(b'\xAA\x55\x01', cmd=0x01))  # 3 字节，不足以解 seq
        m = w.get_metrics()
        self.assertEqual(m['rx_count_return'], 0)
        self.assertEqual(m['rx_bytes_return'], 0)

    def test_round_trip_integration(self):
        """端到端：B 收到正向 → 回写 B.TX → 触发回程帧 → hook 反向匹配 → 反向指标。"""
        w = self._make_worker()
        payload = w._generate_payload(8)
        seq = 8
        # mock 回环串口：read 返回正向 payload；write（B.TX 回写）模拟设备反向回传一个 0xAB 帧
        mock_ser = Mock()
        mock_ser.is_open = True
        mock_ser.read.return_value = payload
        mock_ser.in_waiting = len(payload)

        def _fake_write(data):
            # 设备把 B.TX 的数据作为 0xAB 透传帧反向送回 A.RX → 经 hook 拦截
            w._client._parser.on_frame(self._frame(data, cmd=0x01))

        mock_ser.write.side_effect = _fake_write
        w._loopback_serial = mock_ser
        w._install_return_hook()
        w._register_pending_return(seq, payload)
        w._pending_queue.put((seq, time.time(), payload))
        # 跑读者线程：read→正向匹配→write(B.TX)→触发回程帧→hook→反向匹配
        reader = threading.Thread(target=w._loopback_reader, daemon=True)
        reader.start()
        _deadline = time.time() + 2.0
        while time.time() < _deadline and w.get_metrics()['rx_count_return'] == 0:
            time.sleep(0.02)
        w._stop_flag.set()
        reader.join(timeout=2.0)
        w._uninstall_return_hook()
        m = w.get_metrics()
        self.assertEqual(m['rx_count_return'], 1)       # 反向匹配成功
        self.assertEqual(m['data_corrupt_count_return'], 0)
        self.assertGreater(m['rx_bytes_return'], 0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
