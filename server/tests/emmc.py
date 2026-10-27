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


# ─────────────────── §6.11 ③ 寿命健康度 ───────────────────
#
# 节点内容形如 "0x01 0x01" —— 两个值, 分别是 TYPE_A / TYPE_B 两条通道的
# **已用寿命百分比**(SD 规范: 0x00=0%, 0x0A=100%, 0xFF=未知)。
#
# 这一项以前**根本没实现**: 文档 §6.11 写着「3 项全过」(写 + 读 + 寿命),
# 代码却只跑 fio 写 + 读两阶段 —— 寿命损耗 ≥80% 的板子照样判 passed, 报告里
# 也没有任何寿命数据。§9.4 行 1630 的「eMMC 综合 3 项」同样对不上。
#
# 叫法统一成「**已用寿命百分比**」(life_used_pct): 节点给的就是已用百分比,
# 别再叫 health_pct_wear(健康度) —— 一个说剩余一个说已用, 纸面上必然打架。
LIFE_TIME_PATH = "/sys/block/mmcblk0/device/life_time"
# 损耗**大于** 80% → 判 failed(老化风险, 送修)。恰好 80% 放过。
LIFE_TIME_MAX_USED_PCT = 80


def _parse_life_time(text: str) -> Optional[int]:
    """解析 life_time 节点内容 → 已用寿命百分比(0-100)。

    形如 "0x01 0x01"。取各值里**最大**的 —— 两条通道取最差的那条,
    一条磨穿了就是磨穿了。

    0xFF = 「未知」(SD 规范里的 predefined/unknown), 返回 None ——
    **不能**当成 255% 也不能当成 0%。调用方据此区分「未知」和「读不到」。
    """
    vals = []
    for tok in text.split():
        tok = tok.strip()
        if not tok.lower().startswith("0x"):
            continue
        try:
            v = int(tok, 16)
        except ValueError:
            continue
        if v == 0xFF:
            return None          # 未知 —— 整条作废
        vals.append(min(v * 10, 100))   # 0x0A = 10 → 100%
    if not vals:
        return None
    return max(vals)


def _read_life_time(path: str = LIFE_TIME_PATH) -> tuple:
    """读寿命节点 → (used_pct, raw, err)

    · used_pct 不是 None → 读到了, 那个数就是已用寿命百分比
    · used_pct 是 None、raw 有值、err 是 None → 节点说「未知」(0xFF)
    · used_pct 是 None、err 有值 → 读不到 / 格式不认识
    """
    try:
        with open(path, "r") as f:
            raw = f.read().strip()
    except FileNotFoundError:
        return None, None, f"寿命节点不存在: {path}"
    except OSError as e:
        return None, None, f"读寿命节点失败: {path} ({e})"
    if not raw:
        return None, raw, f"寿命节点是空的: {path}"
    pct = _parse_life_time(raw)
    if pct is None:
        return None, raw, None        # 0xFF 未知 —— 不是错误, 但也判不了
    return pct, raw, None


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
                    # 失败原因要**写进结果** —— 以前只打了日志, result.error 是空的,
                    # 报告的「原因框」于是印出一段空白, 纸面读者不知道到底为什么
                    # 这一项没过。
                    result.error = f"读速度 {read:.1f} MB/s 低于阈值 {min_read} MB/s"
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

        # ── ③ 寿命健康度 (§6.11) ──
        #
        # 放在 fio 之后, 但**不管 fio 跑得怎么样都要查** —— 寿命数据对复盘有用,
        # 而且它只花几毫秒。
        used_pct, raw, life_err = _read_life_time()
        if used_pct is not None:
            result.metrics["life_time_hex"] = raw
            result.metrics["life_used_pct"] = used_pct
            await ctx.on_log(
                f"[eMMC] 寿命: 已用 {used_pct}% (节点 {raw}, 阈值 ≤ {LIFE_TIME_MAX_USED_PCT}%)"
            )
            if used_pct > LIFE_TIME_MAX_USED_PCT:
                await ctx.on_log(
                    f"[eMMC] ❌ 已用寿命 {used_pct}% > {LIFE_TIME_MAX_USED_PCT}% —— 老化风险, 送修"
                )
                all_passed = False
                if not result.error:
                    result.error = (
                        f"eMMC 寿命损耗 {used_pct}% 超过 {LIFE_TIME_MAX_USED_PCT}%, 老化风险, 送修"
                    )
        elif life_err:
            # 读不到 → 判 error(§6.11)。我们根本没能力判这块介质的寿命,
            # 拿「读不到」当「好」是**静默的通过** —— 比失败更糟。
            result.status = "error"
            result.error = f"eMMC 寿命检查失败: {life_err}"
            await ctx.on_log(f"[eMMC] ❌ {result.error}")
            return result
        else:
            # 节点自己说「未知」(0xFF) —— 不是错误, 但也判不了。
            # 放行(不能因为板子没报数就判它坏), 但把「判不了」明写进指标,
            # 不让它悄悄变成「好」。
            result.metrics["life_time_hex"] = raw
            result.metrics["life_used_pct"] = "未知"
            await ctx.on_log(
                f"[eMMC] ⚠ 寿命节点报「未知」({raw}) —— 本项不判寿命, 需人工确认"
            )

        if all_passed:
            result.status = "passed"
        elif result.status == "pending":
            result.status = "failed"
        return result
