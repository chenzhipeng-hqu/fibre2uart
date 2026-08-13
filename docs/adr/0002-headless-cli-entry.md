# ADR-0002：无头 CLI 入口（一次性批处理，供脚本 / AI subprocess 调用）

- 状态：已决议（待实现）
- 日期：2026-08-13
- 相关：`project/cli.py`（新增）、[`project/config.py`](../../project/config.py)、[`project/api.py`](../../project/api.py)、[`project/ui/throughput_panel.py`](../../project/ui/throughput_panel.py)（`ThroughputTestWorker`）、ADR-0001

## 背景（Context）

当前**唯一**的人机入口是 [`ui/main_window.py`](../../project/ui/main_window.py)，必起 `QApplication`（PySide6 GUI）。[`config.py`](../../project/config.py) 虽已有 `argparse + configparser`（优先级 CLI args > `config.ini` > 内置默认），但**没有任何不依赖 PySide6、连上→测→出结果→退出的无头入口**，外部脚本 / 另一个 AI 无法以 subprocess 方式调用本程序做测试。

[`FibreNetworkClient`](../../project/api.py) 的 docstring 原话即为「为上层（UI / **测试脚本**）提供简洁接口」；ADR-0001 落地的双向回环逻辑全在无 UI 的 `ThroughputTestWorker` 上。分层架构已为无头入口铺好路——它只是 `FibreNetworkClient` 的第二个消费者。

**澄清「AI」**：本 CLI **不含任何 AI 代码**。「另一个 AI 可调用」是一组**可脚本化**硬约束的人话版——subprocess 可起、无 GUI、JSON 到 stdout + 退出码、cli+ini 配置、一次性无状态、超时与干净清理。满足这组约束的程序，普通 shell 脚本 / pytest / CI / cron 也全都能调；AI 只是其中最挑剔的消费者。

## 决策（Decision）

新增**独立无头入口 `project/cli.py`**，一次性批处理语义：`cli.py -c <test.ini>` → 连串口 A → 发现拓扑 → 对指定终端口跑**双向往返测试**（复用 ADR-0001 的 `ThroughputTestWorker`）→ stdout 打印 JSON → 按退出码退出。headless 路径**完全不 import PySide6**。

经 12 轮 grilling 锁定的 12 条决策：

1. **范围（Q1）**：只做通信性能 / 往返测试。不做发现健康检查、单指令、升级的无头化。
2. **调用模型（Q2）**：一次性批处理（α）。每次起一个子进程，无状态、天然隔离。不做常驻 JSON-RPC 服务。
3. **环境（Q3）**：同机（i）。另一个 AI / 脚本与本程序在同一 Linux 主机，直接访问 `/dev/ttyUSBx`（串口 A）与回环串口 B，光纤拓扑物理接好。
4. **入口形态（Q4）**：独立 `project/cli.py`（A）。与既有 `project/upgrade.py`、`project/evtmgr.py` 的「独立模块当 CLI」先例一致；headless 不 import PySide6。
5. **调用方（Q5）**：真实 AI agent 与泛指脚本化调用皆有（a+b）。故 stdout JSON 同时给「结构化 `metrics`」与「一行人话 `summary`」——AI 与人都能一眼读懂。
6. **pass/fail 判定（Q6）**：阈值制（b）。丢包率 / 损坏率 ≤ 阈值则 pass，**默认阈值 0（等价严格）**；压力测试等场景可放宽。**无论判定如何，完整 `metrics` JSON 照样吐**，供调用方覆盖本判定。
7. **配置段（Q7）**：复用现有 `[throughput]` 段（a），一个 ini key 一份语义，GUI 与 headless 共享配置源。
8. **参数形态（Q8）**：无头参数**几乎全部走 `-c <path>` 传入的 ini**，不再堆 `--flag`。传入 ini **格式与默认 `config.ini` 一致**，且**只读、不写回 `datas/config.ini`**。AI 写一个 ini、调 `cli.py -c x.ini` 即完事。
9. **超时 / 清理 / 日志（Q9）**：均放进传入的 ini（见下表）。
10. **ini 叠加 vs 替换（Q10）**：**替换**（b）。headless 带 `-c` 时**只用「内置默认 + 传入文件」**，**完全不读、不写 `datas/config.ini`**——天然不污染默认配置，且 AI 结果完全可控、可复现。仅 GUI 路径仍走「叠加 `datas/config.ini`」旧行为。
11. **新增 ini key（Q11）**：写入 [`config.py`](../../project/config.py) 的 `_DEFAULTS`，使 AI 的 ini 与默认 `config.ini` 长得一致。

   | 段 | 新 key | 默认 | 说明 |
   |---|---|---|---|
   | `[throughput]` | `duration` | `30` | 测试时长（秒），到点自动停（比 `max_count` 更适合 AI） |
   | `[throughput]` | `loss_threshold` | `0` | 往返丢包率上限 %（0 = 严格） |
   | `[throughput]` | `corrupt_threshold` | `0` | 往返损坏率上限 %（0 = 严格） |
   | `[headless]` | `discovery_timeout` | `10` | 连上后等拓扑就绪的超时（秒），超了 → fail 退出 |
   | `[headless]` | `log_level` | `INFO` | 日志级别 |
   | `[headless]` | `log_file` | _(空)_ | 空 = 日志走 stderr；给路径则写文件，**绝不污染 stdout** |
   | `[headless]` | `report_file` | _(空)_ | 空 = 只 stdout JSON；给路径则额外写人话 markdown 报告 |

   复用既有 key（不新增）：`[serial].port`=串口 A、`[serial].baudrate`、`[throughput].loopback_port`=回环 B、`[throughput].terminal_addr`、`[throughput].mode / packet_size / interval_ms`。

12. **输出 / 退出码契约（Q12）**：
    - **stdout**：纯 JSON —— `pass: bool`、`summary: str`（一行人话）、完整 `metrics`（含 ADR-0001 的正向 + 反向 + 往返 RTT）、`thresholds`、`violations: []`。
    - **stderr**：日志（除非 `[headless].log_file` 指定文件）。
    - **退出码**：`0`=pass ／ `1`=fail（阈值超限）／ `2`=error（连不上 / 发现超时 / 崩溃）。
    - **资源清理**：到点或 SIGINT(Ctrl-C) 都干净退出（关串口 A/B、join worker / transport / reaper 线程）。

## 关键约束

- **配置加载分支**：`AppConfig` 需新增「外部配置、不走 `datas`」的加载路径（spec 细节）。GUI 路径（读 + 写 `datas/config.ini`）**行为不变**，仅 headless `-c` 走替换分支。
- **复用 ADR-0001 worker**：无头入口**不重写测试逻辑**，直接构造 `ThroughputTestWorker`（含 `loopback_serial_factory`、`return_timeout`、`_install_return_hook` 等），与 GUI 面板共用同一套双向回环实现。
- **stdout 纯净**：JSON 之外的任何输出（日志、banner、进度）一律走 stderr 或 `log_file`，保证调用方能可靠 `json.loads(stdout)`。
- **不挂死**：发现等待有 `discovery_timeout` 兜底；测试有 `duration` 到点；SIGINT 必须能打断并清理，绝不让调用方等到自身超时。

## 后果（Consequences）

- **好**：无头入口让脚本 / CI / 另一个 AI 都能 subprocess 调用本程序做往返测试，分层架构（`FibreNetworkClient` 为非 UI 代码而设计）价值兑现；`datas/config.ini` 不被外部调用污染；结果机器可读、可复现、可判定。
- **代价**：`AppConfig` 多一条「替换式外部配置」加载分支（需保证 GUI 旧行为不回归）；新增 `[headless]` 段与 3 个 `[throughput]` key；`cli.py` 须自管生命周期（发现等待 / 测试驱动 / 资源清理 / 信号处理）。
