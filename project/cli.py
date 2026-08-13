# -*- coding: utf-8 -*-
"""
cli.py — 无头双向往返测试入口（ADR-0002 / #4）

一次性批处理：连串口 A → 发现拓扑 → 对终端口跑双向往返（复用 ADR-0001 的
ThroughputTestWorker）→ stdout 纯 JSON + 退出码 → 干净退出。

  python3 cli.py -c test.ini

- 配置走 #3 的 AppConfig.from_external（不读/不写 datas/config.ini）
- stdout 只放一份 JSON；日志走 stderr 或 [headless].log_file
- 退出码：0=pass / 1=fail(阈值超限) / 2=error(连接/发现/崩溃/SIGINT)
- 全程不 import PySide6
"""
from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import time
from typing import Optional, TextIO

from api import FibreNetworkClient
from config import AppConfig
from events.bus import RouteTableUpdatedEvent
from ui.throughput_panel import ThroughputTestWorker

logger = logging.getLogger(__name__)

# 退出码
EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_ERROR = 2


def _setup_logging(cfg: AppConfig) -> None:
    """配置日志：只往 stderr 或 [headless].log_file，绝不进 stdout。"""
    level = getattr(logging, cfg.headless_log_level, logging.INFO)
    log_file = cfg.headless_log_file
    handlers: list = [logging.StreamHandler(sys.stderr)] if not log_file \
        else [logging.FileHandler(log_file, encoding="utf-8")]
    logging.basicConfig(
        level=level,
        format="[%(asctime)s] %(name)s [%(levelname)s] %(message)s",
        handlers=handlers,
        force=True,
    )


def _wait_discovery(client: FibreNetworkClient, timeout: float,
                    interrupted=None) -> bool:
    """等拓扑发现就绪（RouteTableUpdatedEvent + route_ready），超时返回 False。

    interrupted 为可选的可调用对象（返回 bool）：为 True 时提前返回 False，
    使 SIGINT 能在 discovery 等待期间及时打断，而非干等到 timeout。
    """
    ready = {"done": False}

    def _on_ready(_event):
        ready["done"] = True
    client.on(RouteTableUpdatedEvent, _on_ready)

    deadline = time.time() + timeout
    while time.time() < deadline:
        if interrupted and interrupted():
            return False
        if getattr(client._discovery, "route_ready", False) or ready["done"]:
            return True
        time.sleep(0.1)
    return getattr(client._discovery, "route_ready", False)


def _join_worker(worker: ThroughputTestWorker, timeout: float,
                 interrupted=None) -> None:
    """短轮询 join worker，SIGINT 可打断；超时后仍存活则记警告（不与 disconnect 竞态）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if interrupted and interrupted():
            break
        worker.join(timeout=0.2)
        if not worker.is_alive():
            return
    if worker.is_alive():
        logger.warning("worker 在 %.0fs 内未退出（daemon 线程，进程退出时强杀）", timeout)


def _build_worker(cfg: AppConfig, client: FibreNetworkClient,
                  logical_addr: int) -> ThroughputTestWorker:
    """按 ini 配置构造双向往返测试 worker（复用 ADR-0001 逻辑）。"""
    return ThroughputTestWorker(
        client,
        target_logical_addr=logical_addr,
        mode=cfg.throughput_mode,
        packet_size=cfg.throughput_packet_size,
        interval_ms=cfg.throughput_interval_ms,
        loopback_port=cfg.throughput_loopback_port,
        loopback_baudrate=cfg.throughput_loopback_baudrate,
        duration=cfg.throughput_duration,
        wait_for_ack=True,             # 每包等回程确认（ADR-0001 #2）
        return_timeout=1.0,
    )


def _evaluate(metrics: dict, cfg: AppConfig):
    """阈值判定：返回 (pass, violations)。往返丢包率/损坏率**严格大于**阈值才 fail
    （阈值是 exclusive 上界：等于阈值算 pass）。"""
    violations = []
    tx = metrics.get("tx_count", 0)
    loss_thr = cfg.throughput_loss_threshold
    corrupt_thr = cfg.throughput_corrupt_threshold

    if tx > 0:
        # 往返丢包率：发了 tx，反向收到 rx_count_return
        rx_ret = metrics.get("rx_count_return", 0)
        lost = tx - rx_ret
        loss_pct = lost * 100.0 / tx
        if loss_pct > loss_thr:
            violations.append(
                f"往返丢包率 {loss_pct:.1f}% 超阈值 {loss_thr}%（发 {tx} / 回 {rx_ret}）")

        # 往返损坏率
        corrupt_ret = metrics.get("data_corrupt_count_return", 0)
        corrupt_pct = corrupt_ret * 100.0 / tx
        if corrupt_pct > corrupt_thr:
            violations.append(
                f"往返损坏率 {corrupt_pct:.1f}% 超阈值 {corrupt_thr}%（{corrupt_ret}/{tx}）")

    return (len(violations) == 0), violations


def _summary(passed: bool, metrics: dict, violations: list) -> str:
    """一行人话结论。"""
    tx = metrics.get("tx_count", 0)
    rx_ret = metrics.get("rx_count_return", 0)
    rtt = metrics.get("latencies_roundtrip", [])
    rtt_avg = (sum(rtt) / len(rtt) * 1000) if rtt else 0.0
    verdict = "PASS" if passed else "FAIL"
    return (f"{verdict}: 发 {tx} / 往返收到 {rx_ret}, "
            f"平均往返 RTT {rtt_avg:.1f}ms"
            + (f"; {'; '.join(violations)}" if violations else ""))


def run_headless(
    config_path: str,
    client_factory=None,
    worker_factory=None,
    stdout: Optional[TextIO] = None,
) -> int:
    """核心编排，返回退出码。可注入 client/worker 工厂与 stdout 供测试。

    - client_factory: () -> FibreNetworkClient（默认 FibreNetworkClient）
    - worker_factory: (cfg, client, logical_addr) -> ThroughputTestWorker
                      （默认 _build_worker）
    """
    stdout = stdout or sys.stdout

    # 0. 先用最小 stderr 配置兜底，防止 from_external 内部的 logger 调用
    #    在 _setup_logging(cfg) 之前走 stale/默认 handler 污染 stdout。
    logging.basicConfig(level=logging.INFO,
                        format="[%(asctime)s] %(name)s [%(levelname)s] %(message)s",
                        handlers=[logging.StreamHandler(sys.stderr)],
                        force=True)

    # 1. 加载外部配置（from_external：不碰 datas/config.ini）
    try:
        cfg = AppConfig.from_external(config_path)
    except FileNotFoundError as e:
        logger.error("%s", e)
        return EXIT_ERROR
    _setup_logging(cfg)

    client_factory = client_factory or FibreNetworkClient
    worker_factory = worker_factory or _build_worker
    client = client_factory()

    # SIGINT → 干净退出
    interrupted = {"flag": False}

    def _on_sigint(_signum, _frame):
        interrupted["flag"] = True
    old_handler = signal.signal(signal.SIGINT, _on_sigint)

    worker: Optional[ThroughputTestWorker] = None
    try:
        # 2. 连接串口 A
        port = cfg.serial_port
        if not port:
            logger.error("配置缺少 [serial].port")
            return EXIT_ERROR
        try:
            client.connect(port, cfg.baudrate, hotplug=False)
        except Exception as e:
            logger.error("连接串口失败 %s: %s", port, e)
            return EXIT_ERROR

        if interrupted["flag"]:
            return EXIT_ERROR

        # 3. 等拓扑就绪
        if not _wait_discovery(client, cfg.headless_discovery_timeout,
                               interrupted=lambda: interrupted["flag"]):
            if interrupted["flag"]:
                return EXIT_ERROR
            logger.error("拓扑发现在 %ds 内未就绪", cfg.headless_discovery_timeout)
            return EXIT_ERROR

        if interrupted["flag"]:
            return EXIT_ERROR

        # 4. 校验目标终端口存在
        logical_addr = cfg.throughput_terminal_addr
        device = client.get_device_by_addr(logical_addr)
        if device is None:
            logger.error("目标终端口 0x%04X 不在路由表中", logical_addr)
            return EXIT_ERROR

        # 5. 构造并跑双向往返测试
        worker = worker_factory(cfg, client, logical_addr)
        worker.start()
        # 等到 duration 到点（worker 自管 loopback/hook/reaper）
        deadline = time.time() + cfg.throughput_duration + 5.0  # 5s 宽限
        while worker.is_alive() and time.time() < deadline:
            if interrupted["flag"]:
                break
            time.sleep(0.2)
        worker.stop()
        _join_worker(worker, 5.0, interrupted=lambda: interrupted["flag"])
        metrics = worker.get_metrics()

        if interrupted["flag"]:
            logger.warning("被 SIGINT 中断")
            return EXIT_ERROR

        # 6. 阈值判定
        passed, violations = _evaluate(metrics, cfg)
        result = {
            "pass": passed,
            "summary": _summary(passed, metrics, violations),
            "metrics": metrics,
            "thresholds": {
                "loss": cfg.throughput_loss_threshold,
                "corrupt": cfg.throughput_corrupt_threshold,
            },
            "violations": violations,
        }

        # 7. stdout 纯 JSON
        json.dump(result, stdout, ensure_ascii=False)
        stdout.write("\n")
        stdout.flush()

        # 8. 可选 markdown 报告
        if cfg.headless_report_file:
            try:
                _write_report(cfg.headless_report_file, result)
            except Exception as e:
                logger.warning("写报告失败: %s", e)

        return EXIT_PASS if passed else EXIT_FAIL

    except Exception as e:
        logger.exception("无头测试异常: %s", e)
        return EXIT_ERROR
    finally:
        if worker is not None and worker.is_alive():
            worker.stop()
            _join_worker(worker, 5.0)
        try:
            client.disconnect()
        except Exception:
            pass
        signal.signal(signal.SIGINT, old_handler)


def _write_report(path: str, result: dict) -> None:
    """写一份人话 markdown 报告（[headless].report_file 指定时）。"""
    m = result["metrics"]
    rtt = m.get("latencies_roundtrip", [])
    rtt_avg = (sum(rtt) / len(rtt) * 1000) if rtt else 0.0
    lines = [
        "# 无头双向往返测试报告",
        "",
        f"- 结论：{'**PASS**' if result['pass'] else '**FAIL**'}",
        f"- {result['summary']}",
        "",
        "## 指标",
        f"- 发送帧数 tx_count: {m.get('tx_count', 0)}",
        f"- 正向收到 rx_count: {m.get('rx_count', 0)}",
        f"- 往返收到 rx_count_return: {m.get('rx_count_return', 0)}",
        f"- 往返损坏 data_corrupt_count_return: {m.get('data_corrupt_count_return', 0)}",
        f"- 平均往返 RTT: {rtt_avg:.1f} ms",
        "",
        "## 阈值",
        f"- 丢包率阈值: {result['thresholds']['loss']}%",
        f"- 损坏率阈值: {result['thresholds']['corrupt']}%",
        "",
    ]
    if result["violations"]:
        lines.append("## 违规项")
        for v in result["violations"]:
            lines.append(f"- {v}")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def main(argv: Optional[list] = None) -> int:
    """解析 CLI 参数，返回退出码。"""
    parser = argparse.ArgumentParser(
        prog="cli.py",
        description="fibre2uart 无头双向往返测试（ADR-0002）",
    )
    parser.add_argument("-c", "--config", default=None,
                        help="外部配置文件路径（格式与默认 config.ini 一致，必需）")
    args = parser.parse_args(argv)
    if not args.config:
        logger.error("缺少必需参数 -c/--config <ini>")
        return EXIT_ERROR
    return run_headless(args.config)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
