"""板子重启之后, **死因只留在「上一次启动」的内核日志里**。

**为什么要有这个模块**
    2026-10-29 新板那轮: 板子跑到整轮最后一项时**当场重启** —— 报告上只
    剩一句「服务重启, 这一轮中途夭折(没跑完)」, **看不出板子为什么死**。
    而死因(kernel panic / thermal critical trip / watchdog reset)在重启
    之后只会留在两个地方:

      · systemd-journald 的**上一次启动** —— `journalctl -b -1`;
        前提是 `/var/log/journal` 是**持久化**的(不是每次重启清空的
        volatile 目录)。
      · rsyslog 的 `/var/log/kern.log` / `/var/log/syslog`。

    所以: **服务启动、发现有夭折的轮次**时去捞一把, 存成一份**旁挂文件**
    (`logs/boot_evidence_run<N>.txt`), 导出报告时带上 ⇒ **下一次再 reboot,
    报告自己就带死因**, 不用人再跑到板子前面翻日志。

**为什么不写进历史库**
    改历史记录(那一轮 `runs` 行的 `error` 字段)需要单独授权 —— 所以这里
    **一个字都不往历史库里写**: 证据落在 `logs/` 下的独立文件里, 报告
    导出时按 run_id 读回来。历史库该长什么样还长什么样。

**捞不到就什么都不写** —— 没有证据时编一段「疑似原因」比没有更糟:
    报告上写「疑似过热」而实际是驱动 panic, 会把排查方向带偏。
"""
from pathlib import Path
import os
import re
from typing import List, Optional, Tuple

from .config import Config
from .tests._tools import find_tool, run_cmd, with_sudo

# ── 「板子被硬拉死」时该写的话 ────────────────────────────────────────
#
# ⚠️ **两种死法的结论完全相反, 别混**:
#   · **软重启**(上一次启动的日志里有完整的关机序列) ⇒ 是有人/有服务执行了
#     reboot, 板子**没崩** ⇒ 这一项只是「没跑完」, **不代表被测设备不合格**
#     —— 工具没毛病, 板子也没崩, 下一轮重测就是了。
#   · **硬复位**(日志里**没有**关机序列) ⇒ 板子被**硬拉死**了 ⇒
#     **被测设备不合格** —— 它在加压中被拉死了。
#
# ⚠️⚠️ **2026-11-02 之前只有前一种文案** —— 板子死在 CPU 加压里(硬复位),
# 报告一律写「服务重启, 这一轮中途夭折(没跑完) —— 不代表被测设备不合格」,
# **方向是反的**: 板子扛不住加压, 那就是**板子不合格**, 写成「不代表不合格」
# 等于**放行一块会死的板子**。⇒ 硬复位必须单独一种说法。
HARD_RESET_ITEM_ERROR = (
    "板子在这一项上硬复位(掉电 / PMIC 复位 / 看门狗复位) —— 被测设备不合格")
HARD_RESET_RUN_ERROR = (
    "板子在这一轮中途硬复位(掉电 / PMIC 复位 / 看门狗复位) —— 被测设备不合格")
SOFT_ITEM_ERROR = "服务重启, 本项没跑完"
SOFT_RUN_ERROR = "服务重启, 这一轮中途夭折(没跑完) —— 不代表被测设备不合格"

# 候选来源, 按「最有信息量」排前面。{n} 会被换成要取的行数。
#
# ⚠️ 只有 journalctl 那一组是**上一次启动**的 —— journalctl 按 boot id
# 取, 重启之后老的那次还在。后面那组是**跨重启累积**的文件, 语义完全不同
# (见 _try_cumulative_log)。
_CANDIDATES = (
    (["journalctl", "-b", "-1", "-n", "{n}", "--no-pager", "-o", "short-precise"],
     "上一次启动的 journal"),
    (["journalctl", "-k", "-b", "-1", "-n", "{n}", "--no-pager", "-o", "short-precise"],
     "上一次启动的内核日志"),
    # ⚠️ 这两个是**跨重启累积**的文件 —— 尾巴已经是**本次开机**的内容了
    # (见 _pick_suspect_lines)。窗口开大些是为了能跨过重启边界。
    (["tail", "-n", "3000", "/var/log/kern.log"], "/var/log/kern.log(跨重启累积)"),
    (["tail", "-n", "3000", "/var/log/syslog"], "/var/log/syslog(跨重启累积)"),
)

# journalctl 在「没有上一次启动」/「日志不持久」/「权限不够」时会给这几
# 句话 —— 它们**不是死因**, 不能当成证据贴进报告(贴上去就是把「读不到」
# 说成了「板子是这样死的」)。实测这台机器上不提权读 journal 就会得到:
#   "Failed to search journal ACL: Operation not supported"
#   "No journal files were opened due to insufficient permissions."
_EMPTY_MARKERS = (
    "-- No entries --",
    "No journal files were found",
    "No journal files were opened",
    "Failed to open journal",
    "Failed to search journal ACL",
    "insufficient permissions",
    "no entries",
)

# 累积日志(跨重启)的兜底: 从里面**挑可疑行**。
#
# ⚠️ 为什么不能直接 tail 那个文件的尾部: rsyslog 的 kern.log / syslog 是
# **跨重启累积**的 —— 重启之后新一次开机的消息接在后面, 文件尾部已经是
# **本次开机**的内容, 死因在开机 banner **上面**几行。直接 tail 拿到的
# 是开机 banner, 看着像证据, 其实是**误导**。
#
# 所以改成: 取一个够大的窗口(几千行, 跨重启边界), 在 Python 里按关键字
# 挑出可疑行。挑不出来就**老实说挑不出来**, 别拿开机 banner 充数。
_SUSPECT_RE = re.compile(
    r"panic|thermal|trip point|critical temperature|watchdog|oops|BUG:|"
    r"Unable to handle|Unable to mount|read-only filesystem|"
    r"reboot|shutdown|reset|over[- ]?temperature|throttl",
    re.I,
)

# 累积日志兜底: 取多少行窗口 / 最多给多少行可疑行 / 最多给多少行上下文
_CUMULATIVE_WINDOW = 3000
_CUMULATIVE_MAX_HITS = 40
_CUMULATIVE_MAX_TAIL = 60

# 行首的 syslog 时间戳(`Oct 30 09:46:30`)或内核单调时钟(`[  990.111111]`)
_TS_RE = re.compile(r"^([A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})"
                    r"|^\[\s*\d+\.\d+\]")

# ── 「上一次是软重启还是硬复位」 ──────────────────────────────────────
#
# 有序关机会在日志末尾留下一串**特征行**(systemd 的关机序列):
#     Reached target Shutdown. / Reached target Final Step.
#     systemd-reboot.service: Succeeded. / Finished Reboot.
#     Shutting down. / Syncing filesystems and block devices.
#     Sending SIGTERM to remaining processes... / Journal stopped
# (实样本: logs/boot_evidence_run14.txt —— 2026-10-30 老板子 09:46 那次
#  人工 reboot, 序列一行不缺)
#
# **没有**这串行 ⇒ 板子是被**硬拉死**的(掉电 / PMIC 复位 / 看门狗复位)——
# 那才是要查死因的情形。反过来, 有这串行 ⇒ 是**软件重启**(有东西执行了
# reboot), 板子本身没崩 —— 报告里写「板子死了」就是误导。
_ORDERLY_SHUTDOWN_RE = re.compile(
    r"Reached target (Shutdown|Final Step|Reboot)|Finished Reboot|"
    r"systemd-reboot\.service: Succeeded|Shutting down\.|"
    r"Syncing filesystems and block devices|"
    r"Sending SIGTERM to remaining processes|Journal stopped|"
    r"Powering off|Rebooting",
    re.I,
)

# 真死因的关键字 —— 跟「崩溃/过热/掉电」直接相关
_CRITICAL_RE = re.compile(
    r"panic|oops|BUG:|Unable to handle|critical temperature|trip point|"
    r"watchdog|thermal|under[- ]?voltage|brownout|segfault|Call Trace|"
    r"reboot: Restarting system|power down",
    re.I,
)

# ── 噪音行 ──────────────────────────────────────────────────────────
#
# 厂商那个 `brcm_patchram_plus.service` 每 8.25 s 失败一次, 每失败一次就
# 切一次 WLAN/BT 的 PMIC 电源轨(sunxi-rfkill) —— 日志里几百行全是它。
# 真正的死因(HDMI 最后一条 / panic / 过热)就**埋在里面**。
# ⇒ 捞上来之后先把这些行剔掉, **再**截尾巴 —— 否则截出来的一截全是噪音。
_NOISE_RE = re.compile(
    r"brcm_patchram|patchram_plus|sunxi-rfkill|bt_init|S36bt_init|"
    r"bluetooth.*(?:service|daemon)",
    re.I,
)

# 连续多少行以内的 BT 日志**原样保留**(短的那几行不算噪音);
# 超过这个数就收成一行, 但**末尾这几句原样留着** ——
# 板子死之前最后一次电源轨切换就在那儿, 那是最关键的一票证据。
_NOISE_KEEP_INLINE = 2
_NOISE_KEEP_TAIL = 4


def _strip_noise(text: str) -> str:
    """把 BT 相关的噪音行剔掉。

    ⚠️ 噪音行里如果**同时**出现真死因的关键字(panic/过热/看门狗…), 那它
    不是纯噪音 —— 留着。宁可多留几行, 也不能把真正的死因滤掉。
    """
    kept: List[str] = []
    for line in text.splitlines():
        if _NOISE_RE.search(line) and not _CRITICAL_RE.search(line):
            continue
        kept.append(line)
    if not kept:
        return text          # 全是噪音 ⇒ 原样返回, 别把自己滤空了
    return "\n".join(kept)


def _collapse_bt_noise(text: str) -> str:
    """把**连续**的 BT 相关日志收成一行。

    厂商那个 `brcm_patchram_plus` 每 8.25 s 失败一次, 每失败一次就切一次
    WLAN/BT 的电源轨 —— 一次开机几百行, 把真正的死因(HDMI 最后一条 /
    panic / 过热)整个淹掉。收成一行, 但**末尾那几行原样留着** ——
    板子死之前最后一次电源轨切换就在那儿, 那是最关键的一票证据。
    """
    out: List[str] = []
    run: List[str] = []

    def _flush() -> None:
        if not run:
            return
        if len(run) <= _NOISE_KEEP_INLINE:
            out.extend(run)
        else:
            out.append(f"[… BT 相关日志 {len(run)} 条(重复的失败/电源轨切换) …]")
            out.extend(run[-_NOISE_KEEP_TAIL:])
        run.clear()

    for line in text.splitlines():
        if _NOISE_RE.search(line) and not _CRITICAL_RE.search(line):
            run.append(line)
        else:
            _flush()
            out.append(line)
    _flush()
    return "\n".join(out)


def classify_reboot(text: str) -> Tuple[str, str]:
    """上一次是**软重启**还是**硬复位**。

    返回 (判定, 说明); 判定 ∈ {"软重启", "硬复位", "看不出来"}。

    为什么要在意这个 —— 「板子为什么重启」有两个完全不同的答案:
      · 软重启: 有人/有服务执行了 reboot, 板子**没崩** ⇒ 去查是谁重启的
      · 硬复位: 板子被**硬拉死**(掉电 / PMIC 复位 / 看门狗复位)
                 ⇒ 去查死因(过热? 供电? 看门狗?)
    把这两者混为一谈, 排查方向会整个跑偏。
    """
    if not text:
        return "看不出来", "没有日志"
    hits = [ln for ln in text.splitlines()
            if _ORDERLY_SHUTDOWN_RE.search(ln)]
    if hits:
        return ("软重启",
                "上一次启动的日志里有**完整的关机序列** ⇒ 板子是**被软件重启**的"
                "(有人/有服务执行了 reboot), 板子本身没崩:\n    "
                + "\n    ".join(hits[:6]))
    # 没有关机序列 ⇒ 板子**不是**被 systemd 正常重启的, 是被硬拉死的
    extra = ""
    crit = [ln for ln in text.splitlines() if _CRITICAL_RE.search(ln)]
    if crit:
        extra = f"\n    最后一条相关记录: {crit[-1][:160]}"
    return ("硬复位",
            "上一次启动的日志里**没有**关机序列 ⇒ 板子不是被 systemd 正常重启的, "
            "是被**硬拉死**的(掉电 / PMIC 复位 / 看门狗复位 / `reboot -f` 这类"
            "不经过 systemd 的强制重启)" + extra)


#: 「**我们自己的测试正在这块板子上跑**」的痕迹 —— 上一段日志的**尾巴**里
#: 出现这些, 才说明板子死的那一刻, 我们正在它上面加压。
#:
#: 为什么要有这一关: 「上一次启动的日志里没有关机序列」只能说明**板子**是
#: 被硬拉死的, 说明不了**死的时候我们的测试正在上面跑**。两种情况都会
#: 得到「没有关机序列」:
#:   · 板子死在我们的加压里(真·被测设备不合格) —— 尾巴里有我们的痕迹;
#:   · **服务自己死了**(事件循环冻死 / 被 kill), 板子**后来**才被硬拉死 ——
#:     那时候我们的测试早就不在了 —— 尾巴里没有我们的痕迹。
#: 第二种要是也判「板子不合格」, 就是**冤枉板子**(好板子被误判出局)。
#:
#: 认的是**测试自己拉起来的进程/工具**(它们往 journal 里说话, 只有测试
#: 在跑的时候才说) —— **不是**我们程序目录的名字: 那个名字在服务**平时**
#: (每 5 分钟存一次时钟、每分钟被厂商看门狗查一次)也在日志里, 拿它当证据
#: 等于「服务还活着」也算「测试在跑」⇒ 板子平时被硬拉死也会赖到测试头上。
#: 实测这台机器上, `sudo … PWD=/usr/lib/aging-test` 每 5 分钟就出现一次。
_OUR_RUN_RE = re.compile(
    r"stress-ng|\bfio\b|ffmpeg|chromium|ethtool|smartctl|lsusb|"
    r"wpa_supplicant|\[server\]|\[runner\]")


def our_run_at_death(text: str, tail_chars: int = 3000) -> bool:
    """上一段日志的**尾巴**里有没有「我们自己的测试正在跑」的痕迹。

    只看**尾巴**: 上一段日志的开头(开机 banner)里也有我们的痕迹(服务每次
    开机都会启动), 那说明不了「死的时候我们在跑」—— 只有**末尾**那几行
    才跟死亡时刻挨着。

    返回 True = 板子死的时候我们的测试正在它上面跑 ⇒ 那一轮是被板子
    的硬复位打死的 ⇒ **被测设备不合格**。
    """
    tail = (text or "")[-tail_chars:]
    return bool(_OUR_RUN_RE.search(tail))


_EVID_KIND_RE = re.compile(r"【上一次是(软重启|硬复位|看不出来)】")


def classify_evidence_text(text: str) -> str:
    """从**已经整理好的证据文本**里读出软/硬判定。

    为什么不在这里重新扫一遍日志: `previous_boot_evidence` 的头部**已经**
    写好了判定(「【上一次是硬复位】」)—— 重新扫一遍会扫到**再上一次**重启
    留下的 "Rebooting" 之类的字样, 把判定整个翻过来(而窗口越大越容易翻)。

    返回 "软重启" / "硬复位" / "看不出来"。
    """
    m = _EVID_KIND_RE.search(text or "")
    return m.group(1) if m else "看不出来"


def _pick_suspect_lines(text: str, label: str) -> str:
    """从**跨重启累积**的日志里挑出可疑行。

    ⚠️ 这个文件在重启之后接上了新一次开机的消息 —— **文件尾巴是开机
    banner, 不是死因**。直接贴出去 = 把开机 banner 当成「板子为什么死」,
    读者会照着开机 banner 去查, 查半天什么也没有。

    所以这里按关键字挑可疑行; **一条都挑不出来就返回空串** —— 宁可没有,
    也不要假的(「挑不出来」本身是个信息: 那次重启可能连内核日志都没留下,
    即直接掉电 / 硬复位)。

    另外标一句**这些行有多旧** —— 最后一条可疑行离文件末尾还远, 说明
    中间隔着一整轮新开机 ⇒ 那多半是老账, 不是最近这次的重启留下的。
    """
    lines = text.splitlines()
    hit_idx = [i for i, ln in enumerate(lines) if _SUSPECT_RE.search(ln)]
    if not hit_idx:
        return ""
    hits = [lines[i] for i in hit_idx]
    shown = hits[-_CUMULATIVE_MAX_HITS:]
    head = (f"[{label} — 跨重启累积的文件, 下面是从最近 {len(lines)} 行里挑出的"
            f"可疑行(共 {len(hits)} 条, 给出最后 {len(shown)} 条)]")
    gap = len(lines) - 1 - hit_idx[-1]
    note = ""
    if gap > 20:
        note = (f"\n⚠ 最后一条可疑行距文件末尾还有 {gap} 行"
                f"(可疑行时间 {_line_ts(hits[-1]) or '?'}, 文件最新一行 "
                f"{_line_ts(lines[-1]) or '?'}) ⇒ 很可能**不是**最近这次重启"
                f"留下的, 是老账")
    return head + note + "\n" + "\n".join(shown)


def _line_ts(line: str) -> str:
    """取一行开头的 syslog 时间戳(`Oct 30 09:46:30`), 取不到就空串。"""
    m = _TS_RE.match(line.strip())
    return m.group(1) if m else ""


# ── 「板子死在第几级加压」: 从**上一次启动的日志**里读出来 ──────────────
#
# 为什么要有**第二份来源**: 「加压现场记录」(温度/负载/频率)是**边跑边落
# 文件**的, 本该最全 —— 但它是**板子死的那一刻**写的, 尾巴随时可能只写了
# 一半, 极端情况下整份都读不出来(2026-11-02 实测: 新板死在 2 线程那片,
# 报告里那一节整个不见了)。
#
# ⇒ 兜底来源: **上一次启动的 journal 尾巴** —— 每一片的 `sudo` 命令行都在
# 里面(`COMMAND=/usr/bin/timeout ... stress-ng --cpu N --timeout 5.0s`),
# **板子死在哪一片, 最后一条 `--cpu N` 就是哪一片** —— 下一片的命令行
# 没出现, 正说明板子没活到下一片。
#
# 2026-11-02 实样本(新板 A10000000000010, 13:50 那次):
#     sudo ... COMMAND=... stress-ng --cpu 1 --timeout 5.0s   @13:50:35.005
#     sudo ... COMMAND=... stress-ng --cpu 2 --timeout 5.0s   @13:50:40.227
#     ← 日志到此为止 ⇒ 板子死在**第 2 片(2 线程)**, 前面只扛住了 1 线程。
# (上一轮 11:48 那次同样的读法 ⇒ 死在**第 3 片(4 线程)**, 前面扛住 1、2。)
_RAMP_CMD_RE = re.compile(r"stress-ng\s+--cpu\s+(?P<th>\d+)")
#: 同一轮里相邻两片的间隔 —— 正常是 5 秒(一片长); 超过这个数就是**跨轮**了
#: (上一次跑测的残留), 不能算进本轮的台阶。
_RAMP_MAX_GAP_SEC = 30.0
#: journal 尾巴最多往前找多少片(片数再少, 结论也不受影响)
_RAMP_MAX_SLICES = 12


def _ts_seconds(ts: str) -> Optional[float]:
    """`Oct 30 09:46:30` → 当天的第几秒(取不到就 None)。"""
    m = re.match(r"([A-Z][a-z]{2})\s+(\d{1,2})\s+(\d{2}):(\d{2}):(\d{2})", ts or "")
    if not m:
        return None
    _, day, hh, mm, ss = m.groups()
    return int(day) * 86400 + int(hh) * 3600 + int(mm) * 60 + int(ss)


def summarize_cpu_death_from_journal(text: str) -> str:
    """从**上一次启动的 journal 尾巴**里读出「板子死在第几级加压」。

    这是「加压现场记录」的**兜底来源** —— 现场记录可能整个读不出来,
    但 journal 尾巴里的 `stress-ng --cpu N` 命令行**一定在**(那是 sudo
    记的, 板子死后还在)。

    返回空串 = 尾巴里没有我们的加压记录(不该硬套一句「板子死在第 N 片」)。
    """
    if not text:
        return ""
    hits = []       # (时间戳秒, 线程数, 原文行)
    last_ts = None  # 上一行的秒数 —— 长行被折断时(`--cpu N` 落在续行上)
    for line in text.splitlines():
        ts = _ts_seconds(_line_ts(line))
        if ts is not None:
            last_ts = ts           # **每一行**有时间戳都要记(不管它匹配不匹配)
        if "stress-ng" not in line:
            continue
        m = _RAMP_CMD_RE.search(line)
        if not m:
            continue
        hits.append((ts if ts is not None else last_ts,
                     int(m.group("th")), line.strip()))
    if not hits:
        return ""
    # **按时间排**: journalctl 默认是**由旧到新**, 但报告里那一节可能是
    # **由新到旧**排的(渲染时翻转过) —— 两种都得读对 ⇒ 一律按时间戳排。
    hits.sort(key=lambda h: (h[0] is None, h[0] if h[0] is not None else 0))

    # 只要**最后那一轮**的台阶: 从最后一条往回链, 相邻两片的间隔超过
    # 30 秒就断(那是上一轮跑测的残留, 不是本轮的台阶)。
    chain = [hits[-1]]
    for h in reversed(hits[:-1]):
        prev_ts = chain[-1][0]
        if prev_ts is None or h[0] is None:
            break
        gap = prev_ts - h[0]
        if gap < 0 or gap > _RAMP_MAX_GAP_SEC:
            break
        chain.append(h)
        if len(chain) >= _RAMP_MAX_SLICES:
            break
    chain.reverse()

    threads = [h[1] for h in chain]
    death_threads = threads[-1]
    survived = sorted(set(threads[:-1]))
    out = [f"板子死在**{death_threads} 线程**那一片 —— "
           f"上一片起手之后, 板子**没再起下一片**(下一片的命令行没出现) "
           f"⇒ 这一片没跑完。"]
    if survived:
        out.append("前面扛住了 " + "、".join(str(t) for t in survived)
                   + f" 线程 ⇒ 这一轮里, **{death_threads} 线程是这块板子的"
                     f"天花板**。")
    out.append("⚠️ 这一条是从**上一次启动的日志**里读出来的(sudo 命令行里的 "
               "`--cpu N`), 没有温度/频率 —— 那些在「加压现场记录」里, "
               "那份文件这次没读出来。")
    return "\n".join(out)


def evidence_dir() -> Path:
    """证据文件放哪儿 —— 数据目录下的 `logs/`(和事务日志同处)。"""
    return Config.data_root() / "logs"


def cpu_evidence_path(run_id: int) -> Path:
    """某一轮的「CPU 加压现场记录」文件。

    **跟 boot_evidence 同理, 也落文件不落库** —— 但它记的不是内核日志,
    是**加压过程中我们自己采到的温度/负载**: 板子死在加压半路时, 这一项
    的指标还没落库(指标是**跑完**才写的), 跟着进程一起没了 ⇒ 那时候
    这份边跑边写的记录就是唯一的证据(2026-10-30 新板实测)。
    """
    return evidence_dir() / f"cpu_evidence_run{run_id}.txt"


def _read_text_lenient(path: Path) -> str:
    """读一个**可能被硬复位截断**的文件 —— 容错解码, 不因为尾巴缺一截就整份丢。

    为什么必须容错: 「加压现场记录」是**板子死的那一刻**写的 —— 进程被硬拉死
    时, 最后一行很可能只写了一半, 甚至把一个多字节字符(℃)劈成两半 ⇒ 严格
    UTF-8 解码会抛 UnicodeDecodeError ⇒ **整份证据全丢**。
    ⚠️ 2026-11-02 实测踩过这个坑: 新板死在 **2 线程**那一片, 报告里
    「加压现场记录」**整节不见了** —— 恰恰是最该有证据的一次, 证据却没了。
    ⇒ 读字节 + `errors="replace"`: 尾巴缺一截就缺一截, 前面的行照样读得到。

    读不出来(文件不存在 / 没权限)就返回空串 —— 绝不编。
    """
    try:
        raw = path.read_bytes()
    except OSError:
        return ""
    return raw.decode("utf-8", errors="replace")


def read_cpu_evidence(run_id: int) -> str:
    """读回某一轮的加压现场记录(没有 / 读不了就空串)。

    ⚠️ **容错解码** —— 这份文件是板子死的那一刻写的, 尾巴很可能是半行;
    严格解码会抛异常, 害得**整份证据全丢**(见 `_read_text_lenient`)。
    """
    if run_id is None:
        return ""
    return _read_text_lenient(cpu_evidence_path(run_id))


#: 「加压现场记录」的三种状态 —— **「没有」和「有但是空的」是两回事**。
#:
#: ⚠️ 2026-11-02 13:50 实测踩过的坑: 新板死在 2 线程那片, 板子上的
#:    `cpu_evidence_run3.txt` 是 **0 字节**(文件建出来了, 一行数据都没落盘)
#:    ⇒ 读出来是空串 ⇒ 报告**分不清**「压根没有这份记录」和「有, 但被硬复位
#:    带走了」⇒ **静悄悄地什么都不说** —— 而这恰恰是最该有证据的一次。
#:    ⇒ 报告要**明说**是哪一种。
CPU_EVIDENCE_NONE = "none"      #: 压根没有这个文件
CPU_EVIDENCE_EMPTY = "empty"    #: 文件在, 但**一个字节都没有**
CPU_EVIDENCE_CONTENT = "content"  #: 有内容


def cpu_evidence_state(run_id: int) -> str:
    """「加压现场记录」是**没有 / 有但是空的 / 有内容**。"""
    if run_id is None:
        return CPU_EVIDENCE_NONE
    try:
        p = cpu_evidence_path(run_id)
        if not p.exists():
            return CPU_EVIDENCE_NONE
        if p.stat().st_size <= 0:
            return CPU_EVIDENCE_EMPTY
    except OSError:
        return CPU_EVIDENCE_NONE
    return CPU_EVIDENCE_CONTENT


#: 「加压**结束**」那一行 —— 正常跑完才会写(`server/tests/cpu.py` 的
#: `note="加压结束"`); 「加压**起跑**」那一行 —— 每次起跑都写
#: (`note=f"起跑 {label}"`)。
#:
#: ⚠️ 认的是「**起跑**」带一个**空格** —— 文件**头**里那一行是
#: 「# 起跑时板子背景负载: …」, 那是「起跑**时**」, 不是「起跑 」⇒ 认错
#: 会把「压根没加压」当成「加压起手了」。
_EVID_LOAD_START = "起跑 "
_EVID_LOAD_DONE = "加压结束"


def cpu_evidence_cut_short(run_id: int) -> bool:
    """加压**起手了, 但没跑完** ⇒ 板子**死在加压途中**。

    **为什么需要它** (2026-11-04 实测踩出来的):

        「上一次是硬复位」+「死的时候我们的测试正在上面跑」这条判据, 靠的是
        **上一次启动的 journal 尾巴**里看得见我们的痕迹
        (`our_run_at_death`)。⚠️ **但硬复位时 journald 还没来得及落盘的那
        几秒, 尾巴里是没有的** —— 而那几秒**正好**是「加压刚起手」的窗口
        (实测板子死在起手约 2 秒)。

        实测(报告 `A10000000000010_20261104_140033.pdf`): 板子死在 CPU 项
        起手约 2 秒, journal 尾巴里**只有**上一项的 `sudo … ip link show
        wlan0` 和厂商看门狗那几行 —— **一条 stress-ng 都没有** ⇒
        `our_run_at_death` = False ⇒ 这一项判 `stopped`(「服务重启, 本项
        没跑完」)⇒ **不计入通过率分母** ⇒ 一块**在满载下硬复位**的板子,
        只要其它项都过, 报告上看起来是**合格**的 ⇒ **产线会把它放行**。

    ⇒ 这里用**另一条通道**: 「加压现场记录」文件 —— 它是**逐行 fsync 落盘**
    的(硬复位**带不走**), 头里有「起跑」那一行 ⇒ 加压确实开始了; 而
    「加压结束」那一行**只在正常跑完时才写** ⇒ 没有它就说明**没跑完**。

    **口径**: 看**最后一个**标记 —— 一轮跑完几轮之后又死在下一轮里时,
    文件里会同时有「加压结束」(上一轮)和「起跑」(这一轮) ⇒ 最后一个标记
    是「起跑」才算是死在加压途中。

    返回 False = 没有这份记录 / 没起跑 / 跑完了 / 读不出来 —— **绝不编**。
    """
    text = read_cpu_evidence(run_id)
    if not text:
        return False
    last = None
    for line in text.splitlines():
        if _EVID_LOAD_DONE in line:
            last = "done"
        elif _EVID_LOAD_START in line:
            last = "start"
    return last == "start"


# ── pstore: 硬复位后可能还留着的内核崩溃记录 ──────────────────────────
#
# 为什么值得捞: 板子被硬拉死的时候, 内核**往往来不及**把任何东西打到
# 日志里(2026-11-02 新板实测: 上一次启动的日志里最后一行是 stress-ng 的
# metrics, 连一行内核消息都没有)。而 pstore 是内核留给自己的「最后一口气」
# —— 真的是 panic 的话, dmesg 的尾巴会留在 `/sys/fs/pstore/` 里。
#
# 读不到就当没有 —— 这台板子上多半是 root:root 0600, 普通服务账号读不了
# (那就在报告的提示里让操作员自己 `sudo cat` 一下)。
PSTORE_DIR = "/sys/fs/pstore"


def previous_pstore(max_chars: int = 4000) -> str:
    """读 `/sys/fs/pstore/` —— 硬复位可能留下的内核崩溃记录。

    返回空串表示「没有 / 读不到」—— **绝不编**: 没记录就是没记录。
    """
    try:
        names = sorted(os.listdir(PSTORE_DIR))
    except OSError:
        return ""
    chunks = []
    for name in names:
        path = os.path.join(PSTORE_DIR, name)
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError:
            continue
        text = data.decode("utf-8", "replace").strip()
        if not text:
            continue
        chunks.append(f"===== {name} =====\n{text}")
    if not chunks:
        return ""
    return ("[pstore —— 硬复位可能留下的内核崩溃记录]\n"
            + "\n".join(chunks))[-max_chars:]


# ── 「板子死在哪一级加压」: 把现场记录翻译成**一句结论** ──────────────
#
# 现场记录是**一屏原始数据**(200ms 一行, 一轮下来几十行) —— 读者得自己
# 翻到最后一行、看线程数、看温度、再回头找「起跑 第 N/M 片」那几行。
# 而真正要回答的问题只有一句: **板子死在第几级加压上**。
_EV_SAMPLE_RE = re.compile(
    r"^t=\+(?P<t>[\d.]+)s\s+threads=(?P<th>\d+)\s+hot=(?P<hot>[\d.?]+)"
    r"(?P<rest>.*)$")
_EV_SLICE_RE = re.compile(
    r"起跑\s*第\s*(?P<idx>\d+)/(?P<total>\d+)\s*片\((?P<threads>\d+)\s*线程")
_EV_LOAD_RE = re.compile(r"load=(?P<v>[\d.]+)%")
_EV_FREQ_RE = re.compile(r"freq=(?P<v>\d+)MHz")
_EV_DV_RE = re.compile(r"dV=(?P<v>\S+)")
#: 每一行里的背景占用 —— **别的活**占的那部分(总占用减去加压线程自己)
_EV_BG_RE = re.compile(r"bg=(?P<v>[\d.]+)%")
#: 文件头里的「# 起跑时板子背景负载: 36.7%(≈2.9 核) —— …」
_EV_HEADER_BG_RE = re.compile(r"^#\s*起跑时板子背景负载:\s*(?P<v>[\d.]+)%",
                              re.M)
_EV_CRIT_RE = re.compile(r"critical@(?P<v>\d+(?:\.\d+)?)℃")
#: 文件头那一行「# 起始温度: cpub_thermal_zone=47.4℃ cpul_thermal_zone=45.1℃ …」
_EV_START_TEMP_RE = re.compile(r"^#\s*起始温度:\s*(?P<rest>.+)$", re.M)
#: 头里单个核区: 名字=NN.N℃
_EV_ZONE_TEMP_RE = re.compile(r"(?P<zone>[A-Za-z0-9_]+)=(?P<v>[\d.]+)℃")


def _parse_start_temp(text: str):
    """从**文件头**读「起始温度」⇒ (全板最热核区名, 温度℃, 最热CPU核区名, 温度℃)。

    ⚠️ **为什么这个数是新的关键证据** (2026-11-02 现场实测):
    板子 A10000000000010 **单独**跑 CPU 加压(1→2→3→4, 满核 60 秒)**扛住了**,
    而**整轮老化跑下来**死在 CPU 项 ⇒ 「CPU 负载本身」不是触发条件, 触发条件
    是**进这一项时板子已经是什么状态** ⇒ **起始温度就是那个状态**。
    而这一项的指标是**跑完**才落库的 ⇒ 板子死在半路时 `start_temp_c` **也跟着
    没了** ⇒ **只有文件头里那一份还在**(而且 fsync 之后硬复位也带不走)。
    ⇒ 结论里**必须**把这个数说出来, 否则读者只看到「死时多热」, 看不出
    「**进这一项的时候就已经多热了**」。

    ⚠️⚠️ **口径必须跟 `hot=` 一致** (2026-11-02 又修了一次):
    每一行的 `hot=` 是**全板最热核区**(`max(z0.values())`)⇒ 「起跑温度」
    也必须是**全板最热**, 否则「起跑 46.9℃」跟「死时最热 57.1℃」两个数
    **不是一个口径**, 读者会拿它们相减。
    ⚠️ **而且不能只看 CPU 核区**: 前面那一项(GPU 渲染 105 秒)烤热的
    **可能是 GPU 核区** —— 只取 CPU 核区就把它**藏起来**了, 读者会误判成
    「板子进这一项的时候是凉的」⇒ **全板最热才是那个状态**, CPU 核区的值
    另给一个(它是跟 `_read_cpu_temp()` 同一个口径的数, 读者要对比时用)。
    """
    if not text:
        return None, None, None, None
    m = _EV_START_TEMP_RE.search(text)
    if not m:
        return None, None, None, None
    zones = {}
    for zm in _EV_ZONE_TEMP_RE.finditer(m.group("rest")):
        try:
            zones[zm.group("zone")] = float(zm.group("v"))
        except ValueError:
            continue
    if not zones:
        return None, None, None, None
    hot_name = max(zones, key=lambda k: zones[k])
    hot_val = zones[hot_name]
    # CPU 核区 = 名字里带 `cpu`(跟 `_read_cpu_temp()` 同一个口径:
    # **大核 cpub 才是热点**, 小核 cpul 常年低 3℃)。一个都没有 ⇒ 没有。
    cpu_zones = {k: v for k, v in zones.items() if "cpu" in k}
    if cpu_zones:
        cpu_name = max(cpu_zones, key=lambda k: cpu_zones[k])
        cpu_val = cpu_zones[cpu_name]
    else:
        cpu_name, cpu_val = None, None
    return hot_name, hot_val, cpu_name, cpu_val


#: 文件头那一行「# 进这一项时的板子状态: 核数 8 | 开机 6分12秒 | …」
_EV_STATE_RE = re.compile(r"^#\s*进这一项时的板子状态:\s*(?P<rest>.+)$", re.M)
_EV_CORES_RE = re.compile(r"核数\s*(?P<v>\d+)")
_EV_UPTIME_RE = re.compile(r"开机\s*(?P<v>\d+)分(?P<s>\d+)秒")
_EV_LOADAVG_RE = re.compile(r"负载均值\(1/5/15分\)\s*(?P<v>[\d.]+/[\d.]+/[\d.]+)")
_EV_MEM_RE = re.compile(r"可用内存\s*(?P<v>\d+)MB")


def _parse_board_state(text: str) -> dict:
    """从**文件头**读「进这一项时的板子状态」⇒ 核数 / 开机时长 / 负载均值 / 可用内存。

    ⚠️ **为什么要有它**: 「起跑温度」只说了**热**, 而 2026-11-02 那次现场
    拿到的起跑温度是**凉的**(46.9℃)⇒ **「热」出局** ⇒ 要判「进这一项时
    板子是什么状态」就得看**别的维度**(已经连续压了多久 / 系统里堆着多少
    负载)。**负载均值**是累积压力的直接度量(5 分钟那个正好盖住前面的项),
    **核数**决定加压台阶封顶在几线程。读不出来就返回空 dict —— **不编**。
    """
    out: dict = {}
    if not text:
        return out
    m = _EV_STATE_RE.search(text)
    if not m:
        return out
    rest = m.group("rest")
    cm = _EV_CORES_RE.search(rest)
    if cm:
        try:
            out["cores"] = int(cm.group("v"))
        except ValueError:
            pass
    um = _EV_UPTIME_RE.search(rest)
    if um:
        try:
            out["uptime_sec"] = int(um.group("v")) * 60 + int(um.group("s"))
        except ValueError:
            pass
    lm = _EV_LOADAVG_RE.search(rest)
    if lm:
        try:
            out["load"] = [float(x) for x in lm.group("v").split("/")]
        except ValueError:
            pass
    mm = _EV_MEM_RE.search(rest)
    if mm:
        try:
            out["mem_avail_mb"] = int(mm.group("v"))
        except ValueError:
            pass
    return out


def summarize_cpu_death(text: str) -> str:
    """把「加压现场记录」翻译成**一句结论**。

    输出四行:
      ① 板子死在第几级加压(几线程)、起手后多少秒;
      ② 死前最后一行是什么样(温度 / 频率 / 负载 / 电压台阶);
      ③ 前面几级板子扛住了 ⇒ 这一级就是它的天花板;
      ④ 热是不是原因(拿内核的 critical 阈值比)。

    ⚠️ 另外还会加上**起跑温度**(从文件头读) —— 板子**进这一项**时多热,
    跟它**死在的时候**多热, 是两回事: 前者说明**裕量本来剩多少**, 后者只
    说明它死的时候在哪。2026-11-02 实测: 板子**单独**加压扛得住、整轮才死
    ⇒ 触发条件是「进这一项时板子已经什么状态」⇒ **起跑温度是关键证据**。

    ⚠️ **只在该项真的是「板子死在这一项上」时才调它** —— 现场记录文件
    跑完也在, 那时候最后一行是「跑完了」而不是「死了」。
    """
    if not text:
        return ""
    samples = []
    for line in text.splitlines():
        m = _EV_SAMPLE_RE.match(line.strip())
        if not m:
            continue
        try:
            t = float(m.group("t"))
        except ValueError:
            continue
        try:
            hot = float(m.group("hot"))
        except ValueError:
            hot = None
        samples.append({"t": t, "threads": int(m.group("th")),
                        "hot": hot, "rest": m.group("rest")})
    if not samples:
        return ""
    last = samples[-1]

    # 最后一次「起跑 第 N/M 片」= 板子死在的那一级。
    # 同一级的标记会出现在**连续两行**上(起跑那一刻 + 下一个采样点),
    # 要取**第一行** —— 那才是这一片真正起跑的时刻。
    markers = [(s, _EV_SLICE_RE.search(s["rest"])) for s in samples]
    markers = [(s, m) for s, m in markers if m]
    death = None
    if markers:
        last_idx = markers[-1][1].group("idx")
        for s, m in markers:
            if m.group("idx") == last_idx:
                death = (s, m)
                break

    out = []
    # ⚠️⚠️ **死亡时刻只能「框」, 不能「钉」** (2026-11-04 改) ——
    #   采样行**每 EVIDENCE_FSYNC_INTERVAL_SEC 秒才 fsync 一次** ⇒ 两次
    #   fsync 之间的采样点**跟着板子一起没了**(硬复位把页缓存带走) ⇒
    #   盘上最后一行是 t_last ⇒ 板子**那时还活着**, 但它**到底什么时候
    #   死的只能框在 (t_last, t_last + fsync 间隔] 里**。
    # ⚠️ **原来这里直接写「板子死在 t=+0.2s」** —— 那是**把上界说成了
    #   事实**(2026-11-04 板子上实测: 最后一行 t=+0.2s, 真实死时刻
    #   **不可知** —— 只知道板子在那之后、10 秒之内没了)。产线拿这句话
    #   去判「死在起手瞬间」还是「跑了半分钟才死」, 会被带偏。
    try:                              # 函数内 import —— 免得循环引用
        from .tests.cpu import EVIDENCE_FSYNC_INTERVAL_SEC as _FS
    except Exception:
        _FS = 10.0
    fs = float(_FS)
    if death:
        s0, m0 = death
        idx, total = m0.group("idx"), m0.group("total")
        dthreads = int(m0.group("threads"))
        dt = last["t"] - s0["t"]
        out.append(
            f"板子死在第 {idx}/{total} 片({dthreads} 线程) —— 这一片起于 "
            f"t=+{s0['t']:.1f}s, 最后一份**落在盘上**的记录停在 "
            f"t=+{last['t']:.1f}s(这一片起手后 {dt:.1f} 秒)⇒ "
            f"板子**那时还活着**, 之后就再没动静了。")
        out.append(
            f"⚠️ **死亡时刻只能框在 起手后 {dt:.1f} 秒 ~ 起手后 "
            f"{dt + fs:.1f} 秒**: 采样行每 {fs:.0f} 秒才 fsync 一次, "
            f"两次之间的采样点**跟着板子一起没了**(硬复位把页缓存带走)⇒ "
            f"**更精确的死时刻这份记录答不了**。")
        survived = [x["threads"] for x in samples if x["t"] < s0["t"]]
        if survived:
            out.append(
                f"前面 {len(set(survived))} 级板子扛住了(最高 {max(survived)} "
                f"线程)⇒ **{dthreads} 线程是这块板子的天花板**。")
    else:
        out.append(
            f"最后一份**落在盘上**的记录停在 t=+{last['t']:.1f}s ⇒ "
            f"板子**那时还活着**, 之后就再没动静了。")
        out.append(
            f"⚠️ **死亡时刻只能框在 t=+{last['t']:.1f}s ~ "
            f"t=+{last['t'] + fs:.1f}s**: 采样行每 {fs:.0f} 秒才 fsync "
            f"一次, 两次之间的采样点**跟着板子一起没了**(硬复位把页缓存"
            f"带走)⇒ **更精确的死时刻这份记录答不了**。")

    # 死前最后一行: 温度 / 频率 / 负载 / 电压台阶
    bits = []
    if last["hot"] is not None:
        bits.append(f"最热 {last['hot']:.1f}℃")
    fm = _EV_FREQ_RE.search(last["rest"])
    if fm:
        bits.append(f"频率 {fm.group('v')}MHz")
    lm = _EV_LOAD_RE.search(last["rest"])
    if lm:
        bits.append(f"负载 {lm.group('v')}%")
    bgm = _EV_BG_RE.search(last["rest"])
    if bgm:
        bits.append(f"背景占用(别的活) {bgm.group('v')}%")
    dm = _EV_DV_RE.search(last["rest"])
    if dm:
        bits.append(f"电压台阶 {dm.group('v')}")
    if bits:
        out.append("死前最后一行: " + " / ".join(bits) + "。")

    # 热是不是原因 —— 拿内核自己的 critical 阈值比
    crits = [float(c) for c in _EV_CRIT_RE.findall(text)]
    crit = min(crits) if crits else None

    # ── **起跑温度**(文件头里那份) ──
    #
    # ⚠️ 2026-11-02 现场实测逼出来的: 板子**单独**跑 CPU 加压**扛住了**,
    # 整轮跑下来才死在 CPU 项 ⇒ 「CPU 负载本身」不是触发条件 ⇒ 触发条件是
    # **进这一项时板子已经是什么状态** ⇒ **起始温度就是那个状态**。
    # 而这一项的指标是**跑完**才落库的 ⇒ 死在半路时 `start_temp_c` 跟着没了
    # ⇒ **只有文件头里那一份还在**。⇒ 结论里必须把它说出来。
    #
    # ⚠️ **口径 = 全板最热核区** —— 跟每行的 `hot=`(死时最热)同一个口径,
    # 两个数才能相减; 只取 CPU 核区会把 GPU 核区烤的那部分**藏起来**。
    start_zone, start_hot, cpu_zone, cpu_hot = _parse_start_temp(text)
    if start_hot is not None:
        extra = ""
        if cpu_hot is not None and cpu_zone != start_zone:
            extra = f", CPU 核区当时 {cpu_hot:.1f}℃"
        out.append(
            f"**起跑温度 {start_hot:.1f}℃**(全板最热 {start_zone}{extra}; "
            f"取自证据文件头)—— 板子**进这一项的时候就已经这个温度**。"
            + (f" 离内核 critical 阈值({crit:g}℃)本来就只剩 "
               f"{crit - start_hot:.1f}℃。" if crit and start_hot < crit else ""))

    # ── **进这一项时板子是什么状态**(除了热, 还有别的维度) ──
    #
    # ⚠️ 2026-11-02 现场实测: 那次拿到的起跑温度是**凉的**(46.9℃)⇒
    # **「热」这条线出局** ⇒ 要判「进这一项时板子已经是什么状态」, 光看
    # 温度不够, 还得看**已经连续压了多久 / 系统里堆着多少负载**。
    state = _parse_board_state(text)
    if state:
        bits = []
        if state.get("cores"):
            bits.append(f"整机 {state['cores']} 核")
        up = state.get("uptime_sec")
        if up is not None:
            bits.append(f"板子已经连续开机 {up // 60} 分 {up % 60} 秒")
        ld = state.get("load")
        if ld:
            bits.append("进这一项时的负载均值(1/5/15 分) "
                        + "/".join(f"{x:.1f}" for x in ld))
        mb = state.get("mem_avail_mb")
        if mb is not None:
            bits.append(f"可用内存 {mb}MB")
        if bits:
            out.append("**进这一项时板子的状态**: " + " / ".join(bits) + "。")

    # ── **板子上还有别的活**(背景负载) ──
    #
    # ⚠️ **2026-11-03 14:33 那次硬复位靠这个数才算得清账**:
    # 现场记录里**只有 1 个加压线程**的那一级, 板子的占用率在
    # **22.7% ~ 93.2%** 之间乱跳 ⇒ 板子上**别的活**当时占着 0.8 ~ 6.4 核;
    # 10-30 活着跑完 8 线程满载 48 秒的那一次, 同一级只有
    # **23.7% ~ 33.7%** ⇒ 别的活只占 0.3 核, 而且**是平的**。
    # ⇒ **同样是 8 线程, 板子的总功耗差了好几倍** ⇒ 死不死看的是**总**,
    # 不是加压线程数 ⇒ 结论里必须把这个差额说出来, 否则报告会把
    # 「板子扛不住 8 线程」当成板子自己的缺陷, 而其实是「这台机器当时太忙」。
    ncpu = state.get("cores") or 8

    def _cores_of(pct: float) -> float:
        return pct * ncpu / 100.0

    bg_header = _EV_HEADER_BG_RE.search(text)
    bg_vals = []
    for s in samples:
        bm = _EV_BG_RE.search(s["rest"])
        if bm:
            try:
                bg_vals.append(float(bm.group("v")))
            except ValueError:
                pass
    bg_bits = []
    hp = None
    if bg_header:
        try:
            hp = float(bg_header.group("v"))
        except ValueError:
            hp = None
        if hp is not None:
            bg_bits.append(f"起跑时 {hp:.1f}%(≈{_cores_of(hp):.1f} 核)")
    if bg_vals:
        bg_bits.append(f"加压期间平均 {sum(bg_vals) / len(bg_vals):.1f}%"
                       f"(≈{_cores_of(sum(bg_vals) / len(bg_vals)):.1f} 核)"
                       f", 峰值 {max(bg_vals):.1f}%"
                       f"(≈{_cores_of(max(bg_vals)):.1f} 核)")
    if bg_bits:
        out.append("**板子上还有别的活**: " + " / ".join(bg_bits)
                   + " —— 那部分**不是加压线程**(浏览器、代理、别的进程), "
                     "但**照样算**在板子的功耗里。")
        # ⚠️ **头里的起跑值也要算进来** —— 「起跑时板子上就有 3 核的别的活」
        #    和「加压期间一直有 3 核的别的活」是同一个量级的问题。
        worst_vals = list(bg_vals)
        if hp is not None:
            worst_vals.append(hp)
        worst = max(worst_vals) if worst_vals else None
        if worst is not None and worst >= 25.0:
            out.append(
                f"⚠️ **加压的时候板子上还有 ≈{_cores_of(worst):.1f} 核的别的活**"
                f" ⇒ 板子的**总**功耗 = 加压线程 + 这些 ⇒ 同样的加压, "
                f"板子上没别的活时扛得住、这台机器上扛不住 —— "
                f"**这一轮的死, 不能只算在板子头上**, 得先把板子上别的活停掉"
                f"再复测一次才算数。")
        elif worst is not None:
            out.append(
                f"⇒ 加压期间板子上**几乎没有别的活**(峰值 ≈"
                f"{_cores_of(worst):.1f} 核) ⇒ 这一次板子的死活**只**取决于"
                f"加压线程本身。")

    if last["hot"] is not None and crits:
        if last["hot"] < crit:
            out.append(
                f"**热不是原因**: 死时最热 {last['hot']:.1f}℃, 离内核 critical "
                f"阈值({crit:g}℃)还差 {crit - last['hot']:.1f}℃。")
            if start_hot is not None and crit - start_hot < 20:
                out.append(
                    f"⚠️ **但裕量本来就薄**: 起跑时离 critical 就只剩 "
                    f"{crit - start_hot:.1f}℃ ⇒ 这一项的判定"
                    f"**跟进这一项之前板子被烤到什么程度直接相关**。")
        else:
            out.append(
                f"⚠️ **热可能就是原因**: 死时最热 {last['hot']:.1f}℃ 已经"
                f"到/过内核 critical 阈值({crit:g}℃)。")
    return "\n".join(out)


def capture_enabled() -> bool:
    """服务启动时要不要去取「上一次启动」的内核日志。

    **默认开**。取的是**这一台被测设备上一次启动**的内核日志尾巴, 只取
    尾巴, 落在数据目录的 `logs/` 下并印进那一轮的报告 —— **不写历史库**、
    不碰当前这次启动的日志。

    `config.yaml` 里 `report.capture_boot_evidence: false` 可以关掉 ——
    现场不想让服务碰系统日志的话, 一行就能关。
    """
    cfg = Config.get("report") or {}
    return bool(cfg.get("capture_boot_evidence", True))


def evidence_path(run_id: int) -> Path:
    """某一轮的死因证据文件。"""
    return evidence_dir() / f"boot_evidence_run{run_id}.txt"


def save_evidence(run_id: int, text: str) -> Optional[Path]:
    """把证据写成旁挂文件。返回路径; 写不了就返回 None(不影响主流程)。"""
    if not text:
        return None
    path = evidence_path(run_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path
    except OSError:
        return None


def read_evidence(run_id: int) -> str:
    """读回某一轮的死因证据(没有就空串)。"""
    if run_id is None:
        return ""
    return _read_text_lenient(evidence_path(run_id))


async def previous_boot_evidence(max_lines: int = 3000,
                                 max_chars: int = 8000) -> str:
    """捞上一轮(上一次启动)的内核日志尾巴。

    返回一段文本(带来源说明 + 「上一次是软重启还是硬复位」的判定),
    捞不到就返回**空串**。

    先试提权(`sudo -n`)再试不提权 —— 日志文件通常是 root:adm 0640,
    普通服务账号读不到; 但 `sudo` 没配免密时也不能就这么算了,
    不提权那份也许能读到(比如 journal 是 world-readable 的)。

    **窗口开得大 (max_lines 行)**: 死因往往**不在**最后几行 —— 新板实测
    死前最后一条内核消息是 10:03:58 的 HDMI, 板子死在 10:06:32, 中间
    **两分半一句日志都没有**。只看尾巴(原来 40 行)看不出这段静默。

    **BT 噪音收成一行** (`_collapse_bt_noise`): 厂商那个 brcm_patchram
    每 8.25 s 失败一次、每失败一次切一次电源轨 —— 几百行下来把真死因
    淹掉了。收成一行, 但末尾那几句原样留着(死前最后一次切换在那儿)。

    `max_chars` 是**尾部**截断 —— 死因在日志末尾(kernel panic 的最后
    几行、thermal trip 的那几行), 头部反而是开机 banner。

    ⚠️ **尾部留 8000 字符(≈60 行), 不是 2000(≈15 行)** (2026-11-02 改):
    厂商那个看门狗是**周期性**跑的 —— 只看最后 15 行, 往往只看得见它
    「最后一次检查成功」的那一行, 看不见它**本该再跑一次却没跑**的那个
    空档(那正是「看门狗把我们这个服务判成卡死」的现场)。
    """
    pstore = previous_pstore()
    for cmd_tpl, label in _CANDIDATES:
        if find_tool(cmd_tpl[0]) is None:
            continue
        cmd = [c.format(n=max_lines) for c in cmd_tpl]
        cumulative = "累积" in label
        for use_sudo in (True, False):
            full = with_sudo(cmd, timeout_sec=15) if use_sudo else list(cmd)
            try:
                rc, out, _err = await run_cmd(full, timeout=15)
            except Exception:
                continue
            if rc != 0:
                continue
            text = (out or "").strip()
            if not text or len(text) < 20:
                continue
            if any(marker in text for marker in _EMPTY_MARKERS):
                continue
            if cumulative:
                # 跨重启累积的文件: 尾巴是**本次开机**的内容, 得挑可疑行
                text = _pick_suspect_lines(text, label)
                if not text:
                    continue
            else:
                # BT 噪音收成一行 —— 别让重复的失败/电源轨切换淹掉真死因
                text = _collapse_bt_noise(text)
            kind, why = classify_reboot(text)
            head = f"[{label}]\n【上一次是{kind}】{why}\n"
            if cumulative:
                return head + text + _pstore_block(pstore)
            return head + text[-max_chars:] + _pstore_block(pstore)
    return ""


def _pstore_block(pstore: str) -> str:
    """pstore 那一段(有就带上, 没有就不写那一行)。"""
    if not pstore:
        return ""
    return "\n" + pstore


def format_boot_evidence(evidence: str) -> str:
    """把证据整理成报告里那一段的样子。"""
    if not evidence:
        return ""
    return (
        "板子为什么死 —— 下面是**上一次启动**的内核日志尾巴"
        "(重启动作把这轮测试连同日志一起带走了, 死因只留在这里; "
        "开头那一行先分清上一次是**软重启**还是**硬复位**):\n"
        + evidence
    )


def evidence_hint() -> str:
    """给操作员的一句话: 证据没捞到时, 该自己去哪找。"""
    return (
        "板子重启后死因只在**上一次启动**的日志里 —— 在板子上执行 "
        "`journalctl -b -1 -n 60` 或 `dmesg -T | tail -60`(需要的话加 sudo); "
        "内核要是崩过, `sudo cat /sys/fs/pstore/*` 里还留着崩溃记录。"
    )


__all__ = [
    "previous_boot_evidence",
    "previous_pstore",
    "summarize_cpu_death",
    "classify_reboot",
    "classify_evidence_text",
    "our_run_at_death",
    "format_boot_evidence",
    "evidence_hint",
    "capture_enabled",
    "evidence_dir",
    "evidence_path",
    "save_evidence",
    "read_evidence",
    "read_cpu_evidence",
    "cpu_evidence_state",
    "cpu_evidence_cut_short",
    "HARD_RESET_ITEM_ERROR",
    "HARD_RESET_RUN_ERROR",
    "SOFT_ITEM_ERROR",
    "SOFT_RUN_ERROR",
]
