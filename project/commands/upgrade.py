# -*- coding: utf-8 -*-
"""
commands/upgrade.py — 升级指令处理器（0x11~0x15）+ UpgradeJob 状态机

UpgradeCmdHandler : 原始指令 I/O，无状态
UpgradeJob        : 状态机，后台线程驱动完整升级流程

状态机（§4.5）：
  IDLE → JUMP → CHECK → SEND_INFO → SEND_DATA → VERIFY → DONE / ERROR

整体超时保护：
  进入 JUMP 时记录 _upgrade_deadline = now + 120s
  每次状态转移前检查；超时 → ERROR(stage="GLOBAL_TIMEOUT")

重试逻辑：
  retry_count < MAX_RETRIES(3)  → on_error 通知 + 自动重新从 JUMP 开始
  retry_count >= MAX_RETRIES    → on_error 通知 + 停止，等待 user 调用 retry()

数据格式：
  0x11 jump_program      : data0=target(1)  [1=bootloader, 2=app]  data1=log_level(1) [("0 默认", 0), ("1 ERROR", 1), ("2 WARNING", 2), ("3 INFO", 3), ("4 DEBUG", 4)]
  0x12 get_board_info    : 请求 data=[]；响应=location(1)+date(8)+hw(1)+model(1)+uuid(12)
  0x13 send_upgrade_info : data=file_size(4,big)
  0x14 send_upgrade_data : data=sn(2,little-endian)+chunk(N)  sn 从 1 开始，循环至 65535 后回到 1
  0x15 query_progress    : 请求 data=[]；响应=progress(1) [0~100]
"""
from __future__ import annotations

import logging
import struct
import threading
import time
from enum import Enum, auto
from typing import Callable, List, Optional

from device.models import BoardInfo
from protocol.models import ModelType, PortPath
from session.session_manager import SessionManager

logger = logging.getLogger(__name__)


class UpgradeState(Enum):
    IDLE = auto()
    JUMP = auto()
    CHECK = auto()
    SEND_INFO = auto()
    SEND_DATA = auto()
    VERIFY = auto()
    DONE = auto()
    ERROR = auto()


# ──────────────────────────────────────────────
# 原始指令 I/O
# ──────────────────────────────────────────────

class UpgradeCmdHandler:
    """原始升级指令收发（0x11~0x15），无状态，可复用。"""

    CHUNK_SIZE = 240
    BROADCAST_INTERVAL = 0.05   # 广播升级包间隔 50ms（协议要求）
    UNICAST_INTERVAL   = 0.02   # 单播升级包间隔 20ms
    BROADCAST_JUMP_WAIT   = 1.5  # 广播 jump 后等待 bootloader 就绪（秒）
    BROADCAST_ERASE_WAIT  = 4.0  # 广播 send_info 后等待擦除完成（秒）
    BROADCAST_VERIFY_WAIT = 2.0  # 广播 verify jump(2) 后等待 APP 就绪（秒）

    def __init__(self, session_manager: SessionManager,
                 timeout: float = 2.0) -> None:
        self._session = session_manager
        self._timeout = timeout
        # 单包数据重发次数（不含首次发送）
        try:
            from config import get_config
            self._data_send_retries: int = get_config().upgrade_data_send_retries
        except Exception:
            self._data_send_retries = 3

    def jump_program(self, port_path: PortPath, target: int, log_level: int = 0):
        """0x11 跳转程序位置。target: 1=bootloader, 2=app; log_level: 0=默认,1=ERROR,2=WARNING,3=INFO,4=DEBUG

        广播模式：fire-and-forget，不等待响应，返回 None。
        单播模式：等待 MCU 响应帧，返回 CommandFrame。
        """
        data = bytes([target & 0xFF, log_level & 0xFF])
        if port_path.is_broadcast():
            self._session.send_no_reply(port_path, 0x11, data)
            return None
        fut = self._session.send_request(port_path, 0x11, data, self._timeout)
        return fut.result(timeout=self._timeout + 1)

    def get_board_info(self, port_path: PortPath) -> BoardInfo:
        """0x12 获取板卡信息。
        
        响应格式（19 字节）：
          data0: 当前程序位置（1=BOOTLOAD, 2=APP）
          data1: day
          data2: month
          data3-4: year (LSB)
          data5: hardware
          data6: model
          data7-18: uuid (12 bytes)
        """
        fut = self._session.send_request(port_path, 0x12, b'', self._timeout)
        frame = fut.result(timeout=self._timeout + 0.5)
        data = frame.data
        # print(f"get_board_info: raw response={data.hex(' ')}")
        if len(data) < 19:
            raise ValueError(f"get_board_info: short response {len(data)} bytes")
        
        location = data[0]
        day = data[1]
        month = data[2]
        year = struct.unpack('<H', data[3:5])[0]  # LSB 小端序
        mfg_date = f"{year:04d}{month:02d}{day:02d}"
        hardware = data[5]
        model_byte = data[6]
        try:
            model = ModelType(model_byte)
        except ValueError:
            model = ModelType.UNKNOWN
        uuid = data[7:19]
        logger.debug("get_board_info: location=%d, mfg_date=%s, hardware=%d, model=0x%02X, uuid=%s",
                     location, mfg_date, hardware, model_byte, uuid.hex())
        return BoardInfo(location=location, mfg_date=mfg_date,
                         hardware=hardware, model=model, uuid=uuid)

    def send_upgrade_info(self, port_path: PortPath, file_size: int) -> None:
        """0x13 发送升级信息，触发擦除。

        请求: data0-3 = file_size (LSB 小端序)
        单播响应: data0 = 0x01(擦除成功) / 0x02(擦除失败) / 0x03(数据过大)
        广播模式: fire-and-forget，不等待擦除 ACK，调用方负责等待足够时间。
        """
        data = struct.pack('<I', file_size)  # LSB 小端序
        logger.info("send_upgrade_info: file_size=%d (0x%08X)", file_size, file_size)

        if port_path.is_broadcast():
            self._session.send_no_reply(port_path, 0x13, data)
            logger.info("send_upgrade_info(broadcast): 已发送，不等待 ACK")
            return

        # 单播：等待擦除 ACK（耗时较长，timeout=3s）
        fut = self._session.send_request(port_path, 0x13, data, timeout=3.0)
        logger.info("send_upgrade_info: 请求已发送，等待擦除完成...")
        try:
            frame = fut.result(timeout=3.5)
        except Exception as e:
            logger.error("send_upgrade_info: 等待响应失败: %s: %s", type(e).__name__, e)
            raise

        if not frame.data:
            raise ValueError("send_upgrade_info: empty response")
        status = frame.data[0]
        logger.info("send_upgrade_info: 擦除完成，状态=0x%02X", status)
        if status == 0x01:
            time.sleep(1.0)  # 擦除成功后等待 1s，确保 MCU 准备就绪
        elif status == 0x02:
            raise RuntimeError("send_upgrade_info: 擦除失败 (MCU 返回 0x02)")
        elif status == 0x03:
            raise RuntimeError(f"send_upgrade_info: 数据过大无法升级 (file_size={file_size})")
        else:
            raise ValueError(f"send_upgrade_info: 未知响应状态 0x{status:02X}")

    def send_upgrade_data(self, port_path: PortPath,
                          sn: int, chunk: bytes) -> None:
        """0x14 发送一包升级数据。sn(2, little-endian) + chunk(N)。

        广播模式: fire-and-forget，不等待 ACK。
        单播模式: 等待响应并检查 data[2] 状态字节：
            0x01=继续升级, 0x02=升级完成, 0x03=升级失败, 0x04=程序未擦除
        单播失败自动重发，重发次数由 config[upgrade][data_send_retries] 控制。
        """
        data = struct.pack('<H', sn) + chunk

        if port_path.is_broadcast():
            self._session.send_no_reply(port_path, 0x14, data)
            return  # fire-and-forget，不等待 ACK

        # 单播：等待 ACK + 检查状态
        last_exc: Optional[Exception] = None
        for attempt in range(1 + self._data_send_retries):
            try:
                fut = self._session.send_request(port_path, 0x14, data, self._timeout)
                frame = fut.result(timeout=self._timeout + 0.5)
                # 检查响应状态字节 data[2]
                if frame.data and len(frame.data) >= 3:
                    status = frame.data[2]
                    if status == 0x03:
                        raise RuntimeError(f"send_upgrade_data sn={sn}: MCU 返回升级失败 (0x03)")
                    elif status == 0x04:
                        raise RuntimeError(f"send_upgrade_data sn={sn}: 程序未擦除 (0x04)")
                    # 0x01=继续, 0x02=完成 均视为成功
                return
            except Exception as exc:
                last_exc = exc
                if attempt < self._data_send_retries:
                    logger.warning(
                        "send_upgrade_data sn=%d 失败(%s)，第%d次重发...",
                        sn, type(exc).__name__, attempt + 1)
                    time.sleep(0.1)
                else:
                    logger.error(
                        "send_upgrade_data sn=%d 重发%d次均失败: %s",
                        sn, self._data_send_retries, exc)
        raise last_exc  # type: ignore[misc]

    def query_upgrade_progress(self, port_path: PortPath) -> int:
        """0x15 查询升级进度，返回 0~100。"""
        fut = self._session.send_request(port_path, 0x15, b'', self._timeout)
        frame = fut.result(timeout=self._timeout + 0.5)
        return frame.data[0] if frame.data else 0


# ──────────────────────────────────────────────
# 升级状态机
# ──────────────────────────────────────────────

class UpgradeJob:
    """
    升级状态机，后台线程驱动整个升级流程。

    单播模式：port_path 指向具体节点，unicast_paths=None。
    广播升级模式：port_path=PortPath.broadcast()（占位），unicast_paths 为已按路径深度
    降序排列的节点列表。JUMP/SEND_INFO/VERIFY 单播每个节点（最远优先），
    SEND_DATA 广播发送。

    :param handler:        UpgradeCmdHandler 实例
    :param port_path:      目标设备路由路径
    :param firmware:       固件字节串
    :param unicast_paths:  广播升级时的单播路径列表（按深度降序）
    :param on_progress:    进度回调 (progress: int)
    :param on_error:       错误回调 (stage: str, error_code: int, retry_count: int)
    :param on_done:        完成回调 ()
    """

    GLOBAL_TIMEOUT = 120.0   # 2 分钟整体超时

    def __init__(
        self,
        handler: UpgradeCmdHandler,
        port_path: PortPath,
        firmware: bytes,
        on_progress: Optional[Callable[[int], None]] = None,
        on_error: Optional[Callable[[str, int, int], None]] = None,
        on_done: Optional[Callable[[], None]] = None,
        log_level: int = 0,
        unicast_paths: Optional[List[PortPath]] = None,
    ) -> None:
        self._handler = handler
        self._port_path = port_path
        self._firmware = firmware
        self._on_progress = on_progress or (lambda p: None)
        self._on_error = on_error or (lambda s, c, r: None)
        self._on_done = on_done or (lambda: None)
        self._log_level = log_level
        # None=单播模式；非 None=广播升级模式（已按路径深度降序）
        self._unicast_paths: Optional[List[PortPath]] = unicast_paths

        # 从配置读取最大重试次数
        try:
            from config import get_config
            self.max_retries = get_config().upgrade_max_retries
        except Exception:
            self.max_retries = 3

        self.state: UpgradeState = UpgradeState.IDLE
        self.retry_count: int = 0
        self._upgrade_deadline: float = 0.0
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    def start(self) -> None:
        """启动升级（仅在 IDLE 状态可调用）。"""
        if self.state != UpgradeState.IDLE:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name='UpgradeJob')
        self._thread.start()

    def retry(self) -> None:
        """用户点击"重试"（retry_count >= max_retries 后调用）。"""
        self.retry_count = 0
        self.state = UpgradeState.IDLE
        self.start()

    def cancel(self) -> None:
        """取消升级（尽力而为，不保证立即停止）。"""
        self._stop_event.set()

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------

    def _check_deadline(self) -> None:
        if self._stop_event.is_set():
            raise RuntimeError("upgrade cancelled")
        if time.monotonic() > self._upgrade_deadline:
            raise TimeoutError("GLOBAL_TIMEOUT")

    def _do_single_pass(self) -> None:
        """执行一次完整升级（JUMP → DONE）；失败抛异常。"""
        if self._unicast_paths is not None:
            self._do_broadcast_pass()
        else:
            self._do_unicast_pass()

    def _do_unicast_pass(self) -> None:
        """单节点升级流程（JUMP → DONE）。"""
        from config import get_config
        jump_target = 1
        verify_target = 2

        # ── JUMP ──────────────────────────────────────────────────────
        self.state = UpgradeState.JUMP
        jump_retries = max(1, min(10, getattr(get_config(), "upgrade_jump_retries", 3)))
        for attempt in range(jump_retries):
            self._check_deadline()
            frame = self._handler.jump_program(self._port_path, jump_target, self._log_level)
            location = None
            if hasattr(frame, 'data') and frame.data and len(frame.data) > 0:
                location = frame.data[0]
            if location == jump_target:
                break
            if attempt < jump_retries - 1:
                logger.warning("jump 返回不一致，期望=%d，实际=%s，重试 %d/%d",
                               jump_target, location, attempt + 1, jump_retries)
                time.sleep(1)
            else:
                raise RuntimeError(
                    f"jump failed: 期望={jump_target}，实际={location}，已重试{jump_retries}次")

        # ── SEND_INFO ─────────────────────────────────────────────────
        self.state = UpgradeState.SEND_INFO
        self._check_deadline()
        self._handler.send_upgrade_info(self._port_path, len(self._firmware))

        # ── SEND_DATA ─────────────────────────────────────────────────
        self.state = UpgradeState.SEND_DATA
        offset = 0
        sn = 1
        total = len(self._firmware)
        while offset < total:
            self._check_deadline()
            chunk = self._firmware[offset: offset + UpgradeCmdHandler.CHUNK_SIZE]
            self._handler.send_upgrade_data(self._port_path, sn, chunk)
            offset += len(chunk)
            sn = (sn % 65535) + 1
            self._on_progress(min(int(offset / total * 90), 90))
            time.sleep(UpgradeCmdHandler.UNICAST_INTERVAL)

        # ── VERIFY ────────────────────────────────────────────────────
        self.state = UpgradeState.VERIFY
        self._check_deadline()
        logger.info("VERIFY: 发送 jump_program(2) 跳转到 APP...")
        self._handler.jump_program(self._port_path, verify_target)
        time.sleep(0.8)
        self._check_deadline()
        logger.info("VERIFY: 查询板卡信息，确认已跳转到 APP...")
        board_info2 = self._handler.get_board_info(self._port_path)
        logger.info("VERIFY: location=%d, model=0x%02X",
                    board_info2.location, int(board_info2.model))
        if board_info2.location != verify_target:
            raise RuntimeError(
                f"verify failed: 期望={verify_target}，实际={board_info2.location}")

        # ── DONE ──────────────────────────────────────────────────────
        self.state = UpgradeState.DONE
        self._on_progress(100)
        self._on_done()

    def _do_broadcast_pass(self) -> None:
        """广播升级流程。

        按路径深度降序分组，每组执行完整的升级周期：
          JUMP(单播本组) → SEND_INFO(单播本组) → SEND_DATA(广播) → VERIFY(单播本组)
        完成后再处理下一组（深度较浅的节点）。

        示例（3组）：
          深度3节点: JUMP → SEND_INFO → 广播SEND_DATA → VERIFY
          深度2节点: JUMP → SEND_INFO → 广播SEND_DATA → VERIFY
          深度1节点: JUMP → SEND_INFO → 广播SEND_DATA → VERIFY

        最远节点优先的原因：若先让中间节点进入 bootloader 且丢失路由能力，
        则更深节点将无法被寻址；从最深节点开始可保证链路不中断。
        """
        from config import get_config
        from itertools import groupby

        paths = self._unicast_paths or []   # 已按深度降序排列
        broadcast_path = PortPath.broadcast()
        jump_target = 1
        verify_target = 2
        jump_retries = max(1, min(10, getattr(get_config(), "upgrade_jump_retries", 3)))
        total_fw = len(self._firmware)

        # 按路径深度分组（已降序，groupby 直接用）
        groups: list = []
        for depth, grp in groupby(paths, key=lambda p: len(p.ports)):
            groups.append((depth, list(grp)))
        total_groups = len(groups)

        for g_idx, (depth, group) in enumerate(groups):
            n = len(group)
            logger.info("=== 深度%d 组 [%d/%d]：%d 个节点开始升级 ===",
                        depth, g_idx + 1, total_groups, n)

            # ── JUMP：单播本组每个节点 ────────────────────────────────────
            self.state = UpgradeState.JUMP
            for idx, port_path in enumerate(group):
                self._check_deadline()
                logger.info("JUMP 深度%d [%d/%d]: %s → bootloader...",
                            depth, idx + 1, n, port_path)
                for attempt in range(jump_retries):
                    try:
                        frame = self._handler.jump_program(
                            port_path, jump_target, self._log_level)
                        location = (frame.data[0]
                                    if frame and getattr(frame, 'data', None)
                                    else None)
                        if location == jump_target:
                            logger.info("JUMP 深度%d [%d/%d]: %s 成功",
                                        depth, idx + 1, n, port_path)
                            break
                        if attempt < jump_retries - 1:
                            logger.warning(
                                "JUMP 深度%d [%d/%d]: %s 返回 location=%s，重试 %d/%d",
                                depth, idx + 1, n, port_path, location,
                                attempt + 1, jump_retries)
                            time.sleep(0.5)
                        else:
                            raise RuntimeError(
                                f"JUMP {port_path}: 期望={jump_target}，实际={location}，"
                                f"已重试{jump_retries}次")
                    except RuntimeError:
                        raise
                    except Exception as exc:
                        if attempt < jump_retries - 1:
                            logger.warning(
                                "JUMP 深度%d [%d/%d]: %s 异常(%s)，重试 %d/%d",
                                depth, idx + 1, n, port_path, exc,
                                attempt + 1, jump_retries)
                            time.sleep(0.5)
                        else:
                            raise RuntimeError(
                                f"JUMP {port_path} 失败: {exc}") from exc

            # ── SEND_INFO：单播本组每个节点触发擦除 ──────────────────────
            self.state = UpgradeState.SEND_INFO
            for idx, port_path in enumerate(group):
                self._check_deadline()
                logger.info("SEND_INFO 深度%d [%d/%d]: %s 开始擦除...",
                            depth, idx + 1, n, port_path)
                self._handler.send_upgrade_info(port_path, total_fw)
                logger.info("SEND_INFO 深度%d [%d/%d]: %s 擦除完成",
                            depth, idx + 1, n, port_path)

            # ── SEND_DATA：广播发送固件 ───────────────────────────────────
            self.state = UpgradeState.SEND_DATA
            offset = 0
            sn = 1
            prog_base = g_idx * 90 // total_groups
            prog_step = 90 // total_groups
            logger.info("SEND_DATA(broadcast) 深度%d: 开始广播固件，共 %d 字节...",
                        depth, total_fw)
            while offset < total_fw:
                self._check_deadline()
                chunk = self._firmware[offset: offset + UpgradeCmdHandler.CHUNK_SIZE]
                self._handler.send_upgrade_data(broadcast_path, sn, chunk)
                offset += len(chunk)
                sn = (sn % 65535) + 1
                pct = prog_base + min(int(offset / total_fw * prog_step), prog_step)
                self._on_progress(pct)
                time.sleep(UpgradeCmdHandler.BROADCAST_INTERVAL)

            # ── VERIFY：单播本组每个节点跳回 APP 并验证 ──────────────────
            self.state = UpgradeState.VERIFY
            for idx, port_path in enumerate(group):
                self._check_deadline()
                logger.info("VERIFY 深度%d [%d/%d]: %s → APP...",
                            depth, idx + 1, n, port_path)
                self._handler.jump_program(port_path, verify_target, self._log_level)
                time.sleep(0.8)  # 等待 APP 启动就绪
                self._check_deadline()
                board_info = self._handler.get_board_info(port_path)
                logger.info("VERIFY 深度%d [%d/%d]: %s location=%d",
                            depth, idx + 1, n, port_path, board_info.location)
                if board_info.location != verify_target:
                    raise RuntimeError(
                        f"VERIFY {port_path}: 期望={verify_target}，"
                        f"实际={board_info.location}")
                logger.info("VERIFY 深度%d [%d/%d]: %s → APP 成功",
                            depth, idx + 1, n, port_path)

            logger.info("=== 深度%d 组 [%d/%d] 升级完成 ===",
                        depth, g_idx + 1, total_groups)

        # ── DONE ──────────────────────────────────────────────────────────
        self.state = UpgradeState.DONE
        self._on_progress(100)
        self._on_done()

    def _run(self) -> None:
        """后台线程主循环：执行升级，失败时根据 retry_count 决定是否自动重试。"""
        # 广播升级（多节点）需要更长超时；单播 120s
        timeout = 300.0 if self._unicast_paths is not None else self.GLOBAL_TIMEOUT
        self._upgrade_deadline = time.monotonic() + timeout

        while not self._stop_event.is_set():
            try:
                self._do_single_pass()
                return   # 成功退出
            except TimeoutError as e:
                stage_name = self.state.name
                if "GLOBAL_TIMEOUT" in str(e):
                    self.state = UpgradeState.ERROR
                    self._on_error("GLOBAL_TIMEOUT", -1, self.retry_count)
                    return
                # 普通超时，按重试逻辑处理
                self._handle_error(stage_name, -2)
            except RuntimeError as e:
                self._handle_error(self.state.name, -3)
            except Exception as e:
                logger.error(f"升级失败 [{self.state.name}]: {type(e).__name__}: {e}")
                self._handle_error(self.state.name, -4)

    def _handle_error(self, stage: str, code: int) -> None:
        self.state = UpgradeState.ERROR
        self.retry_count += 1
        self._on_error(stage, code, self.retry_count)

        if self.retry_count < self.max_retries:
            # 自动重试：重置状态，继续外层 while 循环
            self.state = UpgradeState.IDLE
        else:
            # 超过最大重试次数，停止等待用户操作
            self._stop_event.set()
