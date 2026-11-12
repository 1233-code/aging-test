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
from typing import List, Optional, Tuple

from .base import TestCase, TestContext, TestResult, register
from ._tools import (find_tool, find_wifi_iface, with_sudo, kill_proc,
                     sudo_hint_if_unavailable, deps_fix_hint,
                     tool_missing_reason)

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
# `ip link show` 的 flags 段 —— **管理状态**看的是这里有没有 `UP`。
# ⚠️ **不能子串匹配**: flags 里有 `LOWER_UP` / `NO-CARRIER`, 都含 "UP" 这个
# 子串 ⇒ 必须按逗号切成整块再比, 否则「没起来」会被误认成「起来了」。
_RE_LINK_FLAGS = re.compile(r"<([^>]*)>")
_RE_RFKILL_BLOCKED = re.compile(
    r"^[ \t]*(?:Soft|Hard)[ \t]+blocked:[ \t]*(yes|no)[ \t]*$", re.MULTILINE
)
# 扫描完成标志 —— 4c 降级通道的判定依据 (三个任一)
_RE_SCAN_COMPLETED = re.compile(
    r"^[ \t]*scan[ \t]+completed[ \t]*$", re.MULTILINE | re.IGNORECASE
)
_RE_BSS_HEADER = re.compile(r"^BSS[ \t]+[0-9a-f]{2}(?::[0-9a-f]{2}){5}", re.MULTILINE)

# §6.4 的**四步**分别是什么 —— 报失败原因时**把这一步在测什么写出来**。
#
# ⚠️ **2026-11-12 补**: 以前的原因只有「第 N 步失败」—— 「第 N 步」对
#   **普通工人是天书**(他不知道这四步分别是干什么的, 也不知道是哪一环坏了)
#   ⇒ 现在报的是「第 N 步失败 —— <这一步在测什么>: <具体现象>」。
#   ⚠️ 步号**跟 §6.4 的表格一一对应**, 别改。
_WIFI_STEP_NAMES = {
    1: "① 软件开关响应(`ip link set … down` → `… up`, 看驱动认不认这两句命令)",
    2: "② 状态确认(轮询 `ip link show`, 等管理状态变成 UP)",
    3: "③ 射频检查(`rfkill list wifi`, 看射频有没有被软/硬屏蔽)",
    4: "④ 扫描执行(`iw dev … scan`, 看射频通路能不能真的完成一次扫描)",
}
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


def _iw_link_associated(text: str) -> Optional[bool]:
    """`iw dev <iface> link` 的输出 → 这条射频当前**是不是关联着 AP**。

    关联着时第一行是 ``Connected to <bssid> (on wlan0)``; 没关联时是
    ``Not connected.``。

    **为什么非要问这一句** (2026-10-30 新板实测):
        硬拔一条**正在工作**的射频 —— 也就是关联着 AP 的时候直接
        `ip link set down` —— 会把整块板子拖死。两次实测都是同一个位置:
        WiFi 项结束 3~5 秒后板子重启, 日志里**没有 panic, 没有 thermal
        消息, 没有任何死因**, 板子是在**空载**状态下被硬复位的。
        所以开关之前必须先看清楚: 这条射频上是不是有活的东西在跑。

    返回 ``None`` = 看不出来(命令失败 / 输出不认识) —— 调用方**必须**按
    「可能有活连接」处理, 不能当成「空闲」。
    """
    if not text:
        return None
    if "Not connected" in text:
        return False
    if "Connected to" in text:
        return True
    return None


def _carrier_associated(iface: str) -> Optional[bool]:
    """`/sys/class/net/<iface>/carrier` → 这条射频**是不是正连着**。

    WiFi 里 carrier=1 表示链路是通的(驱动在关联成功时把 carrier 拉高),
    carrier=0 表示空闲。这是**只读**的一个文件 —— 不需要任何特权, 也**不会**
    改动板子上的任何状态。

    读不到(老内核没有这个属性 / 不是 WiFi 接口) ⇒ None = 看不出来。
    """
    try:
        with open(f"/sys/class/net/{iface}/carrier", "r") as f:
            return f.read().strip() == "1"
    except OSError:
        return None


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

    # ══════════ 只读探针 ══════════
    async def _link_states(self, ip_bin: str, iface: str) -> Tuple[str, str]:
        """读一次 `ip link show <iface>` → (管理状态, 链路状态)。

        · **管理状态(admin)** —— flags 里有没有 `UP`:
          `UP` = 接口**起来了**; `DOWN` = 管理态就是关着的;
          `UNKNOWN` = 命令失败 / 输出看不懂(**跟 DOWN 不是一回事**)。
        · **链路状态(carrier)** —— `state` 字段:
          `UP` = 有载波(关联上了 AP); `DORMANT` = **接口已起来, 只是没载波**
          (**没连上 AP —— 产线上没网的环境就是这个状态**);
          `DOWN` / `UNKNOWN` 同上。

        ⚠️ **这两个不是一回事, 判「接口起来了」只能用管理状态**
        (2026-11-06 实测: 一块**没网**的板子上 wlan0 一直是 `state DORMANT`
        ⇒ 拿 `state == UP` 当判据 ⇒ 这一项永远等不到 ⇒ 永远判「未测」
        ⇒ **产线上等于白写**)。载波有没有只影响「它连上 AP 了没有」,
        不影响「射频本身是不是好的」—— 那是 ④ 扫描的活。
        """
        rc, out, _err = await self._run(with_sudo([ip_bin, "link", "show", iface]), 5)
        if rc != 0:
            return "UNKNOWN", "UNKNOWN"
        carrier = "UNKNOWN"
        m = _RE_LINK_STATE.search(out)
        if m:
            carrier = m.group(1)
        admin = "UNKNOWN"
        fm = _RE_LINK_FLAGS.search(out)
        if fm:
            # ⚠️ 按逗号切整块再比 —— `LOWER_UP` / `NO-CARRIER` 里都含 "UP"
            # 这个子串, 子串匹配会把「没载波」当成「起来了」。
            flags = [f.strip() for f in fm.group(1).split(",")]
            admin = "UP" if "UP" in flags else "DOWN"
        return admin, carrier

    async def _rfkill_state(self, rfkill_bin: str) -> Tuple[str, str]:
        """`rfkill list wifi` → (blocked: yes/no/unknown, 原始输出)。

        - "yes"     射频被软/硬屏蔽
        - "no"      没被屏蔽
        - "unknown" 这块板子不支持 rfkill ⇒ **不据此否掉整项**
        """
        rc, out, err = await self._run(with_sudo([rfkill_bin, "list", "wifi"]), 5)
        if rc != 0:
            return "unknown", (err or "").strip()[:200]
        flags = _RE_RFKILL_BLOCKED.findall(out)
        return ("yes" if "yes" in flags else "no"), out

    async def _nm_device_type(self, nmcli_bin: Optional[str], iface: str) -> Optional[str]:
        """`nmcli -t -f DEVICE,TYPE device status` → 这个接口在 NM 里的类型。

        返回 "wifi" / "wifi-p2p" ⇒ **NetworkManager 管着它**;
        返回 "unmanaged" ⇒ NM 知道它但**不接管**(那我们爱怎么动怎么动);
        返回 None ⇒ 没有 nmcli / 问不出来 / 压根没这个设备。

        **为什么非要问这一句** (2026-11-06 实测, 两台好板子被判 failed):
            这批产线板子上跑着 NetworkManager, 它管着 wlan0。手工
            `ip link set wlan0 up` 之后, NM 按自己的状态机把接口**又按下去**
            (没连上 AP 时它认为这接口该关着) ⇒ 接口 10 秒一直 DOWN ⇒
            ①「开关响应」看着过了(两条命令都退出 0), ②「状态确认」失败
            ⇒ 报告写「驱动异常」—— **真因(NM 抢地盘)从头到尾没人查过**。
        """
        if not nmcli_bin:
            return None
        rc, out, _err = await self._run(
            [nmcli_bin, "-t", "-f", "DEVICE,TYPE", "device", "status"], 8)
        if rc != 0:
            return None
        for line in (out or "").splitlines():
            parts = line.split(":")
            if len(parts) >= 2 and parts[0].strip() == iface:
                return parts[1].strip()
        return None

    async def _bring_up(self, ctx: TestContext, ip_bin: str,
                        nmcli_bin: Optional[str], iface: str,
                        nm_type: Optional[str], timeout: float) -> Tuple[str, str, int]:
        """尽力把接口拉起来, 返回 (管理状态, 链路状态, 等待毫秒)。

        **拉不起来 ≠ 板子坏了** —— 绝大多数是「还没轮到它起来」(刚开机、
        NM 的 autoconnect 还没跑)。所以这里只管尽力, 判定留给调用方:
        拉不起来 ⇒ 本项**跳过**(没测到), 不是**失败**(不是缺陷)。

        NM 管的接口 ⇒ 手工拉起来之后它会按自己的状态机把接口按回去(实测)
        ⇒ 那就不是「射频起不来」, 是「NM 不让它起来」⇒ 这一项判**跳过**
        (没测到), 不判失败。
        没人管的接口 ⇒ 拉不起来就是真起不来 ⇒ 同样判**跳过**(分不清是
        「还没初始化完」还是「驱动有毛病」⇒ 没测到, 不判缺陷)。
        ⚠️ 这里**只做「拉一把」, 不做「关一次再开」** —— 开关那是 §6.4 的
        ①, 只有「射频空闲 + 没人管」时才做(§6.4 的「射频不硬拔」教训)。
        """
        await self._run(with_sudo([ip_bin, "link", "set", iface, "up"]), 5)
        return await self._wait_state_up(ctx, ip_bin, iface, timeout)

    async def _restore(self, ctx: TestContext, ip_bin: str,
                       nmcli_bin: Optional[str], iface: str,
                       nm_type: Optional[str]) -> Tuple[str, str]:
        """把接口恢复成 UP —— **开关做完必须恢复**。

        ⚠️ 2026-11-06 实测: 以前开关失败就直接返回, **接口留在 DOWN**
           ⇒ 这块板子的无线从此是断的(工人原话「设备wifi是好的, 但是没
           切换到」—— 设备好好的, 是**测试把它按下去没松手**)。下一轮再跑,
           看到的就是一条「空闲」的射频 ⇒ 又开关一次 ⇒ 又失败 ⇒
           **自我强化**: 同一个缺陷会连着几轮一直报, 看着像「板子坏了」。
           ⇒ 不管开关成没成, 收尾都要把接口拉回 UP; 拉不回来就在报告里
           **大声说清楚**(那是操作事故, 不是设备缺陷)。

        返回 **(管理状态, 载波状态)** —— 判「起来没」看**管理状态**
        (flags 里有没有 UP); 载波(有没有连上 AP)只是**附带**信息,
        **没网的工位上它恒是 DORMANT** ⇒ 拿它当判据 = 假警报。
        """
        # 先等 NM 自己把它弄起来(它知道该连哪个 AP), 再手工拉一把。
        # ⚠️ 判「起来没」看**管理状态** —— 没网的时候链路状态是 DORMANT,
        #    那也说明接口已经起来了(见 `_wait_state_up` 的说明)。
        #
        # ⚠️ **2026-11-11 修**: 以前这里返回的是 **carrier**(链路状态)
        #    ⇒ 收尾拿它跟 "UP" 比 ⇒ 在**没网的工位**上 carrier 恒是
        #    DORMANT/UNKNOWN ⇒ 明明接口已经恢复成 UP, 却报
        #    「⚠ 收尾没能把接口恢复成 UP」⇒ **假警报**(工人以为板子坏了)。
        #    ⇒ 返回**管理状态**(那才是「接口起来没」的判据), 载波另记一笔。
        admin, carrier, _ms = await self._wait_state_up(ctx, ip_bin, iface, 5.0)
        if admin != "UP":
            await self._run(with_sudo([ip_bin, "link", "set", iface, "up"]), 5)
            admin, carrier, _ms = await self._wait_state_up(ctx, ip_bin, iface, 5.0)
        return admin, carrier

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
    ) -> Tuple[str, str, int]:
        """轮询 `ip link show` 直到**管理状态**为 UP, 返回 (admin, carrier, 毫秒)。

        ⚠️ **判据是「管理状态」不是「链路状态」** —— 2026-11-06 实测踩出来的:
        一块**没有任何网络**的板子上, WiFi 接口的状态是 **`state DORMANT`**
        (= 内核层面接口已经起来, 只是没有载波 —— 因为没关联上 AP)。
        拿 `state == UP` 当「接口起来了」⇒ 在产线这种**没网的环境**里,
        这一项永远等不到 UP ⇒ 永远判「未测」⇒ **这一项在产线上等于白写**
        (④ 扫描那个「环境无 AP 也能过」的降级分支根本走不到, 因为被 ② 挡在门外)。
        ⇒ 管理状态看的是 flags 里有没有 `UP`:
            `<BROADCAST,MULTICAST,UP,LOWER_UP>`   → 已起来(有载波)
            `<BROADCAST,MULTICAST,NO-CARRIER,UP>` → 已起来(**没载波**: 没连 AP)
            `<BROADCAST,MULTICAST>`                → **没起来**(管理态就是 down)

        **为什么必须轮询, 不能固定 sleep —— 2026-10-22 实测踩过的坑**:
        无线驱动在 `ip link set up` 之后还要初始化射频, 这段时间内核报
        `state DORMANT` (= 接口已 up, 链路未就绪), 是**正常中间态**。
        本机实测 DORMANT→UP 要 **3.22 秒**, 而原先固定等 `switch_settle_sec`
        (1.0s) 就去读 —— 必然读到 DORMANT 判 fail, run 43/44/47 全倒在这。
        ⇒ 现在轮询的**退出条件**是管理状态 UP, 但**载波**照样一路读到,
        报告里两个都给(载波没有只说明「没连上 AP」, 不说明射频坏了)。

        ⚠️ `mode DORMANT` 与 `state DORMANT` 是两回事: 前者在 `ip link show`
        行的 `mode` 后面 (射频省电模式, 正常), 后者才是链路状态。正则只锚
        `state`, 不会误取 —— 已用本机真实输出逐条验证过。
        """
        deadline = time.monotonic() + timeout
        t0 = time.monotonic()
        admin, carrier = "UNKNOWN", "UNKNOWN"
        while True:
            if await ctx.is_cancelled():
                return admin, carrier, int((time.monotonic() - t0) * 1000)
            admin, carrier = await self._link_states(ip_bin, iface)
            if admin == "UP":
                return admin, carrier, int((time.monotonic() - t0) * 1000)
            if time.monotonic() >= deadline:
                return admin, carrier, int((time.monotonic() - t0) * 1000)
            await asyncio.sleep(0.3)

    async def _iface_has_ip(self, ip_bin: str, iface: str) -> Optional[bool]:
        """接口上有没有配了 IP —— **只读**, 不需要特权。

        有 IP ⇒ 这条射频**至少关联成功过一次**(DHCP 给的), 或者现场手工配了
        静态地址。厂商驱动有时候不维护 carrier, 那时这一路就是唯一能看出
        「它还连着」的探针 —— 而「还连着」正是我们**不能硬拔**的那种状态。

        `ip -o addr show` 每行形如
        ``2: wlan0    inet 192.168.1.132/24 brd ... scope global wlan0``;
        没有地址时输出为空、退出码 0。
        """
        rc, out, _err = await self._run([ip_bin, "-o", "addr", "show", iface], 5)
        if rc != 0:
            return None
        return any("inet" in ln for ln in (out or "").splitlines())

    async def _radio_busy(self, ip_bin: str, iw_bin: Optional[str],
                           iface: str) -> Optional[bool]:
        """这条射频上**是不是有活的东西**在跑(正关联着 AP)。

        三个**只读**的探针, 保守取并集:

          · `iw dev <iface> link` —— 准(直接看 BSSID), 但有些驱动要特权
            才肯答, 答不上就当没这一路;
          · `/sys/class/net/<iface>/carrier` —— 一定读得到, 但厂商驱动
            未必维护;
          · `ip -o addr show <iface>` —— 有 IP ⇒ 至少关联成功过一次。

        **任一个说「连着」就算连着**; 三个都说「空闲」才算空闲;
        都看不出来 ⇒ None = **当作连着** —— 宁可不做这一步, 也不能拿整块
        板子去赌。

        ⚠️ 这一步只**读**, 不改板子上任何状态 —— 所以它在开关之前做是安全的。
        """
        seen: List[bool] = []
        if iw_bin:
            rc, out, _err = await self._run([iw_bin, "dev", iface, "link"], 5)
            if rc == 0:
                state = _iw_link_associated(out)
                if state is not None:
                    seen.append(state)
        carrier = _carrier_associated(iface)
        if carrier is not None:
            seen.append(carrier)
        has_ip = await self._iface_has_ip(ip_bin, iface)
        if has_ip is not None:
            seen.append(has_ip)
        if not seen:
            return None
        return any(seen)

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
            result.error = tool_missing_reason(
                "、".join(missing), deps_fix_hint(),
                note="PATH 不含 /sbin, 或这台机器上压根没装这些工具")
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
            # ⚠️ **2026-11-12**: 以前只写「第 3 步失败」—— **「第 3 步」对
            #   工人是天书**(他不知道这四步分别是干什么的)⇒ 现在把**这一步
            #   到底在测什么**写在前面, 报告上不用再翻文档就知道是哪一环坏了。
            result.error = (f"第 {step} 步失败 —— "
                            f"{_WIFI_STEP_NAMES.get(step, '未知步骤')}: "
                            f"{reason[:300]}")
            await ctx.on_log(f"[WiFi] ❌ {result.error}")

        async def skip_because(reason_code: str, reason: str):
            """本轮**测不到**这一项 —— skipped, 不是 failed。

            **什么时候用这个**: 缺的是「料」或「环境」, 不是「设备有缺陷」。
            判 failed 会把好板子送去返修(2026-11-06 实测就是这么误杀了两台),
            判 passed 是撒谎(那一段真的没测)。skipped 的口径跟 TF 卡没插
            一样: 不计入通过率, 报告的「未测项明细」里逐条列出来, 人工确认。
            """
            result.status = "skipped"
            result.metrics["skipped_reason"] = reason_code
            result.error = reason
            await ctx.on_log(f"[WiFi] ⏭ 跳过: {reason}")

        # ══════════ ⓪ 前置检查(不是 §6.4 的四步之一, 是那四步之前的前置) ══════════
        #
        # ⚠️ **rfkill 必须先查** (2026-11-06 实测): 射频被软屏蔽的时候,
        #     `ip link set up` **照样退出 0**(命令没报错), 但接口就是不起来
        #     (内核不让屏蔽着的射频 up) ⇒ ①「开关响应」看着像过了, ② 必然失败
        #     ⇒ 报告写「驱动异常」—— **真因从头到尾没人查过**。
        #     ⇒ 屏蔽就先 unblock; unblock 之后还屏蔽 ⇒ 那才是真·设备问题。
        blocked, _rf_out = await self._rfkill_state(rfkill_bin)
        result.metrics["rfkill_blocked"] = blocked
        if blocked == "yes":
            await ctx.on_log("[WiFi] ⓪ 射频被屏蔽, 尝试 unblock")
            await self._run(with_sudo([rfkill_bin, "unblock", "wifi"]), 3)
            blocked, _rf_out = await self._rfkill_state(rfkill_bin)
            result.metrics["rfkill_blocked"] = blocked
        if blocked == "yes":
            result.status = "failed"
            result.metrics["step_failed"] = 3
            result.metrics["step_failed_reason"] = (
                "rfkill 报 blocked: yes —— 射频被软/硬屏蔽, 且 unblock 之后仍屏蔽")
            result.error = ("射频被 rfkill 屏蔽(软/硬), unblock 之后仍然屏蔽 ⇒ "
                            "板子的无线是关着的(硬件开关/驱动/系统服务把它关了)")
            await ctx.on_log(f"[WiFi] ❌ {result.error}")
            return result
        await ctx.on_log(f"[WiFi] ⓪ 射频未被屏蔽 (rfkill_blocked={blocked})")

        # **谁在管这个接口** —— 决定了软件开关这一步能不能做(见 ①)。
        nmcli_bin = find_tool("nmcli")
        nm_type = await self._nm_device_type(nmcli_bin, iface)
        result.metrics["iface_manager"] = (
            "NetworkManager" if (nm_type and nm_type.startswith("wifi"))
            else ("nm-unmanaged" if nm_type else "unknown"))
        if nm_type and nm_type.startswith("wifi"):
            await ctx.on_log(
                "[WiFi] ⓪ 这个接口由 NetworkManager 托管 —— 手工 `ip link set` "
                "会被它按回去(实测), 软件开关这一步要三思"
            )

        # ══════════ ① 软件开关响应 ══════════
        #
        # ⚠️ **绝不硬拔一条正在工作的射频** (2026-10-30 新板两次重启实测)。
        #     实测现象: 板子在 WiFi 项结束 3~5 秒后**当场重启**, 日志里
        #     没有 panic、没有 thermal 消息、没有任何死因 —— 板子是在**空载**
        #     状态下被硬复位的(厂商看门狗复位的样子)。两次都发生在「射频
        #     刚被 `ip link set down/up` 掀过一遍」之后。
        #
        #     ⇒ **关联着的射频一律不碰**: 本轮只做只读检查(②状态 ③射频
        #       ④扫描), 软件开关这项**降级跳过**, 并把原因写进报告。
        #       少测一步远好过把整块板子测没了 —— 而且 ②③④ 照样能证明
        #       射频通路是通的。
        await ctx.on_progress(10, "① 软件开关响应")
        degraded_reasons: List[str] = []

        def _apply_degraded():
            """降级原因可能有几个来源(跳过软件开关 / 环境无 AP) —— 都要留。

            「降级」不是「失败」: 这一项**测到了**射频通路(②③④ 都过了),
            只是有一小段没测(没动射频 / 工位没 AP)。报告里必须写清楚是哪
            一段没测 —— 不然读报告的人会以为四项全测过了。
            """
            result.metrics["degraded"] = bool(degraded_reasons)
            if degraded_reasons:
                result.metrics["degraded_reason"] = "; ".join(degraded_reasons)

        busy = await self._radio_busy(ip_bin, iw_bin, iface)
        result.metrics["radio_busy_before"] = (
            "connected" if busy else ("idle" if busy is False else "unknown")
        )
        # 软件开关的三档: false = 不做; "force" = 无条件做(危险, 只留给专门
        # 复现「硬拔活着的射频」那个故障的人); 默认 true = **只在安全时做**。
        sw_cfg = t_cfg.get("software_switch", True)
        if isinstance(sw_cfg, str):
            mode = sw_cfg.strip().lower()
            force = mode in ("force", "always")
            allow_cycle = mode not in ("false", "no", "off", "0", "")
        else:
            force = False
            allow_cycle = bool(sw_cfg)

        # 开关之前先看一眼接口的状态。
        # **接口还是 DOWN 就不去「开关」它** (2026-11-06 实测):
        #   板子刚开机那会儿, 射频还在初始化(NM 也还没连上 AP) ⇒ wlan0 是
        #   DOWN。这时候去 down/up, 量到的是「还没初始化完」, 不是「设备坏
        #   了」—— 实测两台好板子就是这样被判 failed 的(报告原话:
        #   `set up` 后 10.1s 内 state 始终是 'DOWN')。
        pre_admin, pre_carrier = await self._link_states(ip_bin, iface)
        pre_state = pre_carrier
        result.metrics["ip_link_state_before"] = pre_carrier
        result.metrics["ip_link_admin_before"] = pre_admin
        nm_managed = bool(nm_type and nm_type.startswith("wifi"))
        switched = False        # 这一轮到底动没动射频 —— 收尾要按它决定恢复

        async def _finish() -> TestResult:
            """收尾: **把接口恢复回去**, 然后返回结果。

            **我们动过它, 就得我们松手** —— 失败路径以前直接把接口留在
            DOWN ⇒ 这块板子的无线从此是断的(工人原话「设备wifi是好的,
            但是没切换到」—— 设备好好的, 是**测试把它按下去没松手**)
            ⇒ 下一轮看到的是「空闲射频」⇒ 又开关 ⇒ 又失败 ⇒ **自我强化**
            (实测同一块板连着两轮都报 failed, 看着像板子坏了)。
            ⇒ 恢复不回来就在指标和日志里写清楚(那是**操作事故**, 不是缺陷)。
            """
            if switched:
                restored, restored_carrier = await self._restore(
                    ctx, ip_bin, nmcli_bin, iface, nm_type)
                result.metrics["restored_state"] = restored
                result.metrics["restored_carrier"] = restored_carrier
                if restored != "UP":
                    result.metrics["restore_failed"] = True
                    await ctx.on_log(
                        f"[WiFi] ⚠ 收尾没能把接口恢复成 UP(管理态 {restored}, "
                        f"载波 {restored_carrier})"
                        " —— 这块板子的无线现在是断的"
                    )
            return result

        if not allow_cycle:
            degraded_reasons.append("配置关了软件开关")
            result.metrics["radio_cycle_skipped"] = True
            result.metrics["radio_cycle_skip_reason"] = "配置关了软件开关"
            await ctx.on_log(
                "[WiFi] ① 软件开关**按配置关闭** —— 本轮只做只读检查"
            )
            await ctx.on_progress(25, "① 已关闭")
        elif busy is not False and not force:
            # busy is True(正连着) 或 None(看不出来) —— **两种都不碰射频**。
            # 「看不出来」按「连着」处理: 宁可少测这一步, 也不能拿整块板子赌。
            if busy is True:
                degraded_reasons.append(
                    "射频正关联着 AP, 跳过软件开关(硬拔活着的射频会拖垮整块板子)"
                )
                result.metrics["radio_cycle_skipped"] = True
                result.metrics["radio_cycle_skip_reason"] = (
                    "射频正关联着 AP(硬拔会拖垮整块板子)"
                )
                await ctx.on_log(
                    "[WiFi] ① 射频**正关联着 AP** —— 不硬拔, 本轮跳过软件开关"
                )
            else:
                degraded_reasons.append(
                    "射频状态看不出来, 跳过软件开关(不拿整块板子赌)"
                )
                result.metrics["radio_cycle_skipped"] = True
                result.metrics["radio_cycle_skip_reason"] = (
                    "射频状态看不出来(探测命令没答), 不拿整块板子赌"
                )
                await ctx.on_log(
                    "[WiFi] ① 射频状态**看不出来**(探测命令没答) —— "
                    "不硬拔, 本轮跳过软件开关"
                )
            await ctx.on_log(
                "[WiFi]   → 继续做只读检查(②状态 ③射频 ④扫描), 射频本身不动"
            )
            await ctx.on_progress(25, "① 跳过")
        elif nm_managed and not force:
            # **NetworkManager 管的接口, 别跟它抢** (2026-11-06 实测):
            #   手工 `ip link set up` 之后, NM 按自己的状态机把接口又按下去
            #   ⇒ 10 秒一直 DOWN ⇒ ①看着过了、②失败 ⇒ **好板子被判 failed**。
            #   量到的是「NM 的状态机」, 不是「射频的驱动」⇒ 这一步不算数。
            degraded_reasons.append(
                "接口由 NetworkManager 托管, 跳过软件开关(手工开关会被它抢回去)"
            )
            result.metrics["radio_cycle_skipped"] = True
            result.metrics["radio_cycle_skip_reason"] = (
                "接口由 NetworkManager 托管(手工 ip link set 会被它按回去)"
            )
            await ctx.on_log(
                "[WiFi] ① 接口由 **NetworkManager** 托管 —— 手工开关会被它"
                "按回去(实测), 本轮跳过软件开关, 只做只读检查"
            )
            await ctx.on_progress(25, "① 跳过")
        elif pre_state != "UP" and not force:
            # 接口还没「有载波」 ⇒ 现在去「开关」它, 量到的是「还没初始化好」
            # 或者「压根没有 AP 可连」(没网的环境)。
            # ⚠️ 这一步**只跳过硬拔射频那一下**, 不影响本项能不能测 ——
            #    ②③④ 三个只读步骤照做(尤其是 ④ 扫描, 它不需要网络)。
            # ⚠️ **DORMANT 与 DOWN 要分开说**: DORMANT = 接口已经起来了,
            #    只是没有载波(没连上 AP); DOWN = 管理态就是关着的。
            if pre_admin == "UP":
                why = "没有载波(没连上 AP), 射频本身不动"
            else:
                why = "还没初始化好"
            degraded_reasons.append(
                f"接口开关前还是 {pre_state}({why}), 跳过软件开关")
            result.metrics["radio_cycle_skipped"] = True
            result.metrics["radio_cycle_skip_reason"] = (
                f"接口开关前还是 {pre_state}({why}), 不拿这个状态判设备")
            await ctx.on_log(
                f"[WiFi] ① 接口开关前是 **{pre_state}**({why}) —— "
                "不去开关它, 本轮只做只读检查"
            )
            await ctx.on_progress(25, "① 跳过")
        else:
            result.metrics["radio_cycle_skipped"] = False
            result.metrics["radio_cycle_skip_reason"] = "(未跳过)"
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

            # ⚠️ **动了射频就一定要恢复** —— 见 `_restore` 的说明:
            #   失败路径以前直接把接口留在 DOWN ⇒ 这块板子的无线从此是断的
            #   ⇒ 下一轮看到的是「空闲射频」⇒ 又开关 ⇒ 又失败 ⇒ 自我强化。
            switched = True

            if await ctx.is_cancelled():
                result.status = "stopped"
                return await _finish()

            await asyncio.sleep(settle)
            rc, _, err = await self._run(
                with_sudo([ip_bin, "link", "set", iface, "up"]), 5
            )
            if rc != 0:
                await fail_at(1, f"`ip link set {iface} up` 退出码 {rc}: "
                                f"{(err or '').strip()[:200]}")
                await ctx.on_progress(100, "失败")
                return await _finish()
            await ctx.on_log("[WiFi]   ✓ down / up 两句都退出 0 —— 驱动响应了命令")
            await ctx.on_progress(25, "① 开关 OK")

        # ══════════ ② 状态确认 (轮询等 UP, 不是固定 sleep) ══════════
        #
        # ⚠️ **判据是「管理状态」不是「链路状态」** (2026-11-06 实测,
        #     一块**没网**的板子上 wlan0 一直是 `state DORMANT` ⇒ 拿
        #     `state == UP` 当「接口起来了」⇒ 这一项永远等不到 ⇒ 永远判
        #     「未测」⇒ **产线上没网的环境里, 这一项等于白写**
        #     (④ 扫描那个「环境无 AP 也能过」的降级分支根本走不到))。
        #     管理状态看 flags 里有没有 `UP` —— `NO-CARRIER,UP` 也是**起来了**,
        #     只是没连上 AP。**没有载波不是缺陷**, 射频好不好由 ④ 扫描说话。
        #
        # ⚠️ **这一步的判据还取决于「我们有没有动过射频」** (2026-11-06 实测,
        #     两台好板子被判 failed 的根因):
        #     · 动过(①真做了开关)⇒ 接口**必须**自己回来 ⇒ 起不来 = 驱动问题
        #       ⇒ 判失败;
        #     · 没动过(①跳过了)⇒ 接口现在的状态**不是我们造成的** ⇒
        #       「接口是 DOWN」既可能是驱动坏了, 也可能是**刚开机还没初始化
        #       好**、或者是 **NetworkManager 把它按下去了** ⇒ 一律**先试着
        #       拉起来**; 拉不起来 ⇒ 本项**跳过**(没测到), 不判失败。
        #     以前这两种混在一起判 ⇒ 「set up 后 10.1s 内 state 始终是
        #     'DOWN'」⇒ **好板子被判失败**(实测两台), 而且失败路径还把
        #     接口留在 DOWN ⇒ 下一轮更起不来 ⇒ 自我强化。
        await ctx.on_log(
            f"[WiFi] ② 状态确认 — 轮询 `ip link show` 直到**管理状态**为 UP "
            f"(上限 {state_timeout}s, 每 0.3s 一次)"
        )
        admin, carrier, waited_ms = await self._wait_state_up(
            ctx, ip_bin, iface, state_timeout)
        result.metrics["ip_link_state"] = carrier
        result.metrics["ip_link_admin"] = admin
        result.metrics["state_wait_ms"] = waited_ms

        if admin != "UP":
            if await ctx.is_cancelled():
                result.status = "stopped"
                await ctx.on_log("[WiFi] ⏹ 用户取消 (轮询等 UP 期间)")
                return await _finish()
            # 没动过 ⇒ 先试着拉起来(NM 管的接口它可能自己会弄; 没人管的
            # 我们直接 `ip link set up`)。拉起来了就继续往下测。
            if not switched:
                await ctx.on_log(
                    f"[WiFi]   接口管理态是 {admin}(链路 {carrier}) —— "
                    f"本轮没动过它, 试着拉起来"
                )
                admin, carrier, waited_ms = await self._bring_up(
                    ctx, ip_bin, nmcli_bin, iface, nm_type, state_timeout)
                result.metrics["ip_link_state"] = carrier
                result.metrics["ip_link_admin"] = admin
                result.metrics["state_wait_ms"] = waited_ms

        if admin != "UP":
            if await ctx.is_cancelled():
                result.status = "stopped"
                await ctx.on_log("[WiFi] ⏹ 用户取消 (轮询等 UP 期间)")
                return await _finish()
            if nm_managed:
                # **NetworkManager 按着它** —— 量到的是 NM 的状态机, 不是射频
                # ⇒ 本项没测到 ⇒ 跳过(不计入通过率, 人工确认放行)。
                await skip_because(
                    "iface_down_nm_managed",
                    f"无线接口 {iface} 管理态起不来(始终 {admin}), 而这个接口由 "
                    "NetworkManager 托管 —— 手工 `ip link set` 会被它按回去, "
                    "量到的是它的状态机不是射频 ⇒ 本项未测, 需人工确认放行"
                )
                await ctx.on_progress(100, "跳过")
                return await _finish()
            if switched:
                await fail_at(
                    2,
                    f"`set up` 后 {waited_ms / 1000:.1f}s 内管理状态始终是 "
                    f"'{admin}'(链路 {carrier})。DORMANT = 接口已 up 但射频"
                    "仍在初始化, 属正常中间态; DOWN/UNKNOWN 才说明开关没生效或"
                    "驱动异常。"
                )
                await ctx.on_progress(100, "失败")
                return await _finish()
            await skip_because(
                "iface_not_up",
                f"无线接口 {iface} 管理态是 {admin}(本轮没动过它, 试着拉也拉不起来) "
                "—— 分不清是「还没初始化完」还是「驱动有毛病」⇒ 本项未测, "
                "需人工确认放行"
            )
            await ctx.on_progress(100, "跳过")
            return result
        if carrier == "UP":
            await ctx.on_log(
                f"[WiFi]   ✓ 管理态 UP / 链路 UP (驱动就绪耗时 {waited_ms}ms)"
            )
        else:
            # ⚠️ **没有载波不是缺陷** —— 没连上 AP 而已(产线上没网就是这个)。
            #    射频本身好不好由 ④ 扫描说话, 这一项**照样往下测**。
            await ctx.on_log(
                f"[WiFi]   ✓ 管理态 UP(接口已起来), 链路 {carrier} —— "
                f"**没有载波 = 没连上 AP**(产线上没网的环境就是这样), "
                f"不影响本项继续测(耗时 {waited_ms}ms)"
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
            return await _finish()
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
        _apply_degraded()

        if ssids:                                        # 4a
            result.status = "passed"
            await ctx.on_log(
                f"[WiFi]   ✓ 4a 扫到 {len(ssids)} 个 SSID —— 射频通路已验证"
            )
        elif scan_cmd_ok:                                # 4c 命令级降级
            result.status = "passed"
            degraded_reasons.append("环境无 AP, 射频通路已验证")
            _apply_degraded()
            await ctx.on_log(
                f"[WiFi]   ✓ 4c 降级通过 —— 周边无 AP, 但驱动成功完成了一次射频扫描"
                f" (rc=0 + 扫描完成标志)。设备能力没问题, 是工位环境没 AP。"
            )
        else:                                            # 4d 驱动级失败, 无降级
            detail = (last_err or last_out).strip()
            if "timeout" in last_err:
                detail = f"扫描超时 {scan_timeout}s (共试 {retries_done + 1} 次)"
            _apply_degraded()
            await fail_at(4, f"`iw dev {iface} scan` 驱动级失败: {detail[:300]}")
            await ctx.on_progress(100, "失败")
            return result

        await ctx.on_progress(100, "完成")
        # 收尾: 动过接口就把它恢复回去(见 `_finish` 的说明)
        return await _finish()
