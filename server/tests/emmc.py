"""eMMC / 内置存储测试 - 稀疏文件 fio 测试"""
import asyncio
import os
import time
import re
import shutil
from typing import Optional

from .base import TestCase, TestContext, TestResult, register
from ._tools import (
    find_tool, with_sudo, sudo_hint_if_unavailable,
    run_proc_with_progress, fio_runtime_args, fio_timeout_sec,
)


def _parse_fio_text(text: str) -> dict:
    """解析 fio 文本输出中的关键指标"""
    metrics = {}
    # 找 "READ: bw=...MiB/s" / "WRITE: bw=..." 行
    # (fio 3.16 --output-format=normal 实测确实输出这两行大写标签)
    for line in text.splitlines():
        m = re.search(r"READ:\s*bw=([\d.]+)([KMG]?i?B)/s", line)
        if m:
            v = float(m.group(1))
            unit = m.group(2)
            mult = {"B": 1/1024/1024, "KiB": 1/1024, "MiB": 1, "GiB": 1024}.get(unit, 1)
            metrics["read_mbps"] = v * mult
        m = re.search(r"WRITE:\s*bw=([\d.]+)([KMG]?i?B)/s", line)
        if m:
            v = float(m.group(1))
            unit = m.group(2)
            mult = {"B": 1/1024/1024, "KiB": 1/1024, "MiB": 1, "GiB": 1024}.get(unit, 1)
            metrics["write_mbps"] = v * mult
    return metrics


async def _run_fio_test(
    test_file: str,
    size_mb: int,
    ctx: TestContext,
    duration_sec: float,
    on_metric=None,
    on_progress=None,
) -> dict:
    """跑一次 fio **定长**顺序读写, 返回 {read_mbps, write_mbps}

    duration_sec —— 写、读**各自**跑满多少秒。靠 fio 的 --time_based+--runtime
    (见 fio_runtime_args)。以前这里只给了 --size, fio 把文件跑一遍就退, 于是
    UI 上配的「时长」完全不起作用: 设 60s 十几秒就完事, 工人只会怀疑工具坏了。

    on_progress —— async callback(阶段, 已跑秒, 总秒), 用来报**真实**进度。
    之前这一段是裸 `await proc.communicate()`: 阻塞到进程退出, 中间既不报进度
    也不查取消, 工人看着像卡死, 点了「停止」也得等它自己跑完。

    §11.1 find_tool 找二进制 (普通用户 PATH 不含 /sbin);
    §11.2 裸块设备 / sparse 文件写入走命令级 sudo -n。
    """
    metrics = {}
    fio_bin = find_tool("fio")
    if not fio_bin:
        raise FileNotFoundError(
            "fio: 未找到. 解决: sudo apt install fio (见 docs/需求设计.md §11.7)"
        )

    rt = fio_runtime_args(duration_sec, size_mb)
    tmo = fio_timeout_sec(duration_sec)

    async def one_phase(rw: str, metric_key: str) -> dict:
        # **必须带 timeout_sec**: fio 一旦卡住 (坏块 / 驱动异常) 就是永久挂死,
        # 而它被 §11.2 提权成 root 后我们发不了信号 —— 只能靠同为 root 的
        # `timeout` 到点杀它。
        cmd = with_sudo([
            fio_bin, f"--name={rw}", "--ioengine=libaio",
            "--direct=1", "--bs=1M",
            f"--filename={test_file}", f"--rw={rw}",
            "--output-format=normal",
        ] + rt, timeout_sec=tmo)

        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )

        async def tick(elapsed: float):
            if on_progress:
                await on_progress(rw, elapsed, duration_sec)

        rc, text, killed = await run_proc_with_progress(
            proc, duration_sec, ctx=ctx, on_tick=tick, hard_limit_sec=tmo + 30,
        )
        if killed:
            raise RuntimeError(f"fio {rw} 被中断 (取消或超时)")
        if rc != 0:
            raise RuntimeError(f"fio {rw} 失败: rc={rc} · {text[-300:]}")
        m = _parse_fio_text(text)
        if metric_key in m:
            metrics[metric_key] = m[metric_key]
        if on_metric:
            await on_metric(metric_key, metrics.get(metric_key, 0))
        return m

    await one_phase("write", "write_mbps")
    await one_phase("read", "read_mbps")
    return metrics


@register
class EMMCTest(TestCase):
    name = "emmc"
    display_name = "eMMC 存储"
    description = "内置存储读写性能 (稀疏文件,非裸设备)"

    def expected_seconds(self, test_config: dict) -> Optional[float]:
        # duration_sec 是**每个阶段**的时长, 一轮 = 写 + 读 = ×2
        times = max(1, int(test_config.get("times", 1)))
        duration = max(10, int(test_config.get("duration_sec", 30)))
        return times * duration * 2

    async def run(self, ctx: TestContext) -> TestResult:
        result = TestResult()
        t_cfg = ctx.test_config
        times = max(1, int(t_cfg.get("times", 1)))
        duration = max(10, int(t_cfg.get("duration_sec", 30)))
        min_read = float(t_cfg.get("min_read_mbps", 80))

        test_file = "/tmp/aging_test_emmc.bin"
        size_mb = max(64, int(t_cfg.get("size_mb", 256)))
        all_passed = True
        # 一轮 = 写 duration 秒 + 读 duration 秒
        round_sec = duration * 2
        total_sec = round_sec * times

        # §11.2: 命令级 sudo -n 取 root; 不可用判 error 不静默降级
        if not sudo_hint_if_unavailable(result, "eMMC"):
            await ctx.on_log(f"[eMMC] ✗ {result.error}")
            return result

        # 把实际要跑多久**先说清楚** —— 之前「设 60s 却十几秒结束」就是因为
        # 没人知道它到底该跑多久, 工人只能凭感觉怀疑工具坏了。
        await ctx.on_log(
            f"[eMMC] {times} 轮 × (写 {duration}s + 读 {duration}s)"
            f" = 约 {total_sec}s, 工作集 {size_mb}MB, 文件={test_file}"
        )
        await ctx.on_progress(0, f"准备开始 · 预计 {total_sec}s")

        try:
            for i in range(times):
                if await ctx.is_cancelled():
                    result.status = "stopped"
                    return result
                round_idx = i + 1
                await ctx.on_log(f"[eMMC] 第 {round_idx}/{times} 轮")

                async def tick(phase: str, phase_elapsed: float, phase_total: float,
                               i=i, round_idx=round_idx):
                    """整项进度 = (已完成的轮 + 本轮已完成比例) / 总轮数"""
                    # 本轮里, 写阶段算前半、读阶段算后半
                    done_sec = (0 if phase == "write" else phase_total) + phase_elapsed
                    frac = min(1.0, done_sec / round_sec)
                    pct = (i + frac) / times * 100
                    await ctx.on_progress(
                        int(pct),
                        f"第 {round_idx}/{times} 轮 · {'写' if phase == 'write' else '读'}"
                        f" {phase_elapsed:.0f}s / {phase_total:.0f}s",
                    )

                t0 = time.time()
                m = await _run_fio_test(
                    test_file, size_mb, ctx, duration,
                    on_metric=ctx.on_metric, on_progress=tick,
                )
                elapsed = time.time() - t0

                read = m.get("read_mbps", 0)
                write = m.get("write_mbps", 0)
                await ctx.on_log(f"[eMMC] 读 {read:.1f} MB/s, 写 {write:.1f} MB/s, 耗时 {elapsed:.1f}s")

                if read < min_read:
                    await ctx.on_log(f"[eMMC] ❌ 读速度 {read:.1f} < 阈值 {min_read} MB/s")
                    all_passed = False
                result.metrics[f"round_{round_idx}_read_mbps"] = read
                result.metrics[f"round_{round_idx}_write_mbps"] = write
                if not all_passed:
                    break
        except FileNotFoundError:
            result.status = "error"
            result.error = "未安装 fio"
            return result
        except Exception as e:
            result.status = "error"
            result.error = f"异常: {e!r}"
            return result
        finally:
            # 清理
            try:
                if os.path.exists(test_file):
                    os.unlink(test_file)
            except Exception:
                pass

        if all_passed:
            result.status = "passed"
        elif result.status == "pending":
            result.status = "failed"
        return result
