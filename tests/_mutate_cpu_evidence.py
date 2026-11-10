# 变异测试: 故意把「板子死在加压半路时也要留下现场」改坏, 看
# tests/test_cpu_evidence.py 会不会红。
#
# 为什么要有这么个东西: 「测试全绿」本身说明不了什么 —— 断言写错了、
# 写松了、或者压根没跑到, 都是绿的。唯一能证明这测试**咬得动**的办法,
# 就是亲手把代码改坏, 看它是不是真的红。
#
# 这一份守的是 2026-10-30 新板 A10010000000058 连续两次死在 CPU 加压
# **起手一秒内**换来的规矩:
#   ① 加压要**逐级往上爬**(1 → 2 → 3 → 4 → 8), 不能一把满载;
#   ①' **片数不够时台阶要压缩, 但满载那一片必须留着** —— 短跑也得真的
#      满载过一次, 否则「板子在满载下会死」测不出来 ⇒ 板子假通过;
#   ② 现场要**边跑边落文件**, 每一行**立刻 flush** —— 板子死了, 写下去的行还在;
#   ③ 每一片**起跑前**也要写一行 —— 板子死得太快, 采样一次都来不及跑;
#   ④ 采样要**密**(≤250ms 一次)—— 1 秒一拍, 死在起手一秒内就漏;
#   ⑤ **频率**也要记 —— 电流台阶跟负载台阶是不是同一瞬间, 全看它;
#   ⑥ 采样器**跑完要停** —— 不停就是记录文件无限长;
#   ⑦ 报告要印这一节, 而且**导出时自动带上**(不用每个调用点自己记得传);
#   ⑧ 报告要点名**板子死在哪一项** —— 不用读者自己翻明细表;
#   ⑨ 写记录失败**不许把测试搞挂** —— 记录是加分项, 不是主流程;
#   ⑩ 文件**要有头**(板子型号 / 起始温度 / 过热阈值 / 起始供电电压)——
#      少了头, 读者看着「57.7℃」根本不知道这算不算热;
#   ⑪ 供电轨的**电压台阶**要记(只记变了的那些)—— 那是「加压后哪根轨
#      掉下去了」的唯一证据;
#   ⑫ 板子上「别的活」(背景负载)要**量出来、写进头、每行带着 bg=、
#      报告里说出来** —— 板子的功耗是**总**功耗(2026-11-03 14:59 实测:
#      同样 8 线程, 板子上有没有别的活, 死活差好几倍);
#   ⑬ **供电轨 1Hz**(不是每行都读)—— 一次 13.31ms 的阻塞读写, 每 200ms
#      来一次, 峰值时应用自己就占着一颗核的 10%;
#   ⑭ **起进程这一步必须可替换**(`_spawn_proc`)—— 回归测试原来起的是
#      **真的** `stress-ng --cpu 8`, 把板子压到掉电复位(2026-11-03 14:59);
#   ⑮ **起进程这一步还要有「绊线」**(`asyncio.create_subprocess_exec` 被
#      套一层: 命令里带 `stress-ng` 就当场拦下)—— 光有可替换的口子不够,
#      万一生产代码绕开它, 光换假口子就挡不住真进程 ⇒ 绊线保证
#      「绕开」的后果是**测试变红**, 而不是**板子掉电**。
#
# ⚠️⚠️ **仍然故意不做一个变异**: 「起进程绕开 `_spawn_proc`、直接
#    `create_subprocess_exec`」—— 那个变异一跑就**真的**在板子上起
#    `stress-ng --cpu 8`(2026-11-03 14:59 就是这么把板子压死的),
#    变异脚本自己就成了凶器。⇒ 这条**只能靠人读代码**保证
#    (`server/tests/cpu.py` 里那处调用必须走 `_spawn_proc`)。
#    ⇒ 但**万一哪天真的绕开了**, 回归测试会**当场变红**(绊线会被踩),
#    而不是把板子烤了 —— 这条兜底是 ⑮ 干的。
#
# 做法: **每个变异单开一个全新目录**, 把 server/ + config.yaml + 那个测试
# 文件复制进去, 在**副本**上做一处改动, 在副本目录里跑测试。
# 被改的那个文件是从头到尾**只写一次**(直接写变异后的内容), 别的文件原样
# 复制 —— 所以整个过程不覆写任何已存在的文件, 跑完也不用还原。
# ⚠️ 仓库里 server/ 是产线在跑的那份, **只在副本上改**。
#
# 临时目录用 `tests._tmp.mkdtemp` —— 进程退出时自动删掉, 不在 /tmp 里留残骸。
#
# 跑法: .venv/bin/python tests/_mutate_cpu_evidence.py
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests._tmp import mkdtemp                       # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(REPO, ".venv", "bin", "python")
# ⚠️ **三个测试都要跑** (2026-11-04 加了第三个):
#   · `test_cpu_evidence`     —— 现场记录本身(头/行/落盘/电压台阶/背景负载);
#   · `test_cpu_load_shape`   —— **加压形态**(连续 vs 分片/供电轨不在窗口里/
#     采样间隔/fsync 间隔/判温口径);
#   · `test_cpu_thermal_guard` —— **热保护**(峰值超判定线判 failed / 峰值到过
#     收手线就收手)。⇒ 任何一条, 红了就算「咬得动」。
TESTS = ("tests.test_cpu_evidence", "tests.test_cpu_load_shape",
         "tests.test_cpu_thermal_guard")

# 每个变异: (文件, from, to, 说明)。from 必须在源文件里**找得到**, 否则算失败。
MUTATIONS = [
    (
        "server/tests/cpu.py",
        "                this_threads = (_ramp_threads(threads, s, n_slices) if sliced\n"
        "                                else threads)",
        "                this_threads = threads",
        "CPU: 不逐级加压了, 一把满载(2026-10-30 那两次重启就是起手就满载)",
    ),
    (
        "server/tests/cpu.py",
        "    step = max(0, int(step))\n"
        "    if n_slices is None or int(n_slices) <= 0:\n"
        "        i = step\n"
        "        v = _LADDER[i] if i < len(_LADDER) else _LADDER[-1]\n"
        "        return min(total, v)",
        "    return total                          # 一把满载",
        "CPU: 缓启动没了(第 1 片就用满核)",
    ),
    (
        "server/tests/cpu.py",
        "_LADDER = (1, 2, 3, 4, 8, 16, 32)",
        "_LADDER = (1, 2, 4, 8, 16, 32)",
        "CPU: 台阶里没有 3 了(「3 线程活着、4 线程死」这个缺陷特征就测不出来)",
    ),
    (
        "server/tests/cpu.py",
        "    keep = max(1, int(n_slices) - 1)      # 至少给满载留一片",
        "    keep = len(below)                    # 不压缩 ⇒ 短跑满载不到",
        "CPU: 台阶不压缩了(短跑只 4 片 ⇒ 最后一片才 4 线程 ⇒ 满载没跑到"
        " ⇒ 板子假通过)",
    ),
    (
        "server/tests/cpu.py",
        "        over_temp = (peak_temp is not None and peak_temp > max_temp)",
        "        over_temp = False",
        "CPU: **峰值超判定线不判 failed** ⇒ 烤到 93℃ 的板子被记成「通过」"
        "(2026-11-04 补的那个洞)",
    ),
    (
        "server/tests/cpu.py",
        "        over_temp = (peak_temp is not None and peak_temp > max_temp)",
        "        over_temp = (peak_temp is not None and peak_temp >= max_temp)",
        "CPU: 判定线改成「达到就判」(恰好 85℃ 也判 failed —— 「超过」是严格大于)",
    ),
    (
        "server/tests/cpu.py",
        "                hot = max([t for t in (temp_now, out[\"peak_temp\"])\n"
        "                           if t is not None], default=None)",
        "                hot = temp_now",
        "CPU: 收手只看**末次读数**(峰值到过收手线也当没事 ⇒ 又起一片)",
    ),
    (
        "server/tests/cpu.py",
        "        elif over_temp and result.status == \"pending\":",
        "        elif over_temp:",
        "CPU: 峰值超线把「工人中途停了」盖成 failed(报告上写的原因跟实际不符)",
    ),
    (
        "server/tests/cpu.py",
        "    keep = max(1, int(n_slices) - 1)      # 至少给满载留一片",
        "    keep = 0                             # 满载那一片也丢了",
        "CPU: 满载那一片被挤掉了(短跑压根没满载过)",
    ),
    (
        "server/tests/cpu.py",
        "        below.pop(len(below) // 2)       # 丢中间那级(1 和最接近天花板那级留着)",
        "        below.pop(0)                     # 丢起点那级(起步不轻了)",
        "CPU: 压缩台阶时把起步那级丢了(板子死在起手一秒内, 起步必须轻)",
    ),
    (
        "server/tests/cpu.py",
        "    gov = _read_cpufreq_info()\n"
        "    if gov:\n"
        '        lines.append("# 调速策略(governor/频率上限): " + gov)',
        "    gov = \"\"",
        "CPU: 记录文件的头里没有调速策略(看不出频率为什么一上来就贴着上限)",
    ),
    (
        "server/tests/cpu.py",
        "            evidence.write(threads, hot, zones, load_pct,\n"
        "                           freq=_read_cpu_freq(), rails=None,\n"
        "                           bg_pct=bg_pct)",
        "            pass  # 不记了",
        "CPU: 采样不记了(板子一死, 温度负载频率全蒸发)",
    ),
    (
        "server/tests/cpu.py",
        "EVIDENCE_SAMPLE_INTERVAL_SEC = 2.0",
        "EVIDENCE_SAMPLE_INTERVAL_SEC = 0.2",
        "CPU: 采样又变回 200ms 一行(加压窗口里应用自己就占着一颗核的一小截 "
        "⇒ 这正是打包之后每次都硬复位的那三样负担之一)",
    ),
    (
        "server/tests/cpu.py",
        "EVIDENCE_SAMPLE_INTERVAL_SEC = 2.0",
        "EVIDENCE_SAMPLE_INTERVAL_SEC = 4.0",
        "CPU: 采样太慢(4 秒一行 ⇒ 板子死在起手一秒内时那票关键证据会漏)",
    ),
    (
        "server/tests/cpu.py",
        "            evidence.write(threads, hot, zones, load_pct,\n"
        "                           freq=_read_cpu_freq(), rails=None,\n"
        "                           bg_pct=bg_pct)",
        "            evidence.write(threads, hot, zones, load_pct,\n"
        "                           rails=None,\n"
        "                           bg_pct=bg_pct)",
        "CPU: 频率不记了(看不出电流台阶跟负载台阶是不是同一瞬间)",
    ),
    (
        "server/tests/cpu.py",
        "        finally:\n"
        "            if sampler is not None:\n"
        "                stop.set()\n"
        "                try:\n"
        "                    await asyncio.wait_for(sampler, timeout=2.0)\n"
        "                except asyncio.TimeoutError:\n"
        "                    sampler.cancel()\n"
        "                except asyncio.CancelledError:\n"
        "                    pass",
        "        finally:\n"
        "            pass",
        "CPU: 采样器不停(在背后一直写, 记录文件无限长)",
    ),
    (
        "server/tests/cpu.py",
        "            self._fh.flush()          # 板子随时可能死 —— 得**立刻**交出去",
        "            pass  # 不 flush",
        "CPU: 不立刻落盘(攒在缓冲区里, 板子死了跟着一起没)",
    ),
    (
        "server/tests/cpu.py",
        "            evidence.write(threads, max(z0.values()) if z0 else None, z0, None,\n"
        "                           freq=_read_cpu_freq(), rails=None,\n"
        '                           note=f"起跑 {label}")',
        "            pass  # 不记了",
        "CPU: 起跑前不记那一行(板子死在起手一秒内 ⇒ 一行都留不下)",
    ),
    (
        "server/tests/cpu.py",
        "                self._fh.write(_format_evidence_header(peak_threads,\n"
        "                                                       background_pct,\n"
        "                                                       load_mode))",
        "                pass  # 不写头",
        "CPU: 记录文件没有头(读者不知道多少度算热、哪条轨该多少伏)",
    ),
    (
        "server/tests/cpu.py",
        "            line += self._rails_delta(rails)",
        "            pass  # 不记电压台阶",
        "CPU: 电压台阶不记了(看不出供电轨在加压后掉没掉)",
    ),
    (
        "server/tests/cpu.py",
        "    except Exception:\n"
        "            # **什么都别管** —— 记录证据是加分项, 写不进去就当没有。\n"
        "            # 绝不能因为写记录失败把整个 CPU 项搞挂(那才是真事故)。\n"
        "            pass",
        "    except Exception:\n"
        "            raise",
        "CPU: 写记录失败就把整个测试搞挂(记录是加分项, 不是主流程)",
    ),
    # ── 2026-11-03 新增: 背景负载(板子上别的活) + 供电轨 1Hz + 起进程的口子 ──
    (
        "server/tests/cpu.py",
        "        background_pct = await _measure_background_pct()",
        "        background_pct = None   # 不量了",
        "CPU: 起跑前不量背景负载(板子上别的活占几核就永远不知道)",
    ),
    (
        "server/tests/cpu.py",
        '                line += f" bg={bg_pct:.1f}%"',
        '                pass  # 不记背景占用',
        "CPU: 每一行不记 bg=(板子死的那一刻板子上还有多少别的活, 看不见)",
    ),
    # ⚠️ **2026-11-03 作废一条**: 「供电轨又变回每 200ms 读一次」这个变异
    #    **观察不到了** —— 加压窗口里已经**一次都不读**供电轨(§6.2.6),
    #    「缓存 TTL」这个旋钮压根不在窗口里 ⇒ 把它改成 0.2 没有任何可观察
    #    的后果 ⇒ 「测试还是绿的」不代表测试漏了, 是**这条变异本身死了**。
    #    ⇒ 规则改由下面那条「供电轨又回到加压窗口里读」守着(那条能抓住)。
    # ⚠️ **2026-11-03 作废一条**: 「供电轨不用缓存」也**观察不到了** ——
    #    供电轨现在只在**窗口外**读两次(加压前 / 加压后), 而且两次都是
    #    `max_age=0.0` 的**强制重读** ⇒ 缓存在这条路径上根本不起作用。
    #    ⇒ 同样作废(理由同上)。
    (
        "server/tests/cpu.py",
        "    if background_pct is not None:\n"
        "        lines.append(\n"
        '            f"# 起跑时板子背景负载: {background_pct:.1f}% "',
        "    if False:\n"
        "        lines.append(\n"
        '            f"# 起跑时板子背景负载: {background_pct:.1f}% "',
        "CPU: 记录文件的头里不写背景负载(板子死在半路时, 头是唯一留得下的)",
    ),
    (
        "server/boot_evidence.py",
        '        out.append("**板子上还有别的活**: " + " / ".join(bg_bits)\n'
        '                   + " —— 那部分**不是加压线程**(浏览器、代理、别的进程), "\n'
        '                     "但**照样算**在板子的功耗里。")',
        '        out.append("(略)")',
        "EVID: 结论里不说「板子上还有别的活」(读者会以为是板子自己的天花板)",
    ),
    (
        "server/boot_evidence.py",
        "        if worst is not None and worst >= 25.0:",
        "        if False:",
        "EVID: 背景很高也不提醒(这一轮的死会被算成板子自己的缺陷)",
    ),
    (
        "server/boot_evidence.py",
        '    return evidence_dir() / f"cpu_evidence_run{run_id}.txt"',
        '    return evidence_dir() / "cpu_evidence_wrong_name.txt"',
        "EVID: 现场记录写到别的文件名(报告读不回来)",
    ),
    (
        "server/report.py",
        "        boot_evidence=boot_evidence,\n"
        "        has_boot_evidence=bool(boot_evidence),\n"
        "        cpu_evidence=cpu_evidence,",
        "        boot_evidence=boot_evidence,\n"
        "        has_boot_evidence=bool(boot_evidence),\n"
        '        cpu_evidence="",',
        "REPORT: 报告里不印「加压现场记录」这一节 "
        "[锚点带上下文 ⇒ 只改喂给模板的那处; 导出那处改了没用 —— "
        "render_html 收不到 None 会自己从盘上读回来]",
    ),
    (
        "server/report.py",
        "    if cpu_evidence is None:\n"
        '        cpu_evidence = boot_evidence_mod.read_cpu_evidence(run.get("id"))',
        "    if cpu_evidence is None:\n"
        '        cpu_evidence = ""',
        "REPORT: 导出时不自动带上现场记录(得每个调用点自己记得传)",
    ),
    (
        "server/report.py",
        "        if err.startswith(boot_evidence_mod.SOFT_ITEM_ERROR) or \\\n"
        "                err.startswith(boot_evidence_mod.HARD_RESET_ITEM_ERROR):",
        "        if False and (err.startswith(boot_evidence_mod.SOFT_ITEM_ERROR) or \\\n"
        "                err.startswith(boot_evidence_mod.HARD_RESET_ITEM_ERROR)):",
        "REPORT: 不点名板子死在哪一项(读者得自己翻明细表)",
    ),
    (
        "server/report.py",
        "    if died_in_item_key == \"cpu\" and died_hard_reset:\n"
        "        cpu_death_summary = "
        "boot_evidence_mod.summarize_cpu_death(cpu_evidence)",
        "    if False:\n"
        "        cpu_death_summary = "
        "boot_evidence_mod.summarize_cpu_death(cpu_evidence)",
        "REPORT: 不给「板子死在哪一级加压」的结论(读者得自己翻那屏原始数据)",
    ),
    (
        "server/boot_evidence.py",
        '                f"**热不是原因**: 死时最热 {last[\'hot\']:.1f}℃, 离内核 critical "',
        '                f"**热就是原因**: 死时最热 {last[\'hot\']:.1f}℃, 离内核 critical "',
        "EVID: 结论说反了(明明离 critical 还差 52℃, 却说「热就是原因」)",
    ),
    (
        "server/boot_evidence.py",
        '    "板子在这一项上硬复位(掉电 / PMIC 复位 / 看门狗复位) —— 被测设备不合格")',
        '    "服务重启, 本项没跑完")',
        "EVID: 硬复位的说法被换回软重启那句(方向又反了 —— 放行扛不住的板子)",
    ),
    # ── 2026-11-02 13:50 那次实测加的两条 ──
    (
        "server/report.py",
        "        cpu_death_summary = "
        "boot_evidence_mod.summarize_cpu_death(cpu_evidence)\n"
        "        if not cpu_death_summary:\n"
        "            cpu_death_summary = (\n"
        "                boot_evidence_mod.summarize_cpu_death_from_journal(\n"
        '                    boot_evidence or ""))',
        "        cpu_death_summary = "
        "boot_evidence_mod.summarize_cpu_death(cpu_evidence)",
        "REPORT: 现场记录读不出来时, 没有兜底(报告里「板子死在哪一级加压」"
        "那一节整个不见 —— 恰恰是最该有证据的一次)",
    ),
    (
        "server/report.py",
        '            body = ("(本项被板子硬复位打断 —— 这一项的日志还没来得及落库, "\n'
        '                    "跟着进程一起没了。死因看上面「板子为什么死」那两节: "\n'
        '                    "上一次启动的日志尾巴 + 加压现场记录。)")',
        '            body = "(本项没有产生任何日志 —— 属异常: 跑过的项都该有日志)"',
        "REPORT: 硬复位那一项的日志说明还是那句「属异常」(板子打断 ≠ 工具异常)",
    ),
    # ── 2026-11-02 13:50 那次实测加的: **fsync** + 空文件的解释 ──
    (
        "server/tests/cpu.py",
        "            os.fsync(self._fh.fileno())\n"
        "            self._last_fsync = time.monotonic()",
        "            self._last_fsync = time.monotonic()",
        "CPU: 不 fsync 了(硬复位 ⇒ 页缓存整个丢 ⇒ 证据全没 ⇒ 报告里那节不见)",
    ),
    (
        "server/tests/cpu.py",
        "            if note or (time.monotonic() - self._last_fsync) >= EVIDENCE_FSYNC_INTERVAL_SEC:\n"
        "                self._fsync()",
        "            if True:\n"
        "                self._fsync()",
        "CPU: 每行都 fsync(SD 上太慢 ⇒ 把采样拖垮)",
    ),
    (
        "server/tests/cpu.py",
        "            if note or (time.monotonic() - self._last_fsync) >= EVIDENCE_FSYNC_INTERVAL_SEC:\n"
        "                self._fsync()",
        "            if False:\n"
        "                self._fsync()",
        "CPU: 采样行一次都不 fsync(「起跑」那一行 fsync 也不做)",
    ),
    (
        "server/boot_evidence.py",
        "        if p.stat().st_size <= 0:\n"
        "            return CPU_EVIDENCE_EMPTY",
        "        if False:\n"
        "            return CPU_EVIDENCE_EMPTY",
        "EVID: 0 字节的文件也当成「有内容」(分不清「没有」和「空的」)",
    ),
    (
        "server/report.py",
        "        if state == boot_evidence_mod.CPU_EVIDENCE_EMPTY:",
        "        if False:",
        "REPORT: 现场记录文件是空的 ⇒ 不解释(静悄悄地什么都不说 —— "
        "恰恰是最该有证据的一次)",
    ),
    # ── 2026-11-03 加的: **加压封顶值可调**(页面上的「线程数」旋钮) ──
    #
    # 起因: 板子 A10000000000010 在 CPU 加压项**每次都被硬复位**, 而终端
    # 跑同样的 stress-ng 没事 ⇒ 崖在总占用 ~4.3 核(应用自己就占了 0.7~2.3
    # 核)。要往下试, 唯一能立刻拧小的旋钮就是「加压封顶在几线程」。
    # ⚠️ 这个旋钮最坏的坏法是「**看着能拧, 其实没接上**」—— 工人拧小了,
    #    板子照样死, 而报告上什么异常都看不出来。
    (
        "server/tests/cpu.py",
        '    if req <= 0:\n        req = 1',
        '    if True:\n        req = 1',
        "CPU: **旋钮拧大也不管用**(不管填几一律 1 核 ⇒ 压力上不去, 测不出东西)",
    ),
    (
        "server/tests/cpu.py",
        '    if req <= 0:\n        req = 1',
        '    if req <= 0:\n        req = ncpu',
        "CPU: **下界被放开**(0 又回到「自动 = 按核数 = 满核 8」⇒ 那正是 "
        "2026-11-04 实测会把板子压死的那一档)",
    ),
    (
        "server/tests/cpu.py",
        "        v = _LADDER[i] if i < len(_LADDER) else _LADDER[-1]\n"
        "        return min(total, v)",
        "        v = _LADDER[i] if i < len(_LADDER) else _LADDER[-1]\n"
        "        return v",
        "CPU: 阶梯不按封顶值封顶(封顶拧到 2, 爬到第 5 级还是压 8 线程)",
    ),
    (
        "server/tests/cpu.py",
        '    if peak_threads:\n'
        '        shape = ("阶梯从 1 逐级爬到它" if load_mode == "sliced"',
        '    if False:\n'
        '        shape = ("阶梯从 1 逐级爬到它" if load_mode == "sliced"',
        "CPU: 记录文件的头里不写本轮封顶值(板子死在半路 ⇒ 不知道它本来要压到几)",
    ),
    (
        "server/tests/cpu.py",
        '        result.metrics["peak_threads"] = threads',
        '        result.metrics["peak_threads"] = 0',
        "CPU: 指标里的封顶值永远是 0(报告上答不出「这一轮压到了几线程」)",
    ),
    # ── 2026-11-03 加的: **加压执行体还原成打包前的形态**(§6.2.6) ──
    #
    # 起因: 同一块板子, **打包前每次都正常, 打包后每次都硬复位** ⇒ 差异
    # 全在应用自己这几行上。还原之后, 这几条规矩必须有测试守着 ——
    # 否则哪天谁「顺手优化」回去, 板子又得死一次, 而**没人知道是哪次改动**。
    (
        "server/tests/cpu.py",
        "            n_slices = (max(1, int(round(duration / slice_sec))) if sliced\n"
        "                        else 1)",
        "            n_slices = (max(1, int(round(duration / slice_sec))) if sliced\n"
        "                        else 12)",
        "CPU: 连续模式又变成分 12 片(每片重起一个进程 ⇒ 回到打包后的形态)",
    ),
    (
        "server/tests/cpu.py",
        "                this_threads = (_ramp_threads(threads, s, n_slices) if sliced\n"
        "                                else threads)",
        "                this_threads = _ramp_threads(threads, s, n_slices)",
        "CPU: 连续模式也去爬台阶(n_slices=1 ⇒ _ramp_threads 只给 1 线程 ⇒ "
        "**满载根本没跑到** ⇒ 板子假通过)",
    ),
    (
        "server/tests/cpu.py",
        "                           freq=_read_cpu_freq(), rails=None,\n"
        "                           bg_pct=bg_pct)",
        "                           freq=_read_cpu_freq(), rails=_read_rails_cached(),\n"
        "                           bg_pct=bg_pct)",
        "CPU: 供电轨又回到**加压窗口里**读(每秒扫 66 个 sysfs 文件, 5.4~13.3ms "
        "的阻塞读写 ⇒ 打包后每次都硬复位的三样负担之一)",
    ),
    (
        "server/tests/cpu.py",
        "EVIDENCE_FSYNC_INTERVAL_SEC = 10.0",
        "EVIDENCE_FSYNC_INTERVAL_SEC = 1.0",
        "CPU: 证据文件又变成每秒一次 fsync(根文件系统在 SD 卡上 ⇒ 每秒一次"
        "写电流尖峰, 正好撞上板子最吃力的时候)",
    ),
    (
        "server/tests/_tools.py",
        "    tick_sec: float = 1.0,",
        "    tick_sec: float = 5.0,",
        "CPU: **判温口径**被动了(1 秒一拍 ⇒ 5 秒一拍 ⇒ 峰值采低了 ⇒ "
        "临界板子从「不过」变成「过」—— 那是改产线的判定口径)",
    ),
    # ── 加压上限 8 核 (2026-11-09 晚放回满核; 当天白天是 5, 再之前是 6) ──
    (
        "server/tests/cpu.py",
        "MAX_STRESS_THREADS = 8",
        "MAX_STRESS_THREADS = 64",
        "CPU: **加压上限没了**(8 核 → 64 核 ⇒ 页面拧 8 就真的挨 8 核的压 ⇒ "
        "满核把「7 核就复位」的那批板子压死 ⇒ 工具误杀好板)",
    ),
    (
        "server/tests/cpu.py",
        "    return max(1, min(_requested_threads(requested), MAX_STRESS_THREADS))",
        "    return _requested_threads(requested)",
        "CPU: 上限**没夹住**(自动=核数 ⇒ 8 核机器直接满载)",
    ),
    (
        "server/tests/cpu.py",
        "        threads = _resolve_threads(t_cfg.get(\"threads\", 0))",
        "        threads = _requested_threads(t_cfg.get(\"threads\", 0))",
        "CPU: 这一项**绕过了上限**(`_resolve_threads` → `_requested_threads`) ⇒ "
        "页面上拧 8 就真的压 8 核",
    ),
    (
        "server/tests/cpu.py",
        "        threads_capped = (threads != want_threads)",
        "        threads_capped = False",
        "CPU: 被夹住这件事**不再记**(指标里没有 threads_capped, 日志里也不说 ⇒ "
        "报告上看不出「这一轮其实只压了 6 核」)",
    ),
    (
        "server/tests/cpu.py",
        "        shape = (\"阶梯从 1 逐级爬到它\" if load_mode == \"sliced\"\n"
        "                 else \"连续加压, 一个进程跑满整轮\")",
        "        shape = \"阶梯从 1 逐级爬到它\"",
        "CPU: 现场记录的**头**又写死成「阶梯」(而这一轮跑的是**连续加压**, "
        "压根没有阶梯 ⇒ 产线看报告会被带偏去查爬坡逻辑)",
    ),
    # ── 2026-11-10 加的: **下限夹住也要看得见**(`threads_floored`)──────
    #
    # 起因: 下界从 0 抬到 1 之后, 工人填 `0` 实际只压 1 核, 而
    # `threads_capped` 只反映**上限** ⇒ 报告上写着「配置 0 / 实际 1」却
    # **没有一句话解释这个 0 怎么变成 1 的** ⇒ 看着像老版本「0 = 自动」。
    (
        "server/tests/cpu.py",
        "    return _parse_thread_request(requested) < 1",
        "    return False",
        "CPU: **被下限夹住这件事不再记**(指标里没有 threads_floored ⇒ "
        "报告上看不出「配置 0 其实只压了 1 核」)",
    ),
    (
        "server/tests/cpu.py",
        "    return _parse_thread_request(requested) < 1",
        "    return _parse_thread_request(requested) <= 1",
        "CPU: 下限夹取的判据放宽到「≤ 1」(填 1 也被说成被夹 ⇒ "
        "正常值被误报 ⇒ 工人不信这个指标)",
    ),
    (
        "server/tests/cpu.py",
        "    except (TypeError, ValueError):\n        return 0",
        "    except (TypeError, ValueError):\n        return 1",
        "CPU: 非法值被当成「已经夹过了」(填 'abc' 时 `threads_floored` 不再是"
        "「是」⇒ 而实际压的确实是下限 1 核 ⇒ 指标跟事实对不上)",
    ),
    (
        "server/tests/cpu.py",
        "        threads_floored = _floor_applied(t_cfg.get(\"threads\", 0))",
        "        threads_floored = False",
        "CPU: 指标和日志都不再提「被下限夹住」(只有 threads_capped ⇒ "
        "下限这一半的夹取彻底看不见)",
    ),
    (
        "server/tests/cpu.py",
        "        if threads_floored:\n            result.metrics[\"threads_floored\"] = \"是\"",
        "        if False:\n            result.metrics[\"threads_floored\"] = \"是\"",
        "CPU: 指标**不落库**(日志里说了、报告的指标表里没有 ⇒ "
        "读报告的人看不到)",
    ),
    (
        "server/tests/cpu.py",
        "                f\"[CPU] ⚠️ 线程数被**下限 1 核**夹住: 页面填的是 \"",
        "                f\"[CPU] ⚠️ 线程数被夹住: 页面填的是 \"",
        "CPU: 日志里不再明说「**下限**」(读的人分不清是被上限夹的还是被下限夹的)",
    ),
]


def build_copy(dst, rel, new_text):
    """把 server/ + config.yaml + 那个测试文件复制到 dst, 并把 rel 写成 new_text。

    每个文件**只写一次** —— 被变异的那个根本不复制进来, 直接写变异后的
    内容。全程不覆写任何已存在的文件, 所以跑完不需要还原。
    """
    src = os.path.join(REPO, "server")
    for root, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        relroot = os.path.relpath(root, src)
        outroot = (os.path.join(dst, "server") if relroot == "."
                   else os.path.join(dst, "server", relroot))
        os.makedirs(outroot, exist_ok=True)
        for fn in files:
            relfile = os.path.normpath(os.path.join(relroot, fn))
            if relfile == rel:
                continue          # 这个下面单独写
            shutil.copy2(os.path.join(root, fn), os.path.join(outroot, fn))

    shutil.copy2(os.path.join(REPO, "config.yaml"),
                 os.path.join(dst, "config.yaml"))
    os.makedirs(os.path.join(dst, "tests"), exist_ok=True)
    for extra in ("test_cpu_evidence.py", "test_cpu_load_shape.py",
                  "test_cpu_thermal_guard.py", "_tmp.py"):
        shutil.copy2(os.path.join(REPO, "tests", extra),
                     os.path.join(dst, "tests", extra))

    outp = os.path.join(dst, rel)
    os.makedirs(os.path.dirname(outp), exist_ok=True)
    with open(outp, "w", encoding="utf-8") as f:
        f.write(new_text)


def run_one(work):
    """在副本目录里跑那两个测试。返回 (红了没有, 失败项数, 输出)。"""
    env = dict(os.environ)
    env["PYTHONPATH"] = work
    out_all = ""
    fails = 0
    red = False
    for mod in TESTS:
        proc = subprocess.run(
            [PY, "-m", mod],
            cwd=work, env=env, capture_output=True, text=True, timeout=300,
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        out_all += f"\n----- {mod} (exit={proc.returncode}) -----\n{out}"
        fails += out.count("✗")
        if proc.returncode != 0:
            red = True
    return red, fails, out_all


def main():
    root = mkdtemp(prefix="mut_cpuev_")
    print(f"副本都在 {root} 下面(进程退出时自动删掉)\n")

    bad = 0
    for i, (rel, frm, to, desc) in enumerate(MUTATIONS):
        srcp = os.path.join(REPO, rel)
        with open(srcp, encoding="utf-8") as f:
            orig = f.read()
        if frm not in orig:
            print(f"\n✗ {desc}\n   → 变异点没找到(源文件变了?): {frm[:60]!r}")
            bad += 1
            continue

        work = os.path.join(root, f"m{i:02d}")
        os.makedirs(work, exist_ok=True)
        build_copy(work, rel, orig.replace(frm, to))

        red, fails, out = run_one(work)
        if red and fails == 0:
            # ⚠️ **红了但一个 ✗ 都没有** —— 那多半不是断言把它咬住的, 而是
            #   测试自己崩了(语法错/导入错/异常)。这不算「咬得动」。
            print(f"\n✗ {desc}\n   → 红了, 但一个 ✗ 都没有 —— "
                  f"这是**崩了**, 不是被断言咬住的, 不算数:\n{out[-800:]}")
            bad += 1
        elif red:
            print(f"\n✓ {desc}\n   → 测试变红 ({fails} 项失败) —— 咬得动")
        else:
            print(f"\n✗ {desc}\n   → 测试还是绿的! 这个变异没被守住")
            bad += 1

    print("\n" + ("全部变异都被抓住了 ✓" if bad == 0
                  else f"有 {bad} 个变异没被抓住 ✗"))
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
