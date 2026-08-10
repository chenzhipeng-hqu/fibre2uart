# -*- coding: utf-8 -*-
"""
protocol/test_protocol.py — PortPath / CommandCodec / 枚举 单元测试

测试项：
  PortPath:
    1.  len() / iter()
    2.  append 不可变语义
    3.  broadcast() / local() 工厂方法
    4.  is_broadcast() / is_local()
    5.  __eq__ ports 与 has485 均参与比较
    6.  has485=True 路径
    7.  空路径 len=0
  CommandCodec:
    8.  encode 生成 SOF=0xAA 帧
    9.  decode 字段正确提取
    10. encode+decode 往返：ports / cmd / data 一致
    11. 空 ports 空 data 编解码
    12. 广播路径编解码
    13. decode has485 默认 False
  枚举:
    14. PortType 值
    15. ModelType 值
"""
from __future__ import annotations

import sys
import os
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from protocol.models import PortPath, PortType, ModelType
from protocol.codec import CommandCodec
from frame.builder import FrameBuilder
from frame.parser import FrameParser
from frame.models import SOF_TX


class TestPortPath(unittest.TestCase):

    # 1
    def test_len_and_iter(self):
        """PortPath 支持 len() 和 iter() 容器协议"""
        pp = PortPath(ports=[1, 2, 3])
        self.assertEqual(len(pp), 3)
        self.assertEqual(list(pp), [1, 2, 3])

    # 2
    def test_append_immutable(self):
        """append 返回新实例，原对象不变（不可变语义）"""
        pp = PortPath(ports=[1])
        pp2 = pp.append(2, 3)
        self.assertEqual(pp.ports, [1])        # 原对象不变
        self.assertEqual(pp2.ports, [1, 2, 3])
        self.assertIsNot(pp, pp2)

    # 3
    def test_broadcast_factory(self):
        """broadcast() 工厂方法生成 ports=[0x00] 路径"""
        bc = PortPath.broadcast()
        self.assertEqual(bc.ports, [0x00])

    def test_local_factory(self):
        """local() 工厂方法生成空 ports 本机路径"""
        lc = PortPath.local()
        self.assertEqual(lc.ports, [])

    # 4
    def test_is_broadcast(self):
        """is_broadcast() 正确识别广播路径"""
        self.assertTrue(PortPath.broadcast().is_broadcast())
        self.assertFalse(PortPath([1]).is_broadcast())

    def test_is_local(self):
        """is_local() 正确识别本机空路径"""
        self.assertTrue(PortPath.local().is_local())
        self.assertFalse(PortPath([1]).is_local())

    # 5
    def test_eq_ports_and_has485(self):
        """相等性比较同时考虑 ports 和 has485 字段"""
        pp1 = PortPath(ports=[1, 2], has485=False)
        pp2 = PortPath(ports=[1, 2], has485=False)
        pp3 = PortPath(ports=[1, 2], has485=True)
        pp4 = PortPath(ports=[1, 3], has485=False)
        self.assertEqual(pp1, pp2)
        self.assertNotEqual(pp1, pp3)   # has485 不同
        self.assertNotEqual(pp1, pp4)   # ports 不同

    # 6
    def test_has485_path(self):
        """has485=True 的 RS485 路径属性正确"""
        pp = PortPath(ports=[1, 2, 5, 3], has485=True)
        self.assertTrue(pp.has485)
        self.assertEqual(len(pp), 4)
        self.assertEqual(pp.ports[2], 5)  # rs485Addr 在倒数第二位

    # 7
    def test_empty_path_len_zero(self):
        """空路径 len=0，iter 为空"""
        pp = PortPath.local()
        self.assertEqual(len(pp), 0)
        self.assertEqual(list(pp), [])


class TestCommandCodec(unittest.TestCase):

    def _build_rx_and_parse(self, seq: int, cmd: int, ports: list[int], data: bytes):
        raw = FrameBuilder.build_rx(seq, cmd, ports, data)
        return list(FrameParser().feed(raw))[0]

    # 8
    def test_encode_produces_sof_tx(self):
        """encode 生成 SOF=0xAA 的 PC→MCU 帧"""
        pp = PortPath([1])
        raw = CommandCodec.encode(1, 0x24, pp, b'')
        self.assertEqual(raw[0], SOF_TX)

    # 9
    def test_decode_fields(self):
        """decode 从帧中正确提取 cmd/ports/data"""
        cmd, ports, data = 0x24, [1, 2], b'\x01\x02\x03'
        frame = self._build_rx_and_parse(5, cmd, ports, data)
        dec_cmd, dec_pp, dec_data = CommandCodec.decode(frame)
        self.assertEqual(dec_cmd, cmd)
        self.assertEqual(dec_pp.ports, ports)
        self.assertEqual(dec_data, data)

    # 10
    def test_roundtrip_ports_cmd_data(self):
        """encode+decode 往返后 ports/cmd/data 一致"""
        pp = PortPath([1, 2])
        cmd, seq, data = 0x21, 42, b'\xDE\xAD\xBE\xEF'
        frame = self._build_rx_and_parse(seq, cmd, list(pp.ports), data)
        dec_cmd, dec_pp, dec_data = CommandCodec.decode(frame)
        self.assertEqual(dec_cmd, cmd)
        self.assertEqual(dec_pp.ports, pp.ports)
        self.assertEqual(dec_data, data)

    # 11
    def test_empty_ports_and_data(self):
        """空路由空数据的编解码正确"""
        frame = self._build_rx_and_parse(1, 0x21, [], b'')
        dec_cmd, dec_pp, dec_data = CommandCodec.decode(frame)
        self.assertEqual(dec_pp.ports, [])
        self.assertEqual(dec_data, b'')

    # 12
    def test_broadcast_path(self):
        """广播路径 [0x00] 经解码后 is_broadcast() 为 True"""
        frame = self._build_rx_and_parse(5, 0x11, [0x00], b'\x02')
        dec_cmd, dec_pp, dec_data = CommandCodec.decode(frame)
        self.assertEqual(dec_pp.ports, [0x00])
        self.assertTrue(dec_pp.is_broadcast())

    # 13
    def test_decode_has485_defaults_false(self):
        """decode 生成的 PortPath has485 默认为 False"""
        frame = self._build_rx_and_parse(1, 0x24, [1, 2], b'')
        _, dec_pp, _ = CommandCodec.decode(frame)
        self.assertFalse(dec_pp.has485)


class TestEnums(unittest.TestCase):

    # 14
    def test_port_type_values(self):
        """PortType 枚举值与协议规范一致"""
        self.assertEqual(PortType.NONE, 0)
        self.assertEqual(PortType.PORT_PC, 1)
        self.assertEqual(PortType.PORT_UART, 2)
        self.assertEqual(PortType.PORT_FIBRE, 3)
        self.assertEqual(PortType.PORT_232, 4)
        self.assertEqual(PortType.PORT_485, 5)
        self.assertEqual(PortType.PORT_CAN, 6)

    # 15
    def test_model_type_values(self):
        """ModelType 枚举值与协议规范一致"""
        self.assertEqual(ModelType.BRIDGE_485, 0)
        self.assertEqual(ModelType.FIBRE_485, 1)
        self.assertEqual(ModelType.UNKNOWN, 2)
        self.assertEqual(ModelType.BRIDGE_FIBRE, 3)
        self.assertEqual(ModelType.PC_FIBRE, 0x13)


if __name__ == '__main__':
    unittest.main(verbosity=2)
