"""回归测试: 「停止」和「下一项」两个操作键 (2026-10-29)。

产线的原话: 「应该要有两个操作键, 一个停止一个下一项的按钮, 停止是停止当前和
所有的测试项然后直接跳到结果界面, 当前项和后续项都标记的是跳过; 下一项就是
停止当前的测试项进入到下一项测试, 当前项标记跳过。」

这个测试盯八件事:

    ① 「下一项」→ 当前项记 **skipped**, 后面各项**照常跑**, 整轮正常收尾;
    ② 「下一项」的标志**不许漏到下一项**(新的一项刚开跑就被判跳过 = 灾难);
    ③ 「停止」→ 当前项 + 后面所有项**都**记 skipped, 整轮判 **stopped**
       —— 不是 passed(§9.3: 假通过比假失败更坑: 一台只测了一项的机器拿着
       「整体通过」出货, 连申诉的由头都没有);
    ④ 已经测出来的 failed **不许**被「停止」抹掉(真证据不许被按钮盖掉);
    ⑤ 「停止」按在两项之间的静默期里 → 一样是 stopped, 不是 passed;
    ⑥ `db.decide_run_status`: aborted > 0 → stopped, aborted = 0 → passed;
    ⑦ `Database.recompute_run` 也数 aborted_by_user 那几行 —— 父轮被中止过、
       重测只补了一部分时, 整轮**不能**被重算成 passed;
    ⑧ 两个 API 端点分别打到 `runner.skip_current()` / `runner.stop()`。

为什么「整轮判 stopped」这条最要紧: 「停止」按下去之后, 当前项和后面所有项
都记 **skipped**(用户主动让它别测)。而 skipped 按 §9.2 是**不进 total** 的 ——
于是「3 项测过都过了 + 工人按了停止剩下 7 项」会落进「没有失败项」那一档,
判成 **passed**。那是**假通过**: 那 7 项根本没测, 合不合格谁也不知道。
所以整轮判定必须额外看一个「工人中止过」的标记(§9.2 的 aborted 口径)。

用**真库**(临时 SQLite 文件, 建在系统临时目录) + **假 report**:
逐项结果行是真的写进库里的(报告和历史页都是从库里读逐项结果的, 不落库等于
没测); 报告导出在这里挡掉, 不往生产的 reports/pdf/ 里丢假报告
(见 test_interrupted_run_reconcile.py 里同一套做法)。

跑法: .venv/bin/python -m tests.test_skip_next
"""
import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import db as DB
from server import runner as R
from server.db import Database, decide_run_status
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


class FakeReport:
    """§8.2 报告导出必须在这里挡掉 —— 真跑会往生产的 reports/pdf/ 丢假报告。"""

    @classmethod
    async def export_pdf(cls, run, results):
        return "/fake/report.pdf"

    @staticmethod
    def find_chromium():
        return "/fake/chromium"

    @staticmethod
    def output_dir():
        return None


R.report = FakeReport
# 静默期默认 0 秒(测试要快); 需要它的那个用例自己临时改成 1。
R.INTERVAL_BETWEEN_TESTS_SEC = 0


# ────────────────────────────── 假测试项 ──────────────────────────────

def mk(name, status="passed", error=None, action=None, probe=None,
       cancel_aware=True):
    """造一个假测试项。

    action: 这一项**跑着的时候**干的事(模拟工人按按钮) —— 收到 ctx。
    probe:  这一项**刚开跑**时看一眼 runner 的状态(查标志有没有漏过来)。
    cancel_aware: 真测试项都会看 `ctx.is_cancelled()`, 工人一按就自己停下来
                  —— 这里照做, 不然测的就不是「工人按了会怎样」而是
                  「runner 事后怎么改标签」了。
    """
    class T(TestCase):
        def __init__(self):
            self.display_name = f"假测试项-{name}"

        async def run(self, ctx):
            if probe:
                probe()
            if action:
                await action(ctx)
            if cancel_aware and await ctx.is_cancelled():
                return TestResult(status="stopped", error="用户中途停止")
            return TestResult(status=status, error=error)

    T.name = name
    T.display_name = f"假测试项-{name}"
    return T


async def drive(rn, specs, interval=0):
    """跑一轮, 返回 (summary, 轮级状态, 逐项行列表)。

    逐项行是从**库里**读回来的 —— 报告和历史页都是这么读的, 只看内存里的
    results 等于没验「落库」这一半。
    """
    R.INTERVAL_BETWEEN_TESTS_SEC = interval
    enabled = [(sp["name"], mk(sp["name"], sp.get("status", "passed"),
                                sp.get("error"), sp.get("action"),
                                sp.get("probe"), sp.get("cancel_aware", True)))
               for sp in specs]
    cfg = {"tests": {n: {"enabled": True} for n, _ in enabled},
           "project_root": ""}
    rn.run_id = await Database.create_run(cfg, {"sn": "TESTSN0001"})
    rn.status = "running"
    await rn._run_tests_body(enabled, cfg, write_to_parent=False)
    st, summary_json = await fetch_run(rn.run_id)
    return json.loads(summary_json or "{}"), st, await fetch_items(rn.run_id)


async def fetch_run(rid):
    async with DB.aiosqlite.connect(DB.DB_PATH) as db:
        cur = await db.execute(
            "SELECT status, summary_json FROM runs WHERE id=?", (rid,))
        return await cur.fetchone()


async def fetch_items(rid):
    async with DB.aiosqlite.connect(DB.DB_PATH) as db:
        cur = await db.execute(
            "SELECT test_name, status, metrics_json, error FROM results "
            "WHERE run_id=? ORDER BY id", (rid,))
        return await cur.fetchall()


def item(items, name):
    """从逐项行里挑出某一项, 归一成 {status, reason, error}。"""
    for tn, st, metrics_json, err in items:
        if tn == name:
            try:
                m = json.loads(metrics_json) if metrics_json else {}
            except (TypeError, ValueError):
                m = {}
            return {"status": st, "reason": (m or {}).get("skipped_reason"),
                    "error": err}
    return None


def acct_ok(summary):
    """各状态计数加起来 == 勾选数(数字对得上账)。"""
    acct = (summary.get("passed", 0) + summary.get("failed", 0)
            + summary.get("error", 0) + summary.get("skipped", 0)
            + summary.get("not_run", 0))
    return acct == summary.get("planned"), f"{acct} vs {summary.get('planned')}"


# ══════════════════════════════════════════════════════════════════════
async def case1():
    print("【1】「下一项」→ 当前项记 skipped, 后面各项**照常跑**")
    rn = R.TestRunner()

    async def _press_next(ctx):
        await rn.skip_current()

    summary, status, items = await drive(rn, [
        {"name": "a", "action": _press_next},
        {"name": "b"},
        {"name": "c"},
    ])

    a = item(items, "a")
    check(a and a["status"] == "skipped",
          "a 记 skipped (这一项没测成)", a)
    check(a and a["reason"] == "skipped_by_user",
          "a 带的是「用户跳过此项」的标记(不是「中止整轮」)", a)
    check(item(items, "b")["status"] == "passed",
          "b 照常跑完 → passed", item(items, "b"))
    check(item(items, "c")["status"] == "passed",
          "c 照常跑完 → passed", item(items, "c"))
    check(summary.get("passed") == 2, "passed = 2", summary.get("passed"))
    check(summary.get("skipped") == 1, "skipped = 1", summary.get("skipped"))
    check(summary.get("aborted") == 0,
          "aborted = 0 (跳一项**不算**中止整轮)", summary.get("aborted"))
    check(summary.get("total") == 2,
          "total = 2 (跳掉的那项不算执行过)", summary.get("total"))
    check(status == "passed",
          "整轮判 passed —— 「下一项」不是中止, 整轮照常收尾", status)
    ok, extra = acct_ok(summary)
    check(ok, "各状态计数加起来 == 勾选数", extra)


async def case2():
    print("\n【2】「下一项」的标志**不许漏到下一项**")
    rn = R.TestRunner()
    seen = []

    async def _press_next(ctx):
        await rn.skip_current()

    summary, status, items = await drive(rn, [
        {"name": "a", "action": _press_next},
        # b 刚开跑时看一眼标志 —— 漏过来就是「新的一项刚开跑就被判跳过」
        {"name": "b", "probe": lambda: seen.append(rn._skip_current)},
        {"name": "c", "probe": lambda: seen.append(rn._skip_current)},
    ])

    check(seen == [False, False],
          "b / c 开跑时标志都是 False (没漏过来)", seen)
    check(item(items, "b")["status"] == "passed",
          "b 是 passed, 不是 skipped", item(items, "b"))
    check(item(items, "c")["status"] == "passed",
          "c 是 passed, 不是 skipped", item(items, "c"))
    check(summary.get("skipped") == 1,
          "整轮只跳了一项", summary.get("skipped"))


async def case3():
    print("\n【3】「停止」→ 当前项 + 后面所有项**都**记 skipped, 整轮 stopped")
    rn = R.TestRunner()

    async def _press_stop(ctx):
        await rn.stop()

    summary, status, items = await drive(rn, [
        {"name": "a", "action": _press_stop},
        {"name": "b"},
        {"name": "c"},
    ])

    for nm in ("a", "b", "c"):
        it = item(items, nm)
        check(it and it["status"] == "skipped",
              f"{nm} 记 skipped (用户主动让它别测)", it)
        check(it and it["reason"] == "aborted_by_user",
              f"{nm} 带的是「中止整轮」的标记", it)
    check(len(items) == 3,
          "三个项在库里都有行(没跑的那两个也补上了)", len(items))
    check(summary.get("skipped") == 3, "skipped = 3", summary.get("skipped"))
    check(summary.get("aborted") == 3,
          "aborted = 3 (整轮中止过)", summary.get("aborted"))
    check(summary.get("total") == 0,
          "total = 0 (没跑完的项不算执行过)", summary.get("total"))
    check(status == "stopped",
          "整轮判 **stopped** —— 不是 passed(假通过), 也不是 skipped", status)
    ok, extra = acct_ok(summary)
    check(ok, "各状态计数加起来 == 勾选数", extra)


async def case4():
    print("\n【4】已经测出来的 failed **不许**被「停止」抹掉")
    rn = R.TestRunner()

    async def _press_stop(ctx):
        await rn.stop()

    summary, status, items = await drive(rn, [
        # 这一项**测出了失败**, 工人刚好在这时候按了「停止」
        {"name": "a", "status": "failed", "error": "温度超了",
         "action": _press_stop, "cancel_aware": False},
        {"name": "b"},
        {"name": "c"},
    ])

    a = item(items, "a")
    check(a and a["status"] == "failed",
          "a 仍然是 failed (真证据不许被按钮盖掉)", a)
    check(a and a["error"] == "温度超了",
          "a 的失败原因原样留着", a)
    check(summary.get("failed") == 1, "failed = 1", summary.get("failed"))
    check(summary.get("aborted") == 2,
          "aborted = 2 (后面两项是被中止掉的)", summary.get("aborted"))
    check(status == "failed",
          "整轮判 failed —— 「停止」不许把板子真不合格的结论抹掉", status)


async def case5():
    print("\n【5】「停止」按在两项之间的**静默期**里 → 也是 stopped, 不是 passed")
    rn = R.TestRunner()
    # 静默期是个**时间窗**, 靠掐表按按钮是不可靠的 —— 这里把窗口**撑开**:
    # 进静默期的第一件事就是按「停止」, 然后走**真的** _inter_test_interval
    # (它自己那一句 `if self.cancelled: return False` 就是被验的东西)。
    orig = rn._inter_test_interval

    async def _patched(next_name, total_sec):
        await rn.stop()
        return await orig(next_name, total_sec)

    rn._inter_test_interval = _patched

    summary, status, items = await drive(rn, [
        {"name": "a"},
        {"name": "b"},
        {"name": "c"},
    ], interval=1)

    check(item(items, "a")["status"] == "passed",
          "a 的通过结论原样留着", item(items, "a"))
    check(item(items, "b")["status"] == "skipped",
          "b 记 skipped", item(items, "b"))
    check(item(items, "c")["status"] == "skipped",
          "c 记 skipped", item(items, "c"))
    check(summary.get("aborted") == 2,
          "aborted = 2", summary.get("aborted"))
    check(status == "stopped",
          "整轮判 **stopped** —— 这一轮只测了一项, 不能宣布整体通过", status)


async def case6():
    print("\n【6】db.decide_run_status 的口径")
    check(decide_run_status({"total": 3, "passed": 3, "failed": 0, "error": 0,
                              "skipped": 0, "not_run": 0, "aborted": 0})
          == "passed", "全过且没人中止 → passed", None)
    check(decide_run_status({"total": 2, "passed": 2, "failed": 0, "error": 0,
                              "skipped": 1, "not_run": 0, "aborted": 1})
          == "stopped",
          "工人中止过 → stopped(不是 passed —— 假通过最有害)", None)
    check(decide_run_status({"total": 0, "passed": 0, "failed": 0, "error": 0,
                              "skipped": 3, "not_run": 0, "aborted": 0})
          == "skipped",
          "环境原因跳过(没有 aborted 标记)→ skipped, 不牵连整轮", None)
    check(decide_run_status({"total": 0, "passed": 0, "failed": 0, "error": 0,
                              "skipped": 3, "not_run": 0, "aborted": 3})
          == "stopped", "中止掉的项 → stopped", None)
    check(decide_run_status({"total": 1, "passed": 0, "failed": 1, "error": 0,
                              "skipped": 2, "not_run": 0, "aborted": 2})
          == "failed",
          "已经出结论的 failed 排在最前 —— 不被「中止」盖掉", None)
    check(decide_run_status({"total": 1, "passed": 1, "failed": 0, "error": 0,
                              "skipped": 0, "not_run": 2, "aborted": 0})
          == "stopped", "有没跑到的项 → stopped", None)


async def case7():
    print("\n【7】recompute_run 也要数 aborted_by_user 那几行")
    cfg = {"tests": {"p_a": {"enabled": True}, "p_b": {"enabled": True}}}

    rid = await Database.create_run(cfg, {"sn": "TESTSN0001"})
    await Database.upsert_result(rid, "p_a", status="passed")
    await Database.upsert_result(rid, "p_b", status="skipped",
                                 metrics={"skipped_reason": "aborted_by_user"})
    merged = await Database.recompute_run(rid)
    check(merged["summary"].get("aborted") == 1,
          "aborted = 1 (重算时也数出来了)", merged["summary"].get("aborted"))
    check(merged["status"] == "stopped",
          "父轮被中止过、重测只补了一部分 → 整轮仍判 stopped", merged["status"])

    # 对照: **环境原因**的跳过(没插卡)不该把整轮判成 stopped
    rid2 = await Database.create_run(cfg, {"sn": "TESTSN0001"})
    await Database.upsert_result(rid2, "p_a", status="passed")
    await Database.upsert_result(rid2, "p_b", status="skipped",
                                 metrics={"skipped_reason": "no_card"})
    merged2 = await Database.recompute_run(rid2)
    check(merged2["summary"].get("aborted") == 0,
          "对照: aborted = 0", merged2["summary"].get("aborted"))
    check(merged2["status"] == "passed",
          "对照: 环境原因的跳过**不**把整轮判成 stopped", merged2["status"])


async def case8():
    print("\n【8】两个 API 端点各自打到对应的 runner 方法")
    from server import main as M

    calls = []

    class FakeRunner:
        status = "running"

        async def stop(self):
            calls.append("stop")
            return {"ok": True}

        async def skip_current(self):
            calls.append("skip_current")
            return {"ok": True}

    def find_endpoint(app, path):
        for r in app.routes:
            if getattr(r, "path", None) == path:
                return r.endpoint
        return None

    ep_stop = find_endpoint(M.app, "/api/test/stop")
    ep_next = find_endpoint(M.app, "/api/test/next")
    check(ep_stop is not None, "有 /api/test/stop 这个端点", None)
    check(ep_next is not None, "有 /api/test/next 这个端点", None)
    if not (ep_stop and ep_next):
        return

    old = M.runner
    M.runner = FakeRunner()
    try:
        r_stop = await ep_stop()
        r_next = await ep_next()
    finally:
        M.runner = old

    check(calls == ["stop", "skip_current"],
          "「停止」→ runner.stop(), 「下一项」→ runner.skip_current()", calls)
    check(r_stop == {"ok": True} and r_next == {"ok": True},
          "端点把 runner 的答复原样返回(前端靠 data.ok 判成败)",
          (r_stop, r_next))


async def case9():
    print("\n【9】「下一项」按在两项之间的**静默期**里 → 静默期立刻结束")
    rn = R.TestRunner()
    orig = rn._inter_test_interval

    async def _patched(next_name, total_sec):
        await rn.skip_current()      # 工人卡在静默期里按了「下一项」
        return await orig(next_name, total_sec)

    rn._inter_test_interval = _patched

    t0 = time.monotonic()
    summary, status, items = await drive(rn, [
        {"name": "a"},
        {"name": "b"},
    ], interval=3)
    elapsed = time.monotonic() - t0

    # 「下一项」的意思是「别等了, 直接进下一项」—— 静默期还照等 3 秒的话,
    # 工人按了键看着像没反应(这正是这个键最容易被骂的地方)。
    check(elapsed < 1.0,
          "静默期没白等 3 秒 (按了「下一项」还干等 = 「点了像没反应」)",
          "%.2fs" % elapsed)
    check(item(items, "b")["status"] == "passed",
          "b 照常跑完 (静默期里没有「当前项」, 没有东西可跳)",
          item(items, "b"))
    check(summary.get("skipped") == 0,
          "没有项被跳过 (上一项的结论不许被改)", summary.get("skipped"))


async def main():
    tmp = mkdtemp(prefix="aging_skipnext_")
    DB.DB_PATH = Path(tmp) / "t.db"
    await Database.init()

    for c in (case1, case2, case3, case4, case5, case6, case7, case8, case9):
        await c()

    print("\n" + ("全部通过" if not FAILS else f"有失败项 ({len(FAILS)})"))
    for f in FAILS:
        print("  ✗ " + f)
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
