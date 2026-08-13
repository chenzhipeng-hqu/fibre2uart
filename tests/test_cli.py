# -*- coding: utf-8 -*-
"""
tests/test_cli.py — 无头 CLI 入口自测（ADR-0002 / #4）

测试 cli 的外部可观察行为：给定配置/输入 → stdout JSON 内容、退出码、资源清理。
硬件/并发用依赖注入隔离（mock client + mock worker）。

运行：PYTHONPATH=project python3 tests/test_cli.py
"""
import io
import json
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)
_PROJ = os.path.join(_BASE, 'project')
if _PROJ not in sys.path:
    sys.path.insert(0, _PROJ)

import cli  # noqa: E402


def _write_ini(tmp: str, **sections) -> str:
    """写一份 ini 到 tmp，sections = {'serial': {'port': ...}, ...}。"""
    path = os.path.join(tmp, "test.ini")
    lines = []
    for sec, kv in sections.items():
        lines.append(f"[{sec}]")
        for k, v in kv.items():
            lines.append(f"{k} = {v}")
        lines.append("")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return path


def _default_terminal_ini(tmp, **overrides):
    """一份能跑通的最小 ini：串口 A + 回环 B + 终端口 + headless（discovery_timeout 调小便于测）。"""
    serial = {"port": "/dev/ttyUSB0", "baudrate": "1000000"}
    throughput = {
        "terminal_addr": "1",
        "loopback_port": "/dev/ttyUSB1",
        "loopback_baudrate": "1000000",
        "mode": "固定包",
        "packet_size": "64",
        "interval_ms": "10",
        "duration": "1",
    }
    headless = {"discovery_timeout": "2", "log_level": "INFO"}
    serial.update(overrides.pop("serial", {}))
    throughput.update(overrides.pop("throughput", {}))
    headless.update(overrides.pop("headless", {}))
    return _write_ini(tmp, serial=serial, throughput=throughput, headless=headless)


class _FakeRouteReady:
    """模拟 client._discovery.route_ready：初始 False，connect 后可被设 True。"""
    def __init__(self):
        self.route_ready = False


def _make_mock_client(route_ready_obj=None, device=None, connect_raises=False):
    """构造一个行为可控的 mock FibreNetworkClient。"""
    client = Mock()
    client._discovery = route_ready_obj or _FakeRouteReady()
    client.get_device_by_addr.return_value = device
    if connect_raises:
        client.connect.side_effect = OSError("port not found")
    # on() 注册回调，测试可通过触发 ready 事件推进
    client._on_callbacks = {}

    def _on(event_type, callback):
        client._on_callbacks[event_type] = callback
        return Mock()

    client.on.side_effect = _on
    return client


def _make_mock_worker(metrics=None, alive_duration=0.0):
    """构造一个 mock ThroughputTestWorker：start 立即让 is_alive 在短时间后变 False。"""
    worker = Mock()
    worker._started = False

    default_metrics = {
        'tx_count': 10, 'tx_ok_count': 10, 'rx_count': 10,
        'send_err_count': 0, 'loopback_miss_count': 0,
        'data_corrupt_count': 0, 'latencies': [0.01],
        'tx_bytes': 640, 'rx_bytes': 640,
        'rx_count_return': 10, 'rx_bytes_return': 640,
        'data_corrupt_count_return': 0, 'loopback_miss_count_return': 0,
        'latencies_roundtrip': [0.02],
    }
    if metrics:
        default_metrics.update(metrics)
    worker.get_metrics.return_value = default_metrics

    import threading, time
    stop_event = threading.Event()

    def _start():
        worker._started = True
        # 模拟 duration 到点自动停（alive_duration 后）
        if alive_duration > 0:
            def _auto():
                time.sleep(alive_duration)
                stop_event.set()
            threading.Thread(target=_auto, daemon=True).start()

    def _is_alive():
        return worker._started and not stop_event.is_set()

    def _stop():
        stop_event.set()

    worker.start.side_effect = _start
    worker.is_alive.side_effect = _is_alive
    worker.stop.side_effect = _stop
    return worker


def _make_terminal_device():
    from device.models import Device, ModelType
    from protocol.models import PortPath, PortType
    return Device(
        uid=b'\x01' * 12, model=ModelType.FIBRE_485,
        port_type=PortType.PORT_485, port_path=PortPath([1, 2]),
        logical_addr=0x0001, is_terminal=True,
    )


class TestCliJsonContract(unittest.TestCase):
    """stdout JSON 契约：合法 JSON、含全部字段。"""

    def test_pass_path_outputs_valid_json_with_all_fields(self):
        """全绿路径：stdout 是合法 JSON，含 pass/summary/metrics/thresholds/violations。"""
        with tempfile.TemporaryDirectory() as tmp:
            ini = _default_terminal_ini(tmp)
            client = _make_mock_client(device=_make_terminal_device())
            worker = _make_mock_worker()
            out = io.StringIO()

            # connect 后触发 route_ready=True（模拟发现完成）
            def _connect(*a, **kw):
                client._discovery.route_ready = True
            client.connect.side_effect = _connect

            rc = cli.run_headless(ini,
                                  client_factory=lambda: client,
                                  worker_factory=lambda *a, **kw: worker,
                                  stdout=out)
            data = json.loads(out.getvalue())  # 不抛 = stdout 纯 JSON
            self.assertEqual(rc, 0)
            self.assertTrue(data['pass'])
            self.assertIsInstance(data['summary'], str)
            self.assertIn('metrics', data)
            self.assertIn('tx_count', data['metrics'])
            self.assertIn('rx_count_return', data['metrics'])
            self.assertIn('latencies_roundtrip', data['metrics'])
            self.assertIn('thresholds', data)
            self.assertIn('violations', data)
            self.assertEqual(data['violations'], [])

    def test_fail_path_exit_code_1(self):
        """阈值超限（有丢包）→ pass=False、退出码 1、violations 非空、metrics 照样吐。"""
        with tempfile.TemporaryDirectory() as tmp:
            ini = _default_terminal_ini(tmp)
            client = _make_mock_client(device=_make_terminal_device())
            worker = _make_mock_worker(metrics={
                'tx_count': 10, 'rx_count_return': 8,  # 丢 2 包
                'data_corrupt_count_return': 0,
            })
            out = io.StringIO()
            client.connect.side_effect = lambda *a, **kw: setattr(
                client._discovery, 'route_ready', True)

            rc = cli.run_headless(ini,
                                  client_factory=lambda: client,
                                  worker_factory=lambda *a, **kw: worker,
                                  stdout=out)
            data = json.loads(out.getvalue())
            self.assertEqual(rc, 1)
            self.assertFalse(data['pass'])
            self.assertGreater(len(data['violations']), 0)
            self.assertIn('tx_count', data['metrics'])  # metrics 照样完整


class TestCliExitCodes(unittest.TestCase):
    """退出码映射。"""

    def test_no_config_arg_exit_2(self):
        self.assertEqual(cli.main([]), 2)

    def test_missing_config_file_exit_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc = cli.main(["-c", os.path.join(tmp, "nope.ini")])
            self.assertEqual(rc, 2)

    def test_connect_failure_exit_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            ini = _default_terminal_ini(tmp)
            client = _make_mock_client(connect_raises=True)
            out = io.StringIO()
            rc = cli.run_headless(ini,
                                  client_factory=lambda: client,
                                  worker_factory=lambda *a, **kw: _make_mock_worker(),
                                  stdout=out)
            self.assertEqual(rc, 2)

    def test_discovery_timeout_exit_2(self):
        """拓扑恒未就绪 → 超时退出 2。"""
        with tempfile.TemporaryDirectory() as tmp:
            ini = _default_terminal_ini(tmp, headless={"discovery_timeout": "1"})
            client = _make_mock_client(device=_make_terminal_device())
            client._discovery.route_ready = False  # 恒不就绪
            out = io.StringIO()
            rc = cli.run_headless(ini,
                                  client_factory=lambda: client,
                                  worker_factory=lambda *a, **kw: _make_mock_worker(),
                                  stdout=out)
            self.assertEqual(rc, 2)

    def test_target_terminal_not_found_exit_2(self):
        """目标终端口不存在（路由表里没有）→ 退出 2。"""
        with tempfile.TemporaryDirectory() as tmp:
            ini = _default_terminal_ini(tmp)
            client = _make_mock_client(device=None)  # 找不到
            client.connect.side_effect = lambda *a, **kw: setattr(
                client._discovery, 'route_ready', True)
            out = io.StringIO()
            rc = cli.run_headless(ini,
                                  client_factory=lambda: client,
                                  worker_factory=lambda *a, **kw: _make_mock_worker(),
                                  stdout=out)
            self.assertEqual(rc, 2)


class TestCliInterrupted(unittest.TestCase):
    """SIGINT 响应（review #4 加固）：长阻塞期间能及时打断。"""

    def test_wait_discovery_interrupted_returns_fast(self):
        """_wait_discovery 的 interrupted 回调为 True → 立即返回 False，不等 timeout。"""
        client = _make_mock_client()
        client._discovery.route_ready = False  # 恒不就绪
        # interrupted 在 0.3s 后变 True
        flag = {"v": False}

        def _interrupted():
            return flag["v"]

        import threading
        def _fire():
            time.sleep(0.3)
            flag["v"] = True
        threading.Thread(target=_fire, daemon=True).start()

        t0 = time.time()
        ok = cli._wait_discovery(client, timeout=30, interrupted=_interrupted)
        elapsed = time.time() - t0
        self.assertFalse(ok)
        self.assertLess(elapsed, 5.0, "interrupted 应在 5s 内打断，而非等满 30s timeout")


class TestCliResourceCleanup(unittest.TestCase):
    """资源清理：无论成败，worker.stop/join、client.disconnect 都被调。"""

    def test_pass_path_cleans_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            ini = _default_terminal_ini(tmp)
            client = _make_mock_client(device=_make_terminal_device())
            worker = _make_mock_worker()
            client.connect.side_effect = lambda *a, **kw: setattr(
                client._discovery, 'route_ready', True)
            cli.run_headless(ini,
                             client_factory=lambda: client,
                             worker_factory=lambda *a, **kw: worker,
                             stdout=io.StringIO())
            worker.stop.assert_called()
            worker.join.assert_called()
            client.disconnect.assert_called()

    def test_discovery_timeout_still_disconnects(self):
        """发现超时也要断开已建立的连接。"""
        with tempfile.TemporaryDirectory() as tmp:
            ini = _default_terminal_ini(tmp, headless={"discovery_timeout": "1"})
            client = _make_mock_client()
            client._discovery.route_ready = False
            cli.run_headless(ini,
                             client_factory=lambda: client,
                             worker_factory=lambda *a, **kw: _make_mock_worker(),
                             stdout=io.StringIO())
            client.disconnect.assert_called()


class TestCliNoPyside(unittest.TestCase):
    """cli.py 不得 import PySide6（ADR-0002 环境隔离）。"""

    def test_no_pyside_import(self):
        import ast
        cli_path = os.path.join(_PROJ, "cli.py")
        with open(cli_path, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for n in node.names:
                    self.assertFalse(
                        n.name.startswith("PySide6"),
                        f"cli.py 不应 import PySide6（发现 import {n.name}）")
            elif isinstance(node, ast.ImportFrom):
                self.assertFalse(
                    node.module and node.module.startswith("PySide6"),
                    f"cli.py 不应 from PySide6 import（发现 {node.module}）")


if __name__ == "__main__":
    unittest.main(verbosity=2)
