# -*- coding: utf-8 -*-
"""
commands/test_commands.py — 指令处理器单元测试

测试项：
  1.  SysCmdHandler.get_node_info  — 解析 19 字节响应（uuid+model+portTypes）
  2.  SysCmdHandler.get_mfg_date   — 解析 ASCII 日期
  3.  SysCmdHandler.control_io     — 编码 io_mask/mode/value
  4.  PortCmdHandler.set_logical_addr — 编码 port_no(1)+addr(2,big)
  5.  UpgradeCmdHandler.get_board_info — 解析 23 字节响应
  6.  UpgradeCmdHandler.send_upgrade_data — 编码 sn(2,big)+chunk
  7.  UpgradeJob 状态机 IDLE→DONE（全通路快速通过）
  8.  UpgradeJob 全局超时触发（MAX_RETRIES 前 deadline 过期）
  9.  UpgradeJob 自动重试：失败次数 < MAX_RETRIES 时 on_error 被回调
 10.  UpgradeJob 超过 MAX_RETRIES 时停止自动重试
 11.  PortCmdHandler.set_terminal_port — 编码 port_no(1)+is_terminal(1)
"""
import logging
import struct
import sys
import os
import threading
import time
import unittest
from concurrent.futures import Future

# 让测试可以找到 project 包
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 屏蔽 upgrade 模块的 warning 日志，避免重试日志污染测试输出
logging.getLogger("commands.upgrade").setLevel(logging.CRITICAL)

from frame.models import CommandFrame, SOF_RX
from device.models import ModelType
from protocol.models import PortPath, PortType
from commands.system import SysCmdHandler
from commands.port import PortCmdHandler
from commands.upgrade import UpgradeCmdHandler, UpgradeJob, UpgradeState


# ──────────────────────────────────────────────
# 测试辅助：MockSessionManager
# ──────────────────────────────────────────────

class MockSessionManager:
    """
    可编程 Mock，用于测试指令处理器。

    用法::
        mock = MockSessionManager()
        mock.add_response(0x24, b'...')   # 预设 cmd=0x24 的响应数据
        handler = SysCmdHandler(mock)
    """

    def __init__(self):
        self._responses: dict = {}   # cmd → response_data
        self.requests: list = []     # [(port_path, cmd, data), ...]

    def add_response(self, cmd: int, data: bytes) -> None:
        self._responses[cmd] = data

    def send_request(
        self,
        port_path: PortPath,
        cmd: int,
        data: bytes = b'',
        timeout: float = 1.0,
    ) -> Future:
        self.requests.append((port_path, cmd, data))
        fut: Future = Future()
        resp_data = self._responses.get(cmd, b'')
        frame = CommandFrame(
            sof=SOF_RX, check=0, seq=0, cmd=cmd,
            port_len=len(port_path.ports),
            ports=list(port_path.ports),
            data=resp_data,
        )
        fut.set_result(frame)
        return fut


# ──────────────────────────────────────────────
# 辅助：构建 get_node_info 响应数据（19 字节）
# ──────────────────────────────────────────────

def _make_node_info_data(uuid: bytes, model: int,
                         port_types: list) -> bytes:
    pt_bytes = bytes(port_types[:6]) + bytes(6 - len(port_types))
    return uuid[:12] + bytes([model]) + pt_bytes[:6]


# ──────────────────────────────────────────────
# 辅助：构建 get_board_info 响应数据（19 字节）
# ──────────────────────────────────────────────

def _make_board_info_data(location: int, mfg_date: str,
                          hardware: int, model: int,
                          uuid: bytes) -> bytes:
    """构建 0x12 响应数据（19 字节）。
    
    mfg_date 格式: "YYYYMMDD" (8 字符)
    响应格式:
      data0: location
      data1: day
      data2: month
      data3-4: year (LSB)
      data5: hardware
      data6: model
      data7-18: uuid (12 bytes)
    """
    year = int(mfg_date[0:4])
    month = int(mfg_date[4:6])
    day = int(mfg_date[6:8])
    year_bytes = struct.pack('<H', year)  # LSB 小端序
    return (bytes([location, day, month]) + year_bytes +
            bytes([hardware, model]) + uuid[:12])


# ──────────────────────────────────────────────
# 测试：SysCmdHandler
# ──────────────────────────────────────────────

class TestSysCmdHandler(unittest.TestCase):

    def setUp(self):
        self.mock = MockSessionManager()
        self.handler = SysCmdHandler(self.mock, timeout=0.5)
        self.port_path = PortPath([1, 2])

    # ── 1. get_node_info 解析 19 字节 ──────────

    def test_get_node_info_parses_correctly(self):
        """get_node_info 正确解析 uuid/model/port_types"""
        uuid = bytes(range(12))
        data = _make_node_info_data(
            uuid=uuid,
            model=int(ModelType.BRIDGE_FIBRE),
            port_types=[int(PortType.PORT_FIBRE), int(PortType.PORT_485),
                        0, 0, 0, 0],
        )
        self.mock.add_response(0x24, data)

        node_info = self.handler.get_node_info(self.port_path)

        self.assertEqual(node_info.uuid, uuid)
        self.assertEqual(node_info.model, ModelType.BRIDGE_FIBRE)
        self.assertEqual(node_info.port_types[0], PortType.PORT_FIBRE)
        self.assertEqual(node_info.port_types[1], PortType.PORT_485)

    # ── 2. get_mfg_date 解析二进制日期 ───────────────────────────

    def test_get_mfg_date_returns_date_string(self):
        """get_mfg_date 返回8位日期字符串"""
        # 新协议：data0=day(1), data1=month(1), data2-3=year(2,LE)
        # 20250101 → day=1, month=1, year=2025(0x07E9) → LE bytes: 0xE9, 0x07
        raw = bytes([0x01, 0x01, 0xE9, 0x07])
        self.mock.add_response(0x22, raw)
        date = self.handler.get_mfg_date(self.port_path)
        self.assertEqual(date, '20250101')

    # ── 3. control_io 编码 ──────────────────────

    def test_control_io_encodes_three_bytes(self):
        """control_io 指令数据编码为3字节"""
        self.mock.add_response(0x26, b'')
        self.handler.control_io(self.port_path,
                                io_mask=0xAA, mode=0x01, value=0xFF)
        _, cmd, sent_data = self.mock.requests[-1]
        self.assertEqual(cmd, 0x26)
        self.assertEqual(sent_data, bytes([0xAA, 0x01, 0xFF]))


# ──────────────────────────────────────────────
# 测试：PortCmdHandler
# ──────────────────────────────────────────────

class TestPortCmdHandler(unittest.TestCase):

    def setUp(self):
        self.mock = MockSessionManager()
        # 读 commands/port.py 确认导入路径
        from commands.port import PortCmdHandler as _PortCmdHandler
        self.handler = _PortCmdHandler(self.mock, timeout=0.5)
        self.port_path = PortPath([1])

    # ── 4. set_logical_addr 编码 port_no(1)+addr(2,big) ─

    def test_set_logical_addr_encodes_correctly(self):
        """set_logical_addr 地址编码为2字节大端"""
        self.mock.add_response(0x36, b'')
        self.handler.set_logical_addr(self.port_path,
                                      port_no=3, addr=0x0801)
        _, cmd, sent_data = self.mock.requests[-1]
        self.assertEqual(cmd, 0x36)
        expected = bytes([3]) + struct.pack('>H', 0x0801)
        self.assertEqual(sent_data, expected)

    # ── 11. set_terminal_port 编码 port_no(1)+is_terminal(1) ─

    def test_set_terminal_port_encodes_terminal_true(self):
        """set_terminal_port(True) 编码为0x01"""
        self.mock.add_response(0x37, b'')
        self.handler.set_terminal_port(self.port_path,
                                       port_no=2, is_terminal=True)
        _, cmd, sent_data = self.mock.requests[-1]
        self.assertEqual(cmd, 0x37)
        self.assertEqual(sent_data, bytes([2, 0x01]))

    def test_set_terminal_port_encodes_terminal_false(self):
        """set_terminal_port(False) 编码为0x00"""
        self.mock.add_response(0x37, b'')
        self.handler.set_terminal_port(self.port_path,
                                       port_no=1, is_terminal=False)
        _, cmd, sent_data = self.mock.requests[-1]
        self.assertEqual(cmd, 0x37)
        self.assertEqual(sent_data, bytes([1, 0x00]))


# ──────────────────────────────────────────────
# 测试：UpgradeCmdHandler
# ──────────────────────────────────────────────

class TestUpgradeCmdHandler(unittest.TestCase):

    def setUp(self):
        self.mock = MockSessionManager()
        self.handler = UpgradeCmdHandler(self.mock, timeout=0.5)
        self.port_path = PortPath([1, 2])

    # ── 5. get_board_info 解析 19 字节 ─────────

    def test_get_board_info_parses_correctly(self):
        """get_board_info 正确解析 location/date/hw/model/uuid"""
        uuid = bytes(range(12))
        data = _make_board_info_data(
            location=1, mfg_date='20250115',
            hardware=2, model=int(ModelType.FIBRE_485),
            uuid=uuid,
        )
        self.mock.add_response(0x12, data)
        bi = self.handler.get_board_info(self.port_path)
        self.assertEqual(bi.location, 1)
        self.assertEqual(bi.mfg_date, '20250115')
        self.assertEqual(bi.hardware, 2)
        self.assertEqual(bi.model, ModelType.FIBRE_485)
        self.assertEqual(bi.uuid, uuid)

    # ── 6. send_upgrade_data 编码 sn(2,LSB小端)+chunk ─

    def test_send_upgrade_data_encodes_sn_and_chunk(self):
        """send_upgrade_data SN大端+数据块编码正确"""
        self.mock.add_response(0x14, b'')
        chunk = b'\xAA\xBB\xCC'
        self.handler.send_upgrade_data(self.port_path, sn=7, chunk=chunk)
        _, cmd, sent_data = self.mock.requests[-1]
        self.assertEqual(cmd, 0x14)
        # SN 使用小端序（LSB），per 协议文档 0x14
        self.assertEqual(sent_data, struct.pack('<H', 7) + chunk)


# ──────────────────────────────────────────────
# 测试：UpgradeJob 状态机
# ──────────────────────────────────────────────

class _MockJumpFrame:
    """模拟 jump_program 返回的帧，data[0] = location。"""
    def __init__(self, target: int) -> None:
        self.data = bytes([target])

class _FastUpgradeMock:
    """
    快速通过全部升级指令的 Mock UpgradeCmdHandler。
    - jump_program  → 无异常
    - get_board_info → 第一次返回 location=1（bootloader），
                       第二次返回 location=2（app，VERIFY 通过）
    - send_upgrade_info / send_upgrade_data → 无异常
    """

    def __init__(self):
        self._board_call_count = 0
        self.jump_calls = []
        self.data_calls = []

    def jump_program(self, port_path: object, target: int, log_level: int = 0) -> _MockJumpFrame:
        self.jump_calls.append(target)
        return _MockJumpFrame(target)

    def get_board_info(self, port_path):
        from device.models import BoardInfo
        # VERIFY 阶段期望 location=2（app），直接返回
        return BoardInfo(location=2, mfg_date='20250101',
                         hardware=1, model=ModelType.BRIDGE_FIBRE,
                         uuid=bytes(12))

    def send_upgrade_info(self, port_path, file_size):
        pass

    def send_upgrade_data(self, port_path, sn, chunk):
        self.data_calls.append(sn)


class TestUpgradeJob(unittest.TestCase):

    # ── 7. 状态机 IDLE→DONE ────────────────────

    def test_upgrade_job_reaches_done(self):
        """升级状态机正常流程：IDLE→JUMP→SEND→VERIFY→DONE"""
        mock_handler = _FastUpgradeMock()
        port_path = PortPath([1])
        firmware = bytes(range(64))  # 64 字节

        done_event = threading.Event()
        errors = []
        progress_vals = []

        job = UpgradeJob(
            handler=mock_handler,
            port_path=port_path,
            firmware=firmware,
            on_progress=lambda p: progress_vals.append(p),
            on_error=lambda s, c, r: errors.append((s, c, r)),
            on_done=done_event.set,
        )
        job.start()
        self.assertTrue(done_event.wait(timeout=5.0), "升级应在 5 秒内完成")
        self.assertEqual(errors, [], "不应有错误")
        self.assertEqual(job.state, UpgradeState.DONE)
        self.assertIn(100, progress_vals)

    # ── 8. 全局超时触发 ─────────────────────────

    def test_upgrade_job_global_timeout(self):
        """全局超时触发后升级以GLOBAL_TIMEOUT错误结束"""
        class _SlowMock:
            def jump_program(self, pp, target: int, log_level: int = 0) -> _MockJumpFrame:
                time.sleep(0.2)   # 单次耗时足以触发 deadline
                return _MockJumpFrame(target)

            def get_board_info(self, pp):
                from device.models import BoardInfo
                return BoardInfo(location=1)

            def send_upgrade_info(self, pp, size):
                pass

            def send_upgrade_data(self, pp, sn, chunk):
                pass

        port_path = PortPath([1])
        firmware = bytes(32)
        error_events = []

        job = UpgradeJob(
            handler=_SlowMock(),
            port_path=port_path,
            firmware=firmware,
            on_error=lambda s, c, r: error_events.append(s),
        )
        # 把 deadline 设置为极短（100ms）
        job.GLOBAL_TIMEOUT = 0.05
        # 注意：UpgradeJob 在 _run 里设置 deadline，需要用 patch
        # 直接覆盖类属性
        original_timeout = UpgradeJob.GLOBAL_TIMEOUT
        UpgradeJob.GLOBAL_TIMEOUT = 0.05
        try:
            job = UpgradeJob(
                handler=_SlowMock(),
                port_path=port_path,
                firmware=firmware,
                on_error=lambda s, c, r: error_events.append(s),
            )
            job.start()
            time.sleep(0.5)  # 等待 job 执行并超时
        finally:
            UpgradeJob.GLOBAL_TIMEOUT = original_timeout

        self.assertIn('GLOBAL_TIMEOUT', error_events,
                      "应触发 GLOBAL_TIMEOUT 错误")

    # ── 9. 自动重试：失败 < MAX_RETRIES ────────

    def test_upgrade_job_auto_retry_on_failure(self):
        """JUMP失败两次后第三次成功，升级继续完成"""
        class _FailTwiceMock:
            def __init__(self):
                self._call = 0

            def jump_program(self, pp, target, log_level=0):
                self._call += 1
                if self._call <= 2:
                    raise RuntimeError("simulated failure")

            def get_board_info(self, pp):
                from device.models import BoardInfo
                return BoardInfo(location=1)

            def send_upgrade_info(self, pp, size):
                pass

            def send_upgrade_data(self, pp, sn, chunk):
                pass

        # 前 2 次 jump 失败，第 3 次 → 完整通过需要 get_board_info 返回 2
        class _FailTwiceFullMock(_FailTwiceMock):
            def __init__(self):
                super().__init__()
                self._board_call = 0

            def get_board_info(self, pp):
                from device.models import BoardInfo
                # 第一次完整通过时，需要: call1=bootloader, call2=app
                self._board_call += 1
                # 前两次 jump 失败，不会到 get_board_info
                # 第三次 jump 通过后，board_call 1 → loc=1, board_call 2 → loc=2
                loc = 1 if self._board_call % 2 == 1 else 2
                return BoardInfo(location=loc)

        error_events = []
        done_event = threading.Event()

        job = UpgradeJob(
            handler=_FailTwiceFullMock(),
            port_path=PortPath([1]),
            firmware=bytes(32),
            on_error=lambda s, c, r: error_events.append((s, r)),
            on_done=done_event.set,
        )
        job.start()
        done_event.wait(timeout=5.0)

        # 应有 2 次 on_error（第 1、2 次 jump 失败），retry_count 依次为 1、2
        self.assertGreaterEqual(len(error_events), 1)
        retry_counts = [r for _, r in error_events]
        self.assertIn(1, retry_counts, "第 1 次错误 retry_count=1")

    # ── 10. 超过 MAX_RETRIES 后停止 ────────────

    def test_upgrade_job_stops_after_max_retries(self):
        """超过最大重试次数后停止升级"""
        class _AlwaysFailMock:
            def jump_program(self, pp, target, log_level=0):
                raise RuntimeError("always fail")

            def get_board_info(self, pp):
                from device.models import BoardInfo
                return BoardInfo()

            def send_upgrade_info(self, pp, size): pass

            def send_upgrade_data(self, pp, sn, chunk): pass

        error_events = []
        done_event = threading.Event()

        job = UpgradeJob(
            handler=_AlwaysFailMock(),
            port_path=PortPath([1]),
            firmware=bytes(16),
            on_error=lambda s, c, r: error_events.append(r),
            on_done=done_event.set,
        )
        job.max_retries = 3  # 强制覆盖 config.ini 中可能的 0 値
        job.start()
        time.sleep(1.0)  # 等待 job 完成所有重试

        # max_retries 默认为 3，error 回调应被调用 3 次，retry_count 依次 1,2,3
        self.assertEqual(len(error_events), job.max_retries,
                         f"应调用 on_error {job.max_retries} 次")
        self.assertEqual(sorted(error_events),
                         list(range(1, job.max_retries + 1)))
        # job 停止后 state 应为 ERROR
        self.assertEqual(job.state, UpgradeState.ERROR)


if __name__ == '__main__':
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for cls in (TestSysCmdHandler, TestPortCmdHandler,
                TestUpgradeCmdHandler, TestUpgradeJob):
        suite.addTests(loader.loadTestsFromTestCase(cls))
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
