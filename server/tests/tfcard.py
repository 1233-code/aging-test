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
        size_mb = 64
        test_file = os.path.join(mount, ".aging_test_tf.bin")
        all_passed = True
        await ctx.on_progress(20, "准备写入测试文件")

        try:
            for i in range(times):
                if await ctx.is_cancelled():
                    result.status = "stopped"
                    all_passed = False
                    break

                round_idx = i + 1
                pct_base = int(20 + (i / times) * 75)
                await ctx.on_log(f"[TF] 第 {round_idx}/{times} 轮")
                await ctx.on_progress(pct_base, f"第 {round_idx} 轮")

                t0 = time.time()
                m = await _run_fio_test(test_file, size_mb, ctx, on_metric=ctx.on_metric)
                elapsed = time.time() - t0

                read_mbps = m.get("read_mbps", 0)
                write_mbps = m.get("write_mbps", 0)
                await ctx.on_log(f"[TF] 读 {read_mbps:.1f} MB/s, 写 {write_mbps:.1f} MB/s")

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
