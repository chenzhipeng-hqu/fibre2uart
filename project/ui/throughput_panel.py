# -*- coding: utf-8 -*-
"""
ui/throughput_panel.py — 通信性能测试面板

ThroughputTestPanel(QWidget) 特性：
  - 选择测试终端口与回环串口
  - 配置测试模式（固定包 / 随机包）
  - 实时监控：丢帧率、RTT、吞吐量、队列积压、系统资源
  - 延迟分布直方图可视化
  - 导出 Markdown 测试报告

自测（__main__）：弹出窗口，注入 Mock Client，模拟测试流程。
"""
from __future__ import annotations

import gc
import logging
import os
import struct
import sys
import queue
import threading
import time
from datetime import datetime
from typing import Optional

import psutil
from PySide6.QtCore import Qt, QTimer, Signal, Slot
from PySide6.QtGui import QKeySequence, QShortcut

from config import get_config
from frame.models import SOF_RX
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QHBoxLayout, QLabel, QLineEdit,
    QMessageBox, QProgressBar, QPushButton, QSlider, QSpinBox, QTextEdit,
    QVBoxLayout, QWidget,
)

logger = logging.getLogger(__name__)


class _AutoRefreshComboBox(QComboBox):
    """展开下拉列表前自动调用 refresh_func 重新填充选项。"""
    def __init__(self, refresh_func, parent=None):
        super().__init__(parent)
        self._refresh_func = refresh_func

    def showPopup(self):
        self._refresh_func()
        super().showPopup()


class ThroughputTestWorker(threading.Thread):
    """后台测试线程，执行实际通信测试并收集指标。"""

    def __init__(self, client, target_logical_addr: int = None, mode: str = "大包持续",
                 packet_size: int = 240, interval_ms: int = 20, loopback_port: str = None,
                 loopback_baudrate: int = 1000000, duration: int = 60,
                 max_count: int = 0, target_port_path=None, stop_on_error: bool = False,
                 wait_for_ack: bool = False, return_timeout: float = 1.0,
                 loopback_serial_factory=None):
        super().__init__(daemon=True, name="ThroughputTestWorker")
        self._client = client
        self._logical_addr = target_logical_addr
        self._port_path = target_port_path
        self._stop_on_error = stop_on_error
        self._wait_for_ack = wait_for_ack  # 等待回环确认后再发下一包
        self._mode = mode
        self._packet_size = packet_size
        self._interval_ms = max(interval_ms, 1)  # 发送间隔（毫秒），最小 1ms
        self._loopback_port = loopback_port
        self._loopback_baudrate = loopback_baudrate
        self._duration = duration  # 测试持续时间（秒）
        self._max_count = max(max_count, 0)  # 发送次数上限，0=无限
        self._stop_flag = threading.Event()
        self._pause_flag = threading.Event()  # 暂停标志
        self._ack_event = threading.Event()   # 等待回环确认（wait_for_ack 模式）
        self._start_time = None
        self._pause_time = None  # 暂停时刻
        self._total_pause_duration = 0.0  # 累计暂停时长
        self._loopback_serial = None  # 回环串口对象
        self._reader = None  # 回环读取线程引用（用于退出前 join，避免 close/read 竞态）
        self._pending_queue: queue.Queue = queue.Queue()  # 待验证已发包队列
        self._return_timeout = max(return_timeout, 0.01)   # 反向回程每包超时（秒）
        self._loopback_serial_factory = loopback_serial_factory  # 可注入的回环串口工厂（测试用）
        self._pending_returns: dict = {}   # 反向待回程：seq -> {'payload':bytes,'deadline':float}
        self._return_lock = threading.Lock()  # 保护 _pending_returns 与反向指标
        self._original_on_frame = None      # hook 安装前保存的原 on_frame 回调
        self._return_reaper = None          # 反向超时清扫线程引用
        
        self._metrics = {
            'tx_count': 0,           # 尝试发送的帧数（含发送失败）
            'tx_ok_count': 0,        # send_cmd 未抛异常的帧数
            'rx_count': 0,
            'send_err_count': 0,     # send_cmd 调用异常次数
            'loopback_miss_count': 0,  # 回环串口超时未收到次数
            'data_corrupt_count': 0,   # 全包内容比对失败次数
            'latencies': [],
            'tx_bytes': 0,
            'rx_bytes': 0,
            'queue_depth_samples': [],
            'pending_samples': [],
            # 吞吐量区间采样：每条记录 (timestamp, tx_bytes_snapshot, rx_bytes_snapshot)
            'throughput_samples': [],
            # 回环模式：'normal' 正常等待 / 'fast_drain' 连续超时后的快速放行
            'loopback_mode': 'normal',
            'loopback_mode_msg': '',   # 模式切换提示（供监控区显示）
            # 反向（设备→PC）回程指标 —— 见 ADR-0001
            'rx_count_return': 0,             # 反向 A 端收到回程帧数
            'rx_bytes_return': 0,
            'data_corrupt_count_return': 0,   # 反向内容比对失败
            'loopback_miss_count_return': 0,  # 反向超时未收到
        }
        self._last_sample_time: float = 0.0  # 上次采样时刻
        # _metrics 跨发送线程/读者线程/UI 线程共享。每个键只有一个写线程
        # （发送线程写 tx_*、读者线程写 rx_*），故单键计数无需加锁；此锁只用于
        # 保证多键复合更新（loopback 模式+消息、吞吐量采样）与 get_metrics() 快照的一致性。
        self._metrics_lock = threading.Lock()

    def run(self):
        """执行测试循环。"""
        self._start_time = time.time()
        seq = 1
        # 注意：发送间隔不缓存为局部变量，每次循环动态读取 _interval_ms，支持暂停后更新
        
        # 打开回环串口
        if self._loopback_port:
            try:
                import serial
                if self._loopback_serial_factory is not None:
                    # 测试注入的工厂
                    self._loopback_serial = self._loopback_serial_factory(
                        self._loopback_port, self._loopback_baudrate, timeout=0.5)
                else:
                    self._loopback_serial = serial.Serial(
                        self._loopback_port, self._loopback_baudrate, timeout=0.5
                    )  # timeout=0.5 让 read(n) 最多等 0.5s，足够应对 USB 断包
                logger.info(f"已打开回环串口: {self._loopback_port} @ {self._loopback_baudrate}bps")
                self._reader = threading.Thread(target=self._loopback_reader, daemon=True, name="LoopbackReader")
                self._reader.start()
            except Exception as e:
                logger.error(f"打开回环串口失败: {e}")
                self._loopback_serial = None
        
        logger.info(f"开始通信测试: mode={self._mode}, size={self._packet_size}, "
                    f"interval={self._interval_ms}ms, duration={self._duration}s")
        
        try:
            # 安装 hook + 启动 reaper 在 try 内：确保 finally 总能卸载 hook / 回收 reaper（见 ADR-0001）。
            # 放在 try 外的话，安装与 try 之间若抛异常，finally 不执行 → hook 泄漏在共享 client._parser 上。
            self._install_return_hook()
            self._return_reaper = threading.Thread(target=self._return_reaper_loop,
                                                   daemon=True, name="ReturnReaper")
            self._return_reaper.start()
            while not self._stop_flag.is_set():
                # 检查是否暂停
                if self._pause_flag.is_set():
                    if self._pause_time is None:
                        self._pause_time = time.time()
                        logger.info("测试已暂停")
                    time.sleep(0.1)  # 暂停时休眠，减少CPU占用
                    continue

                # 从暂停恢复
                if self._pause_time is not None:
                    self._total_pause_duration += time.time() - self._pause_time
                    self._pause_time = None
                    logger.info("测试已继续")

                # 检查是否超时（扣除暂停时间）；无限次模式不检查时长
                elapsed = time.time() - self._start_time - self._total_pause_duration
                if self._max_count == 0:
                    pass  # 无限发送，不检查时长
                elif elapsed > self._duration:
                    logger.info("测试时长到达，自动停止")
                    break

                # 根据模式生成测试数据
                payload = self._generate_payload(seq)

                # 采样队列深度（如果 client 支持）
                self._sample_queue_depth()
                # 每秒采样一次吞吐量
                self._sample_throughput()

                # 发送并计时
                t0 = time.perf_counter()
                self._metrics['tx_count'] += 1  # 先计入尝试发送（含失败）
                try:
                    # 根据模式选择发送方式
                    if self._port_path:
                        # 路径模式：直接通过 session 发送
                        self._client._session.send_request(self._port_path, 0x01, payload)
                    else:
                        # 逻辑地址模式：通过 send_cmd 发送
                        self._client.send_cmd(self._logical_addr, 0x01, payload)
                    self._metrics['tx_ok_count'] += 1
                    self._metrics['tx_bytes'] += len(payload)

                    if (len(payload) > 30):
                        preview = payload[:15].hex(' ').upper() + ' ... ' + payload[-5:].hex(' ').upper()
                        logger.debug(f"[TX] seq={seq:3d} len={len(payload):3d}B  {preview}")
                    else:
                        logger.debug(f"[TX] seq={seq:3d} len={len(payload):3d}B  {payload.hex(' ').upper()}")

                    # 如果有回环串口，读取返回数据验证
                    if self._loopback_serial:
                        if self._wait_for_ack:
                            # 关键：先 clear 再 put。读者的 set() 只会在本包入队之后才发生，
                            # 因此 clear 放在 put 之前绝不会抹掉本帧的确认信号，避免
                            # "读者处理太快 → set() 被随后的 clear() 清除 → wait() 虚假超时"的竞态。
                            self._ack_event.clear()
                        # 投入待验证队列，由独立读取线程处理，不阻塞发送循环
                        self._pending_queue.put((seq, t0, payload))
                        self._register_pending_return(seq, payload)
                        if self._wait_for_ack:
                            # 等待回环读取线程确认收到本包（或超时/停止）
                            if self._stop_flag.is_set():
                                break
                            if not self._ack_event.wait(timeout=1.0):
                                # 1s 内未收到任何 ack（读者线程卡死/崩溃）→ 立即停止
                                logger.warning("等待回环确认 1s 未收到 ack，读者可能已停止，立即停止测试")
                                self._stop_flag.set()
                                break
                    # else: 无回环串口，无法验证接收，不计入 rx_count

                except Exception as e:
                    logger.warning(f"发送失败 seq={seq}: {e}")
                    self._metrics['send_err_count'] += 1
                    if self._stop_on_error:
                        logger.info("出错自动停止已触发")
                        self._stop_flag.set()
                        break

                # 检查是否达到发送次数上限（发完后退出发送循环，等待接收完成）
                if self._max_count > 0 and self._metrics['tx_count'] >= self._max_count:
                    logger.info(f"已发送 {self._metrics['tx_count']} 包，等待接收完成...")
                    break

                seq = (seq % 255) + 1

                # 根据速率控制发送间隔（动态读取，支持暂停后更新间隔）
                time.sleep(self._interval_ms / 1000.0)
        finally:
            # 等待回环读取线程处理完剩余数据（自然结束或手动停止均等待）
            if self._loopback_serial:
                if not self._stop_flag.is_set() and self._max_count > 0:
                    # 自然结束且有次数限制：等待 rx_count 达到 max_count，或超过 duration 超时
                    logger.info(f"发送完成，等待接收完成（上限{self._max_count}包，超时{self._duration}s）...")
                    rx_deadline = self._start_time + self._duration
                    while (self._metrics['rx_count'] < self._max_count
                           and time.time() < rx_deadline
                           and not self._stop_flag.is_set()):
                        time.sleep(0.05)
                    if self._metrics['rx_count'] >= self._max_count:
                        logger.info("所有包已接收，停止测试")
                    else:
                        logger.info(f"接收超时，已收 {self._metrics['rx_count']}/{self._max_count} 包")
                else:
                    # 手动停止或无限模式：给读者最多 2s 处理完已入队的数据。
                    # 用硬性上限，避免读者已提前退出（宽 / stop_on_error 触发）时，
                    # 因队列无人排空而陷入无限等待。
                    logger.info("等待剩余回环数据（最多2s）...")
                    drain_deadline = time.time() + 2.0
                    while time.time() < drain_deadline and not self._pending_queue.empty():
                        time.sleep(0.05)
                    logger.info("回环数据等待完成")
            # 通知回环读取线程退出
            self._stop_flag.set()
            # 等待读取线程真正退出后再关串口，避免 close() 与 read() 竞态
            if self._reader is not None:
                self._reader.join(timeout=2.0)
            # 停止反向超时清扫线程 + 卸载回程捕获 hook（见 ADR-0001）
            if self._return_reaper is not None:
                self._return_reaper.join(timeout=2.0)
                self._return_reaper = None
            self._uninstall_return_hook()
            # 关闭回环串口
            if self._loopback_serial:
                try:
                    self._loopback_serial.close()
                    logger.info("已关闭回环串口")
                except Exception:
                    pass
        
        logger.info(f"测试完成: 发送{self._metrics['tx_count']}包, "
                    f"接收{self._metrics['rx_count']}包")

    FRAME_MAGIC = b'\xAA\x55'
    FAST_DRAIN_THRESHOLD = 100  # 连续未回复帧数达此值后，读者切"快速放行"
    FAST_DRAIN_PROBE_MS = 50    # 快速放行每帧的有界探测窗口(ms)：仅在数据流入时多读以组装分包帧

    def _generate_payload(self, seq: int) -> bytes:
        """根据测试模式生成测试数据。
        
        帧格式：AA 55 + seq(2B大端) + body
        帧头固定4字节，回环读取线程用帧头做同步对齐。
        """
        size = self._packet_size
        
        if self._mode == "混合包":
            import random
            size = random.randint(4, 240)  # 最小4字节（仅帧头）
        
        # 帧头：AA 55 + seq(2B)
        header = self.FRAME_MAGIC + struct.pack('>H', seq)
        body_size = max(size - 4, 0)
        
        if body_size == 0:
            return header
        elif body_size <= 6:
            return header + bytes([seq % 256] * body_size)
        else:
            return header + os.urandom(body_size)

    def _sample_queue_depth(self):
        """采样队列深度（如果 client 暴露了 transport）。"""
        try:
            if hasattr(self._client, '_transport') and self._client._transport:
                transport = self._client._transport
                if hasattr(transport, '_tx_queue'):
                    qsize = transport._tx_queue.qsize()
                    self._metrics['queue_depth_samples'].append(qsize)
            
            if hasattr(self._client, '_session') and self._client._session:
                session = self._client._session
                if hasattr(session, '_pending'):
                    pending_count = len(session._pending)
                    self._metrics['pending_samples'].append(pending_count)
        except Exception:
            pass

    def _sample_throughput(self) -> None:
        """每秒采样一次收发字节数，用于区间吞吐量计算。"""
        now = time.monotonic()
        if now - self._last_sample_time >= 1.0:
            with self._metrics_lock:
                self._metrics['throughput_samples'].append((
                    now,
                    self._metrics['tx_bytes'],
                    self._metrics['rx_bytes'],
                ))
            self._last_sample_time = now

    def _try_match(self, buf: bytearray, seq: int, expected_len: int) -> Optional[bytes]:
        """在缓冲区中扫描帧头 AA 55 对齐，并用 seq 精确校验。

        匹配成功则从 buf 消费掉该帧并返回帧数据；否则返回 None
        （buf 中无魔数时清空，有伪帧头时跳过 2 字节继续扫描）。
        """
        while len(buf) >= expected_len:
            idx = buf.find(self.FRAME_MAGIC)
            if idx == -1:
                buf.clear()  # 无魔数，丢弃全部垃圾数据
                return None
            if idx > 0:
                logger.debug(f"[SYNC] seq={seq:3d} 跳过 {idx} 字节垃圾数据")
                del buf[:idx]  # 对齐到帧头
            if len(buf) < expected_len:
                return None  # 对齐后数据不足，等下次读更多
            # 二次校验：帧头 seq 必须精确匹配期望值
            candidate_seq = struct.unpack('>H', buf[2:4])[0]
            if candidate_seq != seq:
                # 伪帧头，跳过这2字节魔数继续扫描
                logger.debug(f"[SYNC] seq={seq:3d} 伪帧头 rx_seq={candidate_seq}, 跳过")
                del buf[:2]
                continue
            frame_data = bytes(buf[:expected_len])
            del buf[:expected_len]
            return frame_data
        return None

    def _switch_loopback_mode(self, mode: str, miss_count: int) -> None:
        """切换回环模式（仅在实际变化时记录消息/日志，供监控区显示）。"""
        with self._metrics_lock:
            if self._metrics.get('loopback_mode', 'normal') == mode:
                return
            self._metrics['loopback_mode'] = mode
            if mode == 'fast_drain':
                msg = f"⚠ 连续 {miss_count} 帧无回复，已切快速放行"
            else:  # normal
                msg = "✅ 回环已恢复，切回正常等待"
            self._metrics['loopback_mode_msg'] = msg
        # 日志在锁外打，避免持锁期间做 I/O
        if mode == 'fast_drain':
            logger.warning(msg)
        else:
            logger.info(msg)

    def _loopback_reader(self) -> None:
        """独立读取线程：从回环串口接收数据并按帧头同步匹配，不阻塞发送循环。

        帧同步机制：扫描 AA 55 魔数对齐帧头，避免字节流错位导致连锁误判。
        """
        buf = bytearray()
        timeout_miss_count = 0

        while not self._stop_flag.is_set() or not self._pending_queue.empty():
            try:
                seq, t0, payload = self._pending_queue.get(timeout=0.2)
            except queue.Empty:
                continue

            expected_len = len(payload)
            frame_data = None

            if timeout_miss_count >= self.FAST_DRAIN_THRESHOLD:
                # 快速放行：连续超时已超阈值，不再每帧死等 1s，改用短时有界探测。
                # 只在数据流入时多读几次以组装 USB 分包到达的帧，可靠探测回环恢复；
                # 命中即清零计数，下一帧自动切回正常等待。无数据流入时立即判 miss 放行，
                # 既不拖慢发送方、也避免 _pending_queue 在回环彻底失效时积压。
                self._switch_loopback_mode('fast_drain', timeout_miss_count)
                fd_deadline = time.perf_counter() + self.FAST_DRAIN_PROBE_MS / 1000.0
                while time.perf_counter() < fd_deadline and not self._stop_flag.is_set():
                    got = False
                    try:
                        if self._loopback_serial and self._loopback_serial.is_open:
                            n_avail = self._loopback_serial.in_waiting
                            if n_avail > 0:
                                chunk = self._loopback_serial.read(min(n_avail, expected_len))
                                if chunk:
                                    buf.extend(chunk)
                                    got = True
                    except Exception as e:
                        logger.debug(f"[ERR] seq={seq:3d} 快速放行读取异常: {e}")
                        break
                    frame_data = self._try_match(buf, seq, expected_len)
                    if frame_data:
                        break
                    if not got:
                        break  # 本轮无新数据：回环仍未恢复，立即判 miss 放行
                    time.sleep(0.003)  # 短暂休眠，避免 CPU 空转
            else:
                # 正常等待：每帧最多等 1 秒
                self._switch_loopback_mode('normal', timeout_miss_count)
                deadline = time.perf_counter() + 1.0  # 每帧最多等 1 秒
                while time.perf_counter() < deadline and not self._stop_flag.is_set():
                    # 从串口补充数据到缓冲区
                    try:
                        if self._loopback_serial and self._loopback_serial.is_open:
                            chunk = self._loopback_serial.read(expected_len)
                            if chunk:
                                buf.extend(chunk)
                    except Exception as e:
                        logger.debug(f"[ERR] seq={seq:3d} 串口读取异常: {e}")
                        break
                    frame_data = self._try_match(buf, seq, expected_len)
                    if frame_data:
                        break

            if frame_data:
                # 反向触发：把 B 收到的字节原样回写 B.TX（设备反向转发回 A）—— ADR-0001
                if self._loopback_serial is not None:
                    try:
                        self._loopback_serial.write(frame_data)
                    except Exception as e:
                        logger.debug(f"[ECHO] seq={seq:3d} B.TX 回写异常: {e}")
                # 全包内容比对：回环数据应与发送内容完全一致
                rtt = time.perf_counter() - t0
                self._metrics['latencies'].append(rtt)
                self._metrics['rx_count'] += 1
                self._metrics['rx_bytes'] += len(frame_data)
                if frame_data != payload:
                    # _try_match 已保证 seq 精确匹配，故内容不一致即数据损坏
                    self._metrics['data_corrupt_count'] += 1
                    logger.debug(f"[CORRUPT] seq={seq:3d} 内容比对失败 rtt={rtt*1000:.1f}ms")
                else:
                    if len(frame_data) > 30:
                        preview = frame_data[:15].hex(' ').upper() + ' ... ' + frame_data[-5:].hex(' ').upper()
                        logger.debug(f"[RX] seq={seq:3d} len={len(frame_data):3d}B  rtt={rtt*1000:.1f}ms  {preview}")
                    else:
                        logger.debug(f"[RX] seq={seq:3d} len={len(frame_data):3d}B  rtt={rtt*1000:.1f}ms  {frame_data.hex(' ').upper()}")
                timeout_miss_count = 0
            else:
                self._metrics['loopback_miss_count'] += 1
                logger.debug(f"[MISS] seq={seq:3d} 超时未收到回环")
                timeout_miss_count += 1
                # 宽：等待回环确认模式下任一帧未确认即停；出错自动停止同理。
                # 两者均关闭时（blast 模式）继续发，连续超时达阈值后下一帧进入快速放行。
                if self._wait_for_ack or self._stop_on_error:
                    reason = ("等待回环确认：本帧 1s 内未收到回复，立即停止"
                              if self._wait_for_ack else "出错自动停止已触发")
                    logger.info(reason)
                    self._stop_flag.set()
                    self._ack_event.set()  # 解除发送循环等待
                    break
            # 通知发送循环本包已处理完毕（成功或超时）
            self._ack_event.set()

    # ── 反向链路（设备→PC）捕获与校验 —— ADR-0001 ────────────────
    # worker 包装 FrameParser.on_frame，拦截 0xAB/cmd<0x10 的回程透传帧，
    # 用 AA55+seq 匹配本包、内容比对；不破坏虚拟串口路由（原回调照常调用）。

    def _register_pending_return(self, seq: int, payload: bytes,
                                 deadline: Optional[float] = None) -> None:
        """登记一个待回程包，供反向匹配查表。

        deadline 为 None 时取 now + _return_timeout；测试可传显式时刻（0.0 = 已过期）。
        """
        dl = deadline if deadline is not None else time.time() + self._return_timeout
        with self._return_lock:
            self._pending_returns[seq] = {'payload': payload, 'deadline': dl}

    @staticmethod
    def _extract_seq(data: bytes) -> Optional[int]:
        """从回程帧 data 中解出嵌入的 seq（AA55 + seq(2B 大端)）；不匹配返回 None。"""
        if len(data) >= 4 and data[:2] == b'\xAA\x55':
            return struct.unpack('>H', data[2:4])[0]
        return None

    def _on_return_frame(self, frame) -> None:
        """on_frame hook 回调：处理 0xAB/cmd<0x10 的反向回程帧。

        在 transport 接收线程被调用。_pending_returns 的取用加锁；反向指标每个键
        只有本线程一个写线程（与 tx_*/rx_* 同构），单键自增无需锁（见 _metrics 注释）。
        """
        # 双重过滤（hook 已过滤一次）：仅 MCU→PC 透传帧
        if frame.sof != SOF_RX or frame.cmd >= 0x10:
            return
        seq = self._extract_seq(frame.data)
        if seq is None:
            return
        with self._return_lock:
            entry = self._pending_returns.pop(seq, None)  # 取用必须加锁（reaper 也碰该表）
        if entry is None:
            return  # 未知 seq（非本测试包 / 重复 / 已超时），忽略
        payload = entry['payload']
        # 反向指标：单一写线程（transport 接收线程），无需锁，与既有 tx_*/rx_* 一致
        self._metrics['rx_count_return'] += 1
        self._metrics['rx_bytes_return'] += len(frame.data)
        if frame.data != payload:
            self._metrics['data_corrupt_count_return'] += 1
            logger.debug(f"[REVERSE-CORRUPT] seq={seq:3d} 反向内容比对失败")
        else:
            logger.debug(f"[REVERSE-OK] seq={seq:3d} len={len(frame.data):3d}B")

    def _install_return_hook(self) -> None:
        """包装 client._parser.on_frame：拦截反向回程帧，再照常调原回调。"""
        parser = getattr(self._client, '_parser', None)
        if parser is None:
            self._original_on_frame = None
            return
        # 安装时把原回调捕获到局部变量 original，闭包引用它（而非 self._original_on_frame）。
        # 这样 _uninstall_return_hook 把 self._original_on_frame 置 None 时不会影响在飞 hook，
        # 消除 check-then-call 的 TOCTOU（不会出现「检查通过→被置 None→调用 None(frame)」）。
        original = parser.on_frame
        self._original_on_frame = original

        def _hook(frame):
            # 反向回程透传帧 → 反向匹配；original 为安装时捕获，始终调用（虚拟串口路由不受影响）
            if frame.sof == SOF_RX and frame.cmd < 0x10:
                self._on_return_frame(frame)
            if original is not None:
                original(frame)

        parser.on_frame = _hook

    def _uninstall_return_hook(self) -> None:
        """还原 on_frame 原回调。"""
        parser = getattr(self._client, '_parser', None)
        if parser is None or self._original_on_frame is None:
            return
        parser.on_frame = self._original_on_frame
        self._original_on_frame = None

    def _reap_expired_returns(self) -> None:
        """清扫已过 deadline 的待回程项，计反向 miss。"""
        now = time.time()
        with self._return_lock:
            expired = [s for s, e in self._pending_returns.items() if e['deadline'] <= now]
            for s in expired:
                del self._pending_returns[s]
        if expired:
            # 单一写线程（reaper），无需锁
            self._metrics['loopback_miss_count_return'] += len(expired)
            logger.debug(f"[REVERSE-MISS] 反向超时未回 {len(expired)} 包: {expired}")

    def _return_reaper_loop(self) -> None:
        """周期清扫反向超时项，直到停止且无待回程。"""
        while not self._stop_flag.is_set() or self._pending_returns:
            self._reap_expired_returns()
            time.sleep(0.1)

    def stop(self):
        """停止测试。"""
        self._stop_flag.set()
        self._ack_event.set()  # 解除 wait_for_ack 等待，让发送循环尽快退出
    
    def pause(self) -> None:
        """暂停测试。"""
        self._pause_flag.set()
    
    def resume(self) -> None:
        """继续测试。"""
        self._pause_flag.clear()

    def get_metrics(self) -> dict:
        """获取当前指标快照。"""
        with self._metrics_lock:
            return self._metrics.copy()

    def get_elapsed(self) -> float:
        """获取已运行时间（秒）。"""
        if self._start_time:
            return time.time() - self._start_time
        return 0.0


class ThroughputTestPanel(QWidget):
    """通信性能测试面板。"""

    _metrics_signal: Signal = Signal(dict)  # 后台线程 → 主线程刷新
    _dev_found_signal: Signal = Signal(object)  # Device（主线程更新下拉）
    _dev_offline_signal: Signal = Signal(bytes)  # uid（主线程移除下拉）

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._client = None
        self._worker: Optional[ThroughputTestWorker] = None
        self._refresh_timer = QTimer(self)
        self._refresh_timer.timeout.connect(self._refresh_metrics)
        self._metrics_signal.connect(self._update_ui)
        self._is_running = False  # 测试是否正在运行
        self._is_paused = False  # 测试是否已暂停
        
        # 已加入下拉的终端口 logicAddr 集合（去重）
        self._known_addrs: set = set()
        # uid → logicAddr（用于离线时定位下拉项）
        self._uid_to_addr: dict = {}
        # 停止测试后保存最终指标快照，供导出报告使用
        self._last_metrics: dict = {}
        # 停止测试后保存 worker 参数（包大小/间隔），避免报告读取 spinbox 时值已被修改
        self._last_worker_params: dict = {}
        
        self._build_ui()
        self._load_config()
        
        # 连接设备发现/离线信号
        self._dev_found_signal.connect(self._on_dev_found)
        self._dev_offline_signal.connect(self._on_dev_offline)

    def _load_config(self) -> None:
        """从 config.ini 恢复上次的配置。"""
        cfg = get_config()
        # 目标模式
        mode = cfg.throughput_target_mode
        idx = self._combo_target_mode.findText(mode)
        if idx >= 0:
            self._combo_target_mode.setCurrentIndex(idx)
        # 测试路径
        if cfg.throughput_port_path:
            self._edit_path.setText(cfg.throughput_port_path)
        # 回环串口
        port = cfg.throughput_loopback_port
        if port:
            idx = self._combo_loopback.findText(port)
            if idx >= 0:
                self._combo_loopback.setCurrentIndex(idx)
            else:
                # 串口不在列表中时直接写入文本（editable 模式）
                self._combo_loopback.setEditText(port)
        # 回环波特率
        baud = str(cfg.throughput_loopback_baudrate)
        idx = self._combo_baudrate.findText(baud)
        if idx >= 0:
            self._combo_baudrate.setCurrentIndex(idx)
        # 终端口地址（下拉还没有条目时记录待恢复值，留到设备发现后匹配）
        self._pending_terminal_addr = cfg.throughput_terminal_addr
        # 测试模式（固定包/混合包）
        test_mode = cfg.throughput_mode
        idx = self._combo_mode.findText(test_mode)
        if idx >= 0:
            self._combo_mode.setCurrentIndex(idx)
        # 包大小
        self._spin_size.setValue(cfg.throughput_packet_size)
        # 发送间隔
        self._spin_rate.setValue(cfg.throughput_interval_ms)
        # 等待回环确认
        self._chk_wait_ack.setChecked(cfg.throughput_wait_for_ack)
        # 出错自动停止
        self._chk_stop_on_err.setChecked(cfg.throughput_stop_on_error)
        # 发送次数
        self._spin_count.setValue(cfg.throughput_max_count)

    def _save_config(self) -> None:
        """将当前配置写入 config.ini（内容相同则跳过）。"""
        target_mode = self._combo_target_mode.currentText()
        terminal_addr = self._combo_terminal.currentData() or 0
        port_path = self._edit_path.text().strip() if target_mode == "测试路径" else ""
        get_config().set_throughput_config(
            target_mode=target_mode,
            terminal_addr=terminal_addr,
            port_path=port_path,
            loopback_port=self._combo_loopback.currentText(),
            loopback_baudrate=self._combo_baudrate.currentData() or 1000000,
            mode=self._combo_mode.currentText(),
            packet_size=self._spin_size.value(),
            interval_ms=self._spin_rate.value(),
            wait_for_ack=self._chk_wait_ack.isChecked(),
            stop_on_error=self._chk_stop_on_err.isChecked(),
            max_count=self._spin_count.value(),
        )

    def set_client(self, client) -> None:
        """设置 FibreNetworkClient 实例。"""
        self._client = client

    def handle_device_found(self, event) -> None:
        """订阅 DeviceFoundEvent 后由 EventBus 调用。"""
        self._dev_found_signal.emit(event.device)
    
    def handle_device_offline(self, event) -> None:
        """订阅 DeviceOfflineEvent 后由 EventBus 调用。"""
        self._dev_offline_signal.emit(event.uid)

    def _build_ui(self):
        """构建 UI 布局。"""
        layout = QVBoxLayout(self)
        
        # ── 测试配置区 ─────────────────────────────
        hConfig = QHBoxLayout()

        # 测试目标模式选择
        # hConfig.addWidget(QLabel("测试目标:"))
        self._combo_target_mode = QComboBox()
        self._combo_target_mode.addItems(["测试终端口", "测试路径"])
        self._combo_target_mode.setMaximumWidth(100)
        self._combo_target_mode.currentTextChanged.connect(self._on_target_mode_changed)
        hConfig.addWidget(self._combo_target_mode)

        # 测试终端口下拉框
        self._combo_terminal = QComboBox()
        self._combo_terminal.setMinimumWidth(150)
        hConfig.addWidget(self._combo_terminal)

        # 测试路径输入框
        self._edit_path = QLineEdit()
        self._edit_path.setPlaceholderText("1 2 3")
        self._edit_path.setMinimumWidth(150)
        self._edit_path.setVisible(False)
        hConfig.addWidget(self._edit_path)
        
        # 回环串口
        hConfig.addWidget(QLabel("  回环串口:"))
        self._combo_loopback = _AutoRefreshComboBox(self._populate_serial_ports)
        self._combo_loopback.setMinimumWidth(150)
        self._populate_serial_ports()
        hConfig.addWidget(self._combo_loopback)

        # 回环串口波特率
        hConfig.addWidget(QLabel("波特率:"))
        self._combo_baudrate = QComboBox()
        for baud in [9600, 19200, 38400, 57600, 115200, 230400, 460800, 700000, 921600, 1000000, 2000000]:
            self._combo_baudrate.addItem(str(baud), userData=baud)
        self._combo_baudrate.setCurrentIndex(8)  # 默认 1000000bps
        hConfig.addWidget(self._combo_baudrate)
        
        # 开始/停止按钮
        self._btn_start = QPushButton("开始测试 (F5)")
        self._btn_start.setMinimumWidth(100)
        self._btn_start.clicked.connect(self._on_start_stop_toggle)
        QShortcut(QKeySequence("F5"), self).activated.connect(self._on_start_stop_toggle)
        hConfig.addWidget(self._btn_start)
        
        # 暂停/继续按钮
        self._btn_pause = QPushButton("暂停测试")
        self._btn_pause.setMinimumWidth(100)
        self._btn_pause.setEnabled(False)
        self._btn_pause.clicked.connect(self._on_pause_resume_toggle)
        hConfig.addWidget(self._btn_pause)
        
        hConfig.addStretch()
        layout.addLayout(hConfig)
        
        # ── 测试模式配置 ───────────────────────────
        hMode = QHBoxLayout()
        # hMode.addWidget(QLabel("测试模式:"))
        self._combo_mode = QComboBox()
        self._combo_mode.addItems(["固定包", "混合包"])
        self._combo_mode.setCurrentIndex(0)  # 默认固定包
        hMode.addWidget(self._combo_mode)
        
        hMode.addWidget(QLabel("  包大小:"))
        self._spin_size = QSpinBox()
        self._spin_size.setRange(1, 240)
        self._spin_size.setValue(240)
        self._spin_size.setMinimumWidth(50)
        # self._spin_size.valueChanged.connect(self._on_size_changed)
        hMode.addWidget(self._spin_size)
        self._lbl_size = QLabel("bytes")
        self._lbl_size.setMinimumWidth(30)
        hMode.addWidget(self._lbl_size)

        hMode.addWidget(QLabel("  发送间隔:"))
        self._spin_rate = QSpinBox()
        self._spin_rate.setRange(1, 2000)
        self._spin_rate.setValue(10)
        self._spin_rate.setMinimumWidth(50)
        # self._spin_rate.valueChanged.connect(self._on_rate_changed)
        hMode.addWidget(self._spin_rate)
        self._lbl_rate = QLabel("ms")
        self._lbl_rate.setMinimumWidth(20)
        hMode.addWidget(self._lbl_rate)

        self._btn_burst = QPushButton("突发传输")
        self._btn_burst.setMinimumWidth(50)
        self._btn_burst.setToolTip("一次性无间隔连续发送 20 包")
        self._btn_burst.clicked.connect(self._on_burst)
        hMode.addWidget(self._btn_burst)

        self._chk_wait_ack = QCheckBox("等待回环确认")
        self._chk_wait_ack.setMinimumWidth(60)
        self._chk_wait_ack.setChecked(False)
        self._chk_wait_ack.setToolTip("开启后，每发送一包必须等待回环串口收到回复后再发下一包")
        hMode.addWidget(self._chk_wait_ack)

        self._chk_stop_on_err = QCheckBox("出错自动停止")
        self._chk_stop_on_err.setChecked(False)
        self._chk_stop_on_err.setMinimumWidth(80)

        hMode.addWidget(self._chk_stop_on_err)

        hMode.addWidget(QLabel("  发送次数:"))
        self._spin_count = QSpinBox()
        self._spin_count.setRange(0, 999999)
        self._spin_count.setValue(0)
        self._spin_count.setSpecialValueText("∞ 无限")
        self._spin_count.setMinimumWidth(60)
        hMode.addWidget(self._spin_count)

        self._btn_export = QPushButton("导出报告")
        self._btn_export.setMinimumWidth(50)
        self._btn_export.setEnabled(False)
        self._btn_export.clicked.connect(self._on_export)
        hMode.addWidget(self._btn_export)
        
        hMode.addStretch()
        layout.addLayout(hMode)
        
        # ── 实时监控区 ─────────────────────────────
        layout.addWidget(QLabel("实时监控"))
        
        self._monitor_text = QTextEdit()
        self._monitor_text.setReadOnly(True)
        self._monitor_text.setMaximumHeight(200)
        self._monitor_text.setStyleSheet("font-family: monospace; font-size: 10pt;")
        layout.addWidget(self._monitor_text)
        
        # ── 队列与进度条 ───────────────────────────
        hQueue = QHBoxLayout()
        # hQueue.addWidget(QLabel("待发送队列:"))
        # self._progress_tx_queue = QProgressBar()
        # self._progress_tx_queue.setRange(0, 50)
        # self._progress_tx_queue.setValue(0)
        # self._progress_tx_queue.setFormat("%v / 50")
        # self._progress_tx_queue.setMaximumWidth(150)
        # hQueue.addWidget(self._progress_tx_queue)
        
        # hQueue.addWidget(QLabel("  等待响应:"))
        # self._progress_pending = QProgressBar()
        # self._progress_pending.setRange(0, 999999)
        # self._progress_pending.setValue(0)
        # self._progress_pending.setFormat("%v / %v")
        # self._progress_pending.setMaximumWidth(150)
        # hQueue.addWidget(self._progress_pending)
        
        hQueue.addStretch()
        layout.addLayout(hQueue)
        
        # ── 操作按钮 ───────────────────────────────
        hBtn = QHBoxLayout()
        # self._btn_stop = QPushButton("停止测试")
        # self._btn_stop.setEnabled(False)
        # self._btn_stop.clicked.connect(self._on_stop)
        # hBtn.addWidget(self._btn_stop)
        
        # self._btn_export = QPushButton("导出报告")
        # self._btn_export.setEnabled(False)
        # self._btn_export.clicked.connect(self._on_export)
        # hBtn.addWidget(self._btn_export)
        
        # hBtn.addStretch()
        layout.addLayout(hBtn)
        
        layout.addStretch()

    def _populate_serial_ports(self):
        """扫描可用串口填充回环串口下拉框，保留当前选中项。"""
        current = self._combo_loopback.currentText()
        self._combo_loopback.clear()
        try:
            import serial.tools.list_ports
            ports = [p.device for p in serial.tools.list_ports.comports()]
        except Exception:
            ports = []

        if not ports:
            ports = ["/dev/ttyUSB0", "/dev/ttyUSB1", "COM1", "COM2"]

        for p in ports:
            self._combo_loopback.addItem(p)

        # 刷新后恢复之前的选中项
        idx = self._combo_loopback.findText(current)
        if idx >= 0:
            self._combo_loopback.setCurrentIndex(idx)

    @Slot(object)
    def _on_dev_found(self, device) -> None:
        """节点发现后追加到终端口下拉（仅终端口、去重）。"""
        addr = device.logical_addr
        if addr == 0 or addr in self._known_addrs or not device.is_terminal:
            return
        self._known_addrs.add(addr)
        self._uid_to_addr[device.uid] = addr
        self._combo_terminal.addItem(f"终端口 (0x{addr:04X})", userData=addr)
        # 如果匹配上次保存的终端口地址，自动选中
        if self._pending_terminal_addr and addr == self._pending_terminal_addr:
            self._combo_terminal.setCurrentIndex(self._combo_terminal.count() - 1)
            self._pending_terminal_addr = 0
    
    @Slot(bytes)
    def _on_dev_offline(self, uid: bytes) -> None:
        """节点离线后从下拉中移除。"""
        addr = self._uid_to_addr.pop(uid, None)
        if addr is None:
            return
        self._known_addrs.discard(addr)
        for i in range(self._combo_terminal.count()):
            if self._combo_terminal.itemData(i) == addr:
                self._combo_terminal.removeItem(i)
                break

    @Slot(str)
    def _on_target_mode_changed(self, mode: str) -> None:
        """目标模式切换时显示/隐藏对应的输入控件。"""
        if mode == "测试终端口":
            self._combo_terminal.setVisible(True)
            self._edit_path.setVisible(False)
        else:  # 测试路径
            self._combo_terminal.setVisible(False)
            self._edit_path.setVisible(True)

    @Slot()
    def _on_start_stop_toggle(self):
        """开始/停止测试切换。"""
        if self._is_running:
            self._on_stop()
        else:
            self._on_start()

    @Slot()
    def _on_pause_resume_toggle(self):
        """暂停/继续测试切换。"""
        if self._is_paused:
            self._on_resume()
        else:
            self._on_pause()

    @Slot()
    def _on_burst(self):
        """突发传输：一次性无间隔连续发送 20 包。"""
        if not self._client:
            QMessageBox.warning(self, "提示", "请先连接设备")
            return

        target_mode = self._combo_target_mode.currentText()
        if target_mode == "测试终端口":
            logical_addr = self._combo_terminal.currentData()
            if not logical_addr:
                QMessageBox.warning(self, "提示", "请选择测试终端口")
                return
            port_path = None
        else:
            path_text = self._edit_path.text().strip()
            if not path_text:
                QMessageBox.warning(self, "提示", "请输入测试路径")
                return
            try:
                from protocol.models import PortPath
                parts = path_text.replace(',', ' ').split()
                port_path = PortPath([int(p) for p in parts if p])
                logical_addr = None
            except ValueError as e:
                QMessageBox.warning(self, "提示", f"路径格式错误: {e}")
                return

        packet_size = max(self._spin_size.value(), 4)
        client = self._client

        def _burst_task():
            results = []
            for i in range(20):
                seq = i + 1
                header = ThroughputTestWorker.FRAME_MAGIC + struct.pack('>H', seq)
                body_size = max(packet_size - 4, 0)
                if body_size == 0:
                    payload = header
                elif body_size <= 6:
                    payload = header + bytes([seq % 256] * body_size)
                else:
                    payload = header + os.urandom(body_size)
                t0 = time.perf_counter()
                try:
                    if port_path:
                        client._session.send_request(port_path, 0x01, payload)
                    else:
                        client.send_cmd(logical_addr, 0x01, payload)
                    rtt_ms = (time.perf_counter() - t0) * 1000
                    results.append(f"  [{i+1:2d}] ✅ {len(payload)}B  RTT={rtt_ms:.1f}ms")
                except Exception as e:
                    results.append(f"  [{i+1:2d}] ❌ 发送失败: {e}")
            summary = "\n".join(results)
            logger.info(f"突发传输完成:\n{summary}")

        threading.Thread(target=_burst_task, daemon=True, name="BurstSend").start()
        logger.info("突发传输已触发（20包，无间隔）")

    @Slot()
    def _on_start(self):
        """开始测试。"""
        if not self._client:
            QMessageBox.warning(self, "提示", "请先连接设备")
            return

        # 根据目标模式获取地址
        target_mode = self._combo_target_mode.currentText()
        if target_mode == "测试终端口":
            if self._combo_terminal.count() == 0:
                QMessageBox.warning(self, "提示", "未发现终端口，请先扫描设备")
                return
            logical_addr = self._combo_terminal.currentData()
            if not logical_addr:
                QMessageBox.warning(self, "提示", "请选择测试终端口")
                return
        else:  # 测试路径
            path_text = self._edit_path.text().strip()
            if not path_text:
                QMessageBox.warning(self, "提示", "请输入测试路径")
                return
            try:
                from protocol.models import PortPath
                parts = path_text.replace(',', ' ').split()
                port_path = PortPath([int(p) for p in parts if p])
                logical_addr = None  # 路径模式不使用逻辑地址
            except ValueError as e:
                QMessageBox.warning(self, "提示", f"路径格式错误: {e}")
                return
        
        # 保存配置到 config.ini
        self._save_config()
        
        # 获取回环串口
        loopback_port = self._combo_loopback.currentText()
        loopback_baudrate = self._combo_baudrate.currentData() or 1000000
        if not loopback_port:
            reply = QMessageBox.question(
                self, "提示", 
                "未选择回环串口，将无法验证接收数据。\n是否继续？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No
            )
            if reply == QMessageBox.No:
                return
            loopback_port = None
        
        mode = self._combo_mode.currentText()
        packet_size = self._spin_size.value()
        interval_ms = self._spin_rate.value()
        max_count = self._spin_count.value()
        stop_on_error = self._chk_stop_on_err.isChecked()
        wait_for_ack = self._chk_wait_ack.isChecked()

        # 启动测试线程
        if target_mode == "测试终端口":
            self._worker = ThroughputTestWorker(
                self._client, target_logical_addr=logical_addr, mode=mode,
                packet_size=packet_size, interval_ms=interval_ms,
                loopback_port=loopback_port, loopback_baudrate=loopback_baudrate,
                duration=60, max_count=max_count, stop_on_error=stop_on_error,
                wait_for_ack=wait_for_ack
            )
        else:  # 测试路径
            self._worker = ThroughputTestWorker(
                self._client, target_port_path=port_path, mode=mode,
                packet_size=packet_size, interval_ms=interval_ms,
                loopback_port=loopback_port, loopback_baudrate=loopback_baudrate,
                duration=60, max_count=max_count, stop_on_error=stop_on_error,
                wait_for_ack=wait_for_ack
            )
        self._worker.start()
        
        # 启动刷新定时器（100ms）
        self._refresh_timer.start(100)
        
        # 更新按钮状态
        self._is_running = True
        self._is_paused = False
        self._btn_start.setText("停止测试")
        self._btn_start.setStyleSheet("background-color: #f44336; color: white;")
        self._btn_pause.setEnabled(True)
        self._btn_pause.setText("暂停测试")
        self._btn_pause.setStyleSheet("")
        self._btn_export.setEnabled(False)
        
        logger.info(f"开始通信测试: addr=0x{logical_addr:04X}, mode={mode}, "
                    f"size={packet_size}, interval={interval_ms}ms")

    @Slot()
    def _on_pause(self):
        """暂停测试。"""
        if self._worker:
            self._worker.pause()
        
        # 最后刷新一次监控数据
        self._refresh_metrics()
        
        # 按钮2变为绿色"继续测试"
        self._is_paused = True
        self._btn_pause.setText("继续测试")
        self._btn_pause.setStyleSheet("color: #4CAF50; font-weight: bold;")
        
        # 暂停刷新定时器
        self._refresh_timer.stop()
        
        logger.info("测试已暂停")
    
    @Slot()
    def _on_resume(self):
        """继续测试。"""
        if self._worker:
            # 继续前重新加载发送间隔，使暂停期间的修改即刻生效
            new_interval_ms = max(self._spin_rate.value(), 1)
            self._worker._interval_ms = new_interval_ms
            logger.info(f"发送间隔已更新为 {new_interval_ms}ms")
            self._worker.resume()
        
        # 恢复刷新定时器
        self._refresh_timer.start(100)
        
        # 按钮2恢复为暂停测试
        self._is_paused = False
        self._btn_pause.setText("暂停测试")
        self._btn_pause.setStyleSheet("")
        
        logger.info("测试已继续")
    
    @Slot()
    def _on_stop(self):
        """停止测试。"""
        if not self._is_running:
            return
        self._is_running = False  # 先置位，防止 _refresh_metrics 重入

        if self._worker:
            self._worker.stop()
            self._worker.join(timeout=2.0)
            # 保存最终指标快照，供停止后导出报告使用
            self._last_metrics = self._worker.get_metrics()
            # 保存 worker 参数，供报告生成时计算理论速率（避免从 spinbox 读到被修改后的值）
            self._last_worker_params = {
                'packet_size': self._worker._packet_size,
                'interval_ms': self._worker._interval_ms,
            }
        
        self._refresh_timer.stop()
        
        # 最后刷新一次监控数据
        self._refresh_metrics()
        
        self._worker = None

        # 恢复两个按钮状态
        self._is_paused = False
        self._btn_start.setText("开始测试")
        self._btn_start.setStyleSheet("")
        self._btn_pause.setEnabled(False)
        self._btn_pause.setText("暂停测试")
        self._btn_pause.setStyleSheet("")
        self._btn_export.setEnabled(True)
        
        logger.info("测试已停止")

    @Slot()
    def _on_export(self):
        """导出测试报告。"""
        if self._worker:
            metrics = self._worker.get_metrics()
        elif self._last_metrics:
            metrics = self._last_metrics
        else:
            QMessageBox.warning(self, "提示", "无测试数据可导出")
            return
        
        report = self._generate_report(metrics)
        
        # 生成文件名
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"../datas/throughput_report_{timestamp}.md"
        
        try:
            with open(filename, 'w', encoding='utf-8') as f:
                f.write(report)
            QMessageBox.information(self, "成功", f"报告已保存到:\n{filename}")
            logger.info(f"测试报告已导出: {filename}")
        except Exception as e:
            QMessageBox.critical(self, "错误", f"导出失败: {e}")
            logger.error(f"导出报告失败: {e}")

    def _refresh_metrics(self):
        """定时刷新指标（定时器回调）。"""
        if self._worker:
            metrics = self._worker.get_metrics()
            self._metrics_signal.emit(metrics)
            # 检测 worker 是否已自然结束，自动触发停止流程更新按钮状态
            if not self._worker.is_alive() and self._is_running:
                self._on_stop()

    @Slot(dict)
    def _update_ui(self, metrics: dict):
        """更新 UI 显示（主线程）。"""
        tx_count = metrics.get('tx_count', 0)
        rx_count = metrics.get('rx_count', 0)
        latencies = metrics.get('latencies', [])
        tx_bytes = metrics.get('tx_bytes', 0)
        rx_bytes = metrics.get('rx_bytes', 0)
        queue_samples = metrics.get('queue_depth_samples', [])
        pending_samples = metrics.get('pending_samples', [])
        
        tx_ok_count = metrics.get('tx_ok_count', 0)
        send_err_count = metrics.get('send_err_count', 0)
        loopback_miss = metrics.get('loopback_miss_count', 0)
        data_corrupt = metrics.get('data_corrupt_count', 0)
        
        # 判断是否接入了回环串口（有过接收成功或接收超时才算接入）
        has_loopback = (rx_count > 0 or loopback_miss > 0)
        
        # 计算指标
        # 丢帧率 = (尝试发送总数 - 已确认接收数) / 尝试发送总数
        loss_rate = 0.0
        if tx_count > 0 and has_loopback:
            loss_rate = (tx_count - rx_count) / tx_count * 100
        
        avg_rtt = 0.0
        p50_rtt = 0.0
        p70_rtt = 0.0
        p99_rtt = 0.0
        min_rtt = 0.0
        max_rtt = 0.0
        if latencies:
            avg_rtt = sum(latencies) / len(latencies) * 1000  # ms
            sorted_lat = sorted(latencies)
            n = len(sorted_lat)
            p50_rtt = sorted_lat[int(n * 0.50)] * 1000
            p70_rtt = sorted_lat[int(n * 0.70)] * 1000
            p99_idx = int(n * 0.99)
            p99_rtt = sorted_lat[p99_idx] * 1000 if p99_idx < n else 0.0
            min_rtt = min(latencies) * 1000
            max_rtt = max(latencies) * 1000
        
        # 吞吐量：区间采样（最近1秒实测速率）+ 理论速率对比
        throughput_samples = metrics.get('throughput_samples', [])
        # 实测：取最近两个采样点的差值，计算最近1秒内的速率
        realtime_tx_kbps = 0.0
        realtime_rx_kbps = 0.0
        if len(throughput_samples) >= 2:
            t1, tx1, rx1 = throughput_samples[-2]
            t2, tx2, rx2 = throughput_samples[-1]
            dt = t2 - t1
            if dt > 0:
                realtime_tx_kbps = (tx2 - tx1) / dt / 1024
                realtime_rx_kbps = (rx2 - rx1) / dt / 1024
        # 理论发送速率 = actual_packet_size * (1000 / interval_ms) KB/s
        # 注意：帧头固定 4 字节，实际 payload 最小为 4 字节，故用 max(_packet_size, 4)
        theory_tx_kbps = 0.0
        if self._worker:
            pkt = max(self._worker._packet_size, 4)  # 实际最小包含 4 字节帧头
            itv_ms = self._worker._interval_ms
            theory_tx_kbps = pkt * (1000.0 / itv_ms) / 1024  # KB/s
        
        # 队列深度
        avg_queue = sum(queue_samples) / len(queue_samples) if queue_samples else 0
        avg_pending = sum(pending_samples) / len(pending_samples) if pending_samples else 0
        
        # 系统资源
        cpu_percent = psutil.cpu_percent(interval=0)
        memory_mb = psutil.Process().memory_info().rss / 1024 / 1024
        gc_count = gc.get_count()
        
        # 更新监控文本
        loss_str = f"{loss_rate:.2f}%" if has_loopback else "不可测（无回环）"
        if theory_tx_kbps > 0:
            tx_util = realtime_tx_kbps / theory_tx_kbps * 100
            throughput_line = (
                f"吞吐量(实测): 发 {realtime_tx_kbps:.1f} KB/s / 收 {realtime_rx_kbps:.1f} KB/s  "
                f"理论: {theory_tx_kbps:.1f} KB/s  利用率: {tx_util:.1f}%"
            )
        else:
            throughput_line = f"吞吐量(实测): 发 {realtime_tx_kbps:.1f} KB/s / 收 {realtime_rx_kbps:.1f} KB/s"
        elapsed = self._worker.get_elapsed() if self._worker else 0.0
        elapsed_str = f"{int(elapsed//3600):02d}:{int((elapsed%3600)//60):02d}:{int(elapsed%60):02d}"
        text = (
            f"已发送: {tx_count} 包  已接收: {rx_count} 包  待回复： {tx_ok_count-rx_count} 丢帧率: {loss_str}\n"
            f"发送成功: {tx_ok_count}  发送失败: {send_err_count}  回环超时: {loopback_miss}  数据损坏: {data_corrupt}\n"
            f"队列积压: 发送队列={tx_count-tx_ok_count}  等待响应={tx_ok_count-rx_count}\n"
            f"平均RTT: {avg_rtt:.1f}ms   P50: {p50_rtt:.1f}ms   P70: {p70_rtt:.1f}ms   P99: {p99_rtt:.1f}ms   最小: {min_rtt:.1f}ms   最大: {max_rtt:.1f}ms\n"
            f"{throughput_line}\n"
            f"CPU: {cpu_percent:4.1f}%   内存: {memory_mb:4.1f} MB   GC: {gc_count}\n"
            f"已运行: {elapsed_str}"
        )
        # 回环状态行（有回环活动或处于快速放行时显示）
        loopback_mode = metrics.get('loopback_mode', 'normal')
        if has_loopback or loopback_mode == 'fast_drain':
            text += f"\n回环状态: {metrics.get('loopback_mode_msg') or '正常'}"
        self._monitor_text.setPlainText(text)
        
        # 更新进度条
        # if queue_samples:
        #     self._progress_tx_queue.setValue(int(avg_queue))
        # if pending_samples:
        #     self._progress_pending.setValue(int(avg_pending))

    @Slot(int)
    def _on_size_changed(self, value: int):
        """包大小滑块改变。"""
        self._lbl_size.setText(f"{value} 字节")

    @Slot(int)
    def _on_rate_changed(self, value: int):
        """发送间隔滑块改变。"""
        self._lbl_rate.setText(f"{value} ms")

    def _generate_report(self, metrics: dict) -> str:
        """生成 Markdown 格式测试报告。"""
        tx_count = metrics.get('tx_count', 0)
        tx_ok_count = metrics.get('tx_ok_count', 0)
        rx_count = metrics.get('rx_count', 0)
        send_err_count = metrics.get('send_err_count', 0)
        loopback_miss = metrics.get('loopback_miss_count', 0)
        latencies = metrics.get('latencies', [])
        tx_bytes = metrics.get('tx_bytes', 0)
        rx_bytes = metrics.get('rx_bytes', 0)
        data_corrupt = metrics.get('data_corrupt_count', 0)
        
        has_loopback = (rx_count > 0 or loopback_miss > 0)
        # 丢帧率 = (尝试发送总数 - 已确认接收数) / 尝试发送总数
        loss_rate = (tx_count - rx_count) / tx_count * 100 if (tx_count > 0 and has_loopback) else 0.0
        send_err_rate = send_err_count / tx_count * 100 if tx_count > 0 else 0.0
        loopback_miss_rate = loopback_miss / tx_count * 100 if tx_count > 0 else 0.0
        
        elapsed = self._last_metrics.get('_elapsed', 0) if not self._worker else self._worker.get_elapsed()
        if elapsed <= 0:
            elapsed = 1.0

        # 吞吐量：用区间采样计算平均速率
        throughput_samples = metrics.get('throughput_samples', [])
        if len(throughput_samples) >= 2:
            t_first, tx_first, rx_first = throughput_samples[0]
            t_last, tx_last, rx_last = throughput_samples[-1]
            dt = t_last - t_first
            avg_throughput_tx = (tx_last - tx_first) / dt / 1024 if dt > 0 else 0.0
            avg_throughput_rx = (rx_last - rx_first) / dt / 1024 if dt > 0 else 0.0
        else:
            tx_bytes = metrics.get('tx_bytes', 0)
            rx_bytes = metrics.get('rx_bytes', 0)
            avg_throughput_tx = tx_bytes / elapsed / 1024
            avg_throughput_rx = rx_bytes / elapsed / 1024

        # 理论发送速率：优先从 worker 保存的参数读取，避免 spinbox 被修改后计算错误
        # 注意：帧头固定 4 字节，实际 payload 最小为 4 字节，故用 max(pkt, 4)
        if self._worker:
            pkt = max(self._worker._packet_size, 4)
            itv_ms = max(self._worker._interval_ms, 1)
        elif self._last_worker_params:
            pkt = max(self._last_worker_params['packet_size'], 4)
            itv_ms = max(self._last_worker_params['interval_ms'], 1)
        else:
            pkt = max(self._spin_size.value(), 4)
            itv_ms = max(self._spin_rate.value(), 1)
        theory_tx_kbps = pkt * (1000.0 / itv_ms) / 1024
        
        avg_rtt = sum(latencies) / len(latencies) * 1000 if latencies else 0.0
        sorted_lat = sorted(latencies)
        p50_rtt = sorted_lat[len(sorted_lat) // 2] * 1000 if sorted_lat else 0.0
        p99_idx = int(len(sorted_lat) * 0.99)
        p99_rtt = sorted_lat[p99_idx] * 1000 if p99_idx < len(sorted_lat) else 0.0
        max_rtt = max(latencies) * 1000 if latencies else 0.0
        
        report = f"""# 通信性能测试报告

## 测试配置

- **时间**: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
- **测试终端口**: 0x{self._combo_terminal.currentData():04X}
- **测试模式**: {self._combo_mode.currentText()}
- **包大小**: {self._spin_size.value()} 字节
- **发送间隔**: {self._spin_rate.value()} ms
- **发送次数**: {'∞ 无限' if self._spin_count.value() == 0 else str(self._spin_count.value())}
- **持续时间**: {elapsed:.1f} 秒

## 汇总指标

| 指标 | 数值 | 阈值 | 状态 |
|------|------|------|------|
| 尝试发送包数 | {tx_count} | - | - |
| 成功发送包数 | {tx_ok_count} | - | - |
| 接收包数 | {rx_count} | - | - |
| 丢帧率 | {loss_rate:.2f}% | < 0.1% | {'不可测' if not has_loopback else ('✅' if loss_rate < 0.1 else '❌')} |
| 发送失败率 | {send_err_rate:.2f}% | 0% | {'✅' if send_err_rate == 0 else '❌'} |
| 回环超时率 | {loopback_miss_rate:.2f}% | < 0.1% | {'不可测' if not has_loopback else ('✅' if loopback_miss_rate < 0.1 else '❌')} |
| 数据损坏率 | {data_corrupt / rx_count * 100 if rx_count > 0 else 0.0:.2f}% | 0% | {'✅' if data_corrupt == 0 else '❌'} |
| 平均 RTT | {avg_rtt:.2f} ms | - | - |
| P50 RTT | {p50_rtt:.2f} ms | - | - |
| P99 RTT | {p99_rtt:.2f} ms | < 50 ms | {'✅' if p99_rtt < 50 else '❌'} |
| 最大 RTT | {max_rtt:.2f} ms | - | - |
| 发送吞吐量(实测) | {avg_throughput_tx:.2f} KB/s | - | - |
| 接收吞吐量(实测) | {avg_throughput_rx:.2f} KB/s | - | - |
| 理论发送速率 | {theory_tx_kbps:.2f} KB/s | - | - |
| 带宽利用率 | {(avg_throughput_tx / theory_tx_kbps * 100) if theory_tx_kbps > 0 else 0:.1f}% | - | - |

## 延迟分布

| 范围 (ms) | 数量 | 百分比 |
|-----------|------|--------|
"""
        
        # 延迟分布直方图（0-100ms，10个bin）
        if latencies:
            bins = [0, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100, float('inf')]
            bin_counts = [0] * (len(bins) - 1)
            for lat in latencies:
                lat_ms = lat * 1000
                for i in range(len(bins) - 1):
                    if bins[i] <= lat_ms < bins[i+1]:
                        bin_counts[i] += 1
                        break
            
            for i in range(len(bin_counts)):
                start = bins[i]
                end = bins[i+1] if bins[i+1] != float('inf') else '∞'
                count = bin_counts[i]
                percent = count / len(latencies) * 100
                report += f"| {start}-{end} | {count} | {percent:.1f}% |\n"
        
        report += f"""
## 结论

"""
        
        if loss_rate < 0.1 and p99_rtt < 50:
            report += "✅ 所有关键指标均达标，通信性能良好。\n"
        else:
            report += "⚠️ 部分指标未达标，需要优化：\n"
            if loss_rate >= 0.1:
                report += f"- 丢帧率过高({loss_rate:.2f}%)，检查 USB 连接或设备状态\n"
            if p99_rtt >= 50:
                report += f"- P99 延迟过高({p99_rtt:.2f}ms)，检查路由深度或队列积压\n"
        
        return report


# ──────────────────────────────────────────────
# 自测
# ──────────────────────────────────────────────

if __name__ == '__main__':
    import sys
    
    class MockClient:
        """模拟 FibreNetworkClient。"""
        def __init__(self):
            from device.manager import DeviceManager
            from device.models import Device, ModelType
            from protocol.models import PortPath, PortType
            
            self.device_manager = DeviceManager()
            
            # 添加一个模拟终端口
            dev = Device(
                uid=b'\x01' * 12,
                model=ModelType.FIBRE_485,
                port_type=PortType.PORT_485,
                port_path=PortPath([1, 2]),
                logical_addr=0x0001,
                is_terminal=True,
            )
            self.device_manager.add_device(dev)
        
        def send_cmd(self, logical_addr, cmd, data):
            """模拟发送命令，直接成功。"""
            import time
            time.sleep(0.01)  # 模拟 10ms RTT
            return b'\x00'
    
    app = QApplication(sys.argv)
    
    panel = ThroughputTestPanel()
    panel.set_client(MockClient())
    panel.setWindowTitle("通信性能测试 - 自测")
    panel.resize(800, 600)
    panel.show()
    
    sys.exit(app.exec())
