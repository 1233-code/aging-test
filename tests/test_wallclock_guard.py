"""回归测试: **时长不许用墙钟算**。

**起因 (2026-11-12 产线反馈)**
    另一台设备上跑老化, 内存项的日志里「时间突然飙升」——
    工人猜是「时间同步」, **猜对了**: 那台板子的钟是 `fake-hwclock` 还原的
    (只在正常关机时存盘 ⇒ 硬复位后能差 42 天), `ntp.service` 一联网就把它
    **往前拨**。而内存项是**第一项** ⇒ 拨钟正好落在内存项里。

    内存项原来拿 `time.time()` 算「已用/预计」⇒ 墙钟往前一跳, 文案里的
    时间**暴涨**; 更糟的是 GNU `timeout` 走的是 `CLOCK_REALTIME` ⇒ 它
    **提前**到点把 memtester 收尾 ⇒ 原来「rc=124 = 跑满挂钟(预期)」
    ⇒ 跑了几秒也算**通过** ⇒ **静默的假通过**(§9.3 里最危险的一种)。

**这条防线守的是什么**
    墙钟会跳, 单调钟不会 ⇒ **凡是「时长」, 一律 `time.monotonic()`**。
    时钟**偏移**本身要报出来, 走 `server/clock.py::Duration`
    (它两个钟都记, 跳了会告诉你跳了多少)。

**为什么用静态扫描而不是跑一遍**
    「时长」是算出来的, 跑一遍看不出来; 而 `time.time()` 出现在**测试项
    代码**里, 就一定是拿墙钟在算时长(落库/展示的时间戳另有出处:
    `server/clock.py::now()`)。

跑法: .venv/bin/python -m tests.test_wallclock_guard
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

FAILS = []
PASSES = 0

# 墙钟调用的各种写法(带空格/不带、`.time()`/`.monotonic()` 之外的)
WALL_CALL = re.compile(r"\btime\s*\.\s*time\s*\(\s*\)")
# 允许的例外: 这些文件就是**时间戳**的出处, 不是「时长」的出处
ALLOWED = {
    os.path.join("server", "clock.py"),      # 时钟的唯一出处
    os.path.join("tests", "test_wallclock_guard.py"),   # 本文件自己
    os.path.join("tests", "test_memory_progress.py"),   # 本文件要测的那条
}


def check(cond, msg, extra=""):
    global PASSES
    if cond:
        PASSES += 1
        print("    ✓ " + msg)
    else:
        FAILS.append(msg)
        print("    ✗ " + msg + ("  → " + str(extra) if extra else ""))


def section(title):
    print("\n" + title)


# ══════════════════════════════════════════════════════════════════════
section("【1】server/ 下不许有直接调 `time.time()` 的地方")
# ══════════════════════════════════════════════════════════════════════
bad = []
for dirpath, dirnames, filenames in os.walk(os.path.join(REPO, "server")):
    dirnames[:] = [d for d in dirnames if d not in ("__pycache__",)]
    for fn in filenames:
        if not fn.endswith(".py"):
            continue
        path = os.path.join(dirpath, fn)
        rel = os.path.relpath(path, REPO)
        if rel in ALLOWED:
            continue
        for i, line in enumerate(open(path, encoding="utf-8"), 1):
            if WALL_CALL.search(line) and line.lstrip().startswith("#") is False:
                # 注释行不算 —— 但「#」之后的说明文字里提到 time.time() 也不算
                code = line.split("#", 1)[0]
                if WALL_CALL.search(code):
                    bad.append(f"{rel}:{i}: {line.strip()}")
check(not bad, "server/ 里没有直接用墙钟算的地方", "\n      ".join(bad))

# ══════════════════════════════════════════════════════════════════════
section("【2】测试项代码必须走单调钟")
# ══════════════════════════════════════════════════════════════════════
bad = []
for fn in sorted(os.listdir(os.path.join(REPO, "server", "tests"))):
    if not fn.endswith(".py"):
        continue
    rel = os.path.join("server", "tests", fn)
    if rel in ALLOWED:
        continue
    path = os.path.join(REPO, "server", "tests", fn)
    for i, line in enumerate(open(path, encoding="utf-8"), 1):
        code = line.split("#", 1)[0]
        if WALL_CALL.search(code):
            bad.append(f"{rel}:{i}: {line.strip()}")
check(not bad, "server/tests/ 里没有直接用墙钟算的地方", "\n      ".join(bad))

# ══════════════════════════════════════════════════════════════════════
section("【3】这些文件里**确实**用的是单调钟(别改回去)")
# ══════════════════════════════════════════════════════════════════════
for rel in ["server/tests/gpu.py", "server/tests/emmc.py", "server/tests/usb.py",
            "server/tests/tfcard.py", "server/tests/ethernet.py",
            "server/tests/wifi.py", "server/tests/_tools.py"]:
    src = open(os.path.join(REPO, rel), encoding="utf-8").read()
    check("time.monotonic()" in src, f"{rel} 用了单调钟")

# ══════════════════════════════════════════════════════════════════════
section("【4】内存项: 时长走 `Duration`(跳钟会记账)")
# ══════════════════════════════════════════════════════════════════════
mem = open(os.path.join(REPO, "server", "tests", "memory.py"), encoding="utf-8").read()
check("from ..clock import Duration" in mem, "内存项引了 `Duration`")
check("item_sw.elapsed" in mem, "整项时长走 `Duration.elapsed`")
check("round_sw.elapsed" in mem, "轮内时长走 `Duration.elapsed`")
check("round_sw.jumped" in mem, "轮内会检查有没有跳钟")
check("wall_clock_jump_sec" in mem, "跳钟的量记进指标里(留痕)")

# ══════════════════════════════════════════════════════════════════════
section("【5】`Duration` 自己: 时长用单调钟, 墙钟跳变单独记账")
# ══════════════════════════════════════════════════════════════════════
from server import clock as clock_mod                  # noqa: E402

d = clock_mod.Duration()
d._w0 -= 3600.0        # 假装墙钟被往前拨了一小时
check(d.elapsed >= 0 and d.elapsed < 5, "时长走单调钟(拨钟不影响)", d.elapsed)
check(d.jumped, "墙钟跳变被认出来了")
check("3600" in d.jump_text() or "跳了" in d.jump_text(),
      "跳钟的文案说清了", d.jump_text())
d2 = clock_mod.Duration()
check(not d2.jumped, "没跳钟时不瞎报")
check(d2.jump_text() == "", "没跳钟时没有多余的话")

print("\n" + ("全部通过" if not FAILS else f"有失败项 ({len(FAILS)})"))
sys.exit(0 if not FAILS else 1)
