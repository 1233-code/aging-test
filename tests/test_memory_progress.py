"""回归测试: 内存这一项的进度条必须**按轮次**走。

起因: 产线反馈「内存测试那个进度条能不能就是按照设置的次数来显示进度情况,
我看一次差不多就是一分钟, 按这个来显示进度情况」。

查出来根因比想象的还干脆 —— **memtester 4.x 根本不吐百分比**。它的进度是
**原地重绘**的("setting 0"/"testing 0" 反复用退格符擦改), 还原成日志之后
每个测试项只剩一行 "名字 : ok", **一个 % 都没有**。旧代码里那段找百分比
的正则因此从来没匹配上过 —— 于是内存项的进度条**全程停在 0%**, 直到最后
一下跳到 100%。工人看着那根条不动, 只觉得工具卡住了。

改法 (server/tests/memory.py):
  · 进度以**轮次**为主轴: 第 i/N 轮 → i/N 完成, 轮边界是精确刻度。
  · 轮内按**挂钟**插值 —— 一轮约一分钟 (实测 60.4s, 因为 1024MB 跑不满
    18 项, 每轮都被 timeout_sec=60 掐断)。
  · 跑完一轮就用那一轮的**实测**时长校准下一轮的分母 (自校准): 换板子、
    内存带宽不同, 分母会自己纠正, 而不是拿编出来的数字硬套。

跑法: .venv/bin/python tests/test_memory_progress.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.tests import memory as mem

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


# ── 假的挂钟 ──
#
# 内存项的进度是**按挂钟**推的, 不控表就没法测 —— 真等 60 秒一轮, 一个测试
# 要跑两分钟。

class FakeClock:
    def __init__(self, t0=1_700_000_000.0):
        self.t = t0

    def advance(self, dt):
        self.t += dt


class FakeTime:
    """顶替 memory.py 里的 `time` —— 它只用到 time.time()。"""

    def __init__(self, clock):
        self._clock = clock

    def time(self):
        return self._clock.t


# ── 假的子进程 ──

class FakeStdout:
    """按「每 N 秒出一行」吐数据。

    没到点就抛 TimeoutError —— 对应真机上 readline() 的一秒超时, 也正是
    内存项推进度条的那个心跳点 (条就是靠它一秒一跳的)。
    """

    def __init__(self, lines, clock, start, every):
        self._lines = list(lines)
        self._clock = clock
        self._start = start
        self._every = every
        self._next_at = every

    async def readline(self):
        if not self._lines:
            return b""                      # EOF → 读取循环结束
        if self._clock.t - self._start < self._next_at:
            self._clock.advance(1.0)         # 这一秒机器没吭声
            raise asyncio.TimeoutError
        self._next_at += self._every
        # 真管道给的是 **bytes** —— 内存项那边直接 line.decode(),
        # 给 str 会炸 (AttributeError → 整项判 error)。
        return self._lines.pop(0).encode()


class FakeProc:
    def __init__(self, lines, clock, every, rc=0):
        self.stdout = FakeStdout(lines, clock, clock.t, every)
        self.returncode = None
        self._rc = rc
        self.pid = 4242

    async def wait(self):
        self.returncode = self._rc
        return self._rc


class FakeCtx:
    """够用的 TestContext 替身 —— 这次要把 on_progress 记下来。"""

    def __init__(self, cfg):
        self.test_config = cfg
        self.logs = []
        self.progress = []        # [(pct, msg), ...] 按上报顺序

    async def on_log(self, line):
        self.logs.append(line)

    async def on_progress(self, pct, msg):
        self.progress.append((pct, msg))

    async def on_metric(self, k, v):
        pass

    async def is_cancelled(self):
        return False


# 真机 memtester 4.3.0 的行 (逐字节照抄的形状): 名字 + 冒号 + 结论
ITEMS = [
    "  Stuck Address       : ok",
    "  Random Value        : ok",
    "  Compare XOR         : ok",
]
HEADER = [
    "memtester version 4.3.0 (64-bit)",
    "pagesize is 4096",
    "Loop 1/1:",
]


def round_lines():
    """一轮的输出 —— 6 行, 配 every=10 就是一轮 60 秒。"""
    return HEADER + ITEMS


_ORIGINALS = {}


def patch(clock, every_per_round):
    """把内存这一项的外部依赖换成假的。every_per_round: 每轮的行间隔秒数。"""
    _ORIGINALS["find_tool"] = mem.find_tool
    _ORIGINALS["sudo_hint"] = mem.sudo_hint_if_unavailable
    _ORIGINALS["with_sudo"] = mem.with_sudo
    _ORIGINALS["time"] = mem.time
    _ORIGINALS["exec"] = mem.asyncio.create_subprocess_exec

    mem.find_tool = lambda name: "/fake/memtester"
    mem.sudo_hint_if_unavailable = lambda result, label: True
    mem.with_sudo = lambda cmd, timeout_sec=None: ["memtester", "1024M", "1"]
    mem.time = FakeTime(clock)

    it = iter(every_per_round)

    async def fake_exec(*cmd, **kw):
        return FakeProc(round_lines(), clock, next(it), rc=0)

    mem.asyncio.create_subprocess_exec = fake_exec


def restore():
    mem.find_tool = _ORIGINALS["find_tool"]
    mem.sudo_hint_if_unavailable = _ORIGINALS["sudo_hint"]
    mem.with_sudo = _ORIGINALS["with_sudo"]
    mem.time = _ORIGINALS["time"]
    mem.asyncio.create_subprocess_exec = _ORIGINALS["exec"]


async def case1():
    print("【1】times=1: 条要一直在走, 不能停在 0%")
    clock = FakeClock()
    patch(clock, [10])                      # 一轮 = 6 行 × 10s = 60s
    ctx = FakeCtx({"times": 1, "size_mb": 1024, "timeout_sec": 60})
    result = await mem.MemoryTest().run(ctx)
    pcts = [p for p, _ in ctx.progress]
    msgs = [m for _, m in ctx.progress]

    check(result.status == "passed", "判成 passed", result.status)
    check(pcts == sorted(pcts), "进度**只增不减**(不许倒退)", pcts)
    check(pcts[-1] == 100, "最后是 100%", pcts[-1])
    # 旧行为是 0% → 100% 两跳; 现在要一秒一跳, 整整一分钟都在走
    check(len(set(pcts)) >= 40, "进度真的在动 (不同取值 >= 40 个)",
          len(set(pcts)))
    check(pcts[0] == 0, "开头是 0%", pcts[0])
    check("第 1/1 轮" in msgs, "文案写着第几轮", msgs[:3])
    # 本轮**真跑完之前**不许显示 100% —— 提前显示"完成"是骗人 (§9.3)
    first_done = next((i for i, m in enumerate(msgs) if "轮完成" in m), None)
    check(first_done is not None, "有轮次完成刻度", msgs[-3:])
    if first_done is not None:
        check(max(pcts[:first_done]) <= 99,
              "轮次真跑完之前不许显示 100%", max(pcts[:first_done]))
        # 跑到 60 秒时, 条应该已经走到 90% 以上 (分母=一轮实测长度)
        check(max(pcts[:first_done]) >= 90, "跑满一轮时条已接近满格",
              max(pcts[:first_done]))


async def case2():
    print("\n【2】times=2: 条按轮次走, 第 1 轮结束正好在一半")
    clock = FakeClock()
    patch(clock, [10, 10])                  # 两轮, 每轮 60s
    ctx = FakeCtx({"times": 2, "size_mb": 1024, "timeout_sec": 60})
    result = await mem.MemoryTest().run(ctx)
    pcts = [p for p, _ in ctx.progress]
    msgs = [m for _, m in ctx.progress]

    check(result.status == "passed", "判成 passed", result.status)
    check(pcts == sorted(pcts), "进度只增不减", pcts)
    check(pcts[-1] == 100, "最后是 100%", pcts[-1])
    # 第 1 轮跑完 → 正好 50%
    i50 = pcts.index(50) if 50 in pcts else -1
    check(i50 >= 0, "有一格正好停在 50% (第 1 轮完成的刻度)", pcts)
    if i50 >= 0:
        check(max(pcts[:i50]) <= 49, "第 1 轮跑完前不许越过 50%",
              max(pcts[:i50]))
        check("第 2/2 轮" in msgs[i50:], "50% 之后进入第 2 轮", msgs[i50:i50 + 3])
    check("第 1/2 轮完成" in msgs, "第 1 轮有完成刻度")
    check("第 2/2 轮完成" in msgs, "第 2 轮有完成刻度")
    check(len(set(pcts)) >= 60, "两轮下来进度一直在动", len(set(pcts)))


async def case3():
    print("\n【3】分母要**自校准**: 第 2 轮用第 1 轮的实测时长")
    # 这一轮只跑 20 秒 (比估的 60 秒快) —— 第 1 轮的分母还不知道, 条会
    # 只走到三分之一就跳到轮次刻度; 但**第 2 轮的分母应当用第 1 轮的实测
    # 20 秒**, 于是第 2 轮能把自己的那一半走满 —— 不用改代码就纠过来了。
    clock = FakeClock()
    patch(clock, [20 / 6, 20 / 6])          # 6 行, 每行 3.33s → 一轮 20s
    ctx = FakeCtx({"times": 2, "size_mb": 1024, "timeout_sec": 60})
    await mem.MemoryTest().run(ctx)
    pcts = [p for p, _ in ctx.progress]

    i50 = pcts.index(50) if 50 in pcts else -1
    check(i50 >= 0, "有 50% 刻度", pcts)
    if i50 >= 0:
        # 第 1 轮只跑到 ~16% 就到了轮次刻度 (分母还是估的 60 秒)
        check(0 < max(pcts[:i50]) < 50,
              "第 1 轮分母还是估的 60 秒 (没实测过), 条没走满",
              max(pcts[:i50]))
        # 第 2 轮分母换成了实测的 20 秒 → 自己的那一半走满
        check(max(pcts[i50:-1]) >= 99,
              "第 2 轮用实测时长当分母, 自己那一半走满了",
              max(pcts[i50:-1]))


async def case4():
    print("\n【4】expected_seconds: 不再是 None, 而是 轮数 × 一轮")
    t = mem.MemoryTest()
    check(t.expected_seconds({"times": 1, "timeout_sec": 60}) == 60.0,
          "times=1 → 60s", t.expected_seconds({"times": 1, "timeout_sec": 60}))
    check(t.expected_seconds({"times": 2, "timeout_sec": 60}) == 120.0,
          "times=2 → 120s", t.expected_seconds({"times": 2, "timeout_sec": 60}))
    check(t.expected_seconds({"times": 3, "timeout_sec": 90}) == 270.0,
          "times=3, timeout=90 → 270s")
    # 这个值只是机器可读估值, 不参与判定 —— 算错了也不该影响测试结果
    check(t.expected_seconds({}) is not None, "没配也照样给个数")


async def case5():
    print("\n【5】失败那一轮不许把条推到 100% 假装做完")
    clock = FakeClock()
    patch(clock, [10])
    # 把 memtester 的输出换成有 FAILURE 的
    orig = FakeProc

    class FailingProc(orig):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.stdout._lines = [
                "  Stuck Address       : ok",
                "  Compare XOR         : FAILURE:  bad address of size 0x8",
            ]

    _ORIGINALS["exec"] = mem.asyncio.create_subprocess_exec

    async def fake_exec(*cmd, **kw):
        return FailingProc(round_lines(), clock, 10, rc=1)

    mem.asyncio.create_subprocess_exec = fake_exec
    ctx = FakeCtx({"times": 1, "size_mb": 1024, "timeout_sec": 60})
    result = await mem.MemoryTest().run(ctx)
    pcts = [p for p, _ in ctx.progress]
    check(result.status == "failed", "判成 failed", result.status)
    # 条到 100% 只表示"这一项跑完了", 判定看 status —— 但失败时**不能**
    # 报出「第 N/N 轮完成」这种像成功收尾的刻度
    msgs = [m for _, m in ctx.progress]
    check("轮完成" not in " ".join(msgs), "失败时没有「轮完成」的刻度", msgs)


async def main():
    try:
        await case1()
        await case2()
        await case3()
        await case4()
        await case5()
    finally:
        restore()
    print("\n" + ("全部通过" if not FAILS else f"有失败项 ({len(FAILS)})"))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
