"""采集模块的浏览器回归检查: 全部有效组合与页面可见结果签名对应核对。"""
import unittest

from playwright.sync_api import sync_playwright

from collector.core import Collector, Intent
from collector.demo import scenario_failed_options, scenario_out_of_order
from portal.__main__ import running

SETTLED = """(sel) => { const el = document.querySelector(sel);
  const l = el.dataset.loading; return l === 'false' || l === 'error'; }"""


class CollectorBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.driver = sync_playwright().start()
        cls.browser = cls.driver.chromium.launch(headless=True, args=["--no-sandbox"])

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.driver.stop()

    def setUp(self):
        self.server = running()
        self.base, self.state = self.server.__enter__()
        self.context = self.browser.new_context()
        self.page = self.context.new_page()
        self.page.goto(self.base)

    def tearDown(self):
        self.context.close()
        self.server.__exit__(None, None, None)

    def collect(self, intent, on_event=None):
        return Collector(self.page, Intent.from_dict(intent), on_event=on_event).run()

    def query_actions(self):
        return [a for a in self.state["actions"] if a.get("name") == "query"]

    # ---------- 基础遍历与导出 ----------

    def test_base_run_combinations_and_exports(self):
        report = self.collect({"regions": "all"})
        entries = {e["region"]["value"]: e for e in report["entries"]}
        self.assertEqual({"r-n", "r-s", "r-e"}, set(entries))
        self.assertEqual("completed", entries["r-n"]["status"])
        self.assertEqual("completed", entries["r-s"]["status"])
        self.assertEqual("empty", entries["r-e"]["status"])
        # 页面实际执行的条件与资料一起记录
        self.assertEqual("r-n|k-n|p-n", entries["r-n"]["signature"])
        self.assertEqual("r-s|k-s|p-s", entries["r-s"]["signature"])
        self.assertEqual("r-e||", entries["r-e"]["signature"])
        for entry in entries.values():
            self.assertTrue(entry["request_id"])
        # 同一资料保留各自命中关系, 正文按编号及内容版本保存
        self.assertEqual({"SAME-17@1"}, set(report["materials"]))
        self.assertEqual(["SAME-17@1"], report["hits"]["r-n|k-n|p-n"])
        self.assertEqual(["SAME-17@1"], report["hits"]["r-s|k-s|p-s"])
        self.assertEqual([], report["hits"]["r-e||"])
        # 页面动作记录与导出组合一一对应
        logged = {(a["signature"], a["request_id"]) for a in self.query_actions()}
        for entry in report["entries"]:
            self.assertIn((entry["signature"], entry["request_id"]), logged)

    def test_signature_correspondence_regression(self):
        """对生成的全部有效组合与可见结果签名做对应核对。"""
        report = self.collect({"regions": "all"})
        targets = [e for e in report["entries"] if e["signature"]]
        self.assertEqual(3, len(targets))
        for entry in targets:
            with self.subTest(signature=entry["signature"]):
                self.page.select_option("#region", entry["region"]["value"])
                self.page.wait_for_function(SETTLED, arg="#kind")
                if entry["kind"]:
                    self.page.select_option("#kind", entry["kind"]["value"])
                    self.page.wait_for_function(SETTLED, arg="#project")
                if entry["project"]:
                    self.page.select_option("#project", entry["project"]["value"])
                self.page.click("#query")
                self.page.wait_for_function(
                    """() => { const r = document.querySelector('#results');
                       const s = document.querySelector('#signature');
                       return r.dataset.requestId && r.dataset.requestId === s.dataset.requestId
                         && r.getAttribute('aria-busy') === 'false'; }"""
                )
                visible_sig = self.page.eval_on_selector("#signature", "el => el.textContent")
                self.assertEqual(entry["signature"], visible_sig)
                self.assertEqual(
                    entry["signature"],
                    self.page.eval_on_selector("#results", "el => el.dataset.signature"),
                )
                self.assertEqual(
                    self.page.eval_on_selector("#signature", "el => el.dataset.requestId"),
                    self.page.eval_on_selector("#results", "el => el.dataset.requestId"),
                )
                items = self.page.eval_on_selector_all(
                    "#results > p",
                    "els => els.map(e => ({id: e.dataset.id ?? null,"
                    " version: e.dataset.version ?? null, empty: e.dataset.empty === 'true'}))",
                )
                visible = sorted(f"{i['id']}@{i['version']}" for i in items if not i["empty"])
                self.assertEqual(sorted(entry["hits"]), visible)
                for key in visible:
                    self.assertIn(key, report["materials"])

    # ---------- 同名区域 ----------

    def test_duplicate_label_regions_selected_by_value(self):
        report = self.collect({"regions": ["中心区"]})
        completed = [e for e in report["entries"] if e["status"] == "completed"]
        self.assertEqual(2, len(completed))
        by_region = {e["region"]["value"]: e for e in completed}
        self.assertEqual({"r-n", "r-s"}, set(by_region))
        for entry in completed:
            self.assertEqual("中心区", entry["region"]["label"])
        # 同名不同键: 下级选项各自归属自己的父键
        self.assertEqual("r-n|k-n|p-n", by_region["r-n"]["signature"])
        self.assertEqual("r-s|k-s|p-s", by_region["r-s"]["signature"])
        self.assertEqual("k-n", by_region["r-n"]["kind"]["value"])
        self.assertEqual("k-s", by_region["r-s"]["kind"]["value"])

    def test_unsatisfiable_region_reported_not_fallback(self):
        report = self.collect({"regions": ["不存在的区"]})
        self.assertEqual([], [e for e in report["entries"] if e["status"] != "unsatisfiable"])
        self.assertEqual(1, len(report["unsatisfiable"]))
        self.assertEqual("不存在的区", report["unsatisfiable"][0]["spec"])
        self.assertEqual({}, report["hits"])
        self.assertEqual([], self.query_actions())

    # ---------- 乱序门闩 ----------

    def test_out_of_order_stale_options_do_not_pollute(self):
        report = scenario_out_of_order(self.page, self.base)
        self.assertTrue(report["scenario_evidence"]["rapid_parent_switch"])
        completed = [e for e in report["entries"] if e["status"] == "completed"]
        self.assertEqual({"r-n|k-n|p-n", "r-s|k-s|p-s"}, {e["signature"] for e in completed})
        for entry in report["entries"]:
            sig = entry["signature"] or ""
            self.assertNotIn("r-s|k-n", sig)
            self.assertNotIn("r-n|k-s", sig)
        rn = {e["region"]["value"]: e for e in completed}["r-n"]
        self.assertGreaterEqual(rn["attempts"], 2)
        # 页面动作记录里也不存在被污染的查询
        logged = {a["signature"] for a in self.query_actions()}
        self.assertEqual({"r-n|k-n|p-n", "r-s|k-s|p-s"}, logged)

    # ---------- 失败与空分支 ----------

    def test_failed_options_bounded_retry_and_recovery(self):
        report = scenario_failed_options(self.page, self.base)
        self.assertTrue(report["scenario_evidence"]["recovered_after_failure"])
        entries = {e["region"]["value"]: e for e in report["entries"]}
        self.assertEqual("failed", entries["r-n"]["status"])
        self.assertEqual(3, entries["r-n"]["attempts"])
        self.assertIn("加载失败", entries["r-n"]["error"])
        self.assertEqual("completed", entries["r-s"]["status"])
        self.assertEqual("empty", entries["r-e"]["status"])
        # 失败分支没有留下任何查询
        self.assertEqual(
            {"r-s|k-s|p-s", "r-e||"}, {a["signature"] for a in self.query_actions()}
        )

    # ---------- 选项被移除的有界重试 ----------

    def test_removed_option_recovers_by_rebuilding_from_parent(self):
        removed = {"done": False}

        def on_event(name, payload):
            if (
                name == "selecting"
                and payload.get("level") == "kind"
                and payload.get("value") == "k-n"
                and not removed["done"]
            ):
                removed["done"] = True
                self.page.eval_on_selector(
                    "#kind",
                    "el => { const o = [...el.options].find(o => o.value === 'k-n');"
                    " if (o) o.remove(); }",
                )

        report = self.collect(
            {"regions": [{"value": "r-n"}], "timeout_ms": 2000}, on_event=on_event
        )
        self.assertTrue(removed["done"])
        entry = report["entries"][0]
        self.assertEqual("completed", entry["status"])
        self.assertGreaterEqual(entry["attempts"], 2)
        self.assertEqual("r-n|k-n|p-n", entry["signature"])

    # ---------- 采集器不借用运维入口 ----------

    def test_collector_does_not_use_operator_endpoints(self):
        self.page.route("**/__control", lambda route: route.abort())
        self.page.route("**/__state", lambda route: route.abort())
        report = self.collect({"regions": "all"})
        statuses = {e["status"] for e in report["entries"]}
        self.assertEqual({"completed", "empty"}, statuses)


if __name__ == "__main__":
    unittest.main()
