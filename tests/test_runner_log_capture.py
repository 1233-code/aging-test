"""回归测试: runner 把**完整**日志留了档 (§8.2「全量测试日志」)。

报告的日志附录要能当**证据**用, 前提是它记的是全部 —— 这件事的成败不在
模板上, 在**数据源**上: 报告渲染时读的是 DB 里 results.log 那一列。

而这一列以前是残缺的, 因为每项自己写自己的:

    测试项            实时日志      落进 results.log 的
    ─────────────────────────────────────────────
    CPU               ✓            末尾 400 行 (源头就被截了)
    GPU               ✓            末尾 20 行
    内存               ✓            末尾 200 行
    WiFi/USB/网口/    ✓            **空**
    HDMI/风扇/TF/eMMC

改法: 抓点挪到 runner —— `_on_log()` 是各项推日志的**唯一出口**, 每一行
都从那里过。runner 留一份全的, 写库时用它, 各项自己写的那份(截断的)就
不再算数了。

这里验五件事:
    ① 一项推 100 行 → 落库的是 100 行, 一行不少
    ② 各项自己写的半截 result.log 不算数
    ③ §7.8 重试 (插卡后原地重试) → 几次尝试的日志**都**留着, 且不重复
    ④ mini-run 写父轮 → **追加**而不是覆盖 (覆盖掉父轮的原始日志就没了)
    ⑤ 日志超上限 → 截断了必须在指标里留痕 (报告要写出来)

只碰内存里的假 Database / 假 report —— 不写生产库、不写 reports/pdf/。
跑法: .venv/bin/python -m tests.test_runner_log_capture
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
WRITTEN = []          # [(run_id, test_name, fields)]
PARENT_LOGS = {}      # {(run_id, test_name): log}


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
        # ⚠️ 只更新**传进来的**字段 —— 真库 (db.upsert_result) 就是这么做的:
        #    `UPDATE results SET <传了的列> WHERE id=?`。不这么写的话,
        #    「把该项置成 running」那次调用会把 log 列抹成 NULL, 假库比真库
        #    还狠, 测出来的问题全是假的。
        if "log" in fields:
            PARENT_LOGS[(run_id, test_name)] = fields.get("log")

    @staticmethod
    async def get_result_log(run_id, test_name):
        return PARENT_LOGS.get((run_id, test_name))

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


def make_test(n_lines: int):
    """造一个假测试项: 推 n_lines 行日志, 自己**不写** result.log。

    (真的七项就是这样的 —— 压根不写 result.log, 以前报告里就是空的。)
    """

    class T(TestCase):
        name = "fake"
        display_name = "假测试项"

        async def run(self, ctx):
            for i in range(1, n_lines + 1):
                await ctx.on_log(f"第 {i} 行")
            return TestResult(status="passed")

    return T


def last_written(run_id, test_name):
    for rid, tn, fields in reversed(WRITTEN):
        if rid == run_id and tn == test_name:
            return fields
    return None


async def case1():
    print("【1】一项推 100 行 → 落库的是 100 行, 一行不少")
    T = make_test(100)
    rn = R.TestRunner()
    rn.run_id = 1
    cfg = {"tests": {"fake": {"enabled": True}}, "project_root": ""}
    res, _ = await rn._run_one("fake", T, cfg, write_to_parent=False)

    f = last_written(1, "fake")
    check(f is not None, "这一项写进库了")
    got = (f or {}).get("log") or ""
    missing = [i for i in range(1, 101) if f"第 {i} 行" not in got]
    check(not missing, "100 行一行不少", f"少了 {len(missing)} 行: {missing[:5]}")
    check(got.splitlines()[0] == "第 1 行", "第一行在最前面 (不是只有尾巴)",
          got.splitlines()[:1])
    check(res.log == got, "返回的结果对象和落库的是同一份")


async def case2():
    print("\n【2】各项自己写 result.log 也不行 —— runner 留档的才作数")

    # 模拟 GPU 那种「自己只写末尾 20 行」的情况
    class SelfTruncating(TestCase):
        name = "fake"
        display_name = "假测试项"

        async def run(self, ctx):
            for i in range(1, 101):
                await ctx.on_log(f"第 {i} 行")
            r = TestResult(status="passed")
            r.log = "第 81 行\n第 82 行"      # 自己写的半截
            return r

    rn = R.TestRunner()
    rn.run_id = 2
    cfg = {"tests": {"fake": {"enabled": True}}, "project_root": ""}
    await rn._run_one("fake", SelfTruncating, cfg, write_to_parent=False)
    got = (last_written(2, "fake") or {}).get("log") or ""
    check("第 1 行" in got, "runner 的全档盖过了各项写的半截")
    check("第 100 行" in got, "最后一行也在")


async def case3():
    print("\n【3】§7.8 重试 (插卡后原地重试) → 几次尝试的日志**都**留着")
    attempts = []

    class NeedsRetest(TestCase):
        name = "fake"
        display_name = "假测试项"

        async def run(self, ctx):
            n = len(attempts) + 1
            attempts.append(n)
            await ctx.on_log(f"--- 第 {n} 次尝试 ---")
            await ctx.on_log(f"第 {n} 次: 卡槽是空的")
            r = TestResult(status="failed")
            r.error = "卡槽是空的"
            return r

        def operator_prompt(self, result):
            # 前两次要人再试, 第三次放行
            return "插卡了吗?" if len(attempts) < 3 else None

    async def _always_retest(name, prompt, attempt):
        return "retest"

    rn = R.TestRunner()
    rn.run_id = 3
    rn._wait_for_operator = _always_retest
    cfg = {"tests": {"fake": {"enabled": True}}, "project_root": ""}
    await rn._run_tests_body([("fake", NeedsRetest)], cfg, write_to_parent=False)

    got = (last_written(3, "fake") or {}).get("log") or ""
    check(len(attempts) == 3, "确实重试了 3 次", len(attempts))
    check("第 1 次: 卡槽是空的" in got, "第 1 次的日志还在")
    check("第 2 次: 卡槽是空的" in got, "第 2 次的日志还在")
    check("第 3 次: 卡槽是空的" in got, "第 3 次的日志还在")
    check(got.count("第 1 次: 卡槽是空的") == 1,
          "第 1 次的日志没有重复出现", got.count("第 1 次: 卡槽是空的"))


async def case4():
    print("\n【4】mini-run 写父轮 → 父轮的原始日志**一个字都不许动**")
    # 父轮那一行的 log 是**首测的原始日志** —— 返修时最要看的证据
    # (「当初为什么失败」)。重测那一轮的日志留在**重测轮自己的行**里,
    # 报告里另起一节「重测日志附录」印在「日志附录」后面(§7.7.2)。
    #
    # 所以父轮那行**只更新「结论」那几个字段**(状态/耗时/attempt),
    # log 压根不参与写回 —— 冲掉原始失败原因, 或者把两轮混进同一段,
    # 都是把「事后申诉要的两段证据」毁掉。
    PARENT_LOGS[(10, "fake")] = "父轮原来的日志"

    class Simple(TestCase):
        name = "fake"
        display_name = "假测试项"

        async def run(self, ctx):
            await ctx.on_log("重测的日志")
            return TestResult(status="passed")

    rn = R.TestRunner()
    rn.run_id = 11
    rn.parent_run_id = 10
    cfg = {"tests": {"fake": {"enabled": True}}, "project_root": ""}
    await rn._run_one("fake", Simple, cfg, write_to_parent=True)

    got = PARENT_LOGS.get((10, "fake")) or ""
    check(got == "父轮原来的日志",
          "父轮的原始日志**原封不动**(既没被冲掉, 也没被追加)", got)

    mini_log = PARENT_LOGS.get((11, "fake")) or ""
    check("重测的日志" in mini_log,
          "重测的日志写在**重测轮自己**的行里(报告再从那儿取)", mini_log)

    pf = last_written(10, "fake") or {}
    check(pf.get("status") == "passed",
          "父轮那一项的**结论**更新了(状态)", pf)
    check("log" not in pf,
          "父轮那一行的写回里**没有** log 这个字段", pf)


async def case5():
    print("\n【5】日志超上限 → 截断了必须在指标里留痕 (报告要写出来)")
    saved = R.LOG_MAX_LINES
    R.LOG_MAX_LINES = 5
    try:
        T = make_test(20)
        rn = R.TestRunner()
        rn.run_id = 4
        cfg = {"tests": {"fake": {"enabled": True}}, "project_root": ""}
        await rn._run_one("fake", T, cfg, write_to_parent=False)
        f = last_written(4, "fake") or {}
        m = f.get("metrics") or {}
        check(m.get("log_truncated"), "指标里标了 log_truncated", m)
        check(m.get("log_dropped_lines") == 15,
              "写明了丢了 15 行 (20 行留 5 行)", m.get("log_dropped_lines"))
        check(m.get("log_kept_lines") == 5, "写明了保留 5 行", m.get("log_kept_lines"))
        got = f.get("log") or ""
        check("第 20 行" in got, "最新的一行还在")
        check("第 1 行" not in got, "最早的一行被挤掉了 (上限确实在起作用)")
    finally:
        R.LOG_MAX_LINES = saved


async def main():
    await case1()
    await case2()
    await case3()
    await case4()
    await case5()
    print("\n" + ("全部通过" if not FAILS else f"有失败项 ({len(FAILS)})"))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
