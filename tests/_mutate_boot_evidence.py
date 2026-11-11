# 变异测试: 故意把「板子死在加压半路, 证据一个字都不许丢」改坏, 看
# tests/test_boot_evidence.py 会不会红。
#
# 为什么要有这么个东西: 「测试全绿」本身说明不了什么 —— 断言写错了、
# 写松了、或者压根没跑到, 都是绿的。唯一能证明这测试**咬得动**的办法,
# 就是亲手把代码改坏, 看它是不是真的红。
#
# 这一份守的是 2026-11-02 13:50 那次实测换来的两条规矩:
#   ① **容错解码** —— 「加压现场记录」是板子死的那一刻写的, 尾巴可能只写
#      了半行(甚至把一个多字节字符劈成两半)⇒ 严格 UTF-8 解码会抛异常
#      ⇒ **整份证据全丢** ⇒ 报告里「加压现场记录」那一节**整个不见了**
#      —— 恰恰是最该有证据的一次, 证据却没了。
#   ② **兜底来源** —— 现场记录读不出来时, 还能从**上一次启动的日志**里
#      读出 `stress-ng --cpu N` 的命令行 ⇒ 至少说出「死在几线程那一片」。
#      日志尾巴里 sudo 的长命令行**会被折成两行**, `--cpu N` 落在**没有
#      时间戳的续行**上 ⇒ 时间戳要沿用上一行; 而且日志里可能混着**上一轮**
#      的残留 ⇒ 跨轮(间隔 > 30 秒)不能串。
#
# 做法: **每个变异单开一个全新目录**, 把 server/ + config.yaml + 那个测试
# 文件复制进去, 在**副本**上做一处改动, 在副本目录里跑测试。
# 被改的那个文件是从头到尾**只写一次**(直接写变异后的内容), 别的文件原样
# 复制 —— 所以整个过程不覆写任何已存在的文件, 跑完也不用还原。
# ⚠️ 仓库里 server/ 是产线在跑的那份, **只在副本上改**。
#
# 临时目录用 `tests._tmp.mkdtemp` —— 进程退出时自动删掉, 不在 /tmp 里留残骸。
#
# 跑法: .venv/bin/python tests/_mutate_boot_evidence.py
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests._tmp import mkdtemp                       # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(REPO, ".venv", "bin", "python")
TEST = "tests.test_boot_evidence"

# 每个变异: (文件, from, to, 说明)。from 必须在源文件里**找得到**, 否则算失败。
MUTATIONS = [
    # ── ① 容错解码 ──
    (
        "server/boot_evidence.py",
        "    return raw.decode(\"utf-8\", errors=\"replace\")",
        "    return raw.decode(\"utf-8\")",
        "EVID: 又是严格解码了(尾巴劈开一个多字节字符就抛异常 ⇒ 整份证据全丢"
        " ⇒ 报告里「加压现场记录」那一节整个不见)",
    ),
    (
        "server/boot_evidence.py",
        "    try:\n"
        "        raw = path.read_bytes()\n"
        "    except OSError:\n"
        "        return \"\"",
        "    try:\n"
        "        raw = path.read_text(encoding=\"utf-8\").encode(\"utf-8\")\n"
        "    except OSError:\n"
        "        return \"\"\n"
        "    except UnicodeDecodeError:\n"
        "        return \"\"",
        "EVID: 严格解码一失败就**整份丢**(回到 2026-11-02 踩坑时的样子)",
    ),
    (
        "server/boot_evidence.py",
        "    return _read_text_lenient(cpu_evidence_path(run_id))",
        "    try:\n"
        "        return cpu_evidence_path(run_id).read_text(encoding=\"utf-8\")\n"
        "    except (OSError, UnicodeDecodeError):\n"
        "        return \"\"",
        "EVID: 读现场记录不走容错那条路(截断 ⇒ 整份丢)",
    ),
    (
        "server/boot_evidence.py",
        "    return _read_text_lenient(evidence_path(run_id))",
        "    try:\n"
        "        return evidence_path(run_id).read_text(encoding=\"utf-8\")\n"
        "    except (OSError, UnicodeDecodeError):\n"
        "        return \"\"",
        "EVID: 读上一次启动的日志不走容错那条路(截断 ⇒ 兜底也没了)",
    ),
    # ── ② 兜底: 从日志里读「死在几线程」 ──
    (
        "server/boot_evidence.py",
        "    if not text:\n"
        "        return \"\"\n"
        "    hits = []",
        "    if True:\n"
        "        return \"\"\n"
        "    hits = []",
        "EVID: 兜底整个不干了(现场记录读不出来时, 报告就啥也不说)",
    ),
    (
        "server/boot_evidence.py",
        "        if ts is not None:\n"
        "            last_ts = ts           # **每一行**有时间戳都要记(不管它匹配不匹配)",
        "        if ts is not None and \"stress-ng\" in line:\n"
        "            last_ts = ts           # 只在匹配的行上记时间戳",
        "EVID: 时间戳只在匹配行上记(长命令行被折断时, `--cpu N` 落在续行上"
        " ⇒ 时间戳变旧 ⇒ 串到别的片上 ⇒ 说错死在哪)",
    ),
    (
        "server/boot_evidence.py",
        "    hits.sort(key=lambda h: (h[0] is None, h[0] if h[0] is not None else 0))",
        "    hits.sort(key=lambda h: (h[0] is None, -(h[0] if h[0] is not None else 0)))",
        "EVID: 按时间**倒**着排(报告里那一节就是倒着排的 ⇒ 取到最老的一条"
        " ⇒ 死在哪一片说反了)",
    ),
    (
        "server/boot_evidence.py",
        "        if gap < 0 or gap > _RAMP_MAX_GAP_SEC:\n"
        "            break",
        "        if gap < 0:\n"
        "            break",
        "EVID: 跨轮的残留也串进来(上一轮隔了两小时的加压被算成本轮的台阶)",
    ),
    (
        "server/boot_evidence.py",
        "_RAMP_MAX_GAP_SEC = 30.0",
        "_RAMP_MAX_GAP_SEC = 100000.0",
        "EVID: 跨轮判定形同虚设(间隔 30 秒那道坎没了)",
    ),
    (
        "server/boot_evidence.py",
        "                     int(m.group(\"th\")), line.strip()))",
        "                     int(m.group(\"th\")) + 1, line.strip()))",
        "EVID: 报出来的线程数多了一级(死在 2 线程说成 3 线程)",
    ),
    (
        "server/boot_evidence.py",
        "    chain = [hits[-1]]",
        "    chain = [hits[0]]",
        "EVID: 取的是**最早**那片而不是最后那片(死在哪说反了)",
    ),
    (
        "server/boot_evidence.py",
        "    survived = sorted(set(threads[:-1]))",
        "    survived = sorted(set(threads))",
        "EVID: 扛住的那几级也算进「天花板」里(死的那一片也说自己扛住了 ⇒ "
        "结论自相矛盾)",
    ),
    (
        "server/boot_evidence.py",
        "    _, day, hh, mm, ss = m.groups()\n"
        "    return int(day) * 86400 + int(hh) * 3600 + int(mm) * 60 + int(ss)",
        "    _, day, hh, mm, ss = m.groups()\n"
        "    return int(hh) * 3600 + int(mm) * 60 + int(ss)",
        "EVID: 时间戳只算时分秒(跨天/跨小时的间隔算错 ⇒ 跨轮判定失灵)",
    ),
    # ── 2026-11-02 现场实测加的: **起跑温度** ──
    (
        "server/boot_evidence.py",
        "    start_zone, start_hot, cpu_zone, cpu_hot = _parse_start_temp(text)",
        "    start_zone, start_hot, cpu_zone, cpu_hot = None, None, None, None",
        "EVID: 结论里不说起跑温度(板子**进这一项**时多热 —— "
        "触发条件就是那个数, 不说就等于没测)",
    ),
    (
        "server/boot_evidence.py",
        "    hot_name = max(zones, key=lambda k: zones[k])\n"
        "    hot_val = zones[hot_name]",
        "    cpu_only = {k: v for k, v in zones.items() if \"cpu\" in k}\n"
        "    pool = cpu_only or zones\n"
        "    hot_name = max(pool, key=lambda k: pool[k])\n"
        "    hot_val = zones[hot_name]",
        "EVID: 取**最热 CPU 核区**而不是全板最热核区(口径跟每行的 `hot=` "
        "不一致 ⇒ 「起跑温度」跟「死时最热」不是一个东西, 不能相减)",
    ),
    (
        "server/boot_evidence.py",
        "        if cpu_hot is not None and cpu_zone != start_zone:\n"
        "            extra = f\", CPU 核区当时 {cpu_hot:.1f}℃\"",
        "        if False:\n"
        "            extra = f\", CPU 核区当时 {cpu_hot:.1f}℃\"",
        "EVID: 最热的是 GPU 核区时**不提 CPU 核区当时多少**(把另一半状态"
        "藏起来 ⇒ 读者只看到一个数)",
    ),
    (
        "server/boot_evidence.py",
        "            if start_hot is not None and crit - start_hot < 20:",
        "            if False:",
        "EVID: 裕量本来就薄那句没了(起跑就 92℃ 也不提 ⇒ 读者以为还有余量)",
    ),
    (
        "server/boot_evidence.py",
        "    if not text:\n"
        "        return None, None, None, None\n"
        "    m = _EV_START_TEMP_RE.search(text)",
        "    if not text:\n"
        "        return None, None, None, None\n"
        "    return 'cpub_thermal_zone', 47.0, None, None\n"
        "    m = _EV_START_TEMP_RE.search(text)",
        "EVID: 读不出就**编一个**(47℃ 恒定)—— 等于把没有的数据当数据",
    ),
    # ── 2026-11-02 现场实测加的: **进这一项时板子的状态** ──
    (
        "server/boot_evidence.py",
        "    state = _parse_board_state(text)\n"
        "    if state:",
        "    state = {}\n"
        "    if state:",
        "EVID: 结论里不说「进这一项时板子的状态」(板子**进这一项**时已经被"
        "前面的项目压成什么样 —— 触发条件就在这个数里)",
    ),
    (
        "server/boot_evidence.py",
        "    m = _EV_STATE_RE.search(text)\n"
        "    if not m:\n"
        "        return out",
        "    m = _EV_STATE_RE.search(text)\n"
        "    if not m:\n"
        "        return {\"cores\": 8, \"uptime_sec\": 0, \"load\": [0.0, 0.0, 0.0],"
        " \"mem_avail_mb\": 0}",
        "EVID: 读不出板子状态就**编一个**(老版本包里没这一行 ⇒ 报告里写着"
        "「整机 8 核」这种根本没读到的数)",
    ),
    (
        "server/boot_evidence.py",
        "            out[\"load\"] = [float(x) for x in lm.group(\"v\").split(\"/\")]",
        "            out[\"load\"] = [0.0, 0.0, 0.0]",
        "EVID: 负载均值读出来也是 0(累积压力这个数**整个作废**)",
    ),
    # ── 2026-11-04 加: **死亡时刻只能框, 不能钉** ──
    #
    # 采样行每 EVIDENCE_FSYNC_INTERVAL_SEC 秒才 fsync 一次 ⇒ 两次 fsync
    # 之间的采样点**跟着板子一起没了** ⇒ 盘上最后一行只说明「板子那时
    # 还活着」, 死时刻 ∈ (t_last, t_last+10s]。⇒ 报告**不许**把上界
    # 说成事实(2026-11-04 板子上实测: 最后一行 t=+0.2s, 真实死时刻
    # 不可知 —— 产线拿这句话判「死在起手瞬间」还是「跑半分钟才死」会被带偏)。
    (
        "server/boot_evidence.py",
        "        out.append(\n"
        "            f\"⚠️ **死亡时刻只能框在 起手后 {dt:.1f} 秒 ~ 起手后 \"\n"
        "            f\"{dt + fs:.1f} 秒**: 采样行每 {fs:.0f} 秒才 fsync 一次, \"\n"
        "            f\"两次之间的采样点**跟着板子一起没了**(硬复位把页缓存带走)⇒ \"\n"
        "            f\"**更精确的死时刻这份记录答不了**。\")",
        "        pass",
        "EVID: 报告里**不再说死时刻只能框** ⇒ 读者以为 t=+0.2s 就是死的"
        "那一刻(其实只知道板子那时还活着)",
    ),
    (
        "server/boot_evidence.py",
        "            f\"{dt + fs:.1f} 秒**: 采样行每 {fs:.0f} 秒才 fsync 一次, \"",
        "            f\"{dt:.1f} 秒**: 采样行每 {fs:.0f} 秒才 fsync 一次, \"",
        "EVID: 框的**上界算错**(不加上那个 fsync 间隔 ⇒ 上界等于下界 ⇒ "
        "等于又把死亡时刻钉死了)",
    ),
    (
        "server/boot_evidence.py",
        "        out.append(\n"
        "            f\"最后一份**落在盘上**的记录停在 t=+{last['t']:.1f}s ⇒ \"\n"
        "            f\"板子**那时还活着**, 之后就再没动静了。\")",
        "        out.append(\n"
        "            f\"板子死在 t=+{last['t']:.1f}s(加压跑到一半)。\")",
        "EVID: 连续模式又把最后一行的时间说成死亡时刻(2026-11-04 那次实测"
        "走的就是这条分支)",
    ),
    (
        "server/boot_evidence.py",
        "        out.append(\n"
        "            f\"⚠️ **死亡时刻只能框在 t=+{last['t']:.1f}s ~ \"\n"
        "            f\"t=+{last['t'] + fs:.1f}s**: 采样行每 {fs:.0f} 秒才 fsync \"",
        "        out.append(\n"
        "            f\"⚠️ **死亡时刻只能框在 t=+{last['t']:.1f}s ~ \"\n"
        "            f\"t=+{last['t']:.1f}s**: 采样行每 {fs:.0f} 秒才 fsync \"",
        "EVID: 连续模式框的上界算错(同上)",
    ),
    (
        "server/boot_evidence.py",
        "    fs = float(_FS)",
        "    fs = 0.0",
        "EVID: fsync 间隔读成 0 ⇒ 框的上界等于下界 ⇒ 死亡时刻又被钉死了",
    ),
    # ── 2026-11-11: 累积日志切段 + 开机 ID 护栏 ──
    (
        "server/boot_evidence.py",
        "                seg = _previous_boot_segment(raw)\n"
        "                if seg is None:",
        "                seg = raw\n"
        "                if seg is None:",
        "EVID: 累积日志**不切段**了(老账里那次「有序关机」把这一次的硬复位"
        "翻成「软重启」⇒ 死在加压途中的板子判成不赖板子 ⇒ **放行坏板子**)",
    ),
    (
        "server/boot_evidence.py",
        "    if board_rebooted_since_evidence(run_id, text=text) is False:",
        "    if False:",
        "EVID: 开机 ID 那道护栏被删了(板子没重启过、只是服务自己没了 ⇒ "
        "也判板子不合格 ⇒ **冤枉好板**)",
    ),
    (
        "server/boot_evidence.py",
        "    return rec_boot != cur_boot",
        "    return None",
        "EVID: 「板子重启过没有」**永远说不知道**(判不出软硬那档永远不敢"
        "判失败 ⇒ 死在加压途中的板子又被放行)",
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
                 os.path.join(os.path.join(dst, "config.yaml")))
    os.makedirs(os.path.join(dst, "tests"), exist_ok=True)
    for extra in ("test_boot_evidence.py", "_tmp.py"):
        shutil.copy2(os.path.join(REPO, "tests", extra),
                     os.path.join(dst, "tests", extra))

    outp = os.path.join(dst, rel)
    os.makedirs(os.path.dirname(outp), exist_ok=True)
    with open(outp, "w", encoding="utf-8") as f:
        f.write(new_text)


def run_one(work):
    env = dict(os.environ)
    env["PYTHONPATH"] = work
    proc = subprocess.run(
        [PY, "-m", TEST],
        cwd=work, env=env, capture_output=True, text=True, timeout=300,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def main():
    root = mkdtemp(prefix="mut_bootev_")
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

        code, out = run_one(work)
        fails = out.count("✗")
        if code != 0 and fails == 0:
            # ⚠️ **红了但一个 ✗ 都没有** —— 那多半不是断言把它咬住的, 而是
            #   测试自己崩了(语法错/导入错/异常)。这不算「咬得动」。
            print(f"\n✗ {desc}\n   → 红了, 但一个 ✗ 都没有 (exit={code}) —— "
                  f"这是**崩了**, 不是被断言咬住的, 不算数:\n{out[-800:]}")
            bad += 1
        elif code != 0:
            print(f"\n✓ {desc}\n   → 测试变红 (exit={code}, {fails} 项失败) —— 咬得动")
        else:
            print(f"\n✗ {desc}\n   → 测试还是绿的! 这个变异没被守住")
            bad += 1

    print("\n" + ("全部变异都被抓住了 ✓" if bad == 0
                  else f"有 {bad} 个变异没被抓住 ✗"))
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
