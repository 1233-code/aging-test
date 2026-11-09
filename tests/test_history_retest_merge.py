"""回归测试: 历史记录「重测失败项」的**合并** (§7.7)。

起因是「重测失败项」这个功能只做了一半:

    mini-run 会把**逐项结果**就地写回被重测的那一轮(父轮) —— 这部分早就有。
    但父轮自己的 status / summary_json 从来不重算, 它的 PDF 也从来不重导。

于是重测完, 界面上是:
    逐项写着「✅ 通过」   (mini-run 写回去的)
    汇总却写着「❌ 失败 3/5」  (父轮自己的 summary, 还停在重测前)
    点「打开 PDF」看到的还是失败那版报告。

三处自相矛盾 —— 而报告是**纸面归档**用的东西, 归档的人不会去猜哪一份是对的。

这个测试把「合并」这三件事钉死:
    ① 重算口径与 §9.2 一致 (total = passed+failed+error; skipped/stopped 不计;
       有失败/错误 → failed; total==0 → skipped, **不得**判 passed)
    ② 合并后父轮变「通过」, 计数 = 原来通过的项 + 重测后通过的项 (**合并**,
       不是只算重测的那几项)
    ③ 父轮的 PDF 被**重导**, 且新报告里同时有原来通过项和新通过项
       —— 这就是「结合前一次测试的报告合成新的报告」
    ④「第 N 次测试通过」: 首测 = 1, 第一次重测 = 2, 第二次重测 = 3
    ⑤ 合并后父轮在历史列表里排**最上面** (工人刚合完, 不该找不到它)
    ⑥ 只修好一部分 → 父轮仍是 failed, 但计数与报告都更新
    ⑦ PDF 导出失败**不许**掀翻合并 (§8.2 硬规则: 报告失败不阻断结果)
    ⑧ 父轮里残留的 running 行(中途被停的那一项)→ stopped, 计数不缺项
    ⑨ 合并后 runner 的「当前这一轮」切到父轮 —— 总结页显示的是合并后的
       完整结果(5 通过), 不是重测轮自己那 2 项
    ⑩ 边界: 注册表里已经没了的测试项不能重测, 但**不能**因此把整次重测打回

用**真库**(临时 SQLite 文件)+ 假 report: 计数口径那些逻辑是这次要验的主体,
用假库抄一遍等于没验。临时文件建在系统临时目录, **不碰** reports/history.db,
也**不碰** reports/pdf/。

跑法: .venv/bin/python -m tests.test_history_retest_merge
"""
import asyncio
import json
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import db as DB
from server import report as report_mod
from server import runner as R
from server.db import Database
from server.tests.base import TestCase, TestResult
from tests._tmp import mkdtemp

FAILS = []
PASSES = 0


def check(cond, msg, extra=""):
    global PASSES
    if cond:
        PASSES += 1
        print("    ✓ " + msg)
    else:
        FAILS.append(msg)
        print("    ✗ " + msg + ("  → " + str(extra) if extra else ""))


def section(title):
    print("\n" + title)


# ──────────────────────── 假 report (不碰 chromium) ────────────────────────

class FakeReportError(RuntimeError):
    pass


class FakeReport:
    """假 report: 只把「导了什么」记下来, 不生成任何文件。"""

    CALLS = []          # 每次 export_pdf 的入参快照
    FAIL = False        # 打开它 = 模拟 chromium 挂了

    class ReportError(FakeReportError):
        pass

    @classmethod
    async def export_pdf(cls, run, results, retest_runs=None):
        cls.CALLS.append({
            "run_id": run.get("id"),
            "status": run.get("status"),
            "summary": dict(run.get("summary") or {}),
            "retest_count": run.get("retest_count"),
            "results": [
                {"test_name": r.get("test_name"),
                 "status": r.get("status"),
                 "attempt": r.get("attempt")}
                for r in results
            ],
            # §7.7: 重测轮的日志单独一节印在「日志附录」后面 —— 这里把
            # 它**原样**记下来, 供用例核对「父轮的原始日志没被动过」。
            "retest_runs": retest_runs or [],
        })
        if cls.FAIL:
            raise cls.ReportError("假失败: 模拟 chromium 挂了")
        return "/tmp/fake-report-%s.pdf" % run.get("id")

    @staticmethod
    def find_chromium():
        return "/fake/chromium"

    @staticmethod
    def output_dir():
        return None


# ──────────────────────── 假测试项 (不碰真硬件) ────────────────────────

# 假测试项的当前状态 —— 用例之间可以改, 用来模拟「第一次失败、重测通过」。
STATUS = {}

# 假测试项这一轮要吐的日志行 —— {test_name: [行, ...]}。
# 用来验「重测之后, 父轮那一项的日志是不是**两轮都有**」。
LOGS = {}

# 假测试项的「卡住」开关 —— 用来在**重测跑到一半**的时候检查库里的状态。
#   BLOCK[name]   = 放行信号(不给就不往下走)
#   BLOCKED[name] = 已经卡住了(测试用来等这个)
BLOCK = {}
BLOCKED = {}

# 假测试项跑完之后要不要顺手叫停 —— 模拟「工人中途按了暂停」(§9.3)。
#   STOP_AFTER[name] = True → 这一项跑完就调 runner.stop()
#   RUNNER_REF["runner"] = 当前那个 runner(由 run_mini 填)
STOP_AFTER = {}
RUNNER_REF = {}


def make_test(name):
    class T(TestCase):
        def __init__(self):
            self.name = name
            self.display_name = "假测试项-" + name

        async def run(self, ctx):
            gate = BLOCK.get(name)
            if gate is not None:
                ev = BLOCKED.setdefault(name, asyncio.Event())
                ev.set()
                await gate.wait()
            for line in LOGS.get(name, []):
                await ctx.on_log(line)
            if STOP_AFTER.get(name):
                rn = RUNNER_REF.get("runner")
                if rn is not None:
                    await rn.stop()
            return TestResult(
                status=STATUS.get(name, "passed"),
                error=STATUS.get(name + "_err"),
            )

    T.name = name
    T.display_name = "假测试项-" + name
    return T


for _n in ("fx_a", "fx_b", "fx_c"):
    R.TEST_REGISTRY[_n] = make_test(_n)


# ──────────────────────── 建轮次的工具 ────────────────────────

async def make_parent(results, status="failed", sn="TESTSN0001"):
    """建一条「父轮」: config + 逐项结果 + 收尾。

    results = {test_name: status}
    """
    config = {"tests": {n: {"enabled": True} for n in results}}
    rid = await Database.create_run(
        config, {"sn": sn, "operator": "产线甲", "batch": "B1"})
    for n, st in results.items():
        await Database.upsert_result(
            rid, n, status=st, duration_sec=1.0, metrics={},
            log="日志-" + n,
            error=None if st in ("passed",) else "失败原因-" + n,
        )
    summary = {
        "total": sum(1 for s in results.values() if s in ("passed", "failed", "error")),
        "passed": sum(1 for s in results.values() if s == "passed"),
        "failed": sum(1 for s in results.values() if s == "failed"),
        "error": 0, "skipped": 0, "not_run": 0,
        "planned": len(results),
    }
    await Database.finish_run(rid, status, summary)
    return rid


async def run_mini(parent_id, names):
    """起一个重测轮并等它跑完, 返回 (runner, 广播出去的事件)。"""
    FakeReport.CALLS.clear()
    rn = R.TestRunner()
    RUNNER_REF["runner"] = rn        # 假测试项要叫停时用得上
    q = rn.subscribe()
    res = await rn.start_mini(parent_id, names)
    if not res.get("ok"):
        return rn, res, []
    await rn._task
    events = []
    while not q.empty():
        events.append(q.get_nowait())
    return rn, res, events


def events_of(events, kind):
    return [e for e in events if e.get("type") == kind]


# ──────────────────────── 用例 ────────────────────────

async def case1():
    section("【1】重算口径必须跟 §9.2 一致 (不是各算各的)")
    rid = await make_parent({"fx_a": "passed", "fx_b": "failed", "fx_c": "passed"})
    r = await Database.recompute_run(rid)
    check(r is not None, "能重算出结果")
    check(r["status"] == "failed", "有失败项 → failed", r["status"])
    check(r["summary"]["total"] == 3, "total = 3 (passed+failed+error)",
          r["summary"]["total"])
    check(r["summary"]["passed"] == 2, "passed = 2", r["summary"]["passed"])
    check(r["summary"]["failed"] == 1, "failed = 1", r["summary"]["failed"])
    check(r["summary"]["planned"] == 3, "planned = 3 (勾选数)",
          r["summary"]["planned"])

    # 全跳过 → total=0 → skipped, **不得**判 passed
    rid2 = await make_parent({"fx_a": "skipped", "fx_b": "skipped"})
    r2 = await Database.recompute_run(rid2)
    check(r2["status"] == "skipped",
          "一项都没实际执行 → skipped (不是 passed —— §9.2 明令)", r2["status"])
    check(r2["summary"]["total"] == 0, "total = 0", r2["summary"]["total"])


async def case2():
    section("【2】重测通过 → 父轮变「通过」, 计数是**合并**的")
    parent = await make_parent({"fx_a": "passed", "fx_b": "passed",
                                "fx_c": "failed"})
    STATUS["fx_c"] = "passed"          # 这次重测过了
    rn, res, events = await run_mini(parent, ["fx_c"])
    check(res.get("ok"), "重测轮起得来", res)
    check(res.get("parent_run_id") == parent, "合并目标是那条历史记录", res)

    run = await Database.get_run(parent)
    check(run["status"] == "passed",
          "父轮从 failed 变成 passed", run["status"])
    check(run["summary"]["passed"] == 3, "passed = 3 (原来 2 个 + 重测 1 个)",
          run["summary"]["passed"])
    check(run["summary"]["failed"] == 0, "failed = 0", run["summary"]["failed"])
    check(run["summary"]["total"] == 3, "total = 3", run["summary"]["total"])
    check(run["retest_count"] == 1, "retest_count = 1 (被重测过一轮)",
          run["retest_count"])

    ev = events_of(events, "parent_merged")
    check(len(ev) == 1, "广播了 parent_merged", len(ev))
    if ev:
        check(ev[0]["parent_run_id"] == parent, "事件里带的是父轮 id", ev[0])
        check(ev[0]["status"] == "passed", "事件里带合并后的状态", ev[0]["status"])

    fin = events_of(events, "run_finished")
    check(len(fin) == 1, "广播了 run_finished", len(fin))
    if fin:
        check(fin[0]["status"] == "passed",
              "run_finished 带的是**合并后**的状态, 不是重测轮自己的", fin[0]["status"])
        check(fin[0]["summary"]["passed"] == 3,
              "run_finished 的计数也是合并后的 3", fin[0]["summary"]["passed"])


async def case1b():
    section("【1b】error 项也要计入 total —— 「没测成」也是这一项的结论")
    # §9.2: total = passed + failed + error。error(这一项没测成)照样占一格 ——
    # 漏掉它的话, 汇总里的「共测 N 项」和逐项列表对不上账。
    rid = await make_parent({"fx_a": "passed", "fx_b": "error"})
    r = await Database.recompute_run(rid)
    check(r["summary"]["total"] == 2, "total = 2 (error 也占一格)",
          r["summary"]["total"])
    check(r["summary"]["error"] == 1, "error = 1", r["summary"]["error"])
    check(r["summary"]["passed"] == 1, "passed = 1", r["summary"]["passed"])
    check(r["status"] == "failed",
          "有 error → 整轮 failed (这一项没测成, 不能当通过)", r["status"])

    # 重测这一项并通过 → 父轮变 passed, total 仍是 2
    STATUS["fx_b"] = "passed"
    await run_mini(rid, ["fx_b"])
    run = await Database.get_run(rid)
    check(run["status"] == "passed", "重测通过后父轮 passed", run["status"])
    check(run["summary"]["passed"] == 2, "passed = 2", run["summary"]["passed"])
    check(run["summary"]["error"] == 0, "error = 0", run["summary"]["error"])
    check(run["summary"]["total"] == 2, "total = 2", run["summary"]["total"])


async def case3():
    section("【3】报告是**合成**的: 原来通过的项 + 重测后通过的项, 都在新报告里")
    parent = await make_parent({"fx_a": "passed", "fx_b": "passed",
                                "fx_c": "failed"})
    STATUS["fx_c"] = "passed"
    rn, res, events = await run_mini(parent, ["fx_c"])

    check(len(FakeReport.CALLS) == 2,
          "导了两次: 重测轮自己一份 + 父轮合并版一份", len(FakeReport.CALLS))
    merged = [c for c in FakeReport.CALLS if c["run_id"] == parent]
    check(len(merged) == 1, "父轮那份被重导了", len(merged))
    if merged:
        names = [r["test_name"] for r in merged[0]["results"]]
        check(sorted(names) == ["fx_a", "fx_b", "fx_c"],
              "合并版报告里三项都在 (原来通过的 fx_a/fx_b 也在)", names)
        st = {r["test_name"]: r["status"] for r in merged[0]["results"]}
        check(st.get("fx_c") == "passed", "fx_c 在新报告里是 passed", st.get("fx_c"))
        check(merged[0]["status"] == "passed", "报告头部的整体结论是 passed",
              merged[0]["status"])
        check(merged[0]["summary"]["passed"] == 3,
              "报告里的汇总是合并后的 3/3", merged[0]["summary"]["passed"])
    check(rn.pdf_path == "/tmp/fake-report-%s.pdf" % parent,
          "runner 指向的是父轮那份合并报告", rn.pdf_path)


async def case4():
    section("【4】「第 N 次测试通过」: 首测 1 → 第一次重测 2 → 第二次重测 3")
    parent = await make_parent({"fx_a": "passed", "fx_c": "failed"})
    STATUS["fx_c"] = "passed"
    await run_mini(parent, ["fx_c"])
    run = await Database.get_run(parent)
    att = {r["test_name"]: r["attempt"] for r in run["results"]}
    check(att.get("fx_a") == 1, "没重测过的那项 attempt = 1", att.get("fx_a"))
    check(att.get("fx_c") == 2, "重测过一次 → 第 2 次", att.get("fx_c"))

    # 再重测一次 (这次还是过的)
    STATUS["fx_c"] = "passed"
    await run_mini(parent, ["fx_c"])
    run = await Database.get_run(parent)
    att = {r["test_name"]: r["attempt"] for r in run["results"]}
    check(att.get("fx_c") == 3, "再重测一次 → 第 3 次", att.get("fx_c"))
    check(run["retest_count"] == 2, "retest_count = 2", run["retest_count"])


async def case5():
    section("【5】合并后父轮在历史列表里排**最上面**")
    # ⚠️ 被合并的必须是**先建**的那条 —— 它 started_at 更早, 可它刚刚才动过。
    #    这样才区分得出两种排序: 按 updated_at 排 → 它排第一(刚合完, 工人
    #    找得到); 按 started_at 排 → 它沉底(工人刚合完却找不到它)。
    #    (两个用例里的记录都是先建后合, 排序写错了也看不出来 —— 踩过。)
    old = await make_parent({"fx_a": "passed", "fx_c": "failed"})
    other = await make_parent({"fx_a": "passed"})
    STATUS["fx_c"] = "passed"
    await run_mini(old, ["fx_c"])          # 重测**先建**的那条
    rows = await Database.list_runs(50)
    ids = [r["id"] for r in rows]
    check(ids and ids[0] == old,
          "刚合并过的那条排第一 (它刚刚才动过, 是最新的状态)", ids[:3])
    check(old in ids and other in ids, "两条记录都还在列表里", ids)


async def case6():
    section("【6】只修好一部分 → 父轮仍 failed, 但计数与报告都更新")
    parent = await make_parent({"fx_a": "passed", "fx_b": "failed",
                                "fx_c": "failed"})
    STATUS["fx_c"] = "passed"          # fx_c 修好了, fx_b 没重测(仍失败)
    await run_mini(parent, ["fx_c"])
    run = await Database.get_run(parent)
    check(run["status"] == "failed", "还有失败项 → 仍是 failed", run["status"])
    check(run["summary"]["passed"] == 2, "passed = 2", run["summary"]["passed"])
    check(run["summary"]["failed"] == 1, "failed = 1", run["summary"]["failed"])
    check(run["summary"]["total"] == 3, "total = 3", run["summary"]["total"])
    merged = [c for c in FakeReport.CALLS if c["run_id"] == parent]
    check(len(merged) == 1, "报告照样重导了 (不然界面和报告会打架)", len(merged))


async def case7():
    section("【7】PDF 导出失败**不许**掀翻合并 (§8.2 硬规则)")
    parent = await make_parent({"fx_a": "passed", "fx_c": "failed"})
    STATUS["fx_c"] = "passed"
    FakeReport.FAIL = True
    try:
        rn, res, events = await run_mini(parent, ["fx_c"])
    finally:
        FakeReport.FAIL = False
    run = await Database.get_run(parent)
    check(run["status"] == "passed",
          "报告导不出来, 但状态照样合并成 passed", run["status"])
    check(run["summary"]["passed"] == 2, "计数照样合并", run["summary"]["passed"])
    ev = events_of(events, "report_failed")
    check(len(ev) >= 1, "有 report_failed 事件 (界面能提示手动重导)", len(ev))
    # 重测轮自己那份也导不出来(同一个假 report), 所以会有**两条** report_failed:
    # 一条是重测轮的, 一条是父轮合并版的。父轮那条才是要紧的 ——
    # 工人要知道「记录 #N 的报告还没更新, 得手动重导」。
    check(any(e.get("run_id") == parent for e in ev),
          "有一条 report_failed 指的是父轮那份 (提示要手动重导)", ev)


async def case8():
    section("【8】父轮里残留的 running 行 → stopped, 计数不缺项, **整轮不许判通过**")
    parent = await make_parent({"fx_a": "passed", "fx_b": "passed",
                                "fx_c": "failed"})
    # 模拟「重测中途被停」: 父轮那一项停在 running
    await Database.upsert_result(parent, "fx_c", status="running")
    n = await Database.fix_stale_running(parent)
    check(n == 1, "改掉 1 行", n)
    run = await Database.get_run(parent)
    st = {r["test_name"]: r["status"] for r in run["results"]}
    check(st.get("fx_c") == "stopped", "残留的 running 变成 stopped", st.get("fx_c"))
    r = await Database.recompute_run(parent)
    check(r["summary"]["not_run"] == 1, "not_run = 1 (它单列, 不进 total)",
          r["summary"]["not_run"])
    check(r["summary"]["total"] == 2, "total = 2 (stopped 不计入)", r["summary"]["total"])
    check(r["status"] == "stopped",
          "🚨 整轮**不能**判 passed —— 还有一项没跑到(§9.3/§7.7.2)",
          r["status"])


async def case9():
    section("【9】合并后 runner 的「当前这一轮」切到父轮 (总结页显示合并结果)")
    parent = await make_parent({"fx_a": "passed", "fx_b": "passed",
                                "fx_c": "failed"})
    STATUS["fx_c"] = "passed"
    rn, res, events = await run_mini(parent, ["fx_c"])
    check(rn.run_id == parent, "当前这轮 = 父轮", rn.run_id)
    check(rn.status == "passed", "状态 = 合并后的 passed", rn.status)
    check(len(rn.results) == 3,
          "逐项结果是父轮的**全部**三项, 不是重测那一项", list(rn.results))
    check(sorted(rn.test_queue) == ["fx_a", "fx_b", "fx_c"],
          "队列也是父轮的全队列", rn.test_queue)
    snap = rn.snapshot()
    check(snap["run_id"] == parent, "快照里的 run_id = 父轮", snap["run_id"])
    check(len(snap["results"]) == 3,
          "快照里有全部三项 (刷新页面也不会退回「只测了 1 项」)",
          list(snap["results"]))
    check(snap["status"] == "passed", "快照状态 = passed", snap["status"])


async def case10():
    section("【10】边界: 注册表里已经没了的项不能重测, 但不因此打回整次重测")
    parent = await make_parent({"fx_a": "passed", "fx_c": "failed"})
    STATUS["fx_c"] = "passed"
    FakeReport.CALLS.clear()
    rn = R.TestRunner()
    res = await rn.start_mini(parent, ["fx_c", "fx_removed"])
    await rn._task
    check(res.get("ok"), "重测照样起得来", res)
    check(res.get("tests") == ["fx_c"],
          "只重测还在册的那一项", res.get("tests"))
    check(res.get("dropped_tests") == ["fx_removed"],
          "被丢掉的那项要**报出来** (前端要显示)", res.get("dropped_tests"))
    run = await Database.get_run(parent)
    check(run["status"] == "passed", "该过的还是过了", run["status"])


async def case11():
    section("【11】没有失败项的记录不该能重测 (前端按钮的判据)")
    parent = await make_parent({"fx_a": "passed"})
    r = await Database.recompute_run(parent)
    check(r["status"] == "passed", "全过了", r["status"])
    check(r["summary"]["failed"] == 0 and r["summary"]["error"] == 0,
          "没有可重测的项 (failed/error 都是 0)", r["summary"])


async def case12():
    section("【12】报告纸上要认得出「这是合并后的结果」和「第 N 次才过」")
    # 报告是**纸面归档**用的东西。合并完重新出的那份, 得说清两件事:
    #   ① 这是重测合并后的最终结果(不然读者会以为结果凭空变了)
    #   ② 每一项是第几次才过的(返修过的项一眼看得出来)
    parent = await make_parent({"fx_a": "passed", "fx_c": "failed"})
    STATUS["fx_c"] = "passed"
    await run_mini(parent, ["fx_c"])

    run = await Database.get_run(parent)
    results = run.get("results") or []
    run.pop("results", None)
    from server import report as report_mod
    html = report_mod.render_html(run, results, "t.pdf")

    check("重测" in html,
          "报告里写了「重测」两个字(读者知道这是合并后的结果)")
    check("attempt-tag" in html and "第 2 次" in html,
          "返修过的那项印出「第 2 次测试通过」(首测=1, 重测一次=2)")
    check("本报告含" in html,
          "报告头写明这一份含几轮重测 (retest_count=1)")
    check(run.get("retest_count") == 1, "run 里带着 retest_count=1",
          run.get("retest_count"))


async def case13():
    section("【13】多项一起测、其中一项失败 → 重测的日志**单独一节**印在日志附录后面")
    # 工人的原问题: 「同时测了多项, 有一项失败, 重测之后日志能否更新到报告里」。
    # 追加确认: 「可以加到日志附录后面单独说明这一块, **不要动前面的**」。
    #
    # 所以口径是:
    #   · 父轮那一行的 log = **首测的原始日志**, 一个字都不许动
    #     (那是返修时最要看的证据: 「当初为什么失败」)
    #   · 重测那一轮的日志留在**重测轮自己的行**里
    #   · 报告里「日志附录」原样不动, **后面**另起一节「重测日志附录」
    #
    # 为什么不混在一起: 事后申诉时, 读者要分得清哪段是当初的、哪段是后来
    # 补的 —— 混在一段里这个分界就没了, 而这正是归档报告最要紧的事。
    parent = await make_parent({"fx_a": "passed", "fx_b": "passed",
                                "fx_c": "failed"})
    # 重测这一轮吐两行日志
    LOGS["fx_c"] = ["重测第1行: 这次通了", "重测第2行: 收尾"]
    await run_mini(parent, ["fx_c"])

    run = await Database.get_run(parent)
    logs = {r["test_name"]: (r.get("log") or "") for r in run["results"]}
    c_log = logs.get("fx_c", "")
    check(c_log == "日志-fx_c",
          "父轮那一项的 log **原封不动**(既没被冲掉, 也没被追加)", c_log)
    check(logs.get("fx_a") == "日志-fx_a",
          "没重测的那几项日志**原样不动**", logs.get("fx_a"))
    check(logs.get("fx_b") == "日志-fx_b",
          "另一项没重测的也不动", logs.get("fx_b"))

    # 重测轮的日志住在**重测轮自己**的行里
    rrs = await Database.get_retest_runs(parent)
    check(len(rrs) == 1, "查到 1 轮重测", len(rrs))
    retest_log = ""
    if rrs:
        check(rrs[0]["results"], "重测轮里有逐项结果", rrs[0])
        retest_log = rrs[0]["results"][0].get("log") or ""
        check("重测第1行: 这次通了" in retest_log and "重测第2行: 收尾" in retest_log,
              "重测那一轮的日志在重测轮自己的行里", retest_log)
        check(rrs[0]["results"][0]["test_name"] == "fx_c", "就是被重测的那一项",
              rrs[0]["results"][0])
        check(rrs[0]["results"][0]["attempt"] == 2,
              "重测那一项的 attempt = 2 (首测=1, 重测一次=2)",
              rrs[0]["results"][0])

    # 导 PDF 时确实把重测轮交上去了 (父轮那一份要靠它才有那一节)
    parent_call = [c for c in FakeReport.CALLS if c["run_id"] == parent]
    check(len(parent_call) == 1, "父轮的 PDF 重导了一次", len(parent_call))
    if parent_call:
        passed_rr = parent_call[0].get("retest_runs") or []
        check(len(passed_rr) == 1, "重导时把重测轮一起交上去了", len(passed_rr))
        if passed_rr:
            check("重测第1行: 这次通了" in (passed_rr[0]["results"][0].get("log") or ""),
                  "交上去的正是重测那一轮的日志")

    # 报告里: 日志附录**原样**, 后面**另起一节**印重测日志
    from server import report as report_mod
    run2 = await Database.get_run(parent)
    results2 = run2.get("results") or []
    run2.pop("results", None)
    html = report_mod.render_html(run2, results2, "t.pdf",
                                  retest_runs=await Database.get_retest_runs(parent))

    i_log = html.find('<div class="log-appendix">')
    i_ret = html.find('<div class="retest-appendix">')
    check(i_log >= 0, "报告里有「日志附录」")
    check(i_ret >= 0, "报告里有「重测日志附录」")
    check(i_log < i_ret, "「重测日志附录」排在「日志附录」**后面**", (i_log, i_ret))

    # 「日志附录」那一节里**只有**首测的日志 —— 前面的一个字都没被动
    log_section = html[i_log:i_ret] if i_log >= 0 and i_ret > i_log else ""
    check("日志-fx_c" in log_section, "日志附录里还看得到第一次的原始日志",
          log_section[:200])
    check("重测第1行: 这次通了" not in log_section,
          "日志附录里**没有**混进重测的日志", log_section[-300:])
    check("日志-fx_a" in log_section and "日志-fx_b" in log_section,
          "其它项的日志也在(没被重测挤掉)")

    # 新的那一节里才是重测的日志
    ret_section = html[i_ret:] if i_ret >= 0 else ""
    check("重测第1行: 这次通了" in ret_section and "重测第2行: 收尾" in ret_section,
          "重测日志附录里是重测那轮的日志", ret_section[:300])

    # 逐项段要给指路: 被重测过的那一项要说明「另有一轮的日志在后面」
    check("那轮的日志见其后的「重测日志附录」" in html,
          "逐项段给被重测过的那一项指了路(读者不会以为只有首测那份日志)")

    # 没重测过的那一轮, 报告里**不该**冒出这一节
    html_no = report_mod.render_html(run2, results2, "t.pdf")
    check("<h2>重测日志附录</h2>" not in html_no,
          "不传重测轮时, 报告里没有「重测日志附录」这一节")
    check('<div class="retest-appendix">' not in html_no,
          "不传重测轮时, 报告里没有那个重测附录的 div")

    # ── §7.8.6 报告头那三行: 原值**保留**, 重测的那三项**接在后面** ──
    #
    # 这一段是**端到端**的: 库里的时间 → get_retest_runs → 渲染 → 纸面。
    # 光在 report.py 里单测渲染不算数 —— 真正要保证的是「重测轮那一行在
    # 库里记着起点和终点」, 而那正是 get_retest_runs 读出来的两个字段。
    rr_list = await Database.get_retest_runs(parent)
    check(rr_list and rr_list[0].get("started_at") is not None
          and rr_list[0].get("finished_at") is not None,
          "重测轮在库里记着起点和终点", rr_list)
    if rr_list:
        r_started = report_mod._fmt_time(rr_list[0]["started_at"])
        r_finished = report_mod._fmt_time(rr_list[0]["finished_at"])
        r_dur = report_mod._fmt_dur(
            rr_list[0]["finished_at"] - rr_list[0]["started_at"])
        for label, want in (("开始时间", r_started),
                            ("结束时间", r_finished),
                            ("总耗时", r_dur)):
            m_cell = re.search(
                r"<tr>\s*<td>" + label + r"</td>\s*<td>(.*?)</td>", html, re.S)
            cell = re.sub(r"<[^>]+>", " ", m_cell.group(1)) if m_cell else ""
            check("重测第 1 轮: " + want in cell,
                  f"报告头的「{label}」里接着印了「重测第 1 轮: {want}」",
                  cell[:200])
        # 原值**不许**被换掉 —— 首测那一次是什么时候跑的, 那是事实。
        p_dur = report_mod._fmt_dur(run2["finished_at"] - run2["started_at"])
        m_cell = re.search(r"<tr>\s*<td>总耗时</td>\s*<td>(.*?)</td>", html, re.S)
        cell = re.sub(r"<[^>]+>", " ", m_cell.group(1)) if m_cell else ""
        check(p_dur in cell, f"总耗时那一格里**原值** {p_dur} 还在", cell[:200])


async def case14():
    section("【14】历史列表: **一行 = 一条归档记录**(重测轮不单独占一行)")
    # 工人的原话: 「我只重测了两次的记录, 有四个记录都显示有重测」。
    #
    # 根因: 重测轮(mini-run)自己也是 runs 表里的一行, 而历史列表把**每一行**
    # 都列了出来 —— 重测两次 = 2 条原记录 + 2 条重测轮 = **四条**带「重测」
    # 字样的行, 看着像"四条记录都重测过"。实际只重测过两条。
    #
    # 改法: 列表只列归档记录(重测轮过滤掉), 重测轮的信息挂到它那一轮记录的
    # **详情**里(列表一行 = 一条记录, 看着不乱; 证据也不丢)。
    parent = await make_parent({"fx_a": "passed", "fx_c": "failed"})
    STATUS["fx_c"] = "passed"
    LOGS["fx_c"] = ["重测这一轮的行"]
    rn, res, events = await run_mini(parent, ["fx_c"])

    lst = await Database.list_runs(100)
    ids = [r["id"] for r in lst]
    check(parent in ids, "父轮在历史列表里", ids)
    mini_ids = [r["id"] for r in lst if r.get("mini_of")]
    check(not mini_ids,
          "列表里**没有**重测轮的行(一行 = 一条归档记录)", mini_ids)
    # 排障用的开关: 想看原始行时还能看得到
    lst_all = await Database.list_runs(100, include_mini=True)
    n_mini = sum(1 for r in lst_all if r.get("mini_of"))
    check(n_mini >= 1, "原始行里确实有重测轮", n_mini)
    check(any(r.get("mini_of") for r in lst_all),
          "include_mini=True 时原始行还在(排障用)")
    check(len(lst) == len(lst_all) - n_mini,
          "列表条数 = 全部行数 - 重测轮数", (len(lst), len(lst_all), n_mini))

    # 父轮那一行的角标还在 —— 过滤的是**重测轮**, 不是「重测信息」
    row = [r for r in lst if r["id"] == parent]
    check(row and row[0].get("retest_count") == 1,
          "父轮那行仍带着 retest_count=1 (角标照旧显示)", row)

    # 被合并过的那条要排在**最上面**(工人刚合完, 不该找不到它)
    check(ids and ids[0] == parent,
          "刚合并过的那条排在列表最上面", ids)

    # 详情里带着重测轮 —— 信息没丢, 只是不在列表里占行了
    detail = await Database.get_run(parent)
    rrs = detail.get("retest_runs") or []
    check(len(rrs) == 1, "详情里带着那一轮重测", len(rrs))
    if rrs:
        check(rrs[0]["results"][0]["test_name"] == "fx_c",
              "详情里认得出重测的是哪一项", rrs[0]["results"][0])
        check("重测这一轮的行" in (rrs[0]["results"][0].get("log") or ""),
              "详情里能翻到那一轮的日志", rrs[0]["results"][0])


async def case15():
    section("【15】没跑到(stopped)的项**也能重测** —— 重测通过后并回父轮")
    # 事故链条(2026-10-28): TF 卡挂死 → 工人按了暂停 → 重测轮开跑时把父轮
    # 那一项先写成 `running`(让界面别停在旧状态上) → 服务在事件循环里冻死
    # → 收尾逻辑把那一项记成 `stopped`。
    #
    # `stopped` 跟 `skipped` 一样是「**没测成**」—— 补上料、把板子复位之后
    # 就该能再测一次。前端的重测集合里必须有它(见 test_frontend_retest_history
    # 【14】), **后端这一半**在这儿钉住: 后端收的是一份**显式的项名清单**,
    # 不该因为这一项上一轮是 stopped 就拒绝。
    parent = await make_parent({"fx_a": "passed", "fx_b": "stopped"},
                               status="stopped")
    STATUS["fx_b"] = "passed"
    rn, res, events = await run_mini(parent, ["fx_b"])
    check(res.get("ok"), "后端**照收**这一项重测(不看它上一轮什么状态)", res)

    run = await Database.get_run(parent)
    row = [r for r in run["results"] if r["test_name"] == "fx_b"][0]
    check(row["status"] == "passed",
          "重测通过了 → 父轮那一项从 stopped 变成 passed", row["status"])
    check(run["status"] == "passed",
          "整轮也判通过了(这一轮就差这一项)", run["status"])
    check(run["summary"]["passed"] == 2 and run["summary"]["total"] == 2,
          "计数跟着重算(通过 2 项)", run["summary"])
    check(run["summary"].get("not_run", 0) == 0,
          "没有「没跑到」的项了", run["summary"].get("not_run"))
    check(run.get("retest_count") == 1, "记了一次重测", run.get("retest_count"))
    merged = [c for c in FakeReport.CALLS if c["run_id"] == parent]
    check(len(merged) == 1, "父轮的报告照样重导了(界面和报告不能打架)", len(merged))


async def case16():
    section("【16】还有别的项没跑到 → 重测修好了这一项, 整轮**仍然**是 stopped")
    # §7.7.2 / §9.3: 没测过的项合不合格谁也不知道 —— 不能因为重测了一个项
    # 就宣布「整体通过」。这一条跟【15】是一对: 全都测过了才能判通过。
    config = {"tests": {"fx_a": {"enabled": True},
                        "fx_b": {"enabled": True},
                        "fx_c": {"enabled": True}}}
    parent = await Database.create_run(
        config, {"sn": "TESTSN0001", "operator": "产线甲", "batch": "B1"})
    await Database.upsert_result(parent, "fx_a", status="passed",
                                 duration_sec=1.0, metrics={}, log="日志-a",
                                 error=None)
    await Database.upsert_result(parent, "fx_b", status="stopped",
                                 duration_sec=0.0, metrics={}, log="",
                                 error="服务重启, 本项没跑完")
    # fx_c 勾了但**一行都没有** —— 中途夭折, 它压根没轮到。
    await Database.finish_run(parent, "stopped", {
        "total": 1, "passed": 1, "failed": 0, "error": 0,
        "skipped": 0, "not_run": 2, "planned": 3,
    })

    STATUS["fx_b"] = "passed"
    rn, res, events = await run_mini(parent, ["fx_b"])
    check(res.get("ok"), "重测跑起来了", res)

    run = await Database.get_run(parent)
    check(run["status"] == "stopped",
          "整轮**仍然是 stopped** —— fx_c 压根没测过, 不能判通过", run["status"])
    check(run["summary"].get("not_run", 0) == 1,
          "还剩一项没跑到", run["summary"].get("not_run"))
    check(run["summary"]["passed"] == 2,
          "已经测过的两项都算数", run["summary"])


async def case17():
    section("【17】重测那一轮中途被停(记 stopped)→ 父轮**不许**判成「通过」")
    # 这是一条**假通过**的路(假通过比假失败更坑: 坏板子被当合格品放行):
    #
    #   父轮: fx_a 通过, fx_b **失败**(卡挂死) ⇒ 轮级 failed
    #   工人点重测 → 重测轮跑 fx_b → 卡还是挂死 → 工人按「暂停」
    #   ⇒ 重测轮那一项记 **stopped**(没测成), 合并回父轮
    #
    # 父轮里 fx_b 的**首测结论(failed)原样保留** —— 那是一条**真证据**
    # (卡真的挂死过)。§7.7「结论只能被结论替换」: 重测那一轮**没测出结论**,
    # 就不许拿它把父轮原来的结论洗掉。
    # 父轮**绝不能**判 passed —— §9.3: 没测过的项合不合格谁也不知道。
    parent = await make_parent({"fx_a": "passed", "fx_b": "failed"},
                               status="failed")
    STATUS["fx_b"] = "stopped"          # 重测轮里 fx_b 记 stopped(工人按了暂停)
    rn, res, events = await run_mini(parent, ["fx_b"])
    check(res.get("ok"), "重测跑起来了", res)

    run = await Database.get_run(parent)
    row = [r for r in run["results"] if r["test_name"] == "fx_b"][0]
    check(row["status"] == "failed",
          "父轮里 fx_b 仍是 failed —— 首测那条结论不许被「没测成」洗掉",
          row["status"])
    check(row.get("error") == "失败原因-fx_b",
          "失败原因也原样留着(报告上要看得见到底为什么)", row.get("error"))
    check(run["status"] != "passed",
          "🚨 父轮**不能**判 passed —— 没测成的项不能被洗成通过", run["status"])
    check(run["status"] == "failed",
          "父轮判 failed(那张卡的首测结论是真的)", run["status"])
    check(run["summary"]["failed"] == 1,
          "失败项照样计进失败数", run["summary"])
    check(run["summary"]["passed"] == 1,
          "只有 fx_a 算数", run["summary"])
    # 「重测没测成」这件事本身也不许丢 —— 它记在**重测轮自己那一行**里。
    rrs = run.get("retest_runs") or []
    check(len(rrs) == 1 and rrs[0]["results"][0]["status"] == "stopped",
          "重测轮自己那一行记着 stopped(没测成这件事没丢)",
          [r.get("status") for rr in rrs for r in rr.get("results") or []])


async def case18():
    section("【18】重测时卡槽是空的(记 skipped)→ 父轮的失败结论**同样**不许被洗掉")
    # 跟【17】同一类假通过, 但走的是**更常走**的那条路:
    #
    #   工人点重测 → 卡槽里没卡 → 工具问操作员「插卡还是跳过」→
    #   操作员**超时**没接话(默认按「跳过」)→ 这一项记 **skipped**(没测成)
    #
    # skipped 跟 stopped 一样是「**没测成**」: 关于那张卡**没有任何**结论。
    # 它要是把父轮首测那条 failed 洗掉, 父轮就不再有失败项 ⇒ 整轮判 passed
    # ⇒ **假通过**: 一张挂死过的卡就这么放行了。
    parent = await make_parent({"fx_a": "passed", "fx_b": "failed"},
                               status="failed")
    STATUS["fx_b"] = "skipped"          # 重测轮里 fx_b 记 skipped(卡槽空的)
    rn, res, events = await run_mini(parent, ["fx_b"])
    check(res.get("ok"), "重测跑起来了", res)

    run = await Database.get_run(parent)
    row = [r for r in run["results"] if r["test_name"] == "fx_b"][0]
    check(row["status"] == "failed",
          "父轮里 fx_b 仍是 failed —— 「没测成」不是结论, 换不掉结论",
          row["status"])
    check(run["status"] != "passed",
          "🚨 父轮**不能**判 passed —— 失败项被洗掉就是假通过", run["status"])
    check(run["status"] == "failed",
          "父轮判 failed", run["status"])
    check(run["summary"]["failed"] == 1,
          "失败项照样计进失败数", run["summary"])


async def case19():
    section("【19】父轮那一项本来就**没结论** → 重测也没测成 → 整轮判 stopped")
    # 【17】【18】是「父轮有结论 ⇒ 结论保住了」。这一条是另一半:
    # 父轮那一项**本来就没有结论**(上一轮中途夭折, 压根没轮到它),
    # 那就没有东西要保 —— 重测也没测成(stopped)就记 stopped,
    # 整轮**照样不许判 passed**(§7.7.2: 直到每一项都测过为止)。
    parent = await make_parent({"fx_a": "passed", "fx_b": "stopped"},
                               status="stopped")
    STATUS["fx_b"] = "stopped"          # 重测那一轮也没测成
    rn, res, events = await run_mini(parent, ["fx_b"])
    check(res.get("ok"), "重测跑起来了", res)

    run = await Database.get_run(parent)
    row = [r for r in run["results"] if r["test_name"] == "fx_b"][0]
    check(row["status"] == "stopped",
          "父轮里 fx_b = stopped(这一轮压根没测成)", row["status"])
    check(run["status"] != "passed",
          "🚨 整轮**不能**判 passed —— 还有一项没跑到", run["status"])
    check(run["status"] == "stopped",
          "整轮判 stopped", run["status"])
    check(run["summary"].get("not_run", 0) == 1,
          "计数里认得出有一项没跑到", run["summary"])


async def case20():
    section("【20】重测**跑到一半**时, 父轮那一项的结论不许被抹成 running")
    # 「结论只能被结论替换」不只是合并那一刻的事 —— 重测**开跑**的时候,
    # 父轮那一行也不许被写成 running:
    #   父轮那一项判过 failed(卡真的挂死过) → 写成 running = 首测的结论
    #   当场蒸发; 万一这一轮中途服务夭折, 收尾逻辑会把那一行记成 stopped ⇒
    #   首测的失败结论就这么没了(整轮也不再是 failed)。
    parent = await make_parent({"fx_a": "passed", "fx_b": "failed"},
                               status="failed")
    STATUS["fx_b"] = "passed"
    gate = asyncio.Event()
    BLOCK["fx_b"] = gate
    BLOCKED["fx_b"] = asyncio.Event()
    rn = R.TestRunner()
    res = await rn.start_mini(parent, ["fx_b"])
    check(res.get("ok"), "重测跑起来了", res)
    await asyncio.wait_for(BLOCKED["fx_b"].wait(), timeout=10)

    row = [r for r in (await Database.get_run(parent))["results"]
           if r["test_name"] == "fx_b"][0]
    check(row["status"] == "failed",
          "重测跑到一半, 父轮那一项**仍是 failed**(没被抹成 running)",
          row["status"])
    gate.set()
    await rn._task

    run = await Database.get_run(parent)
    row = [r for r in run["results"] if r["test_name"] == "fx_b"][0]
    check(row["status"] == "passed",
          "重测真的测出结论了 → 父轮那一项才换成 passed", row["status"])
    check(run["status"] == "passed", "整轮判通过", run["status"])


async def case21():
    section("【21】合并完: 父轮那条过期「系统级错误」不许再跟结论打架")
    # 场景(2026-10-28 那条事故链的续集): 上一轮服务夭折 → 收尾逻辑给父轮
    # 写上「服务重启, 这一轮中途夭折(没跑完)」+ 轮级 stopped → 工人重测
    # 没测成的项, 这次全过 → 合并回父轮, 父轮判 passed。
    #
    # 那条 error 说的是「**收尾那一刻**这一轮没跑完」—— 合并之后结论已经
    # 重新判过了, 它**过期了**。留着它, 总结页就是「✅ 整体通过」和
    # 「⚠ 本轮异常终止」并排 —— 同一屏上自相矛盾, 工人不知道该信哪个。
    parent = await make_parent({"fx_a": "passed", "fx_b": "stopped"},
                               status="stopped")
    async with DB.aiosqlite.connect(DB.DB_PATH) as db:
        await db.execute(
            "UPDATE runs SET error=? WHERE id=?",
            ("服务重启, 这一轮中途夭折(没跑完) —— 不代表被测设备不合格",
             parent))
        await db.commit()
    STATUS["fx_b"] = "passed"
    rn, res, events = await run_mini(parent, ["fx_b"])
    check(res.get("ok"), "重测跑起来了", res)

    run = await Database.get_run(parent)
    check(run["status"] == "passed",
          "父轮合并后判 passed(没测成的项补测过了)", run["status"])
    check(rn.run_error is None,
          "runner 不再挂着那条过期的「系统级错误」(总结页不许跟结论打架)",
          rn.run_error)

    # 报告那一页也一样: 「✅ 全部通过」跟「系统级错误」不许并排出现。
    # (这里渲染真的 HTML —— 不导出 PDF、不碰 chromium。)
    html = report_mod.render_html(
        run, run.get("results") or [], "fake-merged-report.pdf")
    check("系统级错误" not in html,
          "报告里也不印那条过期的「系统级错误」(同一页纸上不许自相矛盾)",
          html[:400])
    check("本报告含 1 轮重测" in html,
          "但「本报告含 N 轮重测」还在(这一轮被重测过这件事没丢)", html[:400])


async def case22():
    section("【22】重测点名的项**一定得跑** —— 不许被父轮的配置悄悄滤掉")
    # 父轮的配置里没有这一项(老库 / 配置结构变过), 或者写着 enabled=false:
    # 照抄父轮那份 enabled 的话, _build_test_list 会把这一项**悄悄滤掉** ⇒
    # 重测一轮**什么都没跑** ⇒ 没有任何结果合并回父轮 ⇒ 界面永远停在
    # 「⏳ 等待」。**静默地什么都不做比报错糟糕得多** —— 工人以为在重测,
    # 机器上其实什么都没发生。
    for label, parent_cfg in (
        ("父轮配置里没有这一项", {"tests": {"fx_a": {"enabled": True}}}),
        ("父轮配置里写着 enabled=false",
         {"tests": {"fx_a": {"enabled": True},
                    "fx_b": {"enabled": False}}}),
    ):
        parent = await make_parent({"fx_a": "passed", "fx_b": "failed"},
                                   status="failed")
        async with DB.aiosqlite.connect(DB.DB_PATH) as db:
            await db.execute("UPDATE runs SET config_json=? WHERE id=?",
                             (json.dumps(parent_cfg), parent))
            await db.commit()
        STATUS["fx_b"] = "passed"
        rn, res, events = await run_mini(parent, ["fx_b"])
        check(res.get("ok"), f"重测起得来({label})", res)
        check("fx_b" in (res.get("tests") or []),
              f"点名的那一项在重测清单里({label})", res)

        mini_run = await Database.get_run(res["run_id"])
        rows = {r["test_name"]: r["status"] for r in mini_run["results"]}
        check(rows.get("fx_b") == "passed",
              f"重测轮**真的跑了**这一项({label})", rows)

        run = await Database.get_run(parent)
        check(run["status"] == "passed",
              f"结果合并回父轮了({label})", run["status"])


async def case23():
    section("【23】测出失败之后工人按了「暂停」→ 整轮判 **failed**, 不是 stopped")
    # 「工人按了暂停」**不许**把已经测出来的失败抹掉(§9.3: 静默的「通过」
    # 比明确的「失败」有害得多 —— 抹掉 = 坏板子被当成「没失败」放行)。
    #
    # 以前 runner 的判定把 `cancelled` 排在最前面: 这一轮于是判 stopped ——
    # 板子真不合格的结论被工具的死盖住了, 而重测合并时 recompute 算出来
    # 却是 failed ⇒ 同一个轮次在两个地方两个说法, 界面和报告打架。
    # 现在两处用**同一个函数**(db.decide_run_status) ✓。
    parent = await make_parent({"fx_a": "passed", "fx_b": "failed"},
                               status="failed")
    STATUS["fx_b"] = "failed"          # 重测这一项还是失败(卡还是挂死)
    STOP_AFTER["fx_b"] = True          # 跑完就按「暂停」
    rn, res, events = await run_mini(parent, ["fx_b"])
    check(res.get("ok"), "重测跑起来了", res)

    mini_run = await Database.get_run(res["run_id"])
    check(mini_run["status"] == "failed",
          "重测轮判 failed —— 测出来的失败不许被「按了暂停」盖掉",
          mini_run["status"])

    run = await Database.get_run(parent)
    check(run["status"] == "failed",
          "父轮也判 failed(合并后重算, 口径一致)", run["status"])
    check(run["summary"]["failed"] == 1,
          "失败项照样计进失败数", run["summary"])
    STOP_AFTER.clear()


async def main():
    # 真库 + 假 report: 临时目录, 绝不碰 reports/history.db 与 reports/pdf/
    tmp = mkdtemp(prefix="aging_merge_")
    DB.DB_PATH = __import__("pathlib").Path(tmp) / "history.db"
    await Database.init()
    R.report = FakeReport
    R.INTERVAL_BETWEEN_TESTS_SEC = 0

    # 每个用例之间清干净假测试项的状态/日志 —— 否则上一个用例留下的
    # STATUS["fx_c"]="passed" 会**悄悄**替下一个用例决定结果(踩过:
    # 之前 run_mini 里清 STATUS, 反而让「重测仍失败」这种场景根本测不出来)。
    cases = [case1, case1b, case2, case3, case4, case5, case6, case7,
             case8, case9, case10, case11, case12, case13, case14,
             case15, case16, case17, case18, case19, case20,
             case21, case22, case23]
    for c in cases:
        STATUS.clear()
        LOGS.clear()
        BLOCK.clear()
        BLOCKED.clear()
        STOP_AFTER.clear()
        await c()

    print("\n" + ("全部通过" if not FAILS else f"有失败项 ({len(FAILS)})"))
    for f in FAILS:
        print("  ✗ " + f)
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
