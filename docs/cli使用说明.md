# 无头 CLI 使用说明（fibre2uart-cli）

> 对应决策：[ADR-0002](adr/0002-headless-cli-entry.md) ｜ 实现：[`project/cli.py`](../project/cli.py) ｜ 配置基础：[`project/config.py`](../project/config.py) 的 `from_external`

`cli.py` 是一个**无头（无 GUI）入口**，让脚本 / CI / 另一个 AI 能以 subprocess 方式调用本程序，对某个终端口自动跑一次**双向往返通信测试**（正向 A→B + 反向 B→A，复用 [ADR-0001](adr/0001-bidirectional-loopback.md) 的双向回环逻辑），拿到机器可读的 JSON 结果和一个明确的 pass/fail 退出码。

全程**不依赖 PySide6**——运行环境无需安装 GUI 库。

---

## 一、快速开始

### 1. 前置条件

- 同一台 Linux 主机，能直接访问：
  - **串口 A**：PC ↔ 光纤板的主 USB 串口（如 `/dev/ttyUSB0`）
  - **回环串口 B**：接在终端口 RX/TX 上的回环串口（如 `/dev/ttyUSB1`）
- 光纤拓扑物理接好，终端口已接入回环 B
- 已安装依赖：`python3 -m pip install -r requirements.txt`（headless 路径不需要 PySide6，但 pyserial 等仍需要）

### 2. 写一份配置 ini

格式与默认 `datas/config.ini` **完全一致**，缺的 key 走内置默认。最小可用示例：

```ini
[serial]
port = /dev/ttyUSB0          ; 串口 A（必需）
baudrate = 1000000

[throughput]
terminal_addr = 1            ; 要测的终端口逻辑地址（必需，默认 0 = 无效）
loopback_port = /dev/ttyUSB1 ; 回环串口 B（必需）
loopback_baudrate = 1000000
mode = 固定包
packet_size = 64
interval_ms = 10
duration = 30                ; 测试时长（秒），到点自动停

[headless]
discovery_timeout = 10       ; 等拓扑发现就绪的超时（秒）
log_level = INFO             ; DEBUG / INFO / WARNING / ERROR
log_file =                   ; 空 = 日志走 stderr；填路径则写文件
report_file =                ; 空 = 只输出 JSON；填路径则额外写人话 markdown 报告
```

保存为 `test.ini`。

### 3. 运行

```bash
cd project
python3 cli.py -c test.ini
```

> **CWD 必须是 `project/`**——和 GUI 入口 `ui/main_window.py` 一致，模块导入依赖此工作目录。

### 4. 看结果

- **stdout**：一份纯 JSON（见 [§三](#三stdout-json-结构)）
- **stderr**：运行日志（除非 `log_file` 指定了文件）
- **退出码**：`0`=pass / `1`=fail / `2`=error（见 [§四](#四退出码)）

---

## 二、配置项详解

配置优先级：**ini 文件 > 内置默认**。`cli.py` 用 `AppConfig.from_external(path)` 加载——**只读你传入的 ini，不读、不写 `datas/config.ini`**，所以反复调用不会污染 GUI 的配置、结果可复现。

### `[serial]` — 物理串口 A

| key | 默认 | 说明 |
|---|---|---|
| `port` | `""` | 串口 A 设备路径。**必需**，空则退出码 2 |
| `baudrate` | `1000000` | 串口 A 波特率 |

### `[throughput]` — 测试参数

| key | 默认 | 说明 |
|---|---|---|
| `terminal_addr` | `0` | 目标终端口逻辑地址。**必需**（0 无效，会退出码 2）。路由表中不存在也退出码 2 |
| `loopback_port` | `""` | 回环串口 B 设备路径。**必需** |
| `loopback_baudrate` | `1000000` | 回环串口 B 波特率 |
| `mode` | `固定包` | 测试模式 |
| `packet_size` | `240` | 单包字节数 |
| `interval_ms` | `10` | 发送间隔（毫秒） |
| `duration` | `30` | 测试时长（秒），到点自动停 |
| `loss_threshold` | `0` | 往返丢包率上限 %（**严格大于**才 fail，0 = 严格） |
| `corrupt_threshold` | `0` | 往返损坏率上限 %（**严格大于**才 fail，0 = 严格） |

### `[headless]` — 无头运行参数

| key | 默认 | 说明 |
|---|---|---|
| `discovery_timeout` | `10` | 连上后等拓扑发现就绪的超时（秒）。超时 → 退出码 2 |
| `log_level` | `INFO` | 日志级别。无效值自动回退 INFO |
| `log_file` | `""` | 空 = 日志走 stderr；填路径则写该文件。**绝不写 stdout** |
| `report_file` | `""` | 空 = 只输出 JSON；填路径则额外写一份人话 markdown 报告 |

---

## 三、stdout JSON 结构

stdout **只有一份 JSON**（可直接 `json.loads`），结构：

```jsonc
{
  "pass": true,                    // bool：阈值判定结论
  "summary": "PASS: 发 100 / 往返收到 100, 平均往返 RTT 12.3ms",
                                   // 一行人话，AI 和人都能一眼读懂
  "metrics": {                     // 完整指标（透传 worker.get_metrics()）
    "tx_count": 100,               // 正向：尝试发送帧数
    "tx_ok_count": 100,
    "rx_count": 100,               // 正向：回环 B 端收到
    "tx_bytes": 6400,
    "rx_bytes": 6400,
    "send_err_count": 0,
    "loopback_miss_count": 0,      // 正向超时未收到
    "data_corrupt_count": 0,       // 正向内容比对失败
    "latencies": [0.005, ...],     // 正向 RTT（秒）
    "rx_count_return": 100,        // 反向：A 端收到回程帧数
    "rx_bytes_return": 6400,
    "data_corrupt_count_return": 0,// 反向内容比对失败
    "loopback_miss_count_return": 0,
    "latencies_roundtrip": [0.012, ...]  // 往返 RTT（秒）：A发→B收→B发→A收
    // ...其余 worker 指标字段
  },
  "thresholds": {
    "loss": 0,                     // 往返丢包率阈值 %
    "corrupt": 0                   // 往返损坏率阈值 %
  },
  "violations": []                 // 超阈值项的人话描述列表；空 = pass
}
```

### pass/fail 判定（阈值制）

- **往返丢包率** = `(tx_count - rx_count_return) / tx_count × 100%`
- **往返损坏率** = `data_corrupt_count_return / tx_count × 100%`
- 任一**严格大于**对应阈值 → `pass=false`、退出码 1，违规项写入 `violations`
- 阈值默认 `0`（严格：只要有丢包/损坏就 fail）；压力测试等场景可放宽
- **无论 pass 还是 fail，完整 `metrics` 都照样输出**——调用方可覆盖本判定、自己再算

---

## 四、退出码

| 退出码 | 含义 | 触发条件 |
|---|---|---|
| `0` | **pass** | 测试完成且未超阈值 |
| `1` | **fail** | 测试完成但往返丢包率/损坏率超阈值 |
| `2` | **error** | `-c` 缺失 / ini 文件不存在 / `[serial].port` 空 / 连接串口失败 / 拓扑发现超时 / 目标终端口不存在 / 运行异常 / 被 SIGINT(Ctrl-C) 中断 |

> 退出码语义专为脚本/AI 设计：`&&` 链、`if rc == 0` 判定都好用。error 统一归 2，调用方若需区分具体原因，看 stderr 日志或 JSON 是否产出。

---

## 五、给另一个 AI / 脚本的调用示例

### Python 调用 + 解析

```python
import json, subprocess

result = subprocess.run(
    ["python3", "cli.py", "-c", "test.ini"],
    cwd="project",
    capture_output=True, text=True,
)
if result.returncode == 0:
    data = json.loads(result.stdout)
    print(f"测试通过，往返收到 {data['metrics']['rx_count_return']} 包")
elif result.returncode == 1:
    data = json.loads(result.stdout)
    print(f"测试失败：{data['violations']}")
else:  # 2 = error
    print(f"运行出错，见 stderr：\n{result.stderr}")
```

### Shell 判定

```bash
cd project
if python3 cli.py -c test.ini; then
    echo "PASS"
else
    rc=$?
    [ $rc -eq 1 ] && echo "FAIL（阈值超限）" || echo "ERROR（rc=$rc）"
fi
```

### 只想要人话报告（不要 JSON）

```ini
[headless]
report_file = /tmp/report.md     ; 额外写 markdown 报告
log_file = /tmp/run.log          ; 日志进文件，不混进 stdout
```

```bash
python3 cli.py -c test.ini > /tmp/result.json 2>/tmp/run.log
# /tmp/result.json 给程序解析，/tmp/report.md 给人看
```

---

## 六、工作机制（简述）

```
cli.py -c test.ini
  │
  1. AppConfig.from_external(test.ini)   ← 只读传入 ini，不碰 datas/config.ini
  2. FibreNetworkClient.connect(串口A)    ← 不启 hotplug（跑完即退）
  3. 等拓扑发现就绪（RouteTableUpdatedEvent / route_ready，discovery_timeout 兜底）
  4. 校验 terminal_addr 对应的终端口存在
  5. 构造 ThroughputTestWorker（wait_for_ack=True，每包等回程确认）
     │  worker 内部自管（ADR-0001）：
     │  • 打开回环串口 B + 读者线程（正向 B 端校验）
     │  • B 收到后回写 B.TX → 设备反向转发 → A.RX 收 0xAB 透传帧
     │  • on_frame hook 拦截反向帧 + AA55+seq 匹配（反向校验）
     │  • reaper 线程清扫反向超时
  6. 跑 duration 秒 → stop → join → get_metrics()
  7. 阈值判定 → stdout 打印 JSON
  8. （可选）写 markdown 报告
  9. 清理：关回环 B、worker 线程、client.disconnect()（关串口 A + transport/session 线程）
  → 退出码 0 / 1 / 2
```

回环串口 B、反向 hook、reaper 线程**全部由 worker 自管**（在其 `run()` 的 try/finally 内开闭，ADR-0001 已落实），`cli.py` 只负责生命周期编排（连接 / 发现等待 / 驱动 / 判定 / 清理 / 信号处理）。

---

## 七、常见问题

**Q：为什么报「拓扑发现在 10s 内未就绪」（退出码 2）？**
A：连上串口 A 后，程序会递归发现光纤拓扑。超时通常是：物理拓扑未接好 / 串口 A 选错 / baudrate 不对。调大 `[headless].discovery_timeout` 可延长等待，但根因多半是硬件。

**Q：为什么报「目标终端口不在路由表中」（退出码 2）？**
A：`[throughput].terminal_addr` 填的逻辑地址在发现的拓扑里找不到。先用 GUI 或日志确认终端口真实逻辑地址（终端口从 `0x001` 起）。

**Q：丢包率/损坏率总是 100%？**
A：多半是回环串口 B 没接对，或 `loopback_port`/`loopback_baudrate` 配错。反向链路要设备固件支持「终端口 RX 侧反向回传」（见 ADR-0001）。

**Q：可以同时跑 GUI 和 CLI 吗？**
A：可以，但**不要指向同一个串口 A**（串口独占）。CLI 不读不写 `datas/config.ini`，所以配置层面互不污染；物理串口层面需自行错开。

**Q：stdout 里有非 JSON 内容怎么办？**
A：不应该有——日志只走 stderr 或 `log_file`。若发现 stdout 被「污染」，请提 issue；这是契约级 bug。

---

## 八、相关文档

- [ADR-0002](adr/0002-headless-cli-entry.md) — 无头 CLI 入口决策记录（12 轮 grilling 收敛）
- [ADR-0001](adr/0001-bidirectional-loopback.md) — 双向回环通信测试（CLI 复用的测试逻辑）
- [`project/config.py`](../project/config.py) — 配置项定义（`from_external`、所有 key 的 clamp 规则）
- [`project/cli.py`](../project/cli.py) — 实现源码
- [CLAUDE.md](../CLAUDE.md) — 项目整体架构与运行方式
