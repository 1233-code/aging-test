"""eMMC / 内置存储测试 - 稀疏文件 fio 测试"""
import asyncio
import os
import time
import re
import shutil
from typing import Optional

from .base import (
    TestCase, TestContext, TestResult, register,
    TestCancelled, DeviceUnresponsive,
)
from ._tools import (
    find_tool, with_sudo, sudo_hint_if_unavailable,
    run_proc_with_progress, fio_runtime_args, fio_timeout_sec,
    leftover_fio_pids, remove_file_async, loader_error_message,
    deps_fix_hint, tool_missing_reason, humanize_exception, ToolMissing,
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


def _leftover_note(pids) -> str:
    """「还有几个 fio 进程没收掉」—— 如实写进报错。

    设备卡死时 fio 会卡在内核的不可中断状态(D 态), 连同为 root 的
    `timeout` 都杀不掉它, 它就变成孤儿进程(PPID=1)继续往死卡上写。
    操作员必须知道这件事 —— 否则他会以为工具已经收干净了, 拔卡重插时
    那些进程还在写, 卡永远缓不过来。
    """
    if not pids:
        return ""
    return (f"; ⚠ 还有 {len(pids)} 个 fio 进程没收掉"
            f"(PID {', '.join(str(p) for p in pids)}): "
            f"设备卡死时进程会卡在内核不可中断状态, 任何信号都杀不掉它, "
            f"需要把卡拔出来复位才能清掉")


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
        # ⚠️ **2026-11-12**: 这条消息以前写死了「sudo apt install fio」——
        #   **在 U 盘版上那是条死命令**(产线取不到公网, 盘上也没有那个脚本)⇒
        #   工人照着做只会白跑。统一走 `tool_missing_reason()`。
        raise ToolMissing(tool_missing_reason(
            "fio", deps_fix_hint(),
            note="PATH 不含 /sbin, 或这台机器上压根没装这个工具"))

    rt = fio_runtime_args(duration_sec, size_mb)
    tmo = fio_timeout_sec(duration_sec)

    async def one_phase(rw: str, metric_key: str) -> dict:
        # **必须带 timeout_sec**: fio 一旦卡住 (坏块 / 驱动异常) 就是永久挂死,
        # 而它被 §11.2 提权成 root 后我们发不了信号 —— 只能靠同为 root 的
        # `timeout` 到点杀它。
        #
        # ⚠️ 但 `timeout` **也杀不掉**卡在 D 态(内核不可中断)的 fio ——
        #    信号会被内核挂起直到 I/O 返回, 而那张卡永远不返回。于是 fio
        #    变成孤儿进程(PPID=1)继续往死卡上写。`timeout` 的退出码 124
        #    就是「到点了但没杀掉」的信号, 下面据此判「设备无响应」。
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

        out = await run_proc_with_progress(
            proc, duration_sec, ctx=ctx, on_tick=tick, hard_limit_sec=tmo + 30,
        )
        rc, text = out.rc, out.text

        # ── 三种「停下来」要分得开 (§9.1 / §9.3) ──
        #
        #   ① 用户按了「停止」      → TestCancelled      → 这一项记 stopped
        #   ② 设备没应答(卡死了)   → DeviceUnresponsive → 这一项记 failed
        #   ③ fio 自己报错          → RuntimeError       → 这一项记 error
        #
        # 以前①②③全是同一句 RuntimeError, 于是工人自己按的暂停在报告里
        # 变成了「失败」(2026-10-28 实测), 而卡死了也看不出是卡的问题。
        if out.reason == "cancelled":
            raise TestCancelled(f"fio {rw} 被用户中止")
        if out.reason == "hard_limit":
            raise DeviceUnresponsive(
                f"fio {rw} 跑了 {tmo + 30}s 还没结束 —— 设备没有应答"
                + _leftover_note(leftover_fio_pids(os.path.basename(test_file)))
            )
        if rc == 124:
            # `timeout` 到点(退出码 124) —— 设备没应答。注意: 卡在 D 态的
            # fio 这时候**还活着**, timeout 收不掉它。
            raise DeviceUnresponsive(
                f"fio {rw} 跑到挂钟上限({tmo}s)仍未结束 —— 设备没有应答"
                + _leftover_note(leftover_fio_pids(os.path.basename(test_file)))
            )
        if rc != 0:
            # ⚠️ 「工具起不来」跟「跑了但失败」必须分开说 —— 前者是**盘/镜像**
            # 的问题(不是板子坏了), 混成一句会让好板子白白返修(2026-11-06 实测)
            loader_msg = loader_error_message(rc, text)
            if loader_msg:
                raise RuntimeError(loader_msg)
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

                t0 = time.monotonic()
                m = await _run_fio_test(
                    test_file, size_mb, ctx, duration,
                    on_metric=ctx.on_metric, on_progress=tick,
                )
                elapsed = time.monotonic() - t0

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
        except TestCancelled:
            # 用户中途按了「停止」→ 记 **stopped**(§9.3), 不是 error/failed。
            # 没跑完 ≠ 不合格, 更不该把整轮判成失败。
            result.status = "stopped"
            result.error = "用户中途停止 (本项未跑完)"
            await ctx.on_log("[eMMC] 用户中途停止 → 本项记 stopped (不计失败)")
            return result
        except DeviceUnresponsive as e:
            # 设备对读写没应答 = **设备本身坏了** → failed(不合格, 换板子)
            result.status = "failed"
            result.error = (
                f"eMMC **无响应**(对读写没有应答, 卡在 D 态收不掉) —— 这一项"
                f"**没测完**, 但被测的存储**不合格**(要返修)。"
                f"具体经过: {e}"
            )
            await ctx.on_log(f"[eMMC] ❌ {result.error}")
            return result
        except ToolMissing as e:
            # 「fio 压根没装」—— 原因**已经写好了**, 直接拿来用。
            result.status = "error"
            result.error = e.reason
            return result
        except FileNotFoundError as e:
            result.status = "error"
            result.error = tool_missing_reason(
                "fio", deps_fix_hint(),
                note="PATH 不含 /sbin, 或这台机器上压根没装这个工具",
                detail=str(e))
            return result
        except Exception as e:
            result.status = "error"
            result.error = humanize_exception(
                e, item="eMMC 项", stage="读写测试")
            return result
        finally:
            # 清理 —— **走子进程**, 绝不在事件循环里直接 os.unlink:
            # 设备卡死时 unlink 会永远不返回(D 态, 内核不可中断), 而这是
            # 同步调用 → 整个事件循环冻结 → 整个服务死掉(2026-10-28 实测)。
            try:
                await remove_file_async(test_file)
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
