"""场景演示: 同名区域、乱序门闩、失败/空分支区分。

python -m collector.demo [输出目录, 默认 outputs/demo]

运维入口 /__control 只在场景编排中模拟来源调度(门闩与故障),
采集器本身只读页面 DOM。
"""
import json
import sys
from pathlib import Path
from urllib.request import Request, urlopen

from playwright.sync_api import sync_playwright

from portal.__main__ import running

from .core import Collector, Intent, progress_markdown, write_outputs


def control(base, **fields):
    """来源运维调度(仅演示编排使用, 采集器不使用)。"""
    request = Request(
        base + "/__control",
        data=json.dumps(fields).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urlopen(request) as response:
        return json.load(response)


def scenario_base(page, base):
    """全部可见区域: 两个同名“中心区”按各自 value 采集, 空区域留下完成证据。"""
    return Collector(page, Intent.from_dict({"regions": "all"})).run()


def scenario_out_of_order(page, base):
    """乱序门闩: r-n 类型加载被卡住时用户快速改父项, 旧响应晚到不能污染新查询。"""
    control(base, phase="out-of-order", version=1)
    switched = {"done": False}

    def on_event(name, payload):
        if (
            name == "level_load_start"
            and payload.get("level") == "kind"
            and payload.get("parent") == "r-n"
            and not switched["done"]
        ):
            switched["done"] = True
            # 用户快速改父项: r-n 的类型还在加载, 直接切到 r-s
            page.select_option("#region", "r-s")
            page.wait_for_function(
                """() => { const k = document.querySelector('#kind');
                   return k.dataset.loading === 'false' && k.dataset.parent === 'r-s'; }""",
                timeout=5000,
            )
            # 运维释放门闩, 旧 r-n 响应晚到并覆盖类型选项(污染现场)
            control(base, version=2)
            page.wait_for_function(
                """() => document.querySelector('#kind').dataset.parent === 'r-n'
                   && document.querySelector('#region').value === 'r-s'""",
                timeout=5000,
            )

    report = Collector(
        page, Intent.from_dict({"regions": ["中心区"], "max_attempts": 5}), on_event=on_event
    ).run()
    report["scenario_evidence"] = {"rapid_parent_switch": switched["done"]}
    return report


def scenario_failed_options(page, base):
    """失败分支: 类型加载失败且有界重试, 与空分支在同一张进度表中区分。"""
    control(base, phase="failed-options")
    recovered = {"done": False}

    def on_event(name, payload):
        if (
            name == "entry"
            and payload.get("status") == "failed"
            and (payload.get("region") or {}).get("value") == "r-n"
            and not recovered["done"]
        ):
            recovered["done"] = True
            # r-n 分支已按有界重试记为失败, 运维恢复来源, 后续区域正常
            control(base, phase="base")

    report = Collector(
        page, Intent.from_dict({"regions": "all", "max_attempts": 3}), on_event=on_event
    ).run()
    report["scenario_evidence"] = {"recovered_after_failure": recovered["done"]}
    return report


def entries_by_region(report):
    return {
        (e["region"] or {}).get("value"): e
        for e in report["entries"]
        if e["status"] != "unsatisfiable"
    }


def check_base(report):
    failures = []
    by_region = entries_by_region(report)
    if set(by_region) != {"r-n", "r-s", "r-e"}:
        failures.append(f"组合集合异常: {sorted(by_region)}")
    for value, status in (("r-n", "completed"), ("r-s", "completed"), ("r-e", "empty")):
        entry = by_region.get(value)
        if not entry or entry["status"] != status:
            failures.append(f"{value} 状态应为 {status}: {entry and entry['status']}")
    if set(report["materials"]) != {"SAME-17@1"}:
        failures.append(f"资料编号版本异常: {sorted(report['materials'])}")
    expected_hits = {
        "r-n|k-n|p-n": ["SAME-17@1"],
        "r-s|k-s|p-s": ["SAME-17@1"],
        "r-e||": [],
    }
    if report["hits"] != expected_hits:
        failures.append(f"命中关系异常: {report['hits']}")
    empty = by_region.get("r-e", {})
    if empty.get("signature") != "r-e||" or not empty.get("request_id"):
        failures.append("空分支缺少完成证据(签名/请求ID)")
    return failures


def check_out_of_order(report):
    failures = []
    if not report.get("scenario_evidence", {}).get("rapid_parent_switch"):
        failures.append("未能制造快速改父项现场")
    completed = [e for e in report["entries"] if e["status"] == "completed"]
    signatures = {e["signature"] for e in completed}
    if signatures != {"r-n|k-n|p-n", "r-s|k-s|p-s"}:
        failures.append(f"有效组合签名异常: {sorted(signatures)}")
    for entry in report["entries"]:
        sig = entry["signature"] or ""
        if ("r-s|k-n" in sig) or ("r-n|k-s" in sig) or ("r-s|k-n|" in sig):
            failures.append(f"旧选项污染了新查询: {sig}")
    rn = entries_by_region(report).get("r-n", {})
    if rn.get("status") != "completed" or rn.get("attempts", 0) < 2:
        failures.append("r-n 应经历重试后完成")
    return failures


def check_failed_options(report):
    failures = []
    if not report.get("scenario_evidence", {}).get("recovered_after_failure"):
        failures.append("来源恢复编排未生效")
    by_region = entries_by_region(report)
    rn = by_region.get("r-n", {})
    if rn.get("status") != "failed" or rn.get("attempts") != 3:
        failures.append(f"r-n 应为有界重试后的失败: {rn.get('status')}/{rn.get('attempts')}")
    if "加载失败" not in (rn.get("error") or ""):
        failures.append(f"r-n 失败原因异常: {rn.get('error')}")
    if by_region.get("r-s", {}).get("status") != "completed":
        failures.append("r-s 应在来源恢复后完成")
    if by_region.get("r-e", {}).get("status") != "empty":
        failures.append("r-e 应为空分支")
    statuses = {e["status"] for e in by_region.values()}
    if statuses != {"failed", "completed", "empty"}:
        failures.append(f"进度表未区分失败/空/完成: {statuses}")
    return failures


SCENARIOS = [
    ("base", scenario_base, check_base),
    ("out-of-order", scenario_out_of_order, check_out_of_order),
    ("failed-options", scenario_failed_options, check_failed_options),
]


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    out_root = Path(argv[0]) if argv else Path("outputs/demo")
    failures = []
    with sync_playwright() as driver:
        browser = driver.chromium.launch(headless=True, args=["--no-sandbox"])
        for name, scenario, check in SCENARIOS:
            with running() as (base, _state):
                context = browser.new_context()
                page = context.new_page()
                page.goto(base)
                report = scenario(page, base)
                context.close()
            out = write_outputs(report, out_root / name)
            print(f"\n=== 场景 {name} ===")
            print(progress_markdown(report))
            print(f"导出: {out}")
            failures.extend(f"[{name}] {item}" for item in check(report))
        browser.close()
    if failures:
        print("\n演示检查未通过:")
        for item in failures:
            print(" -", item)
        return 1
    print("\n全部演示检查通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
