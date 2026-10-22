"""CPU 压力测试 - 完整实现"""
import asyncio
import os
import time
from typing import Optional

from .base import TestCase, TestContext, TestResult, register
from ._tools import find_tool, with_sudo, kill_proc, sudo_hint_if_unavailable


@register
class CPUTest(TestCase):
    name = "cpu"
    display_name = "CPU 压力"
    description = "使用 stress-ng 对所有 CPU 核心施加负载,监测温度和性能"

    async def run(self, ctx: TestContext) -> TestResult:
        result = TestResult()
        t_cfg = ctx.test_config
        times = max(1, int(t_cfg.get("times", 1)))
        duration = max(5, int(t_cfg.get("duration_sec", 60)))
        threads = int(t_cfg.get("threads", 0)) or os.cpu_count() or 4
        max_temp = float(t_cfg.get("max_temp_c", 85))
        all_passed = True

        # §11.1 find_tool 找二进制; §11.2 命令级 sudo 取 root
        stress_bin = find_tool("stress-ng")
        if not stress_bin:
            result.status = "error"
            result.error = (
                "未找到 stress-ng. 解决: sudo apt install stress-ng "
                "(见 docs/需求设计.md §11.7 打包必装清单)"
            )
            await ctx.on_log(f"[CPU] ✗ {result.error}")
            return result
        await ctx.on_log(f"[CPU] 工具: {stress_bin}")
        if not sudo_hint_if_unavailable(result, "CPU 压力"):
            await ctx.on_log(f"[CPU] ✗ {result.error}")
            return result

        await ctx.on_log(f"[CPU] 启动: {times} 次 × {duration}s, 线程数={threads}, 温度上限={max_temp}°C")
        await ctx.on_progress(0, "准备开始")

        for i in range(times):
            if await ctx.is_cancelled():
                await ctx.on_log("[CPU] 用户取消")
                result.status = "stopped"
                all_passed = False
                break

            round_idx = i + 1
            await ctx.on_log(f"[CPU] 第 {round_idx}/{times} 轮")
            await ctx.on_progress(int((i / times) * 100), f"第 {round_idx} 轮")

            # §11.2: 需 root 的调用一律 `sudo -n <命令>`, 绝不弹密码提示
            cmd = with_sudo([
                stress_bin,
                "--cpu", str(threads),
                "--timeout", f"{duration}s",
                "--metrics-brief",
            ], timeout_sec=duration + 30)
            await ctx.on_log(f"[CPU] 执行: {' '.join(cmd)}")

            t0 = time.time()
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT
                )

                peak_temp = 0
                lines_buf = []
                while True:
                    if await ctx.is_cancelled():
                        # 不用裸 terminate/kill —— 子进程是 sudo 起的 root
                        # 进程, 普通用户对它发信号必然 EPERM。见 kill_proc。
                        await kill_proc(proc, wait_sec=3.0, ctx=ctx)
                        result.status = "stopped"
                        all_passed = False
                        break

                    line = await proc.stdout.readline()
                    if not line:
                        break
                    text = line.decode(errors="replace").rstrip()
                    lines_buf.append(text)
                    if len(lines_buf) > 200:
                        lines_buf = lines_buf[-200:]
                    await ctx.on_log(f"  {text}")

                    # 读温度
                    temp = await _read_cpu_temp()
                    if temp is not None:
                        if temp > peak_temp:
                            peak_temp = temp
                        await ctx.on_metric("cpu_temp_c", temp)
                        if temp > max_temp:
                            await ctx.on_log(f"[CPU] ⚠️ 温度过高: {temp}°C > {max_temp}°C")

                    # 进度: 按时间比例
                    elapsed = time.time() - t0
                    pct = min(duration, elapsed) / duration
                    overall = (i + pct) / times * 100
                    await ctx.on_progress(int(overall), f"运行中 {elapsed:.0f}s / {duration}s, 温度 {temp or '?'}°C")

                rc = await proc.wait() if proc.returncode is None else proc.returncode
                elapsed = time.time() - t0
                result.log = "\n".join(lines_buf)

                if result.status == "stopped":
                    break

                if rc != 0:
                    await ctx.on_log(f"[CPU] ❌ stress-ng 返回非 0: {rc}")
                    all_passed = False
                    break
                if peak_temp > max_temp:
                    await ctx.on_log(f"[CPU] ❌ 峰值温度 {peak_temp}°C 超过 {max_temp}°C")
                    all_passed = False
                    break
                if peak_temp == 0:
                    await ctx.on_log("[CPU] ⚠️ 未读取到温度,跳过温度检查")

            except FileNotFoundError:
                result.error = "未安装 stress-ng,请运行 sudo bash scripts/install_deps.sh"
                result.status = "error"
                all_passed = False
                break
            except Exception as e:
                result.error = f"异常: {e!r}"
                result.status = "error"
                all_passed = False
                break

            result.metrics[f"round_{round_idx}_peak_temp_c"] = peak_temp

        if all_passed:
            result.status = "passed"
        elif result.status == "pending":
            result.status = "failed"

        await ctx.on_progress(100, "完成")
        return result


async def _read_cpu_temp() -> Optional[float]:
    """从 /sys/class/thermal 读 CPU 温度 (°C)"""
    thermal_dir = "/sys/class/thermal"
    try:
        # 找名字含 cpu / soc / x 核心的 zone
        zones = sorted(os.listdir(thermal_dir))
        for z in zones:
            if not z.startswith("thermal_zone"):
                continue
            type_path = os.path.join(thermal_dir, z, "type")
            temp_path = os.path.join(thermal_dir, z, "temp")
            if not os.path.exists(type_path) or not os.path.exists(temp_path):
                continue
            try:
                with open(type_path) as f:
                    ztype = f.read().strip().lower()
                if "cpu" in ztype or "soc" in ztype or "x-t" in ztype or "tsens" in ztype:
                    with open(temp_path) as f:
                        return int(f.read().strip()) / 1000.0
            except (IOError, ValueError):
                continue
        # fallback: 读第一个 zone
        for z in zones:
            if not z.startswith("thermal_zone"):
                continue
            temp_path = os.path.join(thermal_dir, z, "temp")
            if os.path.exists(temp_path):
                try:
                    with open(temp_path) as f:
                        return int(f.read().strip()) / 1000.0
                except (IOError, ValueError):
                    pass
    except Exception:
        pass
    return None
