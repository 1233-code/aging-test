#!/usr/bin/env python3
"""「应用 vs 终端」对照实验 —— 一次跑完, 板子死在哪个阶段, 那项就是元凶。

**背景 (2026-11-02, 新板 A10000000000010)**
    老化测试应用**每次**都在「CPU 压力」这一项里把板子**硬复位**,
    而**同样的加压命令在终端里手动跑, 板子全程没事** (2026-11-02 实测)。

    ⇒ 触发条件一定在**应用与终端的差别**里, 而不在「CPU 满载」本身。
    本脚本把已知的差别**一项一项**加回去, 找出是哪一项要了板子的命。

**四个阶段** (每个阶段都是 1→2→3→4 逐级, 每级 5s —— 跟应用死的那一级对齐)

    S1 终端基线     : `stress-ng --cpu N --timeout 5.0s --metrics-brief`
                      —— 就是用户手动跑的那条命令 (什么包装都没有)
    S2 +sudo 包装   : `sudo -n timeout -k 5.0 35.0 stress-ng --cpu N ...`
                      —— 应用用的是这条 (sudo 免密 + timeout 当内部 killer)
    S3 +采样器      : S2 **外加**一个后台采样线程 (200ms 一次, 照抄应用的
                      读法: /proc/stat + 所有核区 + 所有 cpufreq + 所有供电轨),
                      并且**边跑边写文件**(每行 flush, ≤1s 一次 fsync)
                      —— 应用在加压时干的就是这两件事
    S4 +后台负载    : S2, 但**此时老化测试的应用正在跑、页面开着**
                      —— 应用自己 + 浏览器那点后台占用

    S1 活着 / S2 死 ⇒ sudo+timeout 包装(或者 root 身份)是要命的那一项
    S2 活 / S3 死 ⇒ 采样器/写文件(尤其 fsync)是要命的那一项
    S3 活 / S4 死 ⇒ 后台负载(服务+浏览器)是要命的那一项
    四个都活      ⇒ 触发条件在**应用的编排**里(逐片发命令的节奏、
                     每片之间那次「起跑」fsync、tick 推送、WebSocket 队列…)

**为什么每一步都要 fsync 落盘**: 板子是被**硬复位**打死的 ⇒ **页缓存整个丢**
⇒ 只 flush 的日志跟着板子一起没了(2026-11-02 实测: 板子上那个证据文件
是 **0 字节**)。⇒ 阶段标记**必须 fsync**, 否则「死在第几阶段」这个信息
本身就会丢 —— 那才是真的一点头绪都没有。

**不碰产线数据**: 只读 `/sys` `/proc`, 只起 `stress-ng`, 只往**自己的家目录**
写日志(不写 /var/lib/aging-test, 不碰 history.db)。

**跑法**
    # 建议用 root 跑 (stress-ng 的 root 身份本身也是被测变量之一)
    sudo python3 scripts/probe_app_vs_terminal.py
    # 只跑某几个阶段 / 换线程台阶
    sudo python3 scripts/probe_app_vs_terminal.py --stages 1,2 --threads 1,2,3,4
    # 板子死之后: 看它死在哪
    cat ~/aging_probe.log
"""

import argparse
import glob
import os
import subprocess
import sys
import threading
import time

THERMAL_DIR = "/sys/class/thermal"
CPUFREQ_GLOB = "/sys/devices/system/cpu/cpu[0-9]*/cpufreq"
REGULATOR_GLOB = "/sys/class/regulator/regulator.*"

# 应用每一级的长度 (config.yaml: duration_sec=60 / slice_sec=5 ⇒ 一片 5s)
DEFAULT_SLICE_SEC = 5.0
# 应用死的那一级: 1→2→3→4 里死在 4 那一片
DEFAULT_THREADS = "1,2,3,4"


# ── 日志 (每行 fsync 落盘) ────────────────────────────────────────────────
class Marker:
    """**每写一行就 fsync** 的日志 —— 硬复位会带走页缓存, 不落盘就没了。"""

    def __init__(self, path):
        self.path = path
        self._fh = open(path, "a", encoding="utf-8")

    def write(self, text):
        line = f"[{time.strftime('%H:%M:%S')}] {text}"
        print(line, flush=True)
        try:
            self._fh.write(line + "\n")
            self._fh.flush()
            os.fsync(self._fh.fileno())
        except Exception:
            pass

    def close(self):
        try:
            self._fh.close()
        except Exception:
            pass


# ── 读板子状态 (照抄应用的读法) ──────────────────────────────────────────
def read_zones():
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


def read_freq():
    """当前**最高**频率 (MHz) —— 跟应用记的同一个口径。"""
    hi = None
    for path in sorted(glob.glob(CPUFREQ_GLOB)):
        cur = os.path.join(path, "scaling_cur_freq")
        try:
            with open(cur) as f:
                khz = int(f.read().strip())
        except (OSError, ValueError):
            continue
        if hi is None or khz > hi:
            hi = khz
    return hi / 1000.0 if hi else None


def read_rails():
    """{轨名: mV} —— 全部的轨, 不挑 (跟应用一样)。"""
    out = {}
    for r in sorted(glob.glob(REGULATOR_GLOB)):
        try:
            with open(os.path.join(r, "name")) as f:
                name = f.read().strip()
            with open(os.path.join(r, "microvolts")) as f:
                uv = int(f.read().strip())
        except (OSError, ValueError):
            continue
        if name and uv > 0:
            out[name] = uv // 1000
    return out


def read_cpu_stat():
    """(busy, total) 累计 tick —— 相邻两次相减就是占用率 (跟应用同口径)。"""
    try:
        with open("/proc/stat", "r") as f:
            parts = f.readline().split()
        vals = [int(x) for x in parts[1:]]
        idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
        return (sum(vals) - idle, sum(vals))
    except (OSError, ValueError, IndexError):
        return None


def loadavg():
    try:
        with open("/proc/loadavg") as f:
            return f.read().strip()
    except OSError:
        return "?"


# ── S3 的采样器 (照抄应用的 _sample_evidence + _EvidenceLog) ──────────────
class Sampler(threading.Thread):
    """200ms 一次的现场采样 + 边跑边写文件。

    **为什么单独一个阶段**: 应用的加压过程里, 除了 `stress-ng`, 还并行干着
    两件事 —— ① 200ms 一次地读一圈 `/proc`+`/sys`; ② 把每一行**写进文件**
    (每行 flush, 最多 1 秒一次 fsync, 「起跑」那一行立刻 fsync)。
    本类把这两件事原样搬过来 —— 要是它能把板子弄死, 那就是**应用代码**
    的问题, 跟 `stress-ng` 无关。
    """

    def __init__(self, path, threads_getter):
        super().__init__(daemon=True)
        self.path = path
        self.threads_getter = threads_getter
        self.stop_flag = threading.Event()
        self._fh = open(path, "w", encoding="utf-8")
        self._t0 = time.monotonic()
        self._last_fsync = 0.0
        self._prev_rails = {}
        self._prev_stat = read_cpu_stat()

    def _fsync(self):
        try:
            os.fsync(self._fh.fileno())
            self._last_fsync = time.monotonic()
        except Exception:
            pass

    def _write(self, note=""):
        stat_now = read_cpu_stat()
        load_pct = None
        if self._prev_stat and stat_now:
            d_busy = stat_now[0] - self._prev_stat[0]
            d_total = stat_now[1] - self._prev_stat[1]
            if d_total > 0:
                load_pct = d_busy * 100.0 / d_total
        if stat_now:
            self._prev_stat = stat_now
        zones = read_zones()
        hot = max(zones.values()) if zones else None
        freq = read_freq()
        rails = read_rails()
        dv = ""
        if self._prev_rails:
            changed = [f"{k}:{self._prev_rails.get(k, '?')}->{v}"
                       for k, v in sorted(rails.items())
                       if self._prev_rails.get(k) != v]
            if changed:
                dv = " dV=" + ",".join(changed)
        self._prev_rails = dict(rails)
        hot_s = f"{hot:.1f}" if hot is not None else "?"
        z = " ".join(f"{k}={v:.1f}" for k, v in sorted(zones.items()))
        line = (f"t=+{time.monotonic() - self._t0:.1f}s "
                f"threads={self.threads_getter()} hot={hot_s}")
        if z:
            line += " " + z
        if load_pct is not None:
            line += f" load={load_pct:.1f}%"
        if freq is not None:
            line += f" freq={freq:.0f}MHz"
        line += dv
        if note:
            line += " " + note
        try:
            self._fh.write(line + "\n")
            self._fh.flush()
            if note or (time.monotonic() - self._last_fsync) >= 1.0:
                self._fsync()
        except Exception:
            pass

    def run(self):
        while not self.stop_flag.is_set():
            try:
                self._write()
            except Exception:
                pass
            self.stop_flag.wait(0.2)

    def stop(self):
        self.stop_flag.set()
        try:
            self._fh.flush()
            self._fsync()
            self._fh.close()
        except Exception:
            pass


# ── 跑一级加压 ──────────────────────────────────────────────────────────
def build_cmd(threads, slice_sec, wrapped):
    """应用用的那条命令 vs 终端手敲的那条命令。"""
    base = ["stress-ng", "--cpu", str(threads),
            "--timeout", f"{slice_sec:.1f}s", "--metrics-brief"]
    if not wrapped:
        return base
    # 应用: with_sudo(cmd, timeout_sec=slice_len + 30) ⇒ sudo -n timeout -k 5.0 35.0
    tmo = "/usr/bin/timeout"
    if not os.path.exists(tmo):
        tmo = "timeout"
    return ["sudo", "-n", tmo, "-k", "5.0", f"{slice_sec + 30:.1f}"] + base


def run_slice(threads, slice_sec, wrapped, sampler, marker):
    """跑一级, 返回 (rc, 观察到的负载%)。"""
    cmd = build_cmd(threads, slice_sec, wrapped)
    marker.write(f"  起跑 {threads} 线程: {' '.join(cmd)}")
    if sampler is not None:
        sampler._write(note=f"起跑 {threads} 线程")

    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT)
    except FileNotFoundError:
        marker.write("  ✗ 没装 stress-ng (装一下: apt install stress-ng)")
        return None, 0.0

    stat0 = read_cpu_stat()
    t0 = time.monotonic()
    samples = []
    out = b""
    try:
        # 边等边读 stdout —— stress-ng 带 --metrics-brief 时大部分时间不吐行
        while True:
            if time.monotonic() - t0 >= slice_sec + 3:
                break
            try:
                proc.wait(timeout=0.2)
                break
            except subprocess.TimeoutExpired:
                pass
            stat_now = read_cpu_stat()
            if stat0 and stat_now:
                d_busy = stat_now[0] - stat0[0]
                d_total = stat_now[1] - stat0[1]
                if d_total > 0:
                    samples.append(d_busy * 100.0 / d_total)
    finally:
        if proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                pass
        try:
            out = proc.stdout.read() or b""
        except Exception:
            pass
    dur = time.monotonic() - t0
    avg_load = sum(samples) / len(samples) if samples else 0.0
    rc = proc.returncode
    marker.write(f"  跑完 {threads} 线程 · {dur:.1f}s · exit {rc} · "
                 f"平均负载 {avg_load:.1f}% · 负载均值 {loadavg()}")
    if out:
        tail = out.decode("utf-8", "replace").strip().splitlines()[-3:]
        for line in tail:
            marker.write(f"    stress-ng: {line}")
    return rc, avg_load


# ── 一个阶段 ────────────────────────────────────────────────────────────
STAGE_DESC = {
    1: "终端基线 (裸 stress-ng, 无 sudo 包装, 无采样器)",
    2: "+sudo -n timeout -k 5.0 包装 (跟应用完全一样的调用方式)",
    3: "+200ms 采样器 + 边跑边写文件 (照抄应用)",
    4: "+后台负载 (此时应用正在跑、页面开着)",
}


def run_stage(stage, threads_list, slice_sec, marker, out_dir):
    marker.write(f"===== 阶段 S{stage} 开始: {STAGE_DESC.get(stage, '?')} =====")
    z = read_zones()
    marker.write(f"  板子状态: loadavg {loadavg()} · 核数 {os.cpu_count()} · "
                 f"温度 {dict(sorted(z.items()))}")
    sampler = None
    if stage == 3:
        ev_path = os.path.join(out_dir, "aging_probe_evidence.txt")
        sampler = Sampler(ev_path, lambda: threads_list[0])
        marker.write(f"  采样器写入: {ev_path}")
    try:
        for th in threads_list:
            rc, load = run_slice(th, slice_sec, wrapped=(stage >= 2),
                                 sampler=sampler, marker=marker)
            if rc is None:
                break
    finally:
        if sampler is not None:
            sampler.stop()
    marker.write(f"===== 阶段 S{stage} 结束 (活下来了) =====")


def wait_for_enter(prompt):
    """等回车 —— S4 需要人先把页面打开。"""
    print(prompt, flush=True)
    if not sys.stdin.isatty():
        return
    try:
        sys.stdin.readline()
    except Exception:
        pass


def main():
    ap = argparse.ArgumentParser(description="「应用 vs 终端」对照实验")
    ap.add_argument("--stages", default="1,2,3,4",
                    help="跑哪几个阶段, 逗号分隔 (默认 1,2,3,4)")
    ap.add_argument("--threads", default=DEFAULT_THREADS,
                    help="线程台阶, 逗号分隔 (默认 1,2,3,4)")
    ap.add_argument("--sec", type=float, default=DEFAULT_SLICE_SEC,
                    help="每级跑多少秒 (默认 5)")
    ap.add_argument("--out", default=os.path.expanduser("~"),
                    help="日志目录 (默认家目录)")
    args = ap.parse_args()

    stages = [int(s) for s in args.stages.split(",") if s.strip()]
    threads_list = [int(s) for s in args.threads.split(",") if s.strip()]

    os.makedirs(args.out, exist_ok=True)
    marker = Marker(os.path.join(args.out, "aging_probe.log"))
    marker.write("======== 「应用 vs 终端」对照实验 开始 ========")
    marker.write(f"阶段: {stages} · 线程台阶: {threads_list} · 每级 {args.sec}s")
    marker.write(f"本进程 uid={os.getuid()} (应用里的 stress-ng 是 root 起的)")

    for st in stages:
        if st == 4:
            wait_for_enter(
                "  >>> 现在请在板子上打开老化测试的页面(应用跑着、页面开着), "
                "然后按回车继续 <<<")
        run_stage(st, threads_list, args.sec, marker, args.out)

    marker.write("======== 全部阶段跑完, 板子还活着 ========")
    marker.close()
    print("\n日志: " + os.path.join(args.out, "aging_probe.log"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
