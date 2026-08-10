#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_all.py — 一键启动所有模块自测

执行后在 reports/ 目录生成带时间戳的报告文件：
    reports/test_YYYYMMDD_HHMMSS.txt   （纯文本，兼容现有 CI 流程）
    reports/test_YYYYMMDD_HHMMSS.html  （结构化 HTML，逐条展示测试内容，方便测试工程师查阅）

用法：
    python project/test_all.py
"""
from __future__ import annotations

import html as _html
import io
import os
import sys
import time
import traceback
import unittest
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

# ── 确保 project/ 在 sys.path ──────────────────────────────────────────
_PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
if _PROJECT_DIR not in sys.path:
    sys.path.insert(0, _PROJECT_DIR)

# ── 测试模块列表（按依赖顺序排列）────────────────────────────────────────
TEST_MODULES = [
    "test_config",
    "frame.test_frame",
    "protocol.test_protocol",
    "session.test_session",
    "routing.test_routing",
    "device.test_device",
    "events.test_events",
    "commands.test_commands",
    "transport.test_serial_transport",
    "virtual_serial.test_virtual_serial",
    "ui.test_command_panel",
]

ADDITIONAL_TESTS: list[tuple[str, str]] = []

_REPORTS_DIR = os.path.join(_PROJECT_DIR, "..", "reports")


def _ensure_reports_dir() -> str:
    path = os.path.abspath(_REPORTS_DIR)
    os.makedirs(path, exist_ok=True)
    return path


# ──────────────────────────────────────────────────────────────────────
# 自定义 TestResult：逐条捕获结果、耗时、描述
# ──────────────────────────────────────────────────────────────────────

@dataclass
class CaseResult:
    """单条用例结果。"""
    class_name: str          # 所属 TestCase 类名
    method_name: str         # 方法名
    description: str         # docstring 第一行（中文描述）
    status: str              # 'pass' / 'fail' / 'error' / 'skip'
    duration_ms: float = 0.0
    detail: str = ""         # 失败/错误时的 traceback


class _DetailedResult(unittest.TestResult):
    """逐条记录每个测试用例结果的 TestResult 实现。"""

    def __init__(self) -> None:
        super().__init__()
        self.case_results: list[CaseResult] = []
        self._start_times: dict[str, float] = {}

    def _key(self, test: unittest.TestCase) -> str:
        return test.id()

    def startTest(self, test: unittest.TestCase) -> None:
        super().startTest(test)
        self._start_times[self._key(test)] = time.perf_counter()

    def _elapsed(self, test: unittest.TestCase) -> float:
        start = self._start_times.pop(self._key(test), time.perf_counter())
        return (time.perf_counter() - start) * 1000

    def _class_and_method(self, test: unittest.TestCase) -> tuple[str, str]:
        parts = test.id().rsplit(".", 1)
        if len(parts) == 2:
            return parts[0].rsplit(".", 1)[-1], parts[1]
        return "", parts[0]

    def addSuccess(self, test: unittest.TestCase) -> None:
        super().addSuccess(test)
        cls, mth = self._class_and_method(test)
        self.case_results.append(CaseResult(
            class_name=cls, method_name=mth,
            description=test.shortDescription() or "",
            status="pass", duration_ms=self._elapsed(test),
        ))

    def addFailure(self, test: unittest.TestCase, err) -> None:
        super().addFailure(test, err)
        cls, mth = self._class_and_method(test)
        self.case_results.append(CaseResult(
            class_name=cls, method_name=mth,
            description=test.shortDescription() or "",
            status="fail", duration_ms=self._elapsed(test),
            detail=self._exc_info_to_string(err, test),
        ))

    def addError(self, test: unittest.TestCase, err) -> None:
        super().addError(test, err)
        cls, mth = self._class_and_method(test)
        self.case_results.append(CaseResult(
            class_name=cls, method_name=mth,
            description=test.shortDescription() or "",
            status="error", duration_ms=self._elapsed(test),
            detail=self._exc_info_to_string(err, test),
        ))

    def addSkip(self, test: unittest.TestCase, reason: str) -> None:
        super().addSkip(test, reason)
        cls, mth = self._class_and_method(test)
        self.case_results.append(CaseResult(
            class_name=cls, method_name=mth,
            description=test.shortDescription() or "",
            status="skip", duration_ms=self._elapsed(test),
            detail=reason,
        ))


@dataclass
class ModuleResult:
    name: str
    status: str          # 'pass' / 'fail' / 'load_error'
    n: int = 0
    errors: int = 0
    failures: int = 0
    skipped: int = 0
    duration_ms: float = 0.0
    cases: list[CaseResult] = field(default_factory=list)
    load_error: str = ""


# ──────────────────────────────────────────────────────────────────────
# 主运行函数
# ──────────────────────────────────────────────────────────────────────

def run_all() -> int:
    start_time = datetime.now()
    report_lines: list[str] = []
    module_results: list[ModuleResult] = []

    def out(line: str = "") -> None:
        print(line)
        report_lines.append(line)

    out("=" * 70)
    out("fibre2uart 全量自测报告")
    out(f"开始时间: {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    out("=" * 70)

    total_tests = total_errors = total_failures = total_skipped = 0

    all_modules = list(TEST_MODULES) + [mn for mn, _ in ADDITIONAL_TESTS]
    extra_dirs = {mn: td for mn, td in ADDITIONAL_TESTS}

    for module_name in all_modules:
        out()
        out(f"{'─' * 60}")
        out(f"模块: {module_name}")
        out(f"{'─' * 60}")

        if module_name in extra_dirs:
            td = extra_dirs[module_name]
            if td not in sys.path:
                sys.path.insert(0, td)

        t0 = time.perf_counter()
        try:
            suite = unittest.defaultTestLoader.loadTestsFromName(module_name)
            detail_result = _DetailedResult()
            buf = io.StringIO()
            runner = unittest.TextTestRunner(stream=buf, verbosity=2, buffer=True)
            # 先用 detail_result 跑，再用 runner 跑（获取文字输出）
            suite_copy = unittest.defaultTestLoader.loadTestsFromName(module_name)
            suite.run(detail_result)
            runner.run(suite_copy)
        except Exception as exc:
            dur = (time.perf_counter() - t0) * 1000
            mr = ModuleResult(name=module_name, status="load_error",
                              load_error=str(exc), duration_ms=dur)
            module_results.append(mr)
            out(f"[ERROR] 无法加载模块: {exc}")
            total_errors += 1
            if module_name in extra_dirs:
                td = extra_dirs[module_name]
                if td in sys.path:
                    sys.path.remove(td)
            continue

        dur = (time.perf_counter() - t0) * 1000
        if module_name in extra_dirs:
            td = extra_dirs[module_name]
            if td in sys.path:
                sys.path.remove(td)

        n = detail_result.testsRun
        e = len(detail_result.errors)
        f = len(detail_result.failures)
        s = len(getattr(detail_result, 'skipped', []))

        total_tests += n
        total_errors += e
        total_failures += f
        total_skipped += s

        status = "pass" if (e == 0 and f == 0) else "fail"
        mr = ModuleResult(name=module_name, status=status, n=n,
                          errors=e, failures=f, skipped=s,
                          duration_ms=dur, cases=detail_result.case_results)
        module_results.append(mr)

        # 文字输出
        for cr in detail_result.case_results:
            icon = {"pass": "ok", "fail": "FAIL", "error": "ERROR", "skip": "skip"}[cr.status]
            desc = f"  ({cr.description})" if cr.description else ""
            out(f"  {cr.method_name}{desc} ... {icon}")
            if cr.detail:
                for line in cr.detail.splitlines():
                    out(f"    {line}")

        status_str = (f"✓ PASS  ({n} 个测试, {s} 跳过)"
                      if status == "pass"
                      else f"✗ FAIL  ({n} 个测试, {e} 错误, {f} 失败, {s} 跳过)")
        out(f"\n{status_str}")

    end_time = datetime.now()
    elapsed = (end_time - start_time).total_seconds()
    passed = total_tests - total_failures - total_errors - total_skipped

    out()
    out("=" * 70)
    out("汇总")
    out("=" * 70)
    for mr in module_results:
        if mr.status == "load_error":
            out(f"  ✗ LOAD_ERROR  {mr.name}")
        elif mr.status == "pass":
            out(f"  ✓ PASS  ({mr.n} 个测试, {mr.skipped} 跳过)  {mr.name}")
        else:
            out(f"  ✗ FAIL  ({mr.n} 个测试, {mr.errors} 错误, {mr.failures} 失败)  {mr.name}")

    out()
    out(f"结束时间: {end_time.strftime('%Y-%m-%d %H:%M:%S')}")
    out(f"总耗时:   {elapsed:.2f} 秒")
    out(f"总测试数: {total_tests}  |  通过: {passed}  |  "
        f"失败: {total_failures}  |  错误: {total_errors}  |  跳过: {total_skipped}")
    overall = "全部通过 ✓" if (total_errors + total_failures) == 0 else "存在失败 ✗"
    out(f"\n总体结果: {overall}")
    out("=" * 70)

    # ── 写入 .txt ──────────────────────────────────────────────────────
    reports_dir = _ensure_reports_dir()
    stamp = start_time.strftime('%Y%m%d_%H%M%S')
    txt_path = os.path.join(reports_dir, f"test_{stamp}.txt")
    with open(txt_path, "w", encoding="utf-8") as fp:
        fp.write("\n".join(report_lines) + "\n")
    print(f"\n报告已保存: {txt_path}")

    # ── 写入 .html ─────────────────────────────────────────────────────
    html_path = os.path.join(reports_dir, f"test_{stamp}.html")
    _write_html_report(
        html_path,
        start_time=start_time, end_time=end_time, elapsed=elapsed,
        module_results=module_results,
        total_tests=total_tests, passed=passed,
        total_failures=total_failures, total_errors=total_errors,
        total_skipped=total_skipped,
    )
    print(f"HTML报告已保存: {html_path}")

    return 0 if (total_errors + total_failures) == 0 else 1


# ──────────────────────────────────────────────────────────────────────
# HTML 报告生成（结构化，逐条展示）
# ──────────────────────────────────────────────────────────────────────

def _write_html_report(
    path: str,
    start_time: datetime,
    end_time: datetime,
    elapsed: float,
    module_results: list[ModuleResult],
    total_tests: int,
    passed: int,
    total_failures: int,
    total_errors: int,
    total_skipped: int,
) -> None:
    he = _html.escape
    overall_ok = (total_failures + total_errors) == 0
    overall_bg = "#2e7d32" if overall_ok else "#c62828"
    overall_label = "全部通过 ✓" if overall_ok else "存在失败 ✗"

    # ── 统计卡片 ──────────────────────────────────────────────────────
    cards_data = [
        ("总测试数", total_tests, "#1565c0"),
        ("通过",     passed,         "#2e7d32"),
        ("失败",     total_failures, "#c62828" if total_failures else "#388e3c"),
        ("错误",     total_errors,   "#c62828" if total_errors   else "#388e3c"),
        ("跳过",     total_skipped,  "#e65100" if total_skipped  else "#616161"),
        ("耗时",     f"{elapsed:.1f}s", "#4527a0"),
    ]
    cards_html = "".join(
        f'<div class="card"><div class="card-val" style="color:{c};">{v}</div>'
        f'<div class="card-lbl">{l}</div></div>'
        for l, v, c in cards_data
    )

    # ── 模块详情 ──────────────────────────────────────────────────────
    STATUS_ICON  = {"pass": "✓", "fail": "✗", "error": "✗", "skip": "⊘", "load_error": "✗"}
    STATUS_COLOR = {"pass": "#2e7d32", "fail": "#c62828", "error": "#c62828",
                    "skip": "#e65100", "load_error": "#c62828"}
    ROW_BG       = {"pass": "#f1f8e9", "fail": "#fff3e0", "error": "#ffebee",
                    "skip": "#fafafa", "load_error": "#ffebee"}

    modules_html_parts = []
    for mr in module_results:
        mod_ok = mr.status == "pass"
        mod_hdr_bg  = "#e8f5e9" if mod_ok else "#ffebee"
        mod_bar_clr = "#43a047" if mod_ok else "#e53935"
        mod_icon    = STATUS_ICON.get(mr.status, "?")
        mod_color   = STATUS_COLOR.get(mr.status, "#000")

        if mr.status == "load_error":
            body = f'<div class="load-err">⚠ 模块加载失败：{he(mr.load_error)}</div>'
        else:
            # 按 TestCase 类分组
            groups: dict[str, list[CaseResult]] = {}
            for cr in mr.cases:
                groups.setdefault(cr.class_name, []).append(cr)

            group_parts = []
            for cls_name, cases in groups.items():
                rows = []
                for cr in cases:
                    icon  = STATUS_ICON.get(cr.status, "?")
                    color = STATUS_COLOR.get(cr.status, "#000")
                    bg    = ROW_BG.get(cr.status, "#fff")
                    desc  = he(cr.description) if cr.description else \
                            f'<span style="color:#bbb;font-style:italic;">（暂无描述）</span>'
                    dur   = f"{cr.duration_ms:.1f}ms"
                    detail_html = ""
                    if cr.detail:
                        detail_html = (
                            f'<tr><td colspan="4" class="detail-cell">'
                            f'<pre class="tb">{he(cr.detail)}</pre></td></tr>'
                        )
                    rows.append(
                        f'<tr style="background:{bg};">'
                        f'<td class="tc-icon" style="color:{color};">{icon}</td>'
                        f'<td class="tc-method"><code>{he(cr.method_name)}</code></td>'
                        f'<td class="tc-desc">{desc}</td>'
                        f'<td class="tc-dur">{dur}</td>'
                        f'</tr>{detail_html}'
                    )
                fail_cnt = sum(1 for c in cases if c.status in ("fail", "error"))
                cls_badge = (
                    f'<span class="cls-badge ok">全部通过</span>'
                    if fail_cnt == 0
                    else f'<span class="cls-badge fail">{fail_cnt} 项失败</span>'
                )
                group_parts.append(f"""
          <div class="cls-group">
            <div class="cls-hdr">
              <span class="cls-name">{he(cls_name)}</span>
              {cls_badge}
              <span class="cls-cnt">{len(cases)} 项</span>
            </div>
            <table class="case-table">
              <thead><tr>
                <th style="width:28px;"></th>
                <th>测试方法</th>
                <th>验证内容</th>
                <th style="width:70px;text-align:right;">耗时</th>
              </tr></thead>
              <tbody>{"".join(rows)}</tbody>
            </table>
          </div>""")
            body = "\n".join(group_parts)

        mod_summary = (
            f'{mod_icon} {he(mr.name)}'
            f'&ensp;<span class="mod-stat">{mr.n} 项'
            + (f'，{mr.failures+mr.errors} 失败' if not mod_ok else '') + '</span>'
            + f'&ensp;<span class="mod-dur">{mr.duration_ms:.0f}ms</span>'
        )
        modules_html_parts.append(f"""
    <details {'open' if not mod_ok else ''} class="mod-details">
      <summary class="mod-summary" style="background:{mod_hdr_bg};border-left:4px solid {mod_bar_clr};">
        <span style="color:{mod_color};font-weight:bold;">{mod_summary}</span>
      </summary>
      <div class="mod-body">{body}</div>
    </details>""")

    modules_html = "\n".join(modules_html_parts)

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>fibre2uart 测试报告 {start_time.strftime('%Y-%m-%d %H:%M:%S')}</title>
  <style>
    *{{box-sizing:border-box;margin:0;padding:0}}
    body{{font-family:"Microsoft YaHei","PingFang SC",sans-serif;
         background:#f0f2f5;color:#212121;padding:20px;font-size:14px}}
    h1{{font-size:1.4em;margin-bottom:4px}}
    .meta{{color:#757575;font-size:0.85em;margin-bottom:16px}}
    .overall{{display:inline-block;padding:5px 16px;border-radius:6px;
              font-weight:bold;font-size:1.05em;margin-bottom:16px;
              background:{overall_bg};color:#fff}}
    .cards{{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:20px}}
    .card{{background:#fff;border-radius:8px;padding:10px 18px;
           text-align:center;box-shadow:0 1px 3px #0001;min-width:80px}}
    .card-val{{font-size:1.7em;font-weight:bold}}
    .card-lbl{{font-size:0.75em;color:#666;margin-top:2px}}
    .mod-details{{margin-bottom:8px;border-radius:6px;overflow:hidden;
                  box-shadow:0 1px 3px #0001}}
    .mod-summary{{padding:10px 14px;cursor:pointer;list-style:none;
                  display:flex;align-items:center}}
    .mod-summary::-webkit-details-marker{{display:none}}
    .mod-stat{{font-size:0.82em;color:#555;font-weight:normal}}
    .mod-dur{{font-size:0.78em;color:#9e9e9e;font-weight:normal}}
    .mod-body{{padding:0 0 8px;background:#fff}}
    .cls-group{{margin:8px 12px}}
    .cls-hdr{{display:flex;align-items:center;gap:8px;padding:6px 0 4px;
              border-bottom:1px solid #e0e0e0;margin-bottom:0}}
    .cls-name{{font-family:monospace;font-weight:bold;font-size:0.92em;color:#1565c0}}
    .cls-badge{{font-size:0.72em;padding:1px 7px;border-radius:10px;font-weight:bold}}
    .cls-badge.ok{{background:#c8e6c9;color:#1b5e20}}
    .cls-badge.fail{{background:#ffcdd2;color:#b71c1c}}
    .cls-cnt{{font-size:0.78em;color:#9e9e9e;margin-left:auto}}
    .case-table{{width:100%;border-collapse:collapse;font-size:0.85em;margin-bottom:4px}}
    .case-table th{{text-align:left;padding:4px 8px;font-size:0.8em;
                    color:#757575;font-weight:600;border-bottom:1px solid #eeeeee}}
    .case-table td{{padding:5px 8px;border-bottom:1px solid #f5f5f5;vertical-align:top}}
    .tc-icon{{font-size:1em;text-align:center;width:28px}}
    .tc-method{{font-family:monospace;font-size:0.82em;white-space:nowrap}}
    .tc-desc{{color:#424242}}
    .tc-dur{{text-align:right;color:#9e9e9e;font-size:0.8em;white-space:nowrap}}
    .detail-cell{{padding:0}}
    pre.tb{{background:#1e1e1e;color:#f8f8f2;padding:10px 14px;
            font-size:0.78em;line-height:1.5;overflow-x:auto;
            margin:0;border-top:2px solid #e53935}}
    .load-err{{padding:12px 16px;color:#c62828;font-family:monospace}}
  </style>
</head>
<body>
  <h1>fibre2uart 全量自测报告</h1>
  <div class="meta">
    开始：{he(start_time.strftime('%Y-%m-%d %H:%M:%S'))} &nbsp;|&nbsp;
    结束：{he(end_time.strftime('%Y-%m-%d %H:%M:%S'))}
  </div>
  <div class="overall">{he(overall_label)}</div>
  <div class="cards">{cards_html}</div>
  <div id="modules">{modules_html}</div>
</body>
</html>"""

    with open(path, "w", encoding="utf-8") as fp:
        fp.write(html)


if __name__ == "__main__":
    sys.exit(run_all())
