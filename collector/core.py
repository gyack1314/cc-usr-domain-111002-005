"""级联筛选门户的浏览器采集核心。

只通过页面 DOM 读取选项与结果, 不使用门户后台接口(/api/*、/__*)。
设计要点:
- 下级选项随父项异步重建, 旧响应可能晚到; 每次选择按 value 定位,
  并核对控件与每个 option 的 data-parent 是否属于当前父键。
- 查询前先原子核对整链选择, 点击后等待 #signature 出现新的 request_id,
  再等待 #results 绑定同一 request_id 且签名一致, 才采集资料。
- 加载失败、加载超时、选项被移除、上级筛选被改变、结果不一致都结束
  当前组合尝试, 做有界重试; 重试从有效父项重新触发加载, 按 value 重新定位。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

SELECTORS = {"region": "#region", "kind": "#kind", "project": "#project"}
LEVEL_NAMES = {"region": "区域", "kind": "场所类型", "project": "检测项目"}

SNAPSHOT_JS = """() => {
  const read = (id) => {
    const el = document.querySelector(id);
    return {
      value: el.value,
      loading: el.dataset.loading ?? null,
      parent: el.dataset.parent ?? null,
      options: [...el.options].map((o) => ({
        value: o.value,
        label: o.label || o.textContent,
        parent: o.dataset.parent ?? null,
      })),
    };
  };
  return { region: read('#region'), kind: read('#kind'), project: read('#project') };
}"""

SETTLED_JS = """(sel) => {
  const el = document.querySelector(sel);
  const l = el.dataset.loading;
  return l === 'false' || l === 'error';
}"""


class AttemptError(Exception):
    """单次尝试失败(可有界重试): 加载失败/超时、选项被移除、上级被改变、结果不一致。"""


def real_options(options):
    """去掉 value 为空的“请选择”占位项。"""
    return [o for o in options if o["value"] != ""]


def level_error(state, parent_value):
    """下级控件状态是否属于 parent_value; 返回 None 表示一致, 否则返回原因。"""
    loading = state["loading"]
    if loading == "error":
        return "加载失败(data-loading=error)"
    if loading != "false":
        return f"加载未完成(data-loading={loading!r})"
    if state["parent"] != parent_value:
        return f"控件父键={state['parent']!r}, 期望父键={parent_value!r}"
    for option in real_options(state["options"]):
        if option["parent"] != parent_value:
            return f"选项 {option['value']!r} 的父键={option['parent']!r}, 期望={parent_value!r}"
    return None


def chain_error(snap, path):
    """整链核对: 区域值、类型层级与值、项目层级与值。path 为已确认选择的 value 前缀。"""
    if snap["region"]["value"] != path[0]:
        return f"区域选择被改变: 当前={snap['region']['value']!r}, 期望={path[0]!r}"
    err = level_error(snap["kind"], path[0])
    if err:
        return "场所类型层级: " + err
    if len(path) >= 2:
        if snap["kind"]["value"] != path[1]:
            return f"场所类型选择不符: 当前={snap['kind']['value']!r}, 期望={path[1]!r}"
        err = level_error(snap["project"], path[1])
        if err:
            return "检测项目层级: " + err
    if len(path) >= 3 and snap["project"]["value"] != path[2]:
        return f"检测项目选择不符: 当前={snap['project']['value']!r}, 期望={path[2]!r}"
    return None


def resolve_specs(specs, options):
    """把意图条件解析为可见选项。

    "all"/缺省 -> 当前可见全部; 字符串按标签匹配(同名标签展开为多个);
    {"value": ...}/{"label": ...} 精确指定。匹配不到的一律报告, 不退回“全部”。
    """
    if specs is None or specs == "all":
        return list(options), []
    if isinstance(specs, (str, dict)):
        specs = [specs]
    resolved, unsatisfiable, seen = [], [], set()
    for spec in specs:
        if isinstance(spec, str):
            key, wanted = "label", spec
        elif isinstance(spec, dict) and "value" in spec:
            key, wanted = "value", spec["value"]
        elif isinstance(spec, dict) and "label" in spec:
            key, wanted = "label", spec["label"]
        else:
            unsatisfiable.append(spec)
            continue
        matched = [o for o in options if o[key] == wanted]
        if not matched:
            unsatisfiable.append(spec)
        for option in matched:
            if option["value"] not in seen:
                seen.add(option["value"])
                resolved.append(option)
    return resolved, unsatisfiable


@dataclass
class Intent:
    """筛选意图: 各级条件("all" 或标签/value 列表)与有界重试参数。"""

    regions: object = "all"
    kinds: object = "all"
    projects: object = "all"
    max_attempts: int = 3
    timeout_ms: int = 5000

    @classmethod
    def from_dict(cls, data):
        known = {"regions", "kinds", "projects", "max_attempts", "timeout_ms"}
        unknown = sorted(set(data) - known)
        if unknown:
            raise ValueError(f"意图配置包含未知字段: {unknown}")
        intent = cls(
            regions=data.get("regions", "all"),
            kinds=data.get("kinds", "all"),
            projects=data.get("projects", "all"),
            max_attempts=int(data.get("max_attempts", 3)),
            timeout_ms=int(data.get("timeout_ms", 5000)),
        )
        if intent.max_attempts < 1:
            raise ValueError("max_attempts 必须 >= 1")
        if intent.timeout_ms < 100:
            raise ValueError("timeout_ms 过小")
        return intent

    @classmethod
    def from_file(cls, path):
        with open(path, encoding="utf-8") as fh:
            return cls.from_dict(json.load(fh))

    def to_dict(self):
        return {
            "regions": self.regions,
            "kinds": self.kinds,
            "projects": self.projects,
            "max_attempts": self.max_attempts,
            "timeout_ms": self.timeout_ms,
        }


class Collector:
    """按意图遍历有效组合并采集页面实际执行的条件与资料。

    on_event(name, payload) 可观察进度事件:
    level_load_start / selecting / attempt_failed / entry。
    """

    def __init__(self, page, intent, on_event=None):
        self.page = page
        self.intent = intent
        self.on_event = on_event
        self.entries = []
        self.unsatisfiable = []
        self.materials = {}
        self.hits = {}

    # ---------- 基础工具 ----------

    def emit(self, name, **payload):
        if self.on_event:
            self.on_event(name, payload)

    def snapshot(self):
        return self.page.evaluate(SNAPSHOT_JS)

    def wait_ready(self):
        try:
            self.page.wait_for_function(
                "() => document.querySelectorAll('#region option').length > 1",
                timeout=self.intent.timeout_ms,
            )
        except PlaywrightTimeoutError as exc:
            raise AttemptError("页面区域选项未就绪") from exc

    def wait_settled(self, level):
        try:
            self.page.wait_for_function(
                SETTLED_JS, arg=SELECTORS[level], timeout=self.intent.timeout_ms
            )
        except PlaywrightTimeoutError as exc:
            raise AttemptError(f"{LEVEL_NAMES[level]}加载超时(来源可能未释放)") from exc

    def select_value(self, level, value):
        self.emit("selecting", level=level, value=value)
        try:
            self.page.select_option(SELECTORS[level], value, timeout=self.intent.timeout_ms)
        except PlaywrightError as exc:
            raise AttemptError(
                f"选择{LEVEL_NAMES[level]}={value!r}失败(选项可能被移除): {exc}"
            ) from exc
        snap = self.snapshot()
        if snap[level]["value"] != value:
            raise AttemptError(
                f"{LEVEL_NAMES[level]}选择未生效: 当前={snap[level]['value']!r}, 期望={value!r}"
            )

    def dispatch_change(self, level):
        try:
            self.page.dispatch_event(SELECTORS[level], "change")
        except PlaywrightError as exc:
            raise AttemptError(f"重新触发{LEVEL_NAMES[level]}加载失败: {exc}") from exc

    @staticmethod
    def require_option(level_state, value, parent):
        for option in real_options(level_state["options"]):
            if option["value"] == value and (parent is None or option["parent"] == parent):
                return
        raise AttemptError(f"选项已被移除: value={value!r}")

    # ---------- 链路建立与读取 ----------

    def ensure_chain(self, path, force=False):
        """建立并核对已确认选择链; force=True 时从有效父项重新触发下级加载。"""
        snap = self.snapshot()
        if snap["region"]["value"] != path[0]:
            self.require_option(snap["region"], path[0], None)
            self.select_value("region", path[0])
            self.emit("level_load_start", level="kind", parent=path[0])
            self.wait_settled("kind")
        elif force or level_error(snap["kind"], path[0]):
            self.dispatch_change("region")
            self.emit("level_load_start", level="kind", parent=path[0])
            self.wait_settled("kind")
        if len(path) >= 2:
            snap = self.snapshot()
            err = chain_error(snap, path[:1])
            if err:
                raise AttemptError(err)
            if snap["kind"]["value"] != path[1]:
                self.require_option(snap["kind"], path[1], path[0])
                self.select_value("kind", path[1])
                self.emit("level_load_start", level="project", parent=path[1])
                self.wait_settled("project")
            elif force or level_error(snap["project"], path[1]):
                self.dispatch_change("kind")
                self.emit("level_load_start", level="project", parent=path[1])
                self.wait_settled("project")
        if len(path) >= 3:
            snap = self.snapshot()
            err = chain_error(snap, path[:2])
            if err:
                raise AttemptError(err)
            if snap["project"]["value"] != path[2]:
                self.require_option(snap["project"], path[2], path[1])
                self.select_value("project", path[2])
        err = chain_error(self.snapshot(), path)
        if err:
            raise AttemptError(err)

    def read_options(self, path, level):
        """在链路一致的前提下读取当前下级真实选项(按父键核对过)。"""
        snap = self.snapshot()
        err = chain_error(snap, path)
        if err:
            raise AttemptError(err)
        return real_options(snap[level]["options"])

    # ---------- 查询执行 ----------

    def execute_query(self, expected_sig, prefix):
        """执行查询; 只有签名与 request_id 都和已确认选择一致才返回结果。"""
        err = chain_error(self.snapshot(), prefix)
        if err:
            raise AttemptError("查询前确认失败: " + err)
        prev = self.page.eval_on_selector("#signature", "el => el.dataset.requestId ?? null")
        try:
            self.page.click("#query", timeout=self.intent.timeout_ms)
        except PlaywrightError as exc:
            raise AttemptError(f"查询按钮点击失败: {exc}") from exc
        try:
            self.page.wait_for_function(
                "(prev) => { const s = document.querySelector('#signature');"
                " return !!s.dataset.requestId && s.dataset.requestId !== prev; }",
                arg=prev,
                timeout=self.intent.timeout_ms,
            )
        except PlaywrightTimeoutError as exc:
            raise AttemptError("条件签名未更新") from exc
        sig_text = self.page.eval_on_selector("#signature", "el => el.textContent")
        request_id = self.page.eval_on_selector("#signature", "el => el.dataset.requestId")
        if sig_text != expected_sig:
            raise AttemptError(
                f"页面执行条件 {sig_text!r} 与已确认选择 {expected_sig!r} 不一致"
            )
        try:
            self.page.wait_for_function(
                "(rid) => { const r = document.querySelector('#results');"
                " return r.dataset.requestId === rid && r.getAttribute('aria-busy') === 'false'; }",
                arg=request_id,
                timeout=self.intent.timeout_ms,
            )
        except PlaywrightTimeoutError as exc:
            raise AttemptError("结果面板未绑定本次请求") from exc
        results_sig = self.page.eval_on_selector("#results", "el => el.dataset.signature ?? null")
        if results_sig != expected_sig:
            raise AttemptError(
                f"结果签名 {results_sig!r} 与已确认选择 {expected_sig!r} 不一致"
            )
        items = self.page.eval_on_selector_all(
            "#results > p",
            "els => els.map(e => ({id: e.dataset.id ?? null,"
            " version: e.dataset.version ?? null,"
            " empty: e.dataset.empty === 'true', text: e.textContent}))",
        )
        return {"signature": sig_text, "request_id": request_id, "items": items}

    # ---------- 遍历 ----------

    def run(self):
        started = datetime.now(timezone.utc).isoformat()
        self.wait_ready()
        snap = self.snapshot()
        regions, unsat = resolve_specs(self.intent.regions, real_options(snap["region"]["options"]))
        for spec in unsat:
            self.add_unsatisfiable("region", spec)
        for region in regions:
            self.collect_region(region)
        return self.report(started)

    def collect_region(self, region, inherited=None):
        path = [region["value"]]
        kinds = None
        history = list(inherited or [])
        for attempt in range(1, self.intent.max_attempts + 1):
            try:
                self.ensure_chain(path, force=attempt > 1)
                kinds = self.read_options(path, "kind")
                break
            except AttemptError as exc:
                history.append({"level": "region", "attempt": attempt, "error": str(exc)})
                self.emit("attempt_failed", path=path, attempt=attempt, error=str(exc))
        if kinds is None:
            self.add_entry(self.failed_entry(region, None, None, history))
            return
        resolved, unsat = resolve_specs(self.intent.kinds, kinds)
        for spec in unsat:
            self.add_unsatisfiable("kind", spec, region=region)
        if not kinds:
            self.run_leaf(
                (region["value"], "", ""),
                (region, None, None),
                note="空分支: 该区域没有场所类型选项",
                inherited=history,
            )
            return
        for kind in resolved:
            self.collect_kind(region, kind, inherited=history)

    def collect_kind(self, region, kind, inherited=None):
        path = [region["value"], kind["value"]]
        projects = None
        history = list(inherited or [])
        for attempt in range(1, self.intent.max_attempts + 1):
            try:
                self.ensure_chain(path, force=attempt > 1)
                projects = self.read_options(path, "project")
                break
            except AttemptError as exc:
                history.append({"level": "kind", "attempt": attempt, "error": str(exc)})
                self.emit("attempt_failed", path=path, attempt=attempt, error=str(exc))
        if projects is None:
            self.add_entry(self.failed_entry(region, kind, None, history))
            return
        resolved, unsat = resolve_specs(self.intent.projects, projects)
        for spec in unsat:
            self.add_unsatisfiable("project", spec, region=region, kind=kind)
        if not projects:
            self.run_leaf(
                (region["value"], kind["value"], ""),
                (region, kind, None),
                note="空分支: 该类型下没有检测项目选项",
                inherited=history,
            )
            return
        for project in resolved:
            self.run_leaf(
                (region["value"], kind["value"], project["value"]),
                (region, kind, project),
                inherited=history,
            )

    def run_leaf(self, values, refs, note=None, inherited=None):
        """单个组合: values 尾部可为 ''(空分支的证据查询)。"""
        prefix = [v for v in values if v != ""]
        expected_sig = "|".join(values)
        history = list(inherited or [])
        leaf_attempts = 0
        for attempt in range(1, self.intent.max_attempts + 1):
            leaf_attempts = attempt
            try:
                self.ensure_chain(prefix, force=attempt > 1)
                outcome = self.execute_query(expected_sig, prefix)
            except AttemptError as exc:
                history.append({"level": "leaf", "attempt": attempt, "error": str(exc)})
                self.emit("attempt_failed", path=list(values), attempt=attempt, error=str(exc))
                continue
            items = outcome["items"]
            hit_keys = []
            for item in items:
                if item["empty"]:
                    continue
                key = f"{item['id']}@{item['version']}"
                material = self.materials.setdefault(
                    key,
                    {
                        "id": item["id"],
                        "version": item["version"],
                        "content": item["text"],
                        "other_contents": [],
                        "signatures": [],
                    },
                )
                if item["text"] != material["content"] and item["text"] not in material["other_contents"]:
                    material["other_contents"].append(item["text"])
                if expected_sig not in material["signatures"]:
                    material["signatures"].append(expected_sig)
                if key not in hit_keys:
                    hit_keys.append(key)
            self.hits[expected_sig] = hit_keys
            empty = not hit_keys
            self.add_entry(
                {
                    "region": refs[0],
                    "kind": refs[1],
                    "project": refs[2],
                    "status": "empty" if empty else "completed",
                    "attempts": leaf_attempts + len([h for h in history if h["level"] != "leaf"]),
                    "signature": outcome["signature"],
                    "request_id": outcome["request_id"],
                    "hits": hit_keys,
                    "note": note or ("查询无资料" if empty else ""),
                    "error": None,
                    "history": history,
                }
            )
            return
        self.add_entry(self.failed_entry(refs[0], refs[1], refs[2], history, note=note))

    # ---------- 进度记录 ----------

    def failed_entry(self, region, kind, project, history, note=None):
        return {
            "region": region,
            "kind": kind,
            "project": project,
            "status": "failed",
            "attempts": len(history) or self.intent.max_attempts,
            "signature": None,
            "request_id": None,
            "hits": [],
            "note": note or "",
            "error": history[-1]["error"] if history else "未知错误",
            "history": history,
        }

    def add_unsatisfiable(self, level, spec, region=None, kind=None):
        record = {
            "level": level,
            "spec": spec,
            "reason": "输入不在当前可见范围",
            "region": (region or {}).get("value"),
            "kind": (kind or {}).get("value"),
        }
        self.unsatisfiable.append(record)
        entry = {
            "region": region if region else None,
            "kind": kind if kind else None,
            "project": None,
            "status": "unsatisfiable",
            "attempts": 0,
            "signature": None,
            "request_id": None,
            "hits": [],
            "note": f"{LEVEL_NAMES[level]}条件不在可见范围: {json.dumps(spec, ensure_ascii=False)}",
            "error": None,
            "history": [],
        }
        if level == "region":
            entry["region"] = {"spec": spec}
        elif level == "kind":
            entry["kind"] = {"spec": spec}
        else:
            entry["project"] = {"spec": spec}
        self.add_entry(entry)

    def add_entry(self, entry):
        self.entries.append(entry)
        self.emit("entry", **entry)

    def report(self, started):
        return {
            "generator": "collector/1.0",
            "portal": self.page.url,
            "intent": self.intent.to_dict(),
            "started_at": started,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "entries": self.entries,
            "unsatisfiable": self.unsatisfiable,
            "materials": self.materials,
            "hits": self.hits,
        }


STATUS_LABELS = {
    "completed": "完成",
    "empty": "空分支/无资料",
    "failed": "失败",
    "unsatisfiable": "不可满足",
}


def ref_cell(ref):
    if ref is None:
        return "—"
    if "spec" in ref:
        return f"输入: {json.dumps(ref['spec'], ensure_ascii=False)}"
    return f"{ref['label']} ({ref['value']})"


def progress_markdown(report):
    lines = [
        "| 区域 | 场所类型 | 检测项目 | 状态 | 尝试 | 页面签名 | 请求ID | 命中资料 | 说明 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for entry in report["entries"]:
        signature = (entry["signature"] or "—").replace("|", "\\|")
        note = (entry["note"] or entry["error"] or "—").replace("|", "\\|")
        lines.append(
            "| {region} | {kind} | {project} | {status} | {attempts} | {sig} | {req} | {hits} | {note} |".format(
                region=ref_cell(entry["region"]),
                kind=ref_cell(entry["kind"]),
                project=ref_cell(entry["project"]),
                status=STATUS_LABELS.get(entry["status"], entry["status"]),
                attempts=entry["attempts"],
                sig=signature,
                req=entry["request_id"] or "—",
                hits=", ".join(entry["hits"]) or "—",
                note=note,
            )
        )
    return "\n".join(lines)


def write_outputs(report, out_dir):
    """导出组合进度与结果: report.json / progress.md / materials.json / hits.json。"""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out / "progress.md").write_text(
        "# 组合进度\n\n" + progress_markdown(report) + "\n", encoding="utf-8"
    )
    (out / "materials.json").write_text(
        json.dumps(report["materials"], ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out / "hits.json").write_text(
        json.dumps(report["hits"], ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return out
