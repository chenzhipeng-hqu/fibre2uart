# -*- coding: utf-8 -*-
"""
api.py — FibreNetworkClient（统一对外 API 入口）

组装所有内部模块（transport / frame / session / routing / commands / virtual_serial / events），
为上层（UI / 测试脚本）提供简洁接口。

用法示例::

    client = FibreNetworkClient()
    client.connect('/dev/ttyUSB0', 115200)

    # 等待发现完成
    client.event_bus.subscribe(RouteTableUpdatedEvent, on_ready)

    # 按逻辑地址发送指令
    job = client.upgrade_device(0x801, firmware_bytes)

    # 打开虚拟串口
    path = client.open_virtual_serial(0x001)

    client.disconnect()
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, List, Optional, Type

from commands.port import PortCmdHandler
from commands.system import SysCmdHandler
from commands.upgrade import UpgradeCmdHandler, UpgradeJob
from config import get_config
from device.manager import DeviceManager
from device.models import BoardInfo, Device
from events.bus import EventBus, TransportLostEvent, TransportRestoredEvent
from frame.parser import FrameParser
from protocol.codec import CommandCodec
from protocol.models import PortPath, PortType
from routing.discovery import DiscoveryWorker, TopologyMonitor
from routing.table import PCLogicRoutingTable, PCUIDRoutingTable
from session.seq_manager import SeqManager
from session.session_manager import SessionManager
from transport.serial_transport import SerialTransport
from virtual_serial.manager import VirtualSerialManager
from virtual_serial.port import VirtualSerialPort

logger = logging.getLogger(__name__)


class FibreNetworkClient:
    """
    统一对外入口。

    生命周期::
        client = FibreNetworkClient()
        client.connect(port, baudrate)
        ...
        client.disconnect()
    """

    def __init__(self) -> None:
        # ── 事件总线 ──────────────────────────────
        self.event_bus = EventBus()

        # ── 路由表 ────────────────────────────────
        self.logic_table = PCLogicRoutingTable()
        self.uid_table = PCUIDRoutingTable()

        # ── 设备管理 ──────────────────────────────
        self.device_manager = DeviceManager()

        # ── 帧层 ──────────────────────────────────
        self._parser = FrameParser()
        self._parser.on_error = lambda raw, reason: logger.warning(
            "FrameError: %s %s", raw.hex(), reason)

        # ── 会话层 ────────────────────────────────
        self._seq_mgr = SeqManager()
        self._session = SessionManager(
            seq_manager=self._seq_mgr,
            transport_send=None,    # 延迟到 connect() 时绑定
        )

        # ── 传输层 ────────────────────────────────
        self._transport = SerialTransport(self.event_bus)
        self._transport.on_data_received = self._on_raw_data

        # ── 指令层 ────────────────────────────────
        self.sys_cmd = SysCmdHandler(self._session)
        self.port_cmd = PortCmdHandler(self._session)
        self.upgrade_cmd = UpgradeCmdHandler(self._session)

        # ── 路由发现 ──────────────────────────────
        _cfg = get_config()
        self._discovery = DiscoveryWorker(
            sys_cmd=self.sys_cmd,
            port_cmd=self.port_cmd,
            event_bus=self.event_bus,
            logic_table=self.logic_table,
            uid_table=self.uid_table,
            device_manager=self.device_manager,
            rs485_timeout=_cfg.rs485_timeout,
            rs485_max_addr=_cfg.rs485_max_addr,
        )
        self._monitor = TopologyMonitor(
            worker=self._discovery,
            sys_cmd=self.sys_cmd,
            event_bus=self.event_bus,
            logic_table=self.logic_table,
            uid_table=self.uid_table,
            device_manager=self.device_manager,
        )

        # ── 虚拟串口 ──────────────────────────────
        self._vserial_mgr = VirtualSerialManager(
            logic_table=self.logic_table,
            transport_send=self._transport.send,
            encode_fn=CommandCodec.encode,
            seq_allocate=self._seq_mgr.allocate,
        )

        # 接收消息帧（cmd<0x10）→ 分发到虚拟串口
        self._parser.on_frame = self._on_frame

        # ── 连接重连状态 ──────────────────────────
        self._port: Optional[str] = None
        self._baudrate: int = 115200
        self._hotplug: bool = False          # 热插拔/自动重连开关，connect() 传入
        self._reconnect_thread: Optional[threading.Thread] = None
        self._reconnect_stop = threading.Event()

        # 订阅传输层断连事件
        self.event_bus.subscribe(TransportLostEvent, self._on_transport_lost)

    # ──────────────────────────────────────────
    # 连接管理
    # ──────────────────────────────────────────

    def connect(self, port: str, baudrate: int = 115200,
                _serial_factory=None, hotplug: bool = False) -> None:
        """
        打开物理串口并启动所有后台线程：
          1. SerialTransport.open()
          2. 绑定 session transport_send
          3. 启动 DiscoveryWorker 线程，投递首次 FULL 任务
          4. 启动 TopologyMonitor 线程

        :param hotplug: True=启用 USB 热插拔检测与自动重连；False=关闭（默认）
        """
        self._port = port
        self._baudrate = baudrate
        self._hotplug = hotplug
        self._send_interval_ms = get_config().send_interval_ms

        # 绑定 transport send 到 session
        self._session._transport_send = self._transport.send

        # 启动物理串口
        self._transport.open(port, baudrate,
                             send_interval_ms=self._send_interval_ms,
                             _serial_factory=_serial_factory)

        # 绑定 FrameParser 的帧完成回调（已在 __init__ 设置 on_frame）
        # 帧层 → 会话层
        self._parser.on_frame = self._on_frame

        # 启动发现 worker + 投递首次 FULL
        self._discovery.start()
        self._discovery.enqueue_full(reason="startup")

        # 启动拓扑监控（仅 hotplug=True 时开启）
        if self._hotplug:
            self._monitor.start()
        else:
            logger.info("TopologyMonitor disabled (hotplug=False)")

        logger.info("FibreNetworkClient connected to %s @ %d", port, baudrate)

    def disconnect(self) -> None:
        """关闭连接，清理所有资源。主动断开不触发重连。"""
        self._reconnect_stop.set()
        self._transport.close(emit_event=False)  # 主动关闭，不发布 TransportLostEvent
        self._session.on_transport_lost()         # 直接批量 reject 挂起 Future
        self._vserial_mgr.close_all()
        self._session.close()
        logger.info("FibreNetworkClient disconnected")

    # ──────────────────────────────────────────
    # 设备发现
    # ──────────────────────────────────────────

    def rescan(self) -> None:
        """触发全网重新发现（ScanButton 点击时调用）。"""
        self.logic_table.clear()
        self.uid_table.clear()
        self.device_manager.clear()
        self._discovery.enqueue_full(reason="scan")

    def get_device(self, uid: bytes) -> Optional[Device]:
        return self.device_manager.find_by_uid(uid)

    def get_device_by_addr(self, logical_addr: int) -> Optional[Device]:
        return self.device_manager.find_by_logical_addr(logical_addr)

    # ──────────────────────────────────────────
    # 板卡信息 / 系统指令
    # ──────────────────────────────────────────

    def get_board_info(self, logical_addr: int) -> BoardInfo:
        """通过逻辑地址获取板卡信息（0x12）。"""
        port_path = self._resolve(logical_addr)
        return self.upgrade_cmd.get_board_info(port_path)

    def reset_device(self, logical_addr: int) -> None:
        """系统复位（0x21）。"""
        port_path = self._resolve(logical_addr)
        self.sys_cmd.reset(port_path)

    def set_baudrate(self, logical_addr: int, baudrate: int) -> None:
        """配置终端口波特率（0x33）。

        终端口本身不接受指令，需将 0x33 发给父中继节点。
        流程：推导父路径 + port_no → 先读当前 parity/stopbit → 发送新波特率。
        """
        term_path = self._resolve(logical_addr)
        ports = list(term_path.ports)
        if not ports:
            raise ValueError("set_baudrate: terminal port_path is empty")
        port_no = ports[-1]
        parent_path = PortPath(ports=ports[:-1], has485=term_path.has485)
        # 先读当前 parity / stopbit
        try:
            _, parity, stopbit = self.port_cmd.get_port_config(parent_path, port_no)
        except Exception:
            parity, stopbit = 0, 1   # 读取失败时使用默认小端序 8N1
        self.port_cmd.port_config(parent_path, port_no, baudrate, parity, stopbit)

    # ──────────────────────────────────────────
    # 消息透传
    # ──────────────────────────────────────────

    def send_msg(self, logical_addr: int, msg_type: int,
                 data: bytes) -> 'Future':
        """按逻辑地址向终端口发送透传消息，返回 Future[CommandFrame]。"""
        port_path = self._resolve(logical_addr)
        return self._session.send_request(port_path, msg_type & 0x0F, data)

    def send_cmd(self, logical_addr: int, cmd: int,
                 data: bytes) -> 'Future':
        """按逻辑地址发送任意指令（cmd 不做掩码），返回 Future[CommandFrame]。

        供单指令 UI 等调试面板使用；cmd 原值写入帧，不限制为消息类型。
        """
        port_path = self._resolve(logical_addr)
        return self._session.send_request(port_path, cmd, data)

    def send_broadcast(self, cmd: int, data: bytes) -> None:
        """广播发送指令（fire-and-forget，不等待响应）。

        使用 PortPath.broadcast() ([0x00]) 构建帧，直接调底层 transport 发送，
        不注册 Future，不会触发超时。供单指令 UI 广播模式使用。
        """
        port_path = PortPath.broadcast()
        seq = self._seq_mgr.allocate()
        raw = CommandCodec.encode(seq, cmd, port_path, data)
        self._transport.send(raw)

    # ──────────────────────────────────────────
    # 在线升级
    # ──────────────────────────────────────────

    def upgrade_device(
        self,
        logical_addr: int,
        firmware: bytes,
        on_progress: Optional[Callable] = None,
        on_error: Optional[Callable] = None,
        on_done: Optional[Callable] = None,
        log_level: int = 0,
    ) -> UpgradeJob:
        """按逻辑地址对单节点升级，返回 UpgradeJob（已 start）。"""
        port_path = self._resolve(logical_addr)
        job = UpgradeJob(
            handler=self.upgrade_cmd,
            port_path=port_path,
            firmware=firmware,
            on_progress=on_progress,
            on_error=on_error,
            on_done=on_done,
            log_level=log_level,
        )
        job.start()
        return job

    def broadcast_upgrade(
        self,
        firmware: bytes,
        on_progress: Optional[Callable] = None,
        on_error: Optional[Callable] = None,
        on_done: Optional[Callable] = None,
        log_level: int = 0,
    ) -> UpgradeJob:
        """广播升级所有已发现节点。

        升级策略（最远节点优先）：
          1. JUMP     - 按路径深度降序逐节点单播，等待 ACK 确认进入 bootloader
          2. SEND_INFO - 按路径深度降序逐节点单播，等待 ACK 确认擦除完成
          3. SEND_DATA - 广播发送（所有节点同时接收，节省时间）
          4. VERIFY   - 按路径深度降序逐节点单播，验证已跳回 APP
        """
        # 快照路由表，过滤终端口（逻辑地址 0x001~0x7FF），按路径深度降序（最远优先）
        entries = self.logic_table.all_entries()
        node_paths = [pp for addr, pp in entries if addr >= 0x800]
        node_paths.sort(key=lambda p: len(p.ports), reverse=True)
        if not node_paths:
            logger.warning("broadcast_upgrade: 路由表中无可升级节点（逻辑地址 ≥ 0x800）")
        logger.info("broadcast_upgrade: 共 %d 个节点，路径深度: %s",
                    len(node_paths),
                    [len(p.ports) for p in node_paths])
        job = UpgradeJob(
            handler=self.upgrade_cmd,
            port_path=PortPath.broadcast(),
            firmware=firmware,
            on_progress=on_progress,
            on_error=on_error,
            on_done=on_done,
            log_level=log_level,
            unicast_paths=node_paths,
        )
        job.start()
        return job

    # ──────────────────────────────────────────
    # 虚拟串口
    # ──────────────────────────────────────────

    def open_virtual_serial(self, logical_addr: int,
                            baud_rate: int = 115200) -> str:
        """为终端口创建虚拟串口，返回 device_path。"""
        device = self.device_manager.find_by_logical_addr(logical_addr)
        port_type = device.port_type if device else PortType.PORT_485
        return self._vserial_mgr.create(logical_addr, port_type, baud_rate)

    def close_virtual_serial(self, logical_addr: int) -> None:
        self._vserial_mgr.close(logical_addr)

    def get_virtual_serial(self, logical_addr: int) -> Optional[VirtualSerialPort]:
        """获取已开启的虚拟串口对象，用于读取 tx_bytes/rx_bytes/overflow_count 统计。"""
        return self._vserial_mgr.get(logical_addr)

    def list_virtual_serials(self) -> List[VirtualSerialPort]:
        return self._vserial_mgr.list_all()

    # ──────────────────────────────────────────
    # 事件订阅
    # ──────────────────────────────────────────

    def on(self, event_type: Type, callback: Callable):
        """订阅事件，返回 token（lambda/function 需保持 token 存活）。"""
        return self.event_bus.subscribe(event_type, callback)

    # ──────────────────────────────────────────
    # 内部实现
    # ──────────────────────────────────────────

    def _resolve(self, logical_addr: int) -> PortPath:
        """按逻辑地址查路由表，找不到则 raise。"""
        pp = self.logic_table.lookup(logical_addr)
        if pp is None:
            raise KeyError(f"逻辑地址 0x{logical_addr:04X} 不在路由表中")
        return pp

    def _on_raw_data(self, data: bytes) -> None:
        """SerialTransport.on_data_received → FrameParser.feed（消费 generator 触发 on_frame）。"""
        # feed() 是 generator，必须消费才会执行；on_frame 回调在 feed 内部触发
        for _ in self._parser.feed(data):
            pass

    def _on_frame(self, frame) -> None:
        """FrameParser 解析完一帧 → 路由到会话层或虚拟串口层。"""
        if frame.cmd >= 0x10:
            # 指令应答 → SessionManager 配对 Future
            self._session.on_frame_received(frame)
        else:
            # 消息透传（cmd < 0x10）→ 虚拟串口
            self._vserial_mgr.dispatch(frame)

    def _on_transport_lost(self, event: TransportLostEvent) -> None:
        """USB 断连后：通知 SessionManager 批量 reject；hotplug 开启时才启动重连循环。"""
        self._session.on_transport_lost()
        if self._hotplug and self._discovery.route_ready and self._port is not None:
            self._start_reconnect()
            logger.info("disconnected from %s", self._port)
        else:
            logger.warning("USB disconnected (hotplug disabled, no auto-reconnect)")

    def _start_reconnect(self) -> None:
        """启动重连后台线程（每 1s 尝试一次）。"""
        if (self._reconnect_thread is not None
                and self._reconnect_thread.is_alive()):
            return
        self._reconnect_stop.clear()
        self._reconnect_thread = threading.Thread(
            target=self._reconnect_loop,
            daemon=True, name='Reconnect',
        )
        self._reconnect_thread.start()

    def _reconnect_loop(self) -> None:
        logger.info("Reconnect loop started for %s", self._port)
        while not self._reconnect_stop.wait(1.0):
            try:
                self._transport.open(self._port, self._baudrate,
                                     send_interval_ms=self._send_interval_ms)
                self._session._transport_send = self._transport.send
                self.event_bus.publish(TransportRestoredEvent())
                self._discovery.enqueue_full(reason="reconnect")
                logger.info("Reconnected to %s", self._port)
                return
            except Exception as e:
                logger.debug("Reconnect failed: %s", e)
