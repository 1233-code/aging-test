"""回归测试: 「TF 卡没插 / 网线没插」的提示**只等 30 秒**(2026-11-09 工人要求)。

**要验的三件事**
  ① 提示自己声明 30 秒(`operator_prompt()` 返回的 dict 带 `wait_sec`);
  ② runner **认**这个值 —— 30 秒内没人操作就**直接跳过**, 下一项接着跑,
     这一项仍然是 skipped(没测 ⇒ 不计入通过率, §9.2);
  ③ 别的项**不受影响** —— 没声明 `wait_sec` 的提示仍走全局
     `flow.operator_wait_sec`(默认 300s); 而且**提示自己声明的值压过全局**
     (不是取两者的较小值)。

只把 Database / Config 换成内存里的假实现, 不碰真库、不碰真硬件。
跑法: ./.venv/bin/python tests/test_operator_wait_short.py
"""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import runner as R
from server.tests.base import TestCase, TestResult, TEST_REGISTRY
from server.tests.tfcard import TFCardTest
from server.tests.ethernet import EthTest

EVENTS = []


# ── 假实现(照抄 tests/test_operator_pause.py 的那套) ─────────────────
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


def _set_flow_cfg(seconds):
    """把 runner 看到的 flow 配置换成我们想要的(别的键照旧给空)。

    ⚠️ runner 读的是 `Config.get("flow", {})` —— 拿到的**整个 flow 字典**,
      然后自己从里面掏 `operator_wait_sec`。所以这里必须把
      `operator_wait_sec` 放进**这个字典**里。
      (之前写成了「只有当 key 正好是 flow 时才返回那一个键」的样子 ⇒
      `Config.get("flow", {})` 拿到的是空的 `{}` ⇒ 全局**永远**是默认
      300s ⇒ 「提示自己说的压过全局」那条根本没被真的测到 ——
      变异测试里「与全局取较小值」那个变异当时就是绿的。)
    """
    flow = {"operator_wait_sec": seconds}
    R.Config.get = staticmethod(
        lambda key, default=None: flow if key == "flow" else default
    )


def _wait_sec_of(prompt):
    """提示里声明的等待秒数; 没声明/写坏了 → None。

    ⚠️ **不许在这里抛异常** —— 抛了就变成「测试自己崩了」, 变异测试那边
    只认「被断言咬住的红」, 崩掉不算数。
    """
    if not prompt:
        return None
    try:
        return float(prompt.get("wait_sec"))
    except (TypeError, ValueError):
        return None


# ── ① 提示自己声明 30 秒 ────────────────────────────────────────────
def case_prompts_carry_30s():
    print("【1】TF 卡 / 网线 的提示必须声明「只等 30 秒」")
    ok = True

    tf = TFCardTest().operator_prompt(
        TestResult(status="skipped", metrics={"no_card": True})
    )
    print(f"    TF 卡: wait_sec={tf and tf.get('wait_sec')!r}")
    if _wait_sec_of(tf) != 30.0:
        print("    ✗ TF 卡的提示没有声明 30 秒")
        ok = False
    elif "30" not in (tf.get("text") or ""):
        print("    ✗ TF 卡的提示文案里没告诉工人「30 秒」")
        ok = False
    else:
        print("    ✓ TF 卡: 只等 30 秒, 文案里也写了")

    eth = EthTest().operator_prompt(
        TestResult(status="skipped", metrics={"skipped_reason": "unplugged"})
    )
    print(f"    网线: wait_sec={eth and eth.get('wait_sec')!r}")
    if _wait_sec_of(eth) != 30.0:
        print("    ✗ 网线的提示没有声明 30 秒")
        ok = False
    elif "30" not in (eth.get("text") or ""):
        print("    ✗ 网线的提示文案里没告诉工人「30 秒」")
        ok = False
    else:
        print("    ✓ 网线: 只等 30 秒, 文案里也写了")
    return ok


# ── ② 不该等的情况一个都不许弹框 ───────────────────────────────────
def case_no_prompt_for_other_reasons():
    print("\n【2】「料没到位」以外的情况不许等(TF 挂载失败 / 工位没 DHCP …)")
    ok = True
    cases = [
        ("TF 卡挂载失败(error)", TFCardTest().operator_prompt(
            TestResult(status="error", error="挂载失败", metrics={"mount_failed": True}))),
        ("TF 卡正常通过", TFCardTest().operator_prompt(
            TestResult(status="passed"))),
        ("网口 no_ip(工位没 DHCP)", EthTest().operator_prompt(
            TestResult(status="skipped", metrics={"skipped_reason": "no_ip"}))),
        ("网口 no_egress(上游坏了)", EthTest().operator_prompt(
            TestResult(status="skipped", metrics={"skipped_reason": "no_egress"}))),
    ]
    for label, p in cases:
        print(f"    {label}: prompt={'有' if p else '无'}")
        if p is not None:
            print(f"    ✗ {label} 不该弹框")
            ok = False
    if ok:
        print("    ✓ 这几种都不弹框, 直接往下走")
    return ok


# ── ③ runner 认提示自己的等待上限; 超时 = 直接跳过, 下一项接着跑 ──
def _mk_asking_item(name, prompt_extra):
    """造一个「没就位」的假项: 每次跑都要求操作员。"""
    class T(TestCase):
        pass
    T.name, T.display_name = name, name

    async def run(self, ctx):
        EVENTS.append(f"run:{name}")
        r = TestResult(status="skipped", error="没就位")
        r.metrics["ask"] = True
        return r

    def operator_prompt(self, result):
        p = {"text": f"{name} 没就位"}
        p.update(prompt_extra)
        return p

    T.run, T.operator_prompt = run, operator_prompt
    return T


async def case_runner_honours_wait_sec():
    print("\n【3】提示声明 0.4 秒 → 0.4 秒后自动跳过, 下一项接着跑")
    ok = True
    EVENTS.clear()
    TEST_REGISTRY["asker"] = _mk_asking_item("asker", {"wait_sec": 0.4})
    TEST_REGISTRY["after"] = _mk_asking_item("after", {})   # 这一项不弹框
    # 「after」不弹框 ⇒ 把它改成正常项
    class T2(TestCase):
        pass
    T2.name, T2.display_name = "after", "after"

    async def run2(self, ctx):
        EVENTS.append("run:after")
        return TestResult(status="passed")

    T2.run = run2
    T2.operator_prompt = lambda self, result: None
    TEST_REGISTRY["after"] = T2

    _set_flow_cfg(300)          # 全局 300 秒 —— 提示自己的 0.4 秒必须压过它
    t0 = time.monotonic()
    task = asyncio.create_task(R.TestRunner()._run_tests(
        [(n, TEST_REGISTRY[n]) for n in ("asker", "after")],
        {"tests": {}, "project_root": ""}, write_to_parent=False,
    ))
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=5.0)
    except asyncio.TimeoutError:
        task.cancel()
        print("    ✗ 卡死不动了")
        return False
    elapsed = time.monotonic() - t0
    print(f"    用时 {elapsed:.2f}s · 事件: {' | '.join(EVENTS)}")
    if elapsed > 3.0:
        print(f"    ✗ 等了 {elapsed:.1f}s —— 没认提示自己的等待上限")
        ok = False
    elif elapsed < 0.3:
        print(f"    ✗ 只等了 {elapsed:.2f}s 就放行了 —— 超时值被截断了"
              f"(历史上 int(0.5) 被截成 0 的那个坑)")
        ok = False
    if "run:after" not in EVENTS:
        print("    ✗ 超时之后下一项没接着跑")
        ok = False
    else:
        print("    ✓ 超时后自动往下走了")
    return ok


async def case_global_still_applies():
    print("\n【4】没声明 wait_sec 的提示仍走全局(300s, 不许被 30 秒牵连)")
    ok = True
    rn = R.TestRunner()
    _set_flow_cfg(300)
    t0 = time.monotonic()
    task = asyncio.create_task(rn._wait_for_operator("asker", {"text": "没就位"}, 1))
    # 1.2 秒后它**应该还在等**(全局是 300 秒)
    await asyncio.sleep(1.2)
    still = not task.done()
    print(f"    1.2s 后还在等? {still}")
    if not still:
        print("    ✗ 没过 30 秒就自己放行了 —— 全局值被吃掉了")
        ok = False
    else:
        print("    ✓ 仍在等(全局 300s 生效)")
    # 收尾: 让它以「跳过」结束
    rn.answer_operator("skip")
    try:
        decision = await asyncio.wait_for(task, timeout=2.0)
    except asyncio.TimeoutError:
        decision = None
    print(f"    决定: {decision!r}")
    if decision != "skip":
        print("    ✗ 没收到操作员的决定")
        ok = False
    return ok


async def case_own_wait_beats_global():
    print("\n【5】提示自己声明的值**压过**全局(不是取两者较小值)")
    ok = True
    rn = R.TestRunner()
    _set_flow_cfg(0.4)          # 全局只给 0.4 秒
    task = asyncio.create_task(
        rn._wait_for_operator("asker", {"text": "没就位", "wait_sec": 30.0}, 1)
    )
    await asyncio.sleep(1.2)
    still = not task.done()
    print(f"    1.2s 后还在等? {still} (提示说的是 30 秒)")
    if not still:
        print("    ✗ 用了全局的 0.4 秒 —— 提示自己说的 30 秒被吃掉了")
        ok = False
    else:
        print("    ✓ 用的是提示自己的 30 秒")
    rn.answer_operator("skip")
    try:
        await asyncio.wait_for(task, timeout=2.0)
    except asyncio.TimeoutError:
        pass
    return ok


async def case_bad_wait_sec_falls_back():
    print("\n【6】wait_sec 是 0 / 负数 / 乱写 → 退回全局, 不许变成「不等」")
    ok = True
    for bad in (0, -5, "abc", None):
        rn = R.TestRunner()
        _set_flow_cfg(300)
        task = asyncio.create_task(
            rn._wait_for_operator("asker", {"text": "x", "wait_sec": bad}, 1)
        )
        await asyncio.sleep(0.6)
        still = not task.done()
        rn.answer_operator("skip")
        try:
            await asyncio.wait_for(task, timeout=2.0)
        except asyncio.TimeoutError:
            pass
        print(f"    wait_sec={bad!r} → 0.6s 后还在等? {still}")
        if not still:
            print(f"    ✗ wait_sec={bad!r} 让它直接放行了")
            ok = False
    if ok:
        print("    ✓ 都不合法时退回全局(300s), 没有变成「不等」")
    return ok


async def main():
    ok = True
    ok &= case_prompts_carry_30s()
    ok &= case_no_prompt_for_other_reasons()
    ok &= await case_runner_honours_wait_sec()
    ok &= await case_global_still_applies()
    ok &= await case_own_wait_beats_global()
    ok &= await case_bad_wait_sec_falls_back()
    print("\n" + ("全部通过" if ok else "有失败项"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
