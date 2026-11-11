"""回归测试: TF 卡判「不合格」时, **必须带上原因**(2026-11-11 查出来的)。

起因:
    `server/tests/tfcard.py` 里, 读速度低于门槛时的处理是:
        if read_mbps < min_read:
            await ctx.on_log(f"[TF] ❌ 读速度 {read_mbps:.1f} < {min_read} MB/s")
            all_passed = False
    —— 只把 `all_passed` 拉下来, **一个字都没写进 `result.error`**。
    而这一项最后是这么收尾的:
        elif result.status == "pending":
            result.status = "failed"      # ← error 还是空的
    ⇒ 报告里这一项是「failed」, **错误栏是空的** ⇒ 工人拿到报告只知道
    「TF 卡不合格」, 不知道差在哪儿(读速度不够? 卡接触不良? 卡是坏的?)
    ⇒ 只能把卡拔下来重插重测一遍碰运气, 或者干脆把好卡误当坏卡退回去
    ⇒ **冤枉好板子 / 放过坏卡**。

这个测试守三件事:
  ① 读速度不够 ⇒ 判 failed **而且 error 里说清楚是哪一轮、差多少**;
  ② 多轮里有一轮不达标 ⇒ error 里**点名是第几轮**(不能笼统一句「不通过」);
  ③ 达标 ⇒ 还是 passed, 且**不许**有 error(别把好的也写上失败理由)。

不碰真硬件: 卡、挂载、fio、清理全是假的; 不写生产库、不写 reports/pdf/。
跑法: .venv/bin/python -m tests.test_tfcard_fail_reason
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.tests import tfcard as F                # noqa: E402
from server.tests import _tools as T                # noqa: E402

FAILS = []
PASSES = 0

MOUNT = "/media/User/SDCARD"          # 假的卡挂载点


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


class FakeCtx:
    def __init__(self, test_config=None):
        self.config = {}
        self.test_config = test_config or {}
        self.logs = []
        self.progress = []

    async def on_log(self, line): self.logs.append(line)

    async def on_progress(self, pct, msg): self.progress.append((pct, msg))

    async def on_metric(self, k, v): pass

    async def is_cancelled(self): return False


class Patch:
    """把模块里的几个名字换掉, 退出时还原。"""

    def __init__(self, mod, **kw):
        self.mod = mod
        self.kw = kw
        self.saved = {}

    def __enter__(self):
        for k, v in self.kw.items():
            self.saved[k] = getattr(self.mod, k)
            setattr(self.mod, k, v)
        return self

    def __exit__(self, *exc):
        for k, v in self.saved.items():
            setattr(self.mod, k, v)
        return False


class FakeRunCmd:
    """顶掉 _tools.run_cmd —— 记录命令, 不真的跑。"""

    def __init__(self, rc=0):
        self.cmds = []
        self.rc = rc

    async def __call__(self, cmd, timeout=5.0, input_data=None):
        self.cmds.append(list(cmd))
        return self.rc, "", ""


async def _fake_wait_for_dev(dev, timeout_sec=3.0):
    return True


async def run_tf_item(read_mbps, times=1, min_read=10.0):
    """跑一次 TF 卡项: 挂载层和 fio 层都是假的。

    `read_mbps` 可以是一个数(每轮都一样), 也可以是一个列表(每轮不同)。
    """
    mi = F._MountInfo(MOUNT, None, "existing")

    async def _ensure(ctx, disk, dev):
        return mi

    fake = FakeRunCmd()
    seq = (read_mbps if isinstance(read_mbps, (list, tuple))
           else [read_mbps] * times)
    state = {"i": 0}

    async def fake_fio(test_file, size_mb, ctx, duration_sec,
                       on_metric=None, on_progress=None):
        v = seq[min(state["i"], len(seq) - 1)]
        state["i"] += 1
        return {"read_mbps": v, "write_mbps": 30.0}

    with Patch(F, _ensure_mounted=_ensure, _wait_for_dev=_fake_wait_for_dev,
               _run_fio_test=fake_fio,
               _card_disks=lambda root_disk=None: ["mmcblk1"],
               sudo_hint_if_unavailable=lambda result, name: True):
        with Patch(T, run_cmd=fake):
            ctx = FakeCtx({"times": times, "duration_sec": 10,
                           "min_read_mbps": min_read})
            res = await F.TFCardTest().run(ctx)
            return res, ctx


# ══════════════════════════════════════════════════════════════════════
section("【1】读速度不够 ⇒ 判 failed, 而且**说清楚差多少**")

res, ctx = asyncio.run(run_tf_item(3.0, times=1, min_read=10.0))
check(res.status == "failed", "读速度不够 → 判 failed", res.status)
check(bool((res.error or "").strip()),
      "错误栏**不是空的** —— 报告里要能看出为什么不合格", repr(res.error))
check("3.0" in (res.error or "") and "10.0" in (res.error or ""),
      "错误栏里带上了**实际速度**和**门槛**(工人一眼能看出差多少)",
      res.error)
check("第 1 轮" in (res.error or ""),
      "错误栏里点名是**第几轮**", res.error)

section("【2】多轮里有一轮不达标 ⇒ 点名是哪一轮")

res, ctx = asyncio.run(run_tf_item([50.0, 2.0], times=2, min_read=10.0))
check(res.status == "failed", "第二轮不达标 → 判 failed", res.status)
check("第 2 轮" in (res.error or ""),
      "错误栏点名**第 2 轮**(不是笼统一句「不通过」)", res.error)
check("第 1 轮" not in (res.error or ""),
      "达标的那一轮**不用**写进失败理由(免得看着像它也坏了)",
      res.error)

section("【3】达标 ⇒ 还是 passed, 且不许有 error")

res, ctx = asyncio.run(run_tf_item(50.0, times=1, min_read=10.0))
check(res.status == "passed", "读速度够 → 判 passed", res.status)
check(not (res.error or "").strip(),
      "通过的项**不带**失败理由", repr(res.error))

print("\n" + "─" * 60)
if FAILS:
    print("✗ %d 项失败 / 共 %d 项" % (len(FAILS), PASSES + len(FAILS)))
    for f in FAILS:
        print("   · " + f)
    sys.exit(1)
print("✓ 全部通过 (%d 项)" % PASSES)
