"""§5 回归测试: 每项测试之间的静默期(项间间隔)。

产线 2026-10-26 的要求: 「每项测试之间间隔5s改成3s吧, 五秒有点长了」。

这里盯三件事:
  ① **默认值就是 3 秒** —— 改的是 `server/runner.py` 的
     `INTERVAL_BETWEEN_TESTS_SEC`; 这个数是**写在规格里**的(§5「3 秒静默期」),
     有人改回去时这条会先报出来, 免得文档和代码各说各话。
  ② **事件带的秒数跟常量一致** —— 前端那个倒计时读的是 `between_tests` 事件里
     的 `duration_sec`; 事件带的数跟实际等待的数对不上, 界面上就会倒计时 3 秒
     却等 5 秒(或反过来)。
  ③ **第一项之前不等待** —— 间隔是「项**之间**」的, 整轮开头不白等 3 秒。

只把 Database / Config 换成内存里的假实现, 不碰真库、不碰真硬件、
不往生产的 `reports/pdf/` 里写东西(见 test_operator_pause.py 里 FakeReport
那段说明)。

跑法: .venv/bin/python -m tests.test_inter_test_interval
"""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import runner as R
from server.tests.base import TestCase, TestResult, TEST_REGISTRY

FAILS = []
PASSES = 0

EVENTS = []
STARTS = []          # (项名, 时刻)


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
    async def upsert_result(*a, **k): pass

    @staticmethod
    async def finish_run(*a, **k): pass

    @staticmethod
    async def set_pdf_path(*a, **k): pass


class FakeReport:
    """§8.2 报告导出必须在这里挡掉 —— 真跑的话会往生产归档目录丢假报告。"""

    @classmethod
    async def export_pdf(cls, run, results):
        return "/fake/report.pdf"

    @staticmethod
    def find_chromium(): return "/fake/chromium"

    @staticmethod
    def output_dir(): return None


R.Database = FakeDb
R.report = FakeReport
R.Config.get = staticmethod(lambda *a, **k: {})


def mk(name):
    """造一个假测试项: 记录自己什么时候开的跑。"""
    class T(TestCase):
        pass

    T.name, T.display_name = name, name.upper()

    async def run(self, ctx):
        STARTS.append((name, time.monotonic()))
        EVENTS.append(f"run:{name}")
        return TestResult(status="passed")

    T.run = run
    return T


async def main():
    ok = True

    print("【1】默认间隔就是 3 秒(产线 2026-10-26 定的)")
    check(R.INTERVAL_BETWEEN_TESTS_SEC == 3,
          "INTERVAL_BETWEEN_TESTS_SEC == 3", R.INTERVAL_BETWEEN_TESTS_SEC)

    print("\n【2】间隔真的发生了, 而且事件带的秒数跟常量一致")
    # 用 1 秒而不是 3 秒: 验的是**机制**, 3 秒那个值上面已经钉住了;
    # 每轮测试都真等 3 秒, 套件自己就慢得没法用了。
    R.INTERVAL_BETWEEN_TESTS_SEC = 1
    EVENTS.clear()
    STARTS.clear()
    for n in ("alpha", "beta"):
        TEST_REGISTRY[n] = mk(n)

    rn = R.TestRunner()
    q = rn.subscribe()
    t0 = time.monotonic()
    await rn._run_tests(
        [(n, TEST_REGISTRY[n]) for n in ("alpha", "beta")],
        {"tests": {}, "project_root": ""}, write_to_parent=False,
    )
    elapsed = time.monotonic() - t0

    # 把事件收干净
    events = []
    while not q.empty():
        events.append(q.get_nowait())
    bt = [e for e in events if e.get("type") == "between_tests"]

    check(len(bt) == 1, "两项之间只广播了一次 between_tests", len(bt))
    if bt:
        check(bt[0].get("duration_sec") == 1,
              "事件里的 duration_sec 跟实际等待的秒数一致(前端倒计时靠它)",
              bt[0].get("duration_sec"))
        check(bt[0].get("next") == "beta",
              "事件里说的是下一项的名字", bt[0].get("next"))
    else:
        check(False, "事件里的 duration_sec 跟实际等待的秒数一致(前端倒计时靠它)",
              "没收到 between_tests")

    check(len(STARTS) == 2, "两项都跑了", STARTS)
    if len(STARTS) == 2:
        gap = STARTS[1][1] - STARTS[0][1]
        check(gap >= 1.0, "第二项确实隔了至少 1 秒才开跑", "%.2fs" % gap)
        check(gap < 3.0, "也没有多等(等待就是配置的那个秒数)", "%.2fs" % gap)

    print("\n【3】第一项之前不等待")
    check(bt and bt[0].get("next") == "beta",
          "间隔只出现在项与项之间, 整轮开头不白等", [e.get("next") for e in bt])
    check(elapsed < 2.0, "整轮耗时 ≈ 一段间隔(没有多等一段)", "%.2fs" % elapsed)

    print("\n" + ("全部通过" if not FAILS else "有失败项 (%d)" % len(FAILS)))
    for f in FAILS:
        print("  ✗ " + f)
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
