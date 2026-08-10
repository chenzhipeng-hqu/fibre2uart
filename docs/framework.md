[toc]

# 通信框架设计

> 依据 `/home/chenzhipeng3472/mine/codes/mcu/gd32/codes/fibre2uart/doc/07.光纤板通信协议.md` 设计

---

## 零、产品背景

PC 使用 1 个串口连接一台光纤通信板，产品可级联扩展，PC 端将每个终端口（485/CAN/232）虚拟成独立串口进行收发操作，并将数据解析成光纤通信协议格式由实际物理串口发送到光纤通信板。

### 技术栈

| 类别 | 选型 |
|------|------|
| 语言 | Python 3 |
| UI 框架 | PySide6（Qt for Python） |
| 串口通信 | pyserial |
| 日志 | `project/log.py`（`Log` 类，封装 `logging`，同时输出到文件 `log/<name>.YYYY_MM_DD.log` 和控制台，统一格式 `[时间] 文件->函数:行 [级别] 消息`；日志目录总大小超过 1 GB 时自动删除日期最早的日志文件） |

### 硬件型号

| 型号 | 输入 | 输出 | 说明 |
|------|------|------|------|
| 光纤通信板 (PC_FIBRE) | 1 串口 | 6 光纤串口 | PC 直接连接，根节点 |
| 光纤桥接板 (BRIDGE_FIBRE) | 1 光纤串口 | 6 光纤串口 | 居中转发节点 |
| 光纤转485板 (FIBRE_485) | 1 光纤串口 | 6 个端口 (2×232或485 / 2×CAN或485 / 2×485 可配置) | 可能是终端节点 |
| 485桥接板 (BRIDGE_485) | 1 个 485 | 6 个端口 (1空闲 / 2×232或485 / 2×CAN或485 / 1×485 可配置) | 必定是终端节点 |

### 终端口判断规则

```
is_terminal_port(port_type, has_children) -> bool:
    # 条件 1：非光纤端口即为终端口
    if port_type == PORT_232 or port_type == PORT_CAN:
        return True
    # 条件 2：485 端口且没有下一级节点（可能是未连设备的空光纤口被 485 坚占的情况）
    if port_type == PORT_485 and not has_children:
        return True
    return False
```

### logicAddr 分配规则

```
节点设备（中继节点）： logicAddr 从 0x801 开始递增，根节点为 0x801
终端口（非光纤端口）： logicAddr 从 0x001 开始递增（按发现顺序）
```

### 终端口 uid 规则

终端口无真实 UUID（硬件返回全零 `bytes(12)`），`_write_and_publish` 时通过以下公式生成合成 uid：

```
uid = hashlib.sha1(str(list(port_path.ports)).encode()).digest()[:12]
```

合成 uid 仅在 PC 侧 `DeviceManager` / `TopologyView` / `TerminalPortList` 等组件内部用于幂等去重，不写入路由表（`PCUIDRoutingTable` 只存真实 uuid）。

---

## 一、协议要点梳理

### 1.1 帧类型

| SOF | 方向 | 校验 |
|-----|------|------|
| `0xAA` | PC → MCU | SUM(seq~dataN) |
| `0xAB` | MCU → PC | SUM(seq~dataN) |

### 1.2 帧结构

```
SOF(1) | check(1) | seq(1) | len(1) | cmd(1) | portLen(1) | port[0~N] | data[0~N]
```

- `seq`：去重标识，连续相同 seq 只处理一次
- `len`：**整帧总字节数**（SOF 到最后一个 data 字节，含所有固定头字段）；最小值 6（仅含固定头，无 ports 无 data）；计算公式：`len = 6 + portLen + dataLen`
- `cmd`：指令类型
  - `0`：none
  - `1~4`：消息透传（1=uart, 2=rs485, 3=rs232, 4=can），cmd<0x10
  - `0x11~0x1F`：升级指令
  - `0x21~0x2F`：系统指令
  - `0x31~0x3F`：端口指令
- `portLen`：后续 port 字段的字节数
- `port`：0 个或多个路由跳，每字节 `0x01~0x7F`（下级端口号或 RS485 地址），`0x00` 表示广播

#### portPath 举例

```
# 无 485 中间节点：经过3级光纤转发后到达终端口
[port0, port1, port2]
  port0 = 光纤通信板的某光纤口编号
  port1 = 光纤桥接板的某光纤口编号
  port2 = 光纤转485板的某终端口编号

# 有 485 中间节点：经过光纤后再经 485 桥接板转发
[port0, port1, rs485Addr, port3]
  port0    = 光纤通信板的光纤口
  port1    = 光纤转485板的某 485 端口
  rs485Addr = 485 总线上桥接板的 RS485 地址（1~0x7F），仅可出现在倒数第二个位置
  port3    = 485桥接板的某终端口编号
```

nodes.json 中每条路由条目增加 `"has485"` 字段，标识 portPath 中是否含 RS485 中间节点（用于快速区分路径类型，无需遍历元素）：

```json
// 无 485 节点（portPath 全为端口索引）
{"logical_addr": "0x0001", "port_path": [0, 1, 2], "has485": false}

// 有 485 节点（倒数第二位为 rs485Addr）
{"logical_addr": "0x0002", "port_path": [0, 1, 5, 3], "has485": true}
```

### 1.3 帧模式说明

```
单节点指令：cmd >= 0x10, portLen == 0           → 本节点处理
转发指令：  cmd >= 0x10, portLen > 0            → 按 port 路径转发
广播指令：  cmd >= 0x10, portLen == 1, port=0x00 → 执行后,广播给所有下级
转发消息：  cmd < 0x10,  portLen > 0            → 按 port 路径透传消息
广播消息：  cmd < 0x10,  portLen > 0, port=0x00  → 广播透传消息
```

### 1.4 端口类型（PortType）

| 值 | 类型 |
|----|------|
| 0 | none |
| 1 | PORT_PC |
| 2 | PORT_UART |
| 3 | PORT_FIBRE |
| 4 | PORT_232 |
| 5 | PORT_485 |
| 6 | PORT_CAN |

### 1.5 设备型号（ModelType）

| 值 | 型号 |
|----|------|
| 0 | BRIDGE_485 |
| 1 | FIBRE_485 |
| 2 | UNKNOWN |
| 3 | BRIDGE_FIBRE |
| 0x13 | PC_FIBRE |

---

## 二、分层架构

```
┌─────────────────────────────────────────────────────┐
│                      UI Layer                         │
│   DeviceTreeView — 节点树 + 路由表展示           │
│   UpgradePanel — 升级操作面板                  │
├─────────────────────────────────────────────────────┤
│                  Virtual Serial Layer                  │
│  VirtualSerialManager — 终端口 ↔ 虚拟串口         │
│  VirtualSerialPort — 模拟标准串口读写接口     │
├─────────────────────────────────────────────────────┤
│                  Application Layer                     │
│    FibreNetworkClient — 统一对外 API                 │
├──────────────────────┤──────────────────────────────┤
│   Command Layer      │      Event Layer              │
│ UpgradeCmdHandler    │   EventBus (Pub/Sub)          │
│ SysCmdHandler        │   DeviceOnline / Offline      │
│ PortCmdHandler       │   UpgradeProgress             │
├──────────────────────┴──────────────────────────────┤
│                   Session Layer                      │
│  SeqManager — 序列号分配 & 去重                       │
│  SessionManager — 请求/响应配对 & 超时                 │
├─────────────────────────────────────────────────────┤
│                   Routing Layer                      │
│  PCLogicRoutingTable — logicAddr → PortPath         │
│  PCUIDRoutingTable — UID → PortPath (备用)         │
│  DeviceDiscovery — 递归 0x24 建表（portLen=0 起）     │
├─────────────────────────────────────────────────────┤
│                   Protocol Layer                     │
│  CommandCodec — 统一帧编解码                          │
│  PortPath — 端口路径抽象                              │
├─────────────────────────────────────────────────────┤
│                    Frame Layer                       │
│  FrameParser — 字节流状态机，输出帧对象               │
│  FrameBuilder — 高层参数 → 字节流                    │
│  CommandFrame 数据模型                               │
├─────────────────────────────────────────────────────┤
│                  Transport Layer                     │
│  SerialTransport — 串口读写、异步事件                 │
└─────────────────────────────────────────────────────┘
```

---

## 三、模块详细设计

### 3.1 Transport Layer — 传输层

**职责**：管理物理串口，提供字节流的收发接口。

| 组件 | 说明 |
|------|------|
| `SerialTransport` | 封装 pyserial；维护独立接收线程，收到数据后通过回调向上传递 |

**接口**：

```
open(port, baudrate)
close(emit_event=True)
                  # 主动关闭（emit_event=False）：停止收发线程 → 关闭物理串口，不发布任何事件，不触发重连
                  # 被动断连（emit_event=True，由内部线程调用）：同上，并发布 TransportLostEvent 触发批量清理
send(data: bytes) -> None   # 将字节投入内部发送队列，立即返回；不等待设备响应
                            # 响应通过接收线程 → on_data_received 回调链路异步到达
on_data_received: Callable[[bytes], None]   # 注册回调
```

**要点**：
- 接收线程持续 read，数据放入内部 ring-buffer，再回调 FrameParser
- **"发送"与"等待响应"分离**：`send()` 只投队列（`-> None`），单一发送线程串行消费队列发帧；等待响应的 `Future` 由上层 `SessionManager.send_request()` 管理，串口层无竞争
- 写操作线程安全：所有发送请求入队后由发送线程串行消费，禁止多线程直接调用底层串口写接口
- **通信计数**：维护 `tx_count`（发送帧数）、`rx_count`（接收帧数）、`err_count`（校验失败/丢帧数）三个原子计数器，供 UI 展示每节点链路质量
- **通信消息 DEBUG 日志**：每一条收发帧在 DEBUG 级别下输出到 log，内容包含方向（TX/RX）、seq、cmd、hex 数据和时间戳，便于低层通信诊断；INFO 及以上级别不输出，避免生产日志过量
- **传输层关闭时批量清理**：`close()` 或被动断连发布 `TransportLostEvent` 后：
  - `SessionManager.on_transport_lost()` 将所有挂起 `Future` 批量设为 `TransportLostError`
  - `EventBus` 若尚未 `mark_ready()`，清空 `_startup_buffer`，防止孤立事件积压

#### USB 连接管理与热插拔检测

两个全局标志位贯穿整个生命周期：

| 标志位 | 初始值 | 置 True 时机 | 置 False 时机 |
|--------|-------|------------|-------------|
| `_route_ready` | False | 首次 FULL 发现完成（EventBus.mark_ready() 调用后） | 永不重置 |
| `_discovering` | False | DiscoveryWorker 开始执行任务 | 任务结束（try/finally 保证） |

```
USB 断连被动检测：
  SerialTransport 的发送线程或接收线程捕获 serial.SerialException / OSError
    → 立即关闭串口
    → 若 _route_ready = False（首次路由尚未完成）：
        仅记录日志，不发布 TransportLostEvent；等待程序重试/重启后重新执行全量发现
    → 若 _route_ready = True：
        发布 TransportLostEvent
        → 所有虚拟串口挂起（write 操作进入等待队列）
        → UI 展示"USB 断开，正在重连..."提示

  生成路由表期间（_discovering = True）：
    若 USB 断连 → 中断当前发现任务，等待重连后由 discovery_queue 触发新 FULL 任务

重连循环（每 1s 尝试一次；仅在 `hotplug=True` 且 `_route_ready=True` 后激活）：
    → 尝试 SerialTransport.open(port, baudrate)
    → 成功后发布 TransportRestoredEvent
        → discovery_queue.put(DiscoveryTask(FULL, reason="reconnect"))
        → DiscoveryWorker 执行缓存增量验证，完成后恢复虚拟串口等待队列
        → UI 清除重连提示，刷新设备树

热插拔检测（TopologyMonitor 线程）：
  仅在 connect(hotplug=True) 且 _route_ready=True 后才启动；hotplug=False（默认）时不启动
  轮询间隔 3s：
    若 _discovering = True（DiscoveryWorker 正在执行）→ 跳过本次（避免干扰发现流量）
    否则：发 0x24（portLen=0）查询根节点 portTypes
      portTypes 与缓存完全一致 → 无拓扑变化，继续等待下一轮
      portType 变化或出现新 uuid → discovery_queue.put(DiscoveryTask(INCREMENTAL, port_path))
      uuid 不再响应（超时）     → 发布 DeviceOfflineEvent
                                  → 从路由表移除该节点，关闭对应虚拟串口
```

---

### 3.2 Frame Layer — 帧层

**职责**：在原始字节流与结构化帧对象之间转换；负责 SOF 检测与校验。

#### 数据模型

```
CommandFrame:
    sof: int           # 0xAA (PC→MCU) / 0xAB (MCU→PC)
    check: int
    seq: int
    cmd: int           # 0=消息, 0x11~0x1F=升级, 0x21~0x2F=系统, 0x31~0x3F=端口
    port_len: int      # 后续 port 字段字节数
    ports: List[int]   # 路由路径（0x01~0x7F）；0x00 表示广播
    data: bytes
```

#### FrameParser（状态机）

```
[PC 端解析器]
状态流转：
WAIT_SOF
  → 检测到 0xAB (MCU→PC) → READ_CHECK   # 只接受 MCU 发过来的帧

READ_CHECK → READ_SEQ → READ_LEN → READ_CMD → READ_PORT_LEN
  → READ_PORTS(port_len 字节) → READ_DATA → VALIDATE → EMIT_FRAME
```

- 校验失败：丢帧，回到 WAIT_SOF
- 校验方式：SUM 校验（seq 到 dataN 的字节累加低 8 位）
- **最大帧长保护**：`READ_LEN` 状态完成后检查 `len > MAX_FRAME_DATA_LEN (= 260)`；
  超出上限 → 发布 `FrameErrorEvent(raw, "len_overflow")` + 回到 `WAIT_SOF`，
  防止噪声污染 len 字段后解析器长时间停留错误状态、丢弃后续合法帧

#### FrameBuilder

```
build_tx(seq, cmd, ports, data) -> bytes   # sof=0xAA (PC→MCU), portLen/check 自动计算
build_rx(seq, cmd, ports, data) -> bytes   # sof=0xAB (MCU→PC), 用于仿真/测试
```

---

### 3.3 Protocol Layer — 协议层

**职责**：在帧字段与业务语义之间转换；管理端口路径抽象。

#### PortPath

```
PortPath(ports: List[int])
    # 示例: PortPath([1, 3])     → 经端口1 → 端口3 路由
    # 示例: PortPath([0x00])     → 广播
    # 示例: PortPath([])         → 单节点（本机）
```

#### CommandCodec

```
encode(seq, cmd, port_path: PortPath, data) -> bytes   # 构建统一帧
decode(frame: CommandFrame) -> (cmd, PortPath, bytes)
```

---

### 3.4 Session Layer — 会话层

**职责**：seq 号分配与去重；请求-响应配对；超时管理。

#### SeqManager

```
allocate() -> int
    # 循环分配 0~255，内部使用 threading.Lock 保护
    # 多线程同时调用（升级线程/发现线程/CommandPanel）时互斥，保证 seq 不碰撞
is_duplicate(port_path, seq) -> bool          # 相同来源相同 seq → 重复
```

#### PendingRequest

```
seq: int
port_path: PortPath
sent_at: float             # 时间戳
future: Future             # 调用方等待的 Future
timeout: float             # 默认 500ms，可配置
connection_generation: int # 注册时的连接代际，收帧时校验，防止重连后旧帧误 resolve
```

#### SessionManager

```
_connection_generation: int = 0      # 每次 on_transport_lost() 自增

send_request(port_path, data, timeout) -> Future[ResponseFrame]
    1. 分配 seq（SeqManager.allocate，内部加锁，多线程安全）
    2. 编码帧
    3. 通过 Transport 发送
    4. 注册 PendingRequest（含当前 connection_generation；weakref 持有 Future）

on_frame_received(frame: CommandFrame) -> None
    1. 解析 seq
    2. 查找 PendingRequest；若 pending.connection_generation ≠ _connection_generation
       → 丢弃（重连前的残留帧），避免旧数据错误 resolve 新请求
    3. resolve Future 或忽略（无匹配）

_timeout_checker()          # 后台定时器，清理超时请求并 reject Future

on_transport_lost()         # 订阅 TransportLostEvent
    → _connection_generation += 1   # 使所有已注册 PendingRequest 的代际失效
    → 遍历所有挂起 Future，统一调用 .set_exception(TransportLostError())
    → 批量清理 seq→Future 映射表
```

---

### 3.5 Routing Layer — 路由层

**职责**：维护路由表；提供 `logicAddr → PortPath` 的查找；驱动设备发现流程。

#### PCLogicRoutingTable（PC 端主路由表）

```
Map: logicAddr(uint16) → PortPath
add(logical_addr, port_path)
lookup(logical_addr) -> PortPath | None   # 发送时按逻辑地址解析为路由路径
remove(logical_addr)
clear()                                   # 重新发现时清空
```

#### PCUIDRoutingTable（UID 备用表）

```
Map: UID(bytes) → PortPath
add(uid, port_path)
lookup(uid) -> PortPath | None
```

#### DeviceRoutingTable（各节点本级路由）

```
Map: UID(bytes) → (forward_port: int, rs485_addr: int | None)
```

#### DeviceDiscovery（路由建立流程）

路由表生成流程（仅使用 0x24 指令递归，不需要 0x23）：

**执行模型**：`discovery_queue` 使用 `queue.PriorityQueue`，由单一 **DiscoveryWorker** 线程串行消费；FULL 任务优先级高于 INCREMENTAL，ScanButton 投递 FULL 时同时清空队列中所有待执行的 INCREMENTAL 任务，保证用户扫描命令即时响应。

```python
@dataclass
class DiscoveryTask:
    mode: Literal["FULL", "INCREMENTAL"]
    root_port_path: PortPath  # FULL=[], INCREMENTAL=[port, ...]
    reason: str               # "startup" | "scan" | "reconnect" | "hotplug"
    priority: int = 0         # 0=FULL（最高优先级），1=INCREMENTAL
    def __lt__(self, other): return self.priority < other.priority
```

**投递方**（只投递，不持锁）：
- 程序启动 → 投递 `FULL` 任务（reason="startup"）
- ScanButton 点击 → 投递 `FULL` 任务（reason="scan"，先清空队列中旧任务）
- USB 重连 → 投递 `FULL` 任务（reason="reconnect"，使用缓存增量验证）
- TopologyMonitor 发现变化 → 投递 `INCREMENTAL` 任务（reason="hotplug"）

**DiscoveryWorker 执行流程（4 个阶段）**：

```
任务开始 → 置 _discovering = True（TopologyMonitor 本轮跳过，USB断开检测进入发现模式）
  try:

阶段 A  信息收集（I/O 密集，多线程并行）

  A1. PORT_FIBRE 端口——同层并行收集：
      ThreadPoolExecutor(max_workers=10)
      每个线程仅执行：发 0x24 → 收 NodeInfo{uuid, model, portTypes}
      禁止在线程内写 logicAddr 或路由表（只读操作）
      汇总阶段对每个 Future 单独 try/except：
        成功 → 加入节点树
        异常（超时/解包失败）→ 记录 ERROR + 发布 PortDiscoveryFailedEvent(port_path) + 跳过该子树

  A2. PORT_485 端口——串行轮询（半双工约束）：
      轮询顺序：优先使用 nodes.json 缓存的已知地址列表（跳过无设备地址段）；
                缓存为空时全量轮询 1~0x7F（覆盖未知设备）
      每个地址：超时 50ms，严格「一发一收」
      有回复 → 记录为子节点 NodeInfo，继续递归 A1/A2
      无回复 → 标记为终端口（从缓存中移除该地址，下次跳过）
      补充探测（防止新接入设备漏发现）：
        缓存非空时，从 1~0x7F 中随机抽取 ≤8 个非缓存地址执行额外探测；
        有回复 → 将新地址加入缓存，记录子节点，追加到本次发现树；
        （保证新插入 RS485 设备即使在增量任务中也能被及时检测到）

  A3. PORT_232 / PORT_CAN 端口：
      直接标记为终端口，记录 portPath

  A 阶段完成每个节点后（logicAddr 尚未分配）立即发布：
      EventBus.publish(NodeDiscoveredEvent(port_path, uuid, model, port_types))
      → DeviceTreeView 立即显示「[发现中] <uuid 前 8 字节>」占位行，Phase C 收到
        DeviceFoundEvent 后将其更新为实际逻辑地址与设备信息（两阶段渲染）

阶段 B  logicAddr 分配（CPU 密集，单线程 DFS，无锁竞争）

  DiscoveryWorker 主线程对阶段 A 收集到的树做 DFS 遍历：
    节点设备（中继节点）：logicAddr 从 0x801 起，使用 itertools.count(0x801)
    终端口：            logicAddr 从 0x001 起，使用 itertools.count(0x001)
  INCREMENTAL 模式：优先从 _addr_pool（离线节点归还的地址集合）复用地址；
    pool 为空时才扩展至新地址；_addr_pool 随 nodes.json 持久化
  设备消失时（INCREMENTAL 发现子树中 uuid 无响应）：
    DiscoveryWorker 在发布 DeviceOfflineEvent 之前直接调用 _addr_pool.add(logicAddr)；
    无需通过 EventBus 事件订阅触发归还，避免循环依赖与归还时机不确定的问题

阶段 C  写入 logicAddr（逐节点下发）

  向每个节点发 cmd=0x36 写入分配好的 logicAddr
  终端口无真实 UUID（uuid=bytes(12)）→ 合成 uid：sha1(port_path.ports)[:12]
      目的：TopologyView / TerminalPortList / DeviceManager 均以 uid 做幂等去重，
            若所有终端口共用全零 uid，则只有第一个终端口会被渲染/注册，其余被跳过；
            合成 uid 保证每个终端口拥有唯一标识符，非终端真实 uuid 不受影响。
  发布 DeviceFoundEvent(device) → EventBus（UI 节点树实时增量刷新）
      DeviceTreeView：用 port_path 匹配到 Phase A 的占位行，更新为真实信息
      TopologyView / TerminalPortList：以合成 uid 作 key，幂等插入节点/行
      UpgradePanel：订阅 DeviceFoundEvent / DeviceOfflineEvent 自动维护目标下拉框，
                    不再依赖点击设备树触发添加（避免重复添加 bug）

阶段 D  持久化（原子写入）

  PCLogicRoutingTable.add(logical_addr, port_path, has485)
  PCUIDRoutingTable.add(uid, port_path)
  序列化 → datas/nodes_tmp.json（含 "version":"20260317"）
  os.replace("datas/nodes_tmp.json", "../datas/nodes.json")
  _discovery_error = None   # 正常完成，无异常

  except Exception as e:
    _discovery_error = e    # 记录异常，不在此处 raise（finally 统一处理）
    记录 ERROR 日志

  finally:（无论正常/异常均执行）
    若 _discovery_error 且 reason == "startup"：
      EventBus.publish(DiscoveryFailedEvent(reason=str(_discovery_error)))
      # UI 展示"设备发现失败：<原因>"提示，而非空白界面
    若 FULL 模式（reason="startup"）且尚未 mark_ready：
      EventBus.mark_ready()（保证 startup_buffer 无论正常/异常都被 replay）
      置 _route_ready = True
      注册 TransportLostEvent 处理器（启动 USB 断连重连循环）
      启动 TopologyMonitor 线程
    其他 FULL / INCREMENTAL 正常完成时：
      EventBus.publish(RouteTableUpdatedEvent)
    置 _discovering = False（恢复 TopologyMonitor 轮询 + USB 断开检测正常模式）
```

**启动时缓存加载流程（FULL reason="startup"）**：

```
加载 ../datas/nodes.json，校验 version 字段：
  不匹配 → 丢弃缓存，记录日志，直接执行阶段 A~D（全量发现）
  匹配 → 还原路由表，对每个缓存节点发 0x24 验证 uuid 和 portTypes：
    uuid + portTypes 均不变 → 复用缓存（跳过该子树的阶段 A）
    有变化或无回复        → 对该子树重新执行阶段 A~D
  验证完成后：若响应节点数 > 缓存节点数（发现新接入设备）
    → 记录 WARNING 日志，丢弃缓存，触发完整阶段 A~D（全量重发现）
```

**热插拔增量流程（INCREMENTAL reason="hotplug"）**：

```
仅对 root_port_path 指定的变化端口子树执行阶段 A~D：
  新出现设备 → 分配新 logicAddr，创建虚拟串口
  消失设备   → 发布 DeviceOfflineEvent，从路由表删除，关闭虚拟串口
  全表其余节点不受影响
```

---

### 3.6 Command Layer — 指令层

#### UpgradeCmdHandler（cmd=0x11~0x15）

| 方法 | cmd |
|------|-----|
| `jump_program(port_path, target)` | 0x11：1=bootloader, 2=app |
| `get_board_info(port_path) -> BoardInfo` | 0x12：返回程序位置/日期/hw/model/uuid |
| `send_upgrade_info(port_path, file_size)` | 0x13：触发擦除 |
| `send_upgrade_data(port_path, sn, data)` | 0x14：分包发送，广播间隔 50ms |
| `query_upgrade_progress(port_path) -> int` | 0x15：返回 0~100 |

#### SysCmdHandler（cmd=0x21~0x26）

| 方法 | cmd |
|------|-----|
| `reset(port_path)` | 0x21：系统复位 |
| `get_set_mfg_date(port_path, date=None)` | 0x22：读取/设置出厂日期 |
| `scan_nodes(port_path)` | 0x23：扫描设备节点 |
| `get_node_info(port_path) -> NodeInfo` | 0x24：获取 uuid/model/端口类型 |
| `get_clear_status(port_path, clear) -> Status` | 0x25：查询/清除状态 |
| `control_io(port_path, io_mask, mode, value)` | 0x26：控制 IO |

#### PortCmdHandler（cmd=0x31~0x36）

| 方法 | cmd |
|------|-----|
| `port_reset(port_path, port_no)` | 0x31：端口复位 |
| `port_power(port_path, port_no, on)` | 0x32：端口供电 |
| `port_config(port_path, port_no, baud, parity, stopbit)` | 0x33：波特率/校验/停止位 |
| `set_uart_filter(port_path, port_no, ...)` | 0x34：串口过滤器 |
| `set_can_filter(port_path, port_no, filter_id, can_id)` | 0x35：CAN 过滤器 |
| `set_logical_addr(port_path, port_no, addr)` | 0x36：端口逻辑地址 |

---

### 3.7 Device Layer — 设备层

#### Device（数据模型）

```
Device:
    uid: bytes             # 12字节 UUID
    model: ModelType       # BRIDGE_485/FIBRE_485/BRIDGE_FIBRE/PC_FIBRE...
    port_type: PortType    # 该设备接入父节点的端口类型（由父节点 port_types[端口号-1] 推断并写入）
    port_path: PortPath    # 到达该设备的完整路由路径
    logical_addr: int      # 逻辑地址：节点从 0x801起，终端口从 0x001起
    is_terminal: bool      # True: 终端口（portType != PORT_FIBRE）
    port_types: List[PortType]  # 本设备 6 个端口的类型（来自 0x24 NodeInfo）
                                # index 0~5 对应物理端口 1~6；NONE(0) 表示未配置/空闲
    children: List[Device] # 6个子端口对应的子设备
    parent: Optional[Device]
    board_info: BoardInfo  # 程序位置/出厂日期/hardware/model
```

#### DeviceManager

```
add_device(device: Device)
remove_device(uid: int)
find_by_uid(uid: int) -> Device | None
find_by_logical_addr(addr: int) -> Device | None
get_device_tree() -> Device   # 返回根节点
```

---

### 3.8 Event Layer — 事件层

```
事件类型：
  NodeDiscoveredEvent (port_path, uuid, model, port_types)  # Phase A 立即发布（logicAddr 尚未分配）
  DeviceFoundEvent    (device: Device)                      # Phase C 分配 logicAddr 后发布
  DeviceOfflineEvent  (uid: bytes)
  UpgradeProgressEvent(uid: int, channel: int, progress: int)
  SubDeviceReportEvent(uid: int, error_code: int)
  FrameErrorEvent     (raw: bytes, reason: str)

EventBus:
  subscribe(event_type, callback)
      # 类型分支持有弱引用，防止不同类型 callback 静默失效：
      #   bound method（实例方法）→ weakref.WeakMethod(callback)
      #   普通函数 / lambda / partial → 包装成 _FuncWrapper 对象，再 weakref.ref(_FuncWrapper)
      # 回调对象被 GC 后，死亡引用自动从订阅列表移除，无需手动 unsubscribe
  unsubscribe(event_type, callback)
  publish(event)
      # dispatch 时若弱引用已失效 → 记录 DEBUG 日志并从订阅列表移除
      # 禁止在回调中直接操作 Qt Widget（见 §3.11）

  # 启动缓冲区（deque）：发现完成前缓存所有事件，零丢失
  _startup_buffer: deque[Event]  # 启动阶段积压队列
  _ready: bool = False           # 标志位：所有订阅者已注册

  mark_ready()                   # DiscoveryWorker finally 块保证一定被调用
      # 原子切换 + 锁外 dispatch，防止 replay 期间并发 publish 导致事件乱序或重复分发
      with _lock:
          _ready = True
          buffer = list(_startup_buffer)   # 快照
          _startup_buffer.clear()          # 清空，后续 publish 直接 dispatch
      for event in buffer:
          _dispatch(event)       # 在锁外逐条 dispatch，回调内不持锁
      → 之后 publish() 检测 _ready=True，直接 dispatch，不再缓存
```

---

### 3.9 Application API — 应用层

```
FibreNetworkClient:

  # 连接管理
  connect(port: str, baudrate: int = 115200, hotplug: bool = False)
                  # hotplug=False（默认）：禁用热插拔检测和自动重连，TopologyMonitor 不启动
                  # hotplug=True：启用 USB 热插拔检测、自动重连和 TopologyMonitor
  disconnect()    # 主动断开：close(emit_event=False) 不触发重连，直接批量 reject 挂起 Future

  # 设备发现
  discover() -> DeviceTree
  get_device(uid: int) -> Device

  # 板卡信息（封装 UpgradeCmdHandler 0x12）
  get_board_info(logical_addr: int) -> BoardInfo

  # 数据透传（按逻辑地址寻址）
  send_msg(logical_addr: int, msg_type: int, data: bytes) -> Future[bytes]

  # 系统管理（封装 SysCmdHandler，按逻辑地址寻址）
  reset_device(logical_addr: int)                    # 0x21
  set_baudrate(logical_addr: int, baudrate: int)     # 0x33：自动推导父路径+port_no，先读再写

  # 在线升级（封装 UpgradeCmdHandler，驱动状态机）
  upgrade_device(logical_addr: int, firmware: bytes) -> UpgradeJob
  broadcast_upgrade(firmware: bytes) -> UpgradeJob   # 广播升级选中节点

  # 虚拟串口管理
  open_virtual_serial(logical_addr: int, baud_rate: int = 115200) -> str
  close_virtual_serial(logical_addr: int)
  get_virtual_serial(logical_addr: int) -> Optional[VirtualSerialPort]  # 读取 tx_bytes/rx_bytes/overflow_count
  list_virtual_serials() -> List[VirtualSerialPort]

  # 事件订阅
  on(event_type, callback)
```

---

### 3.10 Virtual Serial Layer — 虚拟串口层

**职责**：将每个终端口（portType != PORT_FIBRE）在操作系统层面创建**可被第三方串口软件直接打开**的虚拟串口设备，并在该设备与光纤通信协议之间双向透传数据。

#### 系统级虚拟串口实现方案

| 平台 | 实现方式 | 第三方可见设备 |
|------|---------|--------------|
| Linux | `os.openpty()` 创建 PTY 对（master/slave），slave 端 symlink 到 `/dev/ttyVcom_0x0001` | `/dev/ttyVcom_0x0001`（可用 minicom / 任意软件打开） |
| Windows | 调用 `com0com` 驱动创建虚拟 COM 对，或使用 `pyserial` + Windows Named Pipe | `COM10` / `COM11`…（设备管理器可见） |

> **PTY 原理（Linux）**：`openpty()` 返回 master_fd / slave_fd，程序持有 master_fd 进行读写，第三方软件打开 slave 端路径即可双向通信，OS 负责中转，无需内核驱动。

#### VirtualSerialPort（系统级 PTY / COM 封装）

```
VirtualSerialPort:
    logical_addr: int       # 对应终端口的 logicAddr
    port_type: PortType     # PORT_485 / PORT_232 / PORT_CAN
    baud_rate: int
    is_open: bool
    overflow_count: int     # write_to_pty 因 master_fd 写满（BlockingIOError）丢弃的批次数
    tx_bytes: int           # 第三方→光纤（上行）累计字节数
    rx_bytes: int           # 光纤→第三方（下行）累计字节数

    # Linux: master_fd(int), slave_path(str) 如 /dev/ttyVcom_0x0001
    # Windows: pipe_name(str) 如 \\\.\pipe\vcom_0x001
    device_path: str        # 第三方软件打开此路径/端口名

    open() -> str
        # Linux:
        # 1. 启动时清理残留 symlink（异常退出遗留）
        glob_clean("/dev/ttyVcom_0x*")   # os.path.islink() + os.unlink()
        # 2. 注册 atexit，正常退出时自动删除全部 symlink
        atexit.register(_cleanup_all_symlinks)
        # 3. 创建 PTY 对
        master_fd, slave_fd = os.openpty()
        slave_path = os.ttyname(slave_fd)
        # 4. 对 slave_fd 调用 tty.setraw()，禁用 PTY echo
        #    防止第三方写入的 TX 字节被内核回显，在 master_fd 读端出现虚假 RX 数据
        tty.setraw(slave_fd)
        # 5. 保留 slave_fd（_slave_keepalive_fd）不关闭
        #    防止 master_fd 因无 slave 打开而进入 POLLHUP/EIO 状态，
        #    导致读取线程意外退出；第三方打开 device_path 后与此 slave_fd 共享同一 PTY
        os.symlink(slave_path, f"/dev/ttyVcom_{logical_addr:#06x}")
        os.chmod(slave_path, 0o666)  # 允许任意用户/第三方软件打开 PTY 设备
        # 设为非阻塞，防止第三方进程不消费时阻塞发送链路
        fcntl.fcntl(master_fd, fcntl.F_SETFL, os.O_NONBLOCK)
        # 写满时捕获 BlockingIOError，丢弃包并递增 overflow_count
        启动 _linux_reader_loop 线程  ← 监听第三方写入，转发到光纤
        return device_path

        # Windows:
        调用 com0com API 分配 COMx 虚拟对，持有其中一端
        启动 _pipe_reader_thread
        return "COMx"

    close()
        1. 置 _closing=True，停止 write_to_pty() 写入
        2. 删除 symlink（第三方软件下次 open 失败，感知设备已消失）
        3. 等待最多 500ms（让第三方软件完成当前 I/O）
        4. os.close(_slave_keepalive_fd)；os.close(master_fd)；停止读取线程
        # 若第三方软件仍持有 slave_fd，OS 返回 EIO 是正常行为；
        # 框架通过 symlink 删除 + 等待窗口尽量减少第三方崩溃概率

    # 光纤 → 第三方: 收到设备下行数据后调用
    write_to_pty(data: bytes) -> None
        if _closing: return            # 关闭中，丢弃写入
        rx_bytes += len(data)          # 记录光纤→第三方（下行）字节数
        try:
            os.write(master_fd, data)  # Linux（非阻塞）
            # Windows: pipe.write(data)
        except BlockingIOError:
            overflow_count += 1        # 原子递增，供 TerminalPortList UI 展示
            记录 DEBUG 日志（含 logical_addr + 丢弃字节数）

    # 第三方 → 光纤: _linux_reader_loop 线程循环读取后调用
    _on_pty_data(data: bytes) -> None
        1. 根据 logical_addr 查路由表 → PortPath
        2. 封装消息透传帧（cmd 由 port_type 决定：UART=0x01, 485=0x02, 232=0x03, CAN=0x04）
        3. CommandCodec.encode() → bytes
        4. tx_bytes += len(data)       # 记录第三方→光纤（上行）字节数
        5. SerialTransport.send()

    # Linux 读取线程（PTY master_fd 可读时调用）
    _linux_reader_loop() -> None
        while not _closing:
            select(master_fd, timeout=0.1)
            if readable:
                data = os.read(master_fd, 4096)
                if data: _on_pty_data(data)
            except OSError(EIO):     # slave 端暂无打开者，等待重试（非退出）
                sleep(0.05); continue
            except OSError:          # 其他错误，退出线程
                break

    on_pty_data: Callable[[bytes], None]   # 外部注入的回调（优先级高于内置路由，用于测试）
```

#### VirtualSerialManager

```
create(logical_addr, port_type, baud) -> VirtualSerialPort
    → 构造 VirtualSerialPort 并调用 open()
    → 返回 device_path 供 UI 展示给用户

close(logical_addr)
    → 调用 VirtualSerialPort.close()

dispatch(frame: CommandFrame) -> None
    → 收到 cmd<0x10 的消息帧时，根据 portPath 解析 logicAddr
    → 调用对应 VirtualSerialPort.write_to_pty(data)
      （第三方软件从 slave/pipe 端读到数据）
```

#### 典型使用流程

```
设备发现完成后：
  VirtualSerialManager.create(logical_addr=0x001, PORT_485, 115200)
  → Linux: 创建 /dev/ttyVcom_0x0001  ← minicom/Python/其他工具打开此路径
  → Win:   创建 COM10             ← 串口助手/SCADA 打开 COM10

第三方软件向 /dev/ttyVcom_0x0001 写入 b'\x01\x02\x03':
  PTY master_fd 可读 → _on_pty_data()
  → 封装成协议帧，经光纤发往 portPath 对应设备

设备回复数据:
  VirtualSerialManager.dispatch(frame)
  → VirtualSerialPort.write_to_pty(data)
  → 第三方软件从 /dev/ttyVcom_0x0001 读到回复
```

---

### 3.11 UI Layer — 界面层

**技术栈**：PySide6（Qt for Python）

**设计目标**：展示设备拓扑、路由表、终端口列表及升级操作。

**UI 文件**：参照 `eloader.ui` 的布局结构，使用 Qt Designer 生成 `fibre2uart.ui`；再通过 `pyside6-uic fibre2uart.ui -o ui.py` 生成 `ui.py`，作为 `main_window.py` 的基类导入，业务逻辑与 UI 布局彻底分离（`.ui` 描述布局，`.py` 实现交互逻辑）。

**UI 与事件分离**：所有业务模块只发布 Event，不直接引用任何 UI 组件；UI 层统一订阅 EventBus，在主线程信号槽中处理所有刷新逻辑。
这种解耦方式使得未来替换 UI 框架（如切换为 Web 或其他 UI）时，业务层无需任何修改。

#### 串口控件规范

| 控件 | 说明 |
|------|------|
| `comboPort`（串口选择） | 下拉框**展开时自动刷新**当前系统可用串口列表（`serial.tools.list_ports.comports()`），确保用户看到最新端口 |
| `comboBaud`（波特率选择） | 预设选项：**9600 / 38400 / 57600 / 115200 / 230400 / 460800 / 921600 / 1000000**；默认选中 1000000 |
| `btnConnect`（连接/断开） | 连接与断开使用**同一按键**：未连接时显示"连接"，点击后切换文字为"断开"并建立串口；已连接时显示"断开"，点击后关闭串口并还原按键文字 |

#### 主界面布局

```
+-------------------------------+---------------------------+
|  [ 扫描设备 ]  [升级]          |   节点详情 / 控制面板       |
|   设备节点树 (DeviceTreeView) |                           |
|   拓扑图 (TopologyView)        |  型号 / UUID / 板卡信息      |
|  - 根节点 [0x801] tx:12 err:0 |  升级按钮 / 进度条          |
|    ├─ PORT1 [光纤] → [0x802]  |  单指令发送 (CommandPanel) |
|    ├─ PORT2 [光纤] → [0x803]  |  logicAddr | cmd | data   |
|    └─ PORT3 [485]  → [0x001]  |  [发送]                   |
|                               +---------------------------+
|                               |  端口信息                  |
|                               |  1:FIBRE  2:FIBRE          |
|                               |  3:485  4:232              |
+-------------------------------+---------------------------+
|   终端口列表 (虚拟串口)                                   |
|  logicAddr | 类型 | 波特率 | tx | rx | err | 状态 | 操作   |
|  0x001     | 485   | 1000000 | 8  | 5  |  0  | 已开启 | 配置  |
+------------------------------------------------------------+
|   日志面板 (LogViewer) — 实时滚动日志，支持级别过滤         |
+------------------------------------------------------------+
|   状态栏 (QStatusBar) — 提示 USB 连接状态 / 操作结果        |
+------------------------------------------------------------+
```

#### 界面组件

| 组件 | PySide6 基类 | 说明 |
|------|------------|------|
| `DeviceTreeView` | `QTreeWidget` | 展示设备拓扑树；两阶段渲染：Phase A 订阅 `NodeDiscoveredEvent` 立即显示「[发现中]」占位行，Phase C 订阅 `DeviceFoundEvent` 更新为真实逻辑地址；可选中查看详情 |
| `ScanButton` | `QPushButton` | 触发全网重新发现，发现中置灰防重入 |
| `RouteTableView` | `QTableWidget` | 展示 logicAddr → PortPath 路由表 |
| `TerminalPortList` | `QTableWidget` | 列出所有终端口，7 列：逻辑地址 / 类型 / 波特率（下拉框+应用按钮）/ TX/RX / 溢出 / 虚拟串口路径 / 操作；波特率列调用 `set_baudrate(logical_addr, baud)` 发 0x33 指令；TX/RX 列（格式 `{tx_bytes}/{rx_bytes}`）和溢出列每 1 秒由 QTimer 从 `VirtualSerialPort` 对象读取刷新；关闭虚拟串口时 TX/RX 重置为 0/0 |
| `UpgradePanel` | `QWidget` + `QProgressBar` | 选择升级目标（广播/单节点），选择固件，进度展示，失败自动重试（最多3次）；目标下拉框由 `DeviceFoundEvent`/`DeviceOfflineEvent` 驱动自动维护（去重，仅含非终端节点），不依赖设备树点击事件；固件路径显示框右对齐，鼠标悬停显示完整路径tooltip；点击"选择固件..."时优先打开已选固件所在目录，否则打开当前运行目录 |
| `CommandPanel` | `QWidget` | 单指令发送：支持两种模式（下拉选择）：1) 逻辑地址模式（输入十六进制地址如 0x0801）；2) 路由路径模式（输入空格分隔路径如 "1 2 3"）；模式切换时输入框提示文本自动更新；点击"发送"后根据模式调用 `send_cmd` 或 `_session.send_request`，响应展示在日志面板 |
| `CmdTestPanel` | `QWidget` | 指令测试：左侧独立 Tab，顶部下拉选择待测试节点（非终端口），点击"开始测试"后按列表逐条执行指令；每条指令固定 3 行展示：第1行指令名称+测试结论，第2行发送指令数据，第3行回复数据；高风险改写类指令（如 0x36/0x37）默认标记为跳过 |
| `LogViewer` | `QPlainTextEdit` | 实时展示 log/py 日志输出；支持 DEBUG/INFO/WARNING/ERROR 级别过滤；最多缓存 5000 行，超出后滚动丢弃最早行 |
| `TopologyView` | `QGraphicsView` | 以节点-边图形式展示设备拓扑（节点=设备，边=光纤/485链路）；订阅 DeviceFoundEvent / DeviceOfflineEvent 动态增删节点；可点击节点跳转详情；每条连线靠近父节点端显示黄色端口号标签（`P{N}` 或 `485:{N}`） |
| `StatusBar` | `QStatusBar` | 固定在主窗口最底部；展示 USB 连接状态（已连接/断开/重连中）、操作结果（发现完成节点数、升级成功/失败）等简短提示信息；所有更新须在主线程通过 `QMainWindow.statusBar().showMessage()` 调用 |
| 主窗口 | `QMainWindow` + `QSplitter` | 上半部分：左侧节点树+拓扑图，右侧详情+单指令；下半部分：终端口列表；底栏：日志面板（可折叠）；最底部：状态栏 |

**线程安全**：接收线程通过 `QMetaObject.invokeMethod` 或 `pyqtSignal` 向 UI 主线程投递更新，禁止在非主线程直接操作 Widget。

**EventBus 回调约束**：EventBus 订阅回调运行在事件发布者的线程上下文（可能是接收线程、升级线程等后台线程）；

**严禁在任何 EventBus 回调中直接操作 Qt Widget**（包括 `setText`、`show`、`hide`、`QMessageBox` 等）。所有 UI 更新必须通过 `pyqtSignal` 或 `QMetaObject.invokeMethod` 投递到主线程后执行。

#### 节点详情面板（tabNodeDetail）

点击节点后右侧自动切换到"节点详情" Tab，显示以下字段：

| 标签 | 内容 | 控件 |
|------|------|------|
| 型号 | `ModelType.name`，如 BRIDGE_FIBRE | `lblModel` |
| UUID | 完整 UUID hex 字符串 | `lblUuid` |
| 逻辑地址 | `0x0801` 格式 | `lblAddr` |
| 路由路径 | `[1, 2, 3]` 格式 | `lblPortPath` |
| 端口信息 | `1:FIBRE  2:FIBRE  3:485  4:232`，跳过 NONE(0) 端口 | `lblPorts`（`wordWrap=True`） |

`lblPorts` 生成逻辑：遍历 `device.port_types`，非 NONE 的端口以 `"{i+1}:{type}"` 拼接，双空格分隔。

#### 拓扑图边标签（TopologyView）

每条连线在靠近父节点 22% 处附加黄色小号端口号标签：

| 连接类型 | 标签样式 | 示例 |
|---------|--------|------|
| 光纤 / 普通端口 | `P{N}` | `P1`、`P3` |
| RS485 地址连接 | `485:{N}` | `485:1`、`485:5` |

标签存储在 `_edges[(parent_uid, child_uid)] = [line, label]`；`clear_topology()` 时随场景一起清除。：EventBus 订阅回调运行在事件发布者的线程上下文（可能是接收线程、升级线程等后台线程）；

**严禁在任何 EventBus 回调中直接操作 Qt Widget**（包括 `setText`、`show`、`hide`、`QMessageBox` 等）。所有 UI 更新必须通过 `pyqtSignal` 或 `QMetaObject.invokeMethod` 投递到主线程后执行。

---

### 3.12 Configuration — 配置模块

**职责**：融合命令行参数与 INI 配置文件，向各模块提供可覆盖的运行时参数。

**文件**：`project/config.py`；**配置文件路径**：`../datas/config.ini`（INI 格式；首次运行时自动生成默认值）

**实现**：`argparse` 解析命令行参数，`configparser` 读写 INI 文件；命令行参数优先级高于配置文件，配置文件优先级高于内置默认值。

#### 配置项说明

```ini
[serial]
port = ""             # 默认串口（空 = 不预选）
baudrate = 921600     # 默认波特率

[discovery]
hotplug = false       # 是否启用 USB 热插拔检测与自动重连（true/false）
rs485_timeout = 0.05  # RS485 轮询超时（秒），范围 [0.01, 5.0]
rs485_max_addr = 127  # RS485 轮询最大地址（1~127，即 0x7F）
```

#### AppConfig

```
AppConfig:
    hotplug        -> bool    # 热插拔开关（default: false）
    rs485_timeout  -> float   # 485 轮询超时，clamped [0.01, 5.0]（default: 0.05）
    rs485_max_addr -> int     # 485 轮询最大地址，clamped [1, 127]（default: 127）
    serial_port    -> str     # 默认串口名（default: 空串）
    baudrate       -> int     # 默认波特率（default: 1000000）
    save()                    # 将当前配置写回 config.ini

get_config() -> AppConfig     # 全局单例，首次调用时初始化
reset_config()                # 重置单例（供测试使用）
```

#### 命令行参数

| 参数 | 类型 | 说明 |
|------|------|------|
| `-c / --config PATH` | str | 指定配置文件路径（绝对路径） |
| `--hotplug` | flag | 启用热插拔（覆盖配置文件） |
| `--rs485-timeout SECS` | float | RS485 轮询超时（秒） |
| `--rs485-max-addr ADDR` | int | RS485 轮询最大地址（1~127） |

#### 集成点

| 模块 | 使用方式 |
|------|---------|
| `DiscoveryWorker` | `__init__` 接受 `rs485_timeout` 和 `rs485_max_addr` 可选参数；`FibreNetworkClient` 在构造时从 `get_config()` 读取并注入 |
| `FibreNetworkClient` | `DiscoveryWorker` 使用配置的 RS485 参数；轮询范围 `range(1, rs485_max_addr+1)`，专用超时实例 `sys_cmd.clone_with_timeout(rs485_timeout)` |
| `MainWindow._on_connect()` | 调用 `client.connect(port, baud, hotplug=get_config().hotplug)`，连接时自动应用配置文件中的热插拔设置 |

#### 4 级路由设备 UI 拓扑图举例（DeviceTreeView，根节点居左）

```
                                                                          ┌─ PORT1(232) [logicAddr:0x001]
                                                    ┌─[BRIDGE_485 0x804]─┼─ PORT2(485) [logicAddr:0x002]
                                                    │  (485addr:0x01)    └─ PORT3(CAN) [logicAddr:0x003]
                               ┌─[FIBRE_485 0x803]──┤
                               │                    ├─ PORT2(485) ──────── [logicAddr:0x004]
                               │                    └─ PORT3(CAN) ──────── [logicAddr:0x005]
              ┌─[BRIDGE_FIBRE  │
              │   0x802]───────┤
              │                │                    ┌─ PORT1(485) ──────── [logicAddr:0x006]
              │                └─[FIBRE_485 0x805]──┤
              │                                     └─ PORT2(232) ──────── [logicAddr:0x007]
[PC:COM3]     │
──[PC_FIBRE   │
   0x801]─────┤
              │                                     ┌─ PORT1(232) ──────── [logicAddr:0x008]
              │                ┌─[FIBRE_485 0x807]──┤
              │                │                    └─ PORT2(485) ──────── [logicAddr:0x009]
              └─[BRIDGE_FIBRE  │
                  0x806]───────┤                    ┌─[BRIDGE_485 0x809]─┬─ PORT1(485) [logicAddr:0x00A]
                               │                    │  (485addr:0x01)    └─ PORT2(CAN) [logicAddr:0x00B]
                               └─[FIBRE_485 0x808]──┤
                                                    └─ PORT2(CAN) ──────── [logicAddr:0x00C]
```

> **路由层级说明**
> - 第 1 级：`PC_FIBRE`（根节点，PC 唯一物理串口接入）
> - 第 2 级：`BRIDGE_FIBRE`（光纤桥接板，扩展光纤链路）
> - 第 3 级：`FIBRE_485`（光纤转 485 板，光纤→多路 232/485/CAN）
> - 第 4 级：`BRIDGE_485`（485 桥接板，通过 RS485 地址区分，再扩展多路终端口）
> - 终端口：每个叶子节点分配唯一 `logicAddr`，在 PC 端映射为虚拟串口（COMx / ttyVx）

### 4.1 PC → 设备（发送指令）

```
App.send_cmd(logical_addr=0x0001, cmd=0x25, data=b'\x00')
  ↓
PCLogicRoutingTable.lookup(0x0001) → PortPath([1, 2])
  ↓
SessionManager.send_request(port_path, cmd, data, timeout=500ms)
  ↓  分配 seq=42
CommandCodec.encode(seq=42, cmd, port_path, data) → bytes
  ↓
Transport.send(bytes)          # sof=0xAA 发出
  ↓
注册 PendingRequest{seq=42, future}
  ↓
await future → 收到 0xAB 响应后 resolve
```

### 4.2 设备 → PC（接收响应）

```
Transport.on_data_received(raw_bytes)
  ↓
FrameParser.feed(bytes) → yield CommandFrame
  ↓
cmd >= 0x10（指令应答）:
  SessionManager.on_frame_received(frame)
    → 匹配 seq → resolve Future
cmd < 0x10（消息透传）:
  VirtualSerialManager.dispatch(frame)
    → 按 frame.ports 匹配已注册的 VirtualSerialPort
    → VirtualSerialPort.write_to_pty(frame.data)
      → rx_bytes += len(data)
      → os.write(master_fd, data)  → 第三方软件从 device_path 读到回复
```

### 4.3 设备发现流程（生成路由表）

```
┌──────────────────────────────────────────────────────┐
│  投递方（只 put，不持锁）                              │
│  程序启动/ScanButton/USB重连/TopologyMonitor          │
│        ↓ discovery_queue.put(DiscoveryTask)           │
└──────────────────────────────────────────────────────┘
                       ↓
┌──────────────────────────────────────────────────────┐
│  DiscoveryWorker（单消费者线程，串行处理队列任务）      │
│                                                      │
│  阶段 A  信息收集（I/O 密集，多线程并行）              │
│    PORT_FIBRE  → ThreadPoolExecutor(max_workers=10)   │
│                  同层并发发 0x24，只收集 NodeInfo       │
│    PORT_485    → 串行轮询地址 1~0x7F（半双工，50ms超时）│
│    PORT_232/CAN→ 直接标记终端口                        │
│    每发现一节点即发布 NodeDiscoveredEvent（UI 立即显示） │
│    递归直到全树收集完毕                                 │
│                       ↓                              │
│  阶段 B  logicAddr 分配（单线程 DFS，无锁竞争）         │
│    节点设备：0x801, 0x802 ...（itertools.count(0x801)）│
│    终端口  ：0x001, 0x002 ...（itertools.count(0x001)）│
│    INCREMENTAL：从当前表最大值 +1 开始，已有节点不变    │
│                       ↓                              │
│  阶段 C  写入 logicAddr                               │
│    发 cmd=0x36 到每个节点                              │
│    终端口合成 uid = sha1(port_path.ports)[:12]         │
│    EventBus.publish(DeviceFoundEvent) 实时刷新树       │
│                       ↓                              │
│  阶段 D  持久化（原子写入）                            │
│    PCLogicRoutingTable / PCUIDRoutingTable 更新        │
│    序列化 → nodes_tmp.json → os.replace → nodes.json  │
│    FULL 模式：EventBus.mark_ready()（replay startup_buffer）│
└──────────────────────────────────────────────────────┘
```

### 4.4 热插拔检测流程（TopologyMonitor）

```
TopologyMonitor 线程（_route_ready=True 后启动，轮询间隔 3s）：
  ↓
  若 _discovering = True → 跳过本次（DiscoveryWorker 正在执行，避免干扰发现流量）
  ↓
  BFS 分级轮询（防止深层节点变化漏检）：
  第 1 级：发 0x24（portLen=0）查询根节点 portTypes
    → 失败（SerialException）→ 本轮跳过（USB 断连由 SerialTransport 处理）
  第 2+ 级：对路由表中所有已知 BRIDGE_FIBRE / FIBRE_485 中继节点各发一次 0x24，
            查询其 portTypes；每级总耗时上限 1s（超出则跳过本轮剩余节点，留到下轮）
  ↓
  对每个被查节点对比 portTypes 与路由表缓存：
  ├─ 完全一致        → 无变化，继续下一节点
  ├─ portType 变化   → _enqueue_incremental(port_path)
  ├─ 出现新 uuid     → _enqueue_incremental(port_path)
  └─ uuid 无响应     → EventBus.publish(DeviceOfflineEvent(uid))
                       从路由表删除，关闭对应虚拟串口
  ↓
  （无需持锁，DiscoveryWorker 负责串行执行所有发现任务）

_enqueue_incremental(port_path)：
  若队列中已存在相同 port_path 的 INCREMENTAL 任务 → 跳过（去重，无需重复入队）
  否则 → discovery_queue.put_nowait(DiscoveryTask(INCREMENTAL, port_path))
         捕获 queue.Full → 丢弃并记录 WARNING（队列已有待处理任务，本次跳过）
```

### 4.5 在线升级流程（状态机）

```
IDLE
  → [App.upgrade_device()] → JUMP
  → [cmd=0x11 跳转到 bootloader，100ms后] → CHECK
  → [cmd=0x12 确认处于 BOOTLOADER] → SEND_INFO
  → [cmd=0x13 发送文件大小，等待擦除完成] → SEND_DATA
  → [循环 cmd=0x14 发送数据包(SN+chunk)，广播间隔50ms]
  → [回复 data1=0x02:升级完成] → VERIFY
  → [cmd=0x12 再次确认板卡状态] → DONE / ERROR

升级失败自动重试流程：
  升级任意阶段出错（超时/校验失败/硬件错误）→ 进入 ERROR 状态
    → 发布 UpgradeErrorEvent(stage, error_code)
    → UI 层（主线程信号槽）弹出 QMessageBox，显示失败阶段 + 设备返回的错误码
    → retry_count < 3：关闭弹窗后自动重新从 JUMP 开始升级，retry_count++
    → retry_count >= 3：停止自动重试，弹窗等待用户操作
    → 用户点击"重试"按钮 → retry_count 归零，重新从 JUMP 开始

整体超时保护（防止状态机永久阻塞）：
  进入 JUMP 状态时记录 _upgrade_deadline = time.monotonic() + 120（2 分钟）
  每次状态转移前检查 time.monotonic() > _upgrade_deadline
    → 超出 → 强制进入 ERROR（stage="GLOBAL_TIMEOUT"）+ 发布 UpgradeErrorEvent
  （保证固件异常/设备卡死时升级任务不会无限挂起占用会话资源）
```

---

### 4.6 通信性能测试流程（ThroughputTest）

**目标**：在真实通信环境下验证 USB 串口和多级路由转发的性能极限，为生产环境部署提供吞吐量/延迟/可靠性基线数据。

#### 测试拓扑

```
┌─────────────────────────────────────────────────────────────┐
│  PC 端（上位机）                                               │
│  ┌────────────────┐       USB                               │
│  │ FibreNetworkAPI├──────────────┐                          │
│  │  (测试客户端)  │              │                          │
│  └────────────────┘              ↓                          │
│                            [SerialTransport]                │
│                                  ↓                          │
└───────────────────────────────────┼───────────────────────────┘
                                  │
                              USB 串口
                                  │
                  ┌───────────────┴───────────────┐
                  │  光纤通信板（PC_FIBRE 0x801）   │
                  └───────────┬───────────────────┘
                              │ 光纤链路
              ┌───────────────┴───────────────┐
              │  光纤转485板（FIBRE_485 0x803） │
              └───────┬───────────────────────┘
                      │ PORT_485 终端口 (logicAddr=0x0001)
                      ↓
            ┌─────────────────────┐
            │  物理串口工具          │ ← 回环连接到 PC 另一串口
            │  (COMx / /dev/ttyUSBx)│
            └─────────────────────┘
                      ↓
              ┌───────────────┐
              │  PC 端监听线程  │ ← 收到数据后立即原样返回（echo）
              └───────────────┘
```

**关键参数配置**：
- **USB 波特率**：1000000 bps（项目上限）
- **测试通道**：虚拟串口 `/dev/ttyVcom_0001`（映射 logicAddr=0x0001）
- **物理回环串口**：波特率与测试需求匹配（9600~1000000 bps）

#### 测试模式

| 模式 | 包大小 | 发送速率 | 持续时间 | 目标参数 |
|------|--------|---------|---------|---------|
| **固定包** | 固定值（默认 240 字节） | 可配置间隔 | 可配置 | 稳态吞吐量，队列积压深度 |
| **混合包** | 4~240 字节随机 | 可配置间隔 | 可配置 | 平均延迟，P99 延迟 |

#### 采集指标

**协议效率**（Protocol Overhead）

```
每帧固定开销：7 字节（SOF + check + seq + len + cmd + checksum，不含 ports）
转义开销：data 中每个 0xAA/0xAB → 2 字节（0xAA 0x00 / 0xAA 0x01）
有效载荷比 = payload_size / (7 + portLen + escaped_payload_size)

测试：
  小包（payload=1）：  7+0+1 = 8 字节，效率 12.5%
  中包（payload=100）：7+0+~100 = ~107 字节，效率 ~93%（转义率 <3%）
  大包（payload=240）：7+0+~240 = ~247 字节，效率 ~97%（升级数据块）
```

**丢帧率**（Frame Loss Rate）

```
发送总帧数：tx_count（客户端计数器）
成功接收帧数：rx_count（回环串口收到数据且长度匹配）
丢帧率 = (tx_count - rx_count) / tx_count * 100%

阈值：< 0.1%（千分之一，921600 bps 下正常值）
```

**顺序错误率**（Sequence Error Rate）

```
每帧 payload 头 4 字节携带递增 seq，回环返回后比对头 4 字节
seq 不匹配则 out_of_order++
顺序错误率 = out_of_order / rx_count * 100%
```

**超时重传率**（Timeout Retry Rate）

```
回环串口 100ms 内未收到数据，计入 retry_count
超时重传率 = retry_count / tx_count * 100%

阈值：< 1%
```

**RTT（往返时延）**

```
对每次 send_cmd()：
  t0 = time.perf_counter()
  等待回环串口收到数据
  rtt = time.perf_counter() - t0
  latencies.append(rtt)

统计：
  平均 RTT = mean(latencies) × 1000  (ms)
  P99 RTT  = percentile(latencies, 99) × 1000  (ms)
  最大 RTT = max(latencies) × 1000  (ms)
```

**吞吐量**（Throughput）

```
测试时长 T（秒，扣除暂停时间）
发送成功字节数：tx_bytes
接收成功字节数：rx_bytes

发送吞吐量 = tx_bytes / T / 1024  （KB/s）
接收吞吐量 = rx_bytes / T / 1024  （KB/s）
```

**队列积压**（Queue Depth）

```
实时监控：
  SerialTransport._send_queue.qsize()   → 待发送帧数量
  SessionManager._pending.__len__()     → 等待响应的 seq 数量

警告阈值：
  _send_queue > 50   → 发送速率超出 USB 处理能力
  _pending > 128     → 会话积压过多
```

**系统资源监控**

```
CPU 使用率：psutil.cpu_percent(interval=0)
内存占用：  psutil.Process().memory_info().rss / 1024^2  （MB）
GC 压力：   gc.get_count()  → (gen0, gen1, gen2)
```

#### UI 布局（ThroughputTestPanel）

通信测试面板作为左侧 Tab（拓扑图之后），与固件升级并列，不占用右侧详情区。

```
左侧 TabWidget (tabLeft)
├── Tab 0: 设备树
├── Tab 1: 拓扑图
├── Tab 2: 通信测试   ← ThroughputTestPanel 在此
├── Tab 3: 指令测试   ← CmdTestPanel 在此
└── Tab 4: 路由表（UI 预留，主窗口运行时可隐藏）
```

**面板布局**：

```
┌─────────────────────────────────────────────────────────────┐
│  测试目标: [下拉：测试终端口/测试路径]                         │
│  [终端口下拉：型号 (0xAddr)] 或 [路径输入框：1 2 3]           │
│  回环串口: [下拉：COMx]  波特率: [下拉]                       │
│  [开始测试 (F5) / 停止测试]  [暂停测试 / 继续测试]            │
├─────────────────────────────────────────────────────────────┤
│  测试模式: [下拉]  包大小: [滑块 1~240] 100字节               │
│  发送间隔: [滑块 1~2000] 10 ms  发送次数: [输入框] ∞ 无限    │
├─────────────────────────────────────────────────────────────┤
│  实时监控                                                    │
│  ┌───────────────────────────────────────────────────────┐  │
│  │  已发送: 1024 包  发送成功: 1024  已接收: 1023 包     │  │
│  │  发送失败: 0  回环超时: 1  丢帧率: 0.10%              │  │
│  │  平均RTT: 15.3ms   P99 RTT: 48.2ms   最大: 65ms      │  │
│  │  吞吐量: 89.2 KB/s (发) / 88.9 KB/s (收)             │  │
│  │  队列积压: 发送队列=2  等待响应=5                      │  │
│  │  CPU: 12.3%   内存: 45.2 MB   GC: (120, 8, 1)       │  │
│  │  已运行: 12.3s                                        │  │
│  └───────────────────────────────────────────────────────┘  │
│  待发送队列: [进度条 0/50]  等待响应: [进度条 5/128]  [导出报告]│
└─────────────────────────────────────────────────────────────┘
```

**交互逻辑**：

```
[测试目标模式切换]：
  "测试终端口" → 显示终端口下拉框，隐藏路径输入框
  "测试路径"   → 隐藏终端口下拉框，显示路径输入框（提示"1 2 3 (空格分割)"）

[开始测试 (F5)] 点击（测试未运行时）：
  1. 根据测试目标模式获取地址：
     - 测试终端口：从下拉框获取 logical_addr
     - 测试路径：解析输入框文本构造 PortPath
  2. 校验已选目标和回环串口（未选回环串口弹确认框）
  3. 创建 ThroughputTestWorker，传递对应参数（logical_addr 或 port_path）
  4. 启动 100ms 刷新定时器
  5. 按钮1 变为红色"停止测试"，按钮2 启用，快捷键 F5 可切换开始/停止

[停止测试] 点击（测试运行中）：
  1. 调用 Worker.stop()，等待线程结束（join timeout=2s）
  2. Worker 自然结束或手动停止时，等待回环数据（最多2s，队列非空则重置倒计时）
  3. 最后刷新一次监控数据（确保显示最终结果）
  4. 按钮1 恢复"开始测试 (F5)"默认样式，按钮2 禁用
  5. 启用"导出报告"按钮

[自然停止检测]：
  刷新定时器检测到 worker.is_alive()==False 且 _is_running==True 时，自动调用 _on_stop()
  更新按钮状态，防止测试完成后按钮状态不变

[暂停测试] 点击（测试运行中且未暂停）：
  1. 调用 Worker.pause()，线程进入休眠循环（0.1s 间隔，减少 CPU 占用）
  2. 停止刷新定时器
  3. 最后刷新一次监控数据（显示暂停时刻的数据快照）
  4. 按钮2 变为绿色文字"继续测试"

[继续测试] 点击（测试已暂停）：
  1. 调用 Worker.resume()，线程恢复发送
  2. 重新启动刷新定时器
  3. 按钮2 恢复"暂停测试"默认样式

[导出报告] 点击：
  生成 Markdown 格式报告，保存到 `../datas/throughput_report_YYYYMMDD_HHMMSS.md`
```

**按钮状态机**：

| 状态 | 按钮1 | 按钮2 |
|------|-------|-------|
| 空闲 | "开始测试"（默认样式） | "暂停测试"（禁用） |
| 运行中 | "停止测试"（红色背景） | "暂停测试"（启用） |
| 已暂停 | "停止测试"（红色背景） | "继续测试"（绿色文字加粗） |

#### 终端口下拉框更新机制

与固件升级面板相同，通过 Signal-Slot 保证主线程更新：

```python
# EventBus 事件（非主线程）
def handle_device_found(self, event) -> None:
    self._dev_found_signal.emit(event.device)

def handle_device_offline(self, event) -> None:
    self._dev_offline_signal.emit(event.uid)

# 主线程 Slot（只添加终端口，与升级面板过滤非终端口相反）
@Slot(object)
def _on_dev_found(self, device) -> None:
    addr = device.logical_addr
    if addr == 0 or addr in self._known_addrs or not device.is_terminal:
        return
    self._known_addrs.add(addr)
    self._uid_to_addr[device.uid] = addr
    model_str = device.model.name if hasattr(device.model, 'name') else str(device.model)
    self._combo_terminal.addItem(f"{model_str} (0x{addr:04X})", userData=addr)

@Slot(bytes)
def _on_dev_offline(self, uid: bytes) -> None:
    addr = self._uid_to_addr.pop(uid, None)
    if addr is None:
        return
    self._known_addrs.discard(addr)
    for i in range(self._combo_terminal.count()):
        if self._combo_terminal.itemData(i) == addr:
            self._combo_terminal.removeItem(i)
            break
```

#### 实现模块

**ThroughputTestWorker**（后台线程）

```python
class ThroughputTestWorker(threading.Thread):
    def __init__(self, client, target_logical_addr: int, mode: str,
                 packet_size: int, interval_ms: int, loopback_port: str = None,
                 duration: int = 60):
        super().__init__(daemon=True, name="ThroughputTestWorker")
        self._client = client
        self._logical_addr = target_logical_addr
        self._mode = mode          # "小包突发" / "大包持续" / "混合负载"
        self._packet_size = packet_size
        self._interval_ms = max(interval_ms, 1)  # 发送间隔（毫秒）
        self._loopback_port = loopback_port
        self._duration = duration  # 有效测试时长（秒，不含暂停时间）
        self._stop_flag = threading.Event()
        self._pause_flag = threading.Event()   # 置位时进入暂停
        self._pause_time = None                # 最近一次暂停时刻
        self._total_pause_duration = 0.0       # 累计暂停时长
        self._loopback_serial = None           # pyserial 对象

    def run(self):
        # 打开回环串口
        if self._loopback_port:
            import serial
            self._loopback_serial = serial.Serial(self._loopback_port, 115200, timeout=0.1)

        try:
            seq = 1
            interval = self._interval_ms / 1000.0  # 转换为秒
            while not self._stop_flag.is_set():
                # 暂停处理：记录暂停时刻，休眠等待
                if self._pause_flag.is_set():
                    if self._pause_time is None:
                        self._pause_time = time.time()
                    time.sleep(0.1)
                    continue
                # 从暂停恢复：累计暂停时长
                if self._pause_time is not None:
                    self._total_pause_duration += time.time() - self._pause_time
                    self._pause_time = None

                # 检查有效运行时长（扣除暂停时间）
                elapsed = time.time() - self._start_time - self._total_pause_duration
                if elapsed > self._duration:
                    break

                payload = self._generate_payload(seq)
                t0 = time.perf_counter()
                try:
                    self._client.send_cmd(self._logical_addr, 0x01, payload)
                    self._metrics['tx_count'] += 1
                    self._metrics['tx_bytes'] += len(payload)

                    if self._loopback_serial:
                        # 等待回环数据（最多 100ms）
                        received = b''
                        deadline = time.time() + 0.1
                        while time.time() < deadline:
                            chunk = self._loopback_serial.read(256)
                            if chunk:
                                received += chunk
                                if len(received) >= len(payload):
                                    break
                            time.sleep(0.001)

                        if received:
                            self._metrics['latencies'].append(time.perf_counter() - t0)
                            self._metrics['rx_count'] += 1
                            self._metrics['rx_bytes'] += len(received)
                            # 验证前 4 字节 seq 是否匹配
                            if len(received) >= 4 and received[:4] != payload[:4]:
                                self._metrics['out_of_order'] += 1
                        else:
                            self._metrics['retry_count'] += 1
                    else:
                        # 无回环串口：假设发送即接收
                        self._metrics['latencies'].append(time.perf_counter() - t0)
                        self._metrics['rx_count'] += 1
                        self._metrics['rx_bytes'] += len(payload)
                except Exception:
                    self._metrics['retry_count'] += 1

                seq = (seq % 255) + 1
                time.sleep(interval)
        finally:
            if self._loopback_serial:
                self._loopback_serial.close()

    def stop(self):   self._stop_flag.set()
    def pause(self):  self._pause_flag.set()
    def resume(self): self._pause_flag.clear()
```

**ThroughputTestPanel**（UI 组件）

```python
class ThroughputTestPanel(QWidget):
    _metrics_signal:     Signal = Signal(dict)    # 后台线程 → 主线程刷新
    _dev_found_signal:   Signal = Signal(object)  # Device（主线程更新下拉）
    _dev_offline_signal: Signal = Signal(bytes)   # uid（主线程移除下拉）

    def __init__(self, parent=None):
        super().__init__(parent)
        self._client = None
        self._worker: Optional[ThroughputTestWorker] = None
        self._refresh_timer = QTimer(self)
        self._refresh_timer.timeout.connect(self._refresh_metrics)
        self._metrics_signal.connect(self._update_ui)
        self._is_running = False
        self._is_paused = False
        self._known_addrs: set = set()
        self._uid_to_addr: dict = {}
        self._build_ui()
        self._dev_found_signal.connect(self._on_dev_found)
        self._dev_offline_signal.connect(self._on_dev_offline)

    # ── 事件总线回调（非主线程）──────────────────
    def handle_device_found(self, event):   self._dev_found_signal.emit(event.device)
    def handle_device_offline(self, event): self._dev_offline_signal.emit(event.uid)

    # ── 开始/停止切换 ────────────────────────────
    def _on_start_stop_toggle(self):
        if self._is_running: self._on_stop()
        else:                self._on_start()

    # ── 暂停/继续切换 ────────────────────────────
    def _on_pause_resume_toggle(self):
        if self._is_paused: self._on_resume()
        else:               self._on_pause()

    def _on_start(self):
        # ...（校验 + 创建 Worker + 启动定时器）
        self._is_running = True
        self._btn_start.setText("停止测试")
        self._btn_start.setStyleSheet("background-color: #f44336; color: white;")
        self._btn_pause.setEnabled(True)

    def _on_stop(self):
        if self._worker:
            self._worker.stop()
            self._worker.join(timeout=2.0)
        self._refresh_timer.stop()
        self._refresh_metrics()    # 最后刷新一次，显示最终结果
        self._worker = None
        self._is_running = False
        self._btn_start.setText("开始测试"); self._btn_start.setStyleSheet("")
        self._btn_pause.setEnabled(False);  self._btn_pause.setStyleSheet("")

    def _on_pause(self):
        if self._worker: self._worker.pause()
        self._refresh_timer.stop()
        self._refresh_metrics()    # 刷新暂停时刻快照
        self._is_paused = True
        self._btn_pause.setText("继续测试")
        self._btn_pause.setStyleSheet("color: #4CAF50; font-weight: bold;")

    def _on_resume(self):
        if self._worker: self._worker.resume()
        self._refresh_timer.start(100)
        self._is_paused = False
        self._btn_pause.setText("暂停测试"); self._btn_pause.setStyleSheet("")

    def _refresh_metrics(self):
        if self._worker:
            self._metrics_signal.emit(self._worker.get_metrics())

    @Slot(dict)
    def _update_ui(self, metrics: dict):
        # 计算并更新监控文本框和进度条
        ...
```

**main_window.py 订阅注册**（`_build_real_widgets` 中）：

```python
self._tok15 = bus.subscribe(DeviceFoundEvent,
                            self._throughput_panel.handle_device_found)
self._tok16 = bus.subscribe(DeviceOfflineEvent,
                            self._throughput_panel.handle_device_offline)
```



---

## 五、目录结构规划

```
project/
├── transport/
│   ├── __init__.py
│   ├── serial_transport.py       # SerialTransport
│   └── test_serial_transport.py  # 自测: 开口/关口/模拟收发
├── frame/
│   ├── __init__.py
│   ├── models.py                 # CommandFrame
│   ├── parser.py                 # FrameParser 状态机
│   ├── builder.py                # FrameBuilder
│   └── test_frame.py             # 自测: 构建帧/解析帧/校验失败丢帧
├── protocol/
│   ├── __init__.py
│   ├── models.py                 # PortPath, PortType, ModelType
│   ├── codec.py                  # CommandCodec
│   └── test_protocol.py          # 自测: 编解码对称性/PortPath 拼接
├── session/
│   ├── __init__.py
│   ├── seq_manager.py            # SeqManager
│   ├── session_manager.py        # SessionManager, PendingRequest
│   └── test_session.py           # 自测: seq 分配去重/超时 reject
├── routing/
│   ├── __init__.py
│   ├── table.py                  # PCLogicRoutingTable, PCUIDRoutingTable
│   ├── discovery.py              # DeviceDiscovery (0x24 递归 + logicAddr 分配)
│   └── test_routing.py           # 自测: 建表/查表/发现模拟
├── commands/
│   ├── __init__.py
│   ├── upgrade.py                # UpgradeCmdHandler (0x11~0x15)
│   ├── system.py                 # SysCmdHandler (0x21~0x26)
│   ├── port.py                   # PortCmdHandler (0x31~0x36)
│   └── test_commands.py          # 自测: 指令编码/升级状态机
├── device/
│   ├── __init__.py
│   ├── models.py                 # Device, BoardInfo, PortType, ModelType
│   ├── manager.py                # DeviceManager, DeviceTree
│   └── test_device.py            # 自测: 树建立/节点查找/logicAddr 分配
├── virtual_serial/
│   ├── __init__.py
│   ├── port.py                   # VirtualSerialPort
│   ├── manager.py                # VirtualSerialManager
│   └── test_virtual_serial.py    # 自测: 虚拟串口读写/dispatch 分发
├── events/
│   ├── __init__.py
│   ├── bus.py                    # EventBus
│   └── test_events.py            # 自测: 订阅/取消订阅/多订阅者
├── ui/
│   ├── fibre2uart.ui             # Qt Designer 布局文件（参照 eloader.ui 格式）
│   ├── ui.py                     # pyside6-uic 自动生成，描述纯布局，不含业务逻辑
│   ├── __init__.py
│   ├── main_window.py            # QMainWindow + QSplitter 主窗口（继承 ui.py 的 Ui_MainWindow）
│   ├── device_tree.py            # DeviceTreeView(QTreeWidget)（__main__ 注入模拟树自测）
│   ├── route_table.py            # RouteTableView(QTableWidget)（__main__ 注入模拟路由表自测）
│   ├── terminal_list.py          # TerminalPortList(QTableWidget)（__main__ 注入模拟数据自测）
│   ├── upgrade_panel.py          # UpgradePanel(QWidget+QProgressBar)（__main__ 模拟升级进度自测）
│   ├── command_panel.py          # CommandPanel(QWidget) 单指令发送（__main__ 自测）
│   ├── cmd_test_panel.py         # CmdTestPanel(QWidget) 指令测试（__main__ 自测）
│   ├── log_viewer.py             # LogViewer(QPlainTextEdit) 日志面板（__main__ 自测）
│   └── topology_view.py          # TopologyView(QGraphicsView) 设备拓扑图（__main__ 自测）
└── api.py                        # FibreNetworkClient (对外统一入口)
└── config.py                     # AppConfig 配置模块（argparse + configparser；生成 datas/config.ini）
└── test_config.py                # 自测: 默认值/clamp边界/save+reload/配置文件覆盖
└── test_all.py                   # 一键启动所有测试代码，执行后生成带时间戳的自测报告
```

> **自测规范**：每个 `test_xxx.py` 均包含 `if __name__ == '__main__':` 入口，可独立运行验证本模块核心逻辑，无需依赖外部设备。UI 模块的自测内嵌在各组件文件的 `__main__` 块中。`test_all.py` 负责一键顺序运行所有测试，**每次执行结束后在 `reports/` 目录下生成一份自测报告**（文件名格式：`test_YYYYMMDD_HHMMSS.log`），报告内容包含：测试开始/结束时间、各模块测试项结果（通过/失败/跳过）及汇总统计。

---

## 六、设计决策说明

| 决策点 | 方案 | 原因 |
|--------|------|------|
| 并发模型 | 接收线程 + Future | 串口 IO 天然阻塞，独立线程接收；业务层用 Future 解耦 |
| 帧解析 | 状态机 | 字节流无边界，状态机健壮处理粘包/断包 |
| 校验 | 统一用 SUM（seq 到 dataN 累加低8位） | 新协议取消子协议 CRC16，全部统一 |
| 序列号去重 | (来源端口路径, seq) 二元组判重 | 防止网络重传导致重复处理 |
| 终端口判断 | portType != PORT_FIBRE | 光纤口为中继，其余均为终端，逻辑简单明确 |
| logicAddr 分配 | 节点从 0x801起，终端口从 0x001起 | 两类地址区间不重叠，易于区分节点和终端口 |
| logicAddr 寻址 | PC 维护 `logicAddr → PortPath` 路由表 | 对上层屏蔽物理拓扑，使用逻辑地址发送更直观 |
| 虚拟串口 | 终端口 → VirtualSerialPort | 封装透传帧收发，对外表现标准串口接口 |
| 路由表 | 两张：logicAddr 表（主）+ UID 备用表 | 发现时同时建表，平衡易用性与唯一性 |
| 发现指令 | 仅 0x24（get_node_info），portLen=0 起步 | 不需要 0x23 扫描，直接递归查询更简洁；只追 PORT_FIBRE 端口 |
| 升级流程 | 显式状态机 | 步骤多、有回滚和错误处理，状态机逻辑清晰 |
| 事件总线 | Pub/Sub | 解耦设备发现、升级进度等异步通知 |

---

## 七、结构化分析（5 个维度）

### 7.1 模块职责清晰度

| 模块 | 单一职责 | 风险点 |
|------|---------|-------|
| Transport | 只做字节流收发 | 不引入帧解析逻辑 |
| Frame | 只做编解码和校验 | 不存储业务状态 |
| Session | 只做 seq 和请求配对 | 不处理帧内容语义 |
| Routing | 只做地址维护 | 不控制业务逻辑 |
| Commands | 每个 Handler 只处理一类 cmd | 不跨类调用 |
| VirtualSerial | 只做透传帧封装 | 不处理升级/系统指令 |
| UI | 只做数据展示和事件触发 | 不嵌入帧编解码逻辑 |

**评估**：各层职责单一，依赖单向向下，有利于独立测试和替换。最大风险在 Routing Layer 和 Command Layer 的交界处，需确保发现完成后才开放指令接口。

---

### 7.2 可扩展性

| 扩展场景 | 支持方式 |
|---------|----------|
| 新增硬件型号 | 只需扩充 `ModelType` 枚举，`DeviceDiscovery` 无需修改 |
| 新增端口类型 | 只需扩充 `PortType` 枚举，`is_terminal_port()` 条件不变 |
| 新增 cmd 类型 | 新建对应 Handler，在 Application Layer 注入 |
| 双物理串口 | `SerialTransport` 封装换为列表即可，上层无感知 |
| UI 框架 | PySide6（QMainWindow/QTreeWidget/QTableWidget/QProgressBar）；UI 层通过 EventBus 解耦，信号投递到主线程，不阻塞接收线程 |

**评估**：横向扩展（新硬件）和纵向扩展（新功能）均可错开修改。路由表设计允许网络拓扑动态变化（重新发现即重建）。

---

### 7.3 容错性

| 错误场景 | 处理机制 |
|---------|----------|
| 帧校验失败 | `FrameParser` 丢帧并返回 WAIT_SOF，不影响后续帧 |
| 请求超时 | `SessionManager._timeout_checker` 自动 reject Future，上层不锁死 |
| 发现中断 | `DeviceDiscovery` 超时后可重新调用，路由表支持增量更新 |
| 设备离线 | EventBus 发布 `DeviceOfflineEvent`，虚拟串口标记关闭，UI 清除节点 |
| 升级失败 | 升级状态机进入 ERROR 状态，支持重试和回滚到 IDLE |
| 帧粘包/断包 | `FrameParser` 状态机天然支持，无内存溢出风险 |

**评估**：各层均有独立的错误隔离，最少作为一个层的错误不会直接崩溃其他层。主要补充点：升级过程断电的防误设计需进一步完善。

---

### 7.4 可测试性

| 模块 | 测试策略 |
|------|----------|
| Frame | 纯内存操作，无外部依赖，可 100% 单元测试 |
| Protocol | 编解码对称性可属性测试，构造任意帧验证 |
| Session | 可用 Mock Transport 驱动，验证 seq 去重/超时/resolve |
| Routing | 纯内存大表，可用 Mock 0x24 回复验证递归发现和 logicAddr 分配 |
| Commands | Mock SessionManager 验证帧格式和状态机流转 |
| VirtualSerial | Mock API 验证 write 封装/dispatch 分发 |
| UI | 注入模拟 DeviceTree 验证渲染结果 |

**评估**：帧层/协议层/会话层/路由层均可简单 Mock 驱动。每个 `test_xxx.py` 自测不依赖外部设备，能在 CI 中直接运行。

---

### 7.5 性能与并发

| 场景 | 分析 |
|------|------|
| 帧解析延迟 | 状态机每字节 O(1)，无堆内存分配，恒定延迟 |
| 小包帧 | 升级数据包最大 256 字节，单包发送 RTT 远小于 1ms（局域串口） |
| 多虚拟串口并发 | VirtualSerialPort 独立缓冲区，dispatch 分发无锁争用 |
| 路由查表 | HashMap O(1) 寻址，数百级节点无性能压力 |
| 升级广播 | 单一帧广播所有下级，50ms 间隔符合协议要求 |
| UI 刷新 | EventBus 异步推送 → PySide6 Signal 投递主线程，不阻塞接收线程 |

**评估**：帧解析和路由查表均为 O(1)，不是等待瓶颈。升级数据包发送速率主要受制于物理串口波特率，与框架本身无关。

---

## 八、设计缺点分析与改进建议

> 共 12 条，分 3 个等级。**等级一（严重）** 可能导致崩溃/数据错乱/功能失效，需重新设计；**等级二（中等）** 影响可靠性或可维护性，需在设计层面修正；**等级三（轻微）** 属工程建议，影响有限。等级一和等级二已全部在对应设计章节中完成修复（详见各章节标注）。

---

### 等级一（严重缺陷）

#### 8.1 SeqManager.allocate() 无线程锁，多线程并发导致 seq 碰撞

**位置**：§3.4 SeqManager  
**问题**：升级线程、发现线程（A1 阶段 ThreadPoolExecutor）和 CommandPanel UI 线程可能同时调用 `send_request()`，进而并发调用 `allocate()`。原设计中 `allocate()` 是无锁的 `_seq += 1; return _seq % 256`，Race Condition 下两个线程可能拿到相同 seq 号，后注册的 `PendingRequest` 覆盖前一个，第一个请求永远无法被 resolve，直到超时后 reject，且 reject 时 Future 数据已无意义。  
**修复**：`allocate()` 内部使用 `threading.Lock()` 保护自增操作，保证多线程调用互斥。已在 §3.4 中更新。

#### 8.2 RS485 增量缓存发现策略遗漏新接入设备

**位置**：§3.5 阶段 A2  
**问题**：A2 阶段在缓存非空时，只轮询 `nodes.json` 已记录的 RS485 地址列表，对缓存之外的地址完全跳过。当用户向某个 RS485 端口新插入一台设备（地址不在缓存中）时，无论是 FULL 还是 INCREMENTAL 任务，该设备在缓存非空的情况下都会被永久忽略，直到显式清空缓存（如 version 不匹配）才能被发现。这违背了"热插拔即时检测"的设计目标。  
**修复**：A2 阶段在完成缓存地址轮询后，额外从 1~0x7F 非缓存地址中随机抽取 ≤8 个进行补充探测；有回复则加入缓存并记录子节点。已在 §3.5 A2 中更新。

#### 8.3 Transport 断连重连后，串口缓冲区残留帧 seq 误 resolve 新 PendingRequest

**位置**：§3.4 SessionManager  
**问题**：`on_transport_lost()` 清空了 `_pending` 映射表，但操作系统串口接收缓冲区可能仍残留重连前设备的回复帧（尤其是波特率较高时）。重连后 `FrameParser` 将缓冲区旧帧解析完毕，其 seq 号（如 `seq=5`）与新注册的 `PendingRequest`（同样为 `seq=5`）匹配成功，新请求被旧数据错误 resolve，上层业务拿到失效数据，可能导致错误的设备控制行为（如错误的升级包偏移量）。  
**修复**：引入 `_connection_generation: int`（每次 `on_transport_lost()` 自增），`PendingRequest` 携带注册时的 `connection_generation`；`on_frame_received()` 收帧时校验 generation 是否匹配，不匹配则丢弃，彻底隔离跨代帧。已在 §3.4 中更新。

---

### 等级二（中等缺陷）

#### 8.4 FrameParser 无最大帧长校验，畸形 len 字段阻塞后续合法帧解析

**位置**：§3.2 FrameParser  
**问题**：`READ_LEN` 状态读取到 `len` 后直接进入 `READ_DATA` 状态等待 `len` 个字节。若串口噪声将 `len` 污染为大值（协议实际最大合理数据长度约 20~30 字节），解析器会持续等待大量字节；等待期间所有后续字节被当作数据消费，新的合法 `SOF(0xAB)` 帧被吞掉丢弃，接收链路事实上阻塞，影响所有并发会话的响应。  
**修复**：`READ_LEN` 状态完成后检查 `len > MAX_FRAME_DATA_LEN (= 260)`；超出上限则发布 `FrameErrorEvent(raw, "len_overflow")` 并回到 `WAIT_SOF` 状态。已在 §3.2 中更新。

#### 8.5 EventBus.mark_ready() 与并发 publish() 之间无互斥，replay 期间新事件可能乱序

**位置**：§3.8 EventBus  
**问题**：`mark_ready()` 遍历 `_startup_buffer` 逐条 dispatch 时，`_ready` 标志尚为 `False`；若此时另一线程调用 `publish()`，检测到 `_ready=False` 会向 `_startup_buffer` 追加新事件。`mark_ready()` 正在迭代的同时被追加，轻则导致新事件在 replay 内被 dispatch 一次、`_ready=True` 后再被正常 dispatch 一次（重复分发），重则在 Python list 迭代中途修改触发运行时异常。  
**修复**：`mark_ready()` 开头在 `_lock` 保护下原子执行 `_ready=True` + `_startup_buffer` 快照 + 清空，之后在锁外按快照逐条 dispatch，避免回调持锁死锁。已在 §3.8 中更新。

#### 8.6 TopologyMonitor 只查根节点，深层中继节点变化形成检测盲区

**位置**：§4.4 TopologyMonitor  
**问题**：原设计每轮仅向根节点发 `0x24(portLen=0)` 查询其直连端口 portTypes。对于二级或更深层的 `BRIDGE_FIBRE / FIBRE_485` 中继节点下发生的热插拔（如第三层的 `FIBRE_485` 下新插一台 `BRIDGE_485`），根节点的 portTypes 不会有任何变化，TopologyMonitor 永远无法感知，直到用户手动触发全量 Scan 才能发现。  
**修复**：TopologyMonitor 改为 BFS 分级轮询：第 1 级查根节点，第 2+ 级对路由表中所有已知中继节点各发一次 `0x24`；每级总耗时上限 1s，超出则留到下轮。已在 §4.4 中更新。

#### 8.7 升级状态机无整体超时，SEND_DATA 阶段设备异常时永久阻塞

**位置**：§4.5 升级状态机  
**问题**：每包有 500ms 的 SessionManager 超时，但状态机本身没有整体截止时间。若设备 flash 擦除异常（如电源波动导致设备反复重启但不回复完成），每包超时后状态机继续重试下一包，固件 128 KB / 256 字节/包 = 512 包，每包超时 500ms × 3 次重试 = 约 12 分钟无限挂起；期间升级线程持有会话资源，其他 send_request 正常运行但 seq 号被持续消耗，最终影响整体链路可用性。  
**修复**：进入 JUMP 状态时记录 `_upgrade_deadline = time.monotonic() + 120`；每次状态转移前检查，超出则强制进入 `ERROR(stage="GLOBAL_TIMEOUT")`。已在 §4.5 中更新。

#### 8.8 INCREMENTAL 发现流程中消失节点 logicAddr 归还 _addr_pool 的触发时序不明确

**位置**：§3.5 阶段 B  
**问题**：原设计只在 §3.5 B 阶段末尾一句"设备离线时（DeviceOfflineEvent）：将其 logicAddr 归还到对应 _addr_pool"，但没有说明由哪个模块订阅该事件并执行归还。若由 DeviceDiscovery 自订阅，需要 EventBus 在发现线程内同步回调，与"禁止在 EventBus 回调中执行 I/O"的约定冲突；若由 DeviceManager 执行，又造成 DeviceManager 持有 _addr_pool 指针的跨层依赖。实现时极易遗漏或产生循环依赖。  
**修复**：明确由 DiscoveryWorker 在发布 `DeviceOfflineEvent` 之前直接调用 `_addr_pool.add(logicAddr)`，无需事件订阅，归还逻辑内聚于发现流程，消除歧义。已在 §3.5 B 阶段中更新。

---

### 等级三（轻微缺陷）

#### 8.9 CommandPanel 无发送速率限制，高频点击可耗尽可用 seq 号

**位置**：§3.11 CommandPanel  
**问题**：用户连续快速点击"发送"按钮，每次触发一个 `send_request()`，若 256 次点击全在 500ms timeout 窗口内完成，SeqManager 绕回后下一个 seq 与仍在等待中的旧 `PendingRequest` 碰撞，旧请求被意外 resolve（数据语义错误）或新请求被忽略（无匹配 PendingRequest）。  
**建议**：CommandPanel "发送"按钮点击后，使用 `QTimer.singleShot(200)` 在 200ms 内置灰防重入；或在 SessionManager 层增加每客户端最大并发请求数限制（如 ≤16）。

#### 8.10 nodes.json version 字段只做字符串全量匹配，不支持向前兼容

**位置**：§3.5 启动缓存加载  
**问题**：version 字段（如 `"20260317"`）为日期字符串，框架版本任何升级都会导致字符串不匹配，所有缓存强制废弃，首次启动必须全量重发现（RS485 全轮询耗时较长）。对于只增加了新字段、不影响路由逻辑的 minor 升级，缓存完全可以复用。  
**建议**：version 改为 `{"major": 1, "minor": 0}` 结构，只比较 major 版本号；minor 版本升级时保留缓存并对新字段做缺省值补全，减少非必要全量重发现。

#### 8.11 VirtualSerialManager.dispatch() 未处理 logicAddr 无对应 VirtualSerialPort 的情形

**位置**：§3.10 VirtualSerialManager  
**问题**：`dispatch()` 通过 portPath 解析 logicAddr 后，直接调用对应 `VirtualSerialPort.write_to_pty(data)`。若该终端口尚未调用 `create()`（用户未手动开启虚拟串口），`_ports` 字典中无对应条目，当前设计未说明此时的处理方式，实现时容易引发 `KeyError` 崩溃或静默丢弃（但不记录），使数据到达情况对用户完全不可见。  
**建议**：`dispatch()` 增加 guard：若 logicAddr 无对应端口，则发布 `UnclaimedDataEvent(port_path, data)` 并记录 DEBUG 日志，便于诊断数据到达但虚拟串口未开启的场景。

#### 8.12 TopologyView 与 DeviceTreeView 的节点联动规则未定义

**位置**：§3.11 TopologyView / DeviceTreeView  
**问题**：文档说 TopologyView "可点击节点跳转详情"，DeviceTreeView 也支持选中节点查看详情。两者共享同一套设备数据，但没有规定当用户在 TopologyView 点击设备时 DeviceTreeView 是否需要同步选中对应行，反之亦然。双向不同步会导致详情面板与高亮状态不一致，影响用户操作体验。  
**建议**：补充联动规则：TopologyView 点击节点 → emit `nodeSelected(uid)` 信号 → `MainWindow` 同步调用 `DeviceTreeView.setCurrentItem(uid)` 并刷新详情面板；DeviceTreeView 选中变化同样 emit `nodeSelected(uid)` 反向通知 TopologyView 更新高亮，实现双向联动。
