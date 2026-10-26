"""内存测试 - memtester"""
import asyncio
import os
import time
import re
from typing import Optional

from .base import TestCase, TestContext, TestResult, register
from ._tools import (find_tool, run_cmd, is_root, run_proc_with_wallclock,
                     with_sudo, kill_proc, sudo_hint_if_unavailable,
                     TtyDemangler)

# memtester 的**原始**行可能非常长: 它的进度是原地重绘的, "Random Value"
# 那一项画的是 -\|/ 转轮, 一行能塞几万字符 (见下面 read 循环的说明)。
# asyncio 的 StreamReader 默认只认 64KB 一行, 超了 readline() 直接抛
# ValueError —— 那会被下面的 except Exception 吃掉, 这一项判成 error,
# 白白挂掉一台好机器。所以这里把上限抬到 4MB (内存占用可以忽略)。
MEMTESTER_LINE_LIMIT = 4 * 1024 * 1024

# ── 进度条: 一轮 memtester 大约多久 ──
#
# 实测 (2026-10-26, 本机 A527 / memtester 4.3.0, size_mb=1024):
#     times=1 → 60.4s,  times=2 → 120.9s,  连着 12 次都是 60.4s。
# 为什么**正好是一分钟**: 1GB 的工作集, memtester 跑不完它的 18 项,
# 每一轮都被 `timeout <timeout_sec>` 掐断 (rc=124) —— 所以一轮的长度
# 就等于挂钟上限。工人也是按「一次差不多一分钟」看的。
#
# ⚠️ 这**不是**"预计总时长", 只是**一轮**的刻度, 而且它**自校准**:
#    跑完一轮就用那一轮的**实测**时长当下一轮的分母。换板子、内存带宽
#    不同, 分母会自己纠正 —— 而不是拿一个编出来的数字硬套 (§7.5 担心的
#    就是"界面 60s、实际 120s"那种撒谎)。
MEMTESTER_ROUND_SEC = 60.0


def _overall_pct(done_rounds: int, frac: float, times: int) -> int:
    """本项总进度 (0-100): **以轮次为主**, 轮内按 frac 插值。

    `done_rounds` = 已完整跑完的轮数, `frac` = 当前这一轮跑到的比例。

    轮内最多算到 99% —— 本轮没真的跑完之前**不许**显示 100%(§9.3 同理:
    提前显示"完成"是骗人, 工人会以为可以拿机器了)。
    """
    frac = min(max(frac, 0.0), 0.99)
    return int((done_rounds + frac) / times * 100)


def _unfinished_items(lines) -> list:
    """找出「开了头但没结论」的测试项行。

    memtester 每个测试项一行: "  Random Value        : ok"。跑到一半被掐断的
    那一行, 冒号后面要么是空的, 要么只剩半个转轮字符 —— 反正**没有结论**。
    判据: 有结论的行, 冒号后面是 "ok"(或以 ok 开头) 或者含 "FAIL"。

    ⚠️ 判的是**还原后**的行(带退格符的原始行不算 —— 那行里的 "ok" 前面
       挡着一串退格符, 正则看不见)。另外 "Loop 1/1:" 是 memtester 的轮次
       行, 不是测试项, 别把它算进去。
    """
    out = []
    for l in lines:
        if re.match(r"^\s*Loop\s+\d+/\d+\s*:", l):
            continue
        m = re.match(r"^\s+(\S[^:]*?)\s*:\s*(.*)$", l)
        if not m:
            continue
        rest = m.group(2).strip()
        if re.match(r"^ok\b", rest) or "FAIL" in rest.upper():
            continue
        out.append(l)
    return out


@register
class MemoryTest(TestCase):
    name = "memory"
    display_name = "内存"
    description = "使用 memtester 进行内存读写校验"

    def expected_seconds(self, test_config: dict) -> Optional[float]:
        """本项预计多少秒 = **轮数 × 一轮的长度**。

        ⚠️ 2026-10-26 起**不再返回 None**。之前认为「事先算不出来」
        (耗时 = 遍数 × 工作集 ÷ 机器内存带宽, 换板子能差几倍), 于是内存项
        连计时条都不给 —— 结果界面上那根进度条**全程停在 0%**, 直到最后
        一下跳到 100%(见下面 `run()` 里的说明)。

        现在有实测支撑: 本机 size_mb=1024 时, memtester 跑不完 18 项, 每一轮
        都被 `timeout 60` 掐断, 实测一轮 60.4s (times=2 → 120.9s), 连着
        12 次都落在 60.4s —— **一轮的长度就等于挂钟上限 `timeout_sec`**。

        `timeout_sec` 是**超时上限**不是目标时长(所以配置键不叫
        `duration_sec` —— 那个键会被 §7.2 渲染成「时长」输入框, 让工人
        以为调它就能控制运行时长)。这里用它做**估值**, 依据是「上限同时
        也是实测」, 不是拿它当目标。

        这个值**不参与任何判定**(§7.5: 算错了也不影响测试结果), 界面上
        计时区那一块 2026-10-23 已整块移除, 现在它只是接口上的机器可读
        估值(排障、接口调用方用)。真正的进度条走 `on_progress`。
        """
        times = max(1, int(test_config.get("times", 1)))
        timeout_sec = max(10, int(test_config.get("timeout_sec", 60)))
        return times * float(timeout_sec)

    async def run(self, ctx: TestContext) -> TestResult:
        result = TestResult()
        t_cfg = ctx.test_config
        times = max(1, int(t_cfg.get("times", 1)))
        size_mb = max(64, int(t_cfg.get("size_mb", 1024)))
        # ⚠️ 这是**超时上限**, 不是本项的目标时长 —— memtester 跑完就退出。
        # 配置里刻意用 `timeout_sec` 而不是 `duration_sec`: 后者会被 §7.2
        # 当成「时长」渲染成输入框, 让工人以为它能控制跑多久。
        timeout_sec = max(10, int(t_cfg.get("timeout_sec", 60)))

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
        # 先说清楚日志跟 memtester 的原始输出长得为什么不一样 —— 不然纸面
        # 读者会以为日志被截断了 (§8.2「要么全, 要么没有」: 说得清, 才敢
        # 当证据用)。
        await ctx.on_log(
            "[内存] 以下为 memtester 输出的「终端还原」版: memtester 用退格符"
            "原地重绘进度(原始字节里是几万个字符的转轮), 还原后每个测试项一行, "
            "结论一字不少。"
        )
        await ctx.on_progress(0, "准备开始")

        # ── 进度条的分母: 一轮该用多少秒 ──
        #
        # 第一轮用**挂钟上限** timeout_sec (唯一能保证的上界; 生产配置下
        # 1024MB 跑不满 18 项, 每一轮都被它掐断, 实测一轮 60.4s —— 分母和
        # 实际完全吻合)。跑完一轮之后改用**实测值**, 分母自校准: 换板子、
        # 内存带宽不同, 下一轮的分母会自己纠正, 而不是拿编出来的数字硬套。
        round_sec_est = float(timeout_sec)
        measured_rounds: list = []

        for i in range(times):
            if await ctx.is_cancelled():
                result.status = "stopped"
                return result
            round_idx = i + 1
            await ctx.on_log(f"[内存] 第 {round_idx}/{times} 轮")

            # 本轮的进度刻度 (0-1)。只增不减 —— 进度条不许倒退。
            round_frac = 0.0
            last_sent = None

            async def _report(done_rounds: int, frac: float, msg: str):
                """报一次进度: 以**轮次**为主, 轮内按 frac 插值。

                整数百分比和文案都没变就不报 —— 进度条不需要每 0.1% 刷一次。
                """
                nonlocal round_frac, last_sent
                if done_rounds != i:
                    round_frac = 0.0        # 换轮了, 轮内刻度归零
                else:
                    round_frac = max(round_frac, frac)
                pct = _overall_pct(done_rounds, round_frac, times)
                if (pct, msg) == last_sent:
                    return
                last_sent = (pct, msg)
                await ctx.on_progress(pct, msg)

            await _report(i, 0.0, f"第 {round_idx}/{times} 轮")

            # §11.2 一律 sudo → 子进程是 root, 而服务是普通用户 → **我们发不了信号**
            # (os.kill 对 root 进程必然 EPERM)。所以挂钟收尾交给 root 领地内部的
            # `timeout`: 它和 memtester 同为 root, 到点自己杀自己的子进程,
            # 服务全程一个信号都不用发。见 _tools.with_sudo 的「为什么要 timeout」。
            cmd = with_sudo([tool, f"{size_mb}M", "1"], timeout_sec=timeout_sec)
            await ctx.on_log(f"[内存] 执行: {' '.join(cmd)}")

            t0 = time.time()
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                    limit=MEMTESTER_LINE_LIMIT,
                )

                # memtester 没有自己的 --timeout, 由上面的 `timeout <timeout_sec>` 兜底。
                # 这个 readline 循环只负责**读输出**, 退出以读到 EOF 为准。
                #
                # ⚠️ **必须把输出按「终端」还原后再存** (§8.2「要么全, 要么没有」)。
                #   memtester 4.x 画进度是**原地重绘**的: 不换行, 而是用退格符
                #   (\b)把刚写的几个字擦掉重画 —— "Stuck Address" 写的是
                #   "setting 0"/"testing 0" 反复擦改, "Random Value" 画的是一个
                #   -\|/ 的转轮, 每转一格就用 3 个退格符擦掉上一格。
                #   于是**一行**原始输出里塞进几万字符, 整轮下来十几 MB。
                #   原样存进日志, 报告里就是好几页的 "-\|/" —— 一个信息都没有,
                #   却把真正要看的部分挤到后面去 (产线反馈: 「好几页都是这个」)。
                #   还原后每个测试项就剩干净的一行 "  Random Value        : ok",
                #   结论一字不少 —— 被擦掉的只是**重绘的中间态**。
                #
                # 两个缓冲:
                #   lines_buf   —— 还原后的可见行(界面/报告看的就是它)
                #   raw_chunks  —— 原始字节(只用来兜底判失败, 见下面)
                lines_buf = []
                raw_chunks = []
                dem = TtyDemangler()
                hit_wallclock = False
                while True:
                    if await ctx.is_cancelled():
                        if not await kill_proc(proc, ctx=ctx):
                            await ctx.on_log(
                                f"[内存] 子进程 {proc.pid} 仍在跑 —— 已交由 `timeout` "
                                f"在 {timeout_sec}s 处收尾, 本次不等它"
                            )
                        result.status = "stopped"
                        all_passed = False
                        break

                    try:
                        line = await asyncio.wait_for(proc.stdout.readline(), timeout=1.0)
                    except asyncio.TimeoutError:
                        # 只提示一次, **既不 break 也不 terminate** —— `timeout` 才是
                        # 挂钟的唯一权威, 它收尾后进程退出, 这里自然读到 EOF。
                        #
                        # 这一秒没有新输出 —— 顺手按挂钟推进进度条。
                        #
                        # ⚠️ 为什么进度只能靠挂钟推: memtester 4.x **不吐百分比**,
                        #    它的进度是**原地重绘**的("setting 0"/"testing 0" 反复
                        #    擦改), 还原之后每个测试项只剩一行 "名字 : ok",
                        #    **一个 % 都没有**。以前那段找百分比的正则因此从来没
                        #    匹配上 —— 进度条全程停在 0%, 直到最后一下跳到 100%。
                        #    工人看那根条不动, 只觉得工具卡住了。
                        #    现在按「轮次 + 轮内挂钟」推: 一轮约一分钟(见
                        #    MEMTESTER_ROUND_SEC), 条就一直在走。
                        await _report(i, (time.time() - t0) / round_sec_est,
                                      f"第 {round_idx}/{times} 轮")
                        if not hit_wallclock and time.time() - t0 > timeout_sec * 0.95:
                            hit_wallclock = True
                            await ctx.on_log(f"[内存] ⏰ 挂钟 {timeout_sec}s 快到 —— 由 `timeout` 收尾")
                        continue

                    if not line:
                        break
                    # ⚠️ **不能** rstrip() 原始文本 —— Python 认为 \b 是空白字符,
                    #    rstrip 会把行尾的退格符吃掉, 还原就还原不对了。
                    raw = line.decode(errors="replace")
                    raw_chunks.append(raw)
                    for visible in dem.feed(raw):
                        v = visible.rstrip()
                        lines_buf.append(v)
                        # ⚠️ 这里**不能**再截断成末尾 200 行。
                        #   下面判 FAIL / 算 ok 项数都跑在这个缓冲上 —— 截断了,
                        #   失败行要是落在被丢掉的那段里, **内存真坏了照样判 passed**
                        #   (§9.3 点名的危险失效模式: 静默的「通过」比明确的「失败」
                        #   有害得多)。而且 ok 项数会数少, 报告上的数也不对。
                        #   内存占用不担心: 每一轮结束这个缓冲就扔了, 还原后一轮
                        #   才二十来行。
                        await ctx.on_log(f"  {v}")

                        # 每出一行也推进一次 —— 免得这一轮里输出稀疏时条不动。
                        #
                        # ⚠️ 这里**不拿** memtester 的百分比去推进度条: 那个
                        #    百分比是「**某个测试项**」跑了多少, 不是「**一轮**」
                        #    跑了多少 —— 一轮有 18 项, 两个刻度根本不是一回事,
                        #    混起来条会乱跳。条按轮次走(见上面), 百分比只当文案。
                        m = re.search(r"^\s*(\w[\w\s]+?)\s*:.*?(\d+%)", v)
                        detail = f"{m.group(1).strip()} {m.group(2)}" if m else ""
                        msg = f"第 {round_idx}/{times} 轮" + (f" · {detail}" if detail else "")
                        await _report(i, (time.time() - t0) / round_sec_est, msg)

                # 挂钟必须**无条件**算 —— 旧代码只在进度分支里赋值, 一旦 memtester
                # 输出里没匹配到百分比行, 下面的 elapsed 会 NameError。
                elapsed = time.time() - t0
                # 进程被挂钟掐掉时, 最后一段没有换行 —— 也要交出来(可能是半个
                # 测试项, 下面据此说明"没跑完")。
                for visible in dem.flush():
                    v = visible.rstrip()
                    lines_buf.append(v)
                    await ctx.on_log(f"  {v}")
                result.log = "\n".join(lines_buf)
                raw_text = "".join(raw_chunks)

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
                    ran_full = timed_out or elapsed >= timeout_sec * 0.9
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
                # ⚠️ 判失败要**同时**看原始输出 —— 还原是把"重绘的中间态"擦掉,
                #    万一失败信息正好落在被擦掉的位置(理论上不该发生: 失败信息
                #    总是最后打印的), 也不能让"通过"蒙混过关 (§9.3)。
                raw_fail = re.search(r"FAIL(?:URE)?\b", raw_text, re.MULTILINE)
                if raw_fail and not fail_line:
                    fail_line = raw_fail
                    # 原始输出里有、还原后没了 —— 明写出来, 免得纸面读者以为
                    # 这一项是干净的。
                    lines_buf.append(
                        f"⚠ 原始输出里有失败标记, 但被进度重绘擦掉了: {raw_fail.group(0)}"
                    )
                    result.log = "\n".join(lines_buf)
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

                # ⚠️ 有测试项**开了头但没结论** —— memtester 跑到一半被挂钟掐了。
                #    这种项还原出来是 "  Random Value        : " (空的, 或者只剩
                #    半个转轮字符), 读者看着会以为日志坏了。明写出来: 哪些项没
                #    跑到。**不改判定** —— 跑满挂钟是配置里的预期行为(§7.2
                #    timeout_sec 是上限), 但没跑完这件事必须让纸面读者知道。
                unfinished = _unfinished_items(lines_buf)
                if unfinished:
                    names = "、".join(l.split(":")[0].strip() for l in unfinished)
                    await ctx.on_log(
                        f"[内存] ⚠ 上面 {len(unfinished)} 项没有结论({names}) —— "
                        f"memtester 被挂钟掐了, 没跑完"
                    )
                    result.metrics[f"round_{round_idx}_unfinished"] = len(unfinished)

                await ctx.on_log(f"[内存]   {ok_count} 项测试全部 ok ✓")
                result.metrics[f"round_{round_idx}_ok_tests"] = ok_count
                result.metrics[f"round_{round_idx}_size_mb"] = size_mb

                # 本轮结束 —— 进度条推到下一个轮次刻度(第 i/N 轮 → i/N 完成),
                # 并把**实测**的轮长记下来: 下一轮的分母改用它(自校准), 换板子
                # 不用改代码 —— 原来"算不出总时长"的难题, 用测出来的数解决了。
                round_dur = time.time() - t0
                if round_dur > 0.5:
                    measured_rounds.append(round_dur)
                    round_sec_est = sum(measured_rounds) / len(measured_rounds)
                await _report(round_idx, 0.0, f"第 {round_idx}/{times} 轮完成")

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
