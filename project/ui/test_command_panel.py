# -*- coding: utf-8 -*-
"""
ui/test_command_panel.py — CommandPanel 单元测试

测试项（无需 Qt 显示，使用 QApplication 空实例）：
  CommandPanelBroadcast:
    1.  广播模式下 send_broadcast 被调用，PortPath 内部为 [0x00]
    2.  广播模式下 addr 存 "0" 到 config
    3.  广播模式下地址框禁用、文本为 0x00
    4.  set_target 在广播模式下被忽略
    5.  切回逻辑地址时地址框启用
  CommandPanelInterval:
    6.  间隔=0 时不等待（发两条总耗时 < 100ms）
    7.  间隔 > 0 时最后一条不等待（count=2，只等 1 次）
    8.  等待期间 cancel 可立即中断间隔
  CommandPanelBroadcastMinInterval:
    9.  广播最小间隔兜底：user_interval < min_interval 时使用 min_interval
    10. 广播最小间隔兜底：user_interval >= min_interval 时使用 user_interval
  CommandPanelConfig:
    11. command_interval_ms 默认值为 0
    12. broadcast_min_interval_ms 默认值为 10
    13. set_command_config 含 interval_ms 正确持久化
"""
from __future__ import annotations

import configparser
import os
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import Future
from unittest.mock import MagicMock, patch, call

# ── 确保 project/ 在 sys.path ──────────────────────────────────────────
_PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_DIR not in sys.path:
    sys.path.insert(0, _PROJECT_DIR)

# 必须在 import Qt 前设置无显示环境
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

_app = None


def _get_app():
    global _app
    if _app is None:
        _app = QApplication.instance() or QApplication(sys.argv)
    return _app


# ── Mock 辅助 ──────────────────────────────────────────────────────────

class _MockSession:
    def send_request(self, port_path, cmd, data, timeout=2.0):
        fut = Future()
        m = MagicMock()
        m.data = b'\xDE\xAD'
        fut.set_result(m)
        return fut


class _MockClient:
    """模拟 FibreNetworkClient，记录 send_broadcast / send_cmd 调用。"""

    def __init__(self):
        self._session = _MockSession()
        self.broadcast_calls: list[tuple] = []   # (cmd, data)
        self.send_cmd_calls: list[tuple] = []    # (addr, cmd, data)

    def send_broadcast(self, cmd: int, data: bytes) -> None:
        self.broadcast_calls.append((cmd, data))

    def send_cmd(self, addr: int, cmd: int, data: bytes) -> Future:
        self.send_cmd_calls.append((addr, cmd, data))
        fut: Future = Future()
        m = MagicMock()
        m.data = b'\xDE\xAD'
        fut.set_result(m)
        return fut


def _make_panel(mock_client=None):
    """创建 CommandPanel 并注入 MockClient，跳过 config 加载。"""
    from ui.command_panel import CommandPanel
    _get_app()
    with patch("ui.command_panel.CommandPanel._load_config"):
        panel = CommandPanel()
    panel.set_client(mock_client or _MockClient())
    return panel


def _wait_idle(panel, timeout=3.0) -> bool:
    """等待发送线程结束（_busy 变回 False），期间处理 Qt 事件。"""
    from PySide6.QtCore import QCoreApplication
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QCoreApplication.processEvents()
        if not panel._busy:
            return True
        time.sleep(0.01)
    return False


# ══════════════════════════════════════════════════════════════════════
# 1. 广播相关
# ══════════════════════════════════════════════════════════════════════

class CommandPanelBroadcast(unittest.TestCase):

    def setUp(self):
        self._client = _MockClient()
        self._panel = _make_panel(self._client)

    def tearDown(self):
        self._panel.close()

    def test_broadcast_calls_send_broadcast(self):
        """广播模式发送后 send_broadcast 被调用一次"""
        p = self._panel
        p._combo_mode.setCurrentText("广播")
        p._edit_cmd.setText("25")
        p._edit_data.setText("01 02")
        p._spin_count.setValue(1)
        p._spin_interval.setValue(0)

        p._on_send()
        _wait_idle(p)

        self.assertEqual(len(self._client.broadcast_calls), 1)
        cmd, data = self._client.broadcast_calls[0]
        self.assertEqual(cmd, 0x25)
        self.assertEqual(data, b'\x01\x02')

    def test_broadcast_send_cmd_not_called(self):
        """广播模式不调用 send_cmd"""
        p = self._panel
        p._combo_mode.setCurrentText("广播")
        p._edit_cmd.setText("25")
        p._spin_count.setValue(1)
        p._spin_interval.setValue(0)

        p._on_send()
        _wait_idle(p)

        self.assertEqual(len(self._client.send_cmd_calls), 0)

    def test_broadcast_addr_box_disabled(self):
        """广播模式下地址框被禁用"""
        p = self._panel
        p._combo_mode.setCurrentText("广播")
        self.assertFalse(p._edit_addr.isEnabled())

    def test_broadcast_addr_box_shows_0x00(self):
        """广播模式下地址框显示 0x00"""
        p = self._panel
        p._combo_mode.setCurrentText("广播")
        self.assertEqual(p._edit_addr.text(), "0x00")

    def test_set_target_ignored_in_broadcast_mode(self):
        """广播模式下 set_target 不改变地址框"""
        p = self._panel
        p._combo_mode.setCurrentText("广播")
        p.set_target(0x0801)
        self.assertEqual(p._edit_addr.text(), "0x00")

    def test_switch_back_to_logic_addr_enables_box(self):
        """从广播切回逻辑地址后地址框重新启用"""
        p = self._panel
        p._combo_mode.setCurrentText("广播")
        p._combo_mode.setCurrentText("逻辑地址")
        self.assertTrue(p._edit_addr.isEnabled())

    def test_broadcast_saves_addr_as_zero(self):
        """广播模式保存配置时 addr 存 '0'"""
        p = self._panel
        p._combo_mode.setCurrentText("广播")
        p._edit_cmd.setText("25")

        saved = {}

        def mock_set(addr_mode, addr, cmd, data, count, interval_ms=0):
            saved['addr_mode'] = addr_mode
            saved['addr'] = addr

        with patch("ui.command_panel.CommandPanel._save_config") as m:
            # 直接调内部 _save_config 验证逻辑
            pass

        # 通过 patch get_config 验证写入值
        mock_cfg = MagicMock()
        mock_cfg.set_command_config.side_effect = mock_set
        with patch("ui.command_panel.get_config", return_value=mock_cfg, create=True):
            # 在 _save_config 内部 import 的是局部 from config import get_config
            # 用另一种方式：直接调 _save_config 并 patch 模块内的 get_config
            import ui.command_panel as _mod
            orig = getattr(_mod, 'get_config', None)
            try:
                import importlib
                cfg_mod = importlib.import_module('config')
                orig_fn = cfg_mod.get_config
                cfg_mod.get_config = lambda: mock_cfg
                p._save_config()
                self.assertEqual(saved.get('addr_mode'), "广播")
                self.assertEqual(saved.get('addr'), "0")
            finally:
                cfg_mod.get_config = orig_fn


# ══════════════════════════════════════════════════════════════════════
# 2. 发送间隔
# ══════════════════════════════════════════════════════════════════════

class CommandPanelInterval(unittest.TestCase):

    def setUp(self):
        self._client = _MockClient()
        self._panel = _make_panel(self._client)

    def tearDown(self):
        self._panel.close()

    def _setup_logic(self, count, interval_ms):
        p = self._panel
        p._combo_mode.setCurrentText("逻辑地址")
        p._edit_addr.setText("0x0801")
        p._edit_cmd.setText("25")
        p._edit_data.setText("")
        p._spin_count.setValue(count)
        p._spin_interval.setValue(interval_ms)

    def test_zero_interval_fast(self):
        """间隔=0 时，发送 2 条总耗时 < 200ms"""
        self._setup_logic(count=2, interval_ms=0)
        t0 = time.monotonic()
        self._panel._on_send()
        _wait_idle(self._panel)
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 0.2)

    def test_last_send_no_wait(self):
        """count=2 时只等 1 次间隔，不多等（总耗时 ≈ 1*interval，< 2*interval）"""
        interval_ms = 100
        self._setup_logic(count=2, interval_ms=interval_ms)
        t0 = time.monotonic()
        self._panel._on_send()
        _wait_idle(self._panel, timeout=5.0)
        elapsed_ms = (time.monotonic() - t0) * 1000
        # 应只等 1 次 ≈ 100ms，不会等 2 次 ≈ 200ms
        self.assertGreaterEqual(elapsed_ms, interval_ms * 0.8)
        self.assertLess(elapsed_ms, interval_ms * 1.8)

    def test_cancel_interrupts_interval_wait(self):
        """等待间隔期间中断，立即停止"""
        interval_ms = 2000   # 等 2 秒
        self._setup_logic(count=3, interval_ms=interval_ms)

        self._panel._on_send()
        time.sleep(0.15)   # 等第一条发完，进入间隔等待
        self._panel._cancel_event.set()  # 模拟中断

        ok = _wait_idle(self._panel, timeout=1.0)  # 应在 1 秒内结束
        self.assertTrue(ok, "中断后未在 1 秒内结束，可能 wait() 未响应 cancel_event")
        # 不应发满 3 条
        self.assertLess(len(self._client.send_cmd_calls), 3)


# ══════════════════════════════════════════════════════════════════════
# 3. 广播最小间隔兜底
# ══════════════════════════════════════════════════════════════════════

class CommandPanelBroadcastMinInterval(unittest.TestCase):

    def setUp(self):
        self._client = _MockClient()
        self._panel = _make_panel(self._client)

    def tearDown(self):
        self._panel.close()

    def _run_broadcast(self, count, user_interval_ms, min_interval_ms):
        """运行广播，返回总耗时（ms）。"""
        p = self._panel
        p._combo_mode.setCurrentText("广播")
        p._edit_cmd.setText("25")
        p._edit_data.setText("")
        p._spin_count.setValue(count)
        p._spin_interval.setValue(user_interval_ms)

        mock_cfg = MagicMock()
        mock_cfg.broadcast_min_interval_ms = min_interval_ms

        import config as cfg_mod
        orig = cfg_mod.get_config
        cfg_mod.get_config = lambda: mock_cfg
        try:
            t0 = time.monotonic()
            p._on_send()
            _wait_idle(p, timeout=5.0)
            return (time.monotonic() - t0) * 1000
        finally:
            cfg_mod.get_config = orig

    def test_min_interval_enforced_when_user_too_small(self):
        """user_interval(0ms) < min_interval(50ms)，实际应用 50ms"""
        elapsed = self._run_broadcast(count=2, user_interval_ms=0, min_interval_ms=50)
        # count=2 只等 1 次间隔，应 >= 50ms
        self.assertGreaterEqual(elapsed, 40)

    def test_user_interval_used_when_larger(self):
        """user_interval(100ms) >= min_interval(10ms)，实际应用 100ms"""
        elapsed = self._run_broadcast(count=2, user_interval_ms=100, min_interval_ms=10)
        # 应 >= 100ms
        self.assertGreaterEqual(elapsed, 80)
        # 不应超过 2 次间隔（最后一条不等）
        self.assertLess(elapsed, 200)


# ══════════════════════════════════════════════════════════════════════
# 4. config 新字段
# ══════════════════════════════════════════════════════════════════════

class CommandPanelConfig(unittest.TestCase):

    def _make_config(self, ini_content: str = ""):
        """在临时目录创建 AppConfig，可选预设 ini 内容。"""
        import config as cfg_mod
        from config import AppConfig, reset_config
        tmp = tempfile.mkdtemp()
        cfg_path = os.path.join(tmp, "config.ini")
        if ini_content:
            with open(cfg_path, "w", encoding="utf-8") as f:
                f.write(ini_content)
        orig = cfg_mod.CONFIG_FILE
        cfg_mod.CONFIG_FILE = cfg_path
        reset_config()
        try:
            c = AppConfig()
        finally:
            cfg_mod.CONFIG_FILE = orig
            reset_config()
        return c, cfg_path, tmp

    def test_command_interval_ms_default(self):
        """command_interval_ms 默认值为 0"""
        c, _, _ = self._make_config()
        self.assertEqual(c.command_interval_ms, 0)

    def test_broadcast_min_interval_ms_default(self):
        """broadcast_min_interval_ms 默认值为 10"""
        c, _, _ = self._make_config()
        self.assertEqual(c.broadcast_min_interval_ms, 10)

    def test_set_command_config_saves_interval_ms(self):
        """set_command_config 含 interval_ms 正确写入并读回"""
        import config as cfg_mod
        from config import AppConfig, reset_config
        tmp = tempfile.mkdtemp()
        cfg_path = os.path.join(tmp, "config.ini")
        orig = cfg_mod.CONFIG_FILE
        cfg_mod.CONFIG_FILE = cfg_path
        reset_config()
        try:
            c1 = AppConfig()
            c1.set_command_config(
                addr_mode="逻辑地址", addr="0x0801",
                cmd="25", data="", count=1, interval_ms=200
            )
            reset_config()
            c2 = AppConfig()
            self.assertEqual(c2.command_interval_ms, 200)
        finally:
            cfg_mod.CONFIG_FILE = orig
            reset_config()

    def test_broadcast_min_interval_ms_from_ini(self):
        """broadcast_min_interval_ms 可通过 config.ini 配置"""
        ini = "[broadcast]\nmin_interval_ms = 50\n"
        c, _, _ = self._make_config(ini)
        self.assertEqual(c.broadcast_min_interval_ms, 50)


if __name__ == "__main__":
    unittest.main(verbosity=2)
