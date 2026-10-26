"""§7.6 回归测试: 快照里那几个**新字段**是不是真的在快照上。

前端那套「靠快照恢复」的逻辑 (tests/test_frontend_snapshot.js) 是靠这几个
字段撑起来的:

    test_queue —— 这一轮**计划**跑哪几项、按什么顺序 (含还没跑到的)
    pdf_path   —— 这一轮导出的 PDF 在哪 (§8.2 按钮二选一)
    sn         —— 这一轮测的是哪台机器
    error      —— 整轮异常终止的原因

少一个, 前端就有一条恢复路径是瞎的: 比如少了 test_queue, 页面刷新后
「还没跑到的那几项」会整个消失 —— 进度条分母不对, 总结页也少几行, 而
界面上一点异常都看不出来。

这个测试只碰内存里的假 Database / 假 report —— 不写生产库、不写
reports/pdf/ 归档目录、不碰真硬件。
跑法: .venv/bin/python -m tests.test_snapshot_fields
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import runner as R
from server.tests.base import TestCase, TestResult, TEST_REGISTRY

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
    async def upsert_result(*a, **k): pass

    @staticmethod
    async def finish_run(*a, **k): pass

    @staticmethod
    async def set_pdf_path(*a, **k): pass


class FakeReport:
    """挡住真导出 —— 真导出会往生产归档目录 reports/pdf/ 里写文件。"""

    calls = 0

    @classmethod
    async def export_pdf(cls, run, results):
        cls.calls += 1
        return "/fake/report.pdf"

    @staticmethod
    def find_chromium(): return "/fake/chromium"

    @staticmethod
    def output_dir(): return None


R.Database = FakeDb
R.report = FakeReport
R.INTERVAL_BETWEEN_TESTS_SEC = 0


def mk(name, display):
    """造一个假测试项 —— 只为占住队列, 不会真的跑。"""

    class T(TestCase):
        pass

    T.name, T.display_name = name, display

    async def run(self, ctx):
        return TestResult(status="passed")

    T.run = run
    return T


def install(*pairs):
    for n, d in pairs:
        TEST_REGISTRY[n] = mk(n, d)


async def main():
    print("【1】snapshot() 必须带齐 test_queue / pdf_path / sn / error")
    install(("memory", "内存"), ("emmc", "EMMC"), ("cpu", "CPU"))

    rn = R.TestRunner()
    rn.run_id = 120
    rn.status = "running"
    rn.sn = "SN-TEST-0001"
    rn.test_queue = ["memory", "emmc", "cpu"]
    rn.pdf_path = "/reports/pdf/SN-TEST-0001_20261026_092509.pdf"
    rn.run_error = "KeyError('x')"

    s = rn.snapshot()
    for f in ("test_queue", "pdf_path", "sn", "error"):
        check(f in s, f"snapshot 里有 {f}")
    check(s["test_queue"] == ["memory", "emmc", "cpu"],
          "test_queue 原样带出来", s.get("test_queue"))
    check(s["pdf_path"] == rn.pdf_path, "pdf_path 原样带出来")
    check(s["sn"] == "SN-TEST-0001", "sn 原样带出来")
    check(s["error"] == "KeyError('x')", "error 原样带出来")

    # 队列得是**副本** —— 快照返回后 runner 还会往队列里加东西的话,
    # 已经发出去的快照会被偷偷改掉(前端拿到的列表会跟别的连接看到的不一样)。
    s2 = rn.snapshot()
    s2["test_queue"].append("篡改")
    check(rn.test_queue == ["memory", "emmc", "cpu"],
          "test_queue 是副本, 改快照改不到 runner", rn.test_queue)

    print("\n【2】start(): 队列必须跟**真正要跑的**列表一模一样")
    rn2 = R.TestRunner()
    ran = {}

    async def fake_run_tests(enabled_tests, config, write_to_parent=False):
        ran["names"] = [n for n, _ in enabled_tests]
        ran["order"] = [n for n, _ in enabled_tests]

    rn2._run_tests = fake_run_tests

    cfg = {
        "tests": {
            "memory": {"enabled": True}, "emmc": {"enabled": True},
            "cpu": {"enabled": True}, "hdmi": {"enabled": False},
        },
        "order": None,
        "project_root": "",
    }
    await rn2.start(cfg)
    await asyncio.sleep(0.01)          # 让 start() 里建的那个 task 跑起来
    check("names" in ran, "确实开跑了", ran)
    check(rn2.test_queue == ran["names"],
          "队列 == 实际跑的列表 (顺序也一样)",
          f"{rn2.test_queue} vs {ran.get('names')}")
    check("hdmi" not in rn2.test_queue, "没勾的项不在队列里")
    check(len(rn2.test_queue) == 3, "队列长度 = 3", len(rn2.test_queue))

    print("\n【3】start_mini(): 队列只含**重测的那几项**")
    rn3 = R.TestRunner()
    ran3 = {}

    async def fake_run_tests3(enabled_tests, config, write_to_parent=False):
        ran3["names"] = [n for n, _ in enabled_tests]

    rn3._run_tests = fake_run_tests3

    class FakeDb2(FakeDb):
        @staticmethod
        async def get_run(rid):
            return {
                "id": rid, "sn": "SN-PARENT",
                "operator": "产线甲", "batch": "B1",
                "config": {
                    "tests": {
                        "memory": {"enabled": True}, "emmc": {"enabled": True},
                        "cpu": {"enabled": True},
                    },
                    "project_root": "",
                },
            }

    R.Database = FakeDb2
    await rn3.start_mini(120, ["emmc", "cpu"])
    await asyncio.sleep(0.01)
    check(rn3.test_queue == ["emmc", "cpu"],
          "mini 队列 = 只重测的两项", rn3.test_queue)
    check(rn3.test_queue == ran3.get("names"),
          "mini 队列 == 实际跑的列表", f"{rn3.test_queue} vs {ran3.get('names')}")
    check(rn3.sn == "SN-PARENT", "mini 沿用父轮的 SN", rn3.sn)
    check(rn3.operator == "产线甲", "mini 沿用父轮的测试人员", rn3.operator)
    R.Database = FakeDb

    print("\n【4】run_finished 事件要带上 sn / finished_at / error")
    rn4 = R.TestRunner()
    q = rn4.subscribe()
    rn4.run_id = 120
    rn4.sn = "SN-TEST-0001"
    rn4.status = "passed"
    rn4.finished_at = 12345.0
    rn4.run_error = None
    rn4.run_config = {}
    await rn4._broadcast({
        "type": "run_finished", "status": "passed", "summary": {},
        "mode": "full", "parent_run_id": None,
        "sn": rn4.sn, "finished_at": rn4.finished_at, "error": rn4.run_error,
    })
    ev = q.get_nowait()
    check(ev["type"] == "run_finished", "收到 run_finished")
    check(ev.get("sn") == "SN-TEST-0001", "事件里有 sn", ev.get("sn"))
    check(ev.get("finished_at") == 12345.0, "事件里有 finished_at")
    check("error" in ev, "事件里有 error 这个键(哪怕是 None)")

    print("\n【5】导出完 PDF 之后, 快照里的 pdf_path 要有值 (§8.2 按钮二选一)")
    rn5 = R.TestRunner()
    rn5.run_id = 120
    rn5.run_config = {}
    await rn5._export_pdf({"passed": 1, "failed": 0})
    check(rn5.pdf_path == "/fake/report.pdf", "runner 上记下了导出路径", rn5.pdf_path)
    check(rn5.snapshot()["pdf_path"] == "/fake/report.pdf",
          "快照里带上了 pdf_path")
    check(FakeReport.calls == 1, "确实调了一次导出")

    print("\n【6】导出失败**不能**掀翻整轮 (§8.2 硬规则)")
    class BrokenReport(FakeReport):
        @classmethod
        async def export_pdf(cls, run, results):
            raise OSError("磁盘满了")

    R.report = BrokenReport
    rn6 = R.TestRunner()
    rn6.run_id = 121
    rn6.run_config = {}
    q6 = rn6.subscribe()
    await rn6._export_pdf({"passed": 1, "failed": 0})
    check(rn6.pdf_path is None, "导出失败 → pdf_path 保持 None", rn6.pdf_path)
    evs = []
    while not q6.empty():
        evs.append(q6.get_nowait())
    check(any(e["type"] == "report_failed" for e in evs),
          "推了一条 report_failed", [e["type"] for e in evs])
    R.report = FakeReport

    print("\n" + ("全部通过" if not FAILS else f"有失败项 ({len(FAILS)})"))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
