"""回归测试: 网口**静态兜底**的两件事。

① **地址要跟工位的网段走** (2026-11-11 查出来的):
    静态兜底配的是配置里写死的 `192.168.1.50/24` ⇒ 而现场工位的网段是
    **192.168.16.0/23**(网卡 192.168.16.26/23, 网关 192.168.16.1)
    ⇒ 两者**不搭界** ⇒ 静态兜底「配上了」, 但跟网关不在同一个网段
    ⇒ **一格都发不出去** ⇒ 静态兜底白配;
    再加上「静态 IP 配上了仍 ping 不通」那条判定 ⇒ 判 **failed**
    ⇒ **冤枉好板子**(真正的原因是工位网段跟配置对不上, 不是板子坏了)。

② **我们自己加的地址, 收尾要删掉**:
    `ip addr add` 加上去的地址是**我们**加的 ⇒ 测完不删, 它就留在板子上
    (下一轮 DHCP 拿到 lease ⇒ 网卡上同时有两个地址; 工人查网卡时看到
    一个自己没配过的地址 ⇒ 排查方向跑偏)。

这个测试**一条真命令都不跑** —— `_run` 整个换成假的, 只看「发了什么命令」。
不碰真网卡、不改真网络配置。
跑法: .venv/bin/python -m tests.test_ethernet_static_fallback
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.tests import ethernet as ETH              # noqa: E402

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


class FakeCtx:
    """够用的 TestContext 替身。"""

    def __init__(self, cfg=None):
        self.config = cfg or {}
        self.test_config = {}
        self.logs = []
        self.progress = []

    async def on_log(self, line):
        self.logs.append(line)

    async def on_progress(self, pct, msg):
        self.progress.append((pct, msg))

    async def on_metric(self, k, v):
        pass

    async def on_alert(self, text, level="", seconds=0):
        pass

    async def is_cancelled(self):
        return False


#: 假的命令执行器 —— 按命令内容回 canned 结果, 并把发过的命令记下来
class FakeRunner:
    def __init__(self, gateway="192.168.16.1", dhcp_rc=1, ping_rc=1,
                 raise_on_ping=False):
        self.gateway = gateway
        self.dhcp_rc = dhcp_rc
        self.ping_rc = ping_rc
        self.raise_on_ping = raise_on_ping
        self.cmds = []

    async def __call__(self, cmd, timeout=5.0):
        c = list(cmd)
        self.cmds.append(c)
        exe = os.path.basename(c[0])
        args = c[1:]
        if exe == "ethtool":
            return 0, "Link detected: yes\nSpeed: 1000Mb/s\nDuplex: Full\n"
        if exe == "udhcpc":
            return self.dhcp_rc, "no lease"
        if exe == "ip":
            if args[:2] == ["route", "show"]:
                if self.gateway:
                    return 0, (f"default via {self.gateway} dev eth0 "
                               "proto static\n")
                return 0, ""
            if args[:2] == ["addr", "add"]:
                return 0, ""
            if args[:2] == ["addr", "del"]:
                return 0, ""
            if args[:2] == ["link", "set"]:
                return 0, ""
            if args[:2] == ["-4", "addr"]:
                return 0, ""
            if args[:1] == ["-4"]:
                return 0, ""
            return 0, ""
        if exe == "ping":
            if self.raise_on_ping:
                raise RuntimeError("ping 炸了(测试这一项)")
            if self.ping_rc == 0:
                return 0, ("3 packets transmitted, 3 received, 0% packet "
                           "loss, time 2003ms\nrtt min/avg/max = 1.0/2.0/3.0 "
                           "ms")
            return 1, ("3 packets transmitted, 0 received, 100% packet "
                       "loss, time 2003ms\n")
        return 0, ""


def _install(monkey_runner):
    """把 ethernet 模块里的外部依赖换成假的。"""
    ETH.find_tool = lambda name: "/usr/bin/" + name
    ETH.with_sudo = lambda cmd, **kw: list(cmd)
    ETH.sudo_hint_if_unavailable = lambda result, name: True
    ETH.EthTest._run = lambda self, cmd, timeout=5.0: monkey_runner(cmd, timeout)


def _add_cmds(runner):
    return [c for c in runner.cmds
            if os.path.basename(c[0]) == "ip" and c[1:3] == ["addr", "add"]]


def _del_cmds(runner):
    return [c for c in runner.cmds
            if os.path.basename(c[0]) == "ip" and c[1:3] == ["addr", "del"]]


async def _one(cfg_eth, runner, gateway="192.168.16.1", dhcp_rc=1,
               ping_rc=1, raise_on_ping=False):
    r = FakeRunner(gateway=gateway, dhcp_rc=dhcp_rc, ping_rc=ping_rc,
                   raise_on_ping=raise_on_ping)
    _install(r)
    cfg = {"eth": cfg_eth}
    ctx = FakeCtx(cfg)
    t = ETH.EthTest()
    try:
        res = await t._run_once(ctx)
    except RuntimeError:
        res = None
    return res, r, ctx


def main():
    # ─────────────────────────────────────────────────────────────
    section("【1】静态兜底的地址要跟**工位网段**走")
    # ─────────────────────────────────────────────────────────────
    cfg = {"iface": "eth0", "ping_gateway": "auto",
           "static_ip": "192.168.1.50/24"}

    res, r, ctx = asyncio.run(_one(cfg, None))
    adds = _add_cmds(r)
    check(len(adds) == 1, "DHCP 失败 ⇒ 走静态兜底(加了一次地址)", adds)
    if adds:
        check(adds[0][3] == "192.168.16.50/24",
              "地址按**网关所在的网段**推(不是配置里那个 192.168.1.50/24)",
              adds[0])
    check(any("不搭界" in ln for ln in ctx.logs),
          "日志里说明了「配置里的地址跟工位网段不搭界, 改用这个」", ctx.logs[-4:])

    # ② 网关跟配置里那个地址**同一网段** ⇒ 用配置里那个(不动)
    res2, r2, _ = asyncio.run(_one(cfg, None, gateway="192.168.1.1"))
    adds2 = _add_cmds(r2)
    check(adds2 and adds2[0][3] == "192.168.1.50/24",
          "同一个网段 ⇒ 照旧用配置里那个地址", adds2)

    # ③ **取不到网关** ⇒ 不瞎猜, 用配置里那个
    res3, r3, _ = asyncio.run(_one(cfg, None, gateway=""))
    adds3 = _add_cmds(r3)
    check(adds3 and adds3[0][3] == "192.168.1.50/24",
          "取不到网关 ⇒ 用配置里那个地址(不瞎猜)", adds3)

    # ─────────────────────────────────────────────────────────────
    section("【2】**我们自己加的地址, 收尾要删掉**")
    # ─────────────────────────────────────────────────────────────
    dels = _del_cmds(r)
    check(len(dels) == 1, "测完把那个地址删掉了", dels)
    if dels:
        check(dels[0][3] == "192.168.16.50",
              "删的就是**加上去那个**", dels[0])
        check(dels[0][-1] == "eth0", "删在同一个网卡上", dels[0])

    # ③ 这一轮**炸了**(抛异常)⇒ 收尾照样要删
    res4, r4, _ = asyncio.run(_one(cfg, None, raise_on_ping=True))
    dels4 = _del_cmds(r4)
    check(len(dels4) == 1,
          "这一轮中途抛异常 ⇒ 收尾照样删(不然地址留在板子上)", dels4)

    # ④ **DHCP 成功** ⇒ 压根没加静态地址 ⇒ 也不该去删(别删了别人的)
    res5, r5, _ = asyncio.run(_one(cfg, None, dhcp_rc=0))
    check(not _add_cmds(r5), "DHCP 拿到 lease ⇒ 不加静态地址", _add_cmds(r5))
    check(not _del_cmds(r5),
          "没加过 ⇒ 收尾不去删网卡上的地址(别删了 DHCP 拿到的那个)",
          _del_cmds(r5))

    # ─────────────────────────────────────────────────────────────
    section("【3】算地址的函数本身")
    # ─────────────────────────────────────────────────────────────
    f = ETH._static_ip_for_gateway
    check(f("192.168.1.50/24", "192.168.16.1") == "192.168.16.50/24",
          "不同网段 ⇒ 按网关推一个", f("192.168.1.50/24", "192.168.16.1"))
    check(f("192.168.1.50/24", "192.168.1.1") == "192.168.1.50/24",
          "同一网段 ⇒ 不动")
    check(f("192.168.1.50/24", "") == "192.168.1.50/24",
          "没有网关 ⇒ 不动(不瞎猜)")
    check(f("192.168.1.50/24", "fe80::1") == "192.168.1.50/24",
          "网关是 IPv6 ⇒ 不动")
    check(f("192.168.1.50/24", "不是IP") == "192.168.1.50/24",
          "网关不是 IP ⇒ 不动")
    check(f("192.168.1.50/24", "192.168.16.256") == "192.168.1.50/24",
          "网关不是合法 IPv4 ⇒ 不动")

    print("\n" + ("全部通过" if not FAILS else f"有失败项 ({len(FAILS)})"))
    for x in FAILS:
        print("  ✗ " + x)
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
