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
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import db as DB
from server import runner as R
from server.db import Database
from server.tests.base import TestCase, TestResult

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


def make_test(name):
    class T(TestCase):
        def __init__(self):
            self.name = name
            self.display_name = "假测试项-" + name

        async def run(self, ctx):
            for line in LOGS.get(name, []):
                await ctx.on_log(line)
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
    section("【8】父轮里残留的 running 行 → stopped, 计数不缺项")
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
    check(r["status"] == "passed",
          "两项都过了 → passed (没跑到的那项单列, 不拖累结论)", r["status"])


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


async def main():
    # 真库 + 假 report: 临时目录, 绝不碰 reports/history.db 与 reports/pdf/
    tmp = tempfile.mkdtemp(prefix="aging_merge_")
    DB.DB_PATH = __import__("pathlib").Path(tmp) / "history.db"
    await Database.init()
    R.report = FakeReport
    R.INTERVAL_BETWEEN_TESTS_SEC = 0

    # 每个用例之间清干净假测试项的状态/日志 —— 否则上一个用例留下的
    # STATUS["fx_c"]="passed" 会**悄悄**替下一个用例决定结果(踩过:
    # 之前 run_mini 里清 STATUS, 反而让「重测仍失败」这种场景根本测不出来)。
    cases = [case1, case1b, case2, case3, case4, case5, case6, case7,
             case8, case9, case10, case11, case12, case13, case14]
    for c in cases:
        STATUS.clear()
        LOGS.clear()
        await c()

    print("\n" + ("全部通过" if not FAILS else f"有失败项 ({len(FAILS)})"))
    for f in FAILS:
        print("  ✗ " + f)
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
