"""回归测试: 内存这一项的日志必须先「终端还原」再存。

起因: 产线反馈「内存测试有很多 Random Value, 这个能不能不显示在日志中, 把这
个去掉, 好几页都是这个」。

查下来 memtester 4.3.0 画进度是**原地重绘**的: 它不换行, 而是用退格符(\\b)
把刚写的几个字擦掉重画 —— "Stuck Address" 写的是 "setting 0"/"testing 0" 反
复擦改, "Random Value" 画的是一个 -\\|/ 的转轮, 每转一格就用退格符擦掉上一格。

    原始(一行几万个字符):
        "  Random Value        :  -\\|/-\\|/-\\|/...(几千个)...ok"
    还原(一行):
        "  Random Value        : ok"

原样存进日志, 报告里就是好几页的 "-\\|/" —— 一个信息都没有, 却把真正要看的
部分挤到后面去。

测试用**真机 memtester 4.3.0 的原始输出**(tests/fixtures/ 里那一段, 逐字节)
喂进去, 看日志里还剩什么、失败还判不判得出来。不跑真 memtester、不碰硬件。
跑法: .venv/bin/python -m tests.test_memory_log_demangle
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.tests import memory as mem
from server import report as report_mod

FAILS = []
PASSES = 0

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "fixtures", "memtester_4_3_0_sample.txt")


def check(cond, msg, extra=""):
    global PASSES
    if cond:
        PASSES += 1
        print("    ✓ " + msg)
    else:
        FAILS.append(msg)
        print("    ✗ " + msg + ("  → " + str(extra) if extra else ""))


# ── 按真机字节造输出 ──
#
# 真机(memtester 4.3.0)的三种行, 逐字节照抄:
#   1. 有进度的测试项: 名字 + 冒号 + 11 个空格 + 11 个退格符, 然后
#      "setting   N"(11 字符) + 11 个退格符 + "testing  N"(11 字符) + 11 个
#      退格符, 反复; 收尾是 11 个空格 + 11 个退格符 + "ok"。
#   2. Random Value 那一项: 名字 + 冒号 + 一个转轮("\\|/"), 每转一格就用
#      退格符擦掉上一格。
#   3. 跑得快的测试项(Compare XOR 等): 直接 "名字 : ok", 没有进度。

SPIN = "\\" + "\b" + "|" + "\b" + "/" + "\b" + "-" + "\b"


def _progress_item(name, n=16, verdict="ok"):
    """第 1 种: 带进度重绘的测试项。"""
    s = f"  {name:<20}: " + " " * 11 + "\b" * 11
    for i in range(n):
        s += f"setting {i:>3}" + "\b" * 11
        s += f"testing {i:>3}" + "\b" * 11
    s += " " * 11 + "\b" * 11 + verdict
    return s


def _spinner_item(name, steps=105, verdict="ok"):
    """第 2 种: Random Value 那一项的转轮。"""
    return f"  {name:<20}: " + " \b" + SPIN * steps + " \b" + verdict


def _plain_item(name, verdict="ok"):
    """第 3 种: 没有进度的测试项。"""
    return f"  {name:<20}: {verdict}"


HEADER = [
    "memtester version 4.3.0 (64-bit)",
    "Copyright (C) 2001-2012 Charles Cazabon.",
    "Licensed under the GNU General Public License version 2 (only).",
    "",
    "pagesize is 4096",
    "pagesizemask is 0xfffffffffffff000",
    "want 1024MB (1073741824 bytes)",
    "got  1024MB (1073741824 bytes), trying mlock ...locked.",
    "Loop 1/1:",
]


class FakeStdout:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    async def readline(self):
        if not self._chunks:
            return b""
        return self._chunks.pop(0)


class FakeProc:
    def __init__(self, chunks, rc=0):
        self.stdout = FakeStdout(chunks)
        self.returncode = None
        self._rc = rc
        self.pid = 4242

    async def wait(self):
        self.returncode = self._rc
        return self._rc


class FakeCtx:
    """够用的 TestContext 替身。"""

    def __init__(self):
        self.test_config = {"times": 1, "size_mb": 1024, "timeout_sec": 30}
        self.logs = []

    async def on_log(self, line): self.logs.append(line)

    async def on_progress(self, pct, msg): pass

    async def on_metric(self, k, v): pass

    async def is_cancelled(self): return False


def patch(chunks, rc=0):
    """把内存那一项用到的外部依赖全换成假的。"""
    mem.find_tool = lambda name: "/fake/memtester"
    mem.sudo_hint_if_unavailable = lambda result, label: True
    mem.with_sudo = lambda cmd, timeout_sec=None: ["memtester", "1024M", "1"]

    async def fake_exec(*cmd, **kw):
        return FakeProc(chunks, rc)

    mem.asyncio.create_subprocess_exec = fake_exec


def enc(lines):
    """假的子进程按行吐; 最后一行可以不带换行(模拟被掐断)。"""
    out = []
    for i, l in enumerate(lines):
        s = l if (i == len(lines) - 1 and not l.endswith("\n")) else l + "\n"
        out.append(s.encode())
    return out


async def case1():
    print("【1】真机原始输出 → 日志里只剩干净的一行一项")
    fixture = open(FIXTURE, encoding="utf-8").read().split("\n")
    if fixture and fixture[-1] == "":
        fixture.pop()
    patch(enc(fixture))
    ctx = FakeCtx()
    result = await mem.MemoryTest().run(ctx)
    log = "\n".join(ctx.logs)
    check("-\\|/" not in log, "日志里没有转轮")
    check("\b" not in log, "日志里没有退格符")
    for name in ("Stuck Address", "Random Value", "Compare XOR",
                 "Sequential Increment"):
        check(f"  {name}" in log and f": ok" in log,
              f"「{name}」还原成了一行 ok")
    check(log.count("Stuck Address") == 1, "每个测试项只出现一次")
    check(result.status == "passed", "判成 passed", result.status)
    check(result.metrics.get("round_1_ok_tests") == 4,
          "ok 项数 = 4 (真机那 4 项)", result.metrics.get("round_1_ok_tests"))
    # 真机输出里有 "Loop 1/1:" 这种轮次行 —— 它不是测试项, 别误报「没跑完」
    check("没有结论" not in log, "跑完了的东西不会被误报成「没跑完」")
    check(result.metrics.get("round_1_unfinished") is None,
          "指标里没有未完成项数", result.metrics.get("round_1_unfinished"))


async def case2():
    print("\n【2】几万个字符的转轮 → 日志被压回几百字符")
    # 1024MB 那次, "Random Value" 一行就是几万字符。
    big = _spinner_item("Random Value", steps=6000)
    check(len(big) > 40000, "造出来的原始行确实很长", len(big))
    lines = HEADER + [_progress_item("Stuck Address"), big,
                      _plain_item("Compare XOR")]
    patch(enc(lines))
    ctx = FakeCtx()
    result = await mem.MemoryTest().run(ctx)
    log = "\n".join(ctx.logs)
    check(len(log) < 3000, "日志总长压到几千字符以内", len(log))
    check("  Random Value        : ok" in log, "转轮那一项还原成了一行 ok")
    check(result.metrics.get("round_1_ok_tests") == 3, "ok 项数 = 3",
          result.metrics.get("round_1_ok_tests"))


async def case3():
    print("\n【3】失败照样判得出来 (还原不能把 FAILURE 吃掉)")
    lines = HEADER + [
        _progress_item("Stuck Address"),
        _plain_item("Compare XOR", verdict="FAILURE:  bad address of size 0x00000008"),
    ]
    patch(enc(lines))
    ctx = FakeCtx()
    result = await mem.MemoryTest().run(ctx)
    check(result.status == "failed", "判成 failed", result.status)
    check("FAILURE" in (result.error or ""), "错误原因里有 FAILURE", result.error)
    check("FAILURE" in "\n".join(ctx.logs), "日志里也留着 FAILURE")


async def case4():
    print("\n【4】失败信息被重绘擦掉了 → 靠**原始输出**兜住 (§9.3)")
    # 还原会把 "FAILURE" 擦掉(退格符盖住了), 但原始字节里还在。
    # 这种情况下**绝不能**判 passed —— 静默的通过最害人。
    line = "  Compare XOR         : FAILURE" + "\b" * 8
    lines = HEADER + [_progress_item("Stuck Address"), line]
    patch(enc(lines))
    ctx = FakeCtx()
    result = await mem.MemoryTest().run(ctx)
    check(result.status == "failed", "判成 failed(没被蒙混成 passed)", result.status)
    check("失败" in "\n".join(ctx.logs) or "FAIL" in "\n".join(ctx.logs),
          "日志里说清楚了失败标记被擦掉")


async def case5():
    print("\n【5】跑到一半被挂钟掐了 → 必须披露「没跑完」")
    # 真机被掐断时长这样: 最后一个转轮字符还没被擦掉。
    cut = "  Random Value        :  \b" + SPIN * 300
    lines = HEADER + [_progress_item("Stuck Address"), cut]
    patch(enc(lines))
    ctx = FakeCtx()
    result = await mem.MemoryTest().run(ctx)
    log = "\n".join(ctx.logs)
    check("没有结论" in log, "日志里明写有项没跑完", log[-300:])
    check("Random Value" in log, "点名了是哪一项")
    check(result.metrics.get("round_1_unfinished") == 1,
          "指标里记了未完成项数", result.metrics.get("round_1_unfinished"))
    # 这一项**仍然**按"跑满挂钟"的既定策略判通过 —— 但没跑完必须写在脸上
    check(result.status == "passed", "状态仍按既有策略(跑满挂钟=预期)", result.status)


async def case6():
    print("\n【6】ok 项数不能因为还原而算错")
    names = ["Stuck Address", "Random Value", "Compare XOR", "Compare SUB",
             "Compare MUL", "Compare DIV", "Compare OR", "Compare AND",
             "Sequential Increment", "Solid Bits", "Block Sequential",
             "Checkerboard", "Bit Spread", "Bit Flip", "Walking Ones",
             "Walking Zeroes", "8-bit Writes", "16-bit Writes"]
    lines = list(HEADER)
    for n in names:
        if n == "Random Value":
            lines.append(_spinner_item(n, steps=20))
        elif n in ("Stuck Address", "Block Sequential"):
            lines.append(_progress_item(n))
        else:
            lines.append(_plain_item(n))
    patch(enc(lines))
    ctx = FakeCtx()
    result = await mem.MemoryTest().run(ctx)
    check(result.metrics.get("round_1_ok_tests") == len(names),
          f"ok 项数 = {len(names)}", result.metrics.get("round_1_ok_tests"))


async def case7():
    print("\n【7】报告的日志附录里也不能有转轮")
    lines = HEADER + [_progress_item("Stuck Address"),
                      _spinner_item("Random Value", steps=500)]
    patch(enc(lines))
    ctx = FakeCtx()
    await mem.MemoryTest().run(ctx)
    log = "\n".join(ctx.logs)
    results = [{"test_name": "memory", "status": "passed", "duration_sec": 1.0,
                "metrics": {}, "error": None, "log": log}]
    run = {"id": 1, "sn": "SN", "operator": "op", "status": "passed",
           "started_at": 1, "finished_at": 2,
           "summary": {"total": 1, "passed": 1, "failed": 0, "error": 0, "skipped": 0},
           "config": {"tests": {}}}
    html = report_mod.render_html(run, results, "x.pdf")
    check("-\\|/" not in html, "报告 HTML 里没有转轮")
    check("Random Value        : ok" in html, "报告里是还原后的一行")


async def main():
    await case1()
    await case2()
    await case3()
    await case4()
    await case5()
    await case6()
    await case7()
    print("\n" + ("全部通过" if not FAILS else f"有失败项 ({len(FAILS)})"))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
