"""回归测试: 板子重启之后, **死因要能自己落到报告里** (§9.3)。

**起因 (2026-10-29 新板实测)**
    整轮跑到最后一项时**板子当场重启** —— 报告上只剩一句「服务重启, 这一轮
    中途夭折(没跑完)」, **看不出板子为什么死**。而死因(kernel panic /
    thermal critical trip / watchdog reset)只留在**上一次启动**的内核日志里,
    那一轮测试的日志跟死因一起被重启动作带走了。

**这个机制长什么样**
    服务启动时发现有夭折的轮次 → 读「上一次启动」的日志尾巴 → 存成**旁挂
    文件**(`logs/boot_evidence_run<N>.txt`) → 之后导出报告时多一节
    「板子为什么死」。

**两条硬约束**
    ① **一个字都不往历史库里写** —— 证据落在独立的文件里。改历史记录要
       单独授权, 这个机制不该、也不会去动 `runs` / `results` 表。
    ② **捞不到就不画** —— 没有证据时编一段「疑似原因」比没有更糟:
       真凶是驱动 panic 时, 报告写「疑似过热」就是把排查方向带偏。

这个测试**不碰真库、不碰真硬件**: 数据目录换成临时目录, 捞日志的三条命令
换成假的。
跑法: .venv/bin/python -m tests.test_boot_evidence
"""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import boot_evidence as be          # noqa: E402
from server import report as report_mod        # noqa: E402
from server.config import Config               # noqa: E402
from tests._tmp import mkdtemp                 # noqa: E402

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


# ══════════════════════════════════════════════════════════════════════
# 数据目录换成临时目录 —— 绝不能把测试产物写进仓库, 更不能碰到真数据
DATA_DIR = mkdtemp(prefix="aging-bootev-")
os.environ["AGING_DATA_DIR"] = DATA_DIR

# 配置是全局的 —— 改之前先存一份, 跑完还回去
_SAVED_CFG = Config._data


def set_cfg(cfg):
    Config._data = cfg


section("【1】证据文件落在数据目录下, 存得进也读得回")

set_cfg({})
check(be.evidence_dir() == be.Config.data_root() / "logs",
      "证据目录 = 数据目录下的 logs/", be.evidence_dir())
check(str(DATA_DIR) in str(be.evidence_path(7)),
      "落在临时数据目录里(不是仓库里)", be.evidence_path(7))

text = "[上一次启动的 journal]\nKernel panic - not syncing: Watchdog timeout"
path = be.save_evidence(7, text)
check(path is not None and os.path.exists(path), "证据文件写出来了", path)
check(be.read_evidence(7) == text, "读回来跟写进去一模一样")
check(be.read_evidence(999) == "", "没有证据的那一轮 ⇒ 空串(不是异常)")
check(be.save_evidence(8, "") is None, "空证据不写文件(没有就不落一个空壳)")


section("【2】报告里多一节「板子为什么死」; 没有证据就不画")


def make_run():
    return {
        "id": 7,
        "sn": "SN-BOOT-EV-0001",
        "operator": "产线甲",
        "status": "stopped",
        "started_at": 1000,
        "finished_at": None,
        "summary": {"total": 3, "passed": 2, "failed": 0, "error": 0,
                    "skipped": 0, "not_run": 1, "planned": 4},
        "config": {"tests": {}},
        "error": "服务重启, 这一轮中途夭折(没跑完)",
    }


html = report_mod.render_html(make_run(), [], "SN-BOOT-EV-0001_20261029_100000.pdf",
                               boot_evidence=text)
check("板子为什么死" in html, "报告里有「板子为什么死」这一节")
check("Kernel panic - not syncing" in html, "证据原文进了报告")
check("上一次启动" in html, "写明了这是**上一次启动**的日志")

html_no = report_mod.render_html(make_run(), [], "SN-BOOT-EV-0001_20261029_100000.pdf")
check("板子为什么死" not in html_no,
      "**没有证据就不画那一节** —— 不编「疑似原因」", html_no[:0])

section("【3】导出时**自动**带上证据 —— 不用每个调用点自己记得传")


class _Stop(Exception):
    pass


captured = {}
_orig_render = report_mod.render_html
_orig_chromium = report_mod.find_chromium


def fake_render_html(run, results, filename="", retest_runs=None,
                     boot_evidence="", cpu_evidence="", **kwargs):
    captured["boot_evidence"] = boot_evidence
    captured["cpu_evidence"] = cpu_evidence
    captured["run_id"] = run.get("id")
    raise _Stop()


report_mod.render_html = fake_render_html
report_mod.find_chromium = lambda: "/bin/true"
try:
    asyncio.run(report_mod.export_pdf({"id": 7}, []))
except _Stop:
    pass
finally:
    report_mod.render_html = _orig_render
    report_mod.find_chromium = _orig_chromium

check(captured.get("run_id") == 7, "导出时用的是这一轮自己的 id", captured)
check(captured.get("boot_evidence") == text,
      "证据被自动带进报告(旁挂文件 → render_html)", captured.get("boot_evidence"))

section("【4】捞日志: 走候选、认空标记、捞不到就空串")

# ── 假的外部依赖: find_tool / run_cmd / with_sudo ──
_orig_find_tool = be.find_tool
_orig_run_cmd = be.run_cmd
_orig_with_sudo = be.with_sudo


def fake_find_tool(name):
    return "/usr/bin/" + name


def patch_cmds(handler):
    """handler(cmd) -> (rc, out, err); 假的 run_cmd 要跟真的一样是**协程**"""
    async def fake_run_cmd(cmd, timeout=5.0, input_data=None):
        return handler(cmd)

    be.find_tool = fake_find_tool
    be.run_cmd = fake_run_cmd
    be.with_sudo = lambda cmd, **kw: list(cmd)


# ① 正常: journalctl 有货
patch_cmds(lambda cmd: (0, "line-1\nline-2\nline-3", ""))
got = asyncio.run(be.previous_boot_evidence())
check("line-3" in got, "捞到了上一次启动的日志", got)
check("上一次启动的 journal" in got, "写明了来源", got)

# ② journalctl 说是空的 ⇒ 必须**跳过**, 不能把「-- No entries --」当证据
patch_cmds(lambda cmd: (0, "-- No entries --", ""))
got = asyncio.run(be.previous_boot_evidence())
check(got == "", "空标记 ⇒ 当作没捞到(不把占位话当死因)", got)

# ③ journalctl 空, 但 kern.log 有货 ⇒ 落到第二个候选
def handler_kern(cmd):
    if "journalctl" in cmd[0]:
        return (0, "No journal files were found", "")
    return (0, "kern-log-tail: thermal trip", "")


patch_cmds(handler_kern)
got = asyncio.run(be.previous_boot_evidence())
check("thermal trip" in got, "第一个候选空 ⇒ 落到第二个候选", got)
check("/var/log/kern.log" in got, "来源标对了", got)

# ④ 全都读不到(没权限 / 没日志) ⇒ 空串, **不抛异常**
patch_cmds(lambda cmd: (1, "", "Permission denied"))
got = asyncio.run(be.previous_boot_evidence())
check(got == "", "全都读不到 ⇒ 空串", got)

# ④b 跨重启累积的那种文件: 尾巴是**本次开机**的内容, 不是死因 ——
#     只贴尾巴 = 把开机 banner 当成「板子为什么死」, 读者照着它白查半天。
#     所以按关键字挑可疑行; 一条都挑不出来 ⇒ 宁可没有, 也不贴开机 banner。
BANNER = ("[   12.345678] Linux version 5.15.0\n"
          "[   12.345679] Booting the kernel.\n"
          "[   12.400000] random: crng init done\n")


def handler_banner(cmd):
    if cmd[0] == "journalctl":
        return (0, "No journal files were found", "")
    return (0, BANNER * 40, "")


patch_cmds(handler_banner)
got = asyncio.run(be.previous_boot_evidence())
check(got == "", "挑不出可疑行 ⇒ **不贴开机 banner 充数**", got[:120])


def handler_crash(cmd):
    if cmd[0] == "journalctl":
        return (0, "No journal files were found", "")
    return (0, BANNER * 40
            + "[  990.111111] Kernel panic - not syncing: Watchdog timeout\n"
            + "[  990.222222] CPU: 3 PID: 1 Comm: systemd\n", "")


patch_cmds(handler_crash)
got = asyncio.run(be.previous_boot_evidence())
check("Kernel panic" in got, "累积文件里的崩溃行被挑出来了", got[:200])
check("Booting the kernel" not in got,
      "开机 banner 没被当成死因贴进去", got[:200])

# ⑤ 工具压根没有 ⇒ 也得是空串, 不是异常
be.find_tool = lambda name: None
patch_cmds(lambda cmd: (0, "x", ""))
be.find_tool = lambda name: None
got = asyncio.run(be.previous_boot_evidence())
check(got == "", "系统里没这个工具 ⇒ 空串", got)

section("【6】上一次是**软重启**还是**硬复位** —— 分清楚, 别混为一谈")

# 实样本(抄自 logs/boot_evidence_run14.txt —— 2026-10-30 老板子 09:46
# 那次人工 reboot, 关机序列一行不缺)
ORDERLY = """Oct 30 09:46:29.991685 ubuntu2004 systemd[1]: Stopped Remount Root.
Oct 30 09:46:30.012588 ubuntu2004 systemd[1]: Reached target Shutdown.
Oct 30 09:46:30.024591 ubuntu2004 systemd[1]: Reached target Final Step.
Oct 30 09:46:30.041759 ubuntu2004 systemd[1]: systemd-reboot.service: Succeeded.
Oct 30 09:46:30.042657 ubuntu2004 systemd[1]: Finished Reboot.
Oct 30 09:46:30.068521 ubuntu2004 systemd[1]: Shutting down.
Oct 30 09:46:30.208300 systemd-shutdown[1]: Syncing filesystems and block devices.
Oct 30 09:46:30.316292 systemd-shutdown[1]: Sending SIGTERM to remaining processes...
Oct 30 09:46:30.316653 systemd-journald[201]: Journal stopped
"""

# 新板实测: 死前最后一条内核消息是 10:03:58 的 HDMI, 死在 10:06:32 ——
# 中间两分半一句日志都没有, **也没有**任何关机序列
HARD = """Oct 30 10:03:58.101112 ubuntu2004 kernel: [drm] hdmi probe ok
Oct 30 10:03:58.101113 ubuntu2004 kernel: [drm] fb0: DRM driver
"""

kind, why = be.classify_reboot(ORDERLY)
check(kind == "软重启", "有完整关机序列 ⇒ 软重启(板子没崩)", (kind, why))
check("没崩" in why, "说清了板子本身没崩", why)

kind, why = be.classify_reboot(HARD)
check(kind == "硬复位", "没有关机序列 ⇒ 硬复位", (kind, why))
check("硬拉死" in why, "说清了是硬拉死(掉电/PMIC/看门狗)", why)

kind, why = be.classify_reboot("")
check(kind == "看不出来", "没日志 ⇒ 看不出来(不瞎猜)", (kind, why))

section("【7】BT 噪音收成一行 —— 别把真死因淹掉, 但末尾那几句要留着")

bt_lines = []
for i in range(40):
    sec = i % 60
    bt_lines.append(f"Oct 30 10:06:{sec:02d}.000001 ubuntu2004 "
                    f"brcm_patchram_plus.service: Failed with result 'exit-code'.")
    bt_lines.append(f"Oct 30 10:06:{sec:02d}.100001 ubuntu2004 "
                    f"sunxi-rfkill: bt power off success")
    bt_lines.append(f"Oct 30 10:06:{sec:02d}.200001 ubuntu2004 "
                    f"sunxi-rfkill: bt power on success")
REAL = "Oct 30 10:03:58.101112 ubuntu2004 kernel: [drm] hdmi probe ok"
collapsed = be._collapse_bt_noise("\n".join(bt_lines) + "\n" + REAL)
check("BT 相关日志" in collapsed, "重复的那一大坨收成了一行说明",
      collapsed[:120])
check(collapsed.count("brcm_patchram") <= 4,
      "重复的失败行收掉了", collapsed.count("brcm_patchram"))
check("bt power on success" in collapsed,
      "末尾那几句电源轨切换**原样留着**(死前最后一次切换在那儿)")
check(collapsed.rstrip().endswith(REAL), "真内容没被挤掉", collapsed[-160:])

section("【8】journal 的窗口要开得大 —— 死因往往不在最后几行")

_seen = {}


def handler_window(cmd):
    _seen["cmd"] = list(cmd)
    return (0, "x" * 200, "")


patch_cmds(handler_window)
asyncio.run(be.previous_boot_evidence())
try:
    n = int(_seen["cmd"][_seen["cmd"].index("-n") + 1])
except (KeyError, ValueError):
    n = -1
check(n >= 1000,
      "窗口 ≥1000 行(死因可能在两分半的静默**之前**, 只看尾巴看不出来)", n)

section("【9】跨重启累积的文件: 标出这些行有多旧(老账 ≠ 这一轮)")

OLD = ("Oct 15 20:31:00.000001 ubuntu2004 kernel: "
       "Kernel panic - not syncing: Watchdog timeout")
banner = "[   12.345678] Linux version 5.15.0\n" * 100
picked = be._pick_suspect_lines(OLD + "\n" + banner, "kern.log")
check("Kernel panic" in picked, "可疑行还在", picked[:120])
check("老账" in picked, "标出了这是老账(离文件末尾太远)", picked[:300])

# 还原
be.find_tool = _orig_find_tool
be.run_cmd = _orig_run_cmd
be.with_sudo = _orig_with_sudo


section("【10】pstore —— 硬复位可能留下的内核崩溃记录(有就带上, 没有就算)")

_pstore_dir = mkdtemp(prefix="aging-pstore-")
_orig_pstore_dir = be.PSTORE_DIR
be.PSTORE_DIR = _pstore_dir
Path(_pstore_dir, "dmesg-ramoops-0").write_text(
    "Kernel panic - not syncing: watchdog reset\nCPU: 3 PID: 1 Comm: systemd\n",
    encoding="utf-8")
got = be.previous_pstore()
check("Kernel panic" in got, "pstore 里的崩溃记录读到了", got[:160])
check("dmesg-ramoops-0" in got, "写明是哪个文件", got[:160])

# 空文件不算证据
Path(_pstore_dir, "dmesg-ramoops-0").write_text("   \n", encoding="utf-8")
check(be.previous_pstore() == "", "空文件 ⇒ 当作没有(不贴空壳)", be.previous_pstore()[:80])

# 目录压根不存在 ⇒ 空串, 不是异常
be.PSTORE_DIR = "/nonexistent/pstore"
check(be.previous_pstore() == "", "目录不存在 ⇒ 空串(不抛异常)")

# 捞日志时把 pstore **带上**(它是硬复位后唯一可能还留着的崩溃记录)
be.PSTORE_DIR = _pstore_dir
Path(_pstore_dir, "dmesg-ramoops-0").write_text(
    "Kernel panic - not syncing: watchdog reset\n", encoding="utf-8")
patch_cmds(lambda cmd: (0, "Nov 02 11:48:01.024101 stress-ng[23156]: metrics\n", ""))
got = asyncio.run(be.previous_boot_evidence())
check("pstore" in got and "Kernel panic" in got,
      "上次启动的日志尾巴**带着** pstore 那段", got[-200:])
be.PSTORE_DIR = _orig_pstore_dir
# 还原外部依赖(下面几节还要用真的)
be.find_tool = _orig_find_tool
be.run_cmd = _orig_run_cmd
be.with_sudo = _orig_with_sudo


section("【11】「板子死在哪一级加压」—— 现场记录翻译成一句结论")

# 实样本: 2026-11-02 新板 SN A10000000000010 实测(报告
# A10000000000010_20261102_114907.pdf)。板子扛住了 1 线程和 2 线程,
# **死在 4 线程那一片起手约 1 秒**, 死时 cpub 57.1℃。
#
# ⚠️ **起始温度 / 过热阈值 / 起始供电轨是现场原文**; 下面那几行采样行是
#    **按现场时间线重排的**(片号按 1→2→3→4→8 的台阶填的), 不是逐字原文。
#    「进这一项时的板子状态」那一行是**新加的字段**(老版本包里没有),
#    这里按现场事实填: **核数 8**(这块 A527 有 8 核 —— 两个 cpufreq policy
#    (1416000/1800000) + 两个 cooling_device(cpu0/cpu4) 是证据)。
REAL_CPU_EVIDENCE = """# 加压现场记录 —— 板子 A10000000000010   采样间隔 0.2s
# 起始温度: cpub_thermal_zone=46.9℃ cpul_thermal_zone=46.1℃ ddr_thermal_zone=45.8℃
# 过热阈值(内核 thermal 驱动): cpub_thermal_zone: passive@70℃ passive@90℃ critical@110℃ | cpul_thermal_zone: passive@70℃ passive@90℃ critical@110℃
# 起始供电轨(mV): axp2202-aldo3=3300 axp2202-dcdc1=900 vdd_sys=900
# 进这一项时的板子状态: 核数 8 | 开机 3分45秒 | 负载均值(1/5/15分) 4.62/3.15/1.88 | 可用内存 2874MB
t=+0.1s threads=1 hot=47.4 cpub_thermal_zone=47.4 freq=1680MHz 起跑 第 1/12 片(1 线程, 缓启动)
t=+0.2s threads=1 hot=47.4 cpub_thermal_zone=47.4 load=66.7% freq=1800MHz dV=axp2202-dcdc1:900→1150
t=+4.9s threads=1 hot=49.6 cpub_thermal_zone=49.6 load=29.1% freq=1800MHz dV=axp2202-dcdc1:920→900
t=+5.5s threads=2 hot=50.5 cpub_thermal_zone=50.5 freq=1200MHz dV=axp2202-dcdc1:900→960 起跑 第 2/12 片(2 线程, 缓启动)
t=+10.9s threads=2 hot=55.4 cpub_thermal_zone=55.4 load=39.2% freq=1800MHz dV=axp2202-dcdc1:1150→1000
t=+10.9s threads=4 hot=54.9 cpub_thermal_zone=54.9 load=66.4% freq=1800MHz dV=axp2202-dcdc1:1000→1050 起跑 第 3/12 片(4 线程, 缓启动)
t=+11.0s threads=4 hot=53.8 cpub_thermal_zone=53.8 freq=1200MHz dV=axp2202-dcdc1:1050→900 起跑 第 3/12 片(4 线程, 缓启动)
t=+11.2s threads=4 hot=56.4 cpub_thermal_zone=56.4 freq=1344MHz
t=+11.5s threads=4 hot=58.4 cpub_thermal_zone=58.4 load=56.4% freq=1800MHz dV=axp2202-dcdc1:900→1150
t=+11.7s threads=4 hot=57.4 cpub_thermal_zone=57.4 load=63.0% freq=1800MHz dV=axp2202-dcdc1:1150→1000
t=+11.9s threads=4 hot=57.1 cpub_thermal_zone=57.1 load=63.5% freq=1800MHz dV=axp2202-dcdc1:1000→1150
"""

s = be.summarize_cpu_death(REAL_CPU_EVIDENCE)
check("第 3/12 片" in s, "说清了死在第几片", s)
check("4 线程" in s, "说清了死在几线程", s)
check("起手后 1.0 秒" in s, "说清了最后一份落盘记录在起手后多久", s)
check("57.1℃" in s, "带上了死前那一刻的温度", s)
check("1800MHz" in s, "带上了死前那一刻的频率", s)
check("axp2202-dcdc1:1000→1150" in s, "带上了死前那一刻的电压台阶", s)
check("天花板" in s, "点出了这块板子的天花板", s)
check("热不是原因" in s, "明说热不是原因(57.1℃ 离 110℃ 还远)", s)
check("110" in s, "把内核的 critical 阈值摆出来做对比", s)

# ── ⚠️ **死亡时刻只能「框」, 不能「钉」** (2026-11-04) ──
#
# 采样行**每 10 秒才 fsync 一次** ⇒ 两次 fsync 之间的采样点**跟着板子
# 一起没了**(硬复位把页缓存带走) ⇒ 盘上最后一行只说明「板子**那时还
# 活着**」, 真实死时刻 ∈ (t_last, t_last + 10s]。
#
# ⚠️ **原来这里写的是「板子死在 t=+11.9s」** —— 那是**把上界说成了
# 事实**: 2026-11-04 板子上实测(SN A10000000000010, 0.1.0-14 包),
# 最后一行 t=+0.2s, 真实死时刻**不可知** —— 产线拿这句话去判「死在
# 起手瞬间」还是「跑了半分钟才死」会被带偏。
check("死亡时刻只能框在" in s,
      "把死亡时刻说成「只能框」而不是钉死一个数", s)
check("起手后 1.0 秒 ~ 起手后 11.0 秒" in s,
      "把框的两端摆出来(最后落盘 1.0 秒, 上界 +10 秒)", s)
check("落在盘上" in s, "明说最后一行是「落在盘上」的(不是「死的那一刻」)", s)
check("板子死在 t=+11.9s" not in s,
      "不许把最后一行的时间说成死亡时刻", s)

# ── 连续加压(没有「第 N/M 片」标记)—— 就是 2026-11-04 那次实测 ──
#
# ⚠️ **连续模式下没有片号**, 走的是另一条分支 —— 那天板子就是死在这儿:
# 6 线程连续加压 60s, 盘上只剩两行(起跑那一行 + t=+0.2s 那一行)。
CONT_EVIDENCE = """# 加压现场记录 —— 板子 A10000000000010   采样间隔 2s
# 起始温度: cpub_thermal_zone=48.8℃ cpul_thermal_zone=49.0℃
# 本轮加压峰值: 6 线程(连续加压, 一个进程跑满整轮)
t=+0.0s threads=6 hot=48.8 cpub_thermal_zone=48.8 cpul_thermal_zone=49.0 freq=1800MHz 起跑 连续加压 60s(6 线程)
t=+0.2s threads=6 hot=50.2 cpub_thermal_zone=50.2 cpul_thermal_zone=49.4 freq=1800MHz
"""
sc = be.summarize_cpu_death(CONT_EVIDENCE)
check("板子死在 t=+0.2s" not in sc,
      "连续模式: 也不许把最后一行的时间说成死亡时刻", sc)
check("t=+0.2s ~ t=+10.2s" in sc,
      "连续模式: 把框的两端摆出来(0.2 秒 ~ 10.2 秒)", sc)
check("死亡时刻只能框在" in sc, "连续模式: 同样明说「只能框」", sc)
check("落在盘上" in sc, "连续模式: 同样明说最后一行是「落在盘上」的", sc)

# 跑完了(最后一行是「跑完」而不是「死」)⇒ **不给结论**
# —— 调用方(报告)只在「板子确实死在 CPU 这一项上」时才调它。
check(be.summarize_cpu_death("") == "", "没有记录 ⇒ 空串")
check(be.summarize_cpu_death("随便一段不是现场记录的文字") == "",
      "不是现场记录 ⇒ 空串(不硬编结论)")

# 热真的到 critical 时, 结论要**反过来**
HOT = REAL_CPU_EVIDENCE.replace("t=+11.9s threads=4 hot=57.1",
                                "t=+11.9s threads=4 hot=112.3")
s_hot = be.summarize_cpu_death(HOT)
check("热可能就是原因" in s_hot, "死时温度过了 critical ⇒ 结论反过来说", s_hot)

section("【12】硬复位 vs 软重启 —— 两种死法的**文案**必须不同")

check("硬拉死" in be.classify_reboot(HARD)[1],
      "硬复位: 说清了是硬拉死", be.classify_reboot(HARD)[1])
check("reboot -f" in be.classify_reboot(HARD)[1],
      "硬复位: 提醒还有 `reboot -f` 这种不经过 systemd 的强制重启",
      be.classify_reboot(HARD)[1])
check("被测设备不合格" in be.HARD_RESET_ITEM_ERROR,
      "硬复位的说法是「被测设备不合格」", be.HARD_RESET_ITEM_ERROR)
check("服务重启" in be.SOFT_ITEM_ERROR,
      "软重启的说法是「服务重启, 本项没跑完」", be.SOFT_ITEM_ERROR)
check("不代表被测设备不合格" in be.SOFT_RUN_ERROR,
      "软重启的**轮级**说法是「不代表被测设备不合格」", be.SOFT_RUN_ERROR)
check("被测设备不合格" in be.HARD_RESET_RUN_ERROR,
      "硬复位的**轮级**说法是「被测设备不合格」", be.HARD_RESET_RUN_ERROR)
check(be.HARD_RESET_ITEM_ERROR != be.SOFT_ITEM_ERROR,
      "两种文案**不是**同一句")
check(be.HARD_RESET_RUN_ERROR != be.SOFT_RUN_ERROR,
      "轮级两种文案也**不是**同一句")


section("【13】「死的时候我们的测试在不在上面跑」—— 不冤枉板子的那最后一道关")


# 实样本: 2026-11-02 新板 SN A10000000000010 的上一次启动日志尾巴
# (报告 A10000000000010_20261102_114907.pdf 的「板子为什么死」那一节)。
# 板子死在**我们的 CPU 加压**里 —— 日志尾巴上是 stress-ng 自己说的话。
REAL_JOURNAL_TAIL = (
    "[上一次启动的 journal]\n"
    "【上一次是硬复位】上一次启动的日志里**没有**关机序列 ⇒ 板子不是被 "
    "systemd 正常重启的, 是被**硬拉死**的\n"
    "Nov 02 11:47:55.544295 ubuntu2004 stress-ng[23114]: memory (MB): total "
    "3902.11, free 1800.33, shared 157.27\n"
    "Nov 02 11:47:55.512962 ubuntu2004 sudo[23108]:     User : TTY=unknown ; "
    "PWD=/usr/lib/aging-test ; USER=root ; COMMAND=/usr/bin/timeout -k 5.0 "
    "35.0 /usr/bin/stress-ng --cpu 2 --timeout 5.0s --metrics-brief\n"
    "Nov 02 11:47:50.236393 ubuntu2004 stress-ng[23078]: memory (MB): total "
    "3902.11, free 1758.06, shared 198.56\n")

# 反例: 板子是被硬拉死的, 但死的时候**我们的测试早就不在了** ——
# 尾巴上只有厂商那个看门狗在跑(外加服务每 5 分钟存一次时钟)。
# 这种要是也判「板子不合格」, 就是冤枉一块好板子。
IDLE_JOURNAL_TAIL = (
    "[上一次启动的 journal]\n"
    "【上一次是硬复位】上一次启动的日志里**没有**关机序列 ⇒ 板子不是被 "
    "systemd 正常重启的, 是被**硬拉死**的\n"
    "Oct 30 18:00:42.923095 ubuntu2004 sudo[275122]:     User : TTY=unknown ; "
    "PWD=/usr/lib/aging-test ; USER=root ; COMMAND=/sbin/fake-hwclock save\n"
    "Oct 30 18:01:01.029113 ubuntu2004 systemd[1]: Starting Anfuhua service "
    "watchdog check...\n"
    "Oct 30 18:01:01.260557 ubuntu2004 watchdog.sh[275235]: [anfuhua] "
    "watchdog check complete\n"
    "Oct 30 18:01:01.265417 ubuntu2004 systemd[1]: Finished Anfuhua service "
    "watchdog check.\n")

check(be.classify_evidence_text(REAL_JOURNAL_TAIL) == "硬复位",
      "从整理好的证据文本里读得出判定(不用重新扫一遍日志)",
      be.classify_evidence_text(REAL_JOURNAL_TAIL))
check(be.classify_evidence_text(IDLE_JOURNAL_TAIL) == "硬复位",
      "同样是硬复位, 判定读得出来")
check(be.classify_evidence_text("") == "看不出来",
      "没有证据 ⇒ 「看不出来」(调用方按老口径走)")

check(be.our_run_at_death(REAL_JOURNAL_TAIL) is True,
      "尾巴里有 stress-ng ⇒ 板子死的时候我们的测试正在上面跑")
check(be.our_run_at_death(IDLE_JOURNAL_TAIL) is False,
      "尾巴里只有看门狗和存时钟 ⇒ **不算**「测试在跑」"
      "(服务平时也在日志里留痕, 不能拿它当证据)")
check(be.our_run_at_death("") is False, "没有日志 ⇒ False")
check(be.our_run_at_death("stress-ng 出现在很前面\n" + "无关的行\n" * 1000) is False,
      "只看**尾巴** —— 出现在开头的痕迹不算(那是开机那会儿的事)")

# ⚠️ 尾巴里的 fake-hwclock 那行带着 `PWD=/usr/lib/aging-test` ——
#    要是把「程序目录名」当成证据, 就会把「服务还活着」误当成「测试在跑」。
check("aging-test" in IDLE_JOURNAL_TAIL,
      "(反例里确实有程序目录名 —— 正好证明不能拿它当证据)")


section("【14】**兜底**: 现场记录读不出来时, 从上一次启动的日志里读「死在几线程」")

# ⚠️ 2026-11-02 13:50 那次实测踩的坑: 板子死在 **2 线程**那一片, 而
# 「加压现场记录」那份文件**整个读不出来**(它是板子死的那一刻写的, 尾巴
# 随时只写了一半 ⇒ 严格 UTF-8 解码抛异常 ⇒ **整份证据全丢**)⇒ 报告里
# 「板子死在哪一级加压」那节**整个不见了** —— 恰恰是最该有证据的一次。
#
# ⇒ 兜底来源: **上一次启动的 journal 尾巴** —— 每一片的 sudo 命令行都在
#    里面(`COMMAND=... stress-ng --cpu N --timeout 5.0s`), 板子死后还在。
#    下一片的命令行**没出现**, 正说明板子没活到下一片。

# 实样本: 2026-11-02 13:50 那轮(新板 A10000000000010)的 journal 尾巴,
# **一个字没改**地从报告 PDF 的「板子为什么死」那一节抄下来的。
# 板子扛住了 1 线程, **死在 2 线程那一片**(13:50:40.227 起手, 之后再没有
# 下一片的命令行 ⇒ 板子没活到 3 线程那片)。
# ⚠️ sudo 的命令行太长, 在报告里被**折成两行** —— `--cpu N` 落在**没有
#    时间戳**的续行上(下面那几行 "AND=..." 就是)⇒ 解析时时间戳要**沿用
#    上一行**。这就是真实形态, 不是造出来的。
REAL_JOURNAL_2TH = """Nov 02 13:50:31.504271 ubuntu2004 sudo[44535]: pam_unix(sudo:session): session closed for user root
Nov 02 13:50:34.823541 ubuntu2004 sudo[45199]:     User : TTY=unknown ; PWD=/usr/lib/aging-test ; USER=root ; COMM
AND=/usr/bin/true
Nov 02 13:50:34.837817 ubuntu2004 sudo[45199]: pam_unix(sudo:session): session opened for user root by (uid=0)
Nov 02 13:50:34.849311 ubuntu2004 sudo[45199]: pam_unix(sudo:session): session closed for user root
Nov 02 13:50:34.990424 ubuntu2004 sudo[45208]:     User : TTY=unknown ; PWD=/usr/lib/aging-test ; USER=root ; COMM
AND=/usr/bin/timeout -k 5.0 35.0 /usr/bin/stress-ng --cpu 1 --timeout 5.0s --metrics-brief
Nov 02 13:50:35.005545 ubuntu2004 sudo[45208]: pam_unix(sudo:session): session opened for user root by (uid=0)
Nov 02 13:50:35.032646 ubuntu2004 stress-ng[45216]: invoked with '/usr/bin/stress-n' by user 0
Nov 02 13:50:40.133556 ubuntu2004 sudo[45208]: pam_unix(sudo:session): session closed for user root
Nov 02 13:50:40.227775 ubuntu2004 sudo[45246]:     User : TTY=unknown ; PWD=/usr/lib/aging-test ; USER=root ; COMM
AND=/usr/bin/timeout -k 5.0 35.0 /usr/bin/stress-ng --cpu 2 --timeout 5.0s --metrics-brief
Nov 02 13:50:40.239977 ubuntu2004 sudo[45246]: pam_unix(sudo:session): session opened for user root by (uid=0)
Nov 02 13:50:40.259515 ubuntu2004 stress-ng[45252]: invoked with '/usr/bin/stress-n' by user 0
"""

s2 = be.summarize_cpu_death_from_journal(REAL_JOURNAL_2TH)
check("2 线程" in s2, "说清了死在几线程那一片(2 线程)", s2)
check("1 线程" in s2, "说清了前面扛住的是 1 线程", s2)
check("天花板" in s2, "点出了天花板", s2)
check("没读出来" in s2, "明说这份结论没有温度/频率(现场记录没读出来)", s2)
# ⚠️ 死的那一片**不能**算进「扛住了」—— 自己扛住自己, 结论就自相矛盾
check("前面扛住了 1 线程" in s2, "扛住的那几级说得准确(只有 1 线程)", s2)
check("前面扛住了 1、2 线程" not in s2,
      "死在 2 线程那一片 ⇒ 2 线程**不算**「扛住了」", s2)

# 反过来: 上一轮(11:48)的尾巴 —— 死在 4 线程那片, 前面扛住 2 线程
REAL_JOURNAL_4TH = """Nov 02 11:47:50.236267 ubuntu2004 stress-ng[23078]: invoked with '/usr/bin/stress-n' by user 0
Nov 02 11:47:55.423857 ubuntu2004 sudo[23071]: pam_unix(sudo:session): session closed for user root
Nov 02 11:47:55.512962 ubuntu2004 sudo[23108]:     User : TTY=unknown ; PWD=/usr/lib/aging-test ; USER=root ; COMM
AND=/usr/bin/timeout -k 5.0 35.0 /usr/bin/stress-ng --cpu 2 --timeout 5.0s --metrics-brief
Nov 02 11:47:55.526197 ubuntu2004 sudo[23108]: pam_unix(sudo:session): session opened for user root by (uid=0)
Nov 02 11:47:55.544169 ubuntu2004 stress-ng[23114]: invoked with '/usr/bin/stress-n' by user 0
Nov 02 11:48:00.893133 ubuntu2004 sudo[23108]: pam_unix(sudo:session): session closed for user root
Nov 02 11:48:00.990939 ubuntu2004 sudo[23150]:     User : TTY=unknown ; PWD=/usr/lib/aging-test ; USER=root ; COMM
AND=/usr/bin/timeout -k 5.0 35.0 /usr/bin/stress-ng --cpu 4 --timeout 5.0s --metrics-brief
Nov 02 11:48:01.004897 ubuntu2004 sudo[23150]: pam_unix(sudo:session): session opened for user root by (uid=0)
Nov 02 11:48:01.023979 ubuntu2004 stress-ng[23156]: invoked with '/usr/bin/stress-n' by user 0
"""
s4 = be.summarize_cpu_death_from_journal(REAL_JOURNAL_4TH)
check("4 线程" in s4, "上一轮: 死在 4 线程那片", s4)

# ⚠️ **两轮之间不能串**: 日志尾巴里可能同时留着**上一轮**和这一轮的加压
#    (跨重启累积) ⇒ 中间隔了几十分钟的那条**不是**这一轮的事。
TWO_RUNS = REAL_JOURNAL_4TH + """
Nov 02 13:50:34.990424 ubuntu2004 sudo[45208]:     User : TTY=unknown ; PWD=/usr/lib/aging-test ; USER=root ; COMM
AND=/usr/bin/timeout -k 5.0 35.0 /usr/bin/stress-ng --cpu 2 --timeout 5.0s --metrics-brief
"""
s2r = be.summarize_cpu_death_from_journal(TWO_RUNS)
check("2 线程" in s2r, "两轮混在一起时, 取**最后**那一轮(这一轮)", s2r)
check("4 线程" not in s2r,
      "上一轮(隔了 2 小时的 4 线程)不跟着一起算", s2r)

# ⚠️ **长行被折断**的情况(报告里那一节是 <pre>, PDF 提取出来时 sudo 的
#    命令行会被折到下一行) ⇒ `--cpu N` 落在**没有时间戳**的续行上 ⇒
#    时间戳要**沿用上一行**。
WRAPPED = """Nov 02 13:50:40.227775 ubuntu2004 sudo[45246]:     User : TTY=unknown ; PWD=/usr/lib/aging-test ; USER=root ; COMM
AND=/usr/bin/timeout -k 5.0 35.0 /usr/bin/stress-ng --cpu 2 --timeout 5.0s --metrics-brief
"""
sw = be.summarize_cpu_death_from_journal(WRAPPED)
check("2 线程" in sw, "命令行被折行也读得出(--cpu N 在续行上)", sw)

# ⚠️ **跨零点**: 一轮测试跑过半夜(23:59:57 起手, 00:00:02 死在下一片)
#    ⇒ 时间戳要**带日期**才算得出间隔 ⇒ 只看时分秒的话, 间隔会变成
#    「负的」⇒ 误判成跨轮 ⇒ 前面扛住的那几级全丢。
MIDNIGHT = """Nov 02 23:59:57.100000 ubuntu2004 sudo[100]:     User : TTY=unknown ; PWD=/usr/lib/aging-test ; USER=root ; COMM
AND=/usr/bin/timeout -k 5.0 35.0 /usr/bin/stress-ng --cpu 1 --timeout 5.0s --metrics-brief
Nov 03 00:00:02.200000 ubuntu2004 sudo[101]:     User : TTY=unknown ; PWD=/usr/lib/aging-test ; USER=root ; COMM
AND=/usr/bin/timeout -k 5.0 35.0 /usr/bin/stress-ng --cpu 2 --timeout 5.0s --metrics-brief
"""
sm = be.summarize_cpu_death_from_journal(MIDNIGHT)
check("2 线程" in sm, "跨零点: 死在 2 线程那一片", sm)
check("1 线程" in sm, "跨零点: 前面扛住的 1 线程没被误判成跨轮丢掉", sm)

# 没有我们的加压记录 ⇒ **空串**(不硬编一句「板子死在第 N 片」)
check(be.summarize_cpu_death_from_journal("") == "", "空串 ⇒ 空结论")
check(be.summarize_cpu_death_from_journal(
    "Nov 02 13:50:30 systemd[1]: Finished Anfuhua service watchdog check.") == "",
      "尾巴里没有加压记录 ⇒ 空结论(不硬编)")
# 别的程序用了 stress-ng(不是我们的加压)—— 只看命令行认不出是我们 ⇒
# 宁可空着, 也不把别人的 stress-ng 当成我们的死因。
check(be.summarize_cpu_death_from_journal(
    "Nov 02 13:50:40 ubuntu2004 stress-ng[9]: invoked with 'stress-ng' by user 0") == "",
      "只有 stress-ng 的横幅、没有 --cpu N ⇒ 不下结论")


section("【15】**容错解码** —— 板子死的那一刻写的文件, 尾巴可能只写了一半")

# ⚠️ 2026-11-02 实测踩的坑: 现场记录是**边跑边写**的, 板子硬复位时最后
#    一行很可能只写了一半 —— 甚至把一个多字节字符(℃)劈成两半 ⇒ 严格
#    UTF-8 解码抛 UnicodeDecodeError ⇒ **整份证据全丢** ⇒ 报告里那一节
#    整个不见了。⇒ 必须容错解码: 尾巴缺一截就缺一截, 前面的行照样读得到。
trunc = be.evidence_dir() / "cpu_evidence_trunc.txt"
trunc.parent.mkdir(parents=True, exist_ok=True)
# 前面的行完好, 最后一行劈开了(℃ 只剩 2 字节)
trunc.write_bytes(
    ("t=+0.1s threads=1 hot=47.4 cpub_thermal_zone=47.4 freq=1680MHz "
     "起跑 第 1/12 片(1 线程, 缓启动)\n"
     "t=+11.9s threads=4 hot=57.1 cpub_thermal_zone=57.1 load=63.5% "
     "freq=1800MHz 死前 57.1℃").encode("utf-8")[:-1]
)
txt = trunc.read_bytes().decode("utf-8", errors="replace")
check("起跑 第 1/12 片" in txt,
      "尾巴被劈开的文件, 前面的行照样读得到", txt[:120])

# ⚠️ 断言要**兜住异常** —— 严格解码会**抛 UnicodeDecodeError**, 那会让
#    整个测试**崩掉**(而不是红), 崩掉的测试不算「咬得动」。
try:
    lenient = be._read_text_lenient(trunc)
    check("起跑 第 1/12 片" in lenient,
          "容错读文件的helper: 截断了也不抛异常, 前面的行照样读得到",
          lenient[:120])
except Exception as exc:                          # noqa: BLE001
    check(False,
          "容错读文件的helper: 截断了也不抛异常, 前面的行照样读得到",
          repr(exc))

# 走一遍真的读路径(read_cpu_evidence): 造一个半截文件 ⇒ 必须读得到内容
p = be.cpu_evidence_path(9999)
p.parent.mkdir(parents=True, exist_ok=True)
p.write_bytes(
    ("t=+0.1s threads=1 hot=47.4 freq=1680MHz 起跑 第 1/12 片\n"
     "t=+5.5s threads=2 hot=50.5 freq=1800MHz 死时 50.5℃").encode("utf-8")[:-1]
)
try:
    got = be.read_cpu_evidence(9999)
    check("起跑 第 1/12 片" in got,
          "read_cpu_evidence: 半截文件也能读出前面的行(不再整份丢弃)", got[:120])
except Exception as exc:                          # noqa: BLE001
    check(False,
          "read_cpu_evidence: 半截文件也能读出前面的行(不再整份丢弃)",
          repr(exc))
# 读不出来(文件不存在)⇒ 空串, 不抛
check(be.read_cpu_evidence(123456789) == "", "文件不存在 ⇒ 空串(不抛异常)")

# 「上一次启动的日志」那份**也一样**容错 —— 它是板子死的那一刻从 journal
# 里捞的, 尾巴同样可能是半行。⇒ **兜底来源**不能自己也掉链子。
ev = be.evidence_path(8888)
ev.parent.mkdir(parents=True, exist_ok=True)
ev.write_bytes(
    ("[上一次启动的 journal]\n【上一次是硬复位】上一次启动的日志里没有关机序列\n"
     "Nov 02 13:50:40.259515 ubuntu2004 stress-ng[45252]: 死时温度 57.1℃")
    .encode("utf-8")[:-1]
)
try:
    got_ev = be.read_evidence(8888)
    check("【上一次是硬复位】" in got_ev,
          "read_evidence: 半截文件也能读出前面的行(兜底来源自己也掉链子就白搭)",
          got_ev[:120])
except Exception as exc:                          # noqa: BLE001
    check(False,
          "read_evidence: 半截文件也能读出前面的行(兜底来源自己也掉链子就白搭)",
          repr(exc))


section("【16】**起跑温度** —— 板子**进这一项**的时候就已经多热, 也要说出来")


# ⚠️⚠️ 2026-11-02 现场实测逼出来的一条: 板子 A10000000000010 **单独**跑
#    CPU 加压(1→2→3→4, 满核 60 秒)**扛住了**, 而**整轮老化**跑下来死在
#    CPU 项 ⇒ 「CPU 负载本身」不是触发条件 ⇒ 触发条件是**进这一项时板子
#    已经是什么状态** ⇒ **起始温度就是那个状态**。
#    而这一项的指标是**跑完**才落库的 ⇒ 死在半路时 `start_temp_c` **跟着
#    没了** ⇒ **只有文件头里那一份还在** ⇒ 结论里必须把它说出来。

s16 = be.summarize_cpu_death(REAL_CPU_EVIDENCE)
check("起跑温度" in s16,
      "结论里带上了**起跑温度**(进这一项时板子多热)", s16)
check("46.9" in s16,
      "起跑温度就是文件头里那个数(46.9℃)", s16)
check("cpub_thermal_zone" in s16,
      "点名是哪个核区(全板最热 = 大核 cpub)", s16)
check("全板最热" in s16,
      "明说这个数是**全板最热**(跟每行的 `hot=` 同一个口径)", s16)
check("ddr" not in s16.split("起跑温度")[1][:80],
      "**没 hotter 的核区就不该被点名**(ddr 45.8℃ 低于 cpub 46.9℃)", s16)

# 裕量本来就薄 ⇒ 要**明说**
HOT_START = REAL_CPU_EVIDENCE.replace(
    "# 起始温度: cpub_thermal_zone=46.9℃ cpul_thermal_zone=46.1℃",
    "# 起始温度: cpub_thermal_zone=92.4℃ cpul_thermal_zone=88.1℃")
s16b = be.summarize_cpu_death(HOT_START)
check("92.4" in s16b, "起跑温度换成高的那个, 结论里跟着换", s16b)
check("裕量本来就薄" in s16b,
      "起跑时离 critical 只剩一点点 ⇒ **明说裕量本来就薄**", s16b)
check("热不是原因" in s16b,
      "「热不是原因」那句**照旧**(死时没到 critical 就是没到)", s16b)

# 没有文件头(老版本 / 文件被截)⇒ **不许编**起跑温度, 但别的东西照给
NO_HEAD = "\n".join(l for l in REAL_CPU_EVIDENCE.splitlines()
                     if not l.startswith("# 起始温度"))
s16c = be.summarize_cpu_death(NO_HEAD)
check("起跑温度" not in s16c,
      "文件头里没有起始温度 ⇒ **一句都不编**", s16c)
check("第 3/12 片" in s16c,
      "但别的结论照给(不能因为少一个数就整段不写)", s16c)

# ⚠️ **口径 = 全板最热核区**(2026-11-02 改): 每一行的 `hot=` 就是
#    `max(所有核区)` ⇒ 「起跑温度」也必须是全板最热, 两个数才能相减。
#    **而且不能只看 CPU 核区** —— 前面那项(GPU 渲染 105 秒)烤热的
#    可能是 GPU 核区, 只看 CPU 就把它**藏起来**了。
MIXED = REAL_CPU_EVIDENCE.replace(
    "# 起始温度: cpub_thermal_zone=46.9℃ cpul_thermal_zone=46.1℃ ddr_thermal_zone=45.8℃",
    "# 起始温度: cpub_thermal_zone=46.9℃ cpul_thermal_zone=49.1℃ ddr_thermal_zone=70.8℃")
name16, val16, cname16, cval16 = be._parse_start_temp(MIXED)
check(val16 == 70.8 and name16 == "ddr_thermal_zone",
      "取**全板最热核区**(ddr 70.8℃), 不是最热的 CPU 核区(cpul 49.1℃)",
      f"{name16}={val16}")
check(cval16 == 49.1 and cname16 == "cpul_thermal_zone",
      "**另外**给出最热 CPU 核区(读者要跟 `_read_cpu_temp()` 对比时用)",
      f"{cname16}={cval16}")
s16d = be.summarize_cpu_death(MIXED)
check("ddr_thermal_zone" in s16d,
      "结论点名**全板最热**的那个核区(不是 CPU 核区顶替)", s16d)
check("CPU 核区当时 49.1℃" in s16d,
      "同时把 CPU 核区当时的温度点出来(别把它藏了)", s16d)

# 一个 CPU 核区都认不出 ⇒ CPU 那一栏就是空, 但全板最热照样给
name16b, val16b, cname16b, cval16b = be._parse_start_temp(
    "# 起始温度: ddr_thermal_zone=70.8℃ gpu_thermal_zone=65.0℃")
check(val16b == 70.8 and name16b == "ddr_thermal_zone",
      "认不出 CPU 核区 ⇒ 全板最热照样给", f"{name16b}={val16b}")
check(cval16b is None and cname16b is None,
      "认不出 CPU 核区 ⇒ CPU 那一栏是空的(**不拿别的核区顶替**)",
      f"{cname16b}={cval16b}")

# 读不出来 ⇒ 四个都是 None
check(be._parse_start_temp("") == (None, None, None, None), "空文本 ⇒ 没有起跑温度")
check(be._parse_start_temp("# 起始温度: 乱七八糟") == (None, None, None, None),
      "头里没有可解析的数 ⇒ 没有起跑温度")


section("【17】**进这一项时板子是什么状态** —— 除了热, 还有别的维度")

# ⚠️ 2026-11-02 现场实测逼出来的: 板子 A10000000000010 **单独**跑 CPU
#    加压**扛住了**, 整轮老化却**每次**死在 CPU 项 ⇒ 触发条件是
#    「**进这一项时板子已经是什么状态**」。
#    而现场拿到的起跑温度是**凉的**(46.9℃)⇒ **「热」这条线出局** ⇒
#    光看温度不够, 还得看**已经连续压了多久 / 系统里堆着多少负载**。
s17 = be.summarize_cpu_death(REAL_CPU_EVIDENCE)
check("进这一项时板子的状态" in s17,
      "结论里带上了**进这一项时板子的状态**那一节", s17)
check("8 核" in s17,
      "点了**核数**(加压台阶封顶在几线程全看它)", s17)
check("3 分 45 秒" in s17,
      "点了**开机时长**(板子已经连续跑了多久)", s17)
check("4.62" in s17 or "4.6" in s17,
      "点了**负载均值**(累积压力的直接度量)", s17)
check("2874" in s17,
      "点了**可用内存**(前面的项留了多少占用)", s17)

# 老版本包里没有这一行 ⇒ **不许编**, 但别的结论照给
NO_STATE = "\n".join(l for l in REAL_CPU_EVIDENCE.splitlines()
                      if not l.startswith("# 进这一项时的板子状态"))
s17b = be.summarize_cpu_death(NO_STATE)
check("进这一项时板子的状态" not in s17b,
      "文件头里没有板子状态 ⇒ **一句都不编**", s17b)
check("第 3/12 片" in s17b,
      "但别的结论照给", s17b)

# 只有一半 ⇒ 给读到的那一半
HALF_STATE = REAL_CPU_EVIDENCE.replace(
    "# 进这一项时的板子状态: 核数 8 | 开机 3分45秒 | "
    "负载均值(1/5/15分) 4.62/3.15/1.88 | 可用内存 2874MB",
    "# 进这一项时的板子状态: 核数 8")
s17c = be.summarize_cpu_death(HALF_STATE)
check("8 核" in s17c, "只有核数 ⇒ 给核数", s17c)
check("可用内存" not in s17c, "没读到的那几项**一个字都不提**", s17c)

st = be._parse_board_state(REAL_CPU_EVIDENCE)
check(st.get("cores") == 8, "解析出核数", st)
check(st.get("uptime_sec") == 225, "解析出开机时长(3 分 45 秒 = 225 秒)", st)
check(st.get("load") == [4.62, 3.15, 1.88], "解析出 1/5/15 分钟负载均值", st)
check(st.get("mem_avail_mb") == 2874, "解析出可用内存", st)
check(be._parse_board_state("") == {}, "空文本 ⇒ 没有板子状态")
check(be._parse_board_state("# 进这一项时的板子状态: 乱七八糟") == {},
      "读不出来 ⇒ 空 dict(**不编**)")


section("【5】要不要去读系统日志 —— 默认开着, 现场一行能关")

set_cfg({"report": {}})
check(be.capture_enabled() is True,
      "默认**开** —— 板子上一次崩了, 死因自己留下来(现场点名授权过)")
set_cfg({"report": {"capture_boot_evidence": False}})
check(be.capture_enabled() is False, "现场写 false ⇒ 关掉")
set_cfg({"report": {"capture_boot_evidence": True}})
check(be.capture_enabled() is True, "现场写 true ⇒ 开")

# 还原配置
Config._data = _SAVED_CFG


print("\n" + ("全部通过" if not FAILS else f"有失败项 ({len(FAILS)})"))
for f in FAILS:
    print("  ✗ " + f)
sys.exit(0 if not FAILS else 1)
