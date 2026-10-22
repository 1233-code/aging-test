"""WiFi 测试 —— 软件开关 + 射频检查 + 扫描 (§6.4)

4 步顺序执行, 每步都有明确的失败归因:

  ① 软件开关响应  ip link set down → 等 1s → ip link set up
                   两句都退出 0 才算驱动响应了命令
  ② 状态确认      ip link show <iface> → state UP (操作确实生效)
  ③ 射频检查      rfkill list wifi → 无 `blocked: yes` (软/硬都没被屏蔽)
  ④ 扫描执行      iw dev <iface> scan (≤15s), 走 4a-4d 降级链

**为什么 ④ 要有降级链 —— 环境问题 ≠ 设备问题**:
  `iw dev wlan0 scan` 在周边一个 AP 都没有的环境下, 驱动照样正常完成扫描,
  输出 BSS 表头 + `scan completed`, **退出码 0**, 只是结果列表为空。
  这条命令成功跑完 = 射频通路已工作。扫不到 SSID 仅仅是环境里没 AP,
  不该判设备坏。所以:

    4a 首次扫描        扫到 ≥1 SSID                    → passed
    4b 重试            仍未命中, 间隔 5s 重试, 最多 2 次 (共 3 次)
    4c 命令级降级      0 SSID 但 rc=0 且输出含扫描完成标志 → passed
                       + degraded=true + "环境无 AP, 射频通路已验证"
    4d 驱动级失败      报错 / 非 0 退出 / 超时 / 无扫描完成标志
                       → failed, **不设降级** —— 驱动报错就是驱动坏了, 必须暴露

**为什么不连 AP / 不验 WPA**: 凭证管理、握手超时、AP 型号差异都会引入大量
额外变量, 反而降低可重复性。屏蔽这层换来「不依赖特定 AP / 不依赖凭证 /
老化阶段纯驱动检查」的稳定测试。WPA 层缺陷率约 5%, 出货后现场报修成本可控。

**为什么不卡 RSSI 阈值**: 工位信号强度受隔墙/金属工位/邻机干扰影响很大,
卡固定 dBm 会把"能用但现场信号弱"误判为不合格。本项只验「驱动/射频/扫描」
三件事全通。
"""
import asyncio
import os
import re
import time
from typing import List, Tuple

from .base import TestCase, TestContext, TestResult, register
from ._tools import find_tool, find_wifi_iface, with_sudo, kill_proc, sudo_hint_if_unavailable

# §11.3: 精确正则, 不做 substring 匹配。
# **注意**: 行内空白一律用 `[ \t]` 不用 `\s` —— Python 的 `\s` 包含 \n,
# 用 `\s*` 会跨行, 把下一行的内容吃进来 (空 SSID 行曾把 "scan completed"
# 当成 SSID 读走)。
#
# `ip link show` 里 "UP" 同时出现在 flags 的 <NO-CARRIER,...,UP> 里和
# "state UP" 里, 所以只认 state 后面的取值。
_RE_LINK_STATE = re.compile(
    r"\bstate[ \t]+(UP|DOWN|UNKNOWN|DORMANT|LOWERLAYERDOWN|TESTING)\b"
)
_RE_RFKILL_BLOCKED = re.compile(
    r"^[ \t]*(?:Soft|Hard)[ \t]+blocked:[ \t]*(yes|no)[ \t]*$", re.MULTILINE
)
# 扫描完成标志 —— 4c 降级通道的判定依据 (三个任一)
_RE_SCAN_COMPLETED = re.compile(
    r"^[ \t]*scan[ \t]+completed[ \t]*$", re.MULTILINE | re.IGNORECASE
)
_RE_BSS_HEADER = re.compile(r"^BSS[ \t]+[0-9a-f]{2}(?::[0-9a-f]{2}){5}", re.MULTILINE)
_RE_RESULTS = re.compile(r"^[ \t]*Results:[ \t]*$", re.MULTILINE)
# 每条 BSS 的 SSID 行 —— 缩进后跟 "SSID: xxx"
_RE_SSID_LINE = re.compile(r"^[ \t]+SSID:[ \t]*(.*?)[ \t]*$", re.MULTILINE)


def _scan_completed(text: str) -> bool:
    """输出是否表明扫描真的跑完了 —— 4c 的判定依据。"""
    return bool(
        _RE_SCAN_COMPLETED.search(text)
        or _RE_BSS_HEADER.search(text)
        or _RE_RESULTS.search(text)
    )


def _parse_ssids(text: str) -> List[str]:
    """提取扫描到的 SSID 列表 —— 按 BSS 分段, 只认 BSS 段内的 SSID 行。

    直接数 `SSID:` 行会把 `iw dev wlan0` 开头那行接口自身的 SSID 也算进去,
    所以先按 BSS 头切段, 再在每段里取 SSID。
    """
    hits = list(_RE_BSS_HEADER.finditer(text))
    out: List[str] = []
    for i, m in enumerate(hits):
        end = hits[i + 1].start() if i + 1 < len(hits) else len(text)
        s = _RE_SSID_LINE.search(text, m.end(), end)
        if s and s.group(1):
            out.append(s.group(1))
    return out


@register
class WiFiTest(TestCase):
    name = "wifi"
    display_name = "WiFi 验证"
    description = "软件开关 + 射频检查 + 扫描 (环境无 AP 时走降级通道)"

    def __init__(self):
        self.proc = None
        self.cancel_done = False

    async def cancel(self):
        """runner.stop() 会 await 到这里 —— 让正在跑的 iw scan 立刻停下。"""
        if self.proc and self.proc.returncode is None and not self.cancel_done:
            self.cancel_done = True
            # 不用裸 terminate/kill —— 子进程是 sudo 起的 root 进程, 普通用户
            # 对它发信号必然 EPERM, 裸调会把收尾自己炸掉, 还让 root 测试进程
            # 泄漏在后台。见 _tools.kill_proc 的说明。
            await kill_proc(self.proc, wait_sec=2.0)

    async def _run(self, cmd: List[str], timeout: float) -> Tuple[int, str, str]:
        """跑一条已包好 sudo 的命令, 返回 (rc, stdout, stderr). 超时 kill."""
        self.cancel_done = False
        try:
            self.proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.DEVNULL,
            )
        except FileNotFoundError:
            return -1, "", f"command not found: {' '.join(cmd)}"
        try:
            out, err = await asyncio.wait_for(self.proc.communicate(), timeout=timeout)
            rc = self.proc.returncode if self.proc.returncode is not None else -1
            return rc, out.decode(errors="replace"), err.decode(errors="replace")
        except asyncio.TimeoutError:
            await self.cancel()
            return -1, "", f"timeout after {timeout}s"

    async def run(self, ctx: TestContext) -> TestResult:
        """按 `times` 跑多轮, 每轮一整套 §6.4 的 4 步 (开关→状态→射频→扫描)。

        老化场景下反复开关射频 + 反复扫描, 比只跑一次更容易暴露**间歇性**驱动
        问题 (第 2 轮才 scan 不出来 / 第 3 轮 ip link 不 UP)。默认仍是 1 轮,
        出货口径不变。
        """
        times = max(1, int(ctx.test_config.get("times", 1)))
        if times == 1:
            return await self._run_once(ctx)      # 单轮 = 原行为, 一行都不绕

        agg = TestResult()
        agg.metrics = {}
        await ctx.on_log(
            f"[WiFi] 按配置跑 {times} 轮 (每轮: 软件开关 → 状态 → 射频 → 扫描)"
        )
        done = 0
        for i in range(times):
            if await ctx.is_cancelled():
                await ctx.on_log("[WiFi] 用户取消")
                agg.status = "stopped"
                agg.metrics["rounds_done"] = done
                return agg

            idx = i + 1
            await ctx.on_log(f"[WiFi] ── 第 {idx}/{times} 轮 ──")
            await ctx.on_progress(int((i / times) * 100), f"第 {idx}/{times} 轮")

            r = await self._run_once(ctx)
            done += 1
            # 顶层指标沿用最后一轮 (与其他多轮项一致), 逐轮另存 round_N_* 留档
            agg.metrics.update(r.metrics or {})
            for k, v in (r.metrics or {}).items():
                agg.metrics[f"round_{idx}_{k}"] = v
            agg.log = r.log
            agg.status = r.status
            agg.error = r.error

            if r.status != "passed":
                # failed / error / skipped 任一 → 后面几轮跑完也是同一个结论,
                # 直接停。与 §6.3 GPU「任一轮失败即整项 failed」同一口径。
                await ctx.on_log(
                    f"[WiFi] 第 {idx}/{times} 轮 = {r.status}, 提前结束 "
                    f"(剩余 {times - done} 轮不跑)"
                )
                break
            await ctx.on_log(f"[WiFi] ✓ 第 {idx}/{times} 轮通过")

        agg.metrics["rounds_done"] = done
        agg.metrics["rounds_configured"] = times
        await ctx.on_progress(100, "完成")
        return agg

    async def _wait_state_up(
        self, ctx: TestContext, ip_bin: str, iface: str, timeout: float
    ) -> Tuple[str, int]:
        """轮询 `ip link show` 直到 state=UP, 返回 (最终 state, 等待毫秒)。

        **为什么必须轮询, 不能固定 sleep —— 2026-10-22 实测踩过的坑**:
        无线驱动在 `ip link set up` 之后还要初始化射频, 这段时间内核报
        `state DORMANT` (= 接口已 up, 链路未就绪), 是**正常中间态**。
        本机实测 DORMANT→UP 要 **3.22 秒**, 而原先固定等 `switch_settle_sec`
        (1.0s) 就去读 —— 必然读到 DORMANT 判 fail, run 43/44/47 全倒在这。

        改轮询后驱动初始化多慢都能过, 只有真超时才 fail, 而且报错能区分
        「没到」和「到了但状态不对」。

        ⚠️ `mode DORMANT` 与 `state DORMANT` 是两回事: 前者在 `ip link show`
        行的 `mode` 后面 (射频省电模式, 正常), 后者才是链路状态。正则只锚
        `state`, 不会误取 —— 已用本机真实输出逐条验证过。
        """
        deadline = time.monotonic() + timeout
        t0 = time.monotonic()
        state = "UNKNOWN"
        while True:
            if await ctx.is_cancelled():
                return state, int((time.monotonic() - t0) * 1000)
            rc, out, _ = await self._run(
                with_sudo([ip_bin, "link", "show", iface]), 5
            )
            if rc == 0:
                m = _RE_LINK_STATE.search(out)
                state = m.group(1) if m else "UNKNOWN"
            if state == "UP":
                return state, int((time.monotonic() - t0) * 1000)
            if time.monotonic() >= deadline:
                return state, int((time.monotonic() - t0) * 1000)
            await asyncio.sleep(0.3)

    async def _run_once(self, ctx: TestContext) -> TestResult:
        result = TestResult()
        t_cfg = ctx.test_config

        # 接口名: 显式配置优先。**只在留空时**才自动探测 —— 一块板上可能有
        # wlan0/wlan1 两块无线, 自动探测按字典序取第一个, 未必是产线要测的那块。
        # 曾经写成 `iface == "wlan0"` 也触发探测, 会让操作员显式指定的值被覆盖。
        iface = (t_cfg.get("iface")
                 or ctx.config.get("interfaces", {}).get("wifi") or "").strip()
        if not iface:
            iface = find_wifi_iface() or "wlan0"

        settle = float(t_cfg.get("switch_settle_sec", 1.0))
        state_timeout = float(t_cfg.get("state_timeout_sec", 10.0))
        scan_timeout = float(t_cfg.get("scan_timeout_sec", 15))
        scan_retries = int(t_cfg.get("scan_retry_count", 2))    # 4b: 额外重试次数
        scan_retry_gap = float(t_cfg.get("scan_retry_gap_sec", 5))

        # ── 工具预检 (§11.1 find_tool) ──
        ip_bin = find_tool("ip")
        rfkill_bin = find_tool("rfkill")
        iw_bin = find_tool("iw")
        missing = [n for n, b in (("ip", ip_bin), ("rfkill", rfkill_bin),
                                 ("iw", iw_bin)) if not b]
        if missing:
            result.status = "error"
            result.error = (
                f"未找到: {', '.join(missing)}. 解决: sudo bash scripts/install_deps.sh "
                "(见 docs/需求设计.md §11.7)"
            )
            await ctx.on_log(f"[WiFi] ✗ {result.error}")
            return result

        # §11.2: 需要 root 的调用走命令级 sudo -n (服务本身不提权启动)。
        # sudo 不可用 → 判 error, **不**静默降级成 skipped —— 那会让工人
        # 看到"通过"而实际没测, 是产线上最危险的一类故障 (§9.3)。
        if not sudo_hint_if_unavailable(result, "WiFi"):
            await ctx.on_log(f"[WiFi] ✗ {result.error}")
            return result

        if not os.path.isdir(f"/sys/class/net/{iface}"):
            result.status = "error"
            detected = find_wifi_iface() or "(找不到)"
            result.error = (
                f"接口 {iface} 不存在. 自动找: {detected}. "
                f"原因: 1) 没 WiFi 硬件 2) skw 驱动未加载 3) 接口名不同"
            )
            await ctx.on_log(f"[WiFi] ✗ {result.error}")
            return result

        await ctx.on_log(
            f"[WiFi] 接口={iface} | 工具: ip={ip_bin}, rfkill={rfkill_bin}, iw={iw_bin}"
        )
        result.metrics["iface"] = iface

        async def fail_at(step: int, reason: str):
            """任一步失败 → failed, 并在 step_failed 里指出是哪一步 (§6.4)。"""
            result.status = "failed"
            result.metrics["step_failed"] = step
            result.metrics["step_failed_reason"] = reason[:400]
            result.error = f"第 {step} 步失败: {reason[:300]}"
            await ctx.on_log(f"[WiFi] ❌ {result.error}")

        # ══════════ ① 软件开关响应 ══════════
        await ctx.on_progress(10, "① 软件开关响应")
        await ctx.on_log(
            f"[WiFi] ① 软件开关响应 — ip link set down → 等 {settle}s → set up"
        )
        rc, _, err = await self._run(
            with_sudo([ip_bin, "link", "set", iface, "down"]), 5
        )
        if rc != 0:
            await fail_at(1, f"`ip link set {iface} down` 退出码 {rc}: "
                            f"{(err or '').strip()[:200]}")
            await ctx.on_progress(100, "失败")
            return result

        if await ctx.is_cancelled():
            result.status = "stopped"
            return result

        await asyncio.sleep(settle)
        rc, _, err = await self._run(
            with_sudo([ip_bin, "link", "set", iface, "up"]), 5
        )
        if rc != 0:
            await fail_at(1, f"`ip link set {iface} up` 退出码 {rc}: "
                            f"{(err or '').strip()[:200]}")
            await ctx.on_progress(100, "失败")
            return result
        await ctx.on_log("[WiFi]   ✓ down / up 两句都退出 0 —— 驱动响应了命令")
        await ctx.on_progress(25, "① 开关 OK")

        # ══════════ ② 状态确认 (轮询等 UP, 不是固定 sleep) ══════════
        await ctx.on_log(
            f"[WiFi] ② 状态确认 — 轮询 `ip link show` 直到 state=UP "
            f"(上限 {state_timeout}s, 每 0.3s 一次)"
        )
        state, waited_ms = await self._wait_state_up(ctx, ip_bin, iface, state_timeout)
        result.metrics["ip_link_state"] = state
        result.metrics["state_wait_ms"] = waited_ms

        if state != "UP":
            if await ctx.is_cancelled():
                result.status = "stopped"
                await ctx.on_log("[WiFi] ⏹ 用户取消 (轮询等 UP 期间)")
                return result
            await fail_at(
                2,
                f"`set up` 后 {waited_ms / 1000:.1f}s 内 state 始终是 '{state}', "
                f"不是 'UP'。DORMANT = 接口已 up 但射频仍在初始化, 属正常中间态; "
                f"DOWN/UNKNOWN 才说明开关没生效或驱动异常。"
            )
            await ctx.on_progress(100, "失败")
            return result
        await ctx.on_log(
            f"[WiFi]   ✓ state=UP (驱动就绪耗时 {waited_ms}ms) —— 操作实际生效"
        )
        await ctx.on_progress(45, "② 状态 UP")

        # ══════════ ③ 射频检查 ══════════
        await ctx.on_log("[WiFi] ③ 射频检查 — rfkill list wifi")
        rc, out, err = await self._run(with_sudo([rfkill_bin, "list", "wifi"]), 5)
        blocked = "no"
        if rc != 0:
            # rfkill 报错本身不判 failed: 有些内核没有 rfkill 支持, 而 ① ② 已经
            # 证明射频通路响应了命令。记 unknown 交给产线人工看, 不据此否掉整项。
            blocked = "unknown"
            await ctx.on_log(
                f"[WiFi]   ⚠ rfkill list 退出码 {rc} ({(err or '').strip()[:120]}), "
                f"记为 unknown —— ① ② 已证明射频通路工作, 不据此判失败"
            )
        else:
            # 精确解析: 逐条读 Soft/Hard blocked, 任一 yes 即视为被屏蔽
            flags = _RE_RFKILL_BLOCKED.findall(out)
            blocked = "yes" if "yes" in flags else "no"
            if blocked == "yes":
                await ctx.on_log(
                    f"[WiFi]   射频被屏蔽 (soft/hard blocked: yes), 尝试 unblock"
                )
                await self._run(with_sudo([rfkill_bin, "unblock", "wifi"]), 3)
                rc2, out2, _ = await self._run(
                    with_sudo([rfkill_bin, "list", "wifi"]), 5
                )
                if rc2 == 0:
                    flags2 = _RE_RFKILL_BLOCKED.findall(out2)
                    blocked = "yes" if "yes" in flags2 else "no"

        result.metrics["rfkill_blocked"] = blocked
        if blocked == "yes":
            await fail_at(3, "rfkill 报 blocked: yes —— 射频被软/硬屏蔽, "
                            "且 unblock 后仍屏蔽")
            await ctx.on_progress(100, "失败")
            return result
        await ctx.on_log(f"[WiFi]   ✓ 射频未被屏蔽 (rfkill_blocked={blocked})")
        await ctx.on_progress(60, "③ 射频 OK")

        # ══════════ ④ 扫描执行 (4a-4d 降级链) ══════════
        await ctx.on_log(
            f"[WiFi] ④ 扫描 — iw dev {iface} scan (超时 {scan_timeout}s, "
            f"最多 {scan_retries + 1} 次)"
        )
        await ctx.on_progress(70, "④ 扫描中")

        ssids: List[str] = []
        scan_cmd_ok = False
        last_out = ""
        last_err = ""
        retries_done = 0
        scan_ms = 0

        for attempt in range(scan_retries + 1):
            if await ctx.is_cancelled():
                result.status = "stopped"
                return result
            if attempt > 0:
                retries_done = attempt
                await ctx.on_log(
                    f"[WiFi]   4b 重试 {attempt}/{scan_retries} —— "
                    f"间隔 {scan_retry_gap}s 排除射频刚唤醒的抖动"
                )
                await asyncio.sleep(scan_retry_gap)

            t0 = time.time()
            # **必须带 timeout_sec**: 部分驱动的 `iw scan` 在射频异常时会一直
            # 挂着不返回。而它被 §11.2 提权成 root 进程后, 我们 (普通用户)
            # 发不了信号收它 —— 只能让同为 root 的 `timeout` 到点自己杀它,
            # 否则测试任务永不结束, 连服务关机都会被拖住。
            rc, out, err = await self._run(
                with_sudo([iw_bin, "dev", iface, "scan"], timeout_sec=scan_timeout),
                scan_timeout + 8,
            )
            scan_ms = int((time.time() - t0) * 1000)
            last_out, last_err = out, err
            done = _scan_completed(out)
            scan_cmd_ok = (rc == 0) and done
            ssids = _parse_ssids(out)

            await ctx.on_log(
                f"[WiFi]   扫描 #{attempt + 1}: rc={rc}, {scan_ms}ms, "
                f"SSID×{len(ssids)}, 扫描完成标志={'有' if done else '无'}"
            )
            if ssids:            # 4a 命中 → 直接过, 不再重试
                break
            if scan_cmd_ok:      # 4c 候选 → 仍按 4b 重试确认不是抖动
                continue

        result.metrics["scan_duration_ms"] = scan_ms
        result.metrics["scan_retry_count"] = retries_done
        result.metrics["scan_cmd_ok"] = scan_cmd_ok
        result.metrics["scan_ssid_count"] = len(ssids)
        result.metrics["top_ssid"] = ssids[0] if ssids else ""
        result.metrics["degraded"] = False

        if ssids:                                        # 4a
            result.status = "passed"
            await ctx.on_log(
                f"[WiFi]   ✓ 4a 扫到 {len(ssids)} 个 SSID —— 射频通路已验证"
            )
        elif scan_cmd_ok:                                # 4c 命令级降级
            result.status = "passed"
            result.metrics["degraded"] = True
            result.metrics["degraded_reason"] = "环境无 AP, 射频通路已验证"
            await ctx.on_log(
                f"[WiFi]   ✓ 4c 降级通过 —— 周边无 AP, 但驱动成功完成了一次射频扫描"
                f" (rc=0 + 扫描完成标志)。设备能力没问题, 是工位环境没 AP。"
            )
        else:                                            # 4d 驱动级失败, 无降级
            detail = (last_err or last_out).strip()
            if "timeout" in last_err:
                detail = f"扫描超时 {scan_timeout}s (共试 {retries_done + 1} 次)"
            await fail_at(4, f"`iw dev {iface} scan` 驱动级失败: {detail[:300]}")
            await ctx.on_progress(100, "失败")
            return result

        await ctx.on_progress(100, "完成")
        return result
