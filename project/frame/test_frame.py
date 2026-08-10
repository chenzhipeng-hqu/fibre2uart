# -*- coding: utf-8 -*-
"""
frame/test_frame.py — FrameParser / FrameBuilder 单元测试

测试项：
  Builder:
    1.  build_tx SOF=0xAA
    2.  build_rx SOF=0xAB
    3.  len 字段 = 6 + portLen + dataLen
    4.  checksum 公式正确
    5.  seq / cmd / portLen 字段位置
    6.  零端口零数据最小帧
    7.  大数据帧（240 字节）
  Parser:
    8.  解析最小帧
    9.  解析带端口带数据帧
    10. 粘包（两帧连续喂入）
    11. 断包（逐字节喂入）
    12. len_overflow → on_error，不 yield
    13. port_len_overflow → on_error，不 yield
    14. len_too_small → on_error，不 yield
    15. checksum_mismatch → on_error，仍 yield（放行策略）
    16. 噪声字节（非 0xAB SOF）被跳过
    17. 0xAA SOF（PC→MCU 帧）被忽略
    18. on_frame 回调触发
    19. Builder+Parser 往返一致
    20. 坏帧后恢复解析好帧
    21. 广播端口 0x00 正常解析
    22. 三帧连续粘包
"""
from __future__ import annotations

import sys
import os
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from frame.builder import FrameBuilder
from frame.parser import FrameParser, MAX_FRAME_DATA_LEN
from frame.models import CommandFrame, SOF_TX, SOF_RX


def _rx(seq: int, cmd: int, ports: list, data: bytes) -> bytes:
    return FrameBuilder.build_rx(seq, cmd, ports, data)


def _parse(raw: bytes, on_error=None):
    return list(FrameParser(on_error=on_error).feed(raw))


# ──────────────────────────────────────────────
class TestFrameBuilder(unittest.TestCase):

    # 1
    def test_build_tx_sof(self):
        """build_tx 帧首字节为 0xAA（PC→MCU 标识）"""
        raw = FrameBuilder.build_tx(1, 0x24, [], b'')
        self.assertEqual(raw[0], SOF_TX)

    # 2
    def test_build_rx_sof(self):
        """build_rx 帧首字节为 0xAB（MCU→PC 标识）"""
        raw = FrameBuilder.build_rx(1, 0x24, [], b'')
        self.assertEqual(raw[0], SOF_RX)

    # 3
    def test_len_field_no_ports_no_data(self):
        """无路由无数据时 len 字段=6（最小帧）"""
        raw = FrameBuilder.build_tx(1, 0x24, [], b'')
        self.assertEqual(raw[3], 6)

    def test_len_field_with_ports_and_data(self):
        """有路由有数据时 len 字段=6+portLen+dataLen"""
        ports, data = [1, 2], b'\x01\x02\x03'
        raw = FrameBuilder.build_tx(1, 0x24, ports, data)
        self.assertEqual(raw[3], 6 + len(ports) + len(data))

    # 4
    def test_checksum_formula(self):
        """校验值=SUM(seq+len+cmd+portLen+ports+data)&0xFF"""
        seq, cmd, ports, data = 10, 0x24, [1, 2], b'\xAA\xBB'
        raw = FrameBuilder.build_rx(seq, cmd, ports, data)
        length = raw[3]
        port_len = raw[5]
        expected = (seq + length + cmd + port_len
                    + sum(ports) + sum(data)) & 0xFF
        self.assertEqual(raw[1], expected)

    # 5
    def test_field_positions(self):
        """帧各字段位置：seq[2]/cmd[4]/portLen[5]/ports/data"""
        seq, cmd, ports, data = 7, 0x21, [3, 4], b'\xFF'
        raw = FrameBuilder.build_tx(seq, cmd, ports, data)
        self.assertEqual(raw[2], seq)
        self.assertEqual(raw[4], cmd)
        self.assertEqual(raw[5], len(ports))
        self.assertEqual(list(raw[6:6+len(ports)]), ports)
        self.assertEqual(raw[6+len(ports):], data)

    # 6
    def test_minimal_frame_length(self):
        """零路由零数据时整帧字节数恰好为 6"""
        raw = FrameBuilder.build_tx(0, 0x00, [], b'')
        self.assertEqual(len(raw), 6)

    # 7
    def test_large_data_frame(self):
        """240字节数据帧长度字段和载荷正确"""
        data = bytes(range(240))
        raw = FrameBuilder.build_rx(1, 0x14, [1], data)
        self.assertEqual(raw[3], 6 + 1 + 240)
        self.assertEqual(raw[7:], data)


# ──────────────────────────────────────────────
class TestFrameParser(unittest.TestCase):

    # 8
    def test_parse_minimal_frame(self):
        """解析最小帧：sof/seq/cmd/ports/data 均正确"""
        raw = _rx(1, 0x24, [], b'')
        frames = _parse(raw)
        self.assertEqual(len(frames), 1)
        f = frames[0]
        self.assertEqual(f.sof, SOF_RX)
        self.assertEqual(f.seq, 1)
        self.assertEqual(f.cmd, 0x24)
        self.assertEqual(f.ports, [])
        self.assertEqual(f.data, b'')

    # 9
    def test_parse_with_ports_and_data(self):
        """解析含路由含数据帧：字段值一一对应"""
        ports, data = [1, 3], b'\x10\x20\x30'
        raw = _rx(7, 0x21, ports, data)
        frames = _parse(raw)
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].ports, ports)
        self.assertEqual(frames[0].data, data)

    # 10
    def test_sticky_packets_two(self):
        """粘包：两帧连续字节流能解析出 2 个独立帧"""
        r1 = _rx(1, 0x24, [], b'')
        r2 = _rx(2, 0x21, [1], b'\xFF')
        frames = _parse(r1 + r2)
        self.assertEqual(len(frames), 2)
        self.assertEqual(frames[0].seq, 1)
        self.assertEqual(frames[1].seq, 2)

    # 11
    def test_fragmented_packet(self):
        """断包：逐字节喂入仍能完整解析出 1 帧"""
        raw = _rx(3, 0x24, [2], b'\xAB')
        parser = FrameParser()
        frames = []
        for b in raw:
            frames.extend(parser.feed(bytes([b])))
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].seq, 3)

    # 12
    def test_len_overflow_error(self):
        """len超限触发on_error('len_overflow')且不输出帧"""
        import frame.parser as fp_mod
        original = fp_mod.MAX_FRAME_DATA_LEN
        fp_mod.MAX_FRAME_DATA_LEN = 10
        try:
            errors: list[str] = []
            bad = bytes([SOF_RX, 0x00, 0x01, 11, 0x24, 0x00])
            frames = _parse(bad, on_error=lambda r, reason: errors.append(reason))
            self.assertEqual(len(frames), 0)
            self.assertIn('len_overflow', errors)
        finally:
            fp_mod.MAX_FRAME_DATA_LEN = original

    # 13
    def test_port_len_overflow_error(self):
        """portLen超出剩余空间触发on_error且不输出帧"""
        errors = []
        seq, cmd, length, port_len = 1, 0x24, 6, 1  # port_len > len-6=0
        check = (seq + length + cmd + port_len) & 0xFF
        bad = bytes([SOF_RX, check, seq, length, cmd, port_len])
        frames = _parse(bad, on_error=lambda r, reason: errors.append(reason))
        self.assertEqual(len(frames), 0)
        self.assertIn('port_len_overflow', errors)

    # 14
    def test_len_too_small_error(self):
        """len<6触发on_error('len_too_small')且不输出帧"""
        errors = []
        seq, cmd, length = 1, 0x24, 5  # < 6
        check = (seq + length + cmd) & 0xFF
        bad = bytes([SOF_RX, check, seq, length, cmd, 0x00])
        frames = _parse(bad, on_error=lambda r, reason: errors.append(reason))
        self.assertEqual(len(frames), 0)
        self.assertIn('len_too_small', errors)

    # 15  checksum_mismatch 仍放行
    def test_checksum_mismatch_still_yields(self):
        """校验不符触发on_error但帧仍放行（容错策略）"""
        errors = []
        raw = bytearray(_rx(5, 0x24, [], b''))
        raw[1] ^= 0xFF
        frames = _parse(bytes(raw), on_error=lambda r, reason: errors.append(reason))
        self.assertEqual(len(frames), 1)   # 仍放行
        self.assertIn('checksum_mismatch', errors)

    # 16
    def test_noise_bytes_skipped(self):
        """SOF前的噪声字节被跳过，后续帧正常解析"""
        noise = bytes([0x00, 0xFF, 0xAA, 0x12])
        raw = _rx(9, 0x21, [], b'')
        frames = _parse(noise + raw)
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].seq, 9)

    # 17
    def test_tx_sof_ignored(self):
        """SOF=0xAA的PC→MCU帧被解析器忽略"""
        raw = FrameBuilder.build_tx(3, 0x24, [1], b'\x01')
        frames = _parse(raw)
        self.assertEqual(len(frames), 0)

    # 18
    def test_on_frame_callback(self):
        """on_frame回调在每帧解析完成时被触发一次"""
        raw = _rx(4, 0x24, [], b'')
        parser = FrameParser()
        received = []
        parser.on_frame = received.append
        list(parser.feed(raw))
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0].seq, 4)

    # 19
    def test_roundtrip(self):
        """Builder构建帧经Parser解析后字段完全一致"""
        ports, data = [1, 2, 3], bytes(range(16))
        raw = _rx(42, 0x14, ports, data)
        frames = _parse(raw)
        self.assertEqual(len(frames), 1)
        f = frames[0]
        self.assertEqual(f.seq, 42)
        self.assertEqual(f.cmd, 0x14)
        self.assertEqual(f.ports, ports)
        self.assertEqual(f.data, data)

    # 20
    def test_recover_after_bad_frame(self):
        """坏帧之后能继续解析后续正常帧"""
        bad = bytearray(_rx(1, 0x24, [], b'\x01'))
        bad[-1] ^= 0xFF   # 破坏数据，但 parser 仍放行（checksum_mismatch 策略）
        good = _rx(2, 0x25, [], b'\x02')
        frames = _parse(bytes(bad) + good)
        seqs = [f.seq for f in frames]
        self.assertIn(2, seqs)

    # 21
    def test_broadcast_port_parsed(self):
        """广播端口0x00可正常解析到ports字段"""
        raw = _rx(9, 0x11, [0x00], b'\x01')
        frames = _parse(raw)
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].ports, [0x00])

    # 22
    def test_three_sticky_frames(self):
        """三帧连续粘包能全部解析出且seq顺序正确"""
        combined = b''.join(_rx(i, 0x24, [], b'') for i in range(3))
        frames = _parse(combined)
        self.assertEqual(len(frames), 3)
        self.assertEqual([f.seq for f in frames], [0, 1, 2])


if __name__ == '__main__':
    unittest.main(verbosity=2)
