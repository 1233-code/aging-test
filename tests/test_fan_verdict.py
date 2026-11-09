"""回归测试: 风扇项的判定必须**分清「风扇不合格」和「IO 层有问题」**。

起因 (2026-10-29, 产线口径拍板):

  **风扇是外接负载 —— 风扇转不转、坏不坏, 都不会改变 GPIO 的读回值。**
  所以 IO 层读出异常 (回读不一致 / 读失败 / 读回不稳) 证明的是
  **这条 IO 链路**有问题, **证明不了风扇不合格**。

  以前这些一律判 `failed` —— 那是**冤枉被测设备**: 好板子因为「IO 抖了一下」
  被送去修。现在按 §9.3 分开:

  | 情形                                | 判        | 理由                              |
  |-------------------------------------|-----------|-----------------------------------|
  | 写后回读不一致 / 读回不稳定          | `error`   | IO 层不可信 → 这一项**没测成**    |
  | 写失败 (os.open/os.write 被拒)       | `error`   | 同上 (原来就对)                   |
  | 驱动模式全部闭环                    | `passed`  | 控制链闭环                        |
  | **只读模式 (fan.drive=false) 采样稳** | `skipped` | 风扇**没被拉起过** → 能不能启停**没测** |

  只读模式原来判 `passed` —— 那是 §9.3 说的「静默的『通过』」: 对一件
  **根本没测**的事给了结论。改判 `skipped` 后它不计入 `total`(§9.2),
  报告里作为「未测项」逐一列出, 质检看得见这一项没测。

这个测试验:
    ① 驱动模式下回读不一致 / 读失败 → `error`, **不是** `failed`
    ② 只读模式采样稳 → `skipped`, **不是** `passed`
    ③ 只读模式采样不稳 → `error`, **不是** `failed`
    ④ 驱动模式全闭环 → `passed`, 但**不再**声称「虚焊/不转被覆盖到了」
       (docs §6.8 的覆盖缺口: 驱动只认电平变化, 不验证风扇真的转了)
    ⑤ 只读模式记 skipped 之后, 轮级口径的连带效果 (§9.2: skipped 不计入 total)

不碰真 GPIO: `_read_level` / `_write_level` 都被换成假的, 节点路径指向一个
临时文件(只为让 `os.path.exists` 为真)。

跑法: .venv/bin/python -m tests.test_fan_verdict
"""
import asyncio
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.tests import fan as F
from server.db import decide_run_status

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
        self.logs = []
        self.progress = []
        self._t0 = time.monotonic()

    async def on_log(self, line):
        self.logs.append(line)

    async def on_progress(self, pct, msg):
        self.progress.append((pct, msg))

    async def on_metric(self, k, v):
        pass

    async def is_cancelled(self):
        return False


class FakeGpio:
    """假的 GPIO。

    `read_script`: 逐次回读的剧本 (None = 这次读失败)。剧本用完就
    「写什么读回什么」—— 真硬件上就是这样的, 假实现要是固定返回 0,
    「拉高」那一相就永远读回不一致, 那是假实现自己造的失败。

    ⚠️ **剧本的第 1 次读是「起始电平」那一次** —— `run()` 进来就
    `_read_level` 一次拿起始电平, 它也会消费剧本的第 1 格。
    """

    def __init__(self, read_script=None, write_ok=True):
        self.level = 0
        self.read_script = list(read_script or [])
        self.write_ok = write_ok
        self.writes = []

    def read(self, path):
        if self.read_script:
            return self.read_script.pop(0)
        return self.level

    def write(self, path, level):
        if not self.write_ok:
            return False
        self.writes.append(level)
        self.level = level
        return True


def patch_gpio(gpio):
    F._read_level = gpio.read
    F._write_level = gpio.write


async def run_fan(gpio, drive=True, cycles=1, settle=0.01, gap=0.01):
    patch_gpio(gpio)
    with tempfile.NamedTemporaryFile(prefix="fake_pi5_", delete=True) as tf:
        path = tf.name
        ctx = FakeCtx(
            config={"fan": {
                "procfs_path": path,
                "drive": drive,
                "active_level": "high",
                "drive_settle_sec": settle,
                "sample_gap_sec": gap,
            }},
            test_config={"cycles": cycles},
        )
        r = await F.FanTest().run(ctx)
    return r, ctx


# ── ① 驱动模式: IO 层异常 → error, 不是 failed ─────────────────

async def case1():
    print("【1】驱动模式: 写后回读不一致 → error (不是 failed)")
    # 剧本: 第 1 格 = 起始电平(0), 第 2 格 = 第一相的回读(写成 1 → 失配)
    gpio = FakeGpio(read_script=[0, 1])
    r, ctx = await run_fan(gpio)
    check(r.status == "error", "回读不一致 → error", r.status)
    check(r.status != "failed", "**不是** failed (不冤枉被测设备)", r.status)
    check("外接" in (r.error or ""),
          "错误信息写明「风扇是外接负载」这条论据", r.error)
    check("没测成" in (r.error or ""),
          "错误信息明说「这一项没测成」", r.error)
    check(any("✗" in l for l in ctx.logs),
          "日志用的是 error 的标记 (✗), 不是 failed 的 ❌", ctx.logs[-2:])
    # 步骤明细仍然留着 (要能查是哪一步失配)
    steps = r.metrics.get("steps") or []
    bad = [s for s in steps if not s["ok"]]
    check(len(bad) == 1 and bad[0]["read_back"] == 1,
          "metrics.steps 里留着失配那一步的明细", steps)


async def case2():
    print("\n【2】驱动模式: 跑到一半 IO 层挂了 (读失败) → error")
    # 剧本: 起始电平 0, 第一相回读 0 (正常), 第二相回读失败
    gpio = FakeGpio(read_script=[0, 0, None])
    r, ctx = await run_fan(gpio)
    check(r.status == "error", "中途读失败 → error", r.status)
    check(r.status != "failed", "不是 failed", r.status)
    bad = [s for s in (r.metrics.get("steps") or []) if s["read_back"] is None]
    check(len(bad) == 1, "metrics.steps 里标出了读失败那一步", r.metrics.get("steps"))


async def case3():
    print("\n【3】驱动模式: 写失败 → error (老口径, 别改坏)")
    gpio = FakeGpio(write_ok=False)
    r, ctx = await run_fan(gpio)
    check(r.status == "error", "写失败 → error", r.status)
    check("不是「风扇坏了」" in (r.error or ""),
          "错误信息仍明说「不是风扇坏了」", r.error)


async def case4():
    print("\n【4】驱动模式: 全部闭环 → passed, 但**不再**声称覆盖了虚焊/不转")
    gpio = FakeGpio()
    r, ctx = await run_fan(gpio, cycles=2)
    check(r.status == "passed", "4 步全闭环 → passed", r.status)
    joined = "\n".join(ctx.logs)
    check("虚焊/不转这条被本项覆盖到了" not in joined,
          "不再声称「虚焊/不转被本项覆盖到了」", joined[-300:])
    check("覆盖不到" in joined or "不验证风扇真的转" in joined,
          "改口成「本项验不了风扇有没有真的转」", joined[-300:])
    # 还原电平这件事不能因为改了判定就丢
    check(r.metrics.get("restored_level") == 0, "结束时把电平还原成起始值",
          r.metrics.get("restored_level"))


# ── ② 只读模式: 没测风扇转动 → skipped ─────────────────────────

async def case5():
    print("\n【5】只读模式 (fan.drive=false): 采样稳 → skipped (不是 passed)")
    gpio = FakeGpio()
    r, ctx = await run_fan(gpio, drive=False, cycles=2)
    check(r.status == "skipped", "只读模式 → skipped", r.status)
    check(r.status != "passed", "**不是** passed (没测过的事不给结论)", r.status)
    check((r.metrics or {}).get("skipped_reason") == "readonly_no_drive",
          "指标里记了跳过原因", (r.metrics or {}).get("skipped_reason"))
    check("没被拉起过" in (r.error or "") or "没被拉起过" in "\n".join(ctx.logs),
          "说明白「风扇没被拉起过」", r.error)
    # 只读模式**不许**写电平
    check(gpio.writes == [], "只读模式一个电平都没写", gpio.writes)


async def case6():
    print("\n【6】只读模式: 采样不稳 → error (不是 failed)")
    # 3 次采样: 0 / 读失败 / 0 (第 1 格是起始电平, 不算采样)
    gpio = FakeGpio(read_script=[0, 0, None, 0])
    r, ctx = await run_fan(gpio, drive=False, cycles=3)
    check(r.status == "error", "读回不稳 → error", r.status)
    check(r.status != "failed", "不是 failed", r.status)
    check("外接" in (r.error or "") or "证明不了风扇不合格" in (r.error or ""),
          "错误信息写明「IO 异常证明不了风扇不合格」", r.error)


async def case7():
    print("\n【7】只读模式: 全读失败 → error (老口径, 别改坏)")
    # 起始电平读得到, 但两次采样全读失败
    gpio = FakeGpio(read_script=[0, None, None])
    r, ctx = await run_fan(gpio, drive=False, cycles=2)
    check(r.status == "error", "全部读失败 → error", r.status)
    check("IO 路径挂了" in (r.error or ""), "错误信息明说 IO 路径挂了", r.error)

    print("\n【7b】起始电平就读不出来 → error (连只读分支都进不去)")
    gpio = FakeGpio(read_script=[None])
    r, ctx = await run_fan(gpio, drive=False, cycles=2)
    check(r.status == "error", "起始读失败 → error", r.status)
    check("不可用" in (r.error or ""),
          "错误信息明说 IO 路径不可用 (不是乱码)", r.error)


# ── ③ 轮级连带效果 (§9.2) ─────────────────────────────────────

def case8():
    print("\n【8】连带效果: 风扇记 skipped → 不计入 total, 轮级口径不受影响")
    # §9.2 的表: skipped 不计入 total, 也不计入 passed/failed/error
    summary = {"total": 9, "passed": 9, "failed": 0, "error": 0,
               "skipped": 1, "not_run": 0, "planned": 10}
    check(decide_run_status(summary) == "passed",
          "其余 9 项都过 → 轮级判 passed (skipped 不拉低通过率)",
          decide_run_status(summary))
    # 一项都没执行 → 不得判 passed
    all_skip = {"total": 0, "passed": 0, "failed": 0, "error": 0,
                "skipped": 10, "not_run": 0, "planned": 10}
    check(decide_run_status(all_skip) == "skipped",
          "全项 skipped → 轮级判 skipped (不判 passed)",
          decide_run_status(all_skip))


async def main():
    await case1()
    await case2()
    await case3()
    await case4()
    await case5()
    await case6()
    await case7()
    case8()
    print("\n" + ("全部通过" if not FAILS else f"有失败项 ({len(FAILS)})"))
    for f in FAILS:
        print("  ✗ " + f)
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
