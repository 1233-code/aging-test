"""风扇 GPIO —— 读写双向体检 (§6.8)

硬件 (B 类纯 GPIO 通断, **无 tach**):
  厂家在内核镜像里把 PI5 暴露成普通文件 `/proc/rp_gpio/pi5`。
  同目录其他脚 (pc2/pc3/pc4/pc7/pc12/pl8) 是通用 GPIO, 非风扇专用。
  dmesg 里有 `fan_gpio_control: probe of fan_gpio_control failed with error -22`
  (`invalid gpio num -2`) —— 厂家那个风扇驱动**开机就没绑上**, 所以这里是
  直接怼裸引脚。

## ⚠️ 为什么全程用 os.open/os.read/os.write, 不用 Python 的 open()

**实测结论 (2026-10-22, 在这台 A527 上逐条跑出来的)**:

| 操作                                         | 结果      |
|----------------------------------------------|-----------|
| `cat /proc/rp_gpio/pi5` (终端)                | ✅        |
| `echo 0/1 > /proc/rp_gpio/pi5` (终端)          | ✅ 能启停风扇 |
| `os.open(O_RDONLY)` + `os.read(fd, 2..1024)`   | ✅        |
| `os.open(O_WRONLY)` + `os.write(fd, b"0\\n")`  | ✅ 回读一致 |
| 同一 fd 连读两次                                | ✅        |
| 带 `O_CLOEXEC` / 带 `fstat`                    | ✅        |
| `open(p,'rb',buffering=0).read(4)`             | ✅        |
| `open(p,'rb').read(4)`  ← Python **缓冲**读     | 💥 kernel panic |

同一台机器 `cat` 完全正常, 只有 Python 的**缓冲读**会崩 → 说明不是驱动完全
不能用, 而是 `io.BufferedReader` 的访问方式踩到了驱动里的某个边界。已逐条
排除的因素: read 长度 (2~1024 全试过都安全)、同一 fd 二次读、`O_CLOEXEC`、
`fstat`。真正的触发条件还没定位 —— 板子每次崩都 COLD-BOOT, 冷启动会把
ramoops 里的调用栈一起清掉, 拿不到 oops 现场。

所以本文件**一律走裸系统调用**: 每个 fd 只读一次、读完立刻关; 写同理。
这正是上面实测 ✅ 的那条路径。**这是规避, 不是修复** —— 驱动为什么在缓冲
路径上崩仍然未知, 属于出货镜像侧的事, 不该由产线工具来绕一辈子。

## ⚠️ 不要再把结论记反: 「写这个节点会 kernel panic」是错的

早期代码注释里写着「一碰写路径板子就 panic」, 那是**从权限位 0666 推断**出来的,
没有任何实测依据, **已作废**。终端里 `echo 0/1 > /proc/rp_gpio/pi5` 实测正常,
能真正启停风扇。本文件现在**会写这个节点**, 而且每次写完都回读校验。
"""
import asyncio
import os
import time
from typing import List, Optional

from .base import TestCase, TestContext, TestResult, register

_DEFAULT_PROCFS_PATH = "/proc/rp_gpio/pi5"
_DEFAULT_CYCLES = 2
_DEFAULT_SAMPLE_GAP_SEC = 0.3
# 写完电平到回读之间的等待。默认 **5 秒**, 这 5 秒里同步发生三件事:
#   ① 电平稳定 (~10ms)  ② 风扇物理启转 (~0.5-1.5s)  ③ 操作员目视确认启停 (~2s)
# 设成 0.5s 的话风扇刚起步就回读, 操作员根本没时间看 —— 而「看得见风扇转起来」
# 正是工人确认虚焊/不转的唯一手段。产线默认 5.0s, 返修可调 8.0s。
_DEFAULT_DRIVE_SETTLE_SEC = 5.0
_DEFAULT_ACTIVE_LEVEL = "high"
# '0\n' / '1\n' —— 一次读 4 字节足够, 也是实测安全的长度
_READ_CHUNK = 4


def _read_level(path: str) -> Optional[int]:
    """读厂家直通 GPIO 文件, 返回 0/1; 缺失 / 读失败 / 数据畸形返回 None.

    **必须 os.read, 不能用 `open(p,'rb').read()`** —— 见模块顶部: 缓冲读会崩板。
    每个 fd 只读一次就关, 不要改成循环读同一个 fd。
    """
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return None
    try:
        data = os.read(fd, _READ_CHUNK)
    except OSError:
        return None
    finally:
        os.close(fd)
    if not data:
        return None
    if data[0] == ord('0'):
        return 0
    if data[0] == ord('1'):
        return 1
    return None                    # 数据畸形当作失败


def _write_level(path: str, level: int) -> bool:
    """把 GPIO 拉到 0/1, 写入成功返回 True。

    对应终端里实测可用的 `echo 0 > /proc/rp_gpio/pi5`。
    同样走裸系统调用 —— `open(p,'w')` 会走缓冲层, 别用。
    """
    payload = b"0\n" if level == 0 else b"1\n"
    try:
        fd = os.open(path, os.O_WRONLY)
    except OSError:
        return False
    try:
        written = os.write(fd, payload)
    except OSError:
        return False
    finally:
        os.close(fd)
    return written == len(payload)


@register
class FanTest(TestCase):
    name = "fan"
    display_name = "风扇 GPIO"
    description = "读电平 + 主动拉低/拉高轮转, 验证控制链双向"

    async def run(self, ctx: TestContext) -> TestResult:
        result = TestResult()
        t_cfg = ctx.test_config or {}
        fan_cfg = ctx.config.get("fan", {}) or {}

        path = fan_cfg.get("procfs_path", _DEFAULT_PROCFS_PATH)
        cycles = max(1, int(t_cfg.get(
            "cycles", fan_cfg.get("cycles", _DEFAULT_CYCLES)
        )))
        gap = float(fan_cfg.get("sample_gap_sec", _DEFAULT_SAMPLE_GAP_SEC))
        settle = float(fan_cfg.get("drive_settle_sec", _DEFAULT_DRIVE_SETTLE_SEC))
        drive = bool(fan_cfg.get("drive", True))
        active_high = (
            str(fan_cfg.get("active_level", _DEFAULT_ACTIVE_LEVEL)).lower() != "low"
        )
        active = 1 if active_high else 0
        inactive = 0 if active_high else 1

        result.metrics["io_method"] = f"vendor procfs, 裸 os.read/os.write ({path})"
        result.metrics["procfs_path"] = path
        result.metrics["drive"] = drive
        result.metrics["active_level"] = "high" if active_high else "low"

        # ── 节点自检 (§6.8: 缺节点判 error, 不静默回退) ──
        if not os.path.exists(path):
            result.status = "error"
            result.error = (
                f"内核镜像缺 {path}, 非本工具可修复 —— 该节点由出货内核镜像提供 "
                f"(apt 包补不了), 需镜像侧解决。不允许用 gpioget / libgpiod 静默"
                f"兼底成 skipped: 那会让工人看到「跳过」而实际是镜像有问题。"
                f"(docs/需求设计.md §6.8 / §9.3)"
            )
            await ctx.on_log(f"[风扇] ✗ {result.error}")
            return result

        await ctx.on_log(f"[风扇] IO: vendor procfs 裸系统调用 ({path})")

        # ── 起始电平 ──
        initial = _read_level(path)
        if initial is None:
            result.status = "error"
            result.error = (
                f"{path} 存在但读不出 '0'/'1' —— IO 路径在启动时就不���用 "
                f"(内容畸形, 或节点权限被改)"
            )
            await ctx.on_log(f"[风扇] ✗ {result.error}")
            return result

        await ctx.on_log(
            f"[风扇] 起始电平: {initial} "
            f"(active={'high' if active_high else 'low'})"
        )
        result.metrics["initial_level"] = initial

        if not drive:
            return await self._readonly_pass(ctx, result, initial)

        # ── 主动轮转 ──
        # 无论正常结束 / 取消 / 抛异常, 都要把电平还原成进来时的样子 ——
        # 产线设备上把风扇留在「停」是实打实的隐患。
        await ctx.on_log(
            f"[风扇] 开始主动轮转 {cycles} 轮: 拉低→回读→拉高→回读\n"
            f"[风扇] ⚠️ 轮转期间风扇会真的启停, 请确认机箱内无异物。"
        )
        await ctx.on_progress(5, "轮转中")

        steps: List[dict] = []
        mismatch: List[dict] = []
        cancelled = False
        try:
            for i in range(1, cycles + 1):
                for target, tag in ((inactive, "inactive"), (active, "active")):
                    if await ctx.is_cancelled():
                        cancelled = True
                        break

                    if not _write_level(path, target):
                        result.metrics["steps"] = steps
                        result.status = "error"
                        result.error = (
                            f"第 {i}/{cycles} 轮写电平 {target} 失败 "
                            f"(os.open/os.write 被拒) —— IO 层先挂了, 后面所有"
                            f"轮转结果都不可信, 不是「风扇坏了」。"
                        )
                        await ctx.on_log(f"[风扇] ✗ {result.error}")
                        return result

                    if await self._sleep_cancellable(ctx, settle):
                        cancelled = True
                        break

                    back = _read_level(path)
                    ok = (back == target)
                    steps.append({
                        "cycle": i, "phase": tag,
                        "wrote": target, "read_back": back, "ok": ok,
                    })
                    await ctx.on_log(
                        f"[风扇]   第 {i}/{cycles} 轮 {tag}: 写 {target} → 读回 "
                        f"{'读失败' if back is None else back}"
                        f"{'  ✅' if ok else '  ❌ 不一致'}"
                    )
                    if not ok:
                        mismatch.append(steps[-1])
                    await ctx.on_metric(
                        "gpio_value", back if back is not None else -1
                    )
                    await ctx.on_progress(
                        int(5 + 85 * (len(steps) / (cycles * 2))),
                        f"{len(steps)}/{cycles * 2} 步",
                    )
                if cancelled:
                    break
        finally:
            # 还原起始电平 —— 正常结束 / 取消 / 抛异常都走这里
            _write_level(path, initial)
            result.metrics["restored_level"] = initial

        result.metrics["steps"] = steps

        if cancelled:
            result.status = "stopped"
            await ctx.on_log("[风扇] ⏹ 用户取消, 已把电平还原成起始值")
            return result

        await ctx.on_progress(100, "完成")

        # ── 判定 ──
        if len(steps) < cycles * 2:
            result.status = "error"
            result.error = (
                f"只完成了 {len(steps)}/{cycles * 2} 步 —— 轮转中断, 结果不完整。"
            )
        elif mismatch:
            bad_writes = [s for s in mismatch if s["read_back"] is None]
            bad_levels = [s for s in mismatch if s["read_back"] is not None]
            result.status = "failed"
            result.error = (
                f"{len(mismatch)}/{len(steps)} 步写后回读不一致, 控制链没闭环。\n"
                f"  读回失败 {len(bad_writes)} 步 (IO 层在运行中挂了), "
                f"电平不符 {len(bad_levels)} 步 (驱动没执行写入, 或虚焊/接触不良)。\n"
                f"  详见 metrics.steps。"
            )
            await ctx.on_log(f"[风扇] ❌ {result.error}")
        else:
            result.status = "passed"
            await ctx.on_log(
                f"[风扇] ✅ {cycles} 轮轮转全部闭环: 写 {inactive} 读回 {inactive}, "
                f"写 {active} 读回 {active}, 共 {len(steps)} 步无一失配。\n"
                f"[风扇]   风扇已确认能启停 —— 虚焊/不转这条被本项覆盖到了。"
            )

        return result

    async def _readonly_pass(
        self, ctx: TestContext, result: TestResult, initial: int
    ) -> TestResult:
        """drive=false 时的只读分支: 连读 N 次看电平稳不稳。"""
        fan_cfg = ctx.config.get("fan", {}) or {}
        t_cfg = ctx.test_config or {}
        path = result.metrics["procfs_path"]
        cycles = max(2, int(t_cfg.get(
            "cycles", fan_cfg.get("cycles", _DEFAULT_CYCLES)
        )))
        gap = float(fan_cfg.get("sample_gap_sec", _DEFAULT_SAMPLE_GAP_SEC))

        await ctx.on_log("[风扇] 只读模式 (fan.drive=false): 不写电平, 只体检读回")
        readings = []
        for i in range(1, cycles + 1):
            if await ctx.is_cancelled():
                result.status = "stopped"
                result.metrics["readings"] = readings
                return result
            level = _read_level(path)
            readings.append({"sample": i, "level": level})
            await ctx.on_log(
                f"[风扇]   第 {i}/{cycles} 次采样: "
                f"{'读失败' if level is None else f'电平 {level}'}"
            )
            await ctx.on_metric("gpio_value", level if level is not None else -1)
            if i < cycles and await self._sleep_cancellable(ctx, gap):
                result.status = "stopped"
                result.metrics["readings"] = readings
                return result

        result.metrics["readings"] = readings
        levels = {r["level"] for r in readings if r["level"] is not None}
        bad = sum(1 for r in readings if r["level"] is None)

        await ctx.on_progress(100, "完成")
        if bad == len(readings):
            result.status = "error"
            result.error = f"{cycles}/{cycles} 次采样全部读失败 —— IO 路径挂了"
        elif bad > 0 or len(levels) > 1:
            result.status = "failed"
            result.error = (
                f"{bad}/{cycles} 次读失败, 读出电平 {sorted(levels)} —— 读回不稳定"
            )
        else:
            result.status = "passed"
            await ctx.on_log(
                f"[风扇] ✅ 只读: {cycles}/{cycles} 次采样稳定在电平 {levels.pop()}\n"
                f"[风扇] ⚠️ 未做主动轮转, 风扇没有被拉起过 —— 虚焊/不转靠工人目视。"
            )
        return result

    @staticmethod
    async def _sleep_cancellable(ctx: TestContext, seconds: float) -> bool:
        """可被取消打断的 sleep。返回 True = 期间被取消。

        分片睡而不是一觉睡到底 —— 否则用户点「停止」还得等满整个间隔才生效。
        """
        deadline = time.monotonic() + seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            if await ctx.is_cancelled():
                return True
            await asyncio.sleep(min(0.2, remaining))