# -*- coding: utf-8 -*-
"""
ui/main_window.py — 主窗口

MainWindow 继承 Ui_MainWindow（纯布局），在其基础上：
  1. 组装所有子 Widget（DeviceTreeView / TopologyView / RouteTableView /
     TerminalPortList / CommandPanel / UpgradePanel / LogViewer）
  2. 绑定 FibreNetworkClient 事件与 UI 更新
  3. 实现串口连接 / 断开 / 扫描 / 升级的业务逻辑
  4. nodeSelected 双向联动（DeviceTreeView ↔ TopologyView ↔ 详情面板）

启动入口（__main__）：直接运行此文件可启动完整 GUI。
"""
from __future__ import annotations

import logging
import sys
import threading
from typing import Optional

from PySide6.QtCore import Qt, QTimer, Signal, Slot
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QMessageBox,
)

# ── 项目路径 ──────────────────────────────────────
import os
_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)

from ui.ui import Ui_MainWindow
from ui.log_viewer import LogViewer, LogViewerHandler
from ui.device_tree import DeviceTreeView
from ui.topology_view import TopologyView
from ui.command_panel import CommandPanel
from ui.upgrade_panel import UpgradePanel
from ui.cmd_test_panel import CmdTestPanel

logger = logging.getLogger(__name__)


class MainWindow(QMainWindow, Ui_MainWindow):
    """
    主窗口：继承纯布局 Ui_MainWindow，实现全部业务交互。
    """

    # 用于跨线程更新 UI 状态栏
    _status_signal: Signal = Signal(str, str)   # (text, color)
    _read_version_done_signal: Signal = Signal()  # 读取版本号完成

    def __init__(self) -> None:
        super().__init__()
        self.setupUi(self)

        # ── 替换占位 Widget 为真实组件 ───────────────
        self._build_real_widgets()

        # ── 内部状态 ──────────────────────────────────
        self._client = None   # FibreNetworkClient（连接后创建）
        self._log_handler: Optional[LogViewerHandler] = None

        # ── 信号连接 ──────────────────────────────────
        self._connect_signals()
        self._status_signal.connect(self._on_status)

        # ── 初始化串口下拉 ────────────────────────────
        self._populate_ports()

        # comboPort 展开时自动刷新串口列表
        _orig_popup = self.comboPort.showPopup
        def _show_popup_refresh():
            self._populate_ports()
            _orig_popup()
        self.comboPort.showPopup = _show_popup_refresh

        logger.info("MainWindow 初始化完成")

    # ──────────────────────────────────────────
    # 构建真实子组件（替换 ui.py 占位 Widget）
    # ──────────────────────────────────────────

    def _build_real_widgets(self) -> None:
        # DeviceTreeView
        self._device_tree = DeviceTreeView(self.tabDeviceTree)
        self.tabDeviceTree.layout().replaceWidget(
            self.deviceTreeWidget, self._device_tree)
        self.deviceTreeWidget.deleteLater()

        # TopologyView
        self._topology = TopologyView(self.tabTopology)
        self.tabTopology.layout().replaceWidget(
            self.topologyView, self._topology)
        self.topologyView.deleteLater()

        # 移除路由表 Tab
        route_idx = self.tabLeft.indexOf(self.tabRouteTable)
        if route_idx >= 0:
            self.tabLeft.removeTab(route_idx)

        # CommandPanel（替换 tabCommandPanel 的表单布局）
        self._cmd_panel = CommandPanel(self.tabCommandPanel)
        # 清空旧布局再插入
        old = self.tabCommandPanel.layout()
        if old:
            while old.count():
                item = old.takeAt(0)
                if item.widget():
                    item.widget().deleteLater()
        from PySide6.QtWidgets import QVBoxLayout
        if not self.tabCommandPanel.layout():
            self.tabCommandPanel.setLayout(QVBoxLayout())
        self.tabCommandPanel.layout().addWidget(self._cmd_panel)

        # UpgradePanel（替换 tabUpgrade 的内容）
        self._upgrade_panel = UpgradePanel(self.tabUpgrade)
        old2 = self.tabUpgrade.layout()
        if old2:
            while old2.count():
                item = old2.takeAt(0)
                if item.widget():
                    item.widget().deleteLater()
        from PySide6.QtWidgets import QVBoxLayout as VL
        if not self.tabUpgrade.layout():
            self.tabUpgrade.setLayout(VL())
        self.tabUpgrade.layout().addWidget(self._upgrade_panel)

        # ThroughputTestPanel（替换 tabThroughput 的内容）
        from ui.throughput_panel import ThroughputTestPanel
        self._throughput_panel = ThroughputTestPanel(self.tabThroughput)
        old3 = self.tabThroughput.layout()
        if old3:
            while old3.count():
                item = old3.takeAt(0)
                if item.widget():
                    item.widget().deleteLater()
        from PySide6.QtWidgets import QVBoxLayout as VL2
        if not self.tabThroughput.layout():
            self.tabThroughput.setLayout(VL2())
        self.tabThroughput.layout().addWidget(self._throughput_panel)

        # CmdTestPanel（替换 tabCmdTest 的内容）
        self._cmd_test_panel = CmdTestPanel(self.tabCmdTest)
        old4 = self.tabCmdTest.layout()
        if old4:
            while old4.count():
                item = old4.takeAt(0)
                if item.widget():
                    item.widget().deleteLater()
        from PySide6.QtWidgets import QVBoxLayout as VL3
        if not self.tabCmdTest.layout():
            self.tabCmdTest.setLayout(VL3())
        self.tabCmdTest.layout().addWidget(self._cmd_test_panel)

        # LogViewer（替换 logViewer）
        self._log_viewer = LogViewer(self.widgetLog)
        self.widgetLog.layout().replaceWidget(
            self.logViewer, self._log_viewer)
        self.logViewer.deleteLater()

    # ──────────────────────────────────────────
    # 信号连接
    # ──────────────────────────────────────────

    def _connect_signals(self) -> None:
        # 工具栏 / 菜单按钮
        self.btnConnect.clicked.connect(self._on_toggle_connection)
        self.btnScan.clicked.connect(self._on_scan)
        self.btnReadVersion.clicked.connect(self._on_read_versions)
        self.actionConnect.triggered.connect(self._on_connect)
        self.actionDisconnect.triggered.connect(self._on_disconnect)
        self.actionScan.triggered.connect(self._on_scan)
        self.actionExit.triggered.connect(self.close)
        self.actionAbout.triggered.connect(self._on_about)

        # 日志级别过滤
        self.logLevelCombo.currentTextChanged.connect(
            self._log_viewer.set_level_filter)
        self.btnClearLog.clicked.connect(self._log_viewer.clear_log)

        # 节点选中联动
        self._device_tree.nodeSelected.connect(self._on_node_selected)
        self._topology.nodeSelected.connect(self._on_topology_node_selected)

        # 读取版本号完成后恢复按钮
        self._read_version_done_signal.connect(
            lambda: self.btnReadVersion.setEnabled(True))

    # ──────────────────────────────────────────
    # 串口下拉初始化
    # ──────────────────────────────────────────

    def _populate_ports(self) -> None:
        """扫描可用串口填充下拉框。"""
        # 保留当前选中项（首次快照已保存串口）
        current = self.comboPort.currentText()
        if not current:
            from config import get_config
            current = get_config().serial_port

        try:
            import serial.tools.list_ports
            ports = [p.device for p in serial.tools.list_ports.comports()]
        except Exception:
            ports = []

        if not ports:
            ports = ["/dev/ttyUSB0", "/dev/ttyS0", "COM1", "COM3"]

        # 如果保存的串口不在列表中，临时插入头部以便选中
        if current and current not in ports:
            ports.insert(0, current)

        self.comboPort.clear()
        for p in ports:
            self.comboPort.addItem(p)

        if current:
            idx = self.comboPort.findText(current)
            if idx >= 0:
                self.comboPort.setCurrentIndex(idx)

    # ──────────────────────────────────────────
    # 连接 / 断开 / 扫描
    # ──────────────────────────────────────────

    @Slot()
    def _on_toggle_connection(self) -> None:
        """btnConnect 切换：未连接 → 连接；已连接 → 断开。"""
        if self._client is None:
            self._on_connect()
        else:
            self._on_disconnect()

    @Slot()
    def _on_connect(self) -> None:
        port = self.comboPort.currentText().strip()
        if not port:
            QMessageBox.warning(self, "提示", "请选择串口")
            return

        baud = int(self.comboBaud.currentText())

        # 延迟导入，避免循环依赖
        from api import FibreNetworkClient
        from events.bus import (
            DeviceFoundEvent, DeviceOfflineEvent, NodeDiscoveredEvent,
            TransportLostEvent, TransportRestoredEvent,
            DiscoveryFailedEvent,
        )

        self._client = FibreNetworkClient()

        # 注册 logging handler → LogViewer
        self._log_handler = LogViewerHandler(self._log_viewer)
        self._log_handler.setFormatter(logging.Formatter(
            '[%(asctime)s] %(name)s [%(levelname)s] %(message)s'))
        logging.getLogger().addHandler(self._log_handler)

        # 绑定波特率/虚拟串口函数到 DeviceTreeView（终端口行）
        self._device_tree._set_baudrate_fn = self._client.set_baudrate
        self._device_tree._open_vserial_fn = self._client.open_virtual_serial
        self._device_tree._close_vserial_fn = self._client.close_virtual_serial

        # 订阅 EventBus 事件
        bus = self._client.event_bus
        self._tok1 = bus.subscribe(NodeDiscoveredEvent,
                                   self._device_tree.handle_node_discovered)
        self._tok2 = bus.subscribe(DeviceFoundEvent,
                                   self._device_tree.handle_device_found)
        self._tok3 = bus.subscribe(DeviceFoundEvent,
                                   self._topology.handle_device_found)
        self._tok5 = bus.subscribe(DeviceFoundEvent,
                                   self._upgrade_panel.handle_device_found)
        self._tok15 = bus.subscribe(DeviceFoundEvent,
                                    self._throughput_panel.handle_device_found)
        self._tok17 = bus.subscribe(DeviceFoundEvent,
                                    self._cmd_test_panel.handle_device_found)
        self._tok6 = bus.subscribe(DeviceOfflineEvent,
                                   self._device_tree.handle_device_offline)
        # 中继节点发现后自动读取版本号
        self._tok14 = bus.subscribe(DeviceFoundEvent,
                                    self._on_relay_found)
        self._tok7 = bus.subscribe(DeviceOfflineEvent,
                                   self._topology.handle_device_offline)
        self._tok9 = bus.subscribe(DeviceOfflineEvent,
                                   self._upgrade_panel.handle_device_offline)
        self._tok16 = bus.subscribe(DeviceOfflineEvent,
                                    self._throughput_panel.handle_device_offline)
        self._tok18 = bus.subscribe(DeviceOfflineEvent,
                                    self._cmd_test_panel.handle_device_offline)
        self._tok11 = bus.subscribe(TransportLostEvent,
                                    lambda e: self._status_signal.emit(
                                        "● USB 断连，重连中…", "orange"))
        self._tok12 = bus.subscribe(TransportRestoredEvent,
                                    lambda e: self._status_signal.emit(
                                        "● 已重连", "#4CAF50"))
        self._tok13 = bus.subscribe(DiscoveryFailedEvent,
                                    lambda e: self._status_signal.emit(
                                        f"● 发现失败: {e.reason}", "#F44336"))

        # 注入 client 到子组件
        self._cmd_panel.set_client(self._client)
        self._upgrade_panel.set_client(self._client)
        self._throughput_panel.set_client(self._client)
        self._cmd_test_panel.set_client(self._client)

        # 连接
        try:
            from config import get_config
            self._client.connect(port, baud, hotplug=get_config().hotplug)
        except Exception as e:
            QMessageBox.critical(self, "连接失败", str(e))
            return

        # 更新按钮状态
        self.btnConnect.setText("断开")
        self.btnScan.setEnabled(True)
        self.btnReadVersion.setEnabled(True)
        self._status_signal.emit("● 已连接，正在发现设备…", "#2196F3")
        logger.info("已连接到 %s @ %d", port, baud)
        # 保存已成功连接的串口到 config.ini
        from config import get_config
        get_config().set_serial_port(port)
    @Slot()
    def _on_disconnect(self) -> None:
        if self._client is not None:
            self._client.disconnect()
            self._client = None

        if self._log_handler is not None:
            logging.getLogger().removeHandler(self._log_handler)
            self._log_handler = None

        self._device_tree.clear_tree()
        self._topology.clear_topology()
        self._upgrade_panel.clear_targets()
        self._cmd_test_panel.clear_targets()
        self.btnConnect.setText("连接")
        self.btnScan.setEnabled(False)
        self.btnReadVersion.setEnabled(False)
        self._status_signal.emit("● 已断开", "gray")
        logger.info("已断开连接")

    @Slot()
    def _on_scan(self) -> None:
        if self._client is None:
            return
        self.btnScan.setEnabled(False)
        self._device_tree.clear_tree()
        self._topology.clear_topology()
        self._upgrade_panel.clear_targets()
        self._cmd_test_panel.clear_targets()
        self._client.rescan()
        # 扫描完成后通过 RouteTableUpdatedEvent 重新刷新，3s 后恢复按钮
        QTimer.singleShot(3000, lambda: self.btnScan.setEnabled(True))

    @Slot()
    def _on_read_versions(self) -> None:
        """读取所有中继节点的版本号，在后台线程中逐节点查询并更新设备树。"""
        if self._client is None:
            return
        self.btnReadVersion.setEnabled(False)
        client = self._client

        def _worker():
            relay_devices = [
                d for d in client.device_manager.all_devices()
                if not d.is_terminal and d.uid != bytes(12)
            ]
            logger.info("读取版本号：共发现 %d 个中继节点", len(relay_devices))
            for device in relay_devices:
                try:
                    info = client.get_board_info(device.logical_addr)
                    loc_str = "APP" if info.location == 2 else "BOOT"
                    version_str = f"{info.mfg_date}_{loc_str}"
                    self._device_tree.update_version(device.uid, version_str)
                    logger.info("版本号 addr=0x%04X: %s", device.logical_addr, version_str)
                except Exception as e:
                    logger.warning("读取版本号失败 addr=0x%04X: %s", device.logical_addr, e)
                    self._device_tree.update_version(device.uid, "读取失败")
            self._read_version_done_signal.emit()

        threading.Thread(target=_worker, daemon=True, name="read_versions").start()

    def _on_relay_found(self, event) -> None:
        """EventBus 回调（任意线程）：中继节点发现后自动获取版本号。"""
        device = event.device
        if device.is_terminal or device.uid == bytes(12):
            return
        client = self._client
        if client is None:
            return

        def _fetch():
            try:
                info = client.get_board_info(device.logical_addr)
                loc_str = "APP" if info.location == 2 else "BOOT"
                version_str = f"{info.mfg_date}_{loc_str}"
                self._device_tree.update_version(device.uid, version_str)
                logger.info("版本号 addr=0x%04X: %s", device.logical_addr, version_str)
            except Exception as e:
                logger.warning("读取版本号失败 addr=0x%04X: %s", device.logical_addr, e)
                self._device_tree.update_version(device.uid, "读取失败")

        threading.Thread(target=_fetch, daemon=True,
                         name=f"ver_{device.logical_addr:04X}").start()

    # ──────────────────────────────────────────
    # 节点选中联动
    # ──────────────────────────────────────────

    @Slot(object)
    def _on_node_selected(self, device) -> None:
        """DeviceTreeView 点击 → 更新详情面板 + TopologyView 高亮。"""
        self.lblModel.setText(
            device.model.name if hasattr(device.model, 'name') else str(device.model))
        self.lblUuid.setText(device.uid.hex())
        self.lblAddr.setText(f"0x{device.logical_addr:04X}")
        self.lblPortPath.setText(str(list(device.port_path.ports)))
        # 端口信息：显示各端口类型，跳过 NONE(0) 端口
        port_types = getattr(device, 'port_types', [])
        if port_types:
            parts = [
                f"{i+1}:{pt.name.replace('PORT_', '')}"
                for i, pt in enumerate(port_types)
                if getattr(pt, 'value', pt) != 0   # 跳过 NONE
            ]
            self.lblPorts.setText("  ".join(parts) if parts else "—")
        else:
            self.lblPorts.setText("—")
        self._topology.select_node(device.uid)

        # 终端口：显示 TX/RX/溢出统计
        is_terminal = getattr(device, 'is_terminal', False)
        for w in (self._lblTermTxLabel, self.lblTermTx,
                  self._lblTermRxLabel, self.lblTermRx,
                  self._lblTermOvLabel, self.lblTermOv):
            w.setVisible(is_terminal)
        if is_terminal and self._client is not None:
            vsp = self._client.get_virtual_serial(device.logical_addr)
            if vsp is not None:
                self.lblTermTx.setText(str(getattr(vsp, 'tx_bytes', '—')))
                self.lblTermRx.setText(str(getattr(vsp, 'rx_bytes', '—')))
                self.lblTermOv.setText(str(getattr(vsp, 'overflow_count', '—')))
            else:
                self.lblTermTx.setText("(未开启)")
                self.lblTermRx.setText("(未开启)")
                self.lblTermOv.setText("(未开启)")

        # 切换到节点详情 Tab
        self.tabRight.setCurrentWidget(self.tabNodeDetail)

    @Slot(bytes)
    def _on_topology_node_selected(self, uid: bytes) -> None:
        """TopologyView 点击 → 联动 DeviceTreeView。"""
        self._device_tree.select_device(uid)

    # ──────────────────────────────────────────
    # 状态栏更新
    # ──────────────────────────────────────────

    @Slot(str, str)
    def _on_status(self, text: str, color: str) -> None:
        self.labelStatus.setText(text)
        self.labelStatus.setStyleSheet(f"color: {color};")
        self.statusBar.showMessage(text.lstrip("● ").strip())

    # ──────────────────────────────────────────
    # 关于
    # ──────────────────────────────────────────

    @Slot()
    def _on_about(self) -> None:
        QMessageBox.about(
            self, "关于 fibre2uart",
            "<b>fibre2uart v1.0</b><br>"
            "光纤通信管理工具<br><br>"
            "Python 3 + PySide6<br>"
            "© 2026 陈志鹏",
        )

    # ──────────────────────────────────────────
    # 窗口关闭
    # ──────────────────────────────────────────

    def closeEvent(self, event) -> None:
        self._on_disconnect()
        event.accept()


# ──────────────────────────────────────────────
# 入口
# ──────────────────────────────────────────────

if __name__ == '__main__':
    logging.basicConfig(level=logging.DEBUG)
    app = QApplication(sys.argv)
    app.setStyle("Fusion")

    # 深色主题
    from PySide6.QtGui import QPalette, QColor
    palette = QPalette()
    palette.setColor(QPalette.Window,          QColor(37, 37, 38))
    palette.setColor(QPalette.WindowText,      Qt.white)
    palette.setColor(QPalette.Base,            QColor(30, 30, 30))
    palette.setColor(QPalette.AlternateBase,   QColor(45, 45, 48))
    palette.setColor(QPalette.ToolTipBase,     Qt.black)
    palette.setColor(QPalette.ToolTipText,     Qt.white)
    palette.setColor(QPalette.Text,            Qt.white)
    palette.setColor(QPalette.Button,          QColor(45, 45, 48))
    palette.setColor(QPalette.ButtonText,      Qt.white)
    palette.setColor(QPalette.BrightText,      Qt.red)
    palette.setColor(QPalette.Highlight,       QColor(0, 122, 204))
    palette.setColor(QPalette.HighlightedText, Qt.white)
    app.setPalette(palette)

    win = MainWindow()
    win.show()
    sys.exit(app.exec())
