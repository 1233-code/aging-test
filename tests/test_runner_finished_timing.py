"""回归测试: `run_finished` 事件必须**同时**带上 `started_at` 和 `finished_at`。

起因 (2026-10-28, §7.8 本轮总耗时): 顶栏那个读秒是「结束 − 起点」算出来的,
所以这两个数必须**来自同一轮**、一起发给前端。

踩过的坑: `run_finished` 原来**只带 `finished_at`**。前端于是这么写:
    run_finished → runFinishedAt = msg.finished_at
    起点          → 沿用上一条 `run_started` 里的那个
普通轮次没问题(两条事件本来就是同一轮), 但**合并重测**(§7.7)就错位了:
    · `run_started`  带的是**重测轮**的起点
    · `run_finished` 带的是**父轮**的结束时间(合并时 runner 已切到父轮)
⇒ 界面上那个总耗时 = 「父轮的结束 − 重测轮的开始」, 大得离谱。
要等后面那次 syncStatus 的快照来纠正 —— 工人刚好在这中间抬头看一眼,
看到的就是一个假数。

这里盯两件事:
    ① `run_finished` 事件里**有** `started_at`
    ② 它跟 `finished_at` 是**同一轮**的一对(合并后都指向父轮)

只碰内存里的假 Database / 假 report —— 不写生产库、不写 reports/pdf/。
跑法: .venv/bin/python -m tests.test_runner_finished_timing
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import runner as R
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


class FakeDb:
    @staticmethod
    async def create_run(config, meta=None): return 1

    @staticmethod
    async def create_mini_run(pid, config, meta=None): return 99

    @staticmethod
    async def get_run(rid): return None

    @staticmethod
    async def upsert_result(run_id, test_name, **fields): pass

    @staticmethod
    async def get_result_log(run_id, test_name): return ""

    @staticmethod
    async def finish_run(*a, **k): pass

    @staticmethod
    async def set_pdf_path(*a, **k): pass


class FakeReport:
    @classmethod
    async def export_pdf(cls, run, results, retest_runs=None):
        return "/fake/report.pdf"

    @staticmethod
    def find_chromium(): return "/fake/chromium"

    @staticmethod
    def output_dir(): return None


R.Database = FakeDb
R.report = FakeReport
R.INTERVAL_BETWEEN_TESTS_SEC = 0


class FakeTest(TestCase):
    name = "fake"
    display_name = "假测试项"

    async def run(self, ctx):
        return TestResult(status="passed")


async def run_one_round():
    """跑一轮假测试, 把广播出去的事件收回来。"""
    rn = R.TestRunner()
    q = rn.subscribe()
    rn.run_id = 1
    rn.started_at = 1000.0          # 假装这一轮是 1000 秒时开的
    cfg = {"tests": {"fake": {"enabled": True}}, "project_root": ""}
    await rn._run_tests([("fake", FakeTest)], cfg, write_to_parent=False)
    events = []
    while not q.empty():
        events.append(q.get_nowait())
    return rn, events


async def case1():
    print("【1】run_finished 必须带上 started_at")
    rn, events = await run_one_round()
    fin = [e for e in events if e.get("type") == "run_finished"]
    check(len(fin) == 1, "收到 1 条 run_finished", len(fin))
    if not fin:
        return
    f = fin[0]
    check("started_at" in f, "事件里有 started_at", sorted(f.keys()))
    check(f.get("started_at") == 1000.0,
          "started_at 是这一轮自己的起点 (1000)", f.get("started_at"))
    check("finished_at" in f, "事件里有 finished_at", sorted(f.keys()))
    check(f.get("finished_at") == rn.finished_at,
          "finished_at 是这一轮自己的结束时间", f.get("finished_at"))
    # 两个数必须是**同一轮**的一对
    check(f.get("started_at") == 1000.0
          and isinstance(f.get("finished_at"), (int, float))
          and f["finished_at"] >= f["started_at"],
          "两个数是同一轮的一对 (起点 ≤ 结束)", f)


async def case2():
    print("\n【2】出错的收尾路径也得带 started_at")
    """整轮异常终止时 broadcast 的那条 run_finished —— 少一个字段,
    前端就会拿「上一条事件的起点」去配「这一轮的结束时间」, 又是错位。"""
    rn = R.TestRunner()
    q = rn.subscribe()
    rn.run_id = 1
    rn.started_at = 2000.0

    class Boom(TestCase):
        name = "boom"
        display_name = "会炸的项"

        async def run(self, ctx):
            raise RuntimeError("炸了")

    cfg = {"tests": {"boom": {"enabled": True}}, "project_root": ""}
    await rn._run_tests([("boom", Boom)], cfg, write_to_parent=False)
    events = []
    while not q.empty():
        events.append(q.get_nowait())
    fin = [e for e in events if e.get("type") == "run_finished"]
    check(len(fin) == 1, "收到 1 条 run_finished", len(fin))
    if not fin:
        return
    check(fin[0].get("started_at") == 2000.0,
          "出错那条也带上了这一轮的起点", fin[0].get("started_at"))


async def case3():
    print("\n【3】一项都没勾选时的那条 run_finished 也带 started_at")
    rn = R.TestRunner()
    q = rn.subscribe()
    rn.run_id = 1
    rn.started_at = 3000.0
    cfg = {"tests": {}, "project_root": ""}
    await rn._run_tests([], cfg, write_to_parent=False)
    events = []
    while not q.empty():
        events.append(q.get_nowait())
    fin = [e for e in events if e.get("type") == "run_finished"]
    check(len(fin) == 1, "收到 1 条 run_finished", len(fin))
    if not fin:
        return
    check(fin[0].get("started_at") == 3000.0,
          "带上了这一轮的起点", fin[0].get("started_at"))


async def case4():
    print("\n【4】外层护栏兜住的那种异常, 它广播的那条也得带 started_at")
    """这一条是 2026-11-04 补上的**覆盖漏洞**。

    【2】那个「会炸的项」是在 `_run_one` 里面炸的 ⇒ 被**逐项的** try 接住
    ⇒ 走的是正常收尾那条广播。**外层护栏**(`_run_tests` 那个 except)
    走的是**另一条**广播 —— 它接的是「`_run_tests_body` 里没接住的异常」
    (最常见的是写库失败: 某项的 metrics 里混进一个 json 序列化不了的对象,
    它发生在逐项 try 之外)。

    以前**没人测过护栏那条** ⇒ 护栏把 started_at 删掉, 上面三条用例照样全绿
    (实测: 删掉之后变异脚本报「测试还是绿的! 这个变异没被守住」)。
    而护栏恰恰是**最该带上起点**的那条 —— 它是「无论如何都会发出 run_finished」
    的最后一道保险, 前端拿到它就算没有 run_started 也能配上起点。
    """
    rn = R.TestRunner()
    q = rn.subscribe()
    rn.run_id = 1
    rn.started_at = 4000.0

    async def boom(*a, **kw):
        raise RuntimeError("写库炸了(逐项的 try 接不住的那种)")

    rn._run_tests_body = boom
    await rn._run_tests([("fake", FakeTest)],
                        {"tests": {}, "project_root": ""},
                        write_to_parent=False)
    events = []
    while not q.empty():
        events.append(q.get_nowait())
    fin = [e for e in events if e.get("type") == "run_finished"]
    check(len(fin) == 1, "收到 1 条 run_finished", len(fin))
    if not fin:
        return
    check(fin[0].get("started_at") == 4000.0,
          "护栏那条也带上了这一轮的起点", fin[0].get("started_at"))
    check("finished_at" in fin[0], "护栏那条也带结束时间", sorted(fin[0].keys()))
    check(fin[0].get("finished_at", 0) >= 4000.0,
          "结束时间不早于起点", fin[0].get("finished_at"))


async def main():
    await case1()
    await case2()
    await case3()
    await case4()
    print("\n" + ("全部通过" if not FAILS else f"有失败项 ({len(FAILS)})"))
    for f in FAILS:
        print("  - " + f)
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
