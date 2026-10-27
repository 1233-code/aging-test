"""回归测试: 风扇项在「等电平稳定」那段静默期里必须有心跳 (§10.1)。

起因: §10.1 写死「单项测试期间, 每 **≤ 2 秒**至少推 1 帧 progress 或 log」。
风扇项的 `drive_settle_sec`(默认 **5 秒**) 是写完电平到回读之间的等待 ——
风扇要真的转起来, 工人还得**看得见**它转, 所以这 5 秒不能省。

但这段等待原来**一帧都不推**: 屏幕上进度条和日志都不动, 整整 5 秒看着像卡死。
默认 `cycles=2` × 两相 = **4 段 × 5 秒 = 20 秒**全静默。

这个测试验:
    ① 等电平稳定期间**确实有 progress 帧**推出来
    ② 心跳帧的间隔**不超过 §10.1 的 2 秒上限**(用缩短的心跳周期模拟)
    ③ 心跳帧的文案带着「等电平稳定」和已等待秒数 —— 工人看得懂现在是干等
    ④ 轮转本身照常跑完, 判定不受影响 (heartbeat 只是多推几帧, 不改结果)

不碰真 GPIO: `_read_level` / `_write_level` 都被换成假的, 节点路径指向一个
临时文件(只为让 `os.path.exists` 为真)。
跑法: .venv/bin/python -m tests.test_fan_heartbeat
"""
import asyncio
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.tests import fan as F

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


class FakeCtx:
    def __init__(self, config, test_config):
        self.config = config
        self.test_config = test_config
        self.progress = []      # (pct, msg, 到达时刻)
        self.logs = []
        self._t0 = time.monotonic()

    async def on_log(self, line): self.logs.append(line)

    async def on_progress(self, pct, msg):
        self.progress.append((pct, msg, time.monotonic() - self._t0))

    async def on_metric(self, k, v): pass

    async def is_cancelled(self): return False


_GPIO = {"level": 0}


def patch_gpio():
    """把 GPIO 读写换成假的 —— **绝不能碰真节点**。

    读回的值必须跟着写入走: 真硬件上写什么就读回什么, 假实现要是固定
    返回 0, 「拉高」那一相就永远读回不一致, 会被判 failed —— 那是假实现
    自己造的失败, 不是被测代码的失败。
    """
    def _read(path):
        return _GPIO["level"]

    def _write(path, level):
        _GPIO["level"] = level
        return True

    F._read_level = _read
    F._write_level = _write


async def run_fan(settle, cycles=1, heartbeat=0.05):
    patch_gpio()
    with tempfile.NamedTemporaryFile(prefix="fake_pi5_", delete=True) as tf:
        path = tf.name
        F._HEARTBEAT_SEC = heartbeat
        ctx = FakeCtx(
            config={"fan": {
                "procfs_path": path,
                "drive": True,
                "active_level": "high",
                "drive_settle_sec": settle,
                "sample_gap_sec": 0.05,
            }},
            test_config={"cycles": cycles},
        )
        r = await F.FanTest().run(ctx)
    return r, ctx


async def case1():
    print("【1】等电平稳定期间必须推 progress 帧 (不是干等)")
    r, ctx = await run_fan(settle=0.3)
    beats = [p for p in ctx.progress if "等待电平稳定" in p[1]]
    check(len(beats) > 0, "有「等待电平稳定」的心跳帧", len(beats))
    check(r.status == "passed", "轮转照常判通过 (心跳不改判定)", r.status)
    # 两相各等一次 → 两段心跳
    check(len(beats) >= 2, "两个相位各有一段心跳", len(beats))


async def case2():
    print("\n【2】心跳帧的间隔不超过 §10.1 的 2 秒上限")
    # 心跳周期缩到 0.05s, 等 0.5s → 至少 5 帧, 且相邻间隔都远小于 2 秒
    r, ctx = await run_fan(settle=0.5, heartbeat=0.05)
    beats = [p for p in ctx.progress if "等待电平稳定" in p[1]]
    check(len(beats) >= 3, "0.5 秒的等待里推了多帧心跳", len(beats))
    gaps = [beats[i + 1][2] - beats[i][2] for i in range(len(beats) - 1)]
    worst = max(gaps) if gaps else 99
    check(worst <= 2.0, "相邻心跳间隔都 ≤ 2 秒", f"最大 {worst:.2f}s")


async def case3():
    print("\n【3】心跳文案带着已等待秒数 (工人看得懂现在是干等)")
    r, ctx = await run_fan(settle=0.3, heartbeat=0.05)
    beats = [p for p in ctx.progress if "等待电平稳定" in p[1]]
    check(bool(beats), "有心跳帧")
    if beats:
        msg = beats[0][1]
        check("/" in msg, "文案里带「已等/总时长」", msg)
        check("轮" in msg, "文案里带轮次", msg)
        print(f"      文案样例: {msg}")


async def case4():
    print("\n【4】心跳只是多推几帧, 不改最终判定和指标")
    r, ctx = await run_fan(settle=0.3, cycles=2, heartbeat=0.05)
    check(r.status == "passed", "2 轮也判通过", r.status)
    check(len(r.metrics.get("steps") or []) == 4,
          "4 个相位都记了 (2 轮 × 2 相)", len(r.metrics.get("steps") or []))
    check(all(s["ok"] for s in (r.metrics.get("steps") or [])),
          "每个相位写回读都一致")


async def main():
    await case1()
    await case2()
    await case3()
    await case4()
    print("\n" + ("全部通过" if not FAILS else f"有失败项 ({len(FAILS)})"))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
