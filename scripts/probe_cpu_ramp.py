#!/usr/bin/env python3
"""分级加压探针 —— 找出「这块板子能扛几线程」, 并盯着温度和频率。

**为什么要有这么个东西** (2026-10-30 新板 A10010000000058)
    新板连续三次死在 **CPU 加压的起手一秒内**(`stress-ng --cpu 4` 起来之后
    第 1 秒, 板子被**硬复位** —— 日志里没有 panic、没有 thermal 消息)。
    而**老板子**(SN 31175c3d…)跑一模一样的序列**照过**(峰值 79℃ / 负载 99.6%)。

    ⇒ 这是**板子个体**的问题, 不是代码的问题。要定位, 得知道两件事:
      ① **它到底能扛几线程** —— 死在第几级, 那一级就是它的天花板;
      ② **死之前那个瞬间**板子是什么状态 —— 温度? 频率? 电压?

**这个探针盯四样东西**
    · **每个核区的温度**(高频采样)—— 既算升温速率, 也抓「假值」
      (厂商 thermal 驱动要是报出一个 ≥ critical trip 的假温度, 内核会**当场
      emergency shutdown**, 那就是「起手一秒内硬复位」最省事的解释);
    · **cpufreq 实际频率** —— governor 是 `ondemand` 时, 负载一上来频率就往上跳
      ⇒ **电流台阶跟负载台阶是同一瞬间** —— 这正是「棕断/PMIC 复位」最可能
      发力的时刻;
    · **负载率**(`/proc/stat` 相邻两次相减, 跟报告里 `avg_load_pct` 同一个口径);
    · **板子还活着没有** —— 脚本挂了就是死了。

**跑法**
    # 本板(老板子)当对照组:
    sudo .venv/bin/python scripts/probe_cpu_ramp.py
    # 换一台板子(新板), 逐级加压:
    sudo .venv/bin/python scripts/probe_cpu_ramp.py --steps 1,2,4,8 --sec 20

**不碰任何产线数据**: 只读 `/sys` `/proc`, 只起 `stress-ng`。
"""
import argparse
import glob
import os
import subprocess
import sys
import time

THERMAL_DIR = "/sys/class/thermal"
CPUFREQ_GLOB = "/sys/devices/system/cpu/cpu[0-9]*/cpufreq"


# ── 读温度 ────────────────────────────────────────────────────────────────
def read_zones():
    """{zone 类型名: 温度℃} —— 所有 thermal zone。"""
    out = {}
    try:
        zones = sorted(os.listdir(THERMAL_DIR))
    except OSError:
        return out
    for z in zones:
        if not z.startswith("thermal_zone"):
            continue
        t = os.path.join(THERMAL_DIR, z, "type")
        v = os.path.join(THERMAL_DIR, z, "temp")
        if not (os.path.exists(t) and os.path.exists(v)):
            continue
        try:
            with open(t) as f:
                ztype = f.read().strip().lower()
            with open(v) as f:
                out[ztype] = int(f.read().strip()) / 1000.0
        except (OSError, ValueError):
            continue
    return out


def read_freqs():
    """{cpu 号: 频率 MHz}。"""
    out = {}
    for path in sorted(glob.glob(CPUFREQ_GLOB)):
        cur = os.path.join(path, "scaling_cur_freq")
        if not os.path.exists(cur):
            continue
        try:
            with open(cur) as f:
                khz = int(f.read().strip())
        except (OSError, ValueError):
            continue
        cpu = path.split("/cpu")[1].split("/")[0]
        out[cpu] = khz / 1000.0
    return out


def read_cpu_stat():
    """(busy, total) 累计 tick —— 相邻两次相减就是占用率。"""
    try:
        with open("/proc/stat", "r") as f:
            parts = f.readline().split()
        vals = [int(x) for x in parts[1:]]
        idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
        return (sum(vals) - idle, sum(vals))
    except (OSError, ValueError, IndexError):
        return None


def trip_points():
    """{zone: [(类型, ℃)]} —— 顺便把 critical trip 找出来。"""
    out = {}
    try:
        zones = sorted(os.listdir(THERMAL_DIR))
    except OSError:
        return out
    for z in zones:
        if not z.startswith("thermal_zone"):
            continue
        base = os.path.join(THERMAL_DIR, z)
        t = os.path.join(base, "type")
        if not os.path.exists(t):
            continue
        try:
            with open(t) as f:
                ztype = f.read().strip().lower()
        except OSError:
            continue
        trips = []
        i = 0
        while True:
            tp = os.path.join(base, f"trip_point_{i}_type")
            tv = os.path.join(base, f"trip_point_{i}_temp")
            if not (os.path.exists(tp) and os.path.exists(tv)):
                break
            try:
                with open(tp) as f:
                    ttype = f.read().strip()
                with open(tv) as f:
                    cels = int(f.read().strip()) / 1000.0
                trips.append((ttype, cels))
            except (OSError, ValueError):
                pass
            i += 1
        out[ztype] = trips
    return out


# ── 跑一级 ────────────────────────────────────────────────────────────────
def run_step(threads, seconds, interval, alert_c):
    """跑一级加压, 返回这一级的观测结果。

    `alert_c` 撞上了就**立刻停手**(不等这一级跑完) —— 探针的第一职责是
    **别把板子烤坏**。
    """
    cmd = ["stress-ng", "--cpu", str(threads), "--timeout", f"{seconds:.1f}s",
           "--metrics-brief"]
    print(f"\n=== 第 {threads} 线程 · {seconds:.0f}s ===")
    print("  起跑: " + " ".join(cmd))
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT)
    except FileNotFoundError:
        print("  ✗ 没装 stress-ng")
        return None

    peak = {}
    spikes = []            # 超过告警线的读数 —— 「假值」的证据
    freq_min = {}
    freq_max = {}
    temps_over_time = []
    stat_prev = read_cpu_stat()
    load_samples = []
    stopped_early = False
    t0 = time.monotonic()
    try:
        while True:
            now = time.monotonic()
            if now - t0 >= seconds:
                break
            z = read_zones()
            f = read_freqs()
            stat_now = read_cpu_stat()
            for k, v in z.items():
                if v > peak.get(k, -1e9):
                    peak[k] = v
                if v >= alert_c:
                    spikes.append((round(now - t0, 2), k, v))
            for k, v in f.items():
                if v < freq_min.get(k, 1e9):
                    freq_min[k] = v
                if v > freq_max.get(k, 0):
                    freq_max[k] = v
            if stat_prev and stat_now:
                d_busy = stat_now[0] - stat_prev[0]
                d_total = stat_now[1] - stat_prev[1]
                if d_total > 0:
                    load_samples.append(d_busy * 100.0 / d_total)
            if stat_now:
                stat_prev = stat_now
            if z:
                temps_over_time.append((round(now - t0, 1), z))
            # ⚠️ **撞线就立刻停** —— 探针的第一职责是别把板子烤坏
            if any(v >= alert_c for v in z.values()):
                stopped_early = True
                print(f"  ⚠️ 撞 {alert_c}℃ 线 ⇒ 立刻停手(不等这一级跑完)")
                break
            time.sleep(interval)
    finally:
        if stopped_early:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:      # noqa: BLE001
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    pass

    dur = time.monotonic() - t0
    avg_load = sum(load_samples) / len(load_samples) if load_samples else 0.0
    # 升温速率: 第一秒的读数 → 最后一秒的读数 (℃/s)
    ramp = {}
    if len(temps_over_time) >= 3:
        first = temps_over_time[0][1]
        last = temps_over_time[-1][1]
        dt = max(temps_over_time[-1][0] - temps_over_time[0][0], 0.001)
        for k in first:
            if k in last:
                ramp[k] = (last[k] - first[k]) / dt

    print(f"  跑了 {dur:.1f}s, 平均负载 {avg_load:.1f}%")
    print("  峰值温度 / 升温速率:")
    for k in sorted(peak):
        r = ramp.get(k)
        rs = f"{r:+.2f}℃/s" if r is not None else "?"
        print(f"      {k:<22} {peak[k]:6.1f}℃   {rs}")
    print("  cpufreq (MHz, min→max):")
    for k in sorted(freq_min, key=lambda x: int(x) if x.isdigit() else 0):
        print(f"      cpu{k:<20} {freq_min[k]:7.0f} → {freq_max[k]:7.0f}")
    if spikes:
        print(f"  ⚠️⚠️ 有 {len(spikes)} 次读数 ≥ {alert_c}℃ —— "
              f"**这要么是快撞上 critical trip, 要么是驱动在报假值**:")
        for s in spikes[:10]:
            print(f"      t=+{s[0]}s {s[1]} = {s[2]:.1f}℃")
    else:
        print(f"  ✓ 没有任何一次读数 ≥ {alert_c}℃(没撞 critical trip, 也没假值)")
    return {"threads": threads, "peak": peak, "ramp": ramp,
            "avg_load": avg_load, "spikes": spikes,
            "freq_min": freq_min, "freq_max": freq_max}


def main():
    ap = argparse.ArgumentParser(description="分级加压探针")
    ap.add_argument("--steps", default="1,2,4,8",
                    help="逐级线程数, 逗号分隔 (默认 1,2,4,8)")
    ap.add_argument("--sec", type=float, default=15.0,
                    help="每一级跑多少秒 (默认 15)")
    ap.add_argument("--interval", type=float, default=0.1,
                    help="采样间隔秒 (默认 0.1)")
    ap.add_argument("--alert-c", type=float, default=95.0,
                    help="温度告警线℃ —— **撞上就立刻停手**(默认 95)")
    args = ap.parse_args()

    steps = [int(s) for s in args.steps.split(",") if s.strip()]

    print("=== 板子状态 ===")
    try:
        with open("/proc/cmdline") as f:
            for tok in f.read().split():
                if tok.startswith("snum="):
                    print("SN:", tok.split("=", 1)[1])
    except OSError:
        pass
    print("核数:", os.cpu_count())
    tp = trip_points()
    for z, trips in sorted(tp.items()):
        crit = [t for t in trips if t[0] == "critical"]
        if crit:
            print(f"  {z:<22} critical trip = {crit[0][1]:.0f}℃")
        else:
            print(f"  {z:<22} (没有 critical trip)")
    print("当前温度:", read_zones())

    results = []
    for s in steps:
        r = run_step(s, args.sec, args.interval, args.alert_c)
        if r is None:
            break
        results.append(r)
        if r["spikes"]:
            break        # 撞线了就收, 别再往上加

    print("\n" + "=" * 60)
    print("汇总")
    print("=" * 60)
    print(f"{'线程':>4}  {'平均负载':>8}  {'最热核区':>20}  {'峰值':>8}  "
          f"{'升温速率':>12}")
    for r in results:
        hotname = max(r["peak"], key=lambda k: r["peak"][k]) if r["peak"] else "?"
        hot = r["peak"].get(hotname, 0.0)
        rs = r["ramp"].get(hotname)
        print(f"{r['threads']:>4}  {r['avg_load']:>7.1f}%  {hotname:>20}  "
              f"{hot:>6.1f}℃  {('%+.2f℃/s' % rs) if rs is not None else '?':>12}")
    allspikes = [(r["threads"], s) for r in results for s in r["spikes"]]
    if allspikes:
        print(f"\n⚠️⚠️ 全程 {len(allspikes)} 次读数超过告警线 —— "
              f"**驱动可能在报假值**(真撞 critical trip 内核会 emergency shutdown)")
    else:
        print("\n✓ 全程没有一次温度读数超过告警线")
    print("\n板子还活着 ⇒ 这一轮没死。死在第几级, 那一级就是天花板。")


if __name__ == "__main__":
    sys.exit(main())
