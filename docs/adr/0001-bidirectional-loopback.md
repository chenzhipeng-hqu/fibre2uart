# ADR-0001：通信测试改为双向回环（正向 A→B + 反向 B→A）

- 状态：已决议（待实现）
- 日期：2026-08-10
- 相关：`project/ui/throughput_panel.py`（ThroughputTestWorker）、`docs/07.光纤板通信协议.md`

## 背景（Context）

通信性能测试（ThroughputTestPanel）当前只验证正向链路：PC（串口 A / USB 主通道）→ 光纤 → 终端口 → 回环串口 B(RX)，在 B 端逐字节校验（`_loopback_reader` + `_try_match`）。反向光纤链路（设备→PC）未被测试。

设备固件支持「终端口 RX 侧数据反向回传」：B 的 TX 写入的数据，经终端口 RX、设备反向转发，作为**独立 0xAB 透传帧（cmd<0x10）**回到 PC 的 A.RX。

现状（trace 结论）：设备主动发来的 0xAB 透传帧经 `FrameParser.on_frame` → [`api._on_frame`](../../project/api.py) 分类（cmd<0x10 → [`VirtualSerialManager.dispatch`](../../project/virtual_serial/manager.py) → 虚拟串口），**当前没有「收到透传数据」的 EventBus 事件**。

## 决策（Decision）

将回环从「单向 + B 端校验」改为「双向回环 + A 端反向校验（保留 B 端正向校验）」：

1. **正向（PC→设备）**：A 发送 → B.RX 收到 → B 端逐字节校验（保留）。
2. **B 回写**：`_loopback_reader` 正向校验通过后，把收到的字节原样 `write` 到 B.TX（**app 软件回写，不新增监听线程** —— 故满足「不做 PC 监听线程 echo」）。
3. **反向（设备→PC）**：B.TX → 终端口 RX → 设备反向转发 → A.RX 收到 0xAB 透传帧。
4. **反向捕获**：worker 启动时包装 `FrameParser.on_frame`，拦截 `SOF=0xAB && cmd<0x10` 的帧，取 `frame.data` 用 `AA55+seq` 匹配本包 seq（复用 `_try_match` 思路）、内容比对 = 反向校验；worker 停止时还原原回调。原分发（虚拟串口路由）不受影响。
5. **完成判定**：每包「等 A 收到回程」（1s 超时）才算完成（`wait_for_ack` 语义重定义）。
6. **指标**：保留正向 `rx_count`/`rx_bytes`/`data_corrupt_count`/`loopback_miss_count`/前向 RTT；新增反向 `rx_count_return`/`rx_bytes_return`/`data_corrupt_count_return`/`loopback_miss_count_return` + 完整往返 RTT。

## 关键约束

- 回程帧在 transport 接收线程触发 `on_frame`，匹配结构需**加锁 / queue 跨线程递交**（不在 worker 线程）。
- 匹配键 = **seq**（嵌在 data 里），对回程帧的 cmd/port 具体取值不敏感；必要时再按 `frame.ports` 限定到测试目标终端口。
- 正向发送保持 **fire-and-forget**（cmd=0x01 透传，非 session 请求-响应）。

## 后果（Consequences）

- **好**：双向都校验，能区分「正向坏」与「反向坏」，定位故障段；反向光纤链路首次纳入测试。
- **代价**：worker 复杂度上升（B 收发并发 + `on_frame` hook 生命周期 + 跨线程匹配）；每包时延约翻倍（含反向段）。
