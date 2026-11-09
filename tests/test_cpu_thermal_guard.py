"""回归测试: CPU 压力项的**热保护** —— 不许把板子烤到内核重启 (§6.2)。

**起因 (2026-10-29 新板实测)**
    整轮跑到最后一项 (CPU 压力) 时**板子直接重启**, 报告上只剩一句
    「服务重启, 这一轮中途夭折(没跑完)」。查这块板子的内核热框架
    (`/sys/class/thermal`):

      · thermal_zone: cpul(小核) / cpub(大核) / gpu / npu / ddr
      · 被动降频 trip = 70℃ / 90℃, **critical trip = 110℃**
      · 冷却手段**只有** cpufreq 降频 (cpu0 / cpu4 两个 cooling_device);
        风扇不在热框架里 (厂商私有 GPIO 驱动, §6.8)

    critical trip 一到, 内核走 emergency shutdown ⇒ **板子当场重启**,
    整轮连同日志一起没了 —— 这是最坏的结果: 没有结论, 也没有证据。

**所以这一项必须自己护住板子**
    ① 看**最热**的那个核区 —— 大核 cpub 才是热点 (实测同一场满载:
       大核峰值 76.6℃ / 小核峰值 73.8℃, 只看小核少报 3℃);
    ② 起跑前就是热的 ⇒ **不加压**, 直接判不合格;
    ③ 跑的过程中到了收手线 ⇒ **主动收手**, 不再起下一片;
    ④ **分片**跑 —— 这么切是为了**不用特权 kill** 就能中途停
       (root 起的进程, 普通用户对它发信号必然 EPERM, §11.2)。

**这个测试不碰真硬件**: 温度目录是它自己造的假目录, stress-ng 是它自己
造的假脚本 (每次被调用就把「温度」抬高, 模拟烤机)。
跑法: .venv/bin/python -m tests.test_cpu_thermal_guard
"""
import asyncio
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.tests import cpu as cpu_mod          # noqa: E402
from server.tests.base import TestContext        # noqa: E402
from tests._tmp import mkdtemp                   # noqa: E402

# ── 数据目录换成临时目录 ──
#
# ⚠️ **必须换** —— CPU 压力项现在会写「加压现场记录」
# (`logs/cpu_evidence_run<N>.txt`, §9.3.3)。不换的话, **跑一次回归就在
# 仓库里落一个文件**(2026-10-30 实测: 仓库 logs/ 里冒出来一个
# cpu_evidence_run1.txt) —— 测试产物一律不许留在仓库里。
DATA_DIR = mkdtemp(prefix="aging-cpuguard-")
os.environ["AGING_DATA_DIR"] = DATA_DIR

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


# ── 假的热目录 + 假的 stress-ng ─────────────────────────────────────────
FAKE_STRESS = """#!/bin/sh
# 假 stress-ng: 每被调用一次就把「CPU 温度」抬高 (模拟烤机)
# ⚠️ 温度要写**毫度** (内核 thermal_zone/temp 的格式: 76000 = 76℃),
#    写成 "76" 会被读成 0.076℃ —— 就跟没加热一样。
n=$(cat "$FAKE_COUNTER" 2>/dev/null || echo 0)
n=$((n + 1))
echo "$n" > "$FAKE_COUNTER"
t=$(( (FAKE_TEMP_BASE + n * FAKE_TEMP_STEP) * 1000 ))
echo "$t" > "$FAKE_ZONE_TEMP"
echo "fake stress-ng run $n, temp=$((FAKE_TEMP_BASE + n * FAKE_TEMP_STEP))C"
exit 0
"""


def make_fake_env(base):
    """造假的热目录 + 假的 stress-ng + 计数器。返回 (dir, zone_temp, counter)。"""
    d = os.path.join(base, "fake")
    os.makedirs(os.path.join(d, "thermal", "thermal_zone0"), exist_ok=True)
    os.makedirs(os.path.join(d, "thermal", "thermal_zone1"), exist_ok=True)
    # 小核: 一直是 40℃; 大核: 由假 stress-ng 改写
    with open(os.path.join(d, "thermal", "thermal_zone0", "type"), "w") as f:
        f.write("cpul_thermal_zone\n")
    with open(os.path.join(d, "thermal", "thermal_zone0", "temp"), "w") as f:
        f.write("40000\n")
    with open(os.path.join(d, "thermal", "thermal_zone1", "type"), "w") as f:
        f.write("cpub_thermal_zone\n")
    zone_temp = os.path.join(d, "thermal", "thermal_zone1", "temp")
    with open(zone_temp, "w") as f:
        f.write("40000\n")
    stress = os.path.join(d, "stress-ng")
    with open(stress, "w") as f:
        f.write(FAKE_STRESS)
    os.chmod(stress, 0o755)
    counter = os.path.join(d, "counter")
    with open(counter, "w") as f:
        f.write("0")
    return d, zone_temp, counter


def patch_cpu(fake_dir, zone_temp, counter, temp_base, temp_step):
    """把 cpu 模块的三个外部依赖换成假的, 并设好假脚本要读的环境变量。"""
    cpu_mod.THERMAL_DIR = os.path.join(fake_dir, "thermal")
    cpu_mod.find_tool = lambda name: os.path.join(fake_dir, "stress-ng")
    cpu_mod.sudo_hint_if_unavailable = lambda result, name: True
    # 不套 sudo/timeout —— 测试机没配免密, 而且这里要验的是**热保护逻辑**
    cpu_mod.with_sudo = lambda cmd, **kw: list(cmd)
    os.environ["FAKE_COUNTER"] = counter
    os.environ["FAKE_ZONE_TEMP"] = zone_temp
    os.environ["FAKE_TEMP_BASE"] = str(temp_base)
    os.environ["FAKE_TEMP_STEP"] = str(temp_step)


def make_ctx(test_config, logs):
    async def on_progress(pct, msg):
        pass

    async def on_log(line):
        logs.append(line)

    async def is_cancelled():
        return False

    async def on_metric(key, value):
        pass

    return TestContext(run_id=1, config={}, test_config=test_config,
                       on_progress=on_progress, on_log=on_log,
                       is_cancelled=is_cancelled, on_metric=on_metric)


def run_cpu(test_config):
    logs = []
    ctx = make_ctx(test_config, logs)
    result = asyncio.run(cpu_mod.CPUTest().run(ctx))
    return result, logs


def counter_value(counter):
    with open(counter) as f:
        return int(f.read().strip() or 0)


# ══════════════════════════════════════════════════════════════════════
section("【1】读温度 —— 取**最热**的那个 CPU 核区, 不是第一个")

base = mkdtemp(prefix="aging-cpu-thermal-")
fake_dir, zone_temp, counter = make_fake_env(base)
patch_cpu(fake_dir, zone_temp, counter, temp_base=76, temp_step=0)

# 小核 40℃, 大核 76℃ —— 旧口径会读小核(40℃), 少报 36℃!
with open(os.path.join(fake_dir, "thermal", "thermal_zone1", "temp"), "w") as f:
    f.write("76000\n")
temp = asyncio.run(cpu_mod._read_cpu_temp())
check(temp == 76.0, "取最热的 CPU 核区 (大核 76℃, 小核 40℃)", temp)
check(cpu_mod._read_cpu_zones is not None, "有独立的 _read_cpu_zones (报告要逐区列)")

section("【2】起跑前就超温 ⇒ 不加压, 直接判不合格")

base = mkdtemp(prefix="aging-cpu-hot-")
fake_dir, zone_temp, counter = make_fake_env(base)
patch_cpu(fake_dir, zone_temp, counter, temp_base=99, temp_step=0)
with open(zone_temp, "w") as f:
    f.write("99000\n")          # 起跑前就 99℃ (收手线 95℃)
result, logs = run_cpu({"times": 1, "duration_sec": 40, "slice_sec": 5,
                        "threads": 4, "max_temp_c": 85, "abort_temp_c": 95})
check(result.status == "failed", "判 failed (板子自己就烤着)", result.status)
check(counter_value(counter) == 0, "**压根没起 stress-ng** —— 不拿满载去烤它",
      counter_value(counter))
check("99" in (result.error or ""), "报错里带着实测温度", result.error)
check(result.metrics.get("thermal_abort") == "是", "指标记了 thermal_abort")

section("【3】跑的过程中超温 ⇒ 主动收手, 不再起下一片")

base = mkdtemp(prefix="aging-cpu-abort-")
fake_dir, zone_temp, counter = make_fake_env(base)
# 每片抬 20℃: 70 → 90 → 110 ⇒ 第 3 片读完就超线, 第 4 片不许起
patch_cpu(fake_dir, zone_temp, counter, temp_base=50, temp_step=20)
result, logs = run_cpu({"times": 1, "duration_sec": 40, "slice_sec": 5,
                        "threads": 4, "max_temp_c": 85, "abort_temp_c": 95})
runs = counter_value(counter)
check(result.status == "failed", "判 failed (散热扛不住满载)", result.status)
check(runs == 3, f"只跑了 3 片就收手 (后面 5 片没起) —— 不是跑满 8 片", runs)
check("超温" in (result.error or ""), "报错明说超温", result.error)
check(result.metrics.get("thermal_abort") == "是", "指标记了 thermal_abort")
check(result.metrics.get("peak_temp_c", 0) >= 95, "峰值温度记下来了",
      result.metrics.get("peak_temp_c"))
check(any("提前收手" in l for l in logs), "日志里写了「提前收手」")
check(any("收手" in l for l in logs), "日志里点明了收手这件事")

section("【4】温度正常 ⇒ 跑满全部片, 判 passed")

base = mkdtemp(prefix="aging-cpu-ok-")
fake_dir, zone_temp, counter = make_fake_env(base)
patch_cpu(fake_dir, zone_temp, counter, temp_base=70, temp_step=0)
result, logs = run_cpu({"times": 1, "duration_sec": 40, "slice_sec": 5,
                        "threads": 4, "max_temp_c": 85, "abort_temp_c": 95})
check(result.status == "passed", "判 passed", result.status)
check(counter_value(counter) == 8, "40s ÷ 5s = 8 片, 一片不落",
      counter_value(counter))
check(result.metrics.get("peak_temp_c") == 70.0, "峰值温度 = 70℃",
      result.metrics.get("peak_temp_c"))
check(result.metrics.get("start_temp_c") is not None, "记了起跑前温度")
check(result.metrics.get("temp_zones_now"), "记了逐区温度 (报告能看出哪个核区最烫)")

section("【5】阈值关系 —— 收手线必须高于判定线")

base = mkdtemp(prefix="aging-cpu-thr-")
fake_dir, zone_temp, counter = make_fake_env(base)
patch_cpu(fake_dir, zone_temp, counter, temp_base=70, temp_step=0)
result, logs = run_cpu({"times": 1, "duration_sec": 10, "slice_sec": 5,
                        "threads": 4, "max_temp_c": 85, "abort_temp_c": 60})
check(result.metrics.get("abort_temp_c", 0) > 85,
      "配错了(收手线 60 < 判定线 85)会自动抬到判定线之上",
      result.metrics.get("abort_temp_c"))

section("【6】逐级加压 (1 → 2 → 3 → 4 → 满载) —— 别一上来就满载")
# ⚠️ 2026-11-09 晚: 满载那一片现在是 **8 线程** —— 加压上限放回 8 核(满核,
#    用户授权的「实验台」状态: 让工人自己在页面上拧线程数找平衡点)。
#    历史: 11-04 是 6 核(上限), 11-09 白天降到 5 核 —— 那个 5 **没被验证过**
#    (「第二轮又挂」的变量是上一轮留下的热, 不是线程数)⇒ 撤回。
#    ⚠️ 这一节跑的是**假** stress-ng(不起真进程) ⇒ 8 线程不会压到板子。
# 2026-10-30 改过: 原来是「第一片用**一半**线程」(8 → 4) 后面的片满载 ——
# 那是第一次加的缓解(新板「一启动就重启」), 但实测**连 4 线程都扛不住**
# ⇒ 缓启动改成逐级爬: 1 → 2 → 4 → 8。板子在哪一级扛不住, 那级就是天花板。
#
# ⚠️ 2026-11-02 又改了两处:
#   · 台阶补一级 **3**(1 → 2 → **3** → 4 → 8)—— 实测板子扛住 1、2 线程,
#     死在 4 线程起手 1 秒 ⇒ 「**3 活着 4 死**」才是天花板, 「2 活着 4 死」
#     只是个范围;
#   · **片数不够时台阶压缩, 但满载那一片必须留着** —— 这一节只跑 20 秒
#     (4 片), 台阶 5 级塞不下 ⇒ 压成 1 → 2 → **4** → 8(丢中间那级)。
#     不压缩的话最后一片才 4 线程 ⇒ **满载根本没跑到** ⇒ 板子假通过。

base = mkdtemp(prefix="aging-cpu-ramp-")
fake_dir, zone_temp, counter = make_fake_env(base)
patch_cpu(fake_dir, zone_temp, counter, temp_base=70, temp_step=0)
logs = []
ctx = make_ctx({"times": 1, "duration_sec": 20, "slice_sec": 5, "threads": 8,
                 "max_temp_c": 85, "abort_temp_c": 95}, logs)
asyncio.run(cpu_mod.CPUTest().run(ctx))
first = next((l for l in logs if "执行:" in l), "")
check("--cpu 1" in first, "第 1 片只有 1 个线程(逐级加压)", first)
check(any("--cpu 2" in l for l in logs), "第 2 片爬到 2 个线程", logs)
check(any("--cpu 4" in l for l in logs), "第 3 片爬到 4 个线程", logs)
check(any("--cpu 8" in l for l in logs),
      "第 4 片才到满载 —— **8 个线程**(加压上限 8 核 = 满核)", logs)
# 短跑也得**真的满载过一次** —— 台阶压缩了, 满载那一片不能被挤掉
check(logs.index(next(l for l in logs if "--cpu 8" in l))
      > logs.index(next(l for l in logs if "--cpu 4" in l)),
      "满载在最后, 不是一步跳过去", logs)

section("【7】分片数 = duration ÷ slice_sec")

base = mkdtemp(prefix="aging-cpu-slice-")
fake_dir, zone_temp, counter = make_fake_env(base)
patch_cpu(fake_dir, zone_temp, counter, temp_base=70, temp_step=0)
result, logs = run_cpu({"times": 1, "duration_sec": 30, "slice_sec": 5,
                        "threads": 4, "max_temp_c": 85, "abort_temp_c": 95})
check(counter_value(counter) == 6, "30s ÷ 5s = 6 片",
      counter_value(counter))
check(result.metrics.get("round_1_slices") == 6, "指标里记了片数",
      result.metrics.get("round_1_slices"))

section("【8】代码里不许出现特权 kill (热保护靠分片, 不靠 signal)")

with open(cpu_mod.__file__, encoding="utf-8") as f:
    src = f.read()
code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
bad = [l.strip() for l in code.splitlines()
       if "kill" in l and ("sudo" in l or "kill -" in l)]
check(not bad, "cpu.py 里没有「用 sudo 去 kill」这种特权收尾", bad)
check("kill_proc(" not in code,
      "cpu.py 不调 kill_proc —— 中途收手靠「不再起下一片」")


section("【9】一片都没跑成 ⇒ 不许把 exit_code / 峰值记成 0")

base = mkdtemp(prefix="aging-cpu-norun-")
fake_dir, zone_temp, counter = make_fake_env(base)
patch_cpu(fake_dir, zone_temp, counter, temp_base=99, temp_step=0)
with open(zone_temp, "w") as f:
    f.write("99000\n")
result, logs = run_cpu({"times": 1, "duration_sec": 40, "slice_sec": 5,
                        "threads": 4, "max_temp_c": 85, "abort_temp_c": 95})
check("exit_code" not in result.metrics,
      "没记 exit_code —— 记成 0 看着像「跑完了而且没出错」",
      result.metrics.get("exit_code"))
check("peak_temp_c" not in result.metrics or result.metrics["peak_temp_c"] > 0,
      "没记一个假的峰值 0", result.metrics.get("peak_temp_c"))
check(result.metrics.get("start_temp_c") == 99.0,
      "但**起跑前**的温度要记下来 (那是唯一的实测证据)",
      result.metrics.get("start_temp_c"))


section("【10】峰值超判定线 ⇒ **判 failed** (2026-11-04 补的洞)")

# **这个洞是怎么发现的**: 用户要去别的设备上验「CPU 测试能不能过」。
# 顺手一问: 峰值落在判定线(85℃)和收手线(95℃)之间时, 到底判什么?
# 实测: **只留一句「⚠️ 温度过高」, 结果照样判 passed** ⇒ 一块烤到 93℃ 的
# 板子会被记成「CPU 通过」。而 §6.2 白纸黑字写着「峰值超过 max_temp_c =
# 判 failed」—— 代码和文档对不上。
#
# 场景: 起跑 70℃ → 跑的过程中冲到 **93℃**(> 判定线 85, < 收手线 95)
#       → 最后一次读到 87℃。
# ⚠️ 最后一个读数故意压低 —— 「峰值高、末次低」是最阴的那种: 只看末次
#    读数会以为没事。**峰值才是这块板子真达到过的温度**。

FAKE_STRESS_SLEEP = """#!/bin/sh
# 假 stress-ng: 活 3 秒再退 —— 让 tick(1Hz) 真的打到, 温度才采得到峰值
sleep 3
exit 0
"""


def make_scripted_temp(values):
    """按调用次序返回一串温度; 用完了就返回最后一个。"""
    seq = list(values)
    state = {"i": 0}

    async def _read():
        i = state["i"]
        state["i"] += 1
        return seq[i] if i < len(seq) else seq[-1]

    return _read


base = mkdtemp(prefix="aging-cpu-overtemp-")
fake_dir, zone_temp, counter = make_fake_env(base)
patch_cpu(fake_dir, zone_temp, counter, temp_base=70, temp_step=0)
# 换成「会活一会儿」的假 stress-ng —— 立刻退的话 tick 一次都打不上,
# 峰值就永远是那个末次读数, 测不出「峰值高、末次低」这档事。
sleep_stress = os.path.join(fake_dir, "stress-ng")
with open(sleep_stress, "w") as f:
    f.write(FAKE_STRESS_SLEEP)
os.chmod(sleep_stress, 0o755)
cpu_mod.find_tool = lambda name: sleep_stress

# 起跑 70 → 第一次 tick 93 → 之后都 87
_orig_read_temp = cpu_mod._read_cpu_temp
cpu_mod._read_cpu_temp = make_scripted_temp([70.0, 93.0, 87.0])
try:
    result, logs = run_cpu({"times": 1, "duration_sec": 5, "slice_sec": 5,
                            "threads": 4, "max_temp_c": 85,
                            "abort_temp_c": 95})
finally:
    cpu_mod._read_cpu_temp = _orig_read_temp

check(result.status == "failed",
      "峰值 93℃(超判定线 85℃)⇒ 判 failed —— 以前这里判的是 passed",
      result.status)
check(result.metrics.get("peak_temp_c") == 93.0, "峰值记下来了 (93℃)",
      result.metrics.get("peak_temp_c"))
check("判定线" in (result.error or ""), "报错里点明是超了判定线",
      result.error)
check("85" in (result.error or ""), "报错里带着判定线的温度值", result.error)
check(result.metrics.get("over_temp") == "是", "指标记了 over_temp",
      result.metrics.get("over_temp"))
check(result.metrics.get("thermal_abort") != "是",
      "**不是** thermal_abort —— 93℃ 没到收手线(95℃), 是峰值超线",
      result.metrics.get("thermal_abort"))

section("【11】峰值**正好等于**判定线 ⇒ 不判 failed (「超过」是严格大于)")

base = mkdtemp(prefix="aging-cpu-edge-")
fake_dir, zone_temp, counter = make_fake_env(base)
patch_cpu(fake_dir, zone_temp, counter, temp_base=70, temp_step=0)
sleep_stress = os.path.join(fake_dir, "stress-ng")
with open(sleep_stress, "w") as f:
    f.write(FAKE_STRESS_SLEEP)
os.chmod(sleep_stress, 0o755)
cpu_mod.find_tool = lambda name: sleep_stress

_orig_read_temp = cpu_mod._read_cpu_temp
cpu_mod._read_cpu_temp = make_scripted_temp([70.0, 85.0, 80.0])
try:
    result, logs = run_cpu({"times": 1, "duration_sec": 5, "slice_sec": 5,
                            "threads": 4, "max_temp_c": 85,
                            "abort_temp_c": 95})
finally:
    cpu_mod._read_cpu_temp = _orig_read_temp

check(result.status == "passed",
      "峰值 85.0℃(== 判定线)⇒ 仍判 passed —— 判据是「超过」不是「达到」",
      result.status)
check(result.metrics.get("over_temp") is None, "没记 over_temp",
      result.metrics.get("over_temp"))


section("【12】峰值 ≥ 收手线 ⇒ **收手**(哪怕末次读数已经降下来了)")

# 「只看最后一次读数」的第二个洞: 板子冲到 97℃(≥ 收手线 95℃)又落回
# 88℃, 只看末次读数会以为「没事」⇒ 又起一片 ⇒ 又冲一次。
# **峰值才是这块板子真达到过的温度** —— 收手线也得按峰值判。

FAKE_STRESS_SLEEP_COUNT = """#!/bin/sh
# 假 stress-ng: 记一次数(数几片), 活 3 秒再退 —— 让 tick(1Hz) 真的打到
n=$(cat "$FAKE_COUNTER" 2>/dev/null || echo 0)
n=$((n + 1))
echo "$n" > "$FAKE_COUNTER"
sleep 3
exit 0
"""

base = mkdtemp(prefix="aging-cpu-hotpeak-")
fake_dir, zone_temp, counter = make_fake_env(base)
patch_cpu(fake_dir, zone_temp, counter, temp_base=70, temp_step=0)
sleep_stress = os.path.join(fake_dir, "stress-ng")
with open(sleep_stress, "w") as f:
    f.write(FAKE_STRESS_SLEEP_COUNT)
os.chmod(sleep_stress, 0o755)
cpu_mod.find_tool = lambda name: sleep_stress

# 起跑 70 → 第 1 片冲到 97(≥ 收手线 95) → 之后都落到 88
_orig_read_temp = cpu_mod._read_cpu_temp
cpu_mod._read_cpu_temp = make_scripted_temp([70.0, 97.0, 88.0])
try:
    result, logs = run_cpu({"times": 1, "duration_sec": 10, "slice_sec": 5,
                            "threads": 4, "max_temp_c": 85,
                            "abort_temp_c": 95})
finally:
    cpu_mod._read_cpu_temp = _orig_read_temp

check(result.status == "failed", "判 failed (烤到过收手线)", result.status)
check(result.metrics.get("thermal_abort") == "是",
      "记了 thermal_abort —— 峰值到过收手线, 不该当成没事",
      result.metrics.get("thermal_abort"))
check(counter_value(counter) == 1,
      "第 1 片跑完就收手, 第 2 片没起 (末次读数 88℃ 不算数)",
      counter_value(counter))
check(result.metrics.get("peak_temp_c") == 97.0, "峰值记下来了 (97℃)",
      result.metrics.get("peak_temp_c"))


section("【13】工人中途点了「停止」⇒ 判 stopped, **不许**被温度盖成 failed")

# 半路停的跑, 峰值不作数 —— 那次跑的原因是「工人停了」, 不是「散热不行」。
# ⚠️ 这条是 2026-11-04 加峰值判定时**顺手补的护栏**: 不加的话, 峰值判定
#    会把 stopped / error 盖成 failed ⇒ 报告上写的原因跟实际不符。

base = mkdtemp(prefix="aging-cpu-stop-")
fake_dir, zone_temp, counter = make_fake_env(base)
patch_cpu(fake_dir, zone_temp, counter, temp_base=70, temp_step=0)
sleep_stress = os.path.join(fake_dir, "stress-ng")
with open(sleep_stress, "w") as f:
    f.write(FAKE_STRESS_SLEEP)
os.chmod(sleep_stress, 0o755)
cpu_mod.find_tool = lambda name: sleep_stress

_orig_read_temp = cpu_mod._read_cpu_temp
cpu_mod._read_cpu_temp = make_scripted_temp([70.0, 93.0, 88.0])

# is_cancelled: 第 1 次(轮次开头) → 继续; 第 2 次(1 秒处的等待循环) → 继续
# (让 tick 采到 93℃); 第 3 次(2 秒处) → 取消 ⇒ 进程被收掉 ⇒ stopped
_cancel_calls = {"n": 0}


async def _is_cancelled():
    _cancel_calls["n"] += 1
    return _cancel_calls["n"] >= 3


_stop_logs = []


async def _on_progress(pct, msg):
    pass


async def _on_log(line):
    _stop_logs.append(line)


async def _on_metric(key, value):
    pass


_stop_ctx = TestContext(run_id=1, config={},
                        test_config={"times": 1, "duration_sec": 10,
                                     "slice_sec": 10, "threads": 4,
                                     "max_temp_c": 85, "abort_temp_c": 95},
                        on_progress=_on_progress, on_log=_on_log,
                        is_cancelled=_is_cancelled, on_metric=_on_metric)
try:
    result = asyncio.run(cpu_mod.CPUTest().run(_stop_ctx))
finally:
    cpu_mod._read_cpu_temp = _orig_read_temp

check(result.status == "stopped",
      "判 stopped —— 峰值超线不许盖掉「工人中途停了」", result.status)
check(result.metrics.get("over_temp") is None,
      "没记 over_temp (半路停的峰值不作数)", result.metrics.get("over_temp"))
check("超温" not in (result.error or ""),
      "报错里也不是超温", result.error)


print("\n" + ("全部通过" if not FAILS else f"有失败项 ({len(FAILS)})"))
for f in FAILS:
    print("  ✗ " + f)
sys.exit(0 if not FAILS else 1)
