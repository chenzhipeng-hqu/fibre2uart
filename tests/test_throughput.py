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


class MockClient:
    """模拟 FibreNetworkClient：send_cmd 立即成功，不走真实链路。"""
    def __init__(self):
        self._transport = Mock()
        self._transport._tx_queue = Mock()
        self._transport._tx_queue.qsize.return_value = 5
        self._session = Mock()
        self._session._pending = {1: None, 2: None, 3: None}

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


if __name__ == '__main__':
    unittest.main(verbosity=2)
