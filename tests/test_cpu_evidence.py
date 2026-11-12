"""回归测试: 板子死在 CPU 加压半路时, **现场要留下来** (§9.3)。

**起因 (2026-10-30 新板实测)**
    换了板子 A10010000000058, 装了新包之后**还是重启**, 而且这次死得更靠前 ——
    死在 **CPU 压力** 那一项上, **起手一秒内**。报告上只有一句
    「服务重启, 本项没跑完」:
      · 温度、负载**一条都没有** —— 指标是这一项**跑完**才落库的,
        板子跟进程一起没了 ⇒ 什么数字都没留下;
      · 死在第几秒、几线程、当时多少度、负载多少 —— **全都不知道**。

    于是加了两样东西:
      ① **逐秒落文件**的「加压现场记录」(`logs/cpu_evidence_run<N>.txt`) ——
         **边跑边写 + 每行立刻 flush**, 板子死了, 已经写下去的行还在;
      ② **逐级加压** (1 → 2 → 4 → 8) —— 板子在哪一级扛不住, 那一级
         就是它的天花板, 而不是一把满载直接暴毙。

**两条硬约束**
    ① **一个字都不往历史库里写** —— 证据落在独立的文件里, 跟 boot_evidence
       一个套路。改历史记录要单独授权, 这个机制不该、也不会去动历史库。
    ② **没有记录就不画那一节** —— 编一段「疑似过热」比没有更糟。

这个测试**不碰真库、不碰真硬件**: 数据目录换成临时目录, 温度/负载的读取
换成假的, stress-ng 换成假的。
跑法: .venv/bin/python -m tests.test_cpu_evidence
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import boot_evidence as be                # noqa: E402
from server import report as report_mod              # noqa: E402
from server.config import Config                     # noqa: E402
from server.tests import cpu as cpu_mod              # noqa: E402
from tests._tmp import mkdtemp                       # noqa: E402

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
DATA_DIR = mkdtemp(prefix="aging-cpuev-")
os.environ["AGING_DATA_DIR"] = DATA_DIR

_SAVED_CFG = Config._data


def set_cfg(cfg):
    Config._data = cfg


section("【1】逐级加压: 1 → 2 → 3 → 4 → 8, 不是一把满载")

# ⚠️ 2026-11-02: 台阶从 1→2→4→8 改成 **1→2→3→4→8** ——
# 实测新板(SN A10000000000010)扛住了 1 线程和 2 线程, 死在 4 线程起手
# 约 1 秒 ⇒ 「2 还是 4」之间卡在哪, 报告上只能猜。补一级 **3** 之后,
# 「3 线程活着、4 线程死」才是这块板子的**天花板**(缺陷特征)。
check(cpu_mod._ramp_threads(8, 0) == 1, "第 1 片用 1 个线程", cpu_mod._ramp_threads(8, 0))
check(cpu_mod._ramp_threads(8, 1) == 2, "第 2 片用 2 个线程", cpu_mod._ramp_threads(8, 1))
check(cpu_mod._ramp_threads(8, 2) == 3, "第 3 片用 3 个线程(2026-11-02 补的一级)",
      cpu_mod._ramp_threads(8, 2))
check(cpu_mod._ramp_threads(8, 3) == 4, "第 4 片用 4 个线程", cpu_mod._ramp_threads(8, 3))
check(cpu_mod._ramp_threads(8, 4) == 8, "第 5 片才到满载 8 线程", cpu_mod._ramp_threads(8, 4))
check(cpu_mod._ramp_threads(8, 9) == 8, "再往后封顶在 8(不越界)", cpu_mod._ramp_threads(8, 9))
check(cpu_mod._ramp_threads(1, 0) == 1, "只有一颗核时就 1 个线程", cpu_mod._ramp_threads(1, 0))
check(cpu_mod._ramp_threads(4, 3) == 4, "4 核机器第 4 片到顶", cpu_mod._ramp_threads(4, 3))
check(cpu_mod._ramp_threads(4, 4) == 4, "4 核机器第 5 片还是 4(不超)", cpu_mod._ramp_threads(4, 4))
check(cpu_mod._ramp_threads(4, 2) == 3, "4 核机器第 3 片是 3(不是直接跳到 4)",
      cpu_mod._ramp_threads(4, 2))
check(cpu_mod._ramp_threads(0, 0) == 1, "0 也不炸(兜底 1)", cpu_mod._ramp_threads(0, 0))
# 台阶必须**单调不减**且**封顶在 total** —— 爬坡爬出倒退或超量都是 bug
seq = [cpu_mod._ramp_threads(8, i) for i in range(12)]
check(seq == sorted(seq), "台阶单调不减", seq)
check(all(v <= 8 for v in seq), "任何一片都不超过总核数", seq)
check(seq[0] == 1 and seq[-1] == 8, "从 1 爬到满载", seq)

# ⚠️ 2026-11-02: **片数不够时台阶要压缩, 但满载那一片必须留着** ——
# 产线默认 60 秒 = 12 片, 台阶是 1,2,3,4,8,8,8,8,8,8,8,8(完整 5 级);
# 可要是只跑 20 秒(4 片), 照老规矩一级一级爬 ⇒ 最后一片才 4 线程 ⇒
# **满载根本没跑到** ⇒ 「板子在 8 线程下会死」测不出来 ⇒ **板子假通过**。
check([cpu_mod._ramp_threads(8, i, 12) for i in range(12)]
      == [1, 2, 3, 4, 8, 8, 8, 8, 8, 8, 8, 8],
      "12 片(产线默认): 完整 5 级台阶, 满载 8 片",
      [cpu_mod._ramp_threads(8, i, 12) for i in range(12)])
short = [cpu_mod._ramp_threads(8, i, 4) for i in range(4)]
check(short == [1, 2, 4, 8], "4 片(短跑): 台阶压缩了, 但**最后一片仍是满载**", short)
check(short[0] == 1, "短跑的起步还是 1 线程(板子死在起手一秒内, 起步必须轻)", short)
check(short[-1] == 8, "短跑也得真的满载过一次(不然板子假通过)", short)
check([cpu_mod._ramp_threads(8, i, 2) for i in range(2)] == [1, 8],
      "只有 2 片: 1 线程起手 + 满载收尾",
      [cpu_mod._ramp_threads(8, i, 2) for i in range(2)])
check([cpu_mod._ramp_threads(8, i, 3) for i in range(3)] == [1, 4, 8],
      "3 片: 丢中间那级, 离天花板最近那级留着",
      [cpu_mod._ramp_threads(8, i, 3) for i in range(3)])
for n in (2, 3, 4, 5, 6, 12):
    s2 = [cpu_mod._ramp_threads(8, i, n) for i in range(n)]
    check(s2 == sorted(s2), f"{n} 片: 压缩后仍然单调不减", s2)
    check(s2[-1] == 8, f"{n} 片: 最后一片一定是满载", s2)
    check(s2[0] == 1, f"{n} 片: 第一片一定是 1 线程", s2)


section("【2】现场记录: 边跑边写, 每行立刻落盘")

set_cfg({})
ev = cpu_mod._EvidenceLog(42)
check(ev.path is not None, "记录文件建出来了", ev.path)
check(str(DATA_DIR) in str(ev.path),
      "落在临时数据目录里(不是仓库里, 更不是真数据目录)", ev.path)
check(os.path.basename(str(ev.path)) == "cpu_evidence_run42.txt",
      "文件名带 run_id(每一轮一份)", ev.path)

# ⚠️ **关键**: 还没 close, 文件里就已经有内容了 ——
#    板子随时可能死, 内容必须**当场**在盘上, 不是攒在缓冲区里。
ev.write(4, 71.2, {"cpul": 68.9, "cpub": 71.2, "gpu": 55.0}, 99.8,
         freq=1800.0, note="片开始")
on_disk = open(ev.path, encoding="utf-8").read()
check("threads=4" in on_disk, "写的那一行当场就在盘上(没有攒在缓冲区)", on_disk)
check("hot=71.2" in on_disk, "最热核区温度记下来了", on_disk)
check("cpub=71.2" in on_disk, "每个核区都记下来了(不只最热的那个)", on_disk)
check("load=99.8" in on_disk, "负载记下来了", on_disk)
check("freq=1800MHz" in on_disk,
      "频率也记下来了(电流台阶跟负载台阶是不是同一瞬间)", on_disk)
check("t=+" in on_disk, "有经过秒数(知道死在第几秒)", on_disk)

# ⚠️ **采样密度**: 这条断言在 2026-11-03 **反过来了**。
#
# 原来要求「密到 250ms 以内」, 理由是「板子死在起手一秒内, 1 秒一拍会漏掉
# 关键那一票」—— 这个理由**本身没错**, 但 200ms 一行的采样**是打包之后才
# 加的**, 而且它**就是「应用比人工老化更重」的一部分**: 每 200ms 一次,
# 每次要读 /proc/stat + 5 个 thermal zone + 8 个 cpufreq ⇒ 加压窗口里
# **应用自己**就占着一颗核的一小截, 而那正是板子电流最高、最吃不消的时候。
# **人工在终端跑老化的时候, 板上没有这些东西。**
#
# ⇒ 现在采样间隔是 2 秒: 死在哪一秒仍然看得见, 而采样本身对板子的额外
#   负担降到 1/10。「起跑」那一行是**立刻**落盘的 ⇒ 「死在第几片」这个
#   最关键的凭据一点没丢。
#
# ⚠️ **判温的口径一个字没动** —— 判温走 tick 的 1Hz, 跟这个采样器是两条
#    线(见 cpu.py 里 EVIDENCE_SAMPLE_INTERVAL_SEC 那段注释)。
check(cpu_mod.EVIDENCE_SAMPLE_INTERVAL_SEC >= 2.0,
      "采样间隔 ≥ 2 秒 —— 别让「加记录」把加压窗口本身搞重(2026-11-03)",
      f"{cpu_mod.EVIDENCE_SAMPLE_INTERVAL_SEC}s")

ev.write(8, 73.0, {"cpul": 70.0, "cpub": 73.0}, 100.0)
ev.close()
body = open(ev.path, encoding="utf-8").read()
# 文件头上有那几行「每轮不变」的信息(板子型号/起始温度/过热阈值/起始电压)
# —— 算的时候只数**数据行**, 头不算
data_lines = [ln for ln in body.strip().splitlines()
              if not ln.startswith("#")]
check(len(data_lines) == 2, "两行就是两行", body)
check("片开始" in body, "特殊事件(片开始)也记下来了", body)

# 读回来
check(be.read_cpu_evidence(42) == body, "读回来跟写进去一模一样")
check(be.read_cpu_evidence(999) == "", "没有记录的那一轮 ⇒ 空串(不是异常)")
check(be.read_cpu_evidence(None) == "", "run_id 是 None ⇒ 空串")

section("【3】写不了就当没有 —— **绝不能因为记证据把测试搞挂**")

# 目录不可写 ⇒ _EvidenceLog 自己降级成"什么都不写"
_orig_mkdir = cpu_mod.cpu_evidence_path


def boom_path(run_id):
    # 真实场景: logs/ 目录建不出来(只读根分区 / 权限不够) ⇒ OSError
    raise OSError(13, "Permission denied")


cpu_mod.cpu_evidence_path = boom_path
try:
    ev2 = cpu_mod._EvidenceLog(43)
    check(ev2.path is None, "路径算不出来 ⇒ 不建文件", ev2.path)
    ev2.write(1, 50.0, {"cpub": 50.0}, 10.0)   # 不许抛
    ev2.close()
    check(True, "写记录失败时**不抛异常**(只是没记录)")
except Exception as e:      # noqa: BLE001
    check(False, "写记录失败时**不抛异常**(只是没记录)", repr(e))
finally:
    cpu_mod.cpu_evidence_path = _orig_mkdir

# ⚠️ 另一条路径: **文件写的时候才失败**(磁盘满了 / 句柄被关掉 / 只读)。
#    这时 `write()` 里的异常处理必须兜住 —— 记录是加分项, 写不进去就当
#    没有, 绝不能把整个 CPU 项搞挂(那才是真事故)。
ev4 = cpu_mod._EvidenceLog(44)
try:
    ev4._fh.close()          # 假装句柄没了(磁盘/文件系统出问题的替身)
    ev4.write(1, 50.0, {"cpub": 50.0}, 10.0)
    ev4.close()
    check(True, "写文件时才失败 ⇒ 也**不抛异常**(只是没记录)")
except Exception as e:      # noqa: BLE001
    check(False, "写文件时才失败 ⇒ 也**不抛异常**(只是没记录)", repr(e))

# run_id 是 None ⇒ 直接不记, 不生成 cpu_evidence_runNone.txt 这种鬼文件
ev3 = cpu_mod._EvidenceLog(None)
check(ev3.path is None, "run_id 是 None ⇒ 不建文件", ev3.path)
ev3.write(1, 50.0, {}, None)
ev3.close()
check(not os.path.exists(os.path.join(str(DATA_DIR), "logs",
                                      "cpu_evidence_runNone.txt")),
      "不留 cpu_evidence_runNone.txt 这种鬼文件")

section("【3b】文件头记着「每轮不变」的东西: 板子/起始温度/过热阈值/起始电压")

# 假的 thermal + regulator 目录 —— 别拿**开发机**的真数据当被测板子的
fake_root = mkdtemp(prefix="aging-fakesys-")
fake_thermal = os.path.join(fake_root, "thermal")
fake_reg = os.path.join(fake_root, "regulator")
zdir = os.path.join(fake_thermal, "thermal_zone0")
os.makedirs(zdir, exist_ok=True)
with open(os.path.join(zdir, "type"), "w") as f:
    f.write("cpub_thermal_zone")
with open(os.path.join(zdir, "temp"), "w") as f:
    f.write("59475")
for idx, (kind, val) in enumerate((("passive", "70000"), ("critical", "110000"))):
    with open(os.path.join(zdir, f"trip_point_{idx}_temp"), "w") as f:
        f.write(val)
    with open(os.path.join(zdir, f"trip_point_{idx}_type"), "w") as f:
        f.write(kind)
for i, (nm, uv) in enumerate((("vdd_sys", "900000"),
                              ("axp2202-aldo3", "3300000"))):
    d = os.path.join(fake_reg, f"regulator.{i}")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "name"), "w") as f:
        f.write(nm)
    with open(os.path.join(d, "microvolts"), "w") as f:
        f.write(uv)

_orig_thermal = cpu_mod.THERMAL_DIR
_orig_reg = cpu_mod.REGULATOR_DIR
cpu_mod.THERMAL_DIR = fake_thermal
cpu_mod.REGULATOR_DIR = fake_reg
try:
    ev5 = cpu_mod._EvidenceLog(77)
    ev5.write(1, 59.5, {"cpub_thermal_zone": 59.5}, 10.0,
              rails=cpu_mod._read_rails())
    ev5.write(8, 61.0, {"cpub_thermal_zone": 61.0}, 99.0,
              rails={**cpu_mod._read_rails(), "vdd_sys": 850})
    ev5.write(8, 61.2, {"cpub_thermal_zone": 61.2}, 99.5,
              rails=cpu_mod._read_rails())
    ev5.close()
    body5 = open(ev5.path, encoding="utf-8").read()
finally:
    cpu_mod.THERMAL_DIR = _orig_thermal
    cpu_mod.REGULATOR_DIR = _orig_reg

check("# 加压现场记录" in body5 and "板子" in body5,
      "文件头写着这是什么、板子是谁", body5[:200])
check("起始温度" in body5 and "cpub_thermal_zone=59.5" in body5,
      "起始温度记下来了(死的时候比这个高了多少, 一眼能看出来)", body5[:300])
check("过热阈值" in body5 and "critical@110℃" in body5,
      "过热阈值记下来了(没有它, 读者看着 59℃ 根本不知道算不算热)",
      body5[:400])
check("vdd_sys=900" in body5, "起始供电电压记下来了", body5[:600])
check("axp2202-aldo3=3300" in body5,
      "连 WLAN/BT 共用的那根轨也记下来了(厂商 BT 失败循环切的就是它)",
      body5[:600])
check("dV=vdd_sys:900→850" in body5,
      "电压**变了**就记下来(电压台阶跟负载台阶是不是同一瞬间)", body5)
check(body5.count("dV=") == 2,
      "变回原值也算一次台阶(来回切也得看得见)", body5)

section("【3c】文件头还记着**调速策略**(governor) —— 板子是不是一直满频率跑")


# 假的 cpufreq 目录 —— 开发机上的真值跟被测板子没关系
fake_cpu = os.path.join(fake_root, "cpu")
for cname, gov, mx in (("cpu0", "performance", "1800000"),
                       ("cpu4", "ondemand", "1400000")):
    cdir = os.path.join(fake_cpu, cname, "cpufreq")
    os.makedirs(cdir, exist_ok=True)
    with open(os.path.join(cdir, "scaling_governor"), "w") as f:
        f.write(gov + "\n")
    with open(os.path.join(cdir, "cpuinfo_max_freq"), "w") as f:
        f.write(mx)

_orig_cpufreq = cpu_mod.CPUFREQ_BASE
cpu_mod.CPUFREQ_BASE = fake_cpu
try:
    gov_info = cpu_mod._read_cpufreq_info()
    ev6 = cpu_mod._EvidenceLog(78)
    ev6.write(1, 50.0, {"cpub_thermal_zone": 50.0}, 30.0)
    ev6.close()
    body6 = open(ev6.path, encoding="utf-8").read()
finally:
    cpu_mod.CPUFREQ_BASE = _orig_cpufreq

check("cpu0: performance" in gov_info and "cpu4: ondemand" in gov_info,
      "各核的 governor 都读到了", gov_info)
check("上限 1800MHz" in gov_info, "频率上限也读到了(换算成 MHz)", gov_info)
check("调速策略" in body6 and "performance" in body6,
      "文件头里写着调速策略(板子一直满频率跑 ⇒ 电流一直很高)", body6[:600])

section("【3d】文件头记着**进这一项时板子的状态** —— 核数/开机时长/负载均值/可用内存")


# ⚠️ 2026-11-02 现场实测逼出来的: 板子 A10000000000010 **单独**跑 CPU
#    加压**扛住了**, 整轮老化却**每次**死在 CPU 项 ⇒ 触发条件是
#    「**进这一项时板子已经是什么状态**」。
#    而现场拿到的**起跑温度是凉的**(46.9℃)⇒ **「热」这条线出局** ⇒
#    光看温度不够, 得把**状态本身**记下来: 核数(加压台阶封顶在几线程)、
#    开机时长、1/5/15 分钟负载均值(累积压力)、可用内存。
_state_stub = {"cores": 8, "uptime_sec": 225.0, "load": [4.62, 3.15, 1.88],
               "mem_avail_mb": 2874}
_orig_state = cpu_mod._read_board_state
try:
    cpu_mod._read_board_state = lambda: dict(_state_stub)
    head3d = cpu_mod._format_evidence_header()
    ev3d = cpu_mod._EvidenceLog(79)
    ev3d.write(1, 50.0, {"cpub_thermal_zone": 50.0}, 30.0)
    ev3d.close()
    body3d = open(ev3d.path, encoding="utf-8").read()
finally:
    cpu_mod._read_board_state = _orig_state

check("进这一项时的板子状态" in head3d,
      "文件头有「进这一项时的板子状态」这一行", head3d)
check("核数 8" in head3d,
      "记了**核数**(加压台阶封顶在几线程全看它 —— 这块 A527 是 8 核)",
      head3d)
check("开机 3分45秒" in head3d, "记了**开机时长**(板子已经连续跑了多久)",
      head3d)
check("负载均值(1/5/15分) 4.62/3.15/1.88" in head3d,
      "记了 1/5/15 分钟负载均值(累积压力的直接度量)", head3d)
check("可用内存 2874MB" in head3d,
      "记了**可用内存**(前面的项留了多少占用)", head3d)
check("进这一项时的板子状态" in body3d,
      "这一行真的写进了证据文件(不是只算了个寂寞)", body3d[:900])

# 读不到的时候 ⇒ **一行都不写**(老版本包 / proc 读不了)
try:
    cpu_mod._read_board_state = lambda: {}
    head3d_empty = cpu_mod._format_evidence_header()
finally:
    cpu_mod._read_board_state = _orig_state
check("进这一项时的板子状态" not in head3d_empty,
      "读不到板子状态 ⇒ **一行都不写**(不编)", head3d_empty)

# 真机上的读取不许抛异常
try:
    st_real = cpu_mod._read_board_state()
    check(isinstance(st_real, dict),
          "真机上读板子状态 ⇒ 返回一个 dict(不许抛异常)", repr(st_real))
    check("cores" in st_real,
          "核数读得到(`os.cpu_count()`)", repr(st_real))
except Exception as e:      # noqa: BLE001
    check(False, "读板子状态不该抛异常", repr(e))

section("【4】报告里多一节「加压现场记录」; 没有就不画")


def make_run():
    return {
        "id": 42,
        "sn": "SN-CPU-EV-0001",
        "operator": "产线甲",
        "status": "stopped",
        "started_at": 1000,
        "finished_at": None,
        "summary": {"total": 3, "passed": 2, "failed": 0, "error": 0,
                    "skipped": 0, "not_run": 1, "planned": 4},
        "config": {"tests": {}},
        "error": "服务重启, 这一轮中途夭折(没跑完)",
    }


html = report_mod.render_html(make_run(), [], "SN-CPU-EV-0001_20261030_100000.pdf",
                               cpu_evidence=body)
check("加压现场记录" in html, "报告里有「加压现场记录」这一节")
check("threads=4" in html, "现场记录原文进了报告")
check("每 200 毫秒" in html, "写明了这是多密地记的(读者才知道这些行之间隔多久)")

html_no = report_mod.render_html(make_run(), [], "SN-CPU-EV-0001_20261030_100000.pdf")
check("加压现场记录" not in html_no,
      "**没有记录就不画那一节** —— 不编「疑似过热」", html_no[:0])

section("【5】板子死在哪一项 —— 报告要**点名**")


class _Stop(Exception):
    pass


# 模拟 db.finalize_interrupted_run 写下的那一行:
#   status='stopped', error='服务重启, 本项没跑完'
results = [
    {"test_name": "gpu", "status": "passed", "error": None,
     "duration_sec": 105, "metrics": {}},
    {"test_name": "cpu", "status": "stopped",
     "error": "服务重启, 本项没跑完",
     "duration_sec": 1, "metrics": {}},
]
html2 = report_mod.render_html(make_run(), results, "x.pdf",
                               boot_evidence="[上一次启动的 journal]\n看门狗复位",
                               cpu_evidence=body)
check("板子为什么死" in html2, "有内核日志时有「板子为什么死」这一节")
# ⚠️ 断言要**足够具体** —— 另一节的说明文字里也有「板子死在加压半路时」,
#    只查「板子死在」会被那段话蒙混过关(变异测试就是这么咬出来的)。
NAMED = "板子死在 <strong>CPU 压力</strong>"
check(NAMED in html2,
      "**点名**板子死在 CPU 压力 这一项上(不用自己翻明细表)", html2[:0])

# 没有那一行(比如是工人自己按的停止) ⇒ 不点名
html3 = report_mod.render_html(make_run(),
                                [{"test_name": "cpu", "status": "stopped",
                                  "error": "用户取消", "duration_sec": 1,
                                  "metrics": {}}],
                                "x.pdf",
                                boot_evidence="[上一次启动的 journal]\n看门狗复位")
check("板子死在 <strong>" not in html3,
      "不是「服务重启」造成的 ⇒ 不点名(别冤枉人)", html3[:0])

section("【5b】**硬复位**: 报告要说「被测设备不合格」, 并给出「死在第几级」的结论")

# 实样本(SN A10000000000010, 2026-11-02 实测): 板子扛住了 1 线程和
# 2 线程, **死在 4 线程那一片起手约 1 秒**, 死时 cpub 57.1℃。
DEATH_EVIDENCE = """# 加压现场记录 —— 板子 A10000000000010   采样间隔 0.2s
# 过热阈值(内核 thermal 驱动): cpub_thermal_zone: passive@70℃ passive@90℃ critical@110℃
t=+10.9s threads=2 hot=55.4 load=39.2% freq=1800MHz
t=+10.9s threads=4 hot=54.9 load=66.4% freq=1800MHz dV=axp2202-dcdc1:1000→1050 起跑 第 3/12 片(4 线程, 缓启动)
t=+11.0s threads=4 hot=53.8 freq=1200MHz 起跑 第 3/12 片(4 线程, 缓启动)
t=+11.5s threads=4 hot=58.4 load=56.4% freq=1800MHz
t=+11.9s threads=4 hot=57.1 load=63.5% freq=1800MHz dV=axp2202-dcdc1:1000→1150
"""

hard_results = [
    {"test_name": "gpu", "status": "passed", "error": None,
     "duration_sec": 105, "metrics": {}},
    {"test_name": "cpu", "status": "failed",
     "error": be.HARD_RESET_ITEM_ERROR,
     "duration_sec": 12, "metrics": {}},
]
html4 = report_mod.render_html(make_run(), hard_results, "x.pdf",
                               boot_evidence="[上一次启动的 journal]\n【上一次是硬复位】",
                               cpu_evidence=DEATH_EVIDENCE)
check("硬复位" in html4, "报告点明是硬复位", html4[:0])
check("不合格" in html4,
      "硬复位 ⇒ 报告明说「被测设备不合格」(不是「不代表不合格」)", html4[:0])
check("板子死在哪一级加压" in html4,
      "多一节「板子死在哪一级加压 —— 结论」", html4[:0])
check("第 3/12 片" in html4 and "4 线程" in html4,
      "结论里点名了死在第几片、几线程", html4[:0])
check("热不是原因" in html4, "结论里明说热不是原因(57.1℃ 离 110℃ 还远)", html4[:0])

# 软重启(有人/有服务重启了)⇒ **不给**「死在第几级」的结论 ——
# 那一轮不是死在加压上的, 最后一行只是「跑到哪儿了」。
soft_results = [
    {"test_name": "cpu", "status": "stopped", "error": be.SOFT_ITEM_ERROR,
     "duration_sec": 1, "metrics": {}},
]
html5 = report_mod.render_html(make_run(), soft_results, "x.pdf",
                               boot_evidence="[上一次启动的 journal]\n关机序列",
                               cpu_evidence=DEATH_EVIDENCE)
check("板子死在哪一级加压" not in html5,
      "软重启 ⇒ 不给「死在第几级」的结论(别编)", html5[:0])

section("【5c】**兜底**: 现场记录读不出来时, 报告仍要说出「死在几线程那一片」")

# ⚠️ 2026-11-02 13:50 实测踩的坑: 新板死在 **2 线程**那一片, 而
#    「加压现场记录」那份文件**整个读不出来**(它是板子死的那一刻写的,
#    尾巴只写了一半 ⇒ 严格 UTF-8 解码抛异常 ⇒ **整份证据全丢**)⇒
#    报告里「板子死在哪一级加压」那一节**整个不见了** —— 恰恰是最该有
#    证据的一次, 证据却没了。
#
# ⇒ 兜底: 从**上一次启动的日志尾巴**里读 sudo 命令行里的 `stress-ng --cpu N`。
#    那是 sudo 记的, 板子死后还在 ⇒ 至少能说出「死在几线程那一片」。
#
#    实样本(报告 A10000000000010_20261102_135211.pdf 的「板子为什么死」):
#    扛住 1 线程, **死在 2 线程那一片**(13:50:40.227 起手, 之后再没有
#    下一片的命令行 ⇒ 板子没活到 3 线程那片)。
JOURNAL_2TH = """Nov 02 13:50:34.990424 ubuntu2004 sudo[45208]:     User : TTY=unknown ; PWD=/usr/lib/aging-test ; USER=root ; COMM
AND=/usr/bin/timeout -k 5.0 35.0 /usr/bin/stress-ng --cpu 1 --timeout 5.0s --metrics-brief
Nov 02 13:50:35.032646 ubuntu2004 stress-ng[45216]: invoked with '/usr/bin/stress-n' by user 0
Nov 02 13:50:40.133556 ubuntu2004 sudo[45208]: pam_unix(sudo:session): session closed for user root
Nov 02 13:50:40.227775 ubuntu2004 sudo[45246]:     User : TTY=unknown ; PWD=/usr/lib/aging-test ; USER=root ; COMM
AND=/usr/bin/timeout -k 5.0 35.0 /usr/bin/stress-ng --cpu 2 --timeout 5.0s --metrics-brief
Nov 02 13:50:40.259515 ubuntu2004 stress-ng[45252]: invoked with '/usr/bin/stress-n' by user 0
"""

# **现场记录整个读不出来**(cpu_evidence="") ⇒ 报告必须**兜底**。
html6 = report_mod.render_html(make_run(), hard_results, "x.pdf",
                               boot_evidence="[上一次启动的 journal]\n"
                                             "【上一次是硬复位】\n" + JOURNAL_2TH,
                               cpu_evidence="")
check("板子死在哪一级加压" in html6,
      "现场记录没有 ⇒ 「板子死在哪一级加压」这一节**照样在**(别整节消失)",
      html6[:0])
check("2 线程" in html6,
      "兜底结论点出了死在 **2 线程**那一片(从日志里的 --cpu N 读出来的)",
      html6[:0])
check("1 线程" in html6,
      "兜底结论也说清了前面扛住的是 1 线程", html6[:0])
check("第 3/12 片" not in html6,
      "兜底结论**不编**片数(那要现场记录才说得清)", html6[:0])

# 日志附录里那一项的说明也要改口径: 不是「工具出 bug」, 是「板子打断了它」
def _log_section(html, keyword):
    """从报告 HTML 里抠出**逐项日志附录**里某一项的那一节。"""
    for part in html.split("===== ")[1:]:
        if keyword in part.split("\n", 1)[0]:
            return part
    return ""


cpu_log = _log_section(html6, "CPU")
check("本项被板子硬复位打断" in cpu_log,
      "日志附录里明说这一项是被板子硬复位打断的", cpu_log[:0])
check("本项没有产生任何日志" not in cpu_log,
      "**不是**「本项没有产生任何日志 —— 属异常」(板子打断 ≠ 工具异常)",
      cpu_log[:0])

# 两个来源都没有 ⇒ 一句都不编(宁可没结论, 也别写个错的)
html7 = report_mod.render_html(make_run(), hard_results, "x.pdf",
                               boot_evidence="[上一次启动的 journal]\n"
                                             "【上一次是硬复位】",
                               cpu_evidence="")
check("板子死在哪一级加压" not in html7,
      "两个来源都没有 ⇒ 不画这一节(不编)", html7[:0])

# 现场记录**在** ⇒ 优先用它(它比日志细: 有温度/频率/片数)
html8 = report_mod.render_html(make_run(), hard_results, "x.pdf",
                               boot_evidence="[上一次启动的 journal]\n"
                                             "【上一次是硬复位】\n" + JOURNAL_2TH,
                               cpu_evidence=DEATH_EVIDENCE)
check("第 3/12 片" in html8,
      "现场记录在 ⇒ 用它(更细: 片数/温度/频率)", html8[:0])
check("死在 <strong>4 线程" not in html8 and "4 线程" in html8,
      "现场记录在 ⇒ 结论以它为准", html8[:0])

section("【5d】**落盘要 fsync** —— 光 flush 到页缓存, 硬复位会把它一起带走")

# ⚠️⚠️ **2026-11-02 13:50 实测踩的最大的坑**: 板子被**硬复位**打死的 ⇒
#    **内存里的页缓存整个丢失** ⇒ 只 `flush()`(交给内核缓存)的证据
#    **跟着板子一起没了**。
#    实测: 板子上 `cpu_evidence_run3.txt` 是 **0 字节** —— 文件建出来了
#    (元数据落盘了), 但一行数据都没落盘 ⇒ 报告里「加压现场记录」那一节
#    **整个不见**, 恰恰是最该有证据的一次。
# ⇒ 光 flush 不够, 必须 **fsync**(强制写进存储)。
#
# 这个测试把 `os.fsync` 换成计数器 —— 断言「写证据的时候真的调了 fsync」。

_fsync_calls = []
_orig_fsync = os.fsync


def _counting_fsync(fd):
    _fsync_calls.append(fd)
    return _orig_fsync(fd)


section("【5d-1】写一行证据 ⇒ 必须真的 fsync 一次")

ev = cpu_mod._EvidenceLog(4242)
check(ev.path is not None, "证据文件建出来了", ev.path)
os.fsync = _counting_fsync
try:
    ev.write(2, 50.0, {"cpub": 51.0}, 40.0, freq=1800.0,
             note="起跑 第 1/12 片(2 线程, 缓启动)")
    check(len(_fsync_calls) >= 1,
          "「起跑」那一行写完 ⇒ **fsync 了**(它得活过硬复位)", len(_fsync_calls))
    # 采样行: 最多 EVIDENCE_FSYNC_INTERVAL_SEC 秒一次 —— 不是每行都
    # fsync(SD 上慢, 而且**写电流尖峰**要撞上板子最吃力的时候)
    n0 = len(_fsync_calls)
    ev.write(2, 51.0, {"cpub": 52.0}, 41.0, freq=1800.0)
    check(len(_fsync_calls) == n0,
          "刚 fsync 完的间隔内, 采样行**不**再 fsync(别把 SD 写爆)", n0)
    ev.close()
finally:
    os.fsync = _orig_fsync

section("【5d-2】文件头也要 fsync —— 头是「多少度算热」那把尺子")

_fsync_calls.clear()
os.fsync = _counting_fsync
try:
    ev2 = cpu_mod._EvidenceLog(4243)
    check(len(_fsync_calls) >= 1, "建文件时就把头 fsync 了", len(_fsync_calls))
    ev2.close()
finally:
    os.fsync = _orig_fsync

section("【5d-3】报告里要**明说**「现场记录文件在, 却是空的」")

# 造一个 0 字节的现场记录文件(就是板子上的样子)—— **run id 要对上**
# (报告是按这一轮的 run_id 去读那个文件的)
empty_run = dict(make_run())
empty_run["id"] = 55555
empty_path = be.cpu_evidence_path(55555)
empty_path.parent.mkdir(parents=True, exist_ok=True)
empty_path.write_bytes(b"")
check(be.cpu_evidence_state(55555) == be.CPU_EVIDENCE_EMPTY,
      "0 字节的文件 ⇒ 状态是「有, 但是空的」", be.cpu_evidence_state(55555))
check(be.cpu_evidence_state(99999999) == be.CPU_EVIDENCE_NONE,
      "压根没这个文件 ⇒ 状态是「没有」", be.cpu_evidence_state(99999999))

html9 = report_mod.render_html(empty_run, hard_results, "x.pdf",
                               boot_evidence="[上一次启动的 journal]\n"
                                             "【上一次是硬复位】\n" + JOURNAL_2TH,
                               cpu_evidence="")
check("为什么这一节是空的" in html9,
      "报告里**明说**了「加压现场记录」那一节为什么是空的", html9[:0])
check("一个字节都没有" in html9,
      "说清了是「文件在, 但一个字节都没有」(不是「压根没记」)", html9[:0])
check("2 线程" in html9,
      "空文件不影响结论 —— 照样从日志里读出「死在 2 线程那一片」", html9[:0])

# 压根没有那个文件 ⇒ 不画这一节(别乱解释)
html10 = report_mod.render_html(make_run(), hard_results, "x.pdf",
                                boot_evidence="[上一次启动的 journal]\n"
                                              "【上一次是硬复位】\n" + JOURNAL_2TH,
                                cpu_evidence="")
check("为什么这一节是空的" not in html10,
      "压根没那个文件 ⇒ 不解释(没东西可解释)", html10[:0])

# 空文件 **且** 日志里连 `stress-ng --cpu N` 都没有 ⇒ 「死在几线程」也
#    读不出来 ⇒ 说明里**不许**说「2 线程」(那等于编)
html10b = report_mod.render_html(empty_run, hard_results, "x.pdf",
                                 boot_evidence="[上一次启动的 journal]\n"
                                               "【上一次是硬复位】\n",
                                 cpu_evidence="")
check("为什么这一节是空的" in html10b,
      "空文件 ⇒ 照样要解释(不能静悄悄地不画)", html10b[:0])
check("这次也没读出来" in html10b,
      "日志里也读不出「死在几线程」⇒ 说明里**明说不知道**", html10b[:0])
check("2 线程" not in html10b,
      "读不出来的时候不许编「死在 2 线程」", html10b[:0])

section("【6】导出时**自动**带上现场记录 —— 不用每个调用点自己记得传")

captured = {}
_orig_render = report_mod.render_html
_orig_chromium = report_mod.find_chromium


def fake_render_html(run, results, filename="", retest_runs=None,
                     boot_evidence="", cpu_evidence="", **kwargs):
    captured["cpu_evidence"] = cpu_evidence
    captured["run_id"] = run.get("id")
    raise _Stop()


report_mod.render_html = fake_render_html
report_mod.find_chromium = lambda: "/bin/true"
try:
    asyncio.run(report_mod.export_pdf({"id": 42}, []))
except _Stop:
    pass
finally:
    report_mod.render_html = _orig_render
    report_mod.find_chromium = _orig_chromium

check(captured.get("run_id") == 42, "导出时用的是这一轮自己的 id", captured)
check(captured.get("cpu_evidence") == body,
      "现场记录被自动带进报告(旁挂文件 → render_html)",
      captured.get("cpu_evidence"))

section("【7】整项跑一遍: 假 stress-ng, 假温度 —— 记录真的落下来了")

# 假外部依赖: 温度全给固定值, stress-ng 换成 "sleep"
_orig_temp = cpu_mod._read_cpu_temp
_orig_zones = cpu_mod._read_all_zones
_orig_cpu_stat = cpu_mod._read_cpu_stat
_orig_freq = cpu_mod._read_cpu_freq
_orig_find_tool = cpu_mod.find_tool
_orig_sudo_hint = cpu_mod.sudo_hint_if_unavailable
_orig_run_proc = cpu_mod.run_proc_with_progress
_orig_spawn = cpu_mod._spawn_proc
_orig_interval = cpu_mod.EVIDENCE_SAMPLE_INTERVAL_SEC

async def _fake_temp():
    return 60.0


async def _fake_zones():
    return {"cpul": 58.0, "cpub": 60.0, "gpu": 45.0}


# ⚠️ **/proc/stat 也得假** —— 真采样有个坑: 内核 100Hz ⇒ 10ms 一个 tick,
#    而假 stress-ng 是**连着**跑两拍 tick 的, 两次采样极可能落在**同一个
#    tick** 里 ⇒ 相减为 0 ⇒ 算不出负载 ⇒ 「逐秒的行里带着负载」这条断言
#    **时好时坏**(2026-10-30 整套回归里就翻过一次车: 单跑绿, 整套跑红)。
#    测环境的时间基准不能靠运气 —— 自己造一个每次都往前挪的计数器。
_stat_n = {"v": 0}


def _fake_cpu_stat():
    """假的 /proc/stat —— 每次采样往前挪 400 个 tick(4 核 × 100Hz × 1 秒),
    占用率固定 75%。"""
    _stat_n["v"] += 400
    total = _stat_n["v"]
    return (total * 3 // 4, total)


cpu_mod._read_cpu_temp = _fake_temp
cpu_mod._read_all_zones = _fake_zones
cpu_mod._read_cpu_stat = _fake_cpu_stat
cpu_mod._read_cpu_freq = lambda: 1800.0
cpu_mod.find_tool = lambda name: "/usr/bin/" + name
cpu_mod.sudo_hint_if_unavailable = lambda result, name: True

# ⚠️⚠️ **起进程这一步必须换成假的**(2026-11-03 14:59 实测踩过):
# 这个测试原来只把 `run_proc_with_progress`(读 stdout/推进度)换成假的,
# **进程却是真的起过的** —— `sudo -n /usr/bin/stress-ng --cpu 8 --timeout 5.0s`
# 在板子上真跑起来了! 而假的 progress **不等进程退** ⇒ 一片接一片地叠 ⇒
# 八个片 = 八个 8 线程 stress-ng 同时在板上跑 ⇒ **回归测试自己把板子
# 压到掉电复位**(2026-11-03 14:59 实测: 跑完回归, 板子当场硬复位;
# 11:03 那次多半也是同一回事)。
#
# ⇒ 现在起进程走 `cpu_mod._spawn_proc`(2026-11-03 从 `create_subprocess_exec`
#    拎出来的), 这里把它换成假的 ⇒ **这个测试再也不碰真硬件**。
_SPAWNED: list = []


async def _fake_spawn(cmd):
    """假的起进程 —— 只记下命令, **什么都不起**。"""
    _SPAWNED.append(list(cmd))
    return object()      # 假的 progress 压根不看它


cpu_mod._spawn_proc = _fake_spawn

# ⚠️⚠️⚠️ **再加一道「绊线」**(2026-11-03 14:59 第二次踩坑之后加的):
# 光把 `_spawn_proc` 换成假的还不够 —— 万一哪天生产代码又绕过这个口子,
# 直接 `asyncio.create_subprocess_exec(...)`, 板子又要被压复位一次, 而那时
# 测试**看起来还是绿的**(它只看得到自己的假进程)。
#
# ⇒ 把 `asyncio.create_subprocess_exec` 也套一层: **只要命令里带
#    `stress-ng`, 就当场报错, 一个真进程都不许起**; 别的命令照常放行
#    (免得把不相干的测试路径也堵死)。
# ⇒ 绊线一旦被踩, 测试**当场红**, 而不是把板子烤了。
_orig_create_exec = asyncio.create_subprocess_exec
_TRIPWIRE_HITS: list = []


async def _guarded_create_exec(*args, **kw):
    """带绊子的起进程 —— 见上面那段说明。"""
    flat = [str(a) for a in args]
    flat += [str(v) for v in kw.values()]
    if any("stress-ng" in a for a in flat):
        _TRIPWIRE_HITS.append(flat)
        raise RuntimeError(
            "❌ 绊线被踩: 有人绕开 `_spawn_proc` 直接起了 stress-ng —— "
            "测试绝不碰真硬件, 已拦下(命令: %s)" % " ".join(flat))
    return await _orig_create_exec(*args, **kw)


asyncio.create_subprocess_exec = _guarded_create_exec

# ⚠️ **采样间隔调快** —— 生产值(0.2s)配假 stress-ng 的短跑, 攒不出几行;
#    这里调成 10ms, 好在**短的**假运行里也能攒出够多的行来验「采样够密」。
#    生产值本身由上面那条断言单独盯着(不许靠这个假值蒙混)。
cpu_mod.EVIDENCE_SAMPLE_INTERVAL_SEC = 0.01


class _ProcOut:
    rc = 0
    text = "fake stress-ng output"
    killed = False
    # ⚠️ **2026-11-12 加**: `ProcOutcome` 有这个字段(「为什么停下来」:
    #   "" / "cancelled" / "hard_limit"), CPU 项用它写「没跑完」的原因 ⇒
    #   假的也得有, 不然生产代码一取就 AttributeError。
    reason = ""


# 每一片真的发出去的命令 —— 用来验「封顶值调小 ⇒ 真的按小的压」
# (§【8】)。记的是**操作员在日志里看到的那行命令**(`[CPU] 执行: …`),
# 不是我们以为发出去的那个数。
#
# ⚠️ 为什么从日志里抠而不是从进程对象上抠: 这台机器上的 Python 是
#    3.8.10, 它的 `asyncio.subprocess.Process` **没有** `.args`
#    (3.9 才有) ⇒ 抠不到。日志行是**操作员看得见**的东西, 拿它当凭据
#    比拿内部属性更贴切。
_CMD_LOG: list = []


async def fake_run_proc(proc, duration, ctx=None, on_tick=None,
                        hard_limit_sec=None, **kw):
    # 模拟 tick_sec=1 的采样: 走两拍。
    # ⚠️ **每一拍都得 await asyncio.sleep** —— 不让出控制权, 采样器那个
    #    task 一次都轮不上, 记录文件里就只剩「起跑」那一行。生产里
    #    run_proc_with_progress 是按挂钟等的, 一定会让出控制权。
    if on_tick:
        for e in (1.0, 2.0):
            await asyncio.sleep(0.02)
            await on_tick(e)
    return _ProcOut()


cpu_mod.run_proc_with_progress = fake_run_proc

from server.tests.base import TestContext        # noqa: E402


async def _noop_progress(pct, msg):
    pass


async def _noop_log(line):
    # 记下日志行 —— §【8】要从 `[CPU] 执行: … --cpu N …` 里抠出每一片
    # 真的发出去的线程数(那是操作员看得见的凭据)。
    _CMD_LOG.append(line)


async def _not_cancelled():
    return False


async def _noop_metric(key, value):
    pass


ctx = TestContext(
    run_id=77,
    config={},
    # 40s / 5s 一片 = 8 片 ⇒ 线程数 1 → 2 → 3 → 4 → 6 → 6 → 6 → 6
    #    (2026-11-02: 台阶补了一级 3 —— 见上面【1】)
    #    ⚠️ 2026-11-09 晚: 这里**故意填 6** —— 加压上限放回 8 核(满核)之后,
    #    填 6 就是 6 ⇒ 这一轮正好验「旋钮拧小了, 真的按小的压」;
    #    (夹住的那条路由 tests/test_cpu_load_shape.py【9】验 —— 那里把板子
    #     假装成 32 核, 专门验「上限还是硬的」)
    test_config={"times": 1, "duration_sec": 40, "slice_sec": 5, "threads": 6},
    on_progress=_noop_progress,
    on_log=_noop_log,
    is_cancelled=_not_cancelled,
    on_metric=_noop_metric,
)
# ⚠️ **自己开 loop**, 不用 `asyncio.run` —— 「采样器停了没有」那条要在
#    **同一个 loop** 里看文件有没有还在长: `asyncio.run` 结束时会把 loop
#    关掉, 残留的 task 跟着被 cancel ⇒ 那样什么都看不出来。
_loop = asyncio.new_event_loop()
asyncio.set_event_loop(_loop)
result = _loop.run_until_complete(cpu_mod.CPUTest().run(ctx))


path = be.cpu_evidence_path(77)
check(path.exists(), "整项跑完, 现场记录文件在", path)
if path.exists():
    txt = path.read_text(encoding="utf-8")
    lines = txt.strip().splitlines()
    # ⚠️ 只数「起跑 第 N/M 片」那几行 —— 文件头里也有「起跑时板子背景负载」
    #    这一行(2026-11-03 加的), 用 "起跑" 匹会把**头**也算进去。
    start_lines = [l for l in lines if "起跑 第" in l]
    check(len(start_lines) == 8, "每一片都有「起跑」那一行(8 片)", len(start_lines))
    check("起跑" in txt, "有「起跑」那一行(板子死在起手一秒内也留得下)", txt)
    # ⚠️ 「逐秒的那些行」跟「起跑那一行」是两回事 —— 只查线程数的话,
    #    起跑那一行就足以蒙混过关(逐秒的记录压根没写也看不出来)。
    #    逐秒的行**带负载**(起跑那一行没有负载) ⇒ 拿这个当区分。
    check(len(lines) > len(start_lines),
          "除了「起跑」, 还有**逐秒**记下来的行", f"{len(lines)} 行 vs "
          f"{len(start_lines)} 行起跑")
    # 「逐秒的那些行」= 不带「起跑」、也不带「加压结束」的那些 ——
    # ⚠️ **「加压结束」那一行也得排除掉**(2026-11-03 补): 它也是整轮
    #    跑完才写的**一次性**行, 跟「起跑」一样带着 freq= ⇒ 留着它,
    #    「采样行里有没有频率」这条断言就会被它蒙混过去(采样器把频率
    #    弄丢了照样绿)。⇒ 只认**真正的采样行**。
    sampler_lines = [l for l in lines
                     if "起跑" not in l and "加压结束" not in l]
    check(len(sampler_lines) > 0,
          "有**真正的采样行**(不是「起跑」也不是「加压结束」)", txt)
    check(any("load=" in l for l in sampler_lines), "逐秒的行里带着负载", txt)
    check("threads=1" in txt, "第 1 片是 1 线程(缓启动)", txt)
    check("threads=2" in txt, "第 2 片爬到 2 线程", txt)
    check("threads=3" in txt, "第 3 片爬到 3 线程(2026-11-02 补的一级)", txt)
    check("threads=4" in txt, "第 4 片爬到 4 线程", txt)
    check("threads=6" in txt, "第 5 片才到满载 **6 线程**(这一轮配置填的就是 6)", txt)
    check("threads=8" not in txt,
          "**这一轮没到 8 线程**(填的是 6 —— 旋钮拧小了就得按小的压)", txt)
    check("cpub=60.0" in txt, "每个核区的温度都记下来了", txt)
    check(any("freq=" in l for l in sampler_lines),
          "频率也记下来了(采样行里就有, 不只是「起跑」那一行)", txt)
    # ⚠️ **采样够不够密** —— 只查「有逐秒的行」不够: 一整片只记一行也
    #    照样「有逐秒的行」, 但板子死在起手一秒内时那一行说不定就是
    #    唯一的一行 ⇒ 关键证据还是丢了。假 stress-ng 跑的那 0.04 秒里
    #    至少得攒出好几行, 才说明采样器**真在按间隔跑**。
    check(len(sampler_lines) >= 4,
          "采样够密: 短短一瞬间就攒出了好几行(不然起手一秒内那票证据会丢)",
          f"{len(sampler_lines)} 行")

    # ⚠️ **采样器停了没有** —— 漏了 `stop.set()` 的话, 采样器这个 task 会
    #    一直挂着(漏一个 = 一片, 八片八个), 还在背后偷偷写。
    #    ⚠️ **别拿「文件有没有在长」当判据** —— 文件在这一步之前已经被
    #    `run()` 收尾时 close 了, 残留的采样器写不进去 ⇒ 看着「没在长」,
    #    其实 task 还挂着。得直接看**有没有残留的 task**。
    pending = [t for t in asyncio.all_tasks(_loop) if not t.done()]
    check(not pending, "跑完之后没有残留的采样 task(采样器停了)",
          [str(t.get_coro()) for t in pending])

_loop.close()

section("【8】加压封顶值可调(页面上的「线程数」旋钮) —— 这条路径也得对")
#
# 起因(2026-11-03): 板子 A10000000000010 在 CPU 加压项**每次都被硬复位**。
# 当时以为「同一台板子在终端跑同样的 stress-ng 没事」⇒ 归咎于应用自己的
# 开销 ⇒ 于是做了这个旋钮, 让工人能立刻把压力拧小。
#
# ⚠️ **2026-11-04: 那个前提被实测推翻了** —— 终端跑 8 线程**一样死**;
#    真正测出来的是: 这块板子 **6 核稳、第 7 核档起手约 2 秒硬复位**
#    (不是热), 同一批的好板 8 核没事 ⇒ **个体差异**, 不是应用的锅。
#    ⇒ 旋钮照样有用(它是**唯一**能立刻把压力拧小的旋钮), 但当时还多
#      了一层**加压上限**(MAX_STRESS_THREADS)—— 拧到 8 也会被夹回上限,
#      免得有人拿满核去过应力、把日常用得好好的板子压死。
#
# ⚠️ **2026-11-09 白天: 上限从 6 降到 5** —— 板子上「整板天花板 ≈ 7 核」,
#    而应用自己那点背景(X + x11vnc + chromium 开着老化页面)就吃 0.7~1.7 核
#    ⇒ 应用声称的「6 线程」其实是**整板 6.7~7.7 核** ⇒ 过线。
#
# ⚠️⚠️ **2026-11-09 晚: 上限放回 8(满核)—— 「实验台」状态**(用户授权):
#    · 5 核那个数**没有被验证过**(当天早上定, 当晚就发现「第二轮又挂」);
#    · 而「第二轮」的变量**不是线程数**, 是**上一轮留下的热** ⇒
#      **在热板子上量出来的 5 核不能当结论**;
#    · ⇒ 用户决定: 「既然降到 5 核都还是会重启, 那就直接改回八核默认,
#      我可以手动降低核心, 不用你改来改去, 这样我先找到一个平衡点
#      再来修改默认参数」⇒ **上限放回 8(满核)**, 让工人自己在页面上
#      拧线程数, 一次只动一个变量, 找到平衡点之后再定默认参数;
#    · ⇒ 这一节改成**填 6**: 证明「旋钮拧小了, 真的按小的压」;
#      「上限还在不在」由 tests/test_cpu_load_shape.py【9】验(那里把板子
#      假装成 32 核, 专门验上限还夹得住)。
#
# 这一节守的是**后端这一半**: 旋钮拧小了, 真的就按小的压吗?
# 证据落在三处: ① 真的发出去的命令行; ② 指标; ③ 现场记录的文件头。

# ① 刚才那一轮(threads=8)真的发出去过 --cpu 8, 而且没有超过封顶值的
def _cpu_arg(line):
    """从日志行 `[CPU] 执行: … --cpu N …` 里抠出 stress-ng 的 --cpu N。

    抠不到就 None。"""
    if "[CPU] 执行:" not in line or "--cpu" not in line:
        return None
    tail = line.split("--cpu", 1)[1].strip()
    tok = tail.split()[0] if tail.split() else ""
    try:
        return int(tok)
    except (TypeError, ValueError):
        return None


def _sent_cpu_args():
    """这一轮每一片**真的**发出去的线程数。"""
    return [_cpu_arg(l) for l in _CMD_LOG if _cpu_arg(l) is not None]


sent_cpu = _sent_cpu_args()
check(len(sent_cpu) == 8,
      "每一片的执行日志里都找得到 --cpu N(8 片)", sent_cpu)
check(sent_cpu and max(sent_cpu) == 6,
      "**最高只压到 6 线程**(这一轮配置填的就是 6)", sent_cpu)
check(sent_cpu and sent_cpu[0] == 1,
      "起步还是 1 线程(缓启动 —— 板子死在起手一秒内, 起步必须轻)", sent_cpu)
check(sent_cpu == [1, 2, 3, 4, 6, 6, 6, 6],
      "整轮阶梯是 1 → 2 → 3 → 4 → 6 → 6 → 6 → 6", sent_cpu)

# ② 指标里记着这一轮的封顶值 —— 报告上要能回答「这一轮压到了几线程」
check(result.metrics.get("peak_threads") == 6,
      "指标 peak_threads = 6(这一轮实际压到的线程数)",
      result.metrics.get("peak_threads"))
check(result.metrics.get("peak_threads_configured") == 6,
      "指标 peak_threads_configured = 6(页面/配置里填的原始值)",
      result.metrics.get("peak_threads_configured"))
check(result.metrics.get("max_stress_threads") == 8,
      "指标里记着加压上限本身(8 = 天花板, 这一轮没顶到它)",
      result.metrics.get("max_stress_threads"))
check(result.metrics.get("threads_capped") != "是",
      "指标 threads_capped ≠ 是(这一轮填的 6 低于上限 8, 没被夹)",
      result.metrics.get("threads_capped"))

# ③ 现场记录的文件头写着封顶值 —— 板子死在半路时, 这是唯一能回答
#    「它死的时候这一轮本来要压到几」的凭据
hdr8 = open(be.cpu_evidence_path(77), encoding="utf-8").read().splitlines()[:8]
check(any("本轮加压峰值" in l and "6 线程" in l for l in hdr8),
      "现场记录的文件头写着「本轮加压峰值: 6 线程」", hdr8)

# ④ **封顶值调小 ⇒ 真的按小的压**(这一条最要紧: 旋钮好看但没接上,
#    工人以为降了压力, 板子照样死 —— 而报告上什么异常都看不出来)
_CMD_LOG.clear()
ctx2 = TestContext(
    run_id=78,
    config={},
    # 20s / 5s 一片 = 4 片 ⇒ 阶梯 1 → 2 → 2 → 2
    test_config={"times": 1, "duration_sec": 20, "slice_sec": 5, "threads": 2},
    on_progress=_noop_progress,
    on_log=_noop_log,
    is_cancelled=_not_cancelled,
    on_metric=_noop_metric,
)
_loop2 = asyncio.new_event_loop()
asyncio.set_event_loop(_loop2)
result2 = _loop2.run_until_complete(cpu_mod.CPUTest().run(ctx2))
_loop2.close()

sent_cpu2 = _sent_cpu_args()
check(len(sent_cpu2) == 4 and max(sent_cpu2) == 2,
      "封顶拧到 2 ⇒ **真的**最高只压到 2 线程(命令里的 --cpu N 不超过 2)",
      sent_cpu2)
check(sent_cpu2 == [1, 2, 2, 2],
      "阶梯变成 1 → 2 → 2 → 2(起步 1, 之后封顶在 2)", sent_cpu2)
check(result2.metrics.get("peak_threads") == 2,
      "指标 peak_threads = 2", result2.metrics.get("peak_threads"))
check(result2.metrics.get("peak_threads_configured") == 2,
      "指标 peak_threads_configured = 2", result2.metrics.get("peak_threads_configured"))
hdr2 = open(be.cpu_evidence_path(78), encoding="utf-8").read().splitlines()[:8]
check(any("本轮加压峰值" in l and "2 线程" in l for l in hdr2),
      "这一轮的现场记录文件头写着「本轮加压峰值: 2 线程」", hdr2)
check("threads=8" not in open(be.cpu_evidence_path(78), encoding="utf-8").read(),
      "这一轮的行里没有出现过 8 线程(别被上一轮的文件串味)")

section("【9】板子上「别的活」(背景负载) —— 得量出来、记下来、报出来")
#
# 起因(2026-11-03 14:33 那次硬复位):
#   · 现场记录里**只有 1 个加压线程**的那一级, 板子的占用率在
#     **22.7% ~ 93.2%** 之间乱跳 ⇒ 板子上**别的活**占着 0.8 ~ 6.4 核;
#   · 10-30 活着跑完 8 线程满载 48 秒的那一次, 同一级只有
#     **23.7% ~ 33.7%** ⇒ 别的活只占 0.3 核, 而且是**平的**。
# ⇒ **同样是 8 线程, 板子的总功耗差了好几倍** ⇒ 死不死看的是**总**,
#   不是加压线程数 ⇒ 这个数不量出来, 下一次死了还是只能猜。
#
# 这一节守三件事:
#   ① 起跑前**量**得出背景, 并**写进指标**;
#   ② 现场记录的**文件头**写着它(板子死在半路时, 这是唯一留得下的);
#   ③ 每一行还带着 `bg=`(总占用 - 加压线程自己)⇒ 死的那一刻板子上
#      还有多少别的活, 一行就看得出来;
#   ④ 报告的「板子死在哪一级加压 —— 结论」里**说出来**。

# ① 背景负载量得出来(假 /proc/stat 固定 75% 占用)
_loop3 = asyncio.new_event_loop()
asyncio.set_event_loop(_loop3)
bg_pct = _loop3.run_until_complete(cpu_mod._measure_background_pct(sec=0.01))
_loop3.close()
check(bg_pct is not None and abs(bg_pct - 75.0) < 0.01,
      "起跑前量得出背景负载(假 /proc/stat 是 75% 占用)", bg_pct)
check(cpu_mod._background_cores(75.0) is not None
      and abs(cpu_mod._background_cores(75.0)
              - 75.0 * (os.cpu_count() or 4) / 100.0) < 0.01,
      "背景负载能换算成核数", cpu_mod._background_cores(75.0))

# ② 指标里记着它 + ③ 文件头写着它
check(result.metrics.get("background_load_pct") is not None,
      "指标里记着这一轮的背景负载", result.metrics.get("background_load_pct"))
check(result.metrics.get("background_cores") is not None,
      "指标里记着背景负载折合几核", result.metrics.get("background_cores"))
hdr9 = open(be.cpu_evidence_path(77), encoding="utf-8").read().splitlines()[:10]
check(any("起跑时板子背景负载" in l for l in hdr9),
      "现场记录的文件头写着「起跑时板子背景负载」", hdr9)

# ④ 每一行带着 bg=(总占用 - 加压线程自己)
txt9 = open(be.cpu_evidence_path(77), encoding="utf-8").read()
sampler9 = [l for l in txt9.splitlines() if "起跑" not in l]
check(any("bg=" in l for l in sampler9),
      "逐秒的行里带着 bg=(板子上别的活占的那部分)", txt9[:400])

# ⑤ 背景高的时候要**说出来**(日志 + 指标)—— 不说, 报告上还是没法判断
check(any("背景负载" in l for l in _CMD_LOG),
      "日志里说了起跑时的背景负载", _CMD_LOG[-6:])
check(result.metrics.get("background_high") == "是",
      "背景负载高 ⇒ 指标 background_high = 是", result.metrics.get("background_high"))

# ⑥ 供电轨**不是**每一行都读(13.31ms 一次的阻塞读写, 5Hz 太奢侈)
_rails_calls = {"n": 0}
_orig_rails = cpu_mod._read_rails


def _counting_rails():
    _rails_calls["n"] += 1
    return {"axp2202-dcdc1": 1150}


cpu_mod._read_rails = _counting_rails
_CMD_LOG.clear()
_loop4 = asyncio.new_event_loop()
asyncio.set_event_loop(_loop4)
ctx4 = TestContext(
    run_id=79,
    config={},
    test_config={"times": 1, "duration_sec": 40, "slice_sec": 5, "threads": 8},
    on_progress=_noop_progress,
    on_log=_noop_log,
    is_cancelled=_not_cancelled,
    on_metric=_noop_metric,
)
_loop4.run_until_complete(cpu_mod.CPUTest().run(ctx4))
_loop4.close()
cpu_mod._read_rails = _orig_rails
txt79 = open(be.cpu_evidence_path(79), encoding="utf-8").read()
n_samples79 = len([l for l in txt79.splitlines() if l.startswith("t=")])
check(n_samples79 >= 8, "这一轮攒出了够多的采样行(用来比)", n_samples79)
# ⚠️ **2026-11-03 改了口径**: 供电轨现在**加压窗口里一次都不读** ——
# 只在**窗口外**读两次(文件头=加压前, 整轮跑完=加压后)。打包之后是
# 「文件头 + 采样时 1Hz」⇒ 加压窗口里每秒扫 66 个 sysfs 文件
# (实测 5.4~13.3ms 一遍), 那正是板子电流最高、最吃不消的时候。
check(_rails_calls["n"] <= 3,
      "供电轨整轮只读 2~3 回(加压前 + 加压后), 加压窗口里 0 次",
      f"读了 {_rails_calls['n']} 次 / {n_samples79} 行")

# ⑦ 报告的结论里要说背景(用一段真的现场记录喂 summarize_cpu_death)
_death_text = "\n".join([
    "# 加压现场记录 —— 板子 A100   采样间隔 0.2s",
    "# 本轮加压峰值: 8 线程(阶梯从 1 逐级爬到它)",
    "# 起跑时板子背景负载: 36.7% (≈2.9 核) —— 这是**别的活**占的",
    "# 起始温度: cpub_thermal_zone=44.4℃",
    "# 过热阈值(内核 thermal 驱动): cpub_thermal_zone: passive@70℃ "
    "passive@90℃ critical@110℃",
    "t=+20.6s threads=4 hot=62.1 cpub_thermal_zone=62.1 load=72.6% "
    "bg=22.6% freq=1800MHz",
    "t=+21.6s threads=8 hot=56.6 cpub_thermal_zone=56.6 load=72.6% "
    "bg=22.6% freq=1416MHz 起跑 第 5/12 片(8 线程)",
])
_sum = be.summarize_cpu_death(_death_text)
# ⚠️ **要查这一整句, 别只查「别的活」三个字**(2026-11-03 补): 逐行的
#    「背景占用(别的活) …」里也有「别的活」三个字 ⇒ 只查那三个字的话,
#    「结论里那一句专门讲背景的话被删了」照样绿 ⇒ 变异测试抓不住。
check("板子上还有别的活" in _sum,
      "结论里说出了「板子上还有别的活」", _sum)
check("2.9" in _sum or "2.8" in _sum, "结论里给出了背景的核数", _sum)
check("不能只算在板子头上" in _sum,
      "背景高 ⇒ 结论明说「这一轮的死不能只算在板子头上」", _sum)

# ⑨ 这个测试**一个真进程都没起过**(2026-11-03 14:59 实测: 原来它起的是
#    真的 `sudo -n /usr/bin/stress-ng --cpu 8`, 把板子压到掉电复位) ——
#    断言「起进程走的是那个可替换的口子」。哪天生产代码又绕开 `_spawn_proc`
#    直接 `create_subprocess_exec`, 这条就会红(那时测试又会开始烤板子了)。
check(len(_SPAWNED) > 0,
      "起进程走的是可替换的口子(这个测试一个真进程都没起)", len(_SPAWNED))
check(all("--cpu" in " ".join(c) for c in _SPAWNED),
      "假起进程记下的命令里确实有 --cpu(证明它被调用到了)", _SPAWNED[:2])

# ⑩ 绊线: 整项跑完**一次都没被踩** —— 说明生产代码确实走的是那个口子,
#    没有偷偷绕开。
check(not _TRIPWIRE_HITS,
      "整项跑完, 绊线一次都没被踩(生产代码没绕开那个口子直接起 stress-ng)",
      _TRIPWIRE_HITS[:2])

# ⑪ 绊线自己也要**验一遍** —— 否则「绊线装上了没」本身就变成信仰了:
#    故意去起一个 stress-ng, 必须被拦下来(拦下来 = 真的没起)。
_tripwire_ok = False
try:
    _loop.run_until_complete(
        asyncio.create_subprocess_exec(
            "sudo", "-n", "/usr/bin/stress-ng", "--cpu", "8"))
except RuntimeError:
    _tripwire_ok = True
check(_tripwire_ok,
      "绊线真的拦得住 stress-ng(自己试一次: 拦下来了 = 一个真进程都没起)",
      _TRIPWIRE_HITS[-1:] if _TRIPWIRE_HITS else "没拦住!")

# 还原
asyncio.create_subprocess_exec = _orig_create_exec
cpu_mod._spawn_proc = _orig_spawn
cpu_mod._read_cpu_temp = _orig_temp
cpu_mod._read_all_zones = _orig_zones
cpu_mod._read_cpu_stat = _orig_cpu_stat
cpu_mod._read_cpu_freq = _orig_freq
cpu_mod.find_tool = _orig_find_tool
cpu_mod.sudo_hint_if_unavailable = _orig_sudo_hint
cpu_mod.run_proc_with_progress = _orig_run_proc
cpu_mod.EVIDENCE_SAMPLE_INTERVAL_SEC = _orig_interval

# 还原配置
Config._data = _SAVED_CFG

print("\n" + ("全部通过" if not FAILS else f"有失败项 ({len(FAILS)})"))
for f in FAILS:
    print("  ✗ " + f)
sys.exit(0 if not FAILS else 1)
