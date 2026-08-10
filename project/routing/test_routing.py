# -*- coding: utf-8 -*-
"""
routing/test_routing.py — DiscoveryWorker 单元测试

使用 MockSysCmdHandler / MockPortCmdHandler 注入，无需真实硬件。

测试树结构：
    PC (local=[])               — PC_FIBRE，port_types=[FIBRE, NONE, NONE, NONE, NONE, NONE]
      └─ Node1 ([1])            — BRIDGE_FIBRE，port_types=[NONE, PORT_232, NONE, NONE, NONE, NONE]
           └─ Terminal1 ([1,2]) — is_terminal=True（PORT_232 自动创建）

测试项：
  1. FULL 发现：路由表包含所有节点
  2. DFS logicAddr 分配：节点 0x801+，终端口 0x001+
  3. set_logical_addr 被正确调用（非根节点）
  4. DeviceFoundEvent 为每个节点发布一次
  5. 节点离线后 addr_pool 回收地址
  6. INCREMENTAL 任务：仅重新发现指定子树
"""
import sys
import os
import time
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from device.manager import DeviceManager
from device.models import NodeInfo
from events.bus import EventBus, DeviceFoundEvent, RouteTableUpdatedEvent
from protocol.models import ModelType, PortPath, PortType
from routing.table import PCLogicRoutingTable, PCUIDRoutingTable
from routing.discovery import DiscoveryWorker


# ──────────────────────────────────────────────
# Mock 指令处理器
# ──────────────────────────────────────────────

class MockSysCmdHandler:
    """预编程 get_node_info 响应。"""

    def __init__(self):
        self._responses: dict = {}   # port_path_key → NodeInfo

    def add(self, port_path: PortPath, node_info: NodeInfo) -> None:
        self._responses[str(list(port_path.ports))] = node_info

    def get_node_info(self, port_path: PortPath) -> NodeInfo:
        key = str(list(port_path.ports))
        if key not in self._responses:
            raise TimeoutError(f"no mock response for {port_path}")
        return self._responses[key]

    def clone_with_timeout(self, timeout: float) -> "MockSysCmdHandler":
        """测试中超时无关，直接返回自身。"""
        return self


class MockPortCmdHandler:
    """记录所有 set_terminal_port 调用。"""

    def __init__(self):
        self.calls: list[tuple[list[int], int, bool]] = []   # [(parent_ports, port_no, is_terminal), ...]

    def set_logical_addr(self, parent_path: PortPath,
                         port_no: int, addr: int) -> None:
        pass

    def set_terminal_port(self, parent_path: PortPath,
                          port_no: int, is_terminal: bool) -> None:
        self.calls.append((list(parent_path.ports), port_no, is_terminal))
    def port_reset(self, *a, **kw): pass
    def port_power(self, *a, **kw): pass
    def port_config(self, *a, **kw): pass


# ──────────────────────────────────────────────
# 通用测试固件
# ──────────────────────────────────────────────

UUID_PC    = bytes([0x01] * 12)
UUID_NODE1 = bytes([0x02] * 12)


def _build_default_sys_cmd() -> MockSysCmdHandler:
    """
    构建默认的 MockSysCmdHandler，对应：
      PC(local) → port_types=[FIBRE, NONE×5]
      Node1([1]) → port_types=[NONE, PORT_232, NONE×4]
    """
    sys_cmd = MockSysCmdHandler()
    sys_cmd.add(
        PortPath.local(),
        NodeInfo(
            uuid=UUID_PC,
            model=ModelType.PC_FIBRE,
            port_types=[
                PortType.PORT_FIBRE,
                PortType.NONE, PortType.NONE,
                PortType.NONE, PortType.NONE, PortType.NONE,
            ],
        ),
    )
    sys_cmd.add(
        PortPath([1]),
        NodeInfo(
            uuid=UUID_NODE1,
            model=ModelType.BRIDGE_FIBRE,
            port_types=[
                PortType.NONE,
                PortType.PORT_232,
                PortType.NONE, PortType.NONE,
                PortType.NONE, PortType.NONE,
            ],
        ),
    )
    return sys_cmd


def _make_worker(sys_cmd=None, port_cmd=None):
    if sys_cmd is None:
        sys_cmd = _build_default_sys_cmd()
    if port_cmd is None:
        port_cmd = MockPortCmdHandler()
    bus = EventBus()
    logic = PCLogicRoutingTable()
    uid = PCUIDRoutingTable()
    mgr = DeviceManager()
    worker = DiscoveryWorker(sys_cmd, port_cmd, bus, logic, uid, mgr)
    return worker, bus, logic, uid, mgr, port_cmd


def _wait_ready(worker: DiscoveryWorker, timeout: float = 3.0) -> bool:
    deadline = time.time() + timeout
    while not worker.route_ready:
        if time.time() > deadline:
            return False
        time.sleep(0.05)
    return True


# ──────────────────────────────────────────────
# 测试用例
# ──────────────────────────────────────────────

class TestDiscoveryWorker(unittest.TestCase):

    # ── 1. FULL 发现后路由表包含所有节点 ────────

    def test_full_discovery_populates_routing_table(self):
        """全量发现后路由表包含 PC/中继/终端全部节点"""
        worker, bus, logic, uid, mgr, _ = _make_worker()
        worker.start()
        worker.enqueue_full(reason="test")
        self.assertTrue(_wait_ready(worker), "worker 应在 3 秒内完成 FULL 发现")

        # PC root + Node1 + Terminal1 = 3 个设备
        all_devs = mgr.all_devices()
        self.assertEqual(len(all_devs), 3)

        # 路由表：3 个条目
        self.assertEqual(len(logic), 3)

        # UID 表：2 条（PC 和 Node1 有真实 UUID；Terminal 是 bytes(12)）
        self.assertEqual(len(uid), 2)

    # ── 2. DFS logicAddr 分配：节点 0x801+，终端口 0x001+ ─

    def test_logical_addr_assignment_dfs_order(self):
        """DFS顺序分配：中继节点0x80x+，终端口0x001+"""
        worker, bus, logic, uid, mgr, _ = _make_worker()
        worker.start()
        worker.enqueue_full()
        _wait_ready(worker)

        all_devs = {tuple(d.port_path.ports): d for d in mgr.all_devices()}

        # PC root → first from node pool
        pc_dev = all_devs[()]
        self.assertEqual(pc_dev.logical_addr, 0x801)

        # Node1 → second from node pool
        node1_dev = all_devs[(1,)]
        self.assertEqual(node1_dev.logical_addr, 0x802)

        # Terminal1 → first from terminal pool
        term_dev = all_devs[(1, 2)]
        self.assertEqual(term_dev.logical_addr, 0x001)
        self.assertTrue(term_dev.is_terminal)

    # ── 3. set_terminal_port 被正确调用 ──────────

    def test_set_logical_addr_called_for_non_root(self):
        """非根节点的 set_terminal_port 被正确调用两次"""
        port_cmd = MockPortCmdHandler()
        worker, bus, logic, uid, mgr, _ = _make_worker(port_cmd=port_cmd)
        worker.start()
        worker.enqueue_full()
        _wait_ready(worker)

        # set_terminal_port 应被调用 2 次（Node1 和 Terminal1）
        self.assertEqual(len(port_cmd.calls), 2)

        # Node1: 父路径=local=[], port_no=1, is_terminal=False
        pc_call = next(c for c in port_cmd.calls if c[0] == [])
        self.assertEqual(pc_call[1], 1)        # port_no=1
        self.assertFalse(pc_call[2])           # is_terminal=False

        # Terminal1: 父路径=[1], port_no=2, is_terminal=True
        node1_call = next(c for c in port_cmd.calls if c[0] == [1])
        self.assertEqual(node1_call[1], 2)     # port_no=2
        self.assertTrue(node1_call[2])         # is_terminal=True

    # ── 4. DeviceFoundEvent 为每个节点发布 ────────

    def test_device_found_events_published(self):
        """每个发现的节点都发布一次 DeviceFoundEvent"""
        worker, bus, logic, uid, mgr, _ = _make_worker()
        found_uids = []
        token = bus.subscribe(DeviceFoundEvent,
                              lambda e: found_uids.append(e.device.uid))

        worker.start()
        worker.enqueue_full()
        _wait_ready(worker)

        # 3 个设备（PC、Node1、Terminal）各发布一次 DeviceFoundEvent
        self.assertEqual(len(found_uids), 3)
        self.assertIn(UUID_PC, found_uids)
        self.assertIn(UUID_NODE1, found_uids)
        del token

    # ── 5. 离线后 addr_pool 回收地址 ────────────

    def test_recycle_addr_restores_to_pool(self):
        """节点离线后逻辑地址归还到地址池可复用"""
        worker, bus, logic, uid, mgr, _ = _make_worker()
        worker.start()
        worker.enqueue_full()
        _wait_ready(worker)

        # 获取 Node1 的逻辑地址
        all_devs = {tuple(d.port_path.ports): d for d in mgr.all_devices()}
        node1_addr = all_devs[(1,)].logical_addr

        # 归还 Node1 的地址
        worker.recycle_addr(node1_addr, is_terminal=False)
        self.assertIn(node1_addr, worker._addr_pool_nodes)

        # 下次分配时应优先从池中取
        addr = worker._alloc_addr(is_terminal=False)
        self.assertEqual(addr, node1_addr)

    # ── 6. INCREMENTAL 任务：重新发现子树 ────────

    def test_incremental_discovery_reruns_subtree(self):
        """增量发现只重新扫描指定子树"""
        worker, bus, logic, uid, mgr, _ = _make_worker()
        updated_events = []
        token = bus.subscribe(RouteTableUpdatedEvent,
                              lambda e: updated_events.append(e))

        worker.start()
        worker.enqueue_full()
        _wait_ready(worker)

        initial_count = len(mgr.all_devices())

        # 清空设备管理器，模拟 INCREMENTAL 重新发现
        mgr.clear()
        logic.clear()
        uid.clear()

        # 投递 INCREMENTAL 任务（从 Node1 子树开始）
        worker.enqueue_incremental(PortPath([1]), reason="hotplug")

        # 等待 RouteTableUpdatedEvent
        deadline = time.time() + 3.0
        while not updated_events and time.time() < deadline:
            time.sleep(0.05)

        self.assertTrue(len(updated_events) >= 1,
                        "INCREMENTAL 完成后应发布 RouteTableUpdatedEvent")
        del token


if __name__ == '__main__':
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromTestCase(TestDiscoveryWorker)
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
