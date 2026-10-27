"""回归测试: USB 设备数要**剔掉 root hub** 才是真设备数 (§6.5)。

起因: `lsusb` 每次都会把 root hub 也列出来 —— 实测本板 6 行输出里 4 行是
root hub:

    Bus 004 Device 003: ID 10c4:0005 Silicon Labs USB OPTICAL MOUSE   ← 真设备
    Bus 004 Device 001: ID 1d6b:0001 Linux Foundation 1.1 root hub    ← 根 Hub
    Bus 003 Device 001: ID 1d6b:0002 Linux Foundation 2.0 root hub    ← 根 Hub
    Bus 002 Device 001: ID 1d6b:0003 Linux Foundation 3.0 root hub    ← 根 Hub
    Bus 001 Device 003: ID 2a7a:8a57  CASUE USB KB                    ← 真设备
    Bus 001 Device 001: ID 1d6b:0002 Linux Foundation 2.0 root hub    ← 根 Hub

不剔掉的话 `usb_count` 恒 ≥ 2 → 「未检测到任何 USB 设备 → failed」那道闸门
**永远不触发** → 空 USB 口会接着走「没有可写的 USB 块设备 → 跳过读写 →
**passed**」, 也就是**空口判通过**。§9.3 把这种「静默的通过」列成最危险的
失效模式: 明确的失败还能申诉, 静默的通过连申诉的由头都没有。

这个测试验四件事:
    ① 只有 root hub → 判 **failed** (空口不能判通过)
    ② 有真设备 → 设备数只数真设备, root hub 不算
    ③ root hub 数单列一个指标 (排查时能看出 lsusb 到底列了几行)
    ④ 空输出 (lsusb 一行都没吐) → 也是 failed

不跑真 lsusb、不碰硬件 —— 输出都是喂进去的假文本。
跑法: .venv/bin/python -m tests.test_usb_root_hub
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.tests import usb as U

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


# 本板 lsusb 的**真实**输出 (2026-10-27 实测) —— 6 行里 4 行是 root hub
REAL_LSUSB = """Bus 004 Device 003: ID 10c4:0005 Silicon Labs USB OPTICAL MOUSE
Bus 004 Device 001: ID 1d6b:0001 Linux Foundation 1.1 root hub
Bus 003 Device 001: ID 1d6b:0002 Linux Foundation 2.0 root hub
Bus 002 Device 001: ID 1d6b:0003 Linux Foundation 3.0 root hub
Bus 001 Device 003: ID 2a7a:8a57  CASUE USB KB
Bus 001 Device 001: ID 1d6b:0002 Linux Foundation 2.0 root hub"""

ONLY_ROOT_HUBS = """Bus 004 Device 001: ID 1d6b:0001 Linux Foundation 1.1 root hub
Bus 003 Device 001: ID 1d6b:0002 Linux Foundation 2.0 root hub
Bus 002 Device 001: ID 1d6b:0003 Linux Foundation 3.0 root hub
Bus 001 Device 001: ID 1d6b:0002 Linux Foundation 2.0 root hub"""


class FakeProc:
    def __init__(self, stdout_text):
        self._out = stdout_text.encode()

    async def communicate(self):
        return self._out, b""


class FakeCtx:
    def __init__(self):
        self.test_config = {"times": 1, "duration_sec": 5}
        self.logs = []

    async def on_log(self, line): self.logs.append(line)

    async def on_progress(self, pct, msg): pass

    async def on_metric(self, k, v): pass

    async def is_cancelled(self): return False


def patch(lsusb_output):
    U.find_tool = lambda name: "/fake/" + name
    U.sudo_hint_if_unavailable = lambda result, label: True
    U._list_usb_block_devices = lambda: []      # 不去找真块设备

    async def fake_exec(*cmd, **kw):
        return FakeProc(lsusb_output)

    U.asyncio.create_subprocess_exec = fake_exec


async def run_usb(lsusb_output):
    patch(lsusb_output)
    ctx = FakeCtx()
    return await U.USBTest().run(ctx)


async def case1():
    print("【1】只有 root hub → 判 **failed** (空口不能判通过)")
    r = await run_usb(ONLY_ROOT_HUBS)
    check(r.status == "failed", "判成 failed", r.status)
    check(r.metrics.get("device_count") == 0,
          "设备数 = 0 (root hub 不算设备)", r.metrics.get("device_count"))
    check(r.metrics.get("root_hub_count") == 4,
          "root hub 数 = 4 (单列指标)", r.metrics.get("root_hub_count"))
    check("USB" in (r.error or ""),
          "错误原因写清了是 USB 设备没检测到", r.error)


async def case2():
    print("\n【2】有真设备 → 设备数只数真设备")
    r = await run_usb(REAL_LSUSB)
    check(r.metrics.get("device_count") == 2,
          "设备数 = 2 (鼠标 + 键盘; 4 个 root hub 不算)", r.metrics.get("device_count"))
    check(r.metrics.get("root_hub_count") == 4,
          "root hub 数 = 4", r.metrics.get("root_hub_count"))
    check(r.status != "failed", "没判 failed", r.status)


async def case3():
    print("\n【3】lsusb 一行都没吐 → 也是 failed")
    r = await run_usb("")
    check(r.status == "failed", "判成 failed", r.status)
    check(r.metrics.get("device_count") == 0, "设备数 = 0",
          r.metrics.get("device_count"))


async def case4():
    print("\n【4】只看 root hub 那几行也能数对 (大小写不敏感)")
    weird = """Bus 001 Device 002: ID 1d6b:0002 Linux Foundation 2.0 ROOT HUB
Bus 001 Device 003: ID 046d:c52b Logitech USB Receiver"""
    r = await run_usb(weird)
    check(r.metrics.get("device_count") == 1,
          "设备数 = 1 (大写的 ROOT HUB 也被剔掉)", r.metrics.get("device_count"))
    check(r.metrics.get("root_hub_count") == 1,
          "root hub 数 = 1", r.metrics.get("root_hub_count"))


async def main():
    await case1()
    await case2()
    await case3()
    await case4()
    print("\n" + ("全部通过" if not FAILS else f"有失败项 ({len(FAILS)})"))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
