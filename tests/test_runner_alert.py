"""回归测试: runner 把**界面大横幅**广播出去 (§6.7.3 的提示通道)。

起因 (2026-10-28, 产线提的): 「屏幕马上要黑了」这个提示, 光写进日志框
不算数 —— 马上要黑的那一块就是工人正盯着的那一块, 提示得摆在进度条底下、
大字重色。于是加了一条新的 WS 事件 `alert`: 测试项调 `ctx.on_alert(...)`,
runner 广播出去, 前端摆横幅。

这条通道断了的后果是**静默**的: 测试照跑、判定照给、报告照出, 只是工人
看不见那句提示 —— 屏突然黑了, 工人只会以为工具卡死。所以这条通道值得
单独盯一道。

这里验三件事:
    ① 测试项调 ctx.on_alert → 订阅者收到 {"type": "alert", ...}
    ② 文字/颜色/倒计时都原样带过去(前端要靠这几个字段渲染横幅)
    ③ 空 text 也得发出去 —— 那是「收起横幅」的信号, 静默吞掉的话横幅
      会一直杵在界面上

只碰内存里的假 Database / 假 report —— 不写生产库、不写 reports/pdf/。
跑法: .venv/bin/python -m tests.test_runner_alert
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
    async def export_pdf(cls, run, results): return "/fake/report.pdf"

    @staticmethod
    def find_chromium(): return "/fake/chromium"

    @staticmethod
    def output_dir(): return None


R.Database = FakeDb
R.report = FakeReport
R.INTERVAL_BETWEEN_TESTS_SEC = 0


def make_test(alerts):
    """造一个假测试项: 按顺序发几条横幅。

    alerts 里每一项是 (text, level, seconds) —— 最后一条传空 text 的那条
    就是「收起横幅」。
    """

    class T(TestCase):
        name = "fake"
        display_name = "假测试项"

        async def run(self, ctx):
            for text, level, seconds in alerts:
                await ctx.on_alert(text, level, seconds)
            return TestResult(status="passed")

    return T


async def run_and_collect(alerts, run_id=1):
    """跑一遍假测试项, 把广播出去的事件收回来。"""
    rn = R.TestRunner()
    rn.run_id = run_id
    q = rn.subscribe()
    cfg = {"tests": {"fake": {"enabled": True}}, "project_root": ""}
    await rn._run_one("fake", make_test(alerts), cfg, write_to_parent=False)
    events = []
    while not q.empty():
        events.append(q.get_nowait())
    return events


async def case1():
    print("【1】测试项调 ctx.on_alert → 订阅者收到 alert 事件")
    events = await run_and_collect([("屏幕即将黑屏", "danger", 3)])
    alerts = [e for e in events if e.get("type") == "alert"]
    check(len(alerts) == 1, "收到 1 条 alert 事件",
          [e.get("type") for e in events])
    if not alerts:
        return
    a = alerts[0]
    check(a.get("name") == "fake", "带的是这一项的名字", a.get("name"))
    check(a.get("text") == "屏幕即将黑屏", "文字原样", a.get("text"))
    check(a.get("level") == "danger", "颜色原样", a.get("level"))
    check(a.get("seconds") == 3, "倒计时秒数原样", a.get("seconds"))


async def case2():
    print("\n【2】空 text 也得发出去 —— 那是「收起横幅」的信号")
    events = await run_and_collect([
        ("屏幕即将黑屏", "danger", 3),
        ("", "warn", 0),
    ])
    alerts = [e for e in events if e.get("type") == "alert"]
    check(len(alerts) == 2, "两条 alert 事件", len(alerts))
    if len(alerts) < 2:
        return
    check(alerts[1].get("text") == "", "第二条是空文字(收起)", alerts[1].get("text"))


async def case3():
    print("\n【3】没调 on_alert 的项 → 一条 alert 都不发(别乱发)")
    events = await run_and_collect([])
    alerts = [e for e in events if e.get("type") == "alert"]
    check(alerts == [], "没有多余的 alert 事件", alerts)


async def main():
    await case1()
    await case2()
    await case3()
    print("\n" + ("全部通过" if not FAILS else f"有失败项 ({len(FAILS)})"))
    for f in FAILS:
        print("  - " + f)
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
