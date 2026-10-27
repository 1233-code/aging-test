"""回归测试: 汇总口径 total = 实际执行的项数 (§9.2), 停掉的项算 stopped。

起因是 §9.2 行 1585-1595 那条硬规矩:

    total = passed + failed + error        ← 只算**实际跑过并出结论**的项
    skipped / stopped **不计入** total
    total == 0 → 整轮判 skipped, **不得**判 passed

而代码以前把 total 直接取成 `len(enabled_tests)`(勾选数), 带来两个后果:

    ① 一轮全跳过 → total=10 而各状态计数加起来=0, 报告上的数字**对不上账**
    ② 全跳过时 any_failed 是 False → 整轮判 **passed** ——
       一项都没测却给出「整体通过 10/10」的空报告。
       §9.3 把这种「静默的通过」列成最危险的失效模式: 明确的失败还能申诉,
       静默的通过连申诉的由头都没有(纸面上写着「通过」)。

这轮还顺手修掉了另一半: 用户中途停止时, 队列里**没跑**的项在 results 表里
**连行都没有** —— 报告逐项段里看不见它们, 界面上显示成「⏳ 等待」,
工人以为还在排队。现在给它们补 stopped 行。

这个测试验六件事:
    ① 一项都没实际执行 → 整轮判 **skipped**, 不是 passed
    ② total = passed + failed + error, skipped / stopped 不进 total
    ③ total 只数**实际执行**的项, 不是勾选数
    ④ 中途停止 → 没跑的项补 stopped 行 (进库 + 进结果)
    ⑤ stopped 与 skipped 分开数 (§9.3: 一个是用户主动跳, 一个是没跑到)
    ⑥ 正在跑的那一项被停止 → 它自己判 stopped, 不是 passed

只碰内存里的假 Database / 假 report —— 不写生产库、不写 reports/pdf/。
跑法: .venv/bin/python -m tests.test_summary_total
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import runner as R
from server.tests.base import TestCase, TestResult

FAILS = []
PASSES = 0

# 假库记下来的东西 (断言要看)
FINISHED = []          # [(run_id, status, summary)]
WRITTEN = []           # [(run_id, test_name, fields)]


def check(cond, msg, extra=""):
    global PASSES
    if cond:
        PASSES += 1
        print("    ✓ " + msg)
    else:
        FAILS.append(msg)
        print("    ✗ " + msg + ("  → " + str(extra) if extra else ""))


class FakeDb:
    @staticmethod
    async def create_run(config, meta=None): return 1

    @staticmethod
    async def create_mini_run(pid, config, meta=None): return 99

    @staticmethod
    async def get_run(rid): return None

    @staticmethod
    async def upsert_result(run_id, test_name, **fields):
        WRITTEN.append((run_id, test_name, fields))

    @staticmethod
    async def get_result_log(run_id, test_name): return None

    @staticmethod
    async def finish_run(run_id, status, summary, error=None):
        FINISHED.append((run_id, status, summary))

    @staticmethod
    async def set_pdf_path(*a, **k): pass


class FakeReport:
    @classmethod
    async def export_pdf(cls, run, results): return "/fake/report.pdf"

    @staticmethod
    def find_chromium(): return "/fake/chromium"

    @staticmethod
    def output_dir(): return None


R.Database = FakeDb
R.report = FakeReport
R.INTERVAL_BETWEEN_TESTS_SEC = 0


def make_test(name, status, error=None, on_run=None, cancel_aware=False):
    """造一个假测试项。

    cancel_aware=True 时, 跑之前会看一眼取消标志 —— 真的测试项就是这样的
    (ctx.is_cancelled()), 工人点了「停止」, 正在跑的那项自己停下来。
    """

    class T(TestCase):
        def __init__(self):
            self.name = name
            self.display_name = f"假测试项-{name}"

        async def run(self, ctx):
            if on_run:
                await on_run(ctx)
            if cancel_aware and await ctx.is_cancelled():
                return TestResult(status="stopped", error="用户中途停止")
            return TestResult(status=status, error=error)

    T.name = name
    T.display_name = f"假测试项-{name}"
    return T


async def run_round(specs):
    """跑一轮。specs = [{name, status, error, on_run, cancel_aware}, ...]

    返回 (runner, summary, 整轮状态)。
    """
    FINISHED.clear()
    WRITTEN.clear()
    enabled = []
    for sp in specs:
        cls = make_test(sp["name"], sp.get("status", "passed"),
                        sp.get("error"), sp.get("on_run"),
                        sp.get("cancel_aware", False))
        enabled.append((sp["name"], cls))

    rn = R.TestRunner()
    rn.run_id = 1
    rn.status = "running"

    cfg = {"tests": {n: {"enabled": True} for n, _ in enabled}, "project_root": ""}
    await rn._run_tests_body(enabled, cfg, write_to_parent=False)
    summary = FINISHED[-1][2] if FINISHED else {}
    return rn, summary, (FINISHED[-1][1] if FINISHED else None)


def _stopper(rn):
    """造一个「按下停止」的回调。"""
    async def _cb(ctx):
        rn.cancelled = True
    return _cb


async def case1():
    print("【1】一项都没实际执行 (全 skipped) → 整轮判 **skipped**, 不是 passed")
    rn, summary, status = await run_round([
        {"name": "a", "status": "skipped", "error": "卡槽是空的"},
        {"name": "b", "status": "skipped", "error": "卡槽是空的"},
        {"name": "c", "status": "skipped", "error": "卡槽是空的"},
    ])
    check(status == "skipped",
          "整轮判 skipped (不是 passed —— §9.2 明令 total=0 不得判 passed)", status)
    check(summary.get("total") == 0, "total = 0", summary.get("total"))
    check(summary.get("passed") == 0, "passed = 0", summary.get("passed"))
    check(summary.get("skipped") == 3, "skipped = 3", summary.get("skipped"))
    check(summary.get("planned") == 3, "planned = 3 (勾选数单列)", summary.get("planned"))


async def case2():
    print("\n【2】total = passed + failed + error, skipped / stopped **不计入**")
    rn, summary, status = await run_round([
        {"name": "a", "status": "passed"},
        {"name": "b", "status": "passed"},
        {"name": "c", "status": "skipped", "error": "卡槽是空的"},
    ])
    check(summary.get("total") == 2,
          "total = 2 (只数实际跑过的两项, 不是勾选的 3)", summary.get("total"))
    check(summary.get("skipped") == 1, "skipped = 1 (单列, 不进 total)",
          summary.get("skipped"))
    check(summary.get("planned") == 3, "planned = 3", summary.get("planned"))
    check(status == "passed", "整轮判 passed", status)
    acct = (summary.get("passed", 0) + summary.get("failed", 0)
            + summary.get("error", 0) + summary.get("skipped", 0)
            + summary.get("not_run", 0))
    check(acct == summary.get("planned"),
          "各状态计数加起来 == 勾选数 (数字对得上账)", f"{acct} vs {summary.get('planned')}")


async def case3():
    print("\n【3】有失败项 → total 含 failed, 整轮判 failed")
    rn, summary, status = await run_round([
        {"name": "a", "status": "passed"},
        {"name": "b", "status": "failed", "error": "温度超了"},
        {"name": "c", "status": "skipped", "error": "卡槽是空的"},
    ])
    check(summary.get("total") == 2, "total = 2 (failed 也算执行过)",
          summary.get("total"))
    check(summary.get("failed") == 1, "failed = 1", summary.get("failed"))
    check(status == "failed", "整轮判 failed", status)


async def case4():
    print("\n【4】中途停止 → 没跑到的项补 **stopped** 行 (进库 + 进结果)")
    rn = R.TestRunner()
    rn.run_id = 1
    rn.status = "running"
    FINISHED.clear()
    WRITTEN.clear()

    enabled = []
    for nm in ("a", "b", "c"):
        # 第一项跑完就把「停止」按下来 (工人刚好在第一项结束时点了停止)
        cb = _stopper(rn) if nm == "a" else None
        enabled.append((nm, make_test(nm, "passed", None, cb)))

    cfg = {"tests": {n: {"enabled": True} for n, _ in enabled}, "project_root": ""}
    await rn._run_tests_body(enabled, cfg, write_to_parent=False)
    summary = FINISHED[-1][2]
    status = FINISHED[-1][1]

    check(status == "stopped", "整轮判 stopped", status)
    check(len(rn.results) == 3,
          "三个项都有结果 (没跑的那两个也补上了)", list(rn.results))
    check(rn.results.get("b") and rn.results["b"].status == "stopped",
          "b 是 stopped", rn.results.get("b") and rn.results["b"].status)
    check(rn.results.get("c") and rn.results["c"].status == "stopped",
          "c 是 stopped", rn.results.get("c") and rn.results["c"].status)
    # 补的行必须真的进库 —— 报告是从库里读逐项结果的
    stopped_rows = [f for (_rid, tn, f) in WRITTEN
                    if tn in ("b", "c") and f.get("status") == "stopped"]
    check(len(stopped_rows) == 2, "b / c 各补了一行进库", len(stopped_rows))
    check(all(f.get("error") for f in stopped_rows),
          "补的行带原因 (报告上写得出为什么没测)",
          [f.get("error") for f in stopped_rows])
    check(summary.get("not_run") == 2, "not_run = 2 (未跑到单列)", summary.get("not_run"))
    check(summary.get("total") == 1,
          "total = 1 (只算真跑过的那一项, stopped 不进 total)",
          summary.get("total"))


async def case5():
    print("\n【5】stopped 与 skipped 要分开数 (§9.3)")
    rn, summary, status = await run_round([
        {"name": "a", "status": "passed"},
        {"name": "b", "status": "skipped", "error": "卡槽是空的"},
        {"name": "c", "status": "passed"},
    ])
    check(summary.get("skipped") == 1, "skipped = 1 (用户主动跳的那项)",
          summary.get("skipped"))
    check(summary.get("not_run") == 0, "not_run = 0 (这轮没被停止)",
          summary.get("not_run"))
    check(summary.get("total") == 2, "total = 2 (跳掉的那项不算执行过)",
          summary.get("total"))


async def case6():
    print("\n【6】正在跑的那一项被停止 → 它自己判 **stopped**, 不是 passed")
    rn = R.TestRunner()
    rn.run_id = 1
    rn.status = "running"
    FINISHED.clear()
    WRITTEN.clear()

    async def _cb(ctx):
        rn.cancelled = True

    enabled = [("a", make_test("a", "passed", None, _cb, cancel_aware=True)),
               ("b", make_test("b", "passed"))]

    cfg = {"tests": {n: {"enabled": True} for n, _ in enabled}, "project_root": ""}
    await rn._run_tests_body(enabled, cfg, write_to_parent=False)
    summary = FINISHED[-1][2]
    status = FINISHED[-1][1]

    check(rn.results.get("a") and rn.results["a"].status == "stopped",
          "a 判 stopped (它没跑完, 不能算通过)",
          rn.results.get("a") and rn.results["a"].status)
    check(status == "stopped", "整轮判 stopped", status)
    check(summary.get("total") == 0,
          "total = 0 (没有一项跑完出结论)", summary.get("total"))
    # §9.3: 「当前正在进行这项 stopped, 其余未跑的项**也** stopped」——
    # 所以 a (被停止的那项) 和 b (没轮到的项) 都算 not_run, 是 2 不是 1。
    check(summary.get("not_run") == 2,
          "not_run = 2 (a 是被停止的, b 是没跑到的 —— 两者都是 stopped)",
          summary.get("not_run"))


async def main():
    await case1()
    await case2()
    await case3()
    await case4()
    await case5()
    await case6()
    print("\n" + ("全部通过" if not FAILS else f"有失败项 ({len(FAILS)})"))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
