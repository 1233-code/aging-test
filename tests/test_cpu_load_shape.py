"""回归测试: CPU 压力项的**加压形态** —— 必须是「打包前那个形态」。

**为什么有这个测试** (2026-11-03 建立, 2026-11-04 改了前提)

    同一块板子(SN 31175c3d01850801f56):
      · **打包之前**(HEAD = 445986d, 10-28)跑 CPU 压力 —— **每次都正常**;
      · **打包之后**(0.1.0-5 ~ 0.1.0-11, 10-29 ~ 11-02)跑 —— **每次都硬复位**。

    ⇒ 当时判定: 差异全在应用**自己**这几行代码上。打包之后, CPU 项相对
      打包前**多了三样东西**, 且**全都跑在加压窗口里**:

        ① **供电轨扫描**: `_read_rails()` 一次读 66 个 sysfs 文件, 板子上
           实测 **5.4~13.3ms 一遍**, 打包后是**每 1 秒一次**;
        ② **证据采样器**: 打包前**根本没有证据文件**; 打包后是**每 200ms
           一行**(每行要读 /proc/stat + 5 个 thermal zone + 8 个 cpufreq);
        ③ **证据文件的周期 fsync**: 根文件系统在 **SD 卡**上 ⇒ fsync =
           一次真的落盘 = 一次**写电流尖峰**, 打包后是**每 1 秒一次**。

    ⇒ 2026-11-03 按「还原成打包前形态」把这三样收回窗口外/降密度。

⚠️ **2026-11-04: 那个因果链被实测推翻了** —— 板子在 **6 核稳、第 7 核档
   起手约 2 秒硬复位**(不是热: 起跑 60.9℃, critical trip 110℃), 而
   **人工在终端跑 8 线程一样死** ⇒ **跟应用这三样开销无关** ⇒ 应用侧再
   怎么减负也救不回来; 同一批里的好板 8 核没事 ⇒ 这是**个体差异**。
   ⇒ 加压**封顶 6 核**(见 `MAX_STRESS_THREADS`), 不再满核。

   ⇒ **那这个测试还留不留?** 留 —— 但它的**理由变了**: 那三样开销**不是**
   复位的原因, 可是它们**让工具的负载跟人工终端跑的负载不一样**(人工终端
   跑的时候板上没有这些), 而且**都在加压窗口里**——板子电流最高的那几秒
   再往上加杂活, 没有任何好处。**「不在加压窗口里添乱」这条纪律要留着。**

**这个测试不碰真硬件**: 热目录、stress-ng 全是假的, 而且**一个线程都不真起**
(假 stress-ng 立刻退出)。
跑法: .venv/bin/python -m tests.test_cpu_load_shape
"""
import asyncio
import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.tests import cpu as cpu_mod          # noqa: E402
from server.tests.base import TestContext        # noqa: E402
from server.tests._tools import run_proc_with_progress   # noqa: E402
from tests._tmp import mkdtemp                   # noqa: E402

# ── 数据目录换成临时目录 ──
#
# ⚠️ CPU 压力项会写「加压现场记录」(logs/cpu_evidence_run<N>.txt)。
# 不换的话, **跑一次回归就在仓库里落一个文件** —— 测试产物一律不许留在仓库里。
DATA_DIR = mkdtemp(prefix="aging-cpuload-")
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


# ── 假的 stress-ng: 每次被调用就把自己的**命令行**记下来, 然后立刻退出 ──
#
# ⚠️ 关键: 它**立刻退出** —— 于是「这一轮起了几个进程」就等于
#    「这一轮加压起了几片」。连续模式 ⇒ 1 个; 分片模式 ⇒ N 个。
FAKE_STRESS = """#!/bin/sh
echo "$@" >> "$FAKE_ARGS"
exit 0
"""


def make_fake_env(base):
    d = os.path.join(base, "fake")
    os.makedirs(os.path.join(d, "thermal", "thermal_zone0"), exist_ok=True)
    os.makedirs(os.path.join(d, "thermal", "thermal_zone1"), exist_ok=True)
    with open(os.path.join(d, "thermal", "thermal_zone0", "type"), "w") as f:
        f.write("cpul_thermal_zone\n")
    with open(os.path.join(d, "thermal", "thermal_zone0", "temp"), "w") as f:
        f.write("40000\n")
    with open(os.path.join(d, "thermal", "thermal_zone1", "type"), "w") as f:
        f.write("cpub_thermal_zone\n")
    with open(os.path.join(d, "thermal", "thermal_zone1", "temp"), "w") as f:
        f.write("45000\n")
    stress = os.path.join(d, "stress-ng")
    with open(stress, "w") as f:
        f.write(FAKE_STRESS)
    os.chmod(stress, 0o755)
    args_file = os.path.join(d, "args.txt")
    with open(args_file, "w") as f:
        f.write("")
    return d, args_file


def patch_cpu(fake_dir, args_file):
    cpu_mod.THERMAL_DIR = os.path.join(fake_dir, "thermal")
    cpu_mod.find_tool = lambda name: os.path.join(fake_dir, "stress-ng")
    cpu_mod.sudo_hint_if_unavailable = lambda result, name: True
    # 不套 sudo/timeout —— 测试机没配免密; 这里要验的是**加压形态**
    cpu_mod.with_sudo = lambda cmd, **kw: list(cmd)
    os.environ["FAKE_ARGS"] = args_file


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


def fake_invocations(args_file):
    """假 stress-ng 被起了几次, 每次的完整命令行是什么。"""
    with open(args_file, encoding="utf-8") as f:
        body = f.read().strip()
    if not body:
        return []
    return [ln for ln in body.splitlines() if ln.strip()]


# ══════════════════════════════════════════════════════════════════════
section("【1】默认(不配 slice_sec)⇒ **一个进程一次跑满整轮**")

base = mkdtemp(prefix="aging-cpu-continuous-")
fake_dir, args_file = make_fake_env(base)
patch_cpu(fake_dir, args_file)
result, logs = run_cpu({"times": 1, "duration_sec": 60, "threads": 6,
                        "max_temp_c": 85, "abort_temp_c": 95})
inv = fake_invocations(args_file)
check(len(inv) == 1,
      f"60s 整轮只起 **1** 个 stress-ng(打包前就是一次跑满)", len(inv))
check(bool(inv) and "--cpu 6" in inv[0],
      "起手就是**这一轮的封顶 6 线程** —— 连续模式不爬台阶", inv)
check(bool(inv) and "--timeout 60.0s" in inv[0],
      "一次跑满 60 秒(不是 5 秒一片)", inv)
check(result.metrics.get("load_mode") == "continuous",
      "指标里记了 load_mode=continuous", result.metrics.get("load_mode"))
check(any("连续加压" in l for l in logs),
      "日志里写明了「连续加压」", logs)

section("【2】默认形态**不逐级爬**(1→2→3→4→8)—— 那是打包后才有的")

check(bool(inv) and "--cpu 1" not in inv[0],
      "**没有**先起 1 线程再爬 —— 打包前就是一把满载", inv)

section("【3】加压窗口里**一次都不读供电轨**")

# ⚠️ 这是打包后新增的 #1 负担: 每秒扫 66 个 sysfs 文件(实测 5.4~13.3ms
#    一遍), 而且跑在板子电流最高的那一刻上。
RAILS_IN_WINDOW = []
_in_window = {"on": False}

_orig_rails_cached = cpu_mod._read_rails_cached


def _traced_rails(max_age=None):
    if _in_window["on"]:
        RAILS_IN_WINDOW.append(max_age)
    return _orig_rails_cached(max_age=max_age)


_orig_run_slice = cpu_mod.CPUTest._run_slice


async def _traced_run_slice(self, ctx, *a, **kw):
    _in_window["on"] = True
    try:
        return await _orig_run_slice(self, ctx, *a, **kw)
    finally:
        _in_window["on"] = False


cpu_mod._read_rails_cached = _traced_rails
cpu_mod.CPUTest._run_slice = _traced_run_slice
try:
    base = mkdtemp(prefix="aging-cpu-rails-")
    fake_dir, args_file = make_fake_env(base)
    patch_cpu(fake_dir, args_file)
    result, logs = run_cpu({"times": 1, "duration_sec": 60, "threads": 6,
                            "max_temp_c": 85, "abort_temp_c": 95})
finally:
    cpu_mod._read_rails_cached = _orig_rails_cached
    cpu_mod.CPUTest._run_slice = _orig_run_slice

check(not RAILS_IN_WINDOW,
      f"加压窗口里 **0 次**供电轨扫描(打包后是每秒一次)", RAILS_IN_WINDOW)

section("【4】证据采样间隔: **≥ 2 秒**(打包后是 200ms), 但也别太稀")

# ⚠️ **两头都要卡**: 密了(200ms)是「应用自己把加压窗口搞重」(§6.2.6),
#    稀了(4 秒)就「板子死在起手一秒内时那一票关键证据整个丢」——
#    「起跑」那一行虽然立刻落盘, 但**它记的是起跑那一瞬间**, 之后的
#    温度/频率/负载全没了 ⇒ 读者只知道「死了」, 不知道「怎么死的」。
check(2.0 <= cpu_mod.EVIDENCE_SAMPLE_INTERVAL_SEC <= 3.0,
      "证据采样间隔在 2~3 秒之间(别太密也别太稀)",
      f"{cpu_mod.EVIDENCE_SAMPLE_INTERVAL_SEC}s")

section("【5】证据文件的周期 fsync ≥ 10 秒(打包后是每秒一次)")

check(cpu_mod.EVIDENCE_FSYNC_INTERVAL_SEC >= 10.0,
      "周期 fsync 间隔 ≥ 10 秒 —— SD 卡写电流尖峰别撞上板子最吃力的时候",
      f"{cpu_mod.EVIDENCE_FSYNC_INTERVAL_SEC}s")

section("【6】**判温口径一个字没动** —— 还是 tick 的 1Hz")

# ⚠️ 采样器降密度 ≠ 判温降密度。判温走 tick, 采样率一改, 「合格/不合格」
#    的判定口径就跟着改了 —— 那是改产品行为, 不是加记录。
sig = inspect.signature(run_proc_with_progress)
check(sig.parameters.get("tick_sec").default == 1.0,
      "判温 tick 仍是 1 秒一拍",
      sig.parameters.get("tick_sec").default)

section("【7】**分片模式还留着**(显式配 slice_sec 才走)—— 热保护没被削掉")

base = mkdtemp(prefix="aging-cpu-sliced-")
fake_dir, args_file = make_fake_env(base)
patch_cpu(fake_dir, args_file)
result, logs = run_cpu({"times": 1, "duration_sec": 30, "slice_sec": 5,
                        "threads": 6, "max_temp_c": 85, "abort_temp_c": 95})
inv = fake_invocations(args_file)
check(len(inv) == 6, "30s ÷ 5s = 6 片(分片模式仍然可用)", len(inv))
check(result.metrics.get("load_mode") == "sliced",
      "指标里记了 load_mode=sliced", result.metrics.get("load_mode"))
check(any("--cpu 1" in l for l in logs),
      "分片模式仍然逐级爬(1 线程起手) —— 这条是**热保护**用的", logs)

section("【8】配置里**默认没有 slice_sec** —— 默认形态就是连续跑")

import yaml                                    # noqa: E402
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
with open(os.path.join(_REPO, "config.yaml"), encoding="utf-8") as f:
    _cfg = yaml.safe_load(f)
_cpu_defaults = (_cfg.get("defaults") or {}).get("cpu") or {}
check("slice_sec" not in _cpu_defaults,
      "config.yaml 的 cpu 默认里**没有** slice_sec ⇒ 默认连续跑",
      _cpu_defaults.get("slice_sec"))
check(_cpu_defaults.get("duration_sec") == 60,
      "整轮还是 60 秒", _cpu_defaults.get("duration_sec"))
check(_cpu_defaults.get("abort_temp_c") == 95,
      "收手线还在(分片模式下有效)", _cpu_defaults.get("abort_temp_c"))

section("【9】加压**封顶 6 核**(2026-11-04 改)—— 满核是过应力, 会误杀好板")

# ⚠️ 实测依据(SN A10000000000010, 2026-11-04): **6 核稳, 第 7 核档起手约
#    2 秒硬复位**(不是热 —— 起跑 60.9℃, critical trip 110℃); 而**人工在
#    终端跑 8 线程一样死** ⇒ 跟应用无关; 同一批里的**好板 8 核没事**
#    ⇒ 个体差异。⇒ 满核 8 线程会把「日常用得好好的」板子压死 ⇒
#    **产线误杀好板**的代价高于漏判边缘板 ⇒ 封顶 6 核。
check(cpu_mod.MAX_STRESS_THREADS == 6,
      "加压上限常量是 6 核", cpu_mod.MAX_STRESS_THREADS)

_NCPU = os.cpu_count() or 4
check(cpu_mod._resolve_threads(0) == min(_NCPU, 6),
      "0(自动)= 核数, 但再被上限夹住", cpu_mod._resolve_threads(0))
check(cpu_mod._resolve_threads(8) == 6,
      "**填 8 也只压 6 核**(上限不是默认值, 是硬夹)", cpu_mod._resolve_threads(8))
check(cpu_mod._resolve_threads(64) == 6,
      "填 64 也一样夹到 6", cpu_mod._resolve_threads(64))
check(cpu_mod._resolve_threads(6) == 6,
      "填 6 就是 6", cpu_mod._resolve_threads(6))
check(cpu_mod._resolve_threads(2) == 2,
      "填 2 就是 2(可以调小)", cpu_mod._resolve_threads(2))
check(cpu_mod._resolve_threads(-3) == min(_NCPU, 6),
      "负数当自动(不能炸)", cpu_mod._resolve_threads(-3))
check(cpu_mod._resolve_threads("abc") == min(_NCPU, 6),
      "非法值当自动(不能炸)", cpu_mod._resolve_threads("abc"))
check(cpu_mod._resolve_threads(0) >= 1,
      "最少 1 线程(stress-ng --cpu 0 是非法参数)")

check(_cpu_defaults.get("threads") == 6,
      "config.yaml 的 cpu 默认 threads = 6(不是 0=全部核)",
      _cpu_defaults.get("threads"))

# 端到端: 页面/配置填 8 ⇒ 真的只压 6, 而且报告上**看得见**这件事。
base = mkdtemp(prefix="aging-cpu-cap-")
fake_dir, args_file = make_fake_env(base)
patch_cpu(fake_dir, args_file)
result, logs = run_cpu({"times": 1, "duration_sec": 60, "threads": 8,
                        "max_temp_c": 85, "abort_temp_c": 95})
inv = fake_invocations(args_file)
check(bool(inv) and "--cpu 6" in inv[0],
      "端到端: 填 8 线程 ⇒ stress-ng 实际拿到 --cpu 6", inv)
check(result.metrics.get("peak_threads") == 6,
      "指标 peak_threads = 6(实际压了几核)",
      result.metrics.get("peak_threads"))
check(result.metrics.get("peak_threads_configured") == 8,
      "指标 peak_threads_configured = 8(配置里填的原始值)",
      result.metrics.get("peak_threads_configured"))
check(result.metrics.get("max_stress_threads") == 6,
      "指标里记着上限本身(读者知道 6 是政策, 不是板子只有 6 核)",
      result.metrics.get("max_stress_threads"))
check(result.metrics.get("threads_capped") == "是",
      "指标 threads_capped = 是(这一轮被夹过)",
      result.metrics.get("threads_capped"))
check(any("夹住" in l for l in logs),
      "日志里明说「被上限夹住」", logs)

section("【10】现场记录的**头**里那句话得跟这一轮真跑的形态一致")

# ⚠️ 2026-11-04 修: 原来那句话**写死**成「阶梯从 1 逐级爬到它」, 而默认
#    形态是**连续加压**、压根没有阶梯 ⇒ 产线看报告会被带偏去查爬坡逻辑。
_h_cont = cpu_mod._format_evidence_header(peak_threads=6,
                                           background_pct=None,
                                           load_mode="continuous")
check("阶梯" not in _h_cont,
      "连续模式的头上**没有**「阶梯」两个字", _h_cont.splitlines()[:4])
check("连续加压" in _h_cont,
      "连续模式的头里写的是「连续加压」", _h_cont.splitlines()[:4])
_h_sliced = cpu_mod._format_evidence_header(peak_threads=6,
                                            background_pct=None,
                                            load_mode="sliced")
check("阶梯" in _h_sliced,
      "分片模式的头里才写「阶梯」", _h_sliced.splitlines()[:4])


print("\n" + ("全部通过" if not FAILS else f"有失败项 ({len(FAILS)})"))
for f in FAILS:
    print("  ✗ " + f)
sys.exit(0 if not FAILS else 1)
