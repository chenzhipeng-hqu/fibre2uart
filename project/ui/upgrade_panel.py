# -*- coding: utf-8 -*-
"""
ui/upgrade_panel.py — 固件升级面板

UpgradePanel(QWidget) 特性：
  - 选择固件文件（QFileDialog）
  - 下拉选择目标：广播 / 单节点（从已发现节点动态填充）
  - QProgressBar 实时进度展示
  - 升级失败自动重试（最多 3 次）+ QMessageBox 错误码弹窗
  - 所有 UpgradeJob 回调通过 Signal → 主线程更新 UI

自测（__main__）：弹出窗口，注入 MockUpgradeJob，
                   模拟进度更新 / 失败重试 / 完成。
"""
from __future__ import annotations

import sys
import threading
import time
from typing import Optional

from PySide6.QtCore import Qt, QTimer, Signal, Slot
from PySide6.QtWidgets import (
    QApplication, QComboBox, QFileDialog, QHBoxLayout, QLabel,
    QMessageBox, QProgressBar, QPushButton, QVBoxLayout, QWidget,
)


class UpgradePanel(QWidget):
    """固件升级面板。"""

    _progress_signal: Signal = Signal(int)           # 0~100
    _error_signal:    Signal = Signal(str, int, int)  # stage, code, retry_count
    _done_signal:     Signal = Signal()
    _dev_found_signal:   Signal = Signal(object)     # Device（主线程更新下拉）
    _dev_offline_signal: Signal = Signal(bytes)      # uid（主线程移除下拉）

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._client = None
        self._firmware: Optional[bytes] = None
        self._job = None
        # 已加入下拉的 logicAddr 集合（去重）
        self._known_addrs: set = set()
        # uid → logicAddr（用于离线时定位下拉项）
        self._uid_to_addr: dict = {}

        # 从配置读取最大重试次数
        try:
            from config import get_config
            self.max_retries = get_config().upgrade_max_retries
        except Exception:
            self.max_retries = 3

        self._elapsed_secs = 0
        self._build_ui()

        self._progress_signal.connect(self._on_progress)
        self._error_signal.connect(self._on_error)
        self._done_signal.connect(self._on_done)
        self._dev_found_signal.connect(self._on_dev_found)
        self._dev_offline_signal.connect(self._on_dev_offline)

        # 自动加载上次选择的固件文件
        self._autoload_firmware()

    # ──────────────────────────────────────────
    # 公共接口
    # ──────────────────────────────────────────

    def _autoload_firmware(self) -> None:
        """启动时自动加载上次选择的固件文件。"""
        import os
        try:
            from config import get_config
            path = get_config().firmware_path
        except Exception:
            return
        if not path or not os.path.isfile(path):
            return
        try:
            with open(path, 'rb') as f:
                self._firmware = f.read()
            self._edit_file.setText(path)
            self._edit_file.setToolTip(path)
            self._lbl_status.setText(f"已加载 {len(self._firmware)} 字节")
        except Exception:
            pass

    def set_client(self, client) -> None:
        self._client = client

    def handle_device_found(self, event) -> None:
        """订阅 DeviceFoundEvent 后由 EventBus 调用。"""
        self._dev_found_signal.emit(event.device)

    def handle_device_offline(self, event) -> None:
        """订阅 DeviceOfflineEvent 后由 EventBus 调用。"""
        self._dev_offline_signal.emit(event.uid)

    def clear_targets(self) -> None:
        """断开连接时清空节点列表（保留广播项）。"""
        while self._combo_target.count() > 1:
            self._combo_target.removeItem(1)
        self._known_addrs.clear()
        self._uid_to_addr.clear()

    # ──────────────────────────────────────────
    # 内部 UI
    # ──────────────────────────────────────────

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        # 文件选择
        hFile = QHBoxLayout()
        self._edit_file = QLabel("未选择文件")
        self._edit_file.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self._edit_file.setToolTip(self._edit_file.text())
        self._edit_file.setStyleSheet("border: 1px solid #555; padding: 2px;")
        btn_choose = QPushButton("选择固件...")
        btn_choose.clicked.connect(self._choose_file)
        hFile.addWidget(QLabel("固件:"))
        hFile.addWidget(self._edit_file, stretch=1)
        hFile.addWidget(btn_choose)

        # 目标
        hTarget = QHBoxLayout()
        self._combo_target = QComboBox()
        self._combo_target.addItem("广播（选中节点）", userData=None)
        hTarget.addWidget(QLabel("目标:"))
        hTarget.addWidget(self._combo_target, stretch=1)

        # Log 等级
        hLogLevel = QHBoxLayout()
        self._combo_log_level = QComboBox()
        for name, val in [("0 默认", 0), ("1 ERROR", 1), ("2 WARNING", 2), ("3 INFO", 3), ("4 DEBUG", 4)]:
            self._combo_log_level.addItem(name, userData=val)
        self._combo_log_level.setCurrentIndex(2)  # 默认选中2（WARNING）
        hLogLevel.addWidget(QLabel("Log 等级:"))
        hLogLevel.addWidget(self._combo_log_level)
        hLogLevel.addStretch()

        # 进度
        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        self._progress.setMaximumWidth(300)
        self._lbl_elapsed = QLabel("00:00")
        self._lbl_elapsed.setMinimumWidth(55)
        self._lbl_elapsed.setAlignment(Qt.AlignCenter)

        # 计时器（升级总耗时）
        self._elapsed_timer = QTimer(self)
        self._elapsed_timer.setInterval(1000)
        self._elapsed_timer.timeout.connect(self._update_elapsed)

        # 状态
        self._lbl_status = QLabel("就绪")

        # 按钮
        hBtn = QHBoxLayout()
        self._btn_start = QPushButton("开始升级")
        self._btn_start.clicked.connect(self._on_start_or_stop)
        self._btn_retry = QPushButton("重 试")
        self._btn_retry.setEnabled(False)
        self._btn_retry.clicked.connect(self._retry_upgrade)
        hBtn.addWidget(self._btn_start)
        hBtn.addWidget(self._btn_retry)
        hBtn.addStretch()

        hProgress = QHBoxLayout()
        hProgress.addWidget(self._progress, stretch=1)
        hProgress.addWidget(self._lbl_elapsed)

        layout.addLayout(hFile)
        layout.addLayout(hTarget)
        layout.addLayout(hLogLevel)
        layout.addLayout(hProgress)
        layout.addWidget(self._lbl_status)
        layout.addLayout(hBtn)
        layout.addStretch()

    # ──────────────────────────────────────────
    # 按钮回调
    # ──────────────────────────────────────────

    def _choose_file(self) -> None:
        import os

        # 优先使用当前已选择固件所在目录；否则退回当前运行目录
        initial_dir = os.getcwd()
        current_path = self._edit_file.text().strip()
        if current_path and current_path != "未选择文件":
            if os.path.isfile(current_path):
                initial_dir = os.path.dirname(current_path) or initial_dir
            elif os.path.isdir(current_path):
                initial_dir = current_path

        path, _ = QFileDialog.getOpenFileName(
            self, "选择固件文件", initial_dir, "Binary (*.bin *.hex);;All Files (*)")
        if path:
            with open(path, 'rb') as f:
                self._firmware = f.read()
            self._edit_file.setText(path)
            self._edit_file.setToolTip(path)
            self._lbl_status.setText(f"已加载 {len(self._firmware)} 字节")
            # 保存固件路径到 config.ini
            try:
                from config import get_config
                get_config().set_firmware_path(path)
            except Exception:
                pass

    def _on_start_or_stop(self) -> None:
        """开始升级 / 停止升级 按钮共用入口。"""
        if self._btn_start.text() == "停止升级":
            self._stop_upgrade()
        else:
            self._start_upgrade()

    def _stop_upgrade(self) -> None:
        """用户主动停止升级。"""
        if self._job is not None:
            self._job.cancel()
        self._elapsed_timer.stop()
        self._btn_start.setText("开始升级")
        self._btn_start.setEnabled(True)
        self._btn_retry.setEnabled(False)
        self._lbl_status.setText("已停止")

    def _start_upgrade(self) -> None:
        # 每次都重新读取当前固件路径
        import os
        path = self._edit_file.text().strip()
        if not path or not os.path.isfile(path):
            QMessageBox.warning(self, "提示", "请先选择固件文件")
            return
        try:
            with open(path, 'rb') as f:
                self._firmware = f.read()
            self._lbl_status.setText(f"已加载 {len(self._firmware)} 字节")
        except Exception as e:
            QMessageBox.warning(self, "提示", f"固件读取失败: {e}")
            return
        if self._client is None:
            QMessageBox.warning(self, "提示", "请先连接设备")
            return
        self._btn_start.setText("停止升级")
        self._btn_start.setEnabled(True)
        self._btn_retry.setEnabled(False)
        self._progress.setValue(0)
        self._lbl_status.setText("升级中…")
        self._elapsed_secs = 0
        self._lbl_elapsed.setText("00:00")
        self._elapsed_timer.start()

        logical_addr = self._combo_target.currentData()
        log_level = self._combo_log_level.currentData() or 0
        if logical_addr is None:
            # 广播
            self._job = self._client.broadcast_upgrade(
                self._firmware,
                on_progress=self._progress_signal.emit,
                on_error=self._error_signal.emit,
                on_done=self._done_signal.emit,
                log_level=log_level,
            )
        else:
            self._job = self._client.upgrade_device(
                logical_addr,
                self._firmware,
                on_progress=self._progress_signal.emit,
                on_error=self._error_signal.emit,
                on_done=self._done_signal.emit,
                log_level=log_level,
            )

    def _retry_upgrade(self) -> None:
        if self._job is not None:
            self._btn_retry.setEnabled(False)
            self._btn_start.setText("停止升级")
            self._btn_start.setEnabled(True)
            self._progress.setValue(0)
            self._lbl_status.setText("重试中…")
            self._elapsed_timer.start()
            self._job.retry()

    # ──────────────────────────────────────────
    # 设备列表 Slot（主线程）
    # ──────────────────────────────────────────

    @Slot(object)
    def _on_dev_found(self, device) -> None:
        """节点发现后追加到目标下拉（非终端口、去重）。"""
        addr = device.logical_addr
        if addr == 0 or addr in self._known_addrs or device.is_terminal:
            return
        self._known_addrs.add(addr)
        self._uid_to_addr[device.uid] = addr
        model_str = device.model.name if hasattr(device.model, 'name') else str(device.model)
        self._combo_target.addItem(f"{model_str} (0x{addr:04X})", userData=addr)

    @Slot(bytes)
    def _on_dev_offline(self, uid: bytes) -> None:
        """节点离线后从下拉中移除。"""
        addr = self._uid_to_addr.pop(uid, None)
        if addr is None:
            return
        self._known_addrs.discard(addr)
        for i in range(self._combo_target.count()):
            if self._combo_target.itemData(i) == addr:
                self._combo_target.removeItem(i)
                break

    # ──────────────────────────────────────────
    # 升级回调 → 主线程 Slot
    # ──────────────────────────────────────────

    @Slot(int)
    def _on_progress(self, progress: int) -> None:
        self._progress.setValue(progress)
        self._lbl_status.setText(f"升级中… {progress}%")

    @Slot(str, int, int)
    def _on_error(self, stage: str, code: int, retry_count: int) -> None:
        self._elapsed_timer.stop()
        self._btn_start.setText("开始升级")
        self._lbl_status.setText(
            f"❌ 升级失败（阶段: {stage}，错误码: {code:#06x}，"
            f"重试: {retry_count}/{self.max_retries}）")

        if retry_count < self.max_retries:
            # job 内已自动重试，恢复停止升级按鈕并弹窗提示
            self._btn_start.setText("停止升级")
            self._elapsed_timer.start()
            QMessageBox.warning(
                self, "升级失败",
                f"阶段 [{stage}] 发生错误（错误码 {code:#06x}），"
                f"正在自动重试（{retry_count}/{self.max_retries}）…")
        else:
            # 超过最大重试
            ret = QMessageBox.critical(
                self, "升级失败",
                f"已连续失败 {self.max_retries} 次。\n"
                f"最后错误：[{stage}] 错误码 {code:#06x}。\n\n"
                "点击[重试]重新开始升级，或[取消]放弃。",
                QMessageBox.Retry | QMessageBox.Cancel,
            )
            if ret == QMessageBox.Retry:
                self._retry_upgrade()
            else:
                self._btn_start.setEnabled(True)

    @Slot()
    def _on_done(self) -> None:
        self._elapsed_timer.stop()
        self._progress.setValue(100)
        self._lbl_status.setText("✅ 升级完成！")
        self._btn_start.setText("开始升级")
        self._btn_start.setEnabled(True)
        self._btn_retry.setEnabled(False)
        QMessageBox.information(self, "升级完成", "固件升级成功！")


    @Slot()
    def _update_elapsed(self) -> None:
        """每秒触发，更新升级耗时标签。"""
        self._elapsed_secs += 1
        m, s = divmod(self._elapsed_secs, 60)
        self._lbl_elapsed.setText(f"{m:02d}:{s:02d}")


# ──────────────────────────────────────────────
# 自测
# ──────────────────────────────────────────────

if __name__ == '__main__':
    app = QApplication(sys.argv)
    panel = UpgradePanel()
    panel.setWindowTitle("UpgradePanel 自测")
    panel.resize(500, 300)
    panel.show()

    # 模拟进度更新
    def _simulate():
        for i in range(0, 101, 5):
            panel._progress_signal.emit(i)
            time.sleep(0.1)
        # 模拟失败重试
        panel._error_signal.emit("SEND_DATA", -2, 1)

    t = threading.Thread(target=_simulate, daemon=True)
    t.start()

    sys.exit(app.exec())
