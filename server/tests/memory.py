"""内存测试 - memtester"""
import asyncio
import os
import time
import re

from .base import TestCase, TestContext, TestResult, register
from ._tools import find_tool, run_cmd, is_root, run_proc_with_wallclock, with_sudo, kill_proc, sudo_hint_if_unavailable


@register
class MemoryTest(TestCase):
    name = "memory"
    display_name = "内存"
    description = "使用 memtester 进行内存读写校验"

    async def run(self, ctx: TestContext) -> TestResult:
        result = TestResult()
        t_cfg = ctx.test_config
        times = max(1, int(t_cfg.get("times", 1)))
        size_mb = max(64, int(t_cfg.get("size_mb", 1024)))
        duration = max(10, int(t_cfg.get("duration_sec", 60)))

        # 预检工具
        tool = find_tool("memtester")
        if not tool:
            result.status = "error"
            result.error = (
                "未找到 memtester (PATH 不含 /sbin, 或未装). "
                "解决: sudo bash scripts/install_deps.sh"
            )
            await ctx.on_log(f"[内存] ✗ {result.error}")
            return result
        await ctx.on_log(f"[内存] 工具: {tool}")

        # §11.2: 命令级 sudo -n 取 root; 不可用判 error 不静默降级
        if not sudo_hint_if_unavailable(result, "内存"):
            await ctx.on_log(f"[内存] ✗ {result.error}")
            return result

        all_passed = True
        await ctx.on_log(f"[内存] {times} 次 × {size_mb}MB")
        await ctx.on_progress(0, "准备开始")

        for i in range(times):
            if await ctx.is_cancelled():
                result.status = "stopped"
                return result
            round_idx = i + 1
            await ctx.on_log(f"[内存] 第 {round_idx}/{times} 轮")
            await ctx.on_progress(int((i / times) * 100), f"第 {round_idx} 轮")

            # §11.2 一律 sudo → 子进程是 root, 而服务是普通用户 → **我们发不了信号**
            # (os.kill 对 root 进程必然 EPERM)。所以挂钟收尾交给 root 领地内部的
            # `timeout`: 它和 memtester 同为 root, 到点自己杀自己的子进程,
            # 服务全程一个信号都不用发。见 _tools.with_sudo 的「为什么要 timeout」。
            cmd = with_sudo([tool, f"{size_mb}M", "1"], timeout_sec=duration)
            await ctx.on_log(f"[内存] 执行: {' '.join(cmd)}")

            t0 = time.time()
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
                )

                # memtester 没有自己的 --timeout, 由上面的 `timeout <duration>` 兜底。
                # 这个 readline 循环只负责**读输出**, 退出以读到 EOF 为准。
                lines_buf = []
                hit_wallclock = False
                while True:
                    if await ctx.is_cancelled():
                        if not await kill_proc(proc, ctx=ctx):
                            await ctx.on_log(
                                f"[内存] 子进程 {proc.pid} 仍在跑 —— 已交由 `timeout` "
                                f"在 {duration}s 处收尾, 本次不等它"
                            )
                        result.status = "stopped"
                        all_passed = False
                        break

                    try:
                        line = await asyncio.wait_for(proc.stdout.readline(), timeout=1.0)
                    except asyncio.TimeoutError:
                        # 只提示一次, **既不 break 也不 terminate** —— `timeout` 才是
                        # 挂钟的唯一权威, 它收尾后进程退出, 这里自然读到 EOF。
                        if not hit_wallclock and time.time() - t0 > duration * 0.95:
                            hit_wallclock = True
                            await ctx.on_log(f"[内存] ⏰ 挂钟 {duration}s 快到 —— 由 `timeout` 收尾")
                        continue

                    if not line:
                        break
                    text = line.decode(errors="replace").rstrip()
                    lines_buf.append(text)
                    if len(lines_buf) > 200:
                        lines_buf = lines_buf[-200:]
                    await ctx.on_log(f"  {text}")

                    # memtester 阶段提示
                    m = re.search(r"^\s*(\w[\w\s]+?)\s*:.*?(\d+%)", text)
                    if m:
                        phase, pct = m.group(1).strip(), m.group(2)
                        try:
                            phase_pct = int(pct.rstrip("%"))
                        except ValueError:
                            phase_pct = 0
                        overall = (i + phase_pct / 100) / times * 100
                        await ctx.on_progress(int(overall), f"{phase} {pct}")

                # 挂钟必须**无条件**算 —— 旧代码只在进度分支里赋值, 一旦 memtester
                # 输出里没匹配到百分比行, 下面的 elapsed 会 NameError。
                elapsed = time.time() - t0
                result.log = "\n".join(lines_buf)

                if result.status == "stopped":
                    # 立刻返回, **不 await proc.wait()** —— 收尾失败时进程还活着
                    # (root 子进程发不了信号, 见 kill_proc), 等它等于干等到
                    # `timeout` 到点。用户点了「停止」却要再等几十秒, 是最糟的卡。
                    return result

                rc = await proc.wait() if proc.returncode is None else proc.returncode

                # memtester 退出码 + 特定失败模式双重判断
                if rc != 0 and not await ctx.is_cancelled():
                    # `timeout` 到点收尾时退出码是 124 (GNU coreutils 约定),
                    # 说明挂钟跑满了, 是**预期**行为, 不算 fail。
                    timed_out = (rc == 124)
                    ran_full = timed_out or elapsed >= duration * 0.9
                    if ran_full:
                        why = "`timeout` 到点收尾" if timed_out else "挂钟已跑满"
                        await ctx.on_log(f"[内存] 跑满挂钟 {elapsed:.0f}s ({why}, rc={rc})")
                        # 不 break, 继续到下面的 ok_count 统计
                    else:
                        await ctx.on_log(f"[内存] ❌ memtester 返回 {rc} (跑了 {elapsed:.0f}s)")
                        result.error = f"memtester 退出码 {rc}"
                        result.status = "failed"
                        all_passed = False
                        break

                # memtester 输出格式 —— **本机是 4.3.0, 别用老版本的正则**
                #   4.x:   "  Stuck Address       : <进度>ok"  (进度用退格符原地重绘)
                #   1.x:   "test 1: mlock  1MB  1 touch ok"
                # 老代码只认 1.x 的 "test N:" → 在 4.x 上 ok_count 恒为 0,
                # 更糟的是 FAIL 分支也永远不触发 —— **内存真坏了照样判 passed**。
                # 注意**不能**加前导 \b: memtester 4.x 打印的是
                # "setting 3FAILURE at ..." —— "3" 和 "FAILURE" 之间没有词边界,
                # \bFAIL 匹配不上 (这就是老代码漏检的同一类错)。
                fail_line = re.search(r"FAIL(?:URE)?\b", result.log, re.MULTILINE)
                if fail_line:
                    await ctx.on_log(
                        f"[内存] ❌ memtester 报失败: {fail_line.group(0).strip()[:200]}"
                    )
                    result.error = f"memtester 失败: {fail_line.group(0).strip()[:200]}"
                    result.status = "failed"
                    all_passed = False
                    break

                # 算 pass 的测试数: "  Test Name      : ...ok" 一行算一项
                ok_count = len(re.findall(
                    r"^\s*\S[^\n:]*:\s*[^\n]*\bok\b", result.log, re.MULTILINE
                ))

                # **一项都没解析出来 = 我们根本没能力判定内存好坏** → error, 不判 passed。
                # §9.3 点名的危险失效模式: 静默的"通过"比明确的"失败"有害得多。
                if ok_count == 0:
                    result.status = "error"
                    result.error = (
                        f"memtester 一项 ok 都没解析到 —— 无法判定内存好坏。"
                        f"(输出前 3 行: {result.log.splitlines()[:3]})"
                        f"可能是 memtester 版本/输出格式变了, 或根本没跑起来。"
                    )
                    await ctx.on_log(f"[内存] ✗ {result.error}")
                    return result

                await ctx.on_log(f"[内存]   {ok_count} 项测试全部 ok ✓")
                result.metrics[f"round_{round_idx}_ok_tests"] = ok_count
                result.metrics[f"round_{round_idx}_size_mb"] = size_mb

            except FileNotFoundError:
                result.status = "error"
                result.error = "未找到 memtester (PATH 问题)"
                return result
            except Exception as e:
                result.status = "error"
                result.error = f"异常: {e!r}"
                return result

            result.metrics[f"round_{round_idx}_size_mb"] = size_mb

        if all_passed:
            result.status = "passed"
        elif result.status == "pending":
            result.status = "failed"

        await ctx.on_progress(100, "完成")
        return result
