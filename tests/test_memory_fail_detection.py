"""回归测试: 内存这一项的 FAIL 判定不能被**截断过的**日志骗过去。

起因: 报告要「显示全」日志, 顺着查日志的截断点, 查到内存这一项有个
**真的判错 bug**:

    lines_buf.append(text)
    if len(lines_buf) > 200:
        lines_buf = lines_buf[-200:]      # ← 只留末尾 200 行

而下面判 FAIL / 算 ok 项数, 跑的就是这个被截断的缓冲:

    fail_line = re.search(r"FAIL(?:URE)?\\b", result.log, re.MULTILINE)

于是: memtester **前面**报了 FAILURE, 后面又吐了 200 多行, 那个 FAILURE
行就被挤出去了 —— **内存真坏了, 这一项照样判 passed**。

§9.3 把这种「静默的通过」列成最危险的失效模式: 明确的失败还能申诉, 静默
的通过连申诉的由头都没有(纸面上写着「通过」)。

修法: 判定用的缓冲不再截断(每一轮跑完就扔, 一轮几十行, 内存不担心)。

这个测试用**假的 memtester** 喂一段「FAIL 在第 3 行、后面还有 300 行」的
输出, 看这一项判成什么。不跑真 memtester、不碰硬件。
跑法: .venv/bin/python -m tests.test_memory_fail_detection
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


class FakeStdout:
    def __init__(self, lines):
        self._lines = list(lines)

    async def readline(self):
        if not self._lines:
            return b""
        return (self._lines.pop(0) + "\n").encode()


class FakeProc:
    def __init__(self, lines, rc=0):
        self.stdout = FakeStdout(lines)
        self.returncode = None
        self._rc = rc
        self.pid = 4242

    async def wait(self):
        self.returncode = self._rc
        return self._rc


class FakeCtx:
    """够用的 TestContext 替身。"""

    def __init__(self):
        self.test_config = {"times": 1, "size_mb": 64, "timeout_sec": 30}
        self.logs = []

    async def on_log(self, line): self.logs.append(line)

    async def on_progress(self, pct, msg): pass

    async def on_metric(self, k, v): pass

    async def is_cancelled(self): return False


def memtester_output(fail_at=None, n_ok=5):
    """造一段 memtester 输出。fail_at 是 FAILURE 所在行号 (1 起)。"""
    lines = []
    for i in range(1, n_ok + 1):
        lines.append(f"  Test Name {i:<12}: ok")
    if fail_at is not None:
        lines.insert(fail_at - 1, "setting 3FAILURE at 0x1234")
    return lines


def patch(output, rc=0):
    """把内存那一项用到的外部依赖全换成假的。"""
    mem.find_tool = lambda name: "/fake/memtester"
    mem.sudo_hint_if_unavailable = lambda result, label: True
    mem.with_sudo = lambda cmd, timeout_sec=None: ["memtester", "64M", "1"]

    async def fake_exec(*cmd, **kw):
        return FakeProc(output, rc)

    mem.asyncio.create_subprocess_exec = fake_exec


async def case1():
    print("【1】FAIL 在**前面**、后面还有一大堆行 → 必须判 failed")
    out = memtester_output(fail_at=2, n_ok=5)
    # 后面再灌 300 行 ok —— 旧代码会把这 300 行截到只剩末尾 200 行,
    # 第 2 行那个 FAILURE 就被挤出去了。
    out += [f"  Test Name pad {i:<8}: ok" for i in range(300)]
    patch(out)
    ctx = FakeCtx()
    result = await mem.MemoryTest().run(ctx)
    check(result.status == "failed",
          "判成 failed (内存坏了就是坏了)", result.status)
    check("FAILURE" in (result.error or ""),
          "错误原因里带上了 FAILURE", result.error)


async def case2():
    print("\n【2】FAIL 在**末尾** → 判 failed (这一条旧代码也是对的)")
    out = memtester_output(n_ok=5) + ["setting 3FAILURE at 0x1234"]
    patch(out)
    ctx = FakeCtx()
    result = await mem.MemoryTest().run(ctx)
    check(result.status == "failed", "判成 failed", result.status)


async def case3():
    print("\n【3】全 ok → 判 passed, 且 ok 项数不被截断")
    out = memtester_output(n_ok=40)
    patch(out)
    ctx = FakeCtx()
    result = await mem.MemoryTest().run(ctx)
    check(result.status == "passed", "判成 passed", result.status)
    check(result.metrics.get("round_1_ok_tests") == 40,
          "ok 项数是 40 (不是被截断后的数)",
          result.metrics.get("round_1_ok_tests"))


async def case4():
    print("\n【4】落库的 log 要是**完整**的, 不是末尾 200 行")
    out = memtester_output(n_ok=5) + [f"  Test Name pad {i:<8}: ok" for i in range(300)]
    patch(out)
    ctx = FakeCtx()
    result = await mem.MemoryTest().run(ctx)
    log = result.log or ""
    check("Test Name 1 " in log, "第一行还在 (不是只留尾巴)")
    check(log.count("Test Name") >= 300,
          "300 多行都在", log.count("Test Name"))


async def main():
    await case1()
    await case2()
    await case3()
    await case4()
    print("\n" + ("全部通过" if not FAILS else f"有失败项 ({len(FAILS)})"))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
