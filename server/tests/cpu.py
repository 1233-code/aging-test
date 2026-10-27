"""CPU 压力测试 - 完整实现"""
import asyncio
import os
import time
from typing import Optional

from .base import TestCase, TestContext, TestResult, register
from ._tools import (
    find_tool, with_sudo, sudo_hint_if_unavailable, run_proc_with_progress,
)


@register
class CPUTest(TestCase):
    name = "cpu"
    display_name = "CPU 压力"
    description = "使用 stress-ng 对所有 CPU 核心施加负载,监测温度和性能"

    def expected_seconds(self, test_config: dict) -> Optional[float]:
        times = max(1, int(test_config.get("times", 1)))
        duration = max(5, int(test_config.get("duration_sec", 60)))
        return times * duration

    async def run(self, ctx: TestContext) -> TestResult:
        result = TestResult()
        t_cfg = ctx.test_config
        times = max(1, int(t_cfg.get("times", 1)))
        duration = max(5, int(t_cfg.get("duration_sec", 60)))
        threads = int(t_cfg.get("threads", 0)) or os.cpu_count() or 4
        max_temp = float(t_cfg.get("max_temp_c", 85))
        all_passed = True
        # 跨轮聚合用的收集桶 (§6.2 的 `peak_temp_c` / `avg_load_pct` 是
        # **整项**的指标, 不是某一轮的 —— 报告上要回答的是"这台机器这轮
        # 整体什么水平", 而不是"第 2 轮那一瞬间多少度")。
        peak_temps: list = []
        load_pcts: list = []

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
            stat0 = _read_cpu_stat()
            peak_temp = 0
            temp = None
            started = False
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT
                )
                started = True

                async def tick(e: float, i=i):
                    """按挂钟驱动进度与温度采样 —— **不看 stress-ng 吐不吐行**。

                    原来这个循环靠 `readline()` 唤醒, 而 stress-ng 加上
                    --metrics-brief 之后大部分时间**不吐行**: 于是进度条僵住、
                    温度也停止采样, 工人看着像卡死, 实际 CPU 已经满载跑了一分钟。
                    进度和温度必须由计时器驱动, 与 stdout 解耦。
                    """
                    nonlocal peak_temp, temp
                    temp = await _read_cpu_temp()
                    if temp is not None:
                        if temp > peak_temp:
                            peak_temp = temp
                        await ctx.on_metric("cpu_temp_c", temp)
                        if temp > max_temp:
                            await ctx.on_log(f"[CPU] ⚠️ 温度过高: {temp}°C > {max_temp}°C")
                    pct = min(duration, e) / duration
                    await ctx.on_progress(
                        int((i + pct) / times * 100),
                        f"运行中 {e:.0f}s / {duration}s, 温度 {temp if temp is not None else '?'}°C",
                    )

                rc, text, killed = await run_proc_with_progress(
                    proc, duration, ctx=ctx, on_tick=tick,
                    hard_limit_sec=duration + 30,
                )
                elapsed = time.time() - t0
                result.log = text
                # ── 退出码 (§6.2「失败时记温度峰值 + 退出码」) ──
                # 记在**任何**结果下, 不只是失败时: 失败时它是最重要的线索,
                # 成功时它是"确实跑完了而不是被糊弄过去"的凭据。两种情况
                # 都记, 报告上就不用靠"没这一栏"去猜到底跑没跑。
                result.metrics["exit_code"] = rc
                result.metrics[f"round_{round_idx}_exit_code"] = rc
                stat1 = _read_cpu_stat()
                if stat0 and stat1:
                    d_busy = stat1[0] - stat0[0]
                    d_total = stat1[1] - stat0[1]
                    if d_total > 0:
                        pct = round(d_busy * 100.0 / d_total, 1)
                        load_pcts.append(pct)
                        result.metrics[f"round_{round_idx}_load_pct"] = pct
                for line in text.splitlines():
                    await ctx.on_log(f"  {line}")

                if killed or result.status == "stopped":
                    # 不用裸 terminate/kill —— 子进程是 sudo 起的 root 进程,
                    # 普通用户对它发信号必然 EPERM。见 kill_proc。
                    result.status = "stopped"
                    all_passed = False
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
            finally:
                # §6.2「失败时记温度峰值 + 退出码」—— 放在 finally 里, 因为
                # **失败/取消路径也得留下温度峰值**: 超温判不合格时, 那个
                # 91℃ 就是判定的全部依据, 丢了它报告上只剩一句"失败"。
                # 进程压根没起起来 (FileNotFoundError) 时不记 —— 那时候
                # 记一个 0 会被读成"温度正常, 是别的原因失败"。
                if started:
                    result.metrics[f"round_{round_idx}_peak_temp_c"] = peak_temp
                    peak_temps.append(peak_temp)

        # ── 整项聚合指标 (§6.2) ──
        # 温度峰值取**跨轮最大**: 一次老化里最热的那一下才是判超温的依据,
        # 平均值会把"第 1 轮 91℃、第 2 轮 70℃"这种真实的高温洗掉。
        if peak_temps:
            result.metrics["peak_temp_c"] = max(peak_temps)
        if load_pcts:
            result.metrics["avg_load_pct"] = round(sum(load_pcts) / len(load_pcts), 1)

        if all_passed:
            result.status = "passed"
        elif result.status == "pending":
            result.status = "failed"

        await ctx.on_progress(100, "完成")
        return result


def _read_cpu_stat() -> Optional[tuple]:
    """读 /proc/stat 的 cpu 汇总行 → (busy, total) 累计 tick 数。

    §6.2 的 `avg_load_pct` 要的是**占用率百分比**, 不是 loadavg 那个
    "1 分钟平均有几个进程在跑队列里" —— loadavg 是个无量纲的数, 换不成
    百分比 (4 核机器上 loadavg 1.0 可能是 25% 也可能是 100%)。
    /proc/stat 的 cpu 行给的是**累计 tick**: 两次采样相减,
    busy Δ / total Δ 就是那段时间的真实占用率。
    """
    try:
        with open("/proc/stat", "r") as f:
            line = f.readline()      # "cpu  user nice system idle iowait ..."
        parts = line.split()
        if len(parts) < 5 or parts[0] != "cpu":
            return None
        vals = [int(x) for x in parts[1:]]
        idle = vals[3] + (vals[4] if len(vals) > 4 else 0)   # idle + iowait
        total = sum(vals)
        return (total - idle, total)
    except (OSError, ValueError, IndexError):
        return None


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
