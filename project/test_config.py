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
                addr_mode="逻辑地址", logical_addr="0x0801",
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
                addr_mode="逻辑地址", logical_addr="0x0801",
                cmd="25", data="", count=1,
            )
            reset_config()
            c2 = AppConfig()
            self.assertEqual(c2.command_interval_ms, 0)
        finally:
            cfg_module.CONFIG_FILE = orig
            reset_config()


class TestExternalConfig(unittest.TestCase):
    """from_external：替换式外部配置加载（ADR-0002 / #3）。

    外部路径与 GUI 默认路径完全分离：
      - 只用「内置 _DEFAULTS + 传入文件」
      - 不读、不写 datas/config.ini（哨兵路径断言）
      - 不进 get_config() 单例
      - 外部文件不存在 → 明确 error，不落回 datas
      - 外部实例 save() 是 no-op（绝不落盘）
    """

    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp()
        # 哨兵 datas 路径：放在 tmp 内、外部模式绝不应触碰它
        self._sentinel = os.path.join(self._tmp, "datas", "config.ini")
        self._orig_config_file = cfg_module.CONFIG_FILE
        cfg_module.CONFIG_FILE = self._sentinel
        reset_config()

    def tearDown(self) -> None:
        cfg_module.CONFIG_FILE = self._orig_config_file
        reset_config()

    def _write_external(self, body: str) -> str:
        path = os.path.join(self._tmp, "external.ini")
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)
        return path

    # ── S2 外部模式不碰 datas ─────────────────────────────────────────

    def test_external_does_not_touch_datas(self) -> None:
        """from_external 构造后，哨兵 datas 文件从未被创建/写入。"""
        path = self._write_external("[serial]\nport = /dev/ttyUSB0\n")
        c = AppConfig.from_external(path)
        self.assertEqual(c.serial_port, "/dev/ttyUSB0")
        # 哨兵路径不存在 = 外部模式既没 read 也没 save 它
        self.assertFalse(os.path.exists(self._sentinel))

    def test_external_does_not_read_datas(self) -> None:
        """即使 datas 存在脏值，外部模式也不读它——只用传入文件 + 默认。"""
        # 预置一个"脏" datas（hotplug=true），外部模式绝不能看到它
        os.makedirs(os.path.dirname(self._sentinel), exist_ok=True)
        with open(self._sentinel, "w", encoding="utf-8") as f:
            f.write("[discovery]\nhotplug = true\n")
        path = self._write_external(  # 外部文件不写 hotplug
            "[serial]\nport = /dev/ttyUSB0\n")
        c = AppConfig.from_external(path)
        # hotplug 走内置默认 false，而非 datas 的 true
        self.assertFalse(c.hotplug)

    # ── S3 外部 ini 覆盖 + 新 key 默认值 ───────────────────────────────

    def test_external_new_keys_defaults(self) -> None:
        """新 key 默认值正确（最小外部 ini，不写新 key）。"""
        path = self._write_external("[serial]\nport = /dev/ttyUSB0\n")
        c = AppConfig.from_external(path)
        self.assertEqual(c.headless_discovery_timeout, 10)
        self.assertEqual(c.headless_log_level, "INFO")
        self.assertEqual(c.headless_log_file, "")
        self.assertEqual(c.headless_report_file, "")
        self.assertEqual(c.throughput_duration, 30)
        self.assertEqual(c.throughput_loss_threshold, 0)
        self.assertEqual(c.throughput_corrupt_threshold, 0)

    def test_external_new_keys_overridable(self) -> None:
        """新 key 可被外部 ini 覆盖。"""
        path = self._write_external(
            "[headless]\n"
            "discovery_timeout = 20\n"
            "log_level = DEBUG\n"
            "log_file = /tmp/x.log\n"
            "report_file = /tmp/r.md\n"
            "[throughput]\n"
            "duration = 60\n"
            "loss_threshold = 5\n"
            "corrupt_threshold = 3\n"
        )
        c = AppConfig.from_external(path)
        self.assertEqual(c.headless_discovery_timeout, 20)
        self.assertEqual(c.headless_log_level, "DEBUG")
        self.assertEqual(c.headless_log_file, "/tmp/x.log")
        self.assertEqual(c.headless_report_file, "/tmp/r.md")
        self.assertEqual(c.throughput_duration, 60)
        self.assertEqual(c.throughput_loss_threshold, 5)
        self.assertEqual(c.throughput_corrupt_threshold, 3)

    def test_external_overrides_defaults(self) -> None:
        """外部 ini 覆盖内置默认（既有 key，仿 TestAppConfigFileOverrides）。"""
        path = self._write_external(
            "[serial]\nport = /dev/ttyUSB9\nbaudrate = 9600\n"
            "[discovery]\nhotplug = true\nrs485_max_addr = 32\n"
        )
        c = AppConfig.from_external(path)
        self.assertEqual(c.serial_port, "/dev/ttyUSB9")
        self.assertEqual(c.baudrate, 9600)
        self.assertTrue(c.hotplug)
        self.assertEqual(c.rs485_max_addr, 32)

    # ── S4 外部文件不存在 → 明确 error，不落回 datas ───────────────────

    def test_external_missing_file_raises(self) -> None:
        """外部文件不存在 → 抛明确异常，不静默落回 datas。"""
        missing = os.path.join(self._tmp, "nope.ini")
        with self.assertRaises((FileNotFoundError, ValueError)):
            AppConfig.from_external(missing)
        # 且未因此落回 datas 创建/读它
        self.assertFalse(os.path.exists(self._sentinel))

    # ── S5 外部模式绝不 save() ─────────────────────────────────────────

    def test_external_save_is_noop(self) -> None:
        """外部实例 save() 不写盘（哨兵路径内容不变）。"""
        path = self._write_external("[serial]\nport = /dev/ttyUSB0\n")
        c = AppConfig.from_external(path)
        # 预置哨兵文件，save() 若写盘会改变其内容
        os.makedirs(os.path.dirname(self._sentinel), exist_ok=True)
        with open(self._sentinel, "w", encoding="utf-8") as f:
            f.write("SENTINEL_ORIGINAL")
        c.save()  # 应为 no-op
        with open(self._sentinel, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), "SENTINEL_ORIGINAL")

    # ── 不进单例 ───────────────────────────────────────────────────────

    def test_external_not_registered_as_singleton(self) -> None:
        """外部实例不进 get_config() 单例，二者互不影响。"""
        path = self._write_external("[serial]\nport = /dev/ttyUSB0\n")
        ext = AppConfig.from_external(path)
        # get_config() 走 GUI 默认路径（哨兵），不应返回外部实例
        gui_cfg = cfg_module.get_config()
        self.assertIsNot(gui_cfg, ext)
        # GUI 单例读哨兵（不存在 → 默认空串），外部读传入文件
        self.assertEqual(gui_cfg.serial_port, "")
        self.assertEqual(ext.serial_port, "/dev/ttyUSB0")

    # ── review 加固（TOCTOU / setter 守卫 / log_level 校验）─────────────

    def test_external_missing_file_does_not_silently_fallback(self) -> None:
        """文件不存在 → 抛异常且绝不静默落回默认（configparser.read 对缺失文件是静默的，
        必须用 open 原子读，否则会得到全默认配置而调用方不知情）。"""
        missing = os.path.join(self._tmp, "ghost.ini")
        with self.assertRaises(FileNotFoundError):
            AppConfig.from_external(missing)

    def test_external_setter_raises_readonly(self) -> None:
        """只读实例上调用任意 setter → 抛 ReadOnlyConfigError（fail fast，不静默吞）。"""
        from config import ReadOnlyConfigError
        path = self._write_external("[serial]\nport = /dev/ttyUSB0\n")
        c = AppConfig.from_external(path)
        with self.assertRaises(ReadOnlyConfigError):
            c.set_serial_port("/dev/ttyUSB1")
        with self.assertRaises(ReadOnlyConfigError):
            c.set_firmware_path("/tmp/x.bin")
        with self.assertRaises(ReadOnlyConfigError):
            c.set_throughput_config("测试终端口", 1, "", "", 1000000)
        with self.assertRaises(ReadOnlyConfigError):
            c.set_command_config("逻辑地址")
        # 且内存未被改（set_serial_port 应在改内存前就抛）
        self.assertEqual(c.serial_port, "/dev/ttyUSB0")

    def test_gui_setter_not_blocked(self) -> None:
        """GUI 默认实例（非只读）setter 正常工作——守卫未误伤 GUI 路径。"""
        c = AppConfig()  # GUI 路径，_read_only=False
        c.set_serial_port("/dev/ttyUSB7")
        self.assertEqual(c.serial_port, "/dev/ttyUSB7")

    def test_headless_log_level_invalid_falls_back(self) -> None:
        """无效 log_level 回退 INFO（不把垃圾值传给 logging.basicConfig 崩溃）。"""
        path = self._write_external(
            "[headless]\nlog_level = NONSENSE\n")
        c = AppConfig.from_external(path)
        self.assertEqual(c.headless_log_level, "INFO")

    def test_headless_log_level_valid_passthrough(self) -> None:
        """有效 log_level 正常透传（大小写不敏感）。"""
        for lvl in ("debug", "Warning", "ERROR", "critical"):
            path = self._write_external(
                f"[headless]\nlog_level = {lvl}\n")
            c = AppConfig.from_external(path)
            self.assertEqual(c.headless_log_level, lvl.upper())


if __name__ == "__main__":
    unittest.main(verbosity=2)
