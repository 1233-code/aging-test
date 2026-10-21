"""内存测试 - memtester"""
import asyncio
import os
import time
import re

from .base import TestCase, TestContext, TestResult, register
from ._tools import find_tool, run_cmd, is_root, run_proc_with_wallclock, with_sudo, sudo_hint_if_unavailable


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

            cmd = with_sudo([tool, f"{size_mb}M", "1"])
            await ctx.on_log(f"[内存] 执行: {' '.join(cmd)}")

            t0 = time.time()
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
                )

                # memtester 没有 --timeout,用 wall-clock 控制
                lines_buf = []
                while True:
                    if await ctx.is_cancelled():
                        proc.terminate()
                        try:
                            await asyncio.wait_for(proc.wait(), 3)
                        except asyncio.TimeoutError:
                            proc.kill()
                        result.status = "stopped"
                        all_passed = False
                        break

                    try:
                        line = await asyncio.wait_for(proc.stdout.readline(), timeout=1.0)
                    except asyncio.TimeoutError:
                        # 检查超时
                        if time.time() - t0 > duration:
                            proc.terminate()
                            try:
                                await asyncio.wait_for(proc.wait(), 3)
                            except asyncio.TimeoutError:
                                proc.kill()
                            await ctx.on_log(f"[内存] ⏰ 超过时长 {duration}s,终止")
                            break
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
                        elapsed = time.time() - t0
                        overall = (i + phase_pct / 100) / times * 100
                        await ctx.on_progress(int(overall), f"{phase} {pct}")

                rc = await proc.wait() if proc.returncode is None else proc.returncode
                result.log = "\n".join(lines_buf)

                if result.status == "stopped":
                    return result

                # memtester 退出码 + 特定失败模式双重判断
                if rc != 0 and not await ctx.is_cancelled():
                    # 重要: 如果 elapsed >= duration 的 90%, 说明是挂钟到了我们 SIGTERM 杀的
                    # 这种情况 rc=-15 是预期的, 不算 fail (memtester 跑满了)
                    ran_full = elapsed >= duration * 0.9
                    if ran_full:
                        await ctx.on_log(
                            f"[内存] 跑满挂钟 {elapsed:.0f}s (被我们 SIGTERM 杀, rc={rc})"
                        )
                        # 不 break, 继续到下面的 ok_count 统计
                    else:
                        await ctx.on_log(f"[内存] ❌ memtester 返回 {rc} (跑了 {elapsed:.0f}s)")
                        result.error = f"memtester 退出码 {rc}"
                        result.status = "failed"
                        all_passed = False
                        break

                # memtester 内部错误检测:
                #   在 PASS 输出里, 每行 "test N:  ..." 都以 "ok" 结尾
                #   在 FAIL 输出里, 某行会以 "FAIL" 结尾 (大写)
                fail_line = re.search(
                    r"^\s*test\s+\d+:[^\n]*\bFAIL\b",
                    result.log,
                    re.MULTILINE,
                )
                if fail_line:
                    await ctx.on_log(
                        f"[内存] ❌ memtester 报失败: {fail_line.group(0).strip()[:200]}"
                    )
                    result.error = f"memtester 失败: {fail_line.group(0).strip()[:200]}"
                    result.status = "failed"
                    all_passed = False
                    break

                # 算 pass 的测试数
                ok_count = len(re.findall(r"^\s*test\s+\d+:[^\n]*\bok\b", result.log, re.MULTILINE))
                await ctx.on_log(f"[内存]   全部 {ok_count} 项测试通过 ✓")
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
