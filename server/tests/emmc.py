"""eMMC / 内置存储测试 - 稀疏文件 fio 测试"""
import asyncio
import os
import time
import re
import shutil

from .base import TestCase, TestContext, TestResult, register
from ._tools import find_tool, with_sudo, sudo_hint_if_unavailable


def _parse_fio_text(text: str) -> dict:
    """解析 fio 文本输出中的关键指标"""
    metrics = {}
    # 找 "READ: bw=...MiB/s" / "WRITE: bw=..." 行
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


async def _run_fio_test(test_file: str, size_mb: int, ctx: TestContext, on_metric=None) -> dict:
    """跑一次 fio 顺序读写,返回 {read_mbps, write_mbps}

    §11.1 find_tool 找二进制 (普通用户 PATH 不含 /sbin);
    §11.2 裸块设备 / sparse 文件写入走命令级 sudo -n。
    """
    metrics = {}
    fio_bin = find_tool("fio")
    if not fio_bin:
        raise FileNotFoundError(
            "fio: 未找到. 解决: sudo apt install fio (见 docs/需求设计.md §11.7)"
        )

    # 写测试
    cmd_w = with_sudo([
        fio_bin, "--name=write", "--ioengine=libaio",
        "--direct=1", "--bs=1M", f"--size={size_mb}M",
        f"--filename={test_file}", "--rw=write",
        "--output-format=normal",
    ])
    proc = await asyncio.create_subprocess_exec(
        *cmd_w, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    text = (await proc.communicate())[0].decode(errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(f"fio write 失败: {proc.returncode}")
    m = _parse_fio_text(text)
    if "write_mbps" in m:
        metrics["write_mbps"] = m["write_mbps"]
    if on_metric:
        await on_metric("write_mbps", metrics.get("write_mbps", 0))

    # 读测试 —— 从写命令派生出读命令 (cmd_w 现在是 with_sudo 包过的完整命令)
    cmd_r = list(cmd_w)
    cmd_r[cmd_r.index("--rw=write")] = "--rw=read"
    for i, x in enumerate(cmd_r):
        if x == "--name=write":
            cmd_r[i] = "--name=read"
    proc = await asyncio.create_subprocess_exec(
        *cmd_r, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    text = (await proc.communicate())[0].decode(errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(f"fio read 失败: {proc.returncode}")
    m = _parse_fio_text(text)
    if "read_mbps" in m:
        metrics["read_mbps"] = m["read_mbps"]
    if on_metric:
        await on_metric("read_mbps", metrics.get("read_mbps", 0))

    return metrics


@register
class EMMCTest(TestCase):
    name = "emmc"
    display_name = "eMMC 存储"
    description = "内置存储读写性能 (稀疏文件,非裸设备)"

    async def run(self, ctx: TestContext) -> TestResult:
        result = TestResult()
        t_cfg = ctx.test_config
        times = max(1, int(t_cfg.get("times", 1)))
        duration = max(10, int(t_cfg.get("duration_sec", 60)))
        min_read = float(t_cfg.get("min_read_mbps", 80))

        test_file = "/tmp/aging_test_emmc.bin"
        size_mb = max(64, int(t_cfg.get("size_mb", 256)))  # 默认 256MB 跳率快一些, 不再几十秒过完
        all_passed = True

        # §11.2: 命令级 sudo -n 取 root; 不可用判 error 不静默降级
        if not sudo_hint_if_unavailable(result, "eMMC"):
            await ctx.on_log(f"[eMMC] ✗ {result.error}")
            return result

        await ctx.on_log(f"[eMMC] {times} 次, 文件={test_file}, 大小={size_mb}MB")
        await ctx.on_progress(0, "准备开始")

        try:
            for i in range(times):
                if await ctx.is_cancelled():
                    result.status = "stopped"
                    return result
                round_idx = i + 1
                await ctx.on_log(f"[eMMC] 第 {round_idx}/{times} 轮")
                await ctx.on_progress(int((i / times) * 100), f"第 {round_idx} 轮")

                t0 = time.time()
                m = await _run_fio_test(test_file, size_mb, ctx, on_metric=ctx.on_metric)
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
