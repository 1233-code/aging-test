"""回归测试: `scripts/check_eth.sh` 找工具的口径(2026-11-11 上板时实测的坑)。

起因(真实故障, 不是假想):
    上板核验时跑 `bash <盘>/scripts/check_eth.sh`, 脚本报:
        ✗ 未装 ethtool —— U 盘版: 盘上自带 fio / glmark2-es2, 其余工具用板子出厂那份
    而**同一台机器**上 `/api/deps/check` 报的是:
        {"name":"ethtool", "ok":true, "path":"/usr/sbin/ethtool"}
    ⇒ **两个说法互相打脸**。

    病根: 脚本用 `command -v ethtool` 找 ⇒ 而 `ethtool` 装在 **`/usr/sbin`**,
    `/usr/sbin` **不在**非登录 shell 的 PATH 里 ⇒ 找不到 ⇒ 误报「没装」。
    ⇒ 而 Python 侧(`server/tests/_tools.py:find_tool()`)是**按标准目录**找的
    ⇒ 它说「装好了」⇒ **同一台机器两个口径**。

    后果: 工人照这个脚本的结论去排障 ⇒ 追的是一个**不存在的故障**
    ⇒ 而**真故障**(网线没插)反而被这条假象盖住。

这个测试守三件事:
  ① **静态**: 脚本里的 `find_tool` 跟 Python 侧 `find_tool()` 搜**同一批目录**
     (`/usr/local/sbin /usr/sbin /sbin /usr/local/bin`) —— 两边口径不许分叉;
  ② **静态**: 脚本里不再有裸的 `command -v ethtool` 之类(那正是病根);
  ③ **动态**: **真跑一遍脚本**, 把 PATH 里 `/usr/sbin` 去掉、`AGING_TOOL_DIR`
     指向一个临时目录 ⇒ 脚本**必须仍然找得到工具** ⇒ 修法生效。
     并且验证「随包工具目录**排在最前**」(跟 Python 侧一致: 盘上那份才是
     这一版的工具, 板子上碰巧装的旧版不该被用)。

跑法: .venv/bin/python -m tests.test_check_eth_find_tool
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 变异脚本会把**改坏的那一份**指过来(AGING_CHECK_ETH_SCRIPT)。
SCRIPT = os.environ.get("AGING_CHECK_ETH_SCRIPT",
                         os.path.join(REPO, "scripts", "check_eth.sh"))
PY_TOOLS = os.path.join(REPO, "server", "tests", "_tools.py")

FAILS = []
PASSES = 0


def check(cond, msg, extra=""):
    global PASSES
    if cond:
        PASSES += 1
        print("  ✓ " + msg)
    else:
        FAILS.append(msg)
        print("  ✗ " + msg + ("  → " + str(extra) if extra else ""))


def section(title):
    print("\n" + title)


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def find_tool_block(src):
    """把脚本里的 find_tool 函数体抠出来。"""
    m = re.search(r"^find_tool\(\)\s*\{(.*?)^\}", src, re.S | re.M)
    return m.group(1) if m else None


def real_iface():
    """挑一个这台机器上真实存在的网口(脚本第 ① 步就查 /sys/class/net)。"""
    try:
        for name in sorted(os.listdir("/sys/class/net")):
            if name.startswith(("eth", "en")):
                return name
    except OSError:
        pass
    return None


def write_tool(tooldir, name, body):
    path = os.path.join(tooldir, "bin", name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(body)
    os.chmod(path, 0o755)


def run_script(tooldir, iface):
    """在**没有 /usr/sbin 的 PATH**下真跑一遍脚本。"""
    env = {
        "PATH": "/usr/bin:/bin",      # ← 故意**不含** /usr/sbin /sbin
        "HOME": os.environ.get("HOME", "/tmp"),
        "AGING_TOOL_DIR": tooldir,
    }
    p = subprocess.run(["bash", SCRIPT, iface], env=env,
                       capture_output=True, text=True, timeout=120)
    return (p.stdout or "") + (p.stderr or "")


def main():
    src = read(SCRIPT)

    section("① 脚本里有 find_tool, 且搜的目录跟 Python 侧一样")
    block = find_tool_block(src)
    check(block is not None, "脚本里定义了 find_tool()", src[:200])
    py = read(PY_TOOLS)
    py_dirs = re.search(r'for dd in \(([^)]*)\):', py)
    check(py_dirs is not None, "Python 侧 find_tool() 里的目录表找得到")
    if block is not None and py_dirs is not None:
        want = re.findall(r'"([^"]+)"', py_dirs.group(1))
        got = re.findall(r"(/usr/local/sbin|/usr/sbin|/sbin|/usr/local/bin)",
                         block)
        check(got == want,
              "shell 侧搜的目录跟 Python 侧**完全一致**",
              "shell=%s python=%s" % (got, want))
        check("/usr/sbin" in got, "**包含 /usr/sbin**(ethtool/udhcpc/ip 都在那儿)",
              got)

    section("② 脚本里不再有裸的 `command -v <工具>`")
    # ⚠️ **只看代码行, 不看注释** —— 注释里恰恰要**写明**「原来就是用
    #    `command -v ethtool` 才出的问题」, 那不是残留的代码。
    code_only = "\n".join(
        ln for ln in src.splitlines() if not ln.strip().startswith("#"))
    for tool in ("ethtool", "udhcpc", "ping", "ip"):
        bare = "command -v %s" % tool
        check(bare not in code_only,
              "没有 `%s`(PATH 里没有 /usr/sbin ⇒ 找不到 ⇒ 误报没装)"
              % bare)

    section("③ 真跑一遍: PATH 里没有 /usr/sbin, 但工具在随包目录里 ⇒ 仍要找到")
    iface = real_iface()
    if iface is None:
        check(False, "这台机器上找不到网口(eth*/en*) ⇒ 动态测不了", "/sys/class/net")
        return finish()
    print("    (用网口 %s)" % iface)

    tmp = tempfile.mkdtemp(prefix="eth_findtool_")
    try:
        tooldir = os.path.join(tmp, "tools")
        os.makedirs(os.path.join(tooldir, "bin"), exist_ok=True)
        # ethtool: 报「链路 UP」+ 一个**只有这份**才有的速度 ⇒ 能证明
        # 「随包工具目录排在最前」(板子上碰巧也装了一份的话, 用的是这份)。
        write_tool(tooldir, "ethtool",
                   "#!/bin/sh\n"
                   "echo 'Settings for $1:'\n"
                   "echo '  Speed: 9999Mb/s'\n"
                   "echo '  Link detected: yes'\n")
        write_tool(tooldir, "ip",
                   "#!/bin/sh\n"
                   "case \"$1 $2\" in\n"
                   "  '-4 addr') echo '    inet 10.11.12.13/24 brd 10.11.12.255"
                   " scope global eth0';;\n"
                   "  'route get') echo '1.1.1.1 via 10.11.12.1 dev eth0 src "
                   "10.11.12.13';;\n"
                   "  'route show') echo 'default via 10.11.12.1 dev eth0';;\n"
                   "esac\n")
        write_tool(tooldir, "ping",
                   "#!/bin/sh\n"
                   "echo '3 packets transmitted, 3 received, 0% packet loss'\n"
                   "echo 'rtt min/avg/max/mdev = 0.652/0.853/1.203 ms'\n")

        out = run_script(tooldir, iface)
        check("未装 ethtool" not in out,
              "**没有**误报「未装 ethtool」", out[:400])
        check("9999Mb/s" in out,
              "用的是**随包目录**那份 ethtool(排在 PATH 之前)",
              out[:600])
        check("Link detected: yes" in out,
              "链路检测那一段真的跑起来了", out[:600])
        check("10.11.12.13" in out,
              "`ip` 用的也是**随包目录**那份(不是 /usr/bin 里那份)",
              out[:600])
        check("通过" in out,
              "ping 那一步判「通」(不是恒定三级全败)", out[-600:])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    section("④ 修法不许靠「把 /usr/sbin 加进 PATH」这种只对某些会话有效的办法")
    check("export PATH=" not in src,
          "脚本没去改 PATH(改了对非登录 shell 也没用)", "见 src")
    check("find_tool" in src, "走的是 find_tool() 这条统一口径")

    return finish()


def finish():
    print("\n" + "─" * 60)
    if FAILS:
        print("✗ %d 项失败 / 共 %d 项" % (len(FAILS), PASSES + len(FAILS)))
        for f in FAILS:
            print("   · " + f)
        sys.exit(1)
    print("✓ 全部通过 (%d 项)" % PASSES)


if __name__ == "__main__":
    main()
