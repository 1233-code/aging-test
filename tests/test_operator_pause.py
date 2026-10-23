"""§7.8 回归测试: 「停下来等操作员」到底有没有真的生效。

这个 bug 反复咬过 —— 前端弹框挡住了, 后端却一路跑到底, 于是工人点
「跳过 TF 卡」时看到的已经是 USB 的界面。所以这里测的是**后端的时序**:
下一项测试必须等操作员作答之后才开始。

只把 Database / Config 换成内存里的假实现, 不碰真库、不碰真硬件。
跑法: .venv/bin/python -m tests.test_operator_pause
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import runner as R
from server.tests.base import TestCase, TestResult, TEST_REGISTRY

EVENTS = []


class FakeDb:
    # ⚠️ **签名必须跟 server/db.py 保持一致** —— runner 收尾会调
    # `create_run(config, meta)` 传 SN/工位/批次(§2.2/§8.2), 这里少写一个
    # `meta=None` 的话, 平时测不到(本测试直接调 `_run_tests`, 绕过了 `start()`),
    # 等哪天有人补了 `start()` 的用例就炸 TypeError —— 而且报的是
    # "假实现签名不对", 很容易被误读成 runner 的 bug。
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
    """§8.2 报告导出必须在这里挡掉。

    runner 的收尾路径(`_run_tests_body` 末尾)会 `await self._export_pdf(...)`,
    而它调的是真的 `server.report` —— 那会去读真 config.yaml, 然后**把一份 PDF
    写进生产归档目录 `reports/pdf/`**。本测试每跑一个用例就是一轮"完整运行",
    于是每跑一次往归档目录里丢一份 `unknown_*.pdf`。

    测试往生产归档目录里写产物, 和把测试数据写进生产库是同一类错误: 归档目录
    是给产线做纸质归档用的, 混进 `unknown_` 的假报告, 事后没人分得清。
    """
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
R.Config.get = staticmethod(lambda *a, **k: {})
R.INTERVAL_BETWEEN_TESTS_SEC = 0     # 缩短静默期, 让测试只聚焦「停住」


def mk(name, display, ask_times=0):
    """造一个假测试项: 前 ask_times 次都「没就位」(要问操作员), 之后正常。"""
    class T(TestCase):
        pass
    T.name, T.display_name = name, display
    seen = {"n": 0}

    async def run(self, ctx):
        seen["n"] += 1
        EVENTS.append(f"run:{name}#{seen['n']}")
        r = TestResult(status="skipped", error="没就位")
        r.metrics["ask"] = seen["n"] <= ask_times
        return r

    def operator_prompt(self, result):
        return {"text": f"{name} 没就位"} if result.metrics.get("ask") else None

    T.run, T.operator_prompt = run, operator_prompt
    return T


def install(tf_ask, hdmi_ask=0, usb_ask=0):
    for n, d, a in (("tfcard", "TF 卡", tf_ask), ("hdmi", "HDMI", hdmi_ask),
                    ("usb", "USB", usb_ask)):
        TEST_REGISTRY[n] = mk(n, d, a)


async def _start(rn):
    return asyncio.create_task(rn._run_tests(
        [(n, TEST_REGISTRY[n]) for n in ("tfcard", "hdmi", "usb")],
        {"tests": {}, "project_root": ""}, write_to_parent=False,
    ))


async def drive(rn, plan):
    """跑一轮, 每次看到 waiting_operator 就按 plan 作答 (plan 用完按「跳过」)。"""
    task = await _start(rn)
    it = iter(plan)
    while not task.done():
        await asyncio.sleep(0.02)
        w = rn._operator_wait
        if w:
            d = next(it, "skip")
            EVENTS.append(f"wait:{w['name']}->{d}")
            rn.answer_operator(d)
    await task
    return list(EVENTS)


def drain():
    EVENTS.clear()


async def main():
    ok = True

    print("【1】TF 卡没插 → 选「跳过」: 下一项必须等决定之后才开始")
    drain(); install(tf_ask=99)
    got = await drive(R.TestRunner(), ["skip"])
    print("    " + " | ".join(got))
    if got.index("run:hdmi#1") > got.index("wait:tfcard->skip"):
        print("    ✓ hdmi 在收到决定之后才开始")
    else:
        print("    ✗ 后端没等操作员就开跑了 —— 这正是原 bug")
        ok = False

    print("\n【2】TF 卡没插 → 选「已插好」: 应原地重跑 tfcard, 不碰后面的项")
    drain(); install(tf_ask=99)
    got = await drive(R.TestRunner(), ["retest"])
    print("    " + " | ".join(got))
    if got.count("run:tfcard#2") == 1:
        print("    ✓ 原地重跑了 tfcard 第 2 遍")
    else:
        print("    ✗ 选「已插好」后没有原地重测 tfcard")
        ok = False

    print("\n【3】一直插不好: 重测次数必须有上限")
    drain(); install(tf_ask=999)
    got = await drive(R.TestRunner(), ["retest"] * 30)
    # 注意: list.count() 是**精确元素匹配**, 不是子串匹配 ——
    # 用它去数 "run:tfcard#1"/"run:tfcard#2" 会一律得 0。
    n = sum(1 for e in got if e.startswith("run:tfcard#"))
    print(f"    共跑了 {n} 遍 (上限 1+{R.OPERATOR_MAX_RETEST})")
    if n == 1 + R.OPERATOR_MAX_RETEST:
        print("    ✓ 达到上限后自动放行")
    else:
        print("    ✗ 重试次数没被限制住")
        ok = False

    print("\n【4】没人操作: 超时后必须自己往下走, 不能把整轮挂死")
    drain(); install(tf_ask=999)
    R.OPERATOR_WAIT_DEFAULT_SEC = 0.3      # 只有这一场靠超时放行
    task = await _start(R.TestRunner())
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=3.0)
        print("    ✓ 等待超时后自动继续, 整轮正常结束")
    except asyncio.TimeoutError:
        task.cancel()
        print("    ✗ 卡死不动了 —— 工人一走开整轮就废了")
        ok = False
    R.OPERATOR_WAIT_DEFAULT_SEC = 5.0

    print("\n" + ("全部通过" if ok else "有失败项"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))