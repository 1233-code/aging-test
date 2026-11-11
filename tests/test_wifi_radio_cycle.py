"""回归测试: WiFi 项**不许硬拔一条正在工作的射频**(§6.4, 2026-10-30 新板实测)

**起因 (2026-10-29 / 10-30 新板连续两次重启)**
    同一块板子(SN A10010000000058)两次都在**同一个位置**夭折: WiFi 项结束
    之后、CPU 压力项开始之前。两次的 CPU 压力项**连一行记录都没有** ——
    也就是说 CPU 压力测试一次都没跑过, 这一轮老化等于没做。

    现场捞回来的上一次启动日志(`journalctl -b -1 -n 80`)把范围收得很紧:
      · 板子死在 **11:19:52**, 而 WiFi 项的扫描在 **11:19:49** 就结束了 ——
        中间隔了 3 秒, 板子当时**空载**;
      · 日志里**没有 panic、没有 thermal 消息、没有任何死因** ⇒ 板子不是
        软件崩的, 是被**硬复位**的(厂商看门狗复位的样子);
      · 最后一次动这块射频的, 正是 WiFi 项自己的
        `ip link set wlan0 down` → `ip link set wlan0 up`。

    两次崩溃时这块射频**都正关联着 AP**(板子开机后 NetworkManager 自己连上
    了)。所以本测试守住的不变量是:

        **射频正关联着 AP ⇒ 绝不做软件开关那一步。**

    宁可这一小段没测(降级, 并在报告里写明), 也不能拿整块板子去赌。

**这里守的是**
    ① 射频空闲 ⇒ 开关照做(不能把这一项测成残废);
    ② 射频关联着 ⇒ **一条 `ip link set` 都不许发**, 只做只读检查;
    ③ **看不出来** ⇒ 按「关联着」处理(保守);
    ④ 配置能整个关掉软件开关;
    ⑤ 降级原因要**留在报告里**(跳过软件开关 + 环境无 AP 可以同时存在);
    ⑥ **射频项的位置** —— 排在 GPU 之前(§8; 2026-11-04 用户定的);
      蓝牙项紧跟其后(§6.4.1, 同一批实现)。

**不碰真硬件**: 探测用的 `/sys/class/net/*/carrier`、所有 `ip`/`iw`/`rfkill`
命令全部换成假的 —— 跑这个测试不会动本机任何网络状态。

跑法: .venv/bin/python -m tests.test_wifi_radio_cycle
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.tests import wifi as W                        # noqa: E402
from server.tests.base import TestResult                 # noqa: E402

FAILS = []
PASSES = 0


def check(cond, msg, extra=""):
    global PASSES
    if cond:
        PASSES += 1
        print("    ✓ " + msg)
    else:
        FAILS.append(msg)
        print("    ✗ " + msg + ("  → " + str(extra) if extra else ""))


def section(title):
    print("\n" + title)


# ── 假的 ctx ────────────────────────────────────────────────────────

class FakeCtx:
    def __init__(self, test_config=None):
        self.config = {"interfaces": {"wifi": "wlan0"}}
        self.test_config = test_config or {}
        self.logs = []
        self.progress = []

    async def on_log(self, line): self.logs.append(line)

    async def on_progress(self, pct, msg): self.progress.append((pct, msg))

    async def on_metric(self, k, v): pass

    async def is_cancelled(self): return False


# ── 假的板子: 记下所有跑过的命令, 按命令内容回话 ──────────────────────

SCAN_WITH_AP = (
    "BSS aa:bb:cc:dd:ee:ff(on wlan0)\n"
    "\tSSID: FACTORY-AP\n"
    "\tfreq: 5200 MHz\n"
    "scan completed\n"
)
SCAN_NO_AP = "BSS aa:bb:cc:dd:ee:ff(on wlan0)\n\tSSID: \nscan completed\n"


class FakeBoard:
    """假的射频 + 假的命令执行。

    radio_connected  射频是不是正关联着 AP (None = 命令失败, 看不出来)
    link_state       `ip link show` 报的 state
    state_seq        `ip link show` 依次报的状态(最后一个一直重复)——
                     给了它就**代替** link_state; 用它演「开关之后起不来」
    scan_out         `iw dev wlan0 scan` 的输出
    nm_type          `nmcli device status` 报的接口类型
                     ("wifi" = NM 托管它; None = 没装 nmcli / 问不出来)
    rfkill_blocked   射频是不是被屏蔽
    """

    def __init__(self, radio_connected=False, link_state="UP",
                 scan_out=SCAN_WITH_AP, has_ip=False, ip_addr_fails=False,
                 state_seq=None, nm_type=None, rfkill_blocked=False,
                 fail_up_once=False, nm_keeps_down=False,
                 rfkill_stays_blocked=False, up_no_effect=False,
                 up_gives_dormant=False):
        self.radio_connected = radio_connected
        self.link_state = link_state
        self.cur_state = link_state    # 当前状态(被 `ip link set` 改)
        self.state_seq = list(state_seq) if state_seq else None
        self.scan_out = scan_out
        self.has_ip = has_ip
        self.ip_addr_fails = ip_addr_fails
        self.nm_type = nm_type
        self.nm_keeps_down = nm_keeps_down   # NM 会把手工 up 按回去(实测)
        self.rfkill_blocked = rfkill_blocked
        self.rfkill_stays_blocked = rfkill_stays_blocked  # unblock 也没用
        self.fail_up_once = fail_up_once   # 第一次 `set up` 失败(退出码非 0)
        self.up_no_effect = up_no_effect   # `set up` 退出 0 但接口**纹丝不动**
                                           # (驱动没反应 ⇒ 管理态一直 DOWN)
        self.up_gives_dormant = up_gives_dormant   # `set up` 之后接口起来了,
                                           # 但**没载波**(没连上 AP)⇒ DORMANT
        self.up_failed = False
        self.calls = []
        self.link_set_log = []      # 每次 `ip link set` 做了什么, 按顺序

    def _state(self):
        """`ip link show` 这一轮该报什么。

        给了 `state_seq` 就按剧本走(最后一个一直重复); 没给就报当前状态。
        """
        if self.state_seq:
            if len(self.state_seq) > 1:
                return self.state_seq.pop(0)
            return self.state_seq[0]
        return self.cur_state

    async def run(self, cmd, timeout):
        cmd = list(cmd)
        self.calls.append(cmd)
        base = cmd[0].split("/")[-1]
        args = cmd[1:]
        if base == "nmcli":
            # 没装 nmcli / NM 问不出来 ⇒ 空输出(上层当成「没这一路信息」)
            if self.nm_type is None:
                return 0, "", ""
            return 0, f"wlan0:{self.nm_type}\n", ""
        if base == "iw" and "link" in args:
            if self.radio_connected is None:
                return 1, "", "Operation not permitted"
            if self.radio_connected:
                return 0, ("Connected to 0c:84:08:e1:c0:c0 (on wlan0)\n"
                           "\tSSID: AVWA\n\tfreq: 5200 MHz\n"), ""
            return 0, "Not connected.\n", ""
        if base == "ip" and args[:3] == ["-o", "addr", "show"]:
            if self.ip_addr_fails:
                return 1, "", "Operation not permitted"
            if self.has_ip:
                return 0, ("2: wlan0    inet 192.168.1.132/24 brd "
                           "192.168.1.255 scope global wlan0\n"), ""
            return 0, "", ""
        if base == "ip" and args[:2] == ["link", "show"]:
            st = self._state()
            # ⚠️ **flags 必须跟着状态走** —— 真机就是这样:
            #   管理态关着 ⇒ `<BROADCAST,MULTICAST>` (flags 里**没有** UP)
            #   起来了但没载波(没连上 AP) ⇒ `<BROADCAST,MULTICAST,NO-CARRIER,UP>`
            #   起来了且有载波 ⇒ `<BROADCAST,MULTICAST,UP,LOWER_UP>`
            # 判「接口起没起来」看的就是 flags 里有没有 UP —— 所以这个假板子
            # 不许再一律回 `<BROADCAST,MULTICAST,UP>`(那会让「管理态关着」
            # 的用例测不出来)。
            if st == "DOWN":
                flags = "BROADCAST,MULTICAST"
            elif st == "DORMANT":
                flags = "BROADCAST,MULTICAST,NO-CARRIER,UP"
            else:
                flags = "BROADCAST,MULTICAST,UP,LOWER_UP"
            return 0, (f"3: wlan0: <{flags}> mtu 1500 "
                       f"state {st} mode DEFAULT "
                       f"group default qlen 1000\n"), ""
        if base == "ip" and args[:2] == ["link", "set"]:
            action = args[3] if len(args) > 3 else "?"
            self.link_set_log.append(action)
            # 第一次 `set up` 失败 ⇒ 开关那一步就算「没成」
            if self.fail_up_once and action == "up" and not self.up_failed:
                self.up_failed = True
                return 1, "", 'Cannot find device "wlan0"'
            # 状态跟着命令走: down ⇒ DOWN; up ⇒ 只在「现在是 DOWN」时变 UP
            # (DORMANT 状态 `ip link set up` 是不改的 —— 它已经 up, 只是
            #  射频还没就绪; 这正是 2026-11-06 那两台好板子的真实状态)
            if not self.state_seq:
                if action == "down":
                    self.cur_state = "DOWN"
                elif action == "up" and self.cur_state == "DOWN":
                    # NM 管的接口: 它按自己的状态机把手工 up 顶回去 ⇒ 不生效
                    # (2026-11-06 实测: 接口 10 秒一直 DOWN)
                    if self.up_no_effect:
                        pass            # 驱动没反应: 退出 0, 但接口纹丝不动
                    elif not (self.nm_type and self.nm_keeps_down):
                        # ⚠️ `up_gives_dormant`: 工位没网 ⇒ 接口起来了,
                        #   但**没有载波**(没连上 AP)⇒ 链路状态 DORMANT。
                        #   管理态仍是 UP —— 判「起来没」要看管理态。
                        self.cur_state = ("DORMANT" if self.up_gives_dormant
                                          else "UP")
            return 0, "", ""
        if base == "rfkill" and args[:1] == ["list"]:
            # ⚠️ 格式照真机: 设备名单独一行, Soft/Hard blocked 各自缩进一行
            #    (正则只认 `^[ \t]*(Soft|Hard) blocked:` 那种行)
            if self.rfkill_blocked:
                return 0, ("0: wlan0: Wireless\n"
                           "\tSoft blocked: yes\n\tHard blocked: no\n"), ""
            return 0, ("0: wlan0: Wireless\n"
                       "\tSoft blocked: no\n\tHard blocked: no\n"), ""
        if base == "rfkill" and args[:1] == ["unblock"]:
            if not self.rfkill_stays_blocked:
                self.rfkill_blocked = False
            return 0, "", ""
        if base == "iw" and "scan" in args:
            return 0, self.scan_out, ""
        return 0, "", ""

    # ── 断言帮手 ──
    def issued(self, *frags):
        """有没有发过含这些片段的命令。"""
        for c in self.calls:
            joined = " ".join(c)
            if all(f in joined for f in frags):
                return True
        return False

    def link_set_calls(self):
        return [c for c in self.calls
                if c[-2:] in (["link", "show"],) or
                (len(c) >= 2 and c[-3:-1] == ["link", "set"])]


class Patch:
    """把模块里的几个名字换掉, 退出时还原。"""

    def __init__(self, **kw):
        self.kw = kw
        self.saved = {}

    def __enter__(self):
        for k, v in self.kw.items():
            self.saved[k] = getattr(W, k)
            setattr(W, k, v)
        return self

    def __exit__(self, *exc):
        for k, v in self.saved.items():
            setattr(W, k, v)
        return False


class FakeOs:
    """只放行 `/sys/class/net/wlan0` 的 isdir —— 其余照常。"""

    def __init__(self, real, extra_dirs):
        self._real = real
        self.path = self._Path(real.path, extra_dirs)

    def __getattr__(self, name):
        return getattr(self._real, name)

    class _Path:
        def __init__(self, real, extra):
            self._real = real
            self._extra = extra

        def __getattr__(self, name):
            return getattr(self._real, name)

        def isdir(self, p):
            return True if p in self._extra else self._real.isdir(p)


async def run_item(board, test_config=None):
    """跑一遍 WiFi 项, 返回 (result, board)。"""
    cfg = {"iface": "wlan0", "times": 1}
    cfg.update(test_config or {})
    ctx = FakeCtx(cfg)
    item = W.WiFiTest()
    item._run = board.run
    result = await item.run(ctx)
    return result, ctx


def patched(board, carrier=None):
    """把模块的外部依赖全换成假的。

    ⚠️ `nmcli` **默认当没装** —— 只有当这块「板子」明说 nm_type 时才
       给它一个路径(不然每个用例都会莫名其妙地变成 NM 托管的板子)。
    """
    return Patch(
        find_tool=lambda name: (
            None if (name == "nmcli" and board.nm_type is None)
            else f"/usr/bin/{name}"),
        with_sudo=lambda cmd, **kw: list(cmd),
        sudo_hint_if_unavailable=lambda result, name: True,
        _carrier_associated=lambda iface: carrier,
        os=FakeOs(os, {"/sys/class/net/wlan0"}),
    )


# ══════════════════════════════════════════════════════════════════════
section("【1】射频空闲 ⇒ 软件开关照做(这一项不能测成残废)")

board = FakeBoard(radio_connected=False)
with patched(board, carrier=False):
    result, ctx = asyncio.run(run_item(board))

check(board.issued("ip", "link", "set", "wlan0", "down"),
      "射频空闲 ⇒ 发了 `ip link set wlan0 down`", board.calls)
check(board.issued("ip", "link", "set", "wlan0", "up"),
      "…… 接着发了 `ip link set wlan0 up`", board.calls)
check(result.status == "passed", "整项通过", result.status)
check(result.metrics.get("degraded") is False,
      "没降级(软件开关做了, 扫描也扫到 AP)", result.metrics.get("degraded"))
check(result.metrics.get("radio_busy_before") == "idle",
      "记下了测之前射频是空闲的", result.metrics.get("radio_busy_before"))
check(result.metrics.get("radio_cycle_skipped") is False,
      "记下了 ① **没有**被跳过", result.metrics.get("radio_cycle_skipped"))


section("【2】射频关联着 AP ⇒ **一条 ip link set 都不许发**")

board = FakeBoard(radio_connected=True)
with patched(board, carrier=True):
    result, ctx = asyncio.run(run_item(board))

check(not board.issued("link", "set"),
      "射频关联着 ⇒ 一条 `ip link set` 都没发", board.calls)
check(board.issued("iw", "scan"),
      "只读检查照做 —— 扫描还是跑了", board.calls)
check(result.status == "passed", "只读部分通过 ⇒ 整项仍是通过", result.status)
check(result.metrics.get("degraded") is True,
      "但**降级**了(有一小段没测)", result.metrics.get("degraded"))
check("关联" in (result.metrics.get("degraded_reason") or ""),
      "降级原因写明了「射频正关联着 AP, 跳过软件开关」",
      result.metrics.get("degraded_reason"))
check(result.metrics.get("radio_busy_before") == "connected",
      "记下了测之前射频是连着的", result.metrics.get("radio_busy_before"))
check(result.metrics.get("radio_cycle_skipped") is True,
      "记下了 ① 被跳过", result.metrics.get("radio_cycle_skipped"))
check("关联" in (result.metrics.get("radio_cycle_skip_reason") or ""),
      "跳过原因单开一格写明(不跟别的降级原因混在一起)",
      result.metrics.get("radio_cycle_skip_reason"))
check(any("跳过软件开关" in ln for ln in ctx.logs),
      "日志里明写了跳过(工人当场能看见)",
      [ln for ln in ctx.logs if "跳过" in ln])


section("【3】三个探针都答不上来 ⇒ 按「关联着」处理(保守, 不能赌)")

board = FakeBoard(radio_connected=None, ip_addr_fails=True)
with patched(board, carrier=None):
    result, ctx = asyncio.run(run_item(board))

check(not board.issued("link", "set"),
      "探测失败 ⇒ 一样不开关", board.calls)
check(result.metrics.get("radio_busy_before") == "unknown",
      "记的是 unknown", result.metrics.get("radio_busy_before"))
check(result.metrics.get("degraded") is True, "并且降级", result.metrics)
check(result.metrics.get("radio_cycle_skipped") is True,
      "① 记成被跳过", result.metrics.get("radio_cycle_skipped"))


section("【3b】厂商驱动不管 carrier、iw 又要特权 ⇒ 靠「接口上有 IP」兜住")

board = FakeBoard(radio_connected=None, has_ip=True)
with patched(board, carrier=False):
    result, ctx = asyncio.run(run_item(board))

check(not board.issued("link", "set"),
      "iw 答不上 + carrier 说 0 + 接口有 IP ⇒ 仍然不开关", board.calls)
check(result.metrics.get("radio_busy_before") == "connected",
      "判成 connected(有 IP = 至少关联成功过一次)",
      result.metrics.get("radio_busy_before"))


section("【4】carrier 探针单独说「连着」⇒ 也一样不开关")

board = FakeBoard(radio_connected=None)     # iw 问不出来
with patched(board, carrier=True):           # 只有 carrier 答得上
    result, ctx = asyncio.run(run_item(board))

check(not board.issued("link", "set"),
      "carrier 说连着 ⇒ 不开关(探针取并集)", board.calls)
check(result.metrics.get("radio_busy_before") == "connected",
      "记的是 connected", result.metrics.get("radio_busy_before"))


section("【5】配置能整个关掉软件开关")

board = FakeBoard(radio_connected=False)
with patched(board, carrier=False):
    result, ctx = asyncio.run(run_item(board, {"software_switch": False}))

check(not board.issued("link", "set"),
      "software_switch: false ⇒ 连空闲的射频也不碰", board.calls)
check(board.issued("iw", "scan"), "只读检查照做", board.calls)
check(result.metrics.get("degraded") is True, "降级", result.metrics)
check("配置关了软件开关" in (result.metrics.get("degraded_reason") or ""),
      "降级原因写明是配置关的", result.metrics.get("degraded_reason"))
check(result.metrics.get("radio_cycle_skipped") is True
      and "配置关了软件开关" in (result.metrics.get("radio_cycle_skip_reason") or ""),
      "① 跳过原因也单独记了(报告里一眼能看出 ① 没测)",
      result.metrics.get("radio_cycle_skip_reason"))


section("【6】降级原因可以有好几个, 都得留在报告里")

board = FakeBoard(radio_connected=True, scan_out=SCAN_NO_AP)
with patched(board, carrier=True):
    result, ctx = asyncio.run(run_item(board))

reason = result.metrics.get("degraded_reason") or ""
check(result.metrics.get("degraded") is True, "降级", result.metrics)
check("关联" in reason, "原因里有「跳过软件开关」", reason)
check("环境无 AP" in reason, "原因里也有「环境无 AP」(4c)", reason)
check(result.status == "passed", "4c 降级通道仍算通过", result.status)


section("【7】接口开关前就不是 UP ⇒ 不去开关它, 本项**跳过**(不判失败)")

# ⚠️ **这一段的判据 2026-11-06 改过两次** ——
#    ① 以前只要「等不到 UP」就判 failed, 实测把**两台好板子**判成了 failed
#       (报告原话: 「set up 后 10.1s 内 state 始终是 'DOWN'」)⇒ 改成:
#         · **我们动过它**(①真做了开关)⇒ 它起不来 = 驱动问题 ⇒ 判 failed;
#         · **我们没动过它**(①跳过了)⇒ 现在的状态不是我们造成的 ⇒ 分不清是
#           「还没初始化完」还是「驱动有毛病」⇒ 判**跳过**(没测到, 不进
#           通过率分母, 人工确认放行)。
#    ② 后来发现 **DORMANT ≠ 起不来** —— DORMANT = 接口已经起来了, 只是
#       **没有载波**(没连上 AP)。**产线上没网的环境就是这个状态** ⇒
#       拿它当「起不来」⇒ 这一项在产线上**永远判「未测」** ⇒ 等于白写
#       (2026-11-06 产线实跑: 报告里 WiFi 判 skipped, 原因「始终 DORMANT」)。
#       ⇒ 现在的规矩: 判「接口起没起来」看**管理状态**(flags 里有没有 UP),
#         DORMANT / NO-CARRIER 都算**起来了** ⇒ 本项**照样往下测**
#         (④ 扫描不需要网络)。
board = FakeBoard(radio_connected=False, link_state="DORMANT")
with patched(board, carrier=False):
    result, ctx = asyncio.run(run_item(board, {"state_timeout_sec": 1.0}))

check(not board.issued("link", "set", "wlan0", "down"),
      "开关前接口就没有载波(DORMANT) ⇒ **一条 down 都不发**", board.calls)
check(result.metrics.get("radio_cycle_skipped") is True,
      "① 记成跳过(没去开关它)", result.metrics.get("radio_cycle_skipped"))
check(result.metrics.get("ip_link_admin") == "UP",
      "管理态是 UP —— 接口**确实起来了**(DORMANT 只是没载波)",
      result.metrics.get("ip_link_admin"))
check(result.metrics.get("ip_link_state") == "DORMANT",
      "链路状态照样记成 DORMANT(没连上 AP), 两件事分开记",
      result.metrics.get("ip_link_state"))
check(result.status == "passed",
      "**没有载波 ≠ 起不来** ⇒ 本项照样测下去(不是 skipped)", result.status)


section("【7b】**我们动过它** ⇒ 它起不来就是驱动问题 ⇒ 判 failed")

# 剧本: 开关前是 UP(所以我们敢开关), 开关之后一直 DOWN ⇒ 驱动没救回来
board = FakeBoard(radio_connected=False, state_seq=["UP", "DOWN"])
with patched(board, carrier=False):
    result, ctx = asyncio.run(run_item(board,
                                        {"state_timeout_sec": 1.0,
                                         "switch_settle_sec": 0.1}))

check(board.issued("link", "set", "wlan0", "down"),
      "开关前接口是 UP ⇒ 开关照做", board.calls)
check(result.status == "failed", "开关之后起不来 ⇒ 判 failed", result.status)
check(result.metrics.get("step_failed") == 2,
      "指出是第 2 步(状态确认)", result.metrics.get("step_failed"))


section("【7c】**我们动过它, 就得我们松手** —— 失败之后必须把接口恢复成 UP")

# ⚠️ **2026-11-06 实测的自我强化**: 以前开关失败就直接返回, 接口留在
#    DOWN ⇒ 这块板子的无线从此是断的(工人原话「设备wifi是好的, 但是没
#    切换到」—— 设备好好的, 是**测试把它按下去没松手**)⇒ 下一轮看到的是
#    一条「空闲」的射频 ⇒ 又开关 ⇒ 又失败 ⇒ 连着几轮都报 failed, 看着
#    像板子坏了。
board = FakeBoard(radio_connected=False, state_seq=["UP", "DOWN"])
with patched(board, carrier=False):
    result, ctx = asyncio.run(run_item(board,
                                        {"state_timeout_sec": 1.0,
                                         "switch_settle_sec": 0.1}))

check(board.link_set_log.count("up") >= 2,
      "失败之后**又发了一次 up** —— 手松开了(接口不许留在 DOWN)",
      board.link_set_log)
check(result.metrics.get("restored_state") is not None,
      "收尾把接口状态记进了报告", result.metrics.get("restored_state"))
check(result.metrics.get("restore_failed") is True,
      "恢复不回来 ⇒ 在指标里大声说清楚(那是操作事故, 不是设备缺陷)",
      result.metrics.get("restore_failed"))
check(any("恢复" in ln for ln in ctx.logs),
      "日志里也写了(工人当场能看见)",
      [ln for ln in ctx.logs if "恢复" in ln])


section("【8】射频项的位置 —— 排在 **GPU 之前**(2026-11-04 用户定的)")

# ⚠️ **这条断言 2026-11-04 改过方向**: 原来要求「射频项排在整轮最后」
#    (2026-10-30 的理由: 射频是唯一一项会动整机全局状态的, 放最后万一它把
#    板子带崩, 崩的也是最后一步, 老化负载早就跑完落库了)。
#    ⇒ 用户 2026-11-04 改判: 「wifi 还是放到 GPU 测试之前, 然后蓝牙放
#      wifi 后面」⇒ 蓝牙项(2026-11-04 已实现, §6.4.1)紧跟在 WiFi 后面,
#      两者一起在重载之前。
#    ⇒ 代价: 万一射频项把板子带崩, 后面 GPU/CPU 跑不到(写在这里, 别装看不见)。
from server.runner import RECOMMENDED_ORDER                   # noqa: E402


def _pos(name):
    """名字在推荐顺序里的位置; 不在队列里 ⇒ **-1**。

    ⚠️ 故意**不抛异常** —— `.index()` 找不到就抛 ValueError, 整个测试
    直接崩掉 ⇒ 那样「红了」就分不清是**被断言咬住的**还是**自己崩了**
    (变异脚本里那条「红了但一个 ✗ 都没有 = 崩了」的判据就靠这个)。
    """
    seq = list(RECOMMENDED_ORDER)
    return seq.index(name) if name in seq else -1


_i_wifi = _pos("wifi")
_i_gpu = _pos("gpu")
_i_cpu = _pos("cpu")

check(_i_wifi >= 0,
      "射频项在队列里(不许整轮都不测 WiFi)", list(RECOMMENDED_ORDER))
check(_i_wifi >= 0 and _i_gpu >= 0 and _i_wifi < _i_gpu,
      "射频项排在 **GPU 渲染之前**", list(RECOMMENDED_ORDER))
check(_i_wifi >= 0 and _i_cpu >= 0 and _i_wifi < _i_cpu,
      "射频项也排在 **CPU 压力之前**", list(RECOMMENDED_ORDER))
check(bool(RECOMMENDED_ORDER) and RECOMMENDED_ORDER[-1] == "cpu",
      "整轮**最后一项是 CPU 压力**(重载收尾)", list(RECOMMENDED_ORDER))


section("【9】接口由 **NetworkManager** 托管 ⇒ 不跟它抢(不手工开关)")

# ⚠️ **2026-11-06 实测**: 这批产线板子上跑着 NetworkManager, 它管着 wlan0。
#    手工 `ip link set wlan0 up` 之后, NM 按自己的状态机把接口**又按下去**
#    ⇒ 10 秒一直 DOWN ⇒ ①「开关响应」看着过了(两条命令都退出 0), ②失败
#    ⇒ 报告写「驱动异常」—— **真因(NM 抢地盘)从头到尾没人查过**。
#    ⇒ 量到的是「NM 的状态机」, 不是「射频的驱动」⇒ ① 这步不算数。
board = FakeBoard(radio_connected=False, link_state="UP", nm_type="wifi")
with patched(board, carrier=False):
    result, ctx = asyncio.run(run_item(board))

check(not board.issued("link", "set", "wlan0", "down"),
      "NM 托管 ⇒ **一条 down 都不发**", board.calls)
check(result.metrics.get("iface_manager") == "NetworkManager",
      "报告里记下了「这个接口归 NM 管」", result.metrics.get("iface_manager"))
check(result.metrics.get("radio_cycle_skipped") is True,
      "① 记成跳过", result.metrics.get("radio_cycle_skipped"))
check("NetworkManager" in (result.metrics.get("radio_cycle_skip_reason") or ""),
      "跳过原因写明是 NM 托管", result.metrics.get("radio_cycle_skip_reason"))
check(result.status == "passed", "只读部分通过 ⇒ 整项仍是通过", result.status)
check(result.metrics.get("degraded") is True, "降级(有一小段没测)", result.metrics)


section("【10】NM 托管的接口起不来 ⇒ 判**跳过**(不是失败)")

board = FakeBoard(radio_connected=False, link_state="DOWN", nm_type="wifi",
                  nm_keeps_down=True)
with patched(board, carrier=False):
    result, ctx = asyncio.run(run_item(board, {"state_timeout_sec": 1.0}))

check(result.status == "skipped",
      "起不来但责任不在射频 ⇒ 判跳过(不是 failed)", result.status)
check(result.metrics.get("skipped_reason") == "iface_down_nm_managed",
      "跳过原因码单开一格", result.metrics.get("skipped_reason"))
check("NetworkManager" in (result.error or ""),
      "报告里说清了是谁按着它", result.error)


section("【11】`set up` 那句失败 ⇒ 也要把接口恢复成 UP")

# 开关只走了一半(down 成功, up 失败) ⇒ 接口此刻是 DOWN ⇒ 收尾必须再拉一把,
# 否则这块板子的无线从此是断的(下一轮看着像「板子坏了」)。
board = FakeBoard(radio_connected=False, fail_up_once=True)
with patched(board, carrier=False):
    result, ctx = asyncio.run(run_item(board, {"switch_settle_sec": 0.1}))

check(result.status == "failed", "开关的 up 失败 ⇒ 本项判 failed", result.status)
check(result.metrics.get("step_failed") == 1,
      "指出是第 1 步(软件开关)", result.metrics.get("step_failed"))
check(board.link_set_log == ["down", "up", "up"],
      "收尾又发了一次 up(把接口拉回 UP)", board.link_set_log)
check(board.cur_state == "UP", "这块板子的无线不是断的(状态回到 UP)",
      board.cur_state)
check(result.metrics.get("restored_state") == "UP",
      "恢复结果记进了报告", result.metrics.get("restored_state"))
check("restore_failed" not in result.metrics,
      "恢复成功了 ⇒ 不留「恢复失败」的标记", result.metrics.get("restore_failed"))


section("【12】射频被 rfkill 屏蔽 ⇒ **先查再开关**(⓪ 前置检查)")

# ⚠️ **2026-11-06 实测**: 射频被软屏蔽的时候, `ip link set up` **照样退出 0**
#    (命令没报错), 但接口就是不起来 ⇒ ①「开关响应」看着像过了, ②必然失败
#    ⇒ 报告写「驱动异常」—— **真因(被屏蔽)从头到尾没人查过**。
board = FakeBoard(radio_connected=False, link_state="UP", rfkill_blocked=True)
with patched(board, carrier=False):
    result, ctx = asyncio.run(run_item(board))

check(board.issued("rfkill", "unblock"),
      "⓪ 发现被屏蔽 ⇒ 先 unblock", board.calls)
check(any("⓪" in ln and "屏蔽" in ln for ln in ctx.logs),
      "⓪ 这一步在日志里留了痕(不是悄悄发生的)",
      [ln for ln in ctx.logs if "⓪" in ln])
check(result.metrics.get("rfkill_blocked") == "no",
      "unblock 之后重新查了一遍(记的是复查结果)",
      result.metrics.get("rfkill_blocked"))
check(result.status == "passed", "unblock 成功 ⇒ 整项通过", result.status)


section("【12b】unblock 之后**仍然**被屏蔽 ⇒ 那才是真的设备问题")

board = FakeBoard(radio_connected=False, link_state="UP",
                  rfkill_blocked=True, rfkill_stays_blocked=True)
with patched(board, carrier=False):
    result, ctx = asyncio.run(run_item(board))

check(result.status == "failed", "unblock 也没用 ⇒ 判 failed", result.status)
check(result.metrics.get("step_failed") == 3,
      "指出是第 3 步(射频检查)", result.metrics.get("step_failed"))


section("【13】software_switch: \"force\" ⇒ 就算 NM 托管也照做开关")

# 「force」是**留给专门复现那个故障的人**的档位: 明知道 NM 会抢, 也要非
# 开关一次不可(用它把「NM 抢地盘」这个假设证伪)。默认档绝不做这件事。
board = FakeBoard(radio_connected=False, link_state="UP", nm_type="wifi")
with patched(board, carrier=False):
    result, ctx = asyncio.run(run_item(board, {"software_switch": "force"}))

check(board.issued("link", "set", "wlan0", "down"),
      "force ⇒ 开关照做(NM 托管也不例外)", board.calls)
check(result.metrics.get("radio_cycle_skipped") is False,
      "① 不算跳过", result.metrics.get("radio_cycle_skipped"))
check(result.metrics.get("degraded") is False,
      "不降级(开关做了, 扫描也扫到 AP)", result.metrics.get("degraded"))


section("【14】**我们动过它, 就得我们松手** —— 这一条**通过**的路也要恢复")

# 失败路径恢复接口, 已经由【7c】/【11】守着了 —— 但**通过**的那条路
# 也得恢复: 我们把接口 down 过一次, 走的时候就要把它 up 回去。
# ⇒ 不然下一轮看到的还是「一个我们按下去的接口」(§6.4.2 的自我强化)。
board = FakeBoard(radio_connected=False, link_state="UP")
with patched(board, carrier=False):
    result, ctx = asyncio.run(run_item(board))

check(result.status == "passed", "这一项通过", result.status)
check(board.issued("link", "set", "wlan0", "down"),
      "确实动了接口(所以才有得恢复)", board.calls)
check(board.cur_state == "UP",
      "走的时候接口是 UP 的(不是我们按下去的 DOWN)", board.cur_state)
check(result.metrics.get("restored_state") == "UP",
      "恢复结果记进了报告(通过的那条路也要记)", result.metrics.get("restored_state"))
check(result.metrics.get("restore_failed") not in (True, "yes"),
      "恢复成功了 ⇒ 不留「恢复失败」的标记", result.metrics.get("restore_failed"))


section("【15】**没网的工位也能测** —— 没载波 ≠ 起不来, 本项照样测下去")

# 剧本: 一块**完全没有网络**的板子 ——
#   · wlan0 管理态是 UP, 但**没有载波**(= 没关联上 AP ⇒ state DORMANT);
#   · 周边一个 AP 都没有 ⇒ 扫描跑完了但扫不到 SSID。
# ⇒ 这一项**必须测得出结论**(射频通路是好的), 而不是判「未测」——
#    否则产线上没网的工位, 这一项永远白跑(实测 2026-11-06 产线报告:
#    WiFi 判 skipped, 原因「无线接口 wlan0 起不来(始终 DORMANT」)。
board = FakeBoard(radio_connected=False, link_state="DORMANT", scan_out=SCAN_NO_AP)
with patched(board, carrier=False):
    result, ctx = asyncio.run(run_item(board, {"state_timeout_sec": 1.0}))

check(result.status == "passed",
      "没网 + 没 AP ⇒ **照样测得出结论**(通过, 不是 skipped)", result.status)
check(not board.issued("link", "set"),
      "接口管理态已经是 UP ⇒ **一条 `ip link set` 都不发**"
      "(已经起来了就别再拉 —— 手工拉是 NM 抢地盘的动作)",
      board.calls)
check(result.metrics.get("ip_link_admin") == "UP",
      "管理态 UP(接口起来了)", result.metrics.get("ip_link_admin"))
check(result.metrics.get("scan_cmd_ok") is True,
      "扫描命令成功完成了(射频通路是通的)", result.metrics.get("scan_cmd_ok"))
check(result.metrics.get("scan_ssid_count") == 0,
      "周边确实一个 SSID 都没扫到", result.metrics.get("scan_ssid_count"))
check("环境无 AP" in (result.metrics.get("degraded_reason") or ""),
      "降级原因写明「环境无 AP」(这一小段没测, 但设备能力验过了)",
      result.metrics.get("degraded_reason"))
check(result.metrics.get("skipped_reason") is None,
      "**不是** skipped(没测到) —— 它测到了", result.metrics.get("skipped_reason"))


section("【16】接口**管理态**真的 DOWN ⇒ 仍然判**未测**(不是失败)")

# 剧本: 接口管理态就是关着的(flags 里没有 UP), 而且 `ip link set up`
# 退出 0 却**纹丝不动**(驱动没反应) ⇒ 拉不起来 ⇒ 没测到 ⇒ skipped。
# ⚠️ 这一段守的是**别把判据放宽过头**: DORMANT 算起来, DOWN 不算。
board = FakeBoard(radio_connected=False, link_state="DOWN", up_no_effect=True)
with patched(board, carrier=False):
    result, ctx = asyncio.run(run_item(board, {"state_timeout_sec": 1.0}))

check(result.status == "skipped",
      "管理态真的 DOWN ⇒ 判**未测**(不是 failed, 也不是 passed)",
      result.status)
check(result.metrics.get("skipped_reason") == "iface_not_up",
      "跳过原因码单开一格", result.metrics.get("skipped_reason"))
check(result.metrics.get("ip_link_admin") == "DOWN",
      "管理态记的是 DOWN(跟链路的 DORMANT 分开)", result.metrics.get("ip_link_admin"))


section("【17】收尾判「恢复没恢复」看**管理状态**, 不看载波")


# ⚠️ **2026-11-11 查出来的假警报**: 收尾拿的是**载波**(carrier),
#   拿它跟 "UP" 比 ⇒ 在**没网的工位**上载波恒是 DORMANT ⇒ 明明接口
#   已经恢复成 UP, 却报「⚠ 收尾没能把接口恢复成 UP」⇒ 工人以为板子坏了
#   ⇒ **冤枉好板子**(跟 WiFi 那条「我们动过它吗」是同一个道理)。
#
# 剧本: 工位没网 ⇒ 载波一直是 DORMANT; 但**管理状态**(flags 里的 UP)是好的。
board = FakeBoard(radio_connected=False, up_gives_dormant=True)
with patched(board, carrier=False):
    result, ctx = asyncio.run(run_item(board, {"switch_settle_sec": 0.1}))

check(result.metrics.get("restored_state") == "UP",
      "管理态是 UP ⇒ 收尾判「恢复好了」(不拿载波当判据)",
      result.metrics.get("restored_state"))
check("restore_failed" not in result.metrics,
      "没有「恢复失败」的标记(没网的工位上不该有)",
      result.metrics.get("restore_failed"))
check(result.metrics.get("restored_carrier") == "DORMANT",
      "载波另记一笔(DORMANT = 没连上 AP, 跟恢复成没成功无关)",
      result.metrics.get("restored_carrier"))
check(not any("没能把接口恢复成 UP" in ln for ln in ctx.logs),
      "日志里没有那句假警报", [ln for ln in ctx.logs
                             if "恢复" in ln])


print("\n" + ("全部通过" if not FAILS else f"有失败项 ({len(FAILS)})"))
for f in FAILS:
    print("  ✗ " + f)
sys.exit(0 if not FAILS else 1)
