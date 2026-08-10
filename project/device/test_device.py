# -*- coding: utf-8 -*-
"""
device/test_device.py — Device / DeviceManager / 路由表 单元测试

测试项：
  is_terminal_port:
    1.  PORT_232 / PORT_CAN 始终是终端口
    2.  PORT_485 无下级 → 终端；有下级 → 中继
    3.  PORT_FIBRE / PORT_PC 始终不是终端口
  Device:
    4.  add_child 设置 parent 引用
    5.  depth() 层级计算
    6.  remove_child 移除正确，不存在返回 None
    7.  repr 包含 uid / logical_addr
  DeviceManager:
    8.  add_device / find_by_uid / find_by_logical_addr 双索引
    9.  logical_addr=0 不建立地址索引
    10. remove_device 递归移除子树（uid + addr 全部清除）
    11. update_logical_addr 更新地址索引
    12. clear 全部清空，root 置 None
    13. terminal_devices 只返回终端口
    14. all_devices 快照列表
    15. get_root / set_root
    16. __len__
  PCLogicRoutingTable:
    17. add / lookup / remove / clear / __contains__ / __len__
    18. lookup 不存在返回 None
  PCUIDRoutingTable:
    19. add / lookup / remove / __len__
  并发:
    20. DeviceManager 并发写入无竞争
    21. PCLogicRoutingTable 并发写入无竞争
"""
from __future__ import annotations

import sys
import os
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from protocol.models import PortPath, PortType, ModelType
from device.models import Device, BoardInfo, NodeInfo, is_terminal_port
from device.manager import DeviceManager
from routing.table import PCLogicRoutingTable, PCUIDRoutingTable


def _dev(uid_byte: int, model: ModelType = ModelType.BRIDGE_FIBRE,
         port_type: PortType = PortType.PORT_FIBRE,
         ports: list[int] | None = None,
         logical_addr: int = 0,
         is_terminal: bool = False) -> Device:
    return Device(
        uid=bytes([uid_byte] * 12),
        model=model,
        port_type=port_type,
        port_path=PortPath(ports=ports or []),
        logical_addr=logical_addr,
        is_terminal=is_terminal,
    )


class TestIsTerminalPort(unittest.TestCase):

    # 1
    def test_232_always_terminal(self):
        """PORT_232 无论有无下级均为终端口"""
        self.assertTrue(is_terminal_port(PortType.PORT_232, False))
        self.assertTrue(is_terminal_port(PortType.PORT_232, True))

    def test_can_always_terminal(self):
        """PORT_CAN 无论有无下级均为终端口"""
        self.assertTrue(is_terminal_port(PortType.PORT_CAN, False))
        self.assertTrue(is_terminal_port(PortType.PORT_CAN, True))

    # 2
    def test_485_terminal_when_no_children(self):
        """PORT_485 无下级节点时判定为终端口"""
        self.assertTrue(is_terminal_port(PortType.PORT_485, False))

    def test_485_not_terminal_when_has_children(self):
        """PORT_485 有下级节点时判定为中继节点"""
        self.assertFalse(is_terminal_port(PortType.PORT_485, True))

    # 3
    def test_fibre_never_terminal(self):
        """PORT_FIBRE 始终不是终端口"""
        self.assertFalse(is_terminal_port(PortType.PORT_FIBRE, False))
        self.assertFalse(is_terminal_port(PortType.PORT_FIBRE, True))

    def test_pc_never_terminal(self):
        """PORT_PC 始终不是终端口"""
        self.assertFalse(is_terminal_port(PortType.PORT_PC, False))


class TestDevice(unittest.TestCase):

    # 4
    def test_add_child_sets_parent(self):
        """add_child 建立父子关系并设置 parent 引用"""
        root = _dev(1, logical_addr=0x801)
        child = _dev(2, logical_addr=0x802)
        root.add_child(child)
        self.assertIs(child.parent, root)
        self.assertIn(child, root.children)

    # 5
    def test_depth(self):
        """depth() 返回节点在树中的层级（根=0）"""
        root = _dev(1)
        child = _dev(2)
        grandchild = _dev(3)
        root.add_child(child)
        child.add_child(grandchild)
        self.assertEqual(root.depth(), 0)
        self.assertEqual(child.depth(), 1)
        self.assertEqual(grandchild.depth(), 2)

    # 6
    def test_remove_child_found(self):
        """remove_child 成功移除已存在的子节点"""
        root = _dev(1)
        child = _dev(2)
        root.add_child(child)
        removed = root.remove_child(child.uid)
        self.assertIs(removed, child)
        self.assertEqual(len(root.children), 0)

    def test_remove_child_not_found(self):
        """remove_child 对不存在的uid返回 None 不崩溃"""
        root = _dev(1)
        self.assertIsNone(root.remove_child(bytes(12)))

    # 7
    def test_repr_contains_uid_and_addr(self):
        """repr 包含 uid 和 logical_addr 信息"""
        d = _dev(0xAB, logical_addr=0x001)
        r = repr(d)
        self.assertIn('0001', r)   # logical_addr in hex


class TestDeviceManager(unittest.TestCase):

    # 8
    def test_add_and_find(self):
        """add_device 后可通过 uid 和 logical_addr 双索引查到"""
        mgr = DeviceManager()
        d = _dev(1, logical_addr=0x801)
        mgr.add_device(d)
        self.assertIs(mgr.find_by_uid(d.uid), d)
        self.assertIs(mgr.find_by_logical_addr(0x801), d)

    def test_find_missing_returns_none(self):
        """查找不存在的设备返回 None"""
        mgr = DeviceManager()
        self.assertIsNone(mgr.find_by_uid(bytes(12)))
        self.assertIsNone(mgr.find_by_logical_addr(0x999))

    # 9
    def test_zero_addr_not_indexed(self):
        """logical_addr=0 的设备不建立地址索引"""
        mgr = DeviceManager()
        d = _dev(1, logical_addr=0)
        mgr.add_device(d)
        self.assertIsNone(mgr.find_by_logical_addr(0))
        self.assertIs(mgr.find_by_uid(d.uid), d)

    # 10
    def test_remove_subtree(self):
        """remove_device 递归移除子树，双索引同步清理"""
        mgr = DeviceManager()
        root = _dev(1, logical_addr=0x801)
        child = _dev(2, logical_addr=0x802)
        terminal = _dev(3, logical_addr=0x001, is_terminal=True)
        root.add_child(child)
        child.add_child(terminal)
        for d in (root, child, terminal):
            mgr.add_device(d)
        self.assertEqual(len(mgr), 3)

        mgr.remove_device(child.uid)
        self.assertEqual(len(mgr), 1)
        self.assertIsNone(mgr.find_by_uid(child.uid))
        self.assertIsNone(mgr.find_by_uid(terminal.uid))
        self.assertIsNone(mgr.find_by_logical_addr(0x802))
        self.assertIsNone(mgr.find_by_logical_addr(0x001))

    # 11
    def test_update_logical_addr(self):
        """update_logical_addr 更新地址索引，旧地址失效"""
        mgr = DeviceManager()
        d = _dev(1, logical_addr=0x100)
        mgr.add_device(d)
        mgr.update_logical_addr(d.uid, 0x200)
        self.assertIsNone(mgr.find_by_logical_addr(0x100))
        self.assertIs(mgr.find_by_logical_addr(0x200), d)

    # 12
    def test_clear(self):
        """clear 清空所有设备，root 置 None"""
        mgr = DeviceManager()
        for i in range(3):
            mgr.add_device(_dev(i + 1, logical_addr=0x801 + i))
        mgr.clear()
        self.assertEqual(len(mgr), 0)
        self.assertIsNone(mgr.get_root())

    # 13
    def test_terminal_devices(self):
        """terminal_devices 只返回 is_terminal=True 的设备"""
        mgr = DeviceManager()
        mgr.add_device(_dev(1, logical_addr=0x801, is_terminal=False))
        mgr.add_device(_dev(2, logical_addr=0x001, is_terminal=True))
        mgr.add_device(_dev(3, logical_addr=0x002, is_terminal=True))
        terminals = mgr.terminal_devices()
        self.assertEqual(len(terminals), 2)
        self.assertTrue(all(d.is_terminal for d in terminals))

    # 14
    def test_all_devices_snapshot(self):
        """all_devices 返回当前注册设备的快照列表"""
        mgr = DeviceManager()
        d1 = _dev(1, logical_addr=0x801)
        d2 = _dev(2, logical_addr=0x001)
        mgr.add_device(d1)
        mgr.add_device(d2)
        snap = mgr.all_devices()
        self.assertEqual(len(snap), 2)
        self.assertIn(d1, snap)

    # 15
    def test_get_and_set_root(self):
        """get_root/set_root 正确读写根节点"""
        mgr = DeviceManager()
        self.assertIsNone(mgr.get_root())
        d = _dev(1, logical_addr=0x801)
        mgr.add_device(d)
        self.assertIs(mgr.get_root(), d)

        d2 = _dev(2, logical_addr=0x802)
        mgr.set_root(d2)
        self.assertIs(mgr.get_root(), d2)

    # 16
    def test_len(self):
        """len(manager) 反映当前注册设备总数"""
        mgr = DeviceManager()
        self.assertEqual(len(mgr), 0)
        mgr.add_device(_dev(1, logical_addr=0x001))
        self.assertEqual(len(mgr), 1)


class TestRoutingTables(unittest.TestCase):

    # 17
    def test_logic_table_crud(self):
        """PCLogicRoutingTable 增删查清基本操作"""
        table = PCLogicRoutingTable()
        pp = PortPath([1, 2])
        table.add(0x001, pp)
        self.assertEqual(table.lookup(0x001), pp)
        self.assertIn(0x001, table)
        self.assertEqual(len(table), 1)

        table.remove(0x001)
        self.assertIsNone(table.lookup(0x001))
        self.assertEqual(len(table), 0)

        table.add(0x002, pp)
        table.clear()
        self.assertEqual(len(table), 0)

    # 18
    def test_logic_table_lookup_missing(self):
        """查找不存在的逻辑地址返回 None"""
        table = PCLogicRoutingTable()
        self.assertIsNone(table.lookup(0x999))

    # 19
    def test_uid_table_crud(self):
        """PCUIDRoutingTable 增删查基本操作"""
        table = PCUIDRoutingTable()
        uid = bytes([0x01] * 12)
        pp = PortPath([1])
        table.add(uid, pp)
        self.assertEqual(table.lookup(uid), pp)
        self.assertEqual(len(table), 1)

        table.remove(uid)
        self.assertIsNone(table.lookup(uid))
        self.assertEqual(len(table), 0)

    # 20
    def test_device_manager_concurrent(self):
        """DeviceManager 多线程并发写入无竞争"""
        mgr = DeviceManager()
        errors: list[str] = []

        def writer(start: int):
            try:
                for i in range(50):
                    mgr.add_device(_dev((start + i) % 255 + 1,
                                        logical_addr=start * 100 + i))
            except Exception as e:
                errors.append(str(e))

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0, f"并发错误: {errors}")

    # 21
    def test_logic_table_concurrent(self):
        """PCLogicRoutingTable 4线程并发写200条无竞争"""
        table = PCLogicRoutingTable()
        errors: list[str] = []

        def writer(start: int):
            try:
                for i in range(50):
                    table.add(start * 100 + i, PortPath([i % 6 + 1]))
            except Exception as e:
                errors.append(str(e))

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0)
        self.assertEqual(len(table), 200)


if __name__ == '__main__':
    unittest.main(verbosity=2)
