# -*- coding: utf-8 -*-
"""
routing/discovery.py — 设备发现与拓扑监控

DiscoveryWorker：PriorityQueue 单消费者，执行 4 阶段发现流程
  A: 递归收集节点信息（FIBRE 同层并行，485 串行轮询）
  B: DFS 单线程分配 logicAddr（节点池 0x801+，终端口池 0x001+）
  C: 逐节点通过 0x36 写入 logicAddr + 注册路由表 + 发布 DeviceFoundEvent
  D: 原子持久化 nodes.json（tmp 写入后 os.replace）

TopologyMonitor：轮询所有中继节点，检测 UUID 变化/无响应，投递 INCREMENTAL 任务
"""
from __future__ import annotations

import hashlib
import itertools
import json
import os
import queue
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

from commands.system import SysCmdHandler
from device.manager import DeviceManager
from device.models import Device, NodeInfo
from events.bus import (
    DeviceFoundEvent,
    DeviceOfflineEvent,
    DiscoveryFailedEvent,
    EventBus,
    NodeDiscoveredEvent,
    PortDiscoveryFailedEvent,
    RouteTableUpdatedEvent,
)
from protocol.models import ModelType, PortPath, PortType
from routing.table import PCLogicRoutingTable, PCUIDRoutingTable

NODES_JSON_VERSION = "20260317"
NODES_JSON_PATH = os.path.join("..", "datas", "nodes.json")
NODES_TMP_PATH = os.path.join("..", "datas", "nodes_tmp.json")


def _terminal_uid(port_path) -> bytes:
    """为无真实 UUID 的终端口（全零 uuid）生成基于 port_path 的合成 uid，保证唯一性。"""
    key = str(list(port_path.ports)).encode()
    return hashlib.sha1(key).digest()[:12]


# ──────────────────────────────────────────────
# DiscoveryTask
# ──────────────────────────────────────────────

@dataclass(order=False)
class DiscoveryTask:
    mode: str              # "FULL" | "INCREMENTAL"
    root_port_path: PortPath
    reason: str
    priority: int = 0      # 0=FULL（高优先级），1=INCREMENTAL

    def __lt__(self, other: 'DiscoveryTask') -> bool:
        return self.priority < other.priority


# ──────────────────────────────────────────────
# 阶段 A 临时节点信息
# ──────────────────────────────────────────────

@dataclass
class _NodeEntry:
    """Phase A 收集阶段临时节点信息。"""
    uuid: bytes
    model: ModelType
    port_types: List[PortType]
    port_path: PortPath
    is_terminal: bool = False
    logical_addr: int = 0           # Phase B 填充
    port_no_in_parent: int = 0      # 父节点的第几个端口（1-based）
    children: List['_NodeEntry'] = field(default_factory=list)


# ──────────────────────────────────────────────
# DiscoveryWorker
# ──────────────────────────────────────────────

class DiscoveryWorker:
    """
    单消费者发现 Worker，PriorityQueue 驱动，执行 4 阶段发现流程。

    外部注入（方便单元测试替换 mock）：
      sys_cmd  : SysCmdHandler（提供 get_node_info）
      port_cmd : PortCmdHandler（提供 set_logical_addr）
      event_bus, logic_table, uid_table, device_manager
    """

    RS485_FULL_RANGE: List[int] = list(range(1, 0x80))  # 默认：1~127
    RS485_EXTRA_PROBE = 8
    RS485_TIMEOUT: float = 0.05   # 485 轮询默认超时 50ms（半双工约束）

    def __init__(
        self,
        sys_cmd: Any,
        port_cmd: Any,
        event_bus: EventBus,
        logic_table: PCLogicRoutingTable,
        uid_table: PCUIDRoutingTable,
        device_manager: DeviceManager,
        rs485_timeout: Optional[float] = None,
        rs485_max_addr: Optional[int] = None,
    ) -> None:
        """
        :param rs485_timeout:  RS485 单地址轮询超时（秒）；None 时使用 RS485_TIMEOUT 类常量
        :param rs485_max_addr: RS485 轮询最大地址（1~127）；None 时使用 RS485_FULL_RANGE 类常量
        """
        self._sys_cmd = sys_cmd
        self._port_cmd = port_cmd
        self._event_bus = event_bus
        self._logic_table = logic_table
        self._uid_table = uid_table
        self._device_mgr = device_manager

        # 由配置注入或使用类默认值
        _timeout = rs485_timeout if rs485_timeout is not None else self.RS485_TIMEOUT
        _max_addr = rs485_max_addr if rs485_max_addr is not None else 0x7F
        self._rs485_poll_range: List[int] = list(range(1, _max_addr + 1))

        # RS485 轮询专用：短超时实例，避免等待无响应地址耗时过长
        # 通过 clone_with_timeout 共享同一 SessionManager，只覆盖 timeout
        self._rs485_sys_cmd = sys_cmd.clone_with_timeout(_timeout)

        self._queue: queue.PriorityQueue = queue.PriorityQueue(maxsize=64)
        self._thread = threading.Thread(
            target=self._run, daemon=True, name='DiscoveryWorker')

        self._lock = threading.Lock()
        self._route_ready: bool = False
        self._discovering: bool = False

        self._node_counter = itertools.count(0x801)
        self._terminal_counter = itertools.count(0x001)

        self._addr_pool_nodes: Set[int] = set()
        self._addr_pool_terminals: Set[int] = set()

        self._rs485_cache: Dict[str, Set[int]] = {}

    # ──────────────────────────────────────────
    # 公共接口
    # ──────────────────────────────────────────

    def start(self) -> None:
        self._thread.start()

    @property
    def route_ready(self) -> bool:
        return self._route_ready

    @property
    def discovering(self) -> bool:
        return self._discovering

    def enqueue_full(self, reason: str = "startup") -> None:
        task = DiscoveryTask(
            mode="FULL", root_port_path=PortPath.local(),
            reason=reason, priority=0)
        self._put_task(task)

    def enqueue_incremental(self, port_path: PortPath,
                            reason: str = "hotplug") -> None:
        task = DiscoveryTask(
            mode="INCREMENTAL", root_port_path=port_path,
            reason=reason, priority=1)
        self._put_task(task)

    def recycle_addr(self, logical_addr: int, is_terminal: bool) -> None:
        if is_terminal:
            self._addr_pool_terminals.add(logical_addr)
        else:
            self._addr_pool_nodes.add(logical_addr)

    # ──────────────────────────────────────────
    # Worker 主循环
    # ──────────────────────────────────────────

    def _put_task(self, task: DiscoveryTask) -> None:
        try:
            self._queue.put_nowait(task)
        except queue.Full:
            pass

    def _run(self) -> None:
        while True:
            task: DiscoveryTask = self._queue.get()
            with self._lock:
                self._discovering = True
            _error: Optional[Exception] = None
            try:
                self._execute(task)
            except Exception as e:
                _error = e
            finally:
                if _error is not None:
                    self._event_bus.publish(
                        DiscoveryFailedEvent(reason=str(_error)))

                if not self._route_ready:
                    self._event_bus.mark_ready()
                    with self._lock:
                        self._route_ready = True
                elif _error is None:
                    self._event_bus.publish(RouteTableUpdatedEvent())

                with self._lock:
                    self._discovering = False

    # ──────────────────────────────────────────
    # 4 阶段
    # ──────────────────────────────────────────

    def _execute(self, task: DiscoveryTask) -> None:
        if task.mode == "FULL":
            self._reset_for_full_scan()
        root_entry = self._phase_a(task.root_port_path)
        if root_entry is None:
            raise RuntimeError("Phase A: root node unreachable")
        self._phase_b(root_entry)
        self._phase_c(root_entry)
        self._phase_d(root_entry)

    def _reset_for_full_scan(self) -> None:
        """FULL 模式重置地址分配状态，保证每次全量扫描都从 0x801 / 0x001 重新分配。"""
        self._node_counter = itertools.count(0x801)
        self._terminal_counter = itertools.count(0x001)
        self._addr_pool_nodes.clear()
        self._addr_pool_terminals.clear()

    # ── Phase A ──────────────────────────────

    def _phase_a(self, port_path: PortPath,
                 sys_cmd: Optional[Any] = None) -> Optional[_NodeEntry]:
        """递归收集节点信息；FIBRE 同层并行，485 串行。
        
        :param sys_cmd: 覆盖用的 SysCmdHandler（RS485 轮询传入短超时实例）
        """
        cmd = sys_cmd if sys_cmd is not None else self._sys_cmd
        try:
            info: NodeInfo = cmd.get_node_info(port_path)
        except Exception:
            self._event_bus.publish(PortDiscoveryFailedEvent(port_path=port_path))
            return None

        entry = _NodeEntry(
            uuid=info.uuid,
            model=info.model,
            port_types=info.port_types,
            port_path=port_path,
        )

        # Phase A 立即发布：UI 可实时展示（logicAddr 待 Phase B 分配）
        self._event_bus.publish(NodeDiscoveredEvent(
            port_path=port_path,
            uuid=info.uuid,
            model=info.model,
            port_types=info.port_types,
        ))

        # FIBRE 子端口：同层并行
        fibre_futures: Dict[Any, int] = {}
        with ThreadPoolExecutor(max_workers=10) as pool:
            for i, pt in enumerate(info.port_types):
                port_no = i + 1
                if pt == PortType.PORT_FIBRE:
                    child_path = port_path.append(port_no)
                    f = pool.submit(self._phase_a, child_path)
                    fibre_futures[f] = port_no

            for f in as_completed(fibre_futures):
                port_no = fibre_futures[f]
                child_path = port_path.append(port_no)
                try:
                    child = f.result()
                    if child is not None:
                        child.port_no_in_parent = port_no
                        entry.children.append(child)
                except Exception:
                    self._event_bus.publish(
                        PortDiscoveryFailedEvent(port_path=child_path))

        # 485 / 232 / CAN 端口：串行处理
        for i, pt in enumerate(info.port_types):
            port_no = i + 1
            child_path = port_path.append(port_no)
            if pt == PortType.PORT_485:
                rs485_children = self._collect_rs485(child_path, port_no)
                entry.children.extend(rs485_children)
                if not rs485_children:
                    entry.children.append(_NodeEntry(
                        uuid=bytes(12), model=ModelType.TERMINAL,
                        port_types=[], port_path=child_path,
                        is_terminal=True, port_no_in_parent=port_no,
                    ))
            elif pt in (PortType.PORT_232, PortType.PORT_CAN):
                entry.children.append(_NodeEntry(
                    uuid=bytes(12), model=ModelType.TERMINAL,
                    port_types=[], port_path=child_path,
                    is_terminal=True, port_no_in_parent=port_no,
                ))

        return entry

    def _collect_rs485(self, parent_path: PortPath,
                       port_no_in_parent: int) -> List[_NodeEntry]:
        """RS485 串行轮询，返回有响应的子节点列表。"""
        cache_key = str(list(parent_path.ports))
        cached: Set[int] = self._rs485_cache.get(cache_key, set())

        if cached:
            poll_addrs = list(cached)
            non_cached = [a for a in self._rs485_poll_range if a not in cached]
            extra = random.sample(
                non_cached, min(self.RS485_EXTRA_PROBE, len(non_cached)))
            poll_addrs = poll_addrs + extra
        else:
            poll_addrs = self._rs485_poll_range[:]

        found: List[_NodeEntry] = []
        new_cached: Set[int] = set()

        for addr in poll_addrs:
            child_path = PortPath(
                ports=list(parent_path.ports) + [addr],
                has485=True)
            child_entry = self._phase_a(child_path, sys_cmd=self._rs485_sys_cmd)
            if child_entry is not None:
                child_entry.port_no_in_parent = port_no_in_parent
                found.append(child_entry)
                new_cached.add(addr)

        if new_cached:
            self._rs485_cache[cache_key] = new_cached

        return found

    # ── Phase B ──────────────────────────────

    def _phase_b(self, root: _NodeEntry) -> None:
        self._assign_addr_dfs(root)

    def _assign_addr_dfs(self, entry: _NodeEntry) -> None:
        if entry.is_terminal:
            entry.logical_addr = self._alloc_addr(is_terminal=True)
        else:
            entry.logical_addr = self._alloc_addr(is_terminal=False)
            for child in entry.children:
                self._assign_addr_dfs(child)

    def _alloc_addr(self, is_terminal: bool) -> int:
        pool = self._addr_pool_terminals if is_terminal else self._addr_pool_nodes
        if pool:
            return pool.pop()
        return (next(self._terminal_counter)
                if is_terminal else next(self._node_counter))

    # ── Phase C ──────────────────────────────

    def _phase_c(self, root: _NodeEntry) -> None:
        self._write_and_publish(root)

    def _write_and_publish(self, entry: _NodeEntry,
                             parent_device: Optional[Any] = None) -> None:
        # 非根节点：通知父设备为该端口设置 logicAddr（0x36）
        if entry.port_no_in_parent > 0 and not entry.port_path.is_local():
            parent_ports = list(entry.port_path.ports)[:-1]
            parent_path = PortPath(ports=parent_ports,
                                   has485=entry.port_path.has485)
            try:
                # self._port_cmd.set_logical_addr(
                #     parent_path, entry.port_no_in_parent, entry.logical_addr)
                # print("Set logical_addr 0x%04X for port %d of parent path %s, terminal=%s",
                    #    entry.logical_addr, entry.port_no_in_parent, parent_path, entry.is_terminal)
                self._port_cmd.set_terminal_port(
                    parent_path, entry.port_no_in_parent, entry.is_terminal)
            except Exception:
                pass

        self._logic_table.add(entry.logical_addr, entry.port_path)
        if entry.uuid != bytes(12):
            self._uid_table.add(entry.uuid, entry.port_path)

        # 推断本设备接入父节点的端口类型（从父节点的 port_types 里查对应端口号）
        conn_type = PortType.NONE
        if (parent_device is not None
                and entry.port_no_in_parent > 0
                and parent_device.port_types):
            idx = entry.port_no_in_parent - 1
            if 0 <= idx < len(parent_device.port_types):
                conn_type = parent_device.port_types[idx]

        # 终端口无真实 UUID（全零），用 port_path 哈希生成合成 uid 保证唯一性
        effective_uid = (
            _terminal_uid(entry.port_path) if entry.uuid == bytes(12) else entry.uuid
        )

        device = Device(
            uid=effective_uid,
            model=entry.model,
            port_type=conn_type,
            port_path=entry.port_path,
            logical_addr=entry.logical_addr,
            is_terminal=entry.is_terminal,
            port_types=entry.port_types,   # 本设备自身端口配置
        )
        # 建立父子链接（add_child 同时设置 device.parent）
        if parent_device is not None:
            parent_device.add_child(device)
        self._device_mgr.add_device(device)
        self._event_bus.publish(DeviceFoundEvent(device=device))

        for child in entry.children:
            self._write_and_publish(child, parent_device=device)

    # ── Phase D ──────────────────────────────

    def _phase_d(self, root: _NodeEntry) -> None:
        os.makedirs("datas", exist_ok=True)
        entries = self._collect_entries(root, [])
        payload = {
            "version": NODES_JSON_VERSION,
            "nodes": [
                {
                    "uid": e.uuid.hex(),
                    "logical_addr": e.logical_addr,
                    "port_path": list(e.port_path.ports),
                    "has485": e.port_path.has485,
                    "is_terminal": e.is_terminal,
                    "model": int(e.model),
                }
                for e in entries
            ],
            "addr_pool_nodes": list(self._addr_pool_nodes),
            "addr_pool_terminals": list(self._addr_pool_terminals),
            "rs485_cache": {k: list(v) for k, v in self._rs485_cache.items()},
        }
        with open(NODES_TMP_PATH, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(NODES_TMP_PATH, NODES_JSON_PATH)

    @staticmethod
    def _collect_entries(entry: _NodeEntry,
                         acc: List[_NodeEntry]) -> List[_NodeEntry]:
        acc.append(entry)
        for child in entry.children:
            DiscoveryWorker._collect_entries(child, acc)
        return acc


# ──────────────────────────────────────────────
# TopologyMonitor
# ──────────────────────────────────────────────

class TopologyMonitor:
    """
    BFS 多级拓扑轮询监视器。

    - 仅在 route_ready=True 后开始轮询
    - 每 poll_interval 秒对所有已知中继节点发一次 0x24
    - UUID 变化 → 投递 INCREMENTAL 任务
    - 无响应（异常）→ DeviceOfflineEvent + 从路由表移除
    """

    def __init__(
        self,
        worker: DiscoveryWorker,
        sys_cmd: Any,
        event_bus: EventBus,
        logic_table: PCLogicRoutingTable,
        uid_table: PCUIDRoutingTable,
        device_manager: DeviceManager,
        poll_interval: float = 3.0,
    ) -> None:
        self._worker = worker
        self._sys_cmd = sys_cmd
        self._event_bus = event_bus
        self._logic_table = logic_table
        self._uid_table = uid_table
        self._device_mgr = device_manager
        self._poll_interval = poll_interval
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name='TopologyMonitor')

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(self._poll_interval):
            if not self._worker.route_ready:
                continue
            if self._worker.discovering:
                continue
            self._poll_once()

    def _poll_once(self) -> None:
        all_devices = [d for d in self._device_mgr.all_devices()
                       if not d.is_terminal]
        for device in all_devices:
            try:
                info: NodeInfo = self._sys_cmd.get_node_info(device.port_path)
                if info.uuid != device.uid:
                    self._worker.enqueue_incremental(
                        device.port_path, reason="hotplug")
            except Exception:
                self._worker.recycle_addr(device.logical_addr, device.is_terminal)
                self._logic_table.remove(device.logical_addr)
                self._uid_table.remove(device.uid)
                self._device_mgr.remove_device(device.uid)
                self._event_bus.publish(DeviceOfflineEvent(uid=device.uid))
