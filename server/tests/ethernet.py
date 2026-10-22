"""网口测试 - 链路 + DHCP + 出网 (§6.6)

3 步顺序执行, 每步都有环境降级通道:

  ① 链路检测  ethtool <iface> → `Link detected: yes`
               **纯 PHY 层, 零网络依赖** —— 交换机/DHCP/外网全挂也照样能测
  ② 取 IP      2a udhcpc -i <iface> (≤ dhcp_timeout_sec) 拿到 lease
               2b 失败 → 静态兜底 `ip addr add <static_ip>` + `ip link set up`
               2c 两手都不行 → ②③ 判 skipped (env: no_ip)
  ③ 出网      三级降级, 任一级通过即算通过:
               优先级 1  默认网关 (从 `ip route` 取) —— 不受 DNS/公网波动影响
               优先级 2  DNS 服务器 (ping_dns)
               优先级 3  公网域名 (ping_target)

**核心原则 —— 环境问题 ≠ 设备问题** (呼应 §6.4 WiFi 同款设计):
  `failed` 只留给能明确归因到设备的情况:
    - ① 链路不通 (网线/PHY 问题)
    - 静态 IP 配上了仍 ping 不通网关 (网络栈/二层异常), 无兜底
  凡是「设备能做的动作都成功了, 只是环境没配合」的一律 `skipped` + 环境原因标记,
  不计入通过率 (§9.2), 并在 UI / PDF 显式披露。

  未插网线 → skipped (env: unplugged), UI 红色 banner 「请插网线」让工人二选一
            (「已插好·重测」/「跳过此项」), 见前端 interactPrompt。

**吞吐不在老化测试范围** —— §6.6 明确删除 iperf3 测速。
"""
import asyncio
import re
import time
from typing import List, Tuple

from .base import TestCase, TestContext, TestResult, register
from ._tools import find_tool, with_sudo, kill_proc, sudo_hint_if_unavailable

# §11.3: 用精确正则而非 substring 模糊匹配 —— 宽松 in 匹配会被无关文本骗到
_RE_LINK = re.compile(r"^\s*Link detected:\s*(yes|no)\s*$", re.MULTILINE)
_RE_SPEED = re.compile(r"^\s*Speed:\s*(\d+)([KMG]?b/s)", re.MULTILINE)
_RE_DUPLEX = re.compile(r"^\s*Duplex:\s*(\S+)\s*$", re.MULTILINE)
_RE_ROUTE_DEFAULT = re.compile(
    r"^\s*default\s+via\s+(\S+)(?:\s+dev\s+(\S+))?", re.MULTILINE
)
_RE_IP_ADDR = re.compile(r"^\s*inet\s+(\d+\.\d+\.\d+\.\d+)", re.MULTILINE)

# ping 输出解析
_RE_PING_SUMMARY = re.compile(
    r"(\d+)\s+packets?\s+transmitted,\s*(\d+)\s+(?:packets?\s+)?received"
)
_RE_PING_RTT = re.compile(
    r"(?:rtt|round-trip)\s+min/avg/max(?:/(?:mdev|stddev))?\s*=\s*"
    r"([\d.]+)/([\d.]+)/([\d.]+)(?:/([\d.]+))?"
)


def _parse_ethtool(text: str) -> dict:
    """解析 ethtool 输出 —— 精确正则, 不做 substring 匹配 (§11.3)."""
    out = {"link_detected": False, "speed_mbps": 0}
    m = _RE_LINK.search(text)
    if m:
        out["link_detected"] = m.group(1) == "yes"
    m = _RE_SPEED.search(text)
    if m:
        v = float(m.group(1))
        unit = m.group(2)
        if unit == "Kb/s":
            v /= 1000
        elif unit == "Gb/s":
            v *= 1000
        out["speed_mbps"] = int(v)
    m = _RE_DUPLEX.search(text)
    if m:
        out["duplex"] = m.group(1)
    return out


def _parse_ping(text: str) -> dict:
    """解析 ping 输出 —— 精确正则 (§11.3)."""
    out = {
        "sent": 0, "recv": 0, "loss_pct": 100.0,
        "avg_ms": 0.0, "min_ms": 0.0, "max_ms": 0.0,
    }
    m = _RE_PING_SUMMARY.search(text)
    if m:
        sent, recv = int(m.group(1)), int(m.group(2))
        out["sent"] = sent
        out["recv"] = recv
        out["loss_pct"] = round((sent - recv) / sent * 100, 1) if sent else 100.0
    m = _RE_PING_RTT.search(text)
    if m:
        out["min_ms"] = float(m.group(1))
        out["avg_ms"] = float(m.group(2))
        out["max_ms"] = float(m.group(3))
    return out


@register
class EthTest(TestCase):
    name = "ethernet"
    display_name = "网口"
    description = "以太网链路 + DHCP + 出网 (三级降级 ping, 不做吞吐)"

    def __init__(self):
        self.proc = None
        self.cancel_done = False

    async def cancel(self):
        if self.proc and self.proc.returncode is None and not self.cancel_done:
            self.cancel_done = True
            # 不用裸 terminate/kill —— 子进程是 sudo 起的 root 进程, 普通用户
            # 对它发信号必然 EPERM, 裸调会把收尾自己炸成 error, 还让 root 测试
            # 进程泄漏在后台。见 _tools.kill_proc 的说明。
            await kill_proc(self.proc, wait_sec=2.0)

    async def _run(self, cmd: List[str], timeout: float) -> Tuple[int, str]:
        """跑一条已包好 sudo 的命令, 返回 (rc, 合并输出). 超时则 kill."""
        self.cancel_done = False
        try:
            self.proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except FileNotFoundError:
            return -1, f"command not found: {' '.join(cmd)}"
        try:
            out, _ = await asyncio.wait_for(self.proc.communicate(), timeout=timeout)
            rc = self.proc.returncode if self.proc.returncode is not None else -1
            return rc, out.decode(errors="replace")
        except asyncio.TimeoutError:
            await self.cancel()
            return -1, f"timeout after {timeout}s"

    async def run(self, ctx: TestContext) -> TestResult:
        """按 `times` 跑多轮, 每轮一整套 §6.6 的 3 步 (链路→DHCP→出网)。

        老化场景下反复建链 / 反复续 DHCP, 更容易暴露**间歇性**PHY 和网络栈
        问题 (插拔网线留下的震荡、DHCP 服务器偶发不响应)。默认仍是 1 轮,
        出货口径不变。
        """
        times = max(1, int(ctx.test_config.get("times", 1)))
        if times == 1:
            return await self._run_once(ctx)      # 单轮 = 原行为, 一行都不绕

        agg = TestResult()
        agg.metrics = {}
        await ctx.on_log(
            f"[网口] 按配置跑 {times} 轮 (每轮: 链路检测 → 取 IP → 出网连通)"
        )
        done = 0
        for i in range(times):
            if await ctx.is_cancelled():
                await ctx.on_log("[网口] 用户取消")
                agg.status = "stopped"
                agg.metrics["rounds_done"] = done
                return agg

            idx = i + 1
            await ctx.on_log(f"[网口] ── 第 {idx}/{times} 轮 ──")
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
                    f"[网口] 第 {idx}/{times} 轮 = {r.status}, 提前结束 "
                    f"(剩余 {times - done} 轮不跑)"
                )
                break
            await ctx.on_log(f"[网口] ✓ 第 {idx}/{times} 轮通过")

        agg.metrics["rounds_done"] = done
        agg.metrics["rounds_configured"] = times
        await ctx.on_progress(100, "完成")
        return agg

    async def _run_once(self, ctx: TestContext) -> TestResult:
        result = TestResult()
        eth_cfg = ctx.config.get("eth", {})

        iface = eth_cfg.get("iface", "eth0")
        ping_gateway = eth_cfg.get("ping_gateway", "auto")
        ping_dns = eth_cfg.get("ping_dns", "223.5.5.5")
        ping_target = eth_cfg.get("ping_target", "www.baidu.com")
        ping_count = int(eth_cfg.get("ping_count", 6))
        ping_timeout = int(eth_cfg.get("ping_timeout_sec", 2))
        dhcp_timeout = float(eth_cfg.get("dhcp_timeout_sec", 10))
        static_ip = eth_cfg.get("static_ip", "192.168.1.50/24")

        # ── 工具预检 (§11.1 find_tool; §11.2 命令级 sudo) ──
        ethtool_bin = find_tool("ethtool")
        ip_bin = find_tool("ip")
        udhcpc_bin = find_tool("udhcpc")
        ping_bin = find_tool("ping")
        missing = [n for n, b in (("ethtool", ethtool_bin), ("ip", ip_bin),
                                 ("udhcpc", udhcpc_bin), ("ping", ping_bin)) if not b]
        if missing:
            result.status = "error"
            result.error = (
                f"未找到: {', '.join(missing)}. 解决: sudo bash scripts/install_deps.sh "
                "(见 docs/需求设计.md §11.7 —— udhcpc 由 busybox 提供, 不依赖镜像自带)"
            )
            await ctx.on_log(f"[网口] ✗ {result.error}")
            return result
        await ctx.on_log(
            f"[网口] 工具: ethtool={ethtool_bin}, ip={ip_bin}, udhcpc={udhcpc_bin}"
        )
        if not sudo_hint_if_unavailable(result, "网口"):
            await ctx.on_log(f"[网口] ✗ {result.error}")
            return result

        await ctx.on_log(
            f"[网口] 接口={iface} | ping 三级: 网关({ping_gateway}) → DNS({ping_dns}) → 公网({ping_target})"
        )
        await ctx.on_progress(2, "① 检测链路")

        # ══════════ ① 链路检测 (零网络依赖, 纯 PHY 层) ══════════
        rc, text = await self._run(with_sudo([ethtool_bin, iface]), 10)
        if rc != 0:
            result.status = "error"
            result.error = f"ethtool 执行失败 rc={rc}: {text.strip()[:300]}"
            await ctx.on_log(f"[网口] ✗ {result.error}")
            return result

        link = _parse_ethtool(text)
        result.metrics["link_detected"] = link["link_detected"]
        result.metrics["link_speed_mbps"] = link["speed_mbps"]
        if "duplex" in link:
            result.metrics["duplex"] = link["duplex"]

        if not link["link_detected"]:
            # 未插网线 → 交互分支, 前端弹红色 banner 让工人二选一 (§6.6)
            result.status = "skipped"
            result.metrics["skipped_reason"] = "unplugged"
            result.error = "未插网线 (Link detected: no)"
            await ctx.on_log("[网口] ❌ 链路 DOWN —— 网线未插 / PHY 不通")
            await ctx.on_log("[网口] 提示 UI 弹窗: 工人可选「已插好·重测」或「跳过此项」")
            await ctx.on_progress(100, "等待工人选择")
            return result

        await ctx.on_log(f"[网口] ✓ 链路 UP, 协商速率 {link['speed_mbps']} Mbps")
        await ctx.on_progress(15, "① 链路 OK")

        # ══════════ ② 取 IP: 2a DHCP → 2b 静态兜底 ══════════
        ip_mode = "none"
        ip_addr = ""

        await ctx.on_log(f"[网口] ② 取 IP —— DHCP (超时 {dhcp_timeout}s)...")
        await ctx.on_progress(20, "② DHCP 取 IP")

        t0 = time.time()
        # -f 前台 / -n 拿不到 lease 就退出 / -q 拿到就退出 —— 防止 udhcpc 自行 daemonize
        # **必须带 timeout_sec**: udhcpc 在部分驱动/交换机组合下拿不到 lease
        # 也不退出, 会一直挂着。而它被 §11.2 sudo 提权成 root 进程后, 我们
        # (普通用户) 发不了信号去收它 —— 只能让同为 root 的 `timeout` 到点
        # 自己杀它, 否则整个测试任务永不结束, 连服务关机都会被拖住。
        rc, _ = await self._run(
            with_sudo([udhcpc_bin, "-i", iface, "-f", "-n", "-q"],
                      timeout_sec=dhcp_timeout),
            dhcp_timeout + 8,
        )
        dhcp_ms = int((time.time() - t0) * 1000)
        result.metrics["dhcp_duration_ms"] = dhcp_ms

        if rc == 0:
            ip_mode = "dhcp"
            _, text2 = await self._run(
                with_sudo([ip_bin, "-4", "addr", "show", iface]), 5
            )
            m = _RE_IP_ADDR.search(text2)
            if m:
                ip_addr = m.group(1)
            await ctx.on_log(
                f"[网口] ✓ DHCP 拿到 lease ({dhcp_ms}ms), IP={ip_addr or '(读不到)'}"
            )
        else:
            await ctx.on_log(f"[网口] ⚠️ DHCP 未拿到 lease ({dhcp_ms}ms), 走静态兜底")
            await ctx.on_progress(30, "② DHCP 失败 → 静态兜底")
            # 2b 静态兜底
            rc2, text2 = await self._run(
                with_sudo([ip_bin, "addr", "add", static_ip, "dev", iface]), 5
            )
            if rc2 == 0:
                await self._run(with_sudo([ip_bin, "link", "set", iface, "up"]), 5)
                ip_mode = "static"
                ip_addr = static_ip.split("/")[0]
                result.metrics["static_fallback_used"] = True
                await ctx.on_log(f"[网口] ✓ 静态兜底 IP 配上去: {ip_addr}")
            else:
                # 2c DHCP 与静态兜底均失败 → 环境完全不具备取 IP 条件
                result.metrics["static_fallback_used"] = False
                result.metrics["ip_mode"] = "none"
                result.metrics["skipped_reason"] = "no_ip"
                result.status = "skipped"
                result.error = (
                    f"DHCP 与静态兜底均无法取 IP —— 工位环境不具备条件, 不判 failed "
                    f"(静态兜底失败: {text2.strip()[:200]})"
                )
                await ctx.on_log(f"[网口] ⏭ {result.error}")
                await ctx.on_progress(100, "环境不具备取 IP 条件")
                return result

        result.metrics["ip_mode"] = ip_mode
        result.metrics["ip_addr"] = ip_addr
        await ctx.on_progress(40, "② IP 就绪")

        # ══════════ ③ 出网: 三级降级 ping ══════════
        gateway = ""
        if ping_gateway == "auto":
            _, text_g = await self._run(with_sudo([ip_bin, "route", "show", "default"]), 5)
            m = _RE_ROUTE_DEFAULT.search(text_g)
            if m:
                gateway = m.group(1)
            await ctx.on_log(f"[网口]   默认网关: {gateway or '(未取到)'}")
        else:
            gateway = ping_gateway
        result.metrics["ping_gateway"] = gateway

        # 三级目标, 任一通过即通过
        levels = [("gateway", gateway), ("dns", ping_dns), ("public", ping_target)]
        ping_level = "none"
        ping_ok = False
        last = {"sent": 0, "recv": 0}

        await ctx.on_progress(50, "③ 检测出网")
        for level_name, target in levels:
            if not target:
                await ctx.on_log(f"[网口]   {level_name}: 未配置目标, 跳过该级")
                continue
            if await ctx.is_cancelled():
                result.status = "stopped"
                return result

            await ctx.on_log(f"[网口]   ping [{level_name}] {target} ...")
            rc_p, text_p = await self._run(
                with_sudo([
                    ping_bin, "-I", iface,
                    "-c", str(ping_count), "-W", str(ping_timeout),
                    target,
                ]),
                ping_count * ping_timeout + 5,
            )
            pm = _parse_ping(text_p)
            last = pm
            result.metrics["ping_sent"] = pm["sent"]
            result.metrics["ping_recv"] = pm["recv"]
            result.metrics["ping_loss_pct"] = pm["loss_pct"]
            result.metrics["ping_avg_ms"] = pm["avg_ms"]
            result.metrics["ping_min_ms"] = pm["min_ms"]
            result.metrics["ping_max_ms"] = pm["max_ms"]
            result.metrics["ping_target"] = target

            # 通过条件: c/c 全部 echo, 0% loss
            if rc_p == 0 and pm["sent"] > 0 and pm["recv"] == pm["sent"]:
                ping_level = level_name
                ping_ok = True
                await ctx.on_log(
                    f"[网口]   ✓ ping [{level_name}] {target} 通过 "
                    f"({pm['recv']}/{pm['sent']}, avg {pm['avg_ms']}ms)"
                )
                break
            await ctx.on_log(
                f"[网口]   ✗ ping [{level_name}] {target} 未通 "
                f"(rc={rc_p}, {pm['recv']}/{pm['sent']})"
            )

        result.metrics["ping_level"] = ping_level
        result.metrics["degraded"] = False

        # ══════════ 判定矩阵 (环境问题 ≠ 设备问题) ══════════
        if ping_ok:
            result.status = "passed"
            if ip_mode == "static":
                # DHCP 服务器挂了, 但静态 IP 下网路通 → 设备网络栈正常
                result.metrics["degraded"] = True
                result.metrics["degraded_reason"] = (
                    "DHCP 不可用, 静态 IP 兜底下出网正常 —— 建议产线核查 DHCP 服务器"
                )
                await ctx.on_log(f"[网口] ⚠ {result.metrics['degraded_reason']}")
            await ctx.on_progress(100, f"完成 · 出网 OK ({ping_level})")
            return result

        # ── 三级全败 ──
        if ip_mode == "dhcp":
            # PHY ✓ DHCP 客户端 ✓, 仅出网未验 → 上游问题, 不判 failed, 强制复测
            result.status = "skipped"
            result.metrics["skipped_reason"] = "no_egress"
            result.error = (
                "链路与 DHCP 均正常, 但三级 ping 全败 (网关/DNS/公网) —— "
                "判定为工位上游网络问题, 不判设备 failed, 建议复测"
            )
            await ctx.on_log(f"[网口] ⏭ {result.error}")
        else:
            # 静态 IP 配上了仍 ping 不通网关 → 真设备问题, 无兜底
            result.status = "failed"
            result.error = (
                f"静态 IP 兜底 ({ip_addr}) 已配上, 但三级 ping 全败 —— "
                f"静态 IP 下连默认网关都 ping 不通, 判定为设备网络栈/二层异常, 无兜底。"
                f"最后一级 {ping_target}: {last.get('recv', 0)}/{last.get('sent', 0)} 收到"
            )
            await ctx.on_log(f"[网口] ❌ {result.error}")
        await ctx.on_progress(100, "完成")
        return result
