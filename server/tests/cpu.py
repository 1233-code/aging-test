"""CPU 压力测试 - 完整实现"""
import asyncio
import glob
import os
import time
from typing import Optional

from .base import TestCase, TestContext, TestResult, register
from ._tools import (
    find_tool, with_sudo, sudo_hint_if_unavailable, run_proc_with_progress,
    deps_fix_hint, tool_missing_reason, humanize_exception, tool_exit_reason,
)
from ..boot_evidence import cpu_evidence_path, current_boot_id

# ── 热保护 (§6.2「不许把板子烤死」那一段) ─────────────────────────────────
#
# **为什么要有这一段** —— 2026-10-29 新板子实测: 整轮跑到最后一项
# (CPU 压力) 时**板子直接重启**, 报告上只剩一句「服务重启, 这一轮中途夭折」。
# 查这块板子的内核热框架(`/sys/class/thermal`):
#   · thermal_zone: cpul / cpub / gpu / npu / ddr 五个
#   · 被动降频 trip = 70℃ / 90℃, **critical trip = 110℃**
#   · 冷却手段**只有** cpufreq 降频(cpu0 / cpu4 两个 cooling_device),
#     风扇不在热框架里(厂商私有 GPIO 驱动, §6.8)
# critical trip 一到, 内核走 emergency shutdown ⇒ **板子当场重启**, 整轮
# 连同日志一起没了 —— 这是最坏的结果: 没有结论, 也没有证据。
#
# 所以这一项自己**先把板子护住**:
#   ① 看**最热**的那个核区(大核 cpub 才是热点, 只看 cpul 会低估好几度);
#   ② 温度到了 abort_temp_c 就**主动收工**, 不等内核来收;
#   ③ **分片**跑(默认 5s 一片), 片与片之间查温度 —— 这么切分是为了
#      **不用特权 kill 就能中途停**: 上一片自己已经退出了。
#      (root 起的进程, 服务这个普通用户对它发信号必然 EPERM —— §11.2)
#
# 两个温度阈值的**关系**(别搞反):
#   max_temp_c   = 判定阈值(峰值超过它 = 判 failed) —— 默认 85℃
#   abort_temp_c = 提前收手的阈值 —— 默认 95℃, **必须高于** max_temp_c,
#                  且要**低于内核的 critical trip(110℃)**, 给内核留余量。
DEFAULT_SLICE_SEC = 5.0
DEFAULT_ABORT_TEMP_C = 95.0


# 逐级加压的台阶 —— **为什么是 1→2→3→4→8** (2026-11-02 改)
#
# 原来是 1→2→4→8。实测新板(SN A10000000000010, 2026-11-02): 板子扛住了
# 1 线程和 2 线程, **死在 4 线程那一片起手约 1 秒**(现场记录里最后一行
# 是 t=+11.9s threads=4 hot=57.1℃ freq=1800MHz)。⇒ 「2 还是 4」之间到底
# 卡在哪, 报告上只能**猜**。
# ⇒ 中间补一级 **3**: 「3 线程活着、4 线程死」才是这块板子的**天花板**
# (缺陷特征); 「2 活着 4 死」只是个范围。
#
# ⚠️ **对扛得住的板子没有风险, 只有一点点代价**:
#   · 台阶只是**多花 5 秒**爬到满载, **总时长不变**(还是 n_slices × 5 秒),
#     峰值负载不变;
#   · 满载那一段的片数少一片(9 片 → 8 片, 45 秒 → 40 秒)—— 这是拿 5 秒
#     满载换「天花板卡在几线程」这个**缺陷特征**, 值得。
#
# ⚠️ **短跑也得真的满载过一次**(2026-11-02 加 `_ramp_threads` 的 n_slices):
#   台阶是 5 级(1→2→3→4→8), 一轮只有 4 片时(比如只跑 20 秒), 照老规矩
#   一级一级爬 ⇒ **最后一片才 4 线程, 满载根本没跑到** ⇒ 「板子在 8 线程
#   下会死」这件事**测不出来** ⇒ 板子**假通过**。
#   ⇒ 片数不够时**压缩台阶**: 丢掉**中间**那几级(起点还是 1 线程 —— 板子
#     死在**起手一秒内**, 起步必须轻; 离天花板最近那级留着 —— 那才是
#     缺陷特征), 但**满载那一片必须留着**。
#   例(满载 8 线程): 12 片 → 1,2,3,4,8,8,8,8,8,8,8,8; 4 片 → 1,2,4,8。
_LADDER = (1, 2, 3, 4, 8, 16, 32)


# ── 加压线程数的**天花板**(2026-11-04 定) ─────────────────────────────────
#
# ⚠️ **为什么不是满核 8 线程**(实测, SN A10000000000010 + 另一台好板):
#
#   · 这块板子 **6 核稳, 第 7 核档起手约 2 秒硬复位** —— 起跑 60.9℃,
#     critical trip 110℃ ⇒ **不是热**; 最后一行证据 `t=+0.2s threads=8
#     freq=1344MHz` ⇒ 大核还没拉到 1800 就没了 ⇒ 死在**电流台阶**上;
#   · 同一批里的**好板 8 核没事** ⇒ 这是**个体差异**, 不是型号上限;
#   · ⇒ 满核 8 线程是**过应力**: 一块**日常用得好好的**板子(前台业务
#     连 6 核都跑不满)在老化里被 8 线程压死 ⇒ **工具把好板子判死**
#     —— 产线上**误杀一块好板**的代价, 比漏判一块边缘板高得多。
#
# ⇒ **默认 6 核, 且任何配置/页面输入都夹到 ≤ 6**(不只是默认值 —— 页面
#   上那个旋钮也拧不上去)。
#   ⚠️ **这是 2026-11-04 当时的口径** —— 2026-11-09 已降到 **5**, 见下面
#     那段(「6 核封顶」封的是**加压线程数**, 不是**整板占用**)。
#
# ⚠️ **代价, 明说**(别假装没有): 只能扛 6 核的板子现在**判过**了 ——
#   它的缺陷特征(「7 核就复位」)老化报告里**看不出来**, 只能靠
#   `scripts/probe_cpu_ramp.py` 单独测(那个脚本不受这条限制)。
#
# ⚠️⚠️ **2026-11-04 16:15 那一次把「6 核稳」推翻了 —— 天花板本身不变**:
#
#   装了 0.1.0-14(带这条天花板)之后, 应用**只跑 6 线程**
#   (配置快照 `threads=6`, 证据文件 `t=+0.2s threads=6 hot=50.2℃`),
#   这块板子**照样硬复位**了 —— 证据文件里**没有**「加压结束」
#   ⇒ 死在加压途中 ⇒ 判 failed, **不放行**(这条通道是通的)。
#   ⇒ 「6 核稳」那个结论是拿 `scripts/probe_cpu_ramp.py` 的**短跑台阶**
#     (每级几秒)测出来的, **不是**应用这一轮「连续加压 60 秒」的条件 ——
#     **两个不是同一个实验**, 拿前者给后者背书是错的。
#   ⇒ 也可能是**板子自己在退化**: 那一轮是 **Run ID 4**, 而这次开机
#     的日志尾巴里, 应用自己在 16:13:48 跑过 `journalctl -b -1`
#     ⇒ 上一次启动**也是硬复位死的** ⇒ 这块板子**之前已经死过至少 3 次**。
#   ⇒ **天花板仍然留 6**: 「误杀好板」的代价高于「漏判边缘板」, 这条不变。
#     但「6 核稳」这句话**别再拿来说事** —— 现在只能说「6 核能过大多数
#     板子; 这块板子在应用那一轮里没扛住, 死因还没定论」。
#
# ⚠️⚠️⚠️ **2026-11-09: 天花板从 6 降到 5 —— 这次有「应用 vs 终端」的对照**
#
# **当天实测(SN 0058, 三次死在 CPU 项上)**:
#   · 证据头里的「起跑时板子背景负载」: 17.0% / 9.1% / **21.2%**
#     (≈ 1.4 / 0.7 / **1.7 核**) ⇒ 6 线程(8 核的 75%)+ 背景
#     ⇒ **整板总占用 6.7~7.7 核**;
#   · 那块板子的天花板是「整板 ≈ 7 核」(安静板子上量出来的: 6 稳、
#     第 7 核档起手约 2 秒硬复位) ⇒ **过线**;
#   · 背景是谁在吃? **板子自己的桌面栈**: `X` + `x11vnc`(厂商的
#     `anfuhua-mobile-control.service` 拉的屏幕桥) + **chromium 打开的
#     正好是老化页面自己** ⇒ 应用每秒推 2~3 条事件 ⇒ 浏览器重排 ⇒
#     X 合成 ⇒ x11vnc 编码推帧。**应用自己的界面在给 CPU 项加压。**
#   · **对照实验(2026-11-09 当天, 同一台板子)**:
#     终端里 `sudo -n /usr/bin/timeout -k 5 90 /usr/bin/stress-ng
#     --cpu 6 --timeout 60.0s --metrics-brief` **6 核抗住了 60 秒**
#     ⇒ **板子本身扛得住 6 核**(连 0.9 核背景一起 ≈ 6.9 核也没死)
#     ⇒ 死因在**应用这一侧**, 不在硅片上。
#
# ⇒ **天花板降到 5 核**: 5 线程 + 应用自己最坏那 1.7 核背景 = 6.7 核
#   —— 正好落在刚验证过「能活」的那条水位下面。
#   ⚠️ **必须降的是**代码里的**硬上限**, 不只是 config.yaml 的默认值:
#     现场旧盘的 config.yaml 还写着 `threads: 6`(页面每次从 config.yaml
#     取默认值, 又没有 localStorage), 只改默认值 ⇒ **旧盘上根本不生效**。
#     硬上限是唯一能盖过任何配置的地方。
#   ⚠️ **代价, 明说**: 只能扛 6 核的板子现在也判过(跟 6 核时一样,
#     「6 核就复位」这个缺陷特征在老化报告里看不出来)—— 仍然只能靠
#     `scripts/probe_cpu_ramp.py` 单独测(那个脚本不受这条限制),
#     或者在**终端**里手跑(命令见上)。
#   ⚠️ **「0 = 自动」当时也等于 5**(自动 = 核数, 再被上限夹住) ——
#     以前填 0 会回到满核那条危险路上, 那时不会了。
#     ⚠️⚠️ **2026-11-10: 这条路整个取消了** —— 见下面「范围 1..8」那段。
#
# ⚠️⚠️ **2026-11-09 晚(更晚): 默认值定在 4 核 —— 上限仍然是 8**
#
#   · **用户的决定(原话)**: 「确定了, 默认四核, 但是像现在这样支持用户
#     自己选择核数, 四核比较保险」
#   · ⇒ **`MAX_STRESS_THREADS` 仍然是 8**(页面旋钮 max 也是 8): 用户要
#     **自己在页面上拧**线程数 ⇒ **往上调的权限保留**;
#     **默认值改成 4** 在 `config.yaml` 的 `defaults.cpu.threads` ——
#     **默认值和上限是两回事**, 别把上限一起降了(降了旋钮就拧不上去,
#     用户要的「自己选」就没了);
#   · ⇒ **为什么 4 比 5 保险**: 5 核 + 应用自己那 1.7 核背景 ≈ 6.7 核,
#     而板子天花板 ≈ 7 核(2026-11-04 短跑台阶探针)⇒ 4 核 ≈ 5.7 核 ⇒ 留余量;
#   · ⚠️ **满核那一档仍是过应力**(2026-11-04 实测会把边缘板压死)⇒ 往上调
#     之前想清楚; **每轮之间让板子凉回同一个起点**(看 `start_temp_c`);
#   · ✅ 硬复位仍**判 failed 不放行**(§9.3.3 那道兜底一个字没动)。
#
# ⚠️⚠️⚠️ **2026-11-09 晚: 天花板放回 8 核(满核) —— 让工人自己找平衡点**
#   (后来**默认值又定在 4**, 见上面那段; **天花板本身没再动**)
#
# **为什么把 5 核撤回**: 那个 5 核**没有被验证过**(当天早上定的, 当天晚上
#   就发现「第二轮又挂了」)。⇒ 而「第二轮」的变量**不是线程数**, 是
#   **上一轮留下的热**(见下)⇒ 五核那个数是**在热板子上量的**, 量出来的
#   东西**不能当结论**。
#
# ⇒ **用户的决定(原话)**: 「既然降到 5 核都还是会重启, 那就直接改回八核
#   默认, 我可以手动降低核心, 不用你改来改去, 这样我先找到一个平衡点
#   再来修改默认参数」⇒ **天花板放回 8(满核)**, 工人能在**页面上自己拧**
#   线程数, 一次只动一个变量, 找到平衡点之后再定默认参数。
#
# ⚠️ **这是「实验台」状态, 不是「验证过的政策」**:
#   · 8 核 = 满核 = 2026-11-04 实测**会硬复位**的那一档(边缘板);
#   · ✅ 但硬复位**不再是「静悄悄死掉」**: 「死在加压途中 ⇒ 判 failed
#     不放行」那道兜底还开着(§9.3.3)⇒ 板子死了会**判不合格**, 不会
#     因为「服务重启、本项没跑完」而被当成合格品放行;
#   · ⚠️ **每次实验必须记「起跑温度」**(`start_temp_c` / 现场记录的头)
#     —— **热板子上的实验和冷板子上的实验不是同一个实验**。实测:
#     起跑 44.9℃ ⇒ 峰值 72.4℃(温升 +27.5℃); 而第二轮起跑 ≈ 上一轮
#     峰值 ≈ 68~72℃ ⇒ 峰值 ≈ **96~100℃** ⇒ 超 85℃ 判定线 ⇒ 判 failed,
#     离 110℃ critical 只差 10~14℃ ⇒ **内核 emergency shutdown ⇒
#     板子当场重启**。⇒ **找平衡点时, 每轮之间要让板子凉回同一个起点。**
#
# ⚠️⚠️⚠️ **2026-11-10: 线程数的「范围」定死 1..8 —— 下界也抬了**
#
#   · **用户的决定(原话)**: 「cpu核数控制在最低是1,最高是8」
#   · ⇒ **下限从 0 抬到 1**: 以前 `0` = 「自动」= 按核数 = **满核 8** ——
#     而满核 8 正是 2026-11-04 实测**会把板子压死**的那一档(见上面)
#     ⇒ **「填 0 就回到满核」那条路封掉**: 页面旋钮 `min="1"`, 后端把
#     `0` / 负数 / 非法值 一律夹到 **1**;
#   · ⇒ **上限一个字没动**(仍是 8): 用户要自己在页面上拧, 找平衡点
#     ⇒ 旋钮 `max="8"` 不动, `MAX_STRESS_THREADS` 也不动;
#   · ⇒ **「0 = 自动」这个说法在界面上已经没有了**(前端不再有「自动」这
#     一档, 后端也不再把 0 当「按核数」)—— 想要满核就**明明白白填 8**;
#   · ⚠️ **这一段只动「下限」, 判定口径一个字没动**: 峰值 > `max_temp_c`
#     判 failed / 「死在加压途中 ⇒ 判 failed 不放行」那道兜底, 都照旧。
MAX_STRESS_THREADS = 8


def _parse_thread_request(requested) -> int:
    """页面/配置里填的线程数 → 整数(**非法值当 0**)。

    ⚠️ **解析只做一次, 两边共用** —— `_requested_threads`(算出真正压几核)
    和 `_floor_applied`(算「有没有被下限顶上去」)必须对同一个字符串给出
    同一个答案 ⇒ 各写一份 `int()` 的话, 将来改了一处忘了另一处 ⇒
    报告上说「没被夹」而实际压的是 1 核。
    """
    try:
        return int(requested)
    except (TypeError, ValueError):
        return 0


def _requested_threads(requested) -> int:
    """页面/配置里填的线程数 → **不考虑天花板**的目标线程数。

    · **下限 1**(2026-11-10 用户点名: 「cpu核数控制在最低是1,最高是8」):
      `0` / 负数 / 非法值 一律当 **1**;
    · 不能比核数还多;
    · 最少 1 线程 —— `stress-ng --cpu 0` 是非法参数。
    """
    req = _parse_thread_request(requested)
    ncpu = os.cpu_count() or 4
    if req <= 0:
        req = 1
    return max(1, min(req, ncpu))


def _floor_applied(requested) -> bool:
    """请求有没有被**下限 1 核**顶上去(填 0 / 负数 / 非法值)。

    为什么要单独问一句: `threads_capped` 只反映**上限**被夹 ⇒ 工人填 `0`
    (实际只压 1 核)时它仍是「否」⇒ 报告上写着「配置 0 / 实际 1」而**没有
    一句话解释这个 0 是怎么变成 1 的** —— 看着像老版本那个「0 = 自动 = 按
    核数」的残留。2026-11-10 起 `0` **不再**表示自动 ⇒ 得让这个夹取**看得见**。
    """
    return _parse_thread_request(requested) < 1


def _resolve_threads(requested) -> int:
    """真正加压的线程数 = 目标线程数 **再夹到 MAX_STRESS_THREADS**。

    为什么有天花板: 见上面 `MAX_STRESS_THREADS` 那段 —— 满核 8 线程会把
    **日常用得好好的板子**压死(误杀好板的代价高于漏判边缘板)。
    ⚠️ 「6 核稳 / 7 核死」是**短跑台阶**探针的结论, 不是应用连续加压
    那一轮的结论(2026-11-04 实测: 应用只跑 6 线程, 那块板子照样死了)。
    ⚠️ **2026-11-09 白天天花板降到 5**: 终端对照实验证明板子本身扛得住 6 核,
    死在应用这一侧(板子自己的桌面栈吃了 0.7~1.7 核)⇒ 5 线程 + 那点
    背景 = 6.7 核, 落在「刚验证过能活」的水位下面。
    ⚠️ **2026-11-09 晚又放回 8**: 5 核那个数**没被验证过**(「第二轮又挂」的
    变量是上一轮留下的热, 不是线程数)⇒ 按用户要求**放回满核当实验台**,
    让工人在页面上自己拧线程数找平衡点(见上面那段注释)。
    ⚠️ **2026-11-09 晚(更晚)默认值定在 4 核**(在 `config.yaml` 的
    `defaults.cpu.threads` 里)—— **天花板没动, 仍是 8**; 这个函数只管
    上限, 默认值不归它管。
    ⚠️ **2026-11-10 下界也定了 1**(用户: 「cpu核数控制在最低是1,最高是8」)
    —— 「0 = 自动 = 按核数」那条路**取消了**, `0`/负数/非法值一律当 **1**
    (见 `_requested_threads`); **上限没动, 仍是 8。**
    """
    return max(1, min(_requested_threads(requested), MAX_STRESS_THREADS))


def _ramp_threads(total: int, step: int, n_slices: Optional[int] = None) -> int:
    """第 `step` 片(从 0 数)用几个线程 —— **逐级往上爬**。

    1 → 2 → 3 → 4 → 8(封顶在 `total`)。理由见 `_LADDER` 上面那段。

    `n_slices` —— 这一轮**一共几片**。给了它, 台阶会**按片数压缩**:
    台阶再多也不能把**满载**挤没了 —— 短跑也得真的满载过一次,
    否则「板子在满载下会死」这件事根本测不出来 ⇒ 变成**假通过**。
    压缩时丢的是**中间**那几级(起点 1 线程和离天花板最近那级都留着)。

    `total <= 1` 时就 1 —— 板子只有一颗核的时候没什么可爬的。
    """
    total = max(1, int(total))
    if total == 1:
        return 1
    step = max(0, int(step))
    if n_slices is None or int(n_slices) <= 0:
        i = step
        v = _LADDER[i] if i < len(_LADDER) else _LADDER[-1]
        return min(total, v)
    # 天花板(= total)以下的几级
    below = [x for x in _LADDER if x < total]
    keep = max(1, int(n_slices) - 1)      # 至少给满载留一片
    while len(below) > keep:
        below.pop(len(below) // 2)       # 丢中间那级(1 和最接近天花板那级留着)
    if step < len(below):
        return below[step]
    return total


class _EvidenceLog:
    """「加压现场记录」—— 板子死在加压半路时, 这是**唯一**的证据。

    **为什么落文件而不是指标**: 指标是这一项**跑完**才落库的 —— 板子死在
    半路时那些数字跟着进程一起没了(2026-10-30 实测: CPU 加压**起手一秒**
    就被硬复位, 报告上只剩一句「服务重启, 本项没跑完」)。文件是**边跑边
    写**的 —— 板子死了, 已经写下去的行还在。

    每秒一行: 经过秒数 / 线程数 / 最热核区温度 / 全部核区温度 / 负载。

    写不了就当没有 —— 记录证据是**加分项**, 绝不能因为写不进去就把整个
    测试搞挂(那才是真事故)。
    """

    def __init__(self, run_id, peak_threads=None, background_pct=None,
                 load_mode="continuous"):
        self.path = None
        self._fh = None
        self.t0 = time.monotonic()
        self._rails: dict = {}
        self._last_fsync = 0.0
        if run_id is None:
            return
        try:
            p = cpu_evidence_path(run_id)
            p.parent.mkdir(parents=True, exist_ok=True)
            # "w" = 覆盖 —— 同一轮重跑(重测轮是新的 run_id, 不受影响)。
            self._fh = open(p, "w", encoding="utf-8")
            self.path = p
            # 文件头: 板子型号 / 起始温度 / 过热阈值 / 起始供电电压 ——
            # 这些是**每轮不变**的, 写在头上而不是每行重复一遍。
            try:
                self._fh.write(_format_evidence_header(peak_threads,
                                                       background_pct,
                                                       load_mode))
                self._fh.flush()
                self._fsync()      # 头必须**落盘**(见 _fsync 的说明)
            except Exception:
                pass        # 头写不出来就当没有头, 不影响正片
            # 头里那组**起始电压**就是后续比较的基准 —— 记下来, 这样
            # 「加压结束」那一行的 dV= 才是**整轮**的电压台阶
            # (加压窗口里一次都不读, 见 RAILS_SAMPLE_INTERVAL_SEC 那段)。
            # ⚠️ 这里用**缓存**读(不加 max_age=0): 头刚刚读过一遍,
            # 没必要再扫一次那 66 个 sysfs 文件。
            try:
                self._rails = dict(_read_rails_cached())
            except Exception:
                pass
        except Exception:
            # 建不出来就当没有这一份记录 —— 记录证据是**加分项**,
            # 绝不能因为写不进去就把整个测试搞挂。
            self._fh = None

    def _fsync(self) -> None:
        """把**已经交给内核**的内容**真正刷进存储** —— 不是只停在页缓存。

        ⚠️⚠️ **为什么必须 fsync, 光 flush 不够** (2026-11-02 13:50 实测踩过):
        板子是被**硬复位**打死的(掉电 / PMIC 复位)⇒ **内存里的页缓存整个
        丢失** ⇒ 只 `flush()` 到页缓存的证据**跟着板子一起没了**。
        实测: 板子上 `cpu_evidence_run3.txt` 是 **0 字节** —— 文件建出来了
        (元数据落盘了), 但一行数据都没落盘 ⇒ 报告里「加压现场记录」那一节
        **整个不见**, 恰恰是最该有证据的一次。
        ⇒ `flush()` 只把数据交给内核; **只有 `fsync()` 才把它写进存储**,
        硬复位才带不走它。

        fsync 在 SD/eMMC 上要几毫秒到几十毫秒 ⇒ **不能每行都做**(采样是
        2 秒一行, 会把采样拖慢)⇒ 采样行**最多
        EVIDENCE_FSYNC_INTERVAL_SEC 秒 fsync 一次**,
        而**「起跑」那一行**(`note=` 有值)与**文件头**必须**立刻 fsync**
        —— 那一行是「板子死在第几片」的唯一凭据, 丢不得。

        ⚠️⚠️ **为什么周期 fsync 要放到 10 秒**(2026-11-03 还原打包前的形态):
        这台板子的**根文件系统在 SD 卡上**(`/dev/mmcblk0p4`, ext4) ⇒
        一次 fsync = **一次真的落盘** = SD 卡一次写电流尖峰(几十到几百
        mA 量级)。打包之后(2026-11-02)周期 fsync 是**每 1 秒**一次, 也就是说
        **加压窗口里每秒往 SD 卡上砸一次写电流尖峰** —— 而那正是板子总电流
        最高、最吃不消的那几秒。**人工在终端跑老化的时候, 板上没有这个。**
        ⇒ 现在降到 10 秒一次: 代价是硬复位可能丢掉最后 10 秒的采样行
        (可接受 —— 「起跑」那一行是**立刻 fsync** 的, 「死在第几片」永远
        丢不了), 换来的是加压窗口里的 SD 写突发降到 1/10。

        ⚠️⚠️ **调用前必须先 `flush()`** —— `os.fsync()` 刷的是**文件描述符**,
        而 Python 自己那层缓冲区里的字节**还没进 fd** ⇒ 不先 flush 的话,
        fsync 刷的是一份**空的**文件(而 flush 那一行**必须留在调用点**,
        **别挪进这个函数** —— 挪进来就成了「这里会自动 flush」, 于是
        「调用点忘了 flush」这个错**没人看得出来**)。
        """
        if self._fh is None:
            return
        try:
            os.fsync(self._fh.fileno())
            self._last_fsync = time.monotonic()
        except Exception:
            # fsync 失败(比如这个文件系统不支持 / 文件已被关掉)⇒ 不影响正片。
            # ⚠️ 但**别把 flush 省掉** —— flush 至少保证数据交给内核,
            #    软重启(不是硬复位)时还能活下来。
            pass

    def write(self, threads: int, hot: Optional[float], zones: dict,
              load_pct: Optional[float], freq: Optional[float] = None,
              note: str = "", rails: Optional[dict] = None,
              bg_pct: Optional[float] = None) -> None:
        """记一行。`note` 用于「这一行是特殊事件」(如「片切换」)。"""
        if self._fh is None:
            return
        hot_s = f"{hot:.1f}" if hot is not None else "?"
        z = " ".join(f"{k}={v:.1f}" for k, v in sorted(zones.items()))
        try:
            line = (f"t=+{time.monotonic() - self.t0:.1f}s "
                    f"threads={threads} hot={hot_s}")
            if z:
                line += f" {z}"
            if load_pct is not None:
                line += f" load={load_pct:.1f}%"
            if bg_pct is not None:
                # 背景占用 —— **别的活**占的那部分(总占用减去加压线程自己).
                # 这一行是「板子死的时候板子上还有多少别的活」的凭据。
                line += f" bg={bg_pct:.1f}%"
            if freq is not None:
                line += f" freq={freq:.0f}MHz"
            line += self._rails_delta(rails)
            if note:
                line += f" {note}"
            self._fh.write(line + "\n")
            self._fh.flush()          # 板子随时可能死 —— 得**立刻**交出去
            # (flush 必须留在**这一行** —— 见 _fsync 的说明)
            # 「起跑」那一行**立刻 fsync**(它是「死在第几片」的唯一凭据);
            # 采样行最多 EVIDENCE_FSYNC_INTERVAL_SEC 秒一次(见 _fsync 的说明)。
            if note or (time.monotonic() - self._last_fsync) >= EVIDENCE_FSYNC_INTERVAL_SEC:
                self._fsync()
        except Exception:
            # **什么都别管** —— 记录证据是加分项, 写不进去就当没有。
            # 绝不能因为写记录失败把整个 CPU 项搞挂(那才是真事故)。
            pass

    def _rails_delta(self, rails: Optional[dict]) -> str:
        """电压台阶 —— **只记变了的那几根轨**。

        为什么只记变化的: 稳态时电压不动, 每次都把三十几根轨抄一遍,
        只是把文件撑大(这份记录整个进报告)。而**电压台阶跟负载台阶是不是
        同一瞬间**才是要看的 —— 变了才写出来, 一眼就看见。

        第一次采样没有「上一份」可比, 不写台阶(起始电压在文件头上)。
        """
        if not rails:
            return ""
        prev = self._rails
        self._rails = dict(rails)
        if not prev:
            return ""
        changed = [f"{k}:{prev.get(k, '?')}→{v}"
                   for k, v in sorted(rails.items()) if prev.get(k) != v]
        if not changed:
            return ""
        return " dV=" + ",".join(changed)

    def close(self) -> None:
        if self._fh is None:
            return
        try:
            # 收尾也 fsync 一次 —— 正常跑完的那一份, 同样要真的落盘
            # (报告是**纸面归档**, 读的是文件, 不是页缓存)。
            self._fh.flush()
            self._fsync()
        except Exception:
            pass
        try:
            self._fh.close()
        except OSError:
            pass
        self._fh = None


# ── 现场采样的密度 ────────────────────────────────────────────────────────
#
# ⚠️ **为什么是 2 秒而不是 200ms** (2026-11-03 还原打包前的形态):
#
# 200ms 是**打包之后**才有的密度, 理由是「板子死在起手一秒内, 1Hz 采不到
# 关键那一票」。但这个密度本身**就是「app 比人工老化更重」的一部分** ——
# 每 200ms 一次, 每次要读 /proc/stat + 5 个 thermal zone + 8 个 cpufreq,
# 外加**每 1 秒**一次 66 个 regulator sysfs 文件(实测 5.4~13.3ms 一遍)
# ⇒ 峰值那一段, **应用自己**就占着一颗核的一小截 —— 而那正是板子电流最高、
# 最吃不消的时候。**人工在终端跑老化的时候, 板上没有这些东西。**
#
# ⇒ 现在: 加压窗口里**只留 1Hz 的判温**(判定口径一个字没动, 见下),
#   证据记录改成 **2 秒一行** —— 死在哪一秒仍然看得见, 而采样本身
#   对板子的额外负担降到 1/10。
#
# ⚠️⚠️ **它只管记证据, 不参与判温** —— 判温仍然走 tick 的 1Hz。
# 改判温的采样率 = 改「合格/不合格」的判定口径(峰值采得更密, 峰值更高,
# 临界板子会从「过」变成「不过」)—— 那是**改产品行为**, 不是「加个记录」。
# 这两件事必须分开, 否则「加记录」会顺手把产线的判定也改了。
EVIDENCE_SAMPLE_INTERVAL_SEC = 2.0

# 证据文件**周期 fsync** 的间隔 —— 「起跑」/「结束」那种带 note 的行
# 永远**立刻** fsync, 这里管的是**中间的采样行**。
#
# ⚠️ **为什么是 10 秒而不是 1 秒** (2026-11-03 还原打包前的形态):
# 根文件系统在 **SD 卡**上 ⇒ fsync = 真的落盘 = 一次写电流尖峰。
# 1 秒一次 = 加压窗口里每秒砸一次写尖峰到正在吃紧的板子上;
# 人工在终端跑老化时板上没有这个东西。⇒ 降到 10 秒,
# 代价(硬复位可能丢最后 10 秒采样行)由「起跑行立刻 fsync」兜底。
EVIDENCE_FSYNC_INTERVAL_SEC = 10.0

# 供电轨 —— **加压窗口里一次都不读**(2026-11-03 还原打包前的形态)
#
# 为什么: `_read_rails()` 一次要读 **66 个 sysfs 文件**, 板子上实测
# **5.4~13.3ms 一遍** —— 那是**阻塞**在 asyncio 事件循环里的读写。
# 打包之后(2026-11-02)这个扫描是**每 1 秒一次**跑在加压窗口里的 ⇒
# 峰值那一段, 应用**自己**就占着一颗核的一小截 —— 而那正是板子电流最高、
# 最吃不消的时候。**人工在终端跑老化的时候, 板上没有这个东西。**
#
# ⇒ 现在只在**窗口外**读两次: **加压前**(文件头)和**加压后**(整轮跑完
#   那一次)。两次之间的电压台阶 = 整轮加压带来的**总变化** —— 比每秒
#   抄一遍三十几根轨更有用, 也更省。
RAILS_SAMPLE_INTERVAL_SEC = 1.0

# 供电轨的**共享缓存** —— 避免同一时刻重复扫那 66 个文件。
_RAILS_CACHE: dict = {"v": None, "at": 0.0}


def _read_rails_cached(max_age: Optional[float] = None) -> dict:
    """读供电轨 —— **带缓存**: 距上次读不到 `max_age` 秒就直接用旧的。

    ⚠️ **加压窗口里不许调用它**(2026-11-03): 扫 66 个 sysfs 文件要
    5.4~13.3ms, 那是**阻塞**在 asyncio 事件循环里的读写 ⇒ 每秒一次就
    是每秒十几毫秒卡在**板子电流最高的那一刻**上。
    ⇒ 加压窗口里**只**在两个时刻用: 文件头(加压前) 和 整轮跑完(加压后)。

    `max_age=0` = **强制重读**(文件头要的是"起始电压", 不能用旧的)。
    """
    if max_age is None:
        max_age = RAILS_SAMPLE_INTERVAL_SEC
    now = time.monotonic()
    cached = _RAILS_CACHE.get("v")
    if cached is not None and (now - _RAILS_CACHE.get("at", 0.0)) < max_age:
        return cached
    v = _read_rails()
    _RAILS_CACHE["v"] = v
    _RAILS_CACHE["at"] = now
    return v

# 背景负载 —— **板子上别的活**占掉的 CPU(2026-11-03 加)
#
# 为什么要有这个数 —— 2026-11-03 14:33 那次硬复位的账就是这么算出来的:
#   · 现场记录里**只有 1 个加压线程**的那一级, 板子的占用率就在
#     **22.7% ~ 93.2%** 之间乱跳 ⇒ 板子上**别的活**当时占着 0.8 ~ 6.4 核;
#   · 10-30 那次**活着**跑完 8 线程满载 48 秒的现场记录里, 同一级
#     只有 23.7% ~ 33.7% ⇒ 别的活只占 0.3 核, 而且**是平的**。
# ⇒ **同样是 8 线程, 板子的"总电流"差了好几倍** —— 死不死看的是**总**,
#   不是加压线程数。所以这个数必须**量出来、写进记录、报进报告**,
#   否则下一次死了还是只能猜。
BACKGROUND_WARN_PCT = 25.0


async def _measure_background_pct(sec: float = 0.6) -> Optional[float]:
    """板子上**别的活**占掉的 CPU 百分比(加压之前先量一次)。

    量法: 量两遍 `/proc/stat` 的 cpu 汇总行, 中间睡一小会儿, 相减 ——
    这跟 `_read_cpu_stat` 算负载是同一套口径(占用率 = busy Δ / total Δ)。

    ⚠️ **量的时候还没加压** —— 所以量到的**全是**背景(浏览器、代理、
    别的测试、别的进程)。这正是「同样的加压, 为什么这台机器死、那台不死」
    那个差额的来源。
    """
    a = _read_cpu_stat()
    if not a:
        return None
    try:
        await asyncio.sleep(sec)
    except asyncio.CancelledError:
        raise
    b = _read_cpu_stat()
    if not b:
        return None
    d_busy = b[0] - a[0]
    d_total = b[1] - a[1]
    if d_total <= 0:
        return None
    return d_busy * 100.0 / d_total


def _background_cores(pct: Optional[float]) -> Optional[float]:
    """背景占用百分比 → **核数**(8 核板子上 12.5% = 1 核)。"""
    if pct is None:
        return None
    ncpu = os.cpu_count() or 4
    return pct * ncpu / 100.0


async def _sample_evidence(evidence, threads: int, stop: asyncio.Event) -> None:
    """**独立的记录采样** —— 每 2 秒记一行现场(温度/频率/负载/各核区)。

    **为什么不在 tick 里采**: 见 `EVIDENCE_SAMPLE_INTERVAL_SEC` 上面那段。
    tick 是 1 秒一拍(那是**判温**的口径, 不能动), 而这一路**只管记录**。

    **写不进去就当没有** —— 同 `write()` 那条规矩: 记录是加分项, 采样出
    错也绝不能把整个 CPU 项搞挂(那才是真事故)。
    """
    prev = _read_cpu_stat()
    while True:
        try:
            stat_now = _read_cpu_stat()
            load_pct = None
            if prev and stat_now:
                d_busy = stat_now[0] - prev[0]
                d_total = stat_now[1] - prev[1]
                if d_total > 0:
                    load_pct = d_busy * 100.0 / d_total
            if stat_now:
                prev = stat_now
            zones = await _read_all_zones()
            hot = max(zones.values()) if zones else None
            # 背景占用 = 总占用 - 加压线程自己占的 ⇒ **别的活**占的那部分。
            # 算出来写进记录: 板子死的时候, 这一行直接告诉读者
            # 「板子上当时还有多少别的活在抢电」。
            bg_pct = None
            if load_pct is not None:
                bg_pct = max(0.0, load_pct - threads * 100.0 / (os.cpu_count() or 4))
            # ⚠️ **供电轨不在这里读**(2026-11-03 还原打包前的形态) ——
            # 扫 66 个 sysfs 文件要 5.4~13.3ms 的**阻塞**读写, 而这一行
            # 正好落在板子电流最高的那一刻上。窗口内的电压信息由
            # 「加压前 / 加压后」两次采样负责(见 run() 里那两处)。
            evidence.write(threads, hot, zones, load_pct,
                           freq=_read_cpu_freq(), rails=None,
                           bg_pct=bg_pct)
        except Exception:
            pass        # 采样是加分项 —— 出错就当这一行没有
        try:
            await asyncio.wait_for(stop.wait(),
                                   timeout=EVIDENCE_SAMPLE_INTERVAL_SEC)
            return         # 收到停止信号 ⇒ 收工
        except asyncio.TimeoutError:
            continue


def _read_cpu_freq() -> Optional[float]:
    """当前**最高**频率 (MHz)。

    **为什么要记它**: governor 是 `ondemand` 时, 负载一上来频率立刻往上跳
    ⇒ **电流的台阶跟负载的台阶是同一瞬间** —— 那正是「棕断 / PMIC 复位」
    最可能发力的时刻。只记温度看不出「电流突然上来了」, 把频率一起记下来
    才看得出那一瞬间板子在干什么。

    读不到就返回 None(别的内核没有 cpufreq) —— 那一行少个字段而已。
    """
    best = None
    try:
        paths = glob.glob(os.path.join(CPUFREQ_BASE,
                                       "cpu[0-9]*", "cpufreq",
                                       "scaling_cur_freq"))
    except OSError:
        return None
    for p in paths:
        try:
            with open(p) as f:
                khz = int(f.read().strip())
        except (OSError, ValueError):
            continue
        mhz = khz / 1000.0
        if best is None or mhz > best:
            best = mhz
    return best

# 温度从哪儿读 —— 做成模块级常量, 回归测试要能把它指到**假的**目录上去
# (真机上读的是 /sys/class/thermal)。
THERMAL_DIR = "/sys/class/thermal"

# 供电轨从哪儿读 —— 同理, 做成常量好让测试指到假目录
REGULATOR_DIR = "/sys/class/regulator"

# 调速信息(cpufreq)从哪儿读 —— 同理, 做成常量好让测试指到假目录
CPUFREQ_BASE = "/sys/devices/system/cpu"


def _read_model() -> str:
    """板子型号 —— `/sys/firmware/devicetree/base/model`(这块板子上是 A100)。

    记进现场记录的文件头: 死因分析经常要对照「哪块板子」的日志,
    光有一堆温度和频率, 不知道是谁的。
    """
    for p in ("/sys/firmware/devicetree/base/model",
              "/sys/devices/virtual/dmi/id/product_name"):
        try:
            with open(p, "rb") as f:
                name = f.read().decode("utf-8", "replace").strip("\0\n ")
            if name:
                return name
        except OSError:
            continue
    return ""


def _read_rails() -> dict:
    """{供电轨名字: 电压 mV} —— 全部的轨, 不挑。

    ⚠️ `microvolts` 是**调节器当前设定的电压**, 不是万用表读数 ——
    2026-11-02 老板子实测: 小核稳定在 1416MHz 时, 那条轨在 900~1150mV
    之间乱跳 ⇒ 它跟频率**不是**一一对应的。记它不是为了当万用表用, 是为了
    看**电压台阶**跟**负载台阶**是不是同一瞬间(频率台阶已经在记了)。

    为什么要**全部**的轨而不是只记 CPU 那几根: 厂商那个 BT 固件失败循环
    每 8.25 s 切一次 **WLAN/BT 共用的那根轨**(axp2202-aldo3, 3300mV) ——
    只记 CPU 的轨, 就看不见它被切。
    """
    out: dict = {}
    try:
        regs = sorted(glob.glob(os.path.join(REGULATOR_DIR, "regulator.*")))
    except OSError:
        return out
    for r in regs:
        try:
            with open(os.path.join(r, "name")) as f:
                name = f.read().strip()
            with open(os.path.join(r, "microvolts")) as f:
                uv = int(f.read().strip())
        except (OSError, ValueError):
            continue
        if not name or uv <= 0:
            continue
        out[name] = uv // 1000
    return out


def _read_thermal_config() -> dict:
    """{zone 类型: {"cur": 起始温度°C, "trips": [(类型, 温度°C), ...]}}。

    **过热阈值要记下来** —— 板子热不热是拿它比的:
    2026-11-02 新板实测 cpu 大核区 passive=70/90℃ critical=110℃,
    而板子死在 **57.7℃** —— 没有阈值, 读者看着「57.7」根本不知道
    这算不算热, 也不知道离 thermal 停车线还差多远。
    """
    out: dict = {}
    try:
        zones = sorted(os.listdir(THERMAL_DIR))
    except OSError:
        return out
    for z in zones:
        if not z.startswith("thermal_zone"):
            continue
        d = os.path.join(THERMAL_DIR, z)
        try:
            with open(os.path.join(d, "type")) as f:
                ztype = f.read().strip().lower()
            with open(os.path.join(d, "temp")) as f:
                cur = int(f.read().strip()) / 1000.0
        except (OSError, ValueError):
            continue
        trips = []
        for tp in sorted(glob.glob(os.path.join(d, "trip_point_*_temp"))):
            name = os.path.basename(tp)[:-len("_temp")]
            try:
                with open(tp) as f:
                    val = int(f.read().strip()) / 1000.0
                with open(os.path.join(d, name + "_type")) as f:
                    ttype = f.read().strip()
            except (OSError, ValueError):
                continue
            trips.append((ttype, val))
        out[ztype] = {"cur": cur, "trips": trips}
    return out


def _read_cpufreq_info() -> str:
    """各核的**调速策略**(governor) + 频率上限 —— 记进文件头。

    **为什么记它**: 2026-11-02 新板实测, 现场记录里频率**一上来就贴着上限**
    (t=+0.4s 就已经 1800MHz, 而那会儿负载才 66%; 后面负载掉到 22% 频率
    还是 1800MHz) ⇒ 板子的调速策略**根本没往下调**(`performance` 策略,
    或者 `ondemand` 但采样间隔太长)。

    这跟「电流台阶跟负载台阶同一瞬间」直接相关: 板子**一直**满电流,
    加压那一下再叠一层 ⇒ 供电扛不住 ⇒ 硬复位。**没有这一栏, 读者只能
    猜板子是不是一直满频率跑。**
    """
    bits = []
    for p in sorted(glob.glob(os.path.join(CPUFREQ_BASE, "cpu[0-9]*",
                                           "cpufreq"))):
        gov = ""
        try:
            with open(os.path.join(p, "scaling_governor")) as f:
                gov = f.read().strip()
        except (OSError, ValueError):
            pass
        mx = ""
        try:
            with open(os.path.join(p, "cpuinfo_max_freq")) as f:
                raw = f.read().strip()
            mx = f"{int(raw) // 1000}MHz"
        except (OSError, ValueError):
            pass
        if not gov and not mx:
            continue
        name = os.path.basename(os.path.dirname(p))
        bits.append(f"{name}: {gov or '?'}"
                    + (f"(上限 {mx})" if mx else ""))
    return " | ".join(bits)


def _read_board_state() -> dict:
    """板子**进这一项时**的状态 —— 核数 / 开机时长 / 负载均值 / 可用内存。

    ⚠️ **为什么要有这一行** (2026-11-02 现场逼出来的):
    板子 A10000000000010 **单独**跑 CPU 加压扛得住, 整轮老化却**每次**死在
    CPU 项 ⇒ 触发条件是「**进这一项时板子已经是什么状态**」。
    而现场拿到的**起跑温度是凉的**(46.9℃, GPU 核区才 44.2℃)⇒ **「热」
    这条线出局** ⇒ 剩下的嫌疑是「板子已经被前面的项目连续压了多久 / 系统
    里还堆着多少负载」。
    ⇒ 这几个数就是**直接刻画那个状态**的:
      · **核数** —— 加压台阶封顶在几线程全看它(而且它是「8 核还是 4 核」
        这种基础事实, 不该靠记忆);
      · **开机时长** —— 板子已经连续跑了多久;
      · **1/5/15 分钟负载均值** —— **累积压力**的直接度量(5 分钟那个正好
        盖住前面的那几个项);
      · **可用内存** —— 前面的项(memtester / fio)留了多少占用。
    **不记下来, 下一轮还是只能猜。**
    """
    st: dict = {}
    st["cores"] = os.cpu_count() or 0
    try:
        with open("/proc/uptime") as f:
            st["uptime_sec"] = float(f.read().split()[0])
    except (OSError, ValueError, IndexError):
        pass
    try:
        with open("/proc/loadavg") as f:
            st["load"] = [float(x) for x in f.read().split()[:3]]
    except (OSError, ValueError, IndexError):
        pass
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    st["mem_avail_mb"] = int(line.split()[1]) // 1024
                    break
    except (OSError, ValueError, IndexError):
        pass
    return st


def _format_board_state(st: dict) -> str:
    """把 `_read_board_state()` 的结果排成一行能直接读的话。"""
    bits = []
    if st.get("cores"):
        bits.append(f"核数 {st['cores']}")
    up = st.get("uptime_sec")
    if up is not None:
        bits.append(f"开机 {int(up // 60)}分{int(up % 60)}秒")
    ld = st.get("load")
    if ld:
        bits.append("负载均值(1/5/15分) " + "/".join(f"{x:.2f}" for x in ld))
    mb = st.get("mem_avail_mb")
    if mb is not None:
        bits.append(f"可用内存 {mb}MB")
    return " | ".join(bits)


def _format_evidence_header(peak_threads: Optional[int] = None,
                            background_pct: Optional[float] = None,
                            load_mode: str = "continuous") -> str:
    """现场记录文件的**头** —— 板子型号 / 起始温度 / 过热阈值 / 起始供电电压
    / 调速策略 / **进这一项时的板子状态** / **本轮加压峰值** /
    **起跑时板子上的背景负载**。

    这些都是**每轮不变**的东西(或者只变一次), 写在文件头上而不是每行
    重复一遍 —— 每行 200ms 一行, 重复一遍既占地方又没人看。

    `peak_threads` —— 这一轮加压封顶在几个线程。**必须写进头里**: 板子死在
    加压半路时, 报告上唯一能回答「它死的时候压到了几线程」的, 就是这个头
    (每一行自己也带 threads=, 但头里这个是**整轮的封顶值**, 两回事)。
    2026-11-03 起这一项的峰值可以在页面上调(线程数旋钮), 不记下来就没法
    区分「这一轮本来只压到 2 线程」和「这一轮本来要压满、死在半路」。

    ⚠️ `load_mode` —— **头里那句话必须跟这一轮真跑的形态一致**
    (2026-11-04 修): 原来这句话**写死**成「阶梯从 1 逐级爬到它」, 而
    默认形态是**连续加压**、**根本没有阶梯** ⇒ 产线看报告会被「阶梯」
    两个字带偏去查爬坡逻辑, 而报告里压根没有那回事。
    ⇒ 头里按实际形态出文案: 连续 ⇒「连续加压, 一个进程跑满整轮」;
      分片 ⇒「阶梯从 1 逐级爬到它」。

    `background_pct` —— **起跑时板子上别的活占掉的 CPU 百分比**(加压之前
    量的)。**必须写进头里**: 板子的功耗是**总**功耗 —— 加压线程 + 别的活
    一起算。少了这一栏, 报告上就只能说「板子死在 8 线程」, 而真正该问的
    是「8 线程 + 板子上还有 3 核的别的活, 总电流是不是超了」。
    """
    model = _read_model()
    cfg = _read_thermal_config()
    rails = _read_rails_cached(max_age=0.0)   # 头要的是**起始**电压 ⇒ 强制重读
    lines = [f"# 加压现场记录 —— 板子 {model or '型号未知'}"
             f"   采样间隔 {EVIDENCE_SAMPLE_INTERVAL_SEC:g}s"]
    if peak_threads:
        shape = ("阶梯从 1 逐级爬到它" if load_mode == "sliced"
                 else "连续加压, 一个进程跑满整轮")
        lines.append(f"# 本轮加压峰值: {int(peak_threads)} 线程"
                     f"({shape}; 每一行自带 threads= 是当时那一片的值)")
    if background_pct is not None:
        lines.append(
            f"# 起跑时板子背景负载: {background_pct:.1f}% "
            f"(≈{_background_cores(background_pct):.1f} 核) —— 这是**别的活**"
            f"占的(浏览器/代理/别的进程), 不算加压线程数, 但**照样算**在"
            f"板子的功耗里 ⇒ 同样的加压, 板子上越忙越容易掉电复位")
    if cfg:
        temps = " ".join(f"{k}={v['cur']:.1f}℃"
                         for k, v in sorted(cfg.items()))
        lines.append(f"# 起始温度: {temps}")
        trips = []
        for k, v in sorted(cfg.items()):
            if v["trips"]:
                t = " ".join(f"{tt}@{tv:g}℃" for tt, tv in v["trips"])
                trips.append(f"{k}: {t}")
        if trips:
            lines.append("# 过热阈值(内核 thermal 驱动): " + " | ".join(trips))
    if rails:
        lines.append("# 起始供电轨(mV): "
                     + " ".join(f"{k}={v}" for k, v in sorted(rails.items())))
    gov = _read_cpufreq_info()
    if gov:
        lines.append("# 调速策略(governor/频率上限): " + gov)
    state = _format_board_state(_read_board_state())
    if state:
        lines.append("# 进这一项时的板子状态: " + state)
    # ⚠️ **开机 ID 必须写进头里** (2026-11-11 加):
    #   「加压起手了、没跑完」有两种成因 —— **板子死了**(不合格), 或者
    #   **服务自己没了**(工人重启服务 / 事件循环冻死 ⇒ 板子好好的,
    #   **冤枉好板**)。光看这份记录分不出来 ⇒ 记下**当时的开机 ID**,
    #   收尾时对一下现在的开机 ID: **一样** ⇒ 板子压根没重启过 ⇒
    #   是服务自己没了 ⇒ 不赖板子。
    #   (开机 ID **跟时钟无关** —— 这台板子的墙钟会倒退几十天。)
    boot_id = current_boot_id()
    if boot_id:
        lines.append("# 本次开机 ID: " + boot_id)
    return "\n".join(lines) + "\n"


async def _spawn_proc(cmd):
    """起一个子进程 —— **单独拎成一个函数**, 好让回归测试把它换成假的。

    ⚠️⚠️ **为什么必须单独拎出来**(2026-11-03 14:59 实测踩过):
    回归测试 `tests/test_cpu_evidence.py` 把 `run_proc_with_progress`(读
    stdout、推进度)换成了假的, **但进程是真的起过的** —— `create_subprocess_exec`
    照样把 `sudo -n /usr/bin/stress-ng --cpu 8 --timeout 5.0s` 拉起来了。
    而假的 progress 不等进程退 ⇒ **一片接一片地叠**, 八个片就是八个
    8 线程 stress-ng 同时在板上跑 ⇒ **回归测试自己就把板子压到掉电复位**
    (2026-11-03 14:59 实测: 跑完回归, 板子当场硬复位)。

    ⇒ 起进程这一步**必须可替换**: 测试里换成假的 ⇒ 回归测试**再也不碰真硬件**。
    """
    return await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT
    )


def _slice_stopped_reason(reason: str, label: str) -> str:
    """一片加压**没跑完**(被中止)—— 说清是**谁**叫停的。

    ⚠️ **2026-11-12 补**: 以前这条只置 `status = "stopped"`, **一个字的原因
    都不写** ⇒ 报告的「未测项明细」里只剩一个光秃秃的「stopped」
    (模板兜底印「(未记录原因)」)⇒ 工人不知道是自己按了停止, 还是工具卡住了。

    ⚠️ 两种「停下来」性质完全不同, **不许混成一句**:
      · `cancelled`  —— **工人自己**按了「停止」⇒ 没跑完 ≠ 不合格;
      · `hard_limit` —— **我们自己的硬上限**到了(挂钟上限 + 30s)⇒ 工具/环境
        没让这一项跑完, 同样**不是**板子的结论。
    """
    if reason == "cancelled":
        return (f"加压被中止 —— 工人中途按了「停止」, 这一项没跑完"
                f"({label})。没跑完 ≠ 不合格, 补上料/复位之后可以重测。")
    if reason == "hard_limit":
        return (f"加压跑到了我们自己的硬上限还没结束, 这一项被收掉了"
                f"({label})。这一项**没测成** —— 不是「板子不合格」。")
    return (f"加压被中止, 这一项没跑完({label})。没跑完 ≠ 不合格, 可以重测。")


@register
class CPUTest(TestCase):
    name = "cpu"
    display_name = "CPU 压力"
    description = "使用 stress-ng 对所有 CPU 核心施加负载,监测温度和性能"

    def expected_seconds(self, test_config: dict) -> Optional[float]:
        times = max(1, int(test_config.get("times", 1)))
        duration = max(5, int(test_config.get("duration_sec", 60)))
        return times * duration

    async def run(self, ctx: TestContext) -> TestResult:
        result = TestResult()
        t_cfg = ctx.test_config
        times = max(1, int(t_cfg.get("times", 1)))
        duration = max(5, int(t_cfg.get("duration_sec", 60)))
        threads = _resolve_threads(t_cfg.get("threads", 0))
        # 这一轮加压封顶在几线程 —— 记进指标和现场记录的头。
        # ⚠️ 为什么必须记: 板子死在加压半路时, 报告上唯一能回答「它死的时候
        # 压到了几线程」的凭据就是这个数(现场记录每一行也带 threads=,
        # 但那是当时那一片; 头里这个是**整轮封顶值**)。
        # 2026-11-03 起峰值可以在页面上调(「线程数」旋钮), 不记下来就分不清
        # 「这一轮本来就只压到 2 线程」和「这一轮本来要压满、死在半路」。
        try:
            peak_threads_configured = int(t_cfg.get("threads", 0))
        except (TypeError, ValueError):
            peak_threads_configured = 0
        # 「要几核」和「实际压几核」**分两个数记**: 天花板夹过以后这两个
        # 会不一样(填 8 → 实际 6)⇒ 报告上要能看出「这一轮只压到 6 核」
        # 是**政策**定的, 不是这台板子只有 6 核。
        want_threads = _requested_threads(t_cfg.get("threads", 0))
        threads_capped = (threads != want_threads)
        # ⚠️ `threads_capped` 的判据是「**夹完的核数 > 加压上限**」——
        #   `_requested_threads` 已经把请求夹到**核数**了 ⇒ 8 核板子上
        #   want ≤ 8 = MAX ⇒ **capped 恒为「否」** ⇒ 它回答的是
        #   「配置值被改了没有」, **不是**「这一轮是不是满核」。
        #   「这一轮压在满核上」由日志里另一条 `threads >= 上限` 那句管。
        # 有没有被**下限 1 核**顶上去(填 0 / 负数 / 非法值)——
        # `threads_capped` 只反映**上限**, 所以这一句得单独问。
        threads_floored = _floor_applied(t_cfg.get("threads", 0))
        max_temp = float(t_cfg.get("max_temp_c", 85))
        # ── 热保护的两个旋钮 (§6.2) ──
        # ── 加压形态: **连续跑**(默认) 还是 **分片**(显式配了 slice_sec) ──
        #
        # ⚠️ **为什么默认是「连续跑」**(2026-11-03 还原打包前的形态):
        # 打包之后 CPU 项变成了「12 片 + 逐级加压 1→2→3→4→8」⇒ 一轮加压里
        # **stress-ng 进程被反复起了 12 次**, 每起一次就是一次电流台阶;
        # 而打包**之前**是**一次连续**的 stress-ng 跑满整轮时长 —— 跟产线上
        # 人工在终端跑老化的形态**完全一样**。
        # ⇒ 现在默认回到那个形态: 一轮 = **一个**进程跑满 duration 秒。
        #
        # **分片仍然保留**(显式配 `slice_sec` 才有): 它是为了「不用特权
        # kill 就能中途收手」(root 起的进程, 普通用户对它发信号必然 EPERM,
        # §11.2) —— 代价是反复起进程。需要细粒度热保护时再显式配上。
        _slice_raw = t_cfg.get("slice_sec")
        sliced = _slice_raw is not None
        slice_sec = max(1.0, float(_slice_raw)) if sliced else 0.0
        abort_temp = float(t_cfg.get("abort_temp_c", DEFAULT_ABORT_TEMP_C))
        if abort_temp <= max_temp:
            # 提前收手的线必须**高于**判定线 —— 否则「还没到判定温度就先收工」,
            # 峰值永远测不到, failed 就没依据了。
            abort_temp = max_temp + 5.0
        all_passed = True
        # 跨轮聚合用的收集桶 (§6.2 的 `peak_temp_c` / `avg_load_pct` 是
        # **整项**的指标, 不是某一轮的 —— 报告上要回答的是"这台机器这轮
        # 整体什么水平", 而不是"第 2 轮那一瞬间多少度")。
        peak_temps: list = []
        load_pcts: list = []
        thermal_abort = False
        abort_reason = ""

        # ── 起跑前先量一次「板子上别的活」(2026-11-03 加) ──
        #
        # **为什么**: 板子的功耗是**总**功耗 —— 加压线程 + 板子上别的活
        # 一起算。2026-11-03 14:33 那次硬复位就是这么算出来的:
        # 只有 1 个加压线程时板子的占用率就在 22.7% ~ 93.2% 之间乱跳
        # (别的活占着 0.8 ~ 6.4 核), 而 10-30 活着跑完满载 48 秒的那次,
        # 同一级只有 23.7% ~ 33.7% (别的活占 0.3 核, 而且是平的)。
        # ⇒ **同样是 8 线程, 总电流差了好几倍** —— 死不死看的是**总**。
        # 这个数必须量出来: 不量, 下一次死了还是只能猜「板子扛不住 8 线程」。
        background_pct = await _measure_background_pct()
        background_cores = _background_cores(background_pct)
        if background_pct is not None:
            result.metrics["background_load_pct"] = round(background_pct, 1)
            result.metrics["background_cores"] = round(background_cores, 2)
            await ctx.on_log(
                f"[CPU] 起跑时板子背景负载: {background_pct:.1f}% "
                f"(≈{background_cores:.1f} 核 —— 别的活占的, 不算加压)")
            if background_pct >= BACKGROUND_WARN_PCT:
                # 说出来: 这一轮的结论**带着这个前提**才成立 ——
                # 「板子扛不住 8 线程」和「板子上还有 3 核的别的活时扛不住
                # 8 线程」是两回事, 前者是缺陷, 后者是**这台机器当时太忙**。
                result.metrics["background_high"] = "是"
                await ctx.on_log(
                    f"[CPU] ⚠️ 起跑时板子上还有 ≈{background_cores:.1f} 核的"
                    f"别的活 ⇒ **峰值总功耗比实验室高** ⇒ 这一轮"
                    f"「扛不住 N 线程」很可能不是板子自己的天花板, "
                    f"而是这台机器当时太忙")

        # ── 「加压现场记录」文件 ──
        #
        # **边跑边写**, 每秒一行。板子死在加压半路时(2026-10-30 三次实测
        # 全死在起手一秒内), 这一项的指标**一条都落不了库** —— 指标是跑完
        # 才写的, 进程跟板子一起没了 ⇒ 报告上只剩「服务重启, 本项没跑完」。
        # 落文件的这些行还在 ⇒ 下次开报告就知道它死在第几秒、几线程、
        # 多少度、负载多少。
        evidence = _EvidenceLog(getattr(ctx, "run_id", None),
                                peak_threads=threads,
                                background_pct=background_pct,
                                load_mode="sliced" if sliced else "continuous")
        if evidence.path is not None:
            await ctx.on_log(f"[CPU] 加压现场记录: {evidence.path}")

        # §11.1 find_tool 找二进制; §11.2 命令级 sudo 取 root
        stress_bin = find_tool("stress-ng")
        if not stress_bin:
            result.status = "error"
            result.error = tool_missing_reason(
                "stress-ng", deps_fix_hint(),
                note="PATH 不含 /sbin, 或这台机器上压根没装这个工具")
            await ctx.on_log(f"[CPU] ✗ {result.error}")
            evidence.close()
            return result
        await ctx.on_log(f"[CPU] 工具: {stress_bin}")
        if not sudo_hint_if_unavailable(result, "CPU 压力"):
            await ctx.on_log(f"[CPU] ✗ {result.error}")
            evidence.close()
            return result

        await ctx.on_log(
            f"[CPU] 启动: {times} 次 × {duration}s, 线程数={threads}, "
            f"温度上限={max_temp}°C, 提前收手={abort_temp}°C, "
            + (f"分片={slice_sec}s/片" if sliced
               else "连续加压(一个进程跑满整轮)")
        )
        if threads_capped:
            await ctx.on_log(
                f"[CPU] ⚠️ 线程数被**加压上限 {MAX_STRESS_THREADS} 核**夹住: "
                f"要 {want_threads} 核, 实际压 {threads} 核 —— 高核是**过应力"
                f"那一档**(2026-11-04 实测有板子在加压途中**硬复位**, 死因到"
                f"现在还没查清), 误杀好板的代价高于漏判边缘板")
        # ⚠️⚠️ **2026-11-10 加**: 「这一轮压在满核上」的提醒。
        #   ⚠️ 上面那条**在这台板子上永远走不到** —— `threads_capped` 的判据
        #   是「夹完的上限 > 加压上限」, 而 `_requested_threads` 先把请求夹到
        #   **核数** ⇒ 8 核板子上 want ≤ 8 = MAX ⇒ **不可能 > 8**
        #   ⇒ capped 恒为「否」⇒ 那句「满核会压死板子」的提醒**从来没响过**。
        #   ⇒ 真正该提醒的是「**这一轮实际压到了满核**」(拧到 8 就是满核,
        #     不管是自己填的还是被夹出来的)⇒ 判据改成 `threads >= 上限`。
        #   ⇒ 口径跟打包脚本那条警告**一致**(它也是看「用户拧得到几核」,
        #     不看出厂默认 —— 默认改成 4 之后, 照看出厂默认就永远不响了)。
        elif threads >= MAX_STRESS_THREADS:
            await ctx.on_log(
                f"[CPU] ⚠️ 这一轮压的是**满核 {threads} 核** = **过应力那一档** —— "
                f"2026-11-04 实测有板子在加压途中**硬复位**(死因还没查清; 满核"
                f"的温升也最大)⇒ **开跑前请把页面上的「线程数」调低**(出厂默认 4 核)")
        if threads_floored:
            await ctx.on_log(
                f"[CPU] ⚠️ 线程数被**下限 1 核**夹住: 页面填的是 "
                f"{t_cfg.get('threads', 0)!r}, 实际压 {threads} 核 —— "
                f"2026-11-10 起 0 不再表示「自动 = 按核数」, "
                f"想压满请明明白白填 8")
        result.metrics["abort_temp_c"] = abort_temp
        result.metrics["slice_sec"] = slice_sec
        result.metrics["load_mode"] = "sliced" if sliced else "continuous"
        # 本轮加压封顶在几线程(配置的原始值 + 实际封顶值)——
        # 报告上要能回答「这一轮到底压到了几线程」。
        result.metrics["peak_threads"] = threads
        result.metrics["peak_threads_configured"] = peak_threads_configured
        # 加压上限本身也进指标: 读者看到「峰值 6 核」时要知道 **6 是政策
        # 给的封顶**, 不是这台板子只有 6 核。
        result.metrics["max_stress_threads"] = MAX_STRESS_THREADS
        if threads_capped:
            result.metrics["threads_capped"] = "是"
        # 「被下限夹住」也得看得见 —— 否则报告上只有一句「配置 0 / 实际 1」,
        # 读的人会以为 0 还是老版本那个「自动 = 按核数」。
        if threads_floored:
            result.metrics["threads_floored"] = "是"

        # ── 起跑前的温度底 ──
        #
        # 板子本来就是热的(上一项刚烤过 / 风扇不转 / 工位太热)就别再压 ——
        # 硬压下去的结果不是「判不合格」, 是**内核在 110℃ 直接重启板子**,
        # 整轮连同日志一起消失, 什么结论都没有。
        temp0 = await _read_cpu_temp()
        if temp0 is not None:
            result.metrics["start_temp_c"] = round(temp0, 1)
            await ctx.on_log(f"[CPU] 起跑前温度: {temp0:.1f}°C")
            if temp0 >= abort_temp:
                result.status = "failed"
                result.error = (
                    f"起跑前 CPU 就已经 {temp0:.1f}°C (≥ 提前收手线 "
                    f"{abort_temp:.0f}°C) —— 没有加压, 板子自己就烤着。"
                    f"散热不合格(内核 critical trip 是 110℃, 硬压下去板子会直接重启)"
                )
                result.metrics["thermal_abort"] = "是"
                result.metrics["peak_temp_c"] = round(temp0, 1)
                peak_temps.append(temp0)
                await ctx.on_log(f"[CPU] ❌ {result.error}")
                await ctx.on_progress(100, "起跑前超温, 未执行")
                evidence.write(0, temp0, await _read_all_zones(), None,
                               note="起跑前就超温, 未加压")
                evidence.close()
                return result

        await ctx.on_progress(0, "准备开始")

        for i in range(times):
            if await ctx.is_cancelled():
                await ctx.on_log("[CPU] 用户取消")
                result.status = "stopped"
                all_passed = False
                break

            round_idx = i + 1
            await ctx.on_log(f"[CPU] 第 {round_idx}/{times} 轮")
            await ctx.on_progress(int((i / times) * 100), f"第 {round_idx} 轮")

            # ── 分片跑 ──
            #
            # 一片 stress-ng 跑 slice_len 秒, 跑完**自己退**, 我们趁这个
            # 间隙查温度 —— 到了线就**不再起下一片**。
            #
            # 为什么不用「跑到一半 kill 掉」: 那是 root 起的进程, 服务(普通
            # 用户)对它发信号必然 EPERM(§11.2); 分片**不需要任何特权
            # kill** —— 上一片已经自己退出了。
            n_slices = (max(1, int(round(duration / slice_sec))) if sliced
                        else 1)
            slice_len = duration / n_slices
            round_peak = 0.0
            round_rc = 0
            round_text = ""
            slices_done = 0          # 真正跑起来的片数 —— 一片都没跑成时
                                     # **不许**把 exit_code / 峰值记成 0:
                                     # 那看着像「跑完了, 温度 0℃」, 其实是没跑
            stat0 = _read_cpu_stat()
            for s in range(n_slices):
                # ── 逐级加压, **别一上来就满载** ──
                #
                # 满载那一瞬间的电流台阶是这类板子最容易出事的瞬间(实测新板
                # 子「一启动就重启」—— 2026-10-30 三次实测, 全都死在 CPU 项
                # **起手一秒内**: 第一次一把满载 8 线程, 后两次缓启动到 4 线程
                # 也一样死)。所以现在是 1 → 2 → 3 → 4 → 8 **逐级**爬, 每级之间
                # 查一次温度 —— 板子在哪一级扛不住, 那一级就是它的天花板。
                # `n_slices` 传进去: 片数不够时台阶会**压缩**, 但**满载那一片
                # 一定要留着**(短跑也得真的满载过一次 ⇒ 别让板子假通过)。
                # 连续模式: **直接上满载**, 不爬台阶(打包前就是这样的);
                # 分片模式: 逐级爬(理由见 `_ramp_threads` 上面那段)。
                this_threads = (_ramp_threads(threads, s, n_slices) if sliced
                                else threads)
                pct_base = (i + s / n_slices) / times * 100
                pct_span = 100.0 / times / n_slices
                if sliced:
                    label = (f"第 {s + 1}/{n_slices} 片({this_threads} 线程"
                             + (", 缓启动" if this_threads < threads else "")
                             + ")")
                else:
                    label = f"连续加压 {slice_len:.0f}s({threads} 线程)"
                out = await self._run_slice(
                    ctx, stress_bin, this_threads, slice_len, max_temp,
                    pct_base, pct_span, label, evidence)

                if out["error"]:
                    result.error = out["error"]
                    result.status = "error"
                    all_passed = False
                    break

                round_rc = out["rc"]
                round_text = out["text"]
                round_peak = max(round_peak, out["peak_temp"])
                slices_done += 1
                await ctx.on_log(
                    f"[CPU]   {label} 跑完 · {slice_len:.0f}s · "
                    f"峰值 {out['peak_temp']:.1f}°C · exit {out['rc']}"
                )

                # ── 热保护: 片与片之间查温度 ──
                #
                # ⚠️ **看峰值, 不只看最后一次读数** (2026-11-04 修): 板子冲到
                # 96℃ 又落回 88℃ 时, 只看最后一次读数会以为「还没到收手线」
                # ⇒ 又起一片 ⇒ 更热。**峰值才是这块板子真达到过的温度**。
                temp_now = out["temp"]
                if temp_now is None:
                    temp_now = await _read_cpu_temp()
                hot = max([t for t in (temp_now, out["peak_temp"])
                           if t is not None], default=None)
                if hot is not None and hot >= abort_temp:
                    thermal_abort = True
                    abort_reason = (
                        f"温度 {hot:.1f}°C ≥ 提前收手线 {abort_temp:.0f}°C "
                        f"(内核的 critical trip 是 110℃, 再压下去板子会直接重启)"
                    )
                    await ctx.on_log(f"[CPU] ❌ {abort_reason} —— 立刻收手, 不再起下一片")
                    all_passed = False
                    break

                if out["killed"] or result.status == "stopped":
                    # 不用裸 terminate/kill —— 子进程是 sudo 起的 root 进程,
                    # 普通用户对它发信号必然 EPERM。见 kill_proc。
                    result.status = "stopped"
                    result.error = _slice_stopped_reason(out.get("reason", ""),
                                                        label)
                    all_passed = False
                    break
                if round_rc != 0:
                    # ⚠️ **2026-11-12 补**: 以前这里**只打了一行日志**, `result.error`
                    #   是空的 ⇒ 落到下面的 `result.status = "failed"` ⇒ 报告上
                    #   这一项是「不合格」+ **一个原因都没有**(报告模板只能印
                    #   「(未记录原因 —— 属实现缺陷)」)⇒ 工人看到「CPU 不合格」
                    #   却不知道是**板子扛不住**还是**工具自己挂了**。
                    #   ⇒ 现在把**退出码翻译成人话**写进原因框, 并把原始输出
                    #   的尾巴附上(排障的人要看那几行)。
                    result.error = tool_exit_reason(
                        round_rc, tool="stress-ng", stage=label,
                        log_tail=round_text,
                        extra=("这一轮加压**没跑完** ⇒ 按 §6.2「通过 = 退出码 0 "
                               "且峰值温度 ≤ 判定线」, 这一项**判不合格**。"
                               "⚠️ 退出码不等于板子坏了 —— 请先看下面这行原始"
                               "输出, 再决定是返修还是重测。"))
                    await ctx.on_log(f"[CPU] ❌ stress-ng 返回非 0: {round_rc}")
                    await ctx.on_log(f"[CPU] ❌ {result.error}")
                    all_passed = False
                    break

            # ── 加压**之后**再读一次供电轨 ──
            #
            # 加压窗口里**一次都不读**(2026-11-03 还原打包前的形态) ——
            # 扫 66 个 sysfs 文件是 5.4~13.3ms 的**阻塞**读写, 而窗口里
            # 正是板子电流最高、最吃不消的那一刻。
            # ⇒ 窗口外读两次: 文件头(加压前) + 这里(加压后) ⇒ 两点之间
            # 的电压台阶 = **整轮加压**带来的总变化, 比每秒钟抄一遍
            # 三十几根轨更有用, 也更省。
            try:
                rails_after = _read_rails_cached(max_age=0.0)
            except Exception:
                rails_after = None
            if evidence is not None:
                z_end = await _read_all_zones()
                evidence.write(threads,
                               max(z_end.values()) if z_end else None,
                               z_end, None, freq=_read_cpu_freq(),
                               rails=rails_after, note="加压结束")

            # ── 这一轮结束: 记指标 ──
            #
            # ── 退出码 (§6.2「失败时记温度峰值 + 退出码」) ──
            # 记在**任何**结果下, 不只是失败时: 失败时它是最重要的线索,
            # 成功时它是"确实跑完了而不是被糊弄过去"的凭据。两种情况
            # 都记, 报告上就不用靠"没这一栏"去猜到底跑没跑。
            if slices_done:
                result.metrics["exit_code"] = round_rc
                result.metrics[f"round_{round_idx}_exit_code"] = round_rc
            stat1 = _read_cpu_stat()
            if stat0 and stat1:
                d_busy = stat1[0] - stat0[0]
                d_total = stat1[1] - stat0[1]
                if d_total > 0:
                    pct = round(d_busy * 100.0 / d_total, 1)
                    load_pcts.append(pct)
                    result.metrics[f"round_{round_idx}_load_pct"] = pct
            if slices_done:
                result.metrics[f"round_{round_idx}_peak_temp_c"] = round(round_peak, 1)
                peak_temps.append(round_peak)
            result.metrics[f"round_{round_idx}_slices"] = n_slices
            result.metrics[f"round_{round_idx}_slices_done"] = slices_done
            if round_text:
                result.log = round_text

            if thermal_abort:
                break
            if result.status == "error":
                break

        # ── 整项聚合指标 (§6.2) ──
        # 温度峰值取**跨轮最大**: 一次老化里最热的那一下才是判超温的依据,
        # 平均值会把"第 1 轮 91℃、第 2 轮 70℃"这种真实的高温洗掉。
        if peak_temps:
            result.metrics["peak_temp_c"] = round(max(peak_temps), 1)
        # ── 判定线: **峰值超 max_temp_c ⇒ 判 failed** (§6.2) ──
        #
        # ⚠️ 这条以前**没实现** (2026-11-04 补): 原来只有「起跑前超收手线」
        # 和「片后超收手线」两条会判 failed, **峰值落在判定线(85℃)和收手线
        # (95℃)之间时, 只在日志里留一句「⚠️ 温度过高」, 结果照样判 passed**
        # ⇒ 一块烤到 93℃ 的板子会被记成「CPU 通过」。
        # 而 §6.2 白纸黑字写着「峰值超过 max_temp_c = 判 failed」——
        # **代码和文档对不上, 以文档为准**。
        peak_temp = result.metrics.get("peak_temp_c")
        over_temp = (peak_temp is not None and peak_temp > max_temp)
        if load_pcts:
            result.metrics["avg_load_pct"] = round(sum(load_pcts) / len(load_pcts), 1)
        # 每个 thermal zone 的峰值 —— 报告上要能看出**是哪个核区最烫**
        # (2026-10-29: 只看小核会低估好几度, §6.2)。
        zones = await _read_all_zones()
        if zones:
            result.metrics["temp_zones_now"] = ", ".join(
                f"{k}={v:.1f}" for k, v in sorted(zones.items()))

        if thermal_abort:
            # 超温提前收手 = **板子不合格**(散热扛不住), 但**板子没崩** ——
            # 这正是热保护存在的意义: 判 failed, 而不是让内核重启板子。
            result.status = "failed"
            result.error = (
                f"CPU 压力超温, 已提前收手 —— {abort_reason}。"
                f"峰值 {result.metrics.get('peak_temp_c', '?')}°C"
                f"(判定线 {max_temp}°C / 收手线 {abort_temp}°C)。"
                f"板子没崩, 但散热扛不住满载"
            )
            result.metrics["thermal_abort"] = "是"
        elif over_temp and result.status == "pending":
            # 峰值超了判定线但没到收手线 —— 板子**扛住了**, 但**散热不合格**。
            # ⚠️ 跟 thermal_abort 分开报: 这两种是**不同的缺陷** ——
            #    收手 = 压到收手线才停(散热更差); 峰值超线 = 整轮都跑完了
            #    才回头看峰值(连续模式的常态, §6.2.6)。
            # ⚠️ 只在**正常跑完**时判: 工人中途点了「停止」(status=stopped)
            #    或者 stress-ng 自己挂了(status=error), 那才是那次跑的原因 ——
            #    别拿温度把它盖掉(半路停的峰值不作数)。
            result.status = "failed"
            # ⚠️ **2026-11-12**: 前面可能**已经写过原因**(stress-ng 退出码
            #   非 0 那条)⇒ 这里**追加**而不是覆盖 —— 两个都是真的事实,
            #   盖掉一个就等于少告诉工人一件事。
            result.error = (
                ((result.error + " / ") if result.error else "")
                + f"CPU 压力超温 —— 峰值 {peak_temp}°C > 判定线 {max_temp}°C"
                f"(收手线 {abort_temp}°C)。"
                f"板子没崩, 但散热扛不住满载"
            )
            result.metrics["over_temp"] = "是"
            await ctx.on_log(f"[CPU] ❌ {result.error}")
        elif all_passed:
            result.status = "passed"
        elif result.status == "pending":
            result.status = "failed"
            # ⚠️ **2026-11-12 兜底**: 走到这里 = 「这一项没通过, 但前面那些
            #   分支都没写原因」。**没有原因 = 报告上印「(未记录原因 —— 属实现
            #   缺陷)」** ⇒ 工人只看到「不合格」两个字, 不知道该查什么。
            #   ⇒ 这里补一句兜底的话; **前面已经写过原因就不覆盖**。
            result.error = result.error or (
                "CPU 压力测试没通过 —— 但**没记录到具体原因**(stress-ng 的"
                "原始输出在本项日志里)。常见原因: 加压中途 stress-ng 自己退出"
                "了 / 达到了温度收手线。请看本项日志, 再决定复测还是返修。"
            )

        await ctx.on_progress(100, "完成")
        evidence.close()
        return result

    async def _run_slice(self, ctx: TestContext, stress_bin: str, threads: int,
                         slice_len: float, max_temp: float, pct_base: float,
                         pct_span: float, label: str,
                         evidence: Optional[_EvidenceLog] = None) -> dict:
        """跑**一片** stress-ng, 返回这一片的结果。

        返回 {rc, text, killed, peak_temp, temp, error}:
          · error 非 None = 这一片压根没起来(工具缺失/异常)
          · temp = 这一片最后一次读到的温度(收手判断就看它)
        """
        # §11.2: 需 root 的调用一律 `sudo -n <命令>`, 绝不弹密码提示
        cmd = with_sudo([
            stress_bin,
            "--cpu", str(threads),
            "--timeout", f"{slice_len:.1f}s",
            "--metrics-brief",
        ], timeout_sec=slice_len + 30)
        await ctx.on_log(f"[CPU] 执行: {' '.join(cmd)}")

        slice_peak = 0.0
        temp = None

        # ── 「这一片起跑」的那一行 ──
        #
        # **为什么要在起跑前就写一行**: 实测板子死在**起手一秒内** —— 要是
        # 只靠采样器, 板子死得太快就一行都留不下, 报告上又是「啥也没有」。
        # 先写一行, 至少能证明「命令发出去了、当时多少度」。
        if evidence is not None:
            z0 = await _read_all_zones()
            # ⚠️ **供电轨不在这里读**(2026-11-03) —— 这正是加压起手、
            # 电流台阶最陡的那一刻, 最不该往事件循环里塞 13ms 的阻塞
            # 读写。窗口内的电压由「加压前 / 加压后」两次采样负责。
            evidence.write(threads, max(z0.values()) if z0 else None, z0, None,
                           freq=_read_cpu_freq(), rails=None,
                           note=f"起跑 {label}")

        try:
            proc = await _spawn_proc(cmd)
        except FileNotFoundError:
            return {"rc": -1, "text": "", "killed": False, "peak_temp": 0.0,
                    "temp": None, "reason": "",
                    "error": tool_missing_reason(
                        "stress-ng", deps_fix_hint(),
                        note="PATH 不含 /sbin, 或这台机器上压根没装这个工具")}
        except Exception as e:
            return {"rc": -1, "text": "", "killed": False, "peak_temp": 0.0,
                    "temp": None, "reason": "",
                    "error": humanize_exception(
                        e, item="CPU 压力项", stage="起 stress-ng 子进程")}

        async def tick(e: float):
            """按挂钟驱动进度与温度采样 —— **不看 stress-ng 吐不吐行**。

            原来这个循环靠 `readline()` 唤醒, 而 stress-ng 加上
            --metrics-brief 之后大部分时间**不吐行**: 于是进度条僵住、
            温度也停止采样, 工人看着像卡死, 实际 CPU 已经满载跑了一分钟。
            进度和温度必须由计时器驱动, 与 stdout 解耦。

            ⚠️ **这里只管判温, 不管记证据** —— 记证据是 `_sample_evidence`
            的活(2 秒一次)。判温仍旧 1 秒一次: 采样率一改, 「合格/不合格」
            的判定口径就跟着改了, 那是改产品行为, 不是加记录。
            """
            nonlocal slice_peak, temp
            temp = await _read_cpu_temp()
            if temp is not None:
                if temp > slice_peak:
                    slice_peak = temp
                await ctx.on_metric("cpu_temp_c", temp)
                if temp > max_temp:
                    await ctx.on_log(f"[CPU] ⚠️ 温度过高: {temp}°C > {max_temp}°C")

            pct = min(slice_len, e) / slice_len
            await ctx.on_progress(
                int(pct_base + pct * pct_span),
                f"运行中 {e:.0f}s / {slice_len:.0f}s, "
                f"温度 {temp if temp is not None else '?'}°C",
            )

        # ── 高频采样器 (只管记证据) ──
        #
        # ⚠️ **必须跟判温分开**: 采样密 ⇒ 峰值采得高 ⇒ 临界板子可能从
        #   「过」变成「不过」—— 那是改了产线的判定口径。所以采样器**只写
        #   记录文件**, 判温仍旧由上面的 tick 以 1 秒一拍来做。
        stop = asyncio.Event()
        sampler = None
        if evidence is not None:
            sampler = asyncio.create_task(
                _sample_evidence(evidence, threads, stop))
        try:
            out = await run_proc_with_progress(
                proc, slice_len, ctx=ctx, on_tick=tick,
                hard_limit_sec=slice_len + 30,
            )
        finally:
            if sampler is not None:
                stop.set()
                try:
                    await asyncio.wait_for(sampler, timeout=2.0)
                except asyncio.TimeoutError:
                    sampler.cancel()
                except asyncio.CancelledError:
                    pass
        # 进程已经退了, **最后再读一次温度** —— 补上「tick 还没来得及跑」
        # 的空档: 进程很快就退时一个 tick 都不会有, 峰值会记成 0,
        # 收手判断也就没有依据了。
        if temp is None:
            temp = await _read_cpu_temp()
            if temp is not None and temp > slice_peak:
                slice_peak = temp
        return {"rc": out.rc, "text": out.text, "killed": out.killed,
                "peak_temp": slice_peak, "temp": temp, "error": None,
                # ⚠️ 用 getattr 而不是 out.reason —— 回归测试把
                #    `run_proc_with_progress` 换成了假的, 那个假的**不一定**
                #    带 reason 字段 ⇒ 直接取属性会 AttributeError。
                "reason": getattr(out, "reason", "")}


def _read_cpu_stat() -> Optional[tuple]:
    """读 /proc/stat 的 cpu 汇总行 → (busy, total) 累计 tick 数。

    §6.2 的 `avg_load_pct` 要的是**占用率百分比**, 不是 loadavg 那个
    "1 分钟平均有几个进程在跑队列里" —— loadavg 是个无量纲的数, 换不成
    百分比(4 核机器上 loadavg 1.0 可能是 25% 也可能是 100%)。
    /proc/stat 的 cpu 行给的是**累计 tick**: 两次采样相减,
    busy Δ / total Δ 就是那段时间的真实占用率。
    """
    try:
        with open("/proc/stat", "r") as f:
            line = f.readline()      # "cpu  user nice system idle iowait ..."
        parts = line.split()
        if len(parts) < 5 or parts[0] != "cpu":
            return None
        vals = [int(x) for x in parts[1:]]
        idle = vals[3] + (vals[4] if len(vals) > 4 else 0)   # idle + iowait
        total = sum(vals)
        return (total - idle, total)
    except (OSError, ValueError, IndexError):
        return None


async def _read_all_zones() -> dict:
    """{zone 类型名: 温度°C} —— **所有** thermal zone。"""
    out = {}
    try:
        zones = sorted(os.listdir(THERMAL_DIR))
    except OSError:
        return out
    for z in zones:
        if not z.startswith("thermal_zone"):
            continue
        type_path = os.path.join(THERMAL_DIR, z, "type")
        temp_path = os.path.join(THERMAL_DIR, z, "temp")
        if not (os.path.exists(type_path) and os.path.exists(temp_path)):
            continue
        try:
            with open(type_path) as f:
                ztype = f.read().strip().lower()
            with open(temp_path) as f:
                out[ztype] = int(f.read().strip()) / 1000.0
        except (OSError, ValueError):
            continue
    return out


async def _read_cpu_zones() -> dict:
    """{zone 类型名: 温度°C} —— **只取 CPU 相关的** zone。

    认法: 类型名里带 cpu / soc / x-t / tsens。
    ⚠️ 这块板子上有两个: `cpul_thermal_zone`(**小核**) 和
    `cpub_thermal_zone`(**大核**) —— 大核才是热点, 两个都要看。
    """
    out = {}
    for ztype, temp in (await _read_all_zones()).items():
        if ("cpu" in ztype or "soc" in ztype or "x-t" in ztype
                or "tsens" in ztype):
            out[ztype] = temp
    return out


async def _read_cpu_temp() -> Optional[float]:
    """CPU 的温度 (°C) —— **所有 CPU 核区里最热的那一个**。

    ⚠️ 2026-10-29 修正: 原来只返回**第一个**名字里带 "cpu" 的 zone ——
    在这块板子上那是 `cpul_thermal_zone`(**小核**); 真正发烫的是
    `cpub_thermal_zone`(**大核**)。实测同一场满载:
    **大核峰值 76.6℃ / 小核峰值 73.8℃** —— 看错了簇, 少报 3℃。
    而内核的 critical trip 是 110℃, 少报 3℃ 就是少 3℃ 的余量。

    认不出哪个 zone 是 CPU 时(别的内核), 退到「所有 zone 里最热的那个」。
    """
    zones = await _read_cpu_zones()
    if zones:
        return max(zones.values())
    allz = await _read_all_zones()
    return max(allz.values()) if allz else None
