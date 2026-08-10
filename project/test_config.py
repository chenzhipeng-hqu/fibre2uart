# -*- coding: utf-8 -*-
"""
test_config.py — AppConfig 模块自测

测试项：
  1. 默认值正确性
  2. hotplug/rs485_timeout/rs485_max_addr clamp 边界
  3. save() 写文件后可正确 reload
  4. 配置文件覆盖默认值
  5. clamp 下界：rs485_timeout=0 → 0.01；rs485_max_addr=0 → 1
  6. clamp 上界：rs485_timeout=99 → 5.0；rs485_max_addr=200 → 127
"""
from __future__ import annotations

import configparser
import os
import sys
import tempfile
import unittest

# ── 确保 project/ 在 sys.path ──────────────────────────────────────────
_PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
if _PROJECT_DIR not in sys.path:
    sys.path.insert(0, _PROJECT_DIR)

import config as cfg_module
from config import AppConfig, reset_config


class _PatchConfigFile:
    """上下文管理器：临时替换 CONFIG_FILE 路径，保护真实配置文件。"""

    def __init__(self, tmp_dir: str) -> None:
        self._tmp_dir = tmp_dir
        self._orig_path: str = ""
        self._tmp_path: str = ""

    def __enter__(self) -> str:
        self._orig_path = cfg_module.CONFIG_FILE
        self._tmp_path = os.path.join(self._tmp_dir, "config_test.cfg")
        cfg_module.CONFIG_FILE = self._tmp_path
        reset_config()
        return self._tmp_path

    def __exit__(self, *_) -> None:
        cfg_module.CONFIG_FILE = self._orig_path
        reset_config()


class TestAppConfigDefaults(unittest.TestCase):
    """测试默认值。"""

    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp()
        self._patch = _PatchConfigFile(self._tmp)
        self._patch.__enter__()

    def tearDown(self) -> None:
        self._patch.__exit__()

    def test_hotplug_default_false(self) -> None:
        """hotplug 默认值为 False"""
        c = AppConfig()
        self.assertFalse(c.hotplug)

    def test_rs485_timeout_default(self) -> None:
        """rs485_timeout 默认值为 0.05s"""
        c = AppConfig()
        self.assertAlmostEqual(c.rs485_timeout, 0.05)

    def test_rs485_max_addr_default(self) -> None:
        """rs485_max_addr 默认值为 127（0x7F）"""
        c = AppConfig()
        self.assertEqual(c.rs485_max_addr, 127)

    def test_baudrate_default(self) -> None:
        """baudrate 默认值为 1000000"""
        c = AppConfig()
        self.assertEqual(c.baudrate, 1000000)

    def test_serial_port_default_empty(self) -> None:
        """serial_port 默认值为空串"""
        c = AppConfig()
        self.assertEqual(c.serial_port, "")


class TestAppConfigClamp(unittest.TestCase):
    """测试 clamp 边界。"""

    def _make_config_with(self, section: str, key: str, value: str) -> AppConfig:
        tmp = tempfile.mkdtemp()
        cfg_path = os.path.join(tmp, "c.cfg")
        parser = configparser.ConfigParser()
        parser["serial"] = {"port": "", "baudrate": "921600"}
        parser["discovery"] = {
            "hotplug": "false",
            "rs485_timeout": "0.05",
            "rs485_max_addr": "127",
        }
        parser[section][key] = value
        with open(cfg_path, "w") as f:
            parser.write(f)

        orig = cfg_module.CONFIG_FILE
        cfg_module.CONFIG_FILE = cfg_path
        reset_config()
        try:
            return AppConfig()
        finally:
            cfg_module.CONFIG_FILE = orig
            reset_config()

    def test_rs485_timeout_clamp_lower(self) -> None:
        """rs485_timeout=0 被 clamp 到下界 0.01"""
        c = self._make_config_with("discovery", "rs485_timeout", "0")
        self.assertAlmostEqual(c.rs485_timeout, 0.01)

    def test_rs485_timeout_clamp_upper(self) -> None:
        """rs485_timeout=99 被 clamp 到上界 5.0"""
        c = self._make_config_with("discovery", "rs485_timeout", "99")
        self.assertAlmostEqual(c.rs485_timeout, 5.0)

    def test_rs485_max_addr_clamp_lower(self) -> None:
        """rs485_max_addr=0 被 clamp 到下界 1"""
        c = self._make_config_with("discovery", "rs485_max_addr", "0")
        self.assertEqual(c.rs485_max_addr, 1)

    def test_rs485_max_addr_clamp_upper(self) -> None:
        """rs485_max_addr=200 被 clamp 到上界 127"""
        c = self._make_config_with("discovery", "rs485_max_addr", "200")
        self.assertEqual(c.rs485_max_addr, 127)

    def test_rs485_max_addr_exact_max(self) -> None:
        """rs485_max_addr=127 保持不变（边界值）"""
        c = self._make_config_with("discovery", "rs485_max_addr", "127")
        self.assertEqual(c.rs485_max_addr, 127)


class TestAppConfigSaveReload(unittest.TestCase):
    """测试 save() 后可正确 reload。"""

    def test_save_and_reload(self) -> None:
        """修改配置后 save()，再加载能读取到新值"""
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = os.path.join(tmp, "sub", "config.ini")
            orig = cfg_module.CONFIG_FILE
            cfg_module.CONFIG_FILE = cfg_path
            reset_config()
            try:
                c1 = AppConfig()
                # 修改配置项
                c1._cfg["discovery"]["hotplug"] = "true"
                c1._cfg["discovery"]["rs485_timeout"] = "0.1"
                c1._cfg["discovery"]["rs485_max_addr"] = "64"
                c1.save()

                # 重新加载
                reset_config()
                c2 = AppConfig()
                self.assertTrue(c2.hotplug)
                self.assertAlmostEqual(c2.rs485_timeout, 0.1)
                self.assertEqual(c2.rs485_max_addr, 64)
            finally:
                cfg_module.CONFIG_FILE = orig
                reset_config()

    def test_auto_creates_config_file(self) -> None:
        """配置文件不存在时 save() 自动创建"""
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = os.path.join(tmp, "auto", "config.ini")
            orig = cfg_module.CONFIG_FILE
            cfg_module.CONFIG_FILE = cfg_path
            reset_config()
            try:
                AppConfig()
                self.assertTrue(os.path.exists(cfg_path))
            finally:
                cfg_module.CONFIG_FILE = orig
                reset_config()


class TestAppConfigFileOverrides(unittest.TestCase):
    """测试配置文件覆盖默认值。"""

    def test_hotplug_true_from_file(self) -> None:
        """从配置文件读取 hotplug=true 正确解析为 True"""
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = os.path.join(tmp, "c.cfg")
            parser = configparser.ConfigParser()
            parser["serial"] = {"port": "", "baudrate": "9600"}
            parser["discovery"] = {
                "hotplug": "true",
                "rs485_timeout": "0.2",
                "rs485_max_addr": "32",
            }
            with open(cfg_path, "w") as f:
                parser.write(f)

            orig = cfg_module.CONFIG_FILE
            cfg_module.CONFIG_FILE = cfg_path
            reset_config()
            try:
                c = AppConfig()
                self.assertTrue(c.hotplug)
                self.assertAlmostEqual(c.rs485_timeout, 0.2)
                self.assertEqual(c.rs485_max_addr, 32)
                self.assertEqual(c.baudrate, 9600)
            finally:
                cfg_module.CONFIG_FILE = orig
                reset_config()


class TestAppConfigCommandInterval(unittest.TestCase):
    """测试 command_interval_ms 与 broadcast_min_interval_ms 配置项。"""

    def _make_cfg(self, ini_extra: str = "") -> AppConfig:
        tmp = tempfile.mkdtemp()
        cfg_path = os.path.join(tmp, "config.ini")
        if ini_extra:
            with open(cfg_path, "w", encoding="utf-8") as f:
                f.write(ini_extra)
        orig = cfg_module.CONFIG_FILE
        cfg_module.CONFIG_FILE = cfg_path
        reset_config()
        try:
            return AppConfig()
        finally:
            cfg_module.CONFIG_FILE = orig
            reset_config()

    def test_command_interval_ms_default(self) -> None:
        """command_interval_ms 默认值为 0"""
        c = self._make_cfg()
        self.assertEqual(c.command_interval_ms, 0)

    def test_broadcast_min_interval_ms_default(self) -> None:
        """broadcast_min_interval_ms 默认值为 10"""
        c = self._make_cfg()
        self.assertEqual(c.broadcast_min_interval_ms, 10)

    def test_broadcast_min_interval_ms_from_ini(self) -> None:
        """broadcast_min_interval_ms 可通过 config.ini 配置"""
        c = self._make_cfg("[broadcast]\nmin_interval_ms = 50\n")
        self.assertEqual(c.broadcast_min_interval_ms, 50)

    def test_set_command_config_saves_interval_ms(self) -> None:
        """set_command_config 含 interval_ms 正确写入并读回"""
        tmp = tempfile.mkdtemp()
        cfg_path = os.path.join(tmp, "config.ini")
        orig = cfg_module.CONFIG_FILE
        cfg_module.CONFIG_FILE = cfg_path
        reset_config()
        try:
            c1 = AppConfig()
            c1.set_command_config(
                addr_mode="逻辑地址", addr="0x0801",
                cmd="25", data="", count=1, interval_ms=300,
            )
            reset_config()
            c2 = AppConfig()
            self.assertEqual(c2.command_interval_ms, 300)
        finally:
            cfg_module.CONFIG_FILE = orig
            reset_config()

    def test_set_command_config_interval_ms_default_zero(self) -> None:
        """set_command_config 不传 interval_ms 时默认写入 0"""
        tmp = tempfile.mkdtemp()
        cfg_path = os.path.join(tmp, "config.ini")
        orig = cfg_module.CONFIG_FILE
        cfg_module.CONFIG_FILE = cfg_path
        reset_config()
        try:
            c1 = AppConfig()
            c1.set_command_config(
                addr_mode="逻辑地址", addr="0x0801",
                cmd="25", data="", count=1,
            )
            reset_config()
            c2 = AppConfig()
            self.assertEqual(c2.command_interval_ms, 0)
        finally:
            cfg_module.CONFIG_FILE = orig
            reset_config()


if __name__ == "__main__":
    unittest.main(verbosity=2)
