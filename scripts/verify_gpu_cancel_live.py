"""真机验证: 按「停止」之后, 那个 root 的全屏 glmark2 到底有没有消失。

背景 (2026-10-26): glmark2 是 §11.2 要求**提权起的 root 进程**, 而服务是普通
用户 —— 非 root 对 root 发信号必然 EPERM。所以「工人按了停止」之后, 服务方
**自己**收不掉那个全屏窗口, 以前只能等命令里的 `timeout` 到点收尾, 窗口一直
占着屏幕 (工人看着像工具卡死)。

现在起进程时用 `start_new_session=True` 把这一组圈进我们自己新建的进程组,
收尾时以 root 身份 (`sudo -n kill -TERM/-KILL -<pgid>`) 整组收掉。

本脚本在**真机**上跑一遍, 断言:
  ① 取消前: 那一组里确实有一个 **root** 的 glmark2 活着 (全屏窗口占着屏幕);
  ② 取消后: 那一组里**一个活进程都不剩** (窗口已经消失);
  ③ 这一项判 `stopped` (主动中止, 不算失败);
  ④ 提示语说明窗口已被收掉, 不用再让工人按 Esc。

⚠️ 这个脚本会**真的**占住屏幕 (全屏渲染), 也会真的执行 `sudo -n kill`。
   跑法: .venv/bin/python scripts/verify_gpu_cancel_live.py
"""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.tests import gpu  # noqa: E402

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


class LiveCtx:
    """真的 ctx, 只是在 N 秒后开始「取消」。"""

    def __init__(self, cfg, cancel_after_sec):
        self.test_config = cfg
        self.config = {}
        self.cancel_after_sec = cancel_after_sec
        self.t0 = time.time()
        self.logs = []

    async def on_log(self, line):
        self.logs.append(line)
        print("      " + line, flush=True)

    async def on_progress(self, pct, msg):
        pass

    async def on_metric(self, k, v):
        pass

    async def is_cancelled(self):
        return (time.time() - self.t0) > self.cancel_after_sec


async def main():
    # 每场景 8 秒 → 取消前至少跑得起来几个场景, 但整轮还远没跑完
    cfg = {"scene_sec": 8, "rounds": 1, "timeout_sec": 240}
    cancel_after = float(sys.argv[1]) if len(sys.argv) > 1 else 12.0

    print(f"【真机验证】跑到 {cancel_after:g}s 模拟工人按「停止」")
    ctx = LiveCtx(cfg, cancel_after)
    inst = gpu.GPUTest()
    res = await inst.run(ctx)

    print("\n════ 判定 ════")
    check(res.status == "stopped", "这一项判 stopped (主动中止, 不算失败)",
          (res.status, res.error))
    check("整组收掉" in (res.error or ""),
          "提示语说全屏进程已被收掉 (不用再按 Esc)", res.error)

    pgid = inst.pgid
    print(f"    (这一组的 pgid = {pgid})")
    left = gpu._proc_group_members(pgid) if pgid else []
    check(not left, "取消后那一组里一个活进程都不剩 (全屏窗口已消失)",
          [(p, c, s) for p, c, s, _ in left])
    check(not any(c.startswith("glmark2") for _p, c, _s, _cl in left),
          "root 的 glmark2 本体确实没了")

    print("\n" + ("全部通过" if not FAILS else f"有失败项 ({len(FAILS)})"))
    for f in FAILS:
        print("  ✗ " + f)
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
