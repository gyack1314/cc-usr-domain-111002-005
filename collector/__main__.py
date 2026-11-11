"""命令行入口: python -m collector --intent intent.json --out outputs/run"""
import argparse
import sys
from contextlib import nullcontext

from playwright.sync_api import sync_playwright

from portal.__main__ import running

from .core import Collector, Intent, progress_markdown, write_outputs


def main(argv=None):
    parser = argparse.ArgumentParser(description="级联筛选门户资料采集")
    parser.add_argument("--intent", required=True, help="意图配置 JSON 路径")
    parser.add_argument("--out", required=True, help="输出目录")
    parser.add_argument("--url", help="已有门户地址; 缺省时本地启动门户")
    parser.add_argument("--headed", action="store_true", help="显示浏览器窗口")
    args = parser.parse_args(argv)

    intent = Intent.from_file(args.intent)
    server = nullcontext((args.url, None)) if args.url else running()
    with server as (base, _state):
        with sync_playwright() as driver:
            browser = driver.chromium.launch(headless=not args.headed, args=["--no-sandbox"])
            context = browser.new_context()
            page = context.new_page()
            page.goto(base)
            report = Collector(page, intent).run()
            browser.close()
    out = write_outputs(report, args.out)
    print(progress_markdown(report))
    counts = {}
    for entry in report["entries"]:
        counts[entry["status"]] = counts.get(entry["status"], 0) + 1
    print(
        "\n已导出: {out} (完成 {completed}, 空 {empty}, 失败 {failed}, 不可满足 {unsat})".format(
            out=out,
            completed=counts.get("completed", 0),
            empty=counts.get("empty", 0),
            failed=counts.get("failed", 0),
            unsat=counts.get("unsatisfiable", 0),
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
