# -*- coding: utf-8 -*-
"""
virtual_serial/port.py — 虚拟串口（PTY / Windows Pipe）

VirtualSerialPort 将一个终端口（PORT_485 / PORT_232 / PORT_CAN）映射成系统级虚拟串口设备：
  - Linux  : os.openpty()，slave 端 symlink 到 /dev/ttyVCM__NNNN
  - Windows: Named Pipe \\\\.\pipe\\VCM_NNNN（无需额外驱动，pyserial 可读写）

第三方软件直接 open device_path 即可双向通信：
  - 第三方写入 → _reader_thread 读出 → _on_pty_data() → 封帧 → SerialTransport.send()
  - 设备数据到达 → write_to_pty(data)  → 第三方从 device_path 读出

overflow_count：write_to_pty 因 master_fd 写满（BlockingIOError）而丢弃的字节批次数，
                供 TerminalPortList UI 展示链路质量。
"""
from __future__ import annotations

import atexit
import glob
import logging
import os
import sys
import threading
from typing import Callable, Optional

from protocol.models import PortPath, PortType

logger = logging.getLogger(__name__)

# 虚拟串口 symlink 目录（/dev 下，路径符合常规串口命名规范）
VCOM_DIR_LINUX = "/dev"


# ──────────────────────────────────────────────
# 公共辅助
# ──────────────────────────────────────────────

def _cleanup_all_symlinks() -> None:
    """atexit 钩子：程序正常退出时删除所有遗留 symlink。"""
    for pattern_dir in (VCOM_DIR_LINUX, "/tmp"):
        for path in glob.glob(os.path.join(pattern_dir, "ttyVCM_*")):
            if os.path.islink(path):
                try:
                    os.unlink(path)
                except OSError:
                    pass


# ──────────────────────────────────────────────
# VirtualSerialPort
# ──────────────────────────────────────────────

class VirtualSerialPort:
    """
    系统级虚拟串口，对应一个终端口的逻辑通信通道。

    :param logical_addr:     终端口逻辑地址（用于路由查表 + symlink 命名）
    :param port_type:        PORT_485 / PORT_232 / PORT_CAN
    :param baud_rate:        波特率（仅元数据，实际 PTY 不做硬件流控）
    :param logic_table:      PCLogicRoutingTable 实例（供 _on_pty_data 查路由）
    :param transport_send:   SerialTransport.send 的可调用引用
    :param encode_fn:        CommandCodec.encode(seq, cmd, port_path, data)->bytes 的可调用引用
    :param seq_allocate:     SeqManager.allocate() 的可调用引用
    """

    _atexit_registered: bool = False

    def __init__(
        self,
        logical_addr: int,
        port_type: PortType,
        baud_rate: int = 115200,
        logic_table=None,
        transport_send: Optional[Callable[[bytes], None]] = None,
        encode_fn: Optional[Callable] = None,
        seq_allocate: Optional[Callable[[], int]] = None,
    ) -> None:
        self.logical_addr = logical_addr
        self.port_type = port_type
        self.baud_rate = baud_rate

        self._logic_table = logic_table
        self._transport_send = transport_send
        self._encode_fn = encode_fn
        self._seq_allocate = seq_allocate

        self.device_path: str = ''
        self.is_open: bool = False
        self.overflow_count: int = 0       # write_to_pty 溢出计数
        self.tx_bytes: int = 0             # 第三方→光纤（上行）字节数
        self.rx_bytes: int = 0             # 光纤→第三方（下行）字节数

        self._closing: bool = False
        self._master_fd: int = -1
        self._slave_keepalive_fd: int = -1   # 防止 master 出现 EIO/POLLHUP
        self._slave_path: str = ''
        self._symlink_path: str = ''
        self._reader_thread: Optional[threading.Thread] = None

        # Windows Pipe
        self._pipe_handle = None   # win32file handle

        # 外部回调：收到 PTY 数据后（第三方→光纤），上层可覆盖；默认走内置路由
        self.on_pty_data: Optional[Callable[[bytes], None]] = None

    # ──────────────────────────────────────────
    # 生命周期
    # ──────────────────────────────────────────

    def open(self) -> str:
        """
        打开虚拟串口，返回 device_path（第三方软件使用此路径打开）。
        """
        if self.is_open:
            return self.device_path

        if sys.platform == 'win32':
            return self._open_windows()
        else:
            return self._open_linux()

    def close(self) -> None:
        """
        4 步优雅关闭：
          1. 置 _closing=True，停止 write_to_pty
          2. 删除 symlink（第三方软件感知设备消失）
          3. 等待最多 500ms（第三方 I/O 窗口）
          4. close(master_fd)，停止读取线程
        """
        if not self.is_open:
            return
        self._closing = True

        # 2. 删除 symlink
        if self._symlink_path and os.path.islink(self._symlink_path):
            try:
                os.unlink(self._symlink_path)
            except OSError:
                pass

        # 3. 等待 500ms
        if self._reader_thread is not None:
            self._reader_thread.join(timeout=0.5)

        # 4. 关闭 keepalive slave + master_fd
        if sys.platform != 'win32' and self._master_fd >= 0:
            if self._slave_keepalive_fd >= 0:
                try:
                    os.close(self._slave_keepalive_fd)
                except OSError:
                    pass
                self._slave_keepalive_fd = -1
            try:
                os.close(self._master_fd)
            except OSError:
                pass
            self._master_fd = -1
        elif sys.platform == 'win32' and self._pipe_handle is not None:
            try:
                import win32file
                win32file.CloseHandle(self._pipe_handle)
            except Exception:
                pass
            self._pipe_handle = None

        self.is_open = False
        logger.info("VirtualSerialPort closed: %s (0x%04X)",
                    self.device_path, self.logical_addr)

    # ──────────────────────────────────────────
    # 对外写入（光纤 → 第三方）
    # ──────────────────────────────────────────

    def write_to_pty(self, data: bytes) -> None:
        """
        将设备下行数据写入虚拟串口，供第三方软件读取。
        写满时丢弃并递增 overflow_count（非阻塞保护）。
        """
        if self._closing or not self.is_open:
            return
        self.rx_bytes += len(data)   # 设备下行数据，记录总字节数
        try:
            if sys.platform == 'win32':
                if self._pipe_handle is not None:
                    import win32file
                    win32file.WriteFile(self._pipe_handle, data)
            else:
                os.write(self._master_fd, data)
        except BlockingIOError:
            self.overflow_count += 1
            logger.debug("VSerialPort 0x%04X: write overflow (%d bytes dropped)",
                         self.logical_addr, len(data))
        except OSError as e:
            logger.debug("VSerialPort 0x%04X: write error: %s", self.logical_addr, e)

    # ──────────────────────────────────────────
    # Linux PTY
    # ──────────────────────────────────────────

    def _open_linux(self) -> str:
        # 1. 确定 symlink 路径：优先 /dev，无权限时退回 /tmp
        preferred = os.path.join(VCOM_DIR_LINUX,
                                 f"ttyVCM_{self.logical_addr:04x}")
        fallback  = os.path.join("/tmp",
                                 f"ttyVCM_{self.logical_addr:04x}")
        # 探测写权限
        try:
            os.makedirs(VCOM_DIR_LINUX, exist_ok=True)
            test_path = preferred + ".tmp"
            open(test_path, 'w').close()
            os.unlink(test_path)
            self._symlink_path = preferred
        except OSError:
            self._symlink_path = fallback
            logger.warning(
                "无权限在 %s 创建 symlink，退回到 %s",
                VCOM_DIR_LINUX, "/tmp")

        if os.path.islink(self._symlink_path):
            os.unlink(self._symlink_path)

        # 注册 atexit（只注册一次）
        if not VirtualSerialPort._atexit_registered:
            atexit.register(_cleanup_all_symlinks)
            VirtualSerialPort._atexit_registered = True

        # 2. 创建 PTY 对
        self._master_fd, slave_fd = os.openpty()
        self._slave_path = os.ttyname(slave_fd)
        # 禁用 PTY echo：防止第三方写入的 TX 数据被回显为 RX
        try:
            import tty
            tty.setraw(slave_fd)
        except Exception:
            pass
        # 保留 slave_fd 作为 keepalive：防止 master 无 slave 打开时出现 POLLHUP/EIO
        # 第三方通过 device_path（symlink）再打开 slave 后共享同一 PTY 设备
        self._slave_keepalive_fd = slave_fd

        # 3. 创建 symlink
        os.symlink(self._slave_path, self._symlink_path)
        try:
            os.chmod(self._slave_path, 0o666)
        except OSError:
            pass

        # 4. 设为非阻塞
        import fcntl
        flags = fcntl.fcntl(self._master_fd, fcntl.F_GETFL)
        fcntl.fcntl(self._master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

        # 5. 启动读取线程
        self.device_path = self._symlink_path
        self.is_open = True
        self._reader_thread = threading.Thread(
            target=self._linux_reader_loop,
            daemon=True,
            name=f'VSerial-0x{self.logical_addr:04X}',
        )
        self._reader_thread.start()

        logger.info("VirtualSerialPort opened: %s (0x%04X)",
                    self.device_path, self.logical_addr)
        return self.device_path

    def _linux_reader_loop(self) -> None:
        """Linux: 持续从 master_fd 读取第三方写入的数据，转发到光纤。"""
        import errno as _errno
        import select
        import time as _time
        while not self._closing and self._master_fd >= 0:
            try:
                rlist, _, _ = select.select([self._master_fd], [], [], 0.1)
                if rlist:
                    data = os.read(self._master_fd, 4096)
                    if data:
                        self._on_pty_data(data)
            except OSError as e:
                if e.errno == _errno.EIO:
                    # slave 端尚未打开（PTY POLLHUP），等待后重试
                    _time.sleep(0.05)
                    continue
                break

    # ──────────────────────────────────────────
    # Windows Named Pipe
    # ──────────────────────────────────────────

    def _open_windows(self) -> str:
        pipe_name = f"\\\\.\\pipe\\VCM_{self.logical_addr:04x}"
        try:
            import win32pipe
            import win32file
            self._pipe_handle = win32pipe.CreateNamedPipe(
                pipe_name,
                win32pipe.PIPE_ACCESS_DUPLEX,
                win32pipe.PIPE_TYPE_BYTE | win32pipe.PIPE_READMODE_BYTE | win32pipe.PIPE_WAIT,
                1, 65536, 65536, 0, None,
            )
        except Exception as e:
            raise RuntimeError(f"Cannot create Windows named pipe: {e}") from e

        self.device_path = pipe_name
        self.is_open = True
        self._reader_thread = threading.Thread(
            target=self._windows_reader_loop,
            daemon=True,
            name=f'VSerial-0x{self.logical_addr:04X}',
        )
        self._reader_thread.start()
        logger.info("VirtualSerialPort opened: %s", self.device_path)
        return self.device_path

    def _windows_reader_loop(self) -> None:
        try:
            import win32file
            import win32pipe
            win32pipe.ConnectNamedPipe(self._pipe_handle, None)
            while not self._closing:
                hr, data = win32file.ReadFile(self._pipe_handle, 4096)
                if data:
                    self._on_pty_data(bytes(data))
        except Exception:
            pass

    # ──────────────────────────────────────────
    # 内部：第三方 → 光纤
    # ──────────────────────────────────────────

    def _on_pty_data(self, data: bytes) -> None:
        """
        第三方写入 PTY 的数据 → 封成协议帧 → SerialTransport.send()。
        若注册了 on_pty_data 回调则优先使用外部回调（方便测试注入）。
        """
        if self.on_pty_data is not None:
            self.on_pty_data(data)
            return

        if self._logic_table is None or self._transport_send is None:
            return

        # 1. 查路由表
        port_path: Optional[PortPath] = self._logic_table.lookup(self.logical_addr)
        if port_path is None:
            logger.debug("VSerial 0x%04X: no route found, dropping %d bytes",
                         self.logical_addr, len(data))
            return

        # 2. cmd 由端口类型决定（协议 §1.2 cmd<0x10 为消息透传）
        cmd_map = {
            PortType.PORT_UART: 0x01,
            PortType.PORT_232:  0x03,
            PortType.PORT_485:  0x02,
            PortType.PORT_CAN:  0x04,
        }
        cmd = cmd_map.get(self.port_type, 0x01)

        # 3. 编码 + 发送
        if self._encode_fn is not None and self._seq_allocate is not None:
            self.tx_bytes += len(data)   # 第三方上行数据，记录总字节数
            seq = self._seq_allocate()
            raw = self._encode_fn(seq, cmd, port_path, data)
            self._transport_send(raw)
