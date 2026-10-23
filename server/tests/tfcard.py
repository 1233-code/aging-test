"""TF 卡测试 - 自动检测挂载点 (§6.9)

- 检测到挂载点 → 跑 fio 顺序读写 → pass / fail
- 未检测到挂载点 → status=skipped + metrics.no_mount=true
  → 前端识别这个 metric 后弹 prompt(「已插好 / 跳过」二选一)
  → 点"已插好"触发 /api/test/retest 重测
"""
import asyncio
import os
import time
from typing import Optional

from .base import TestCase, TestContext, TestResult, register
from ._tools import sudo_hint_if_unavailable
from .emmc import _run_fio_test


def _find_tfcard_mount() -> Optional[str]:
    """找 TF 卡的挂载点.

    识别方式: /proc/mounts 中设备路径含 /dev/mmcblk1 (TF 卡常见设备名).
    """
    try:
        with open("/proc/mounts") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 3:
                    continue
                device, mountpoint, fstype = parts[0], parts[1], parts[2]
                if "/mmcblk1" in device or "mmcblk1" in device:
                    if mountpoint.startswith("/"):
                        return mountpoint
    except Exception:
        pass
    return None


@register
class TFCardTest(TestCase):
    name = "tfcard"
    display_name = "TF 卡"
    description = "自动检测挂载点 · 未插弹提示让工人选「已插好 / 跳过」"

    def expected_seconds(self, test_config: dict) -> Optional[float]:
        # duration_sec 是**每个阶段**的时长, 一轮 = 写 + 读 = ×2
        times = max(1, int(test_config.get("times", 1)))
        duration = max(10, int(test_config.get("duration_sec", 30)))
        return times * duration * 2

    async def run(self, ctx: TestContext) -> TestResult:
        result = TestResult()
        t_cfg = ctx.test_config
        times = max(1, int(t_cfg.get("times", 1)))
        min_read = float(t_cfg.get("min_read_mbps", 10))

        await ctx.on_log("[TF] 自动检测 TF 卡挂载点...")
        await ctx.on_progress(5, "探挂载点")

        # === 步骤 1: 自动探挂载点 ===
        mount = _find_tfcard_mount()

        if not mount:
            await ctx.on_log("[TF] ❌ 未检测到 TF 卡挂载点")
            await ctx.on_log("[TF] 提示 UI 弹窗: 工人可选「已插好(重测)」或「跳过」")
            result.status = "skipped"
            result.metrics["no_mount"] = True
            result.metrics["detection_method"] = "auto_mount_probe"
            result.error = "未检测到 TF 卡挂载点 (等待工人选择)"
            await ctx.on_progress(100, "等待工人选择")
            return result

        # 探到卡才需要写权限 —— §11.2 命令级 sudo -n (挂载点写入需 root)
        if not sudo_hint_if_unavailable(result, "TF 卡"):
            await ctx.on_log(f"[TF] ✗ {result.error}")
            return result

        await ctx.on_log(f"[TF] ✓ 找到挂载点: {mount}")
        await ctx.on_progress(15, f"挂载点 {mount}")

        # === 步骤 2: 跑 fio 测试 ===
        # duration_sec: 写、读各自跑满多少秒。TF 卡可能很慢 (低速卡 / 接触不良
        # 正是要挑出来的那类), 所以工作集比 eMMC 小, 但**时长照跑** ——
        # 以前这里写死 64MB 且不带 --runtime, 一张卡几秒就测完了, 配的时长形同虚设。
        size_mb = max(16, int(t_cfg.get("size_mb", 64)))
        duration = max(10, int(t_cfg.get("duration_sec", 30)))
        round_sec = duration * 2
        test_file = os.path.join(mount, ".aging_test_tf.bin")
        all_passed = True
        await ctx.on_log(
            f"[TF] {times} 轮 × (写 {duration}s + 读 {duration}s)"
            f" = 约 {round_sec * times}s, 工作集 {size_mb}MB"
        )
        await ctx.on_progress(15, f"准备写入 · 预计 {round_sec * times}s")

        try:
            for i in range(times):
                if await ctx.is_cancelled():
                    result.status = "stopped"
                    all_passed = False
                    break

                round_idx = i + 1
                await ctx.on_log(f"[TF] 第 {round_idx}/{times} 轮")

                async def tick(phase: str, phase_elapsed: float, phase_total: float,
                               i=i, round_idx=round_idx):
                    done_sec = (0 if phase == "write" else phase_total) + phase_elapsed
                    frac = min(1.0, done_sec / round_sec)
                    await ctx.on_progress(
                        int(15 + (i + frac) / times * 85),
                        f"第 {round_idx}/{times} 轮 · {'写' if phase == 'write' else '读'}"
                        f" {phase_elapsed:.0f}s / {phase_total:.0f}s",
                    )

                t0 = time.time()
                m = await _run_fio_test(
                    test_file, size_mb, ctx, duration,
                    on_metric=ctx.on_metric, on_progress=tick,
                )
                elapsed = time.time() - t0

                read_mbps = m.get("read_mbps", 0)
                write_mbps = m.get("write_mbps", 0)
                await ctx.on_log(
                    f"[TF] 读 {read_mbps:.1f} MB/s, 写 {write_mbps:.1f} MB/s"
                    f" (耗时 {elapsed:.1f}s)"
                )

                if read_mbps < min_read:
                    await ctx.on_log(f"[TF] ❌ 读速度 {read_mbps:.1f} < {min_read} MB/s")
                    all_passed = False

                result.metrics[f"round_{round_idx}_read_mbps"] = read_mbps
                result.metrics[f"round_{round_idx}_write_mbps"] = write_mbps
                if not all_passed:
                    break
        except FileNotFoundError:
            result.status = "error"
            result.error = "未安装 fio"
            await ctx.on_progress(100, "缺 fio")
            return result
        except Exception as e:
            result.status = "error"
            result.error = f"异常: {e!r}"
            await ctx.on_progress(100, "异常")
            return result
        finally:
            try:
                if os.path.exists(test_file):
                    os.unlink(test_file)
            except Exception:
                pass

        if all_passed and result.status == "pending":
            result.status = "passed"
        elif result.status == "pending":
            result.status = "failed"

        await ctx.on_progress(100, "完成")
        return result

    def operator_prompt(self, result: TestResult):
        """卡槽是空的 → 停下来等工人插卡 (§7.8)。

        卡槽空不是设备缺陷, 也不是「测完了」—— 它是「料没到位」。
        工人只有两个选择: 插上卡重测, 或者跳过。选完之前整轮必须停住,
        否则下一项会在他犹豫的时候开跑, 屏幕就跑到别的地方去了。
        """
        if result.status == "skipped" and (result.metrics or {}).get("no_mount"):
            return {"text": "未检测到 TF 卡。请插入 TF 卡后选「已插好重测」, 或选「跳过」。"}
        return None
