# -*- coding: utf-8 -*-
"""
config.py — 应用配置管理（argparse + configparser）

配置文件路径：../datas/config.ini 格式）
程序首次运行时自动生成默认配置文件。
命令行参数优先级高于配置文件；配置文件优先级高于内置默认值。

配置项：
  [serial]
  port = ""             # 默认串口（空 = 不预选）
  baudrate = 921600     # 默认波特率
  send_interval_ms = 1  # USB 帧间最小间隔（毫秒），范围 [0, 100]

  [discovery]
  hotplug = false       # 是否启用 USB 热插拔检测与自动重连（true/false）
  rs485_timeout = 0.05  # RS485 轮询超时（秒），范围 [0.01, 5.0]
  rs485_max_addr = 127  # RS485 轮询最大地址（1~127 = 0x7F）

命令行参数：
  -c / --config         指定配置文件路径（绝对路径）
  --hotplug             启用热插拔（覆盖配置文件）
  --rs485-timeout SECS  RS485 轮询超时（秒）
  --rs485-max-addr ADDR RS485 轮询最大地址（1~127）
"""
from __future__ import annotations

import argparse
import configparser
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

# 配置文件路径（相对于工作目录 project/）
CONFIG_FILE = os.path.join("..", "datas", "config.ini")

# 内置默认值
_DEFAULTS: dict = {
    "serial": {
        "port": "",
        "baudrate": "1000000",
        "send_interval_ms": "1",
    },
    "discovery": {
        "hotplug": "false",
        "rs485_timeout": "0.05",
        "rs485_max_addr": "127",
    },
    "ui": {
        "firmware_path": "",
    },
    "upgrade": {
        "max_retries": "3",
        "data_send_retries": "3",
        "jump_retries": "3",
    },
    "throughput": {
        "target_mode": "测试终端口",
        "terminal_addr": "0",
        "port_path": "",
        "loopback_port": "",
        "loopback_baudrate": "1000000",
        "mode": "固定包",
        "packet_size": "240",
        "interval_ms": "10",
        "wait_for_ack": "false",
        "stop_on_error": "false",
        "max_count": "0",
    },
    "command": {
        "addr_mode": "逻辑地址",
        "logical_addr": "0x0801",   # 逻辑地址模式下的地址
        "path_addr": "",            # 路由路径模式下的路径（空格分隔）
        "broadcast_addr": "0x00",  # 广播地址（固定，仅供展示）
        "cmd": "25",
        "data": "",
        "count": "1",
        "interval_ms": "0",   # 发送间隔（毫秒），0=不等待
    },
    "broadcast": {
        "min_interval_ms": "10",   # 广播最小间隔（毫秒）
    },
}


class AppConfig:
    """
    应用配置：融合 config.ini 与命令行参数。

    优先级（从高到低）：命令行参数 > config.ini > 内置默认值

    使用示例::
        from config import get_config
        cfg = get_config()
        print(cfg.hotplug)          # False
        print(cfg.rs485_timeout)    # 0.05
        print(cfg.rs485_max_addr)   # 127
    """

    def __init__(self) -> None:
        self._cfg = configparser.ConfigParser()
        self._load_defaults()
        self._load_file()
        self._apply_args()

    # ──────────────────────────────────────────
    # 初始化
    # ──────────────────────────────────────────

    def _load_defaults(self) -> None:
        """写入内置默认值。"""
        for section, items in _DEFAULTS.items():
            self._cfg[section] = dict(items)

    def _load_file(self) -> None:
        """读取配置文件；不存在则自动创建默认文件。"""
        if os.path.exists(CONFIG_FILE):
            self._cfg.read(CONFIG_FILE, encoding="utf-8")
            logger.info("配置已加载: %s", os.path.abspath(CONFIG_FILE))
        else:
            self.save()
            logger.info("配置文件不存在，已创建默认配置: %s", CONFIG_FILE)

    def _apply_args(self) -> None:
        """解析命令行参数并覆盖对应配置项。"""
        parser = argparse.ArgumentParser(
            description="fibre2uart — 光纤通信转发上位机",
            add_help=False,         # 让 Qt 自己处理 --help
        )
        parser.add_argument(
            "-c", "--config", default=None,
            help="指定配置文件路径（绝对路径）",
        )
        parser.add_argument(
            "--hotplug", action="store_true", default=False,
            help="启用 USB 热插拔检测与自动重连",
        )
        parser.add_argument(
            "--rs485-timeout", type=float, default=None, metavar="SECS",
            help="RS485 轮询超时（秒），默认 0.05",
        )
        parser.add_argument(
            "--rs485-max-addr", type=int, default=None, metavar="ADDR",
            help="RS485 轮询最大地址（1~127），默认 127",
        )
        args, _ = parser.parse_known_args()

        # 命令行指定配置文件路径时，重新加载
        if args.config:
            self._cfg.read(args.config, encoding="utf-8")
            logger.info("命令行指定配置文件: %s", args.config)

        # 命令行显式传入时覆盖配置
        if args.hotplug:
            self._cfg["discovery"]["hotplug"] = "true"
        if args.rs485_timeout is not None:
            self._cfg["discovery"]["rs485_timeout"] = str(args.rs485_timeout)
        if args.rs485_max_addr is not None:
            self._cfg["discovery"]["rs485_max_addr"] = str(args.rs485_max_addr)

    # ──────────────────────────────────────────
    # 持久化
    # ──────────────────────────────────────────

    def save(self) -> None:
        """将当前配置写入 config.ini。"""
        cfg_dir = os.path.dirname(CONFIG_FILE)
        if cfg_dir:
            os.makedirs(cfg_dir, exist_ok=True)
        with open(CONFIG_FILE, "w", encoding="utf-8") as fh:
            self._cfg.write(fh)
        logger.debug("配置已保存: %s", CONFIG_FILE)

    # ──────────────────────────────────────────
    # 属性（读取时自动 clamp/转换）
    # ──────────────────────────────────────────

    @property
    def hotplug(self) -> bool:
        """是否启用 USB 热插拔检测与自动重连。"""
        return self._cfg.getboolean("discovery", "hotplug", fallback=False)

    @property
    def rs485_timeout(self) -> float:
        """RS485 轮询超时（秒），值域 [0.01, 5.0]。"""
        val = self._cfg.getfloat("discovery", "rs485_timeout", fallback=0.05)
        return max(0.01, min(5.0, val))

    @property
    def rs485_max_addr(self) -> int:
        """RS485 轮询最大地址，值域 [1, 127]（0x7F）。"""
        val = self._cfg.getint("discovery", "rs485_max_addr", fallback=127)
        return max(1, min(0x7F, val))

    @property
    def serial_port(self) -> str:
        """默认串口名（空串 = 不预选）。"""
        return self._cfg.get("serial", "port", fallback="")

    @property
    def baudrate(self) -> int:
        """默认波特率。"""
        return self._cfg.getint("serial", "baudrate", fallback=1000000)

    @property
    def send_interval_ms(self) -> int:
        """USB 帧间最小间隔（毫秒），值域 [0, 100]。"""
        val = self._cfg.getint("serial", "send_interval_ms", fallback=1)
        return max(0, min(100, val))

    @property
    def firmware_path(self) -> str:
        """上次选择的固件文件路径（空串 = 无）。"""
        return self._cfg.get("ui", "firmware_path", fallback="")

    def set_serial_port(self, port: str) -> None:
        """更新并持久化串口配置。"""
        if "serial" not in self._cfg:
            self._cfg["serial"] = {}
        self._cfg["serial"]["port"] = port
        self.save()

    def set_firmware_path(self, path: str) -> None:
        """更新并持久化固件文件路径。"""
        if "ui" not in self._cfg:
            self._cfg["ui"] = {}
        self._cfg["ui"]["firmware_path"] = path
        self.save()

    @property
    def upgrade_max_retries(self) -> int:
        """升级失败最大重试次数，值域 [0, 10]。"""
        val = self._cfg.getint("upgrade", "max_retries", fallback=3)
        return max(0, min(10, val))

    @property
    def upgrade_data_send_retries(self) -> int:
        """0x14 单包升级数据重发次数，值域 [0, 10]。0=不重发。"""
        val = self._cfg.getint("upgrade", "data_send_retries", fallback=3)
        return max(0, min(10, val))

    @property
    def upgrade_jump_retries(self) -> int:
        """jump指令最大重试次数，值域[1,10]，默认3。"""
        val = self._cfg.getint("upgrade", "jump_retries", fallback=3)
        return max(1, min(10, val))

    # ── throughput 读写 ────────────────────────────────────────────────

    @property
    def throughput_target_mode(self) -> str:
        return self._cfg.get("throughput", "target_mode", fallback="测试终端口")

    @property
    def throughput_terminal_addr(self) -> int:
        return self._cfg.getint("throughput", "terminal_addr", fallback=0)

    @property
    def throughput_port_path(self) -> str:
        return self._cfg.get("throughput", "port_path", fallback="")

    @property
    def throughput_loopback_port(self) -> str:
        return self._cfg.get("throughput", "loopback_port", fallback="")

    @property
    def throughput_loopback_baudrate(self) -> int:
        return self._cfg.getint("throughput", "loopback_baudrate", fallback=1000000)

    @property
    def throughput_mode(self) -> str:
        return self._cfg.get("throughput", "mode", fallback="固定包")

    @property
    def throughput_packet_size(self) -> int:
        return self._cfg.getint("throughput", "packet_size", fallback=240)

    @property
    def throughput_interval_ms(self) -> int:
        return self._cfg.getint("throughput", "interval_ms", fallback=10)

    @property
    def throughput_wait_for_ack(self) -> bool:
        return self._cfg.getboolean("throughput", "wait_for_ack", fallback=False)

    @property
    def throughput_stop_on_error(self) -> bool:
        return self._cfg.getboolean("throughput", "stop_on_error", fallback=False)

    @property
    def throughput_max_count(self) -> int:
        return self._cfg.getint("throughput", "max_count", fallback=0)

    def set_throughput_config(self, target_mode: str, terminal_addr: int,
                              port_path: str, loopback_port: str,
                              loopback_baudrate: int,
                              mode: str = "固定包", packet_size: int = 240,
                              interval_ms: int = 10, wait_for_ack: bool = False,
                              stop_on_error: bool = False,
                              max_count: int = 0) -> None:
        """保存通信测试面板配置（内容相同则跳过写入）。"""
        sec = "throughput"
        if sec not in self._cfg:
            self._cfg[sec] = {}
        new_vals = {
            "target_mode": target_mode,
            "terminal_addr": str(terminal_addr),
            "port_path": port_path,
            "loopback_port": loopback_port,
            "loopback_baudrate": str(loopback_baudrate),
            "mode": mode,
            "packet_size": str(packet_size),
            "interval_ms": str(interval_ms),
            "wait_for_ack": str(wait_for_ack).lower(),
            "stop_on_error": str(stop_on_error).lower(),
            "max_count": str(max_count),
        }
        if all(self._cfg[sec].get(k) == v for k, v in new_vals.items()):
            return   # 内容相同，跳过写入
        self._cfg[sec].update(new_vals)
        self.save()

    # ── command 面板读写 ───────────────────────────────────────────────

    @property
    def command_addr_mode(self) -> str:
        return self._cfg.get("command", "addr_mode", fallback="逻辑地址")

    @property
    def command_logical_addr(self) -> str:
        return self._cfg.get("command", "logical_addr", fallback="0x0801")

    @property
    def command_path_addr(self) -> str:
        return self._cfg.get("command", "path_addr", fallback="")

    @property
    def command_broadcast_addr(self) -> str:
        return self._cfg.get("command", "broadcast_addr", fallback="0x00")

    @property
    def command_cmd(self) -> str:
        return self._cfg.get("command", "cmd", fallback="25")

    @property
    def command_data(self) -> str:
        return self._cfg.get("command", "data", fallback="")

    @property
    def command_count(self) -> int:
        return self._cfg.getint("command", "count", fallback=1)

    @property
    def command_interval_ms(self) -> int:
        return self._cfg.getint("command", "interval_ms", fallback=0)

    @property
    def broadcast_min_interval_ms(self) -> int:
        return self._cfg.getint("broadcast", "min_interval_ms", fallback=10)

    def set_command_config(self, addr_mode: str,
                           logical_addr: str = "",
                           path_addr: str = "",
                           broadcast_addr: str = "0x00",
                           cmd: str = "", data: str = "",
                           count: int = 1,
                           interval_ms: int = 0) -> None:
        """保存单指令面板配置（内容相同则跳过写入）。"""
        sec = "command"
        if sec not in self._cfg:
            self._cfg[sec] = {}
        new_vals = {
            "addr_mode": addr_mode,
            "logical_addr": logical_addr,
            "path_addr": path_addr,
            "broadcast_addr": broadcast_addr,
            "cmd": cmd,
            "data": data,
            "count": str(count),
            "interval_ms": str(interval_ms),
        }
        if all(self._cfg[sec].get(k) == v for k, v in new_vals.items()):
            return   # 内容相同，跳过写入
        self._cfg[sec].update(new_vals)
        self.save()


# ── 模块级单例 ──────────────────────────────────────────────────────────

_instance: Optional[AppConfig] = None


def get_config() -> AppConfig:
    """获取全局 AppConfig 单例（首次调用时初始化）。"""
    global _instance
    if _instance is None:
        _instance = AppConfig()
    return _instance


def reset_config() -> None:
    """重置单例（供测试使用）。"""
    global _instance
    _instance = None
