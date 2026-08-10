# 代码生成计划（自底向上）

## 阶段一：数据模型与纯算法层（无 I/O 依赖）

| 步骤 | 文件 | 内容 | 依赖 | 状态 |
|------|------|------|------|------|
| 1 | `frame/models.py` | `CommandFrame` 数据类 | 无 | ✅ |
| 2 | `protocol/models.py` | `PortPath`、`PortType`、`ModelType` 枚举 | 无 | ✅ |
| 3 | `device/models.py` | `Device`、`BoardInfo`、`NodeInfo` 数据类 | `protocol/models.py` | ✅ |
| 4 | `frame/parser.py` | `FrameParser` 状态机（含 MAX_FRAME_DATA_LEN 保护） | `frame/models.py` | ✅ |
| 5 | `frame/builder.py` | `FrameBuilder` | `frame/models.py` | ✅ |
| 6 | `frame/test_frame.py` | 自测：构建/解析/校验失败丢帧/len 超限 | 步骤 1-5 | ✅ |
| 7 | `protocol/codec.py` | `CommandCodec` 编解码 | `frame/models.py`、`protocol/models.py` | ✅ |
| 8 | `protocol/test_protocol.py` | 自测：编解码对称性/PortPath 拼接 | 步骤 7 | ✅ |

---

## 阶段二：会话与路由层（有状态、无硬件）
-
| 步骤 | 文件 | 内容 | 依赖 | 状态 |
|------|------|------|------|------|
| 9 | `session/seq_manager.py` | `SeqManager`（threading.Lock 保护 allocate） | 无 | ✅ |
| 10 | `session/session_manager.py` | `SessionManager`、`PendingRequest`（含 connection_generation 代际） | 步骤 7、9 | ✅ |
| 11 | `session/test_session.py` | 自测：seq 分配/超时 reject/断连批量 reject/代际校验 | 步骤 9-10 | ✅ |
| 12 | `routing/table.py` | `PCLogicRoutingTable`、`PCUIDRoutingTable` | `protocol/models.py` | ✅ |
| 13 | `device/manager.py` | `DeviceManager`、`DeviceTree` | `device/models.py` | ✅ |
| 14 | `device/test_device.py` | 自测：树建立/节点查找/logicAddr 分配 | 步骤 12-13 | ✅ |

---

## 阶段三：事件与指令层

| 步骤 | 文件 | 内容 | 依赖 | 状态 |
|------|------|------|------|------|
| 15 | `events/bus.py` | `EventBus`（WeakMethod、`_FuncWrapper`、startup_buffer、mark_ready 原子切换） | 无 | ✅ |
| 16 | `events/test_events.py` | 自测：订阅/弱引用失效自动移除/mark_ready 乱序/startup_buffer replay | 步骤 15 | ✅ |
| 17 | `commands/system.py` | `SysCmdHandler`（0x21~0x26） | 步骤 10 | ✅ |
| 18 | `commands/port.py` | `PortCmdHandler`（0x31~0x36） | 步骤 10 | ✅ |
| 19 | `commands/upgrade.py` | `UpgradeCmdHandler`（0x11~0x15）+ 升级状态机 + `_upgrade_deadline` 全局超时 | 步骤 10、15 | ✅ |
| 20 | `commands/test_commands.py` | 自测：指令编码/升级状态机流转/全局超时触发 | 步骤 17-19 | ✅ |

---

## 阶段四：传输与路由发现层（涉及线程 / I/O）

| 步骤 | 文件 | 内容 | 依赖 | 状态 |
|------|------|------|------|------|
| 21 | `transport/serial_transport.py` | `SerialTransport`（发送队列线程、rx/tx/err 计数、`close()` 批量清理 Future + EventBus） | 步骤 10、15 | ✅ |
| 22 | `transport/test_serial_transport.py` | 自测：Mock 串口收发/断连事件/批量清理验证 | 步骤 21 | ✅ |
| 23 | `routing/discovery.py` | `DeviceDiscovery`、`DiscoveryWorker`（4 阶段 A/B/C/D + PriorityQueue + _addr_pool + BFS TopologyMonitor）| 步骤 10、12、13、15、17-18 | ✅ |
| 24 | `routing/test_routing.py` | 自测：Mock 0x24 回复/建表/增量发现/RS485 补充探测 | 步骤 23 | ✅ |

---

## 阶段五：虚拟串口层

| 步骤 | 文件 | 内容 | 依赖 | 状态 |
|------|------|------|------|------|
| 25 | `virtual_serial/port.py` | `VirtualSerialPort`（PTY 非阻塞、overflow_count、graceful close 4 步） | 步骤 12、21 | ✅ |
| 26 | `virtual_serial/manager.py` | `VirtualSerialManager`（create/close/dispatch、UnclaimedDataEvent guard） | 步骤 25、15 | ✅ |
| 27 | `virtual_serial/test_virtual_serial.py` | 自测：PTY 读写/dispatch 分发/未开启端口 guard | 步骤 25-26 | ✅ |

---

## 阶段六：应用层 API

| 步骤 | 文件 | 内容 | 依赖 | 状态 |
|------|------|------|------|------|
| 28 | `api.py` | `FibreNetworkClient`（统一对外入口，组装所有模块） | 全部 | ✅ |

---

## 阶段七：UI 层

| 步骤 | 文件 | 内容 | 依赖 | 状态 |
|------|------|------|------|------|
| 29 | `ui/fibre2uart.ui` | Qt Designer 布局文件（参照 eloader.ui 格式） | 无 | ✅ |
| 30 | `ui/ui.py` | `pyside6-uic` 生成（纯布局，不含业务逻辑） | 步骤 29 | ✅ |
| 31 | `ui/log_viewer.py` | `LogViewer`（5000 行上限、级别过滤、线程安全 invokeMethod） | 步骤 15 | ✅ |
| 32 | `ui/device_tree.py` | `DeviceTreeView`（tx/err/版本号/虚拟串口号，订阅 DeviceFoundEvent） | 步骤 13、15 | ✅ |
| 33 | `ui/route_table.py` | `RouteTableView`（logicAddr → PortPath 表格） | 步骤 12 | ✅ |
| 34 | `ui/terminal_list.py` | `TerminalPortList`（overflow_count 展示、虚拟串口状态） | 步骤 26 | ✅ |
| 35 | `ui/topology_view.py` | `TopologyView`（QGraphicsView、节点-边图、nodeSelected 信号双向联动） | 步骤 13、15 | ✅ |
| 36 | `ui/command_panel.py` | `CommandPanel`（单指令发送、200ms QTimer 防重入） | 步骤 10 | ✅ |
| 37 | `ui/upgrade_panel.py` | `UpgradePanel`（进度条、3 次自动重试、QMessageBox 错误码弹窗） | 步骤 19、15 | ✅ |
| 38 | `ui/main_window.py` | `MainWindow`（继承 `ui.py`、组装所有组件、nodeSelected 双向联动） | 步骤 30-37 | ✅ |

---

## 依赖关系图

```
frame ──────────────────────────────────────────────────────────────────┐
protocol ───────────────────────────────────────────────────────────────┤
         │                                                              │
         ▼                                                              │
       session ──────────────────────────────────────────────────────┐ │
       routing/table ────────────────────────────────────────────┐   │ │
       device/manager ───────────────────────────────────────┐   │   │ │
                                                             │   │   │ │
events ──────────────────────────────────────────────────┐  │   │   │ │
commands ────────────────────────────────────────────┐   │  │   │   │ │
                                                     │   │  │   │   │ │
                                                     ▼   ▼  ▼   ▼   ▼ ▼
transport ──────────────────────────────────────► routing/discovery
                                                         │
                                                         ▼
                                               virtual_serial
                                                         │
                                                         ▼
                                                       api.py
                                                         │
                                                         ▼
                                                        UI
```

---

## 自测规范

- 每个 `test_xxx.py` 包含 `if __name__ == '__main__':` 入口，可独立运行，无需真实硬件
- UI 组件自测内嵌在各文件的 `__main__` 块中，注入模拟数据验证渲染
- 每步完成后运行自测通过，再进入下一步

## 进度标记说明

| 符号 | 含义 |
|------|------|
| ⬜ | 未开始 |
| 🔄 | 进行中 |
| ✅ | 已完成并通过自测 |
| ❌ | 自测未通过，需修复 |
