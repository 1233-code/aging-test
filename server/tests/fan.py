"""风扇测试 - 风扇使能状态监测

硬件背景:
  A527 板子上风扇接 40-pin 排针 PI5, 由 vendor 内核驱动 `fan_gpio_control` 直接控制.
  vendor 同时在 /proc/rp_gpio/ 下暴露了所有 GPIO 直通文件 (0666 权限, 无需 root),
  其中 /proc/rp_gpio/pi5 就是风扇使能脚的当前电平, 单字节 '0' 或 '1'.

读 GPIO 路径优先级 (§1):
  1. /proc/rp_gpio/pi5  (厂家直通文件, 最简最快, 唯一推荐路径)
  2. Python libgpiod v1   (fallback: 备板不挂 /proc/rp_gpio/ 时)
  3. gpioget 命令        (fallback: 只装了 libgpiod-tools 时)
  4. sysfs /sys/class/gpio (legacy: 内核 < 4.8 镜像, A527 几乎用不到)
  5. 失败 → 明确报错

PI5 自动探测 (§2) 同样优先用 Python libgpiod v1 找 "PI5" label.
"""
import asyncio
import os
import re
import shutil
import time
from typing import Optional, Tuple

from .base import TestCase, TestContext, TestResult, register
from ._tools import with_sudo

# 默认 vendor 直通路径 (可通过 config.fan.procfs_path 覆盖)
_DEFAULT_PROCFS_PATH = "/proc/rp_gpio/pi5"


# ──── 工具函数: 命令查找 ────
def _find_tool(name: str) -> Optional[str]:
    p = shutil.which(name)
    if p:
        return p
    for d in ("/usr/sbin", "/sbin", "/usr/local/sbin"):
        c = os.path.join(d, name)
        if os.access(c, os.X_OK, follow_symlinks=False):
            return c
    return None


def _try_import_gpiod_v1() -> Optional[object]:
    """尝试 import 官方 libgpiod v1 Python 绑定 (包名 python3-libgpiod).

    pip 装的 'gpiod' 包不是官方, 不能用. 这里通过 feature check 区分.
    官方 v1 接口有: gpiod.Chip, gpiod.LINE_REQ_DIR_IN
    """
    try:
        import gpiod
        if hasattr(gpiod, "Chip") and hasattr(gpiod, "LINE_REQ_DIR_IN"):
            return gpiod
    except ImportError:
        pass
    return None


_GPIOD = _try_import_gpiod_v1()


# ──── P1: vendor /proc/rp_gpio/ 直通读取 (主推) ────
def _read_via_procfs(path: str) -> Optional[int]:
    """读 /proc/rp_gpio/pi5 这种厂家直通文件.

    返回 0/1, 文件不存在 / read 失败返回 None.
    这种文件永远是单个字符 + 换行, 直接 read 一个 byte 就够.
    """
    try:
        with open(path, "rb") as f:
            data = f.read(4)  # '0\n' / '1\n' / 极短
    except (FileNotFoundError, PermissionError, OSError):
        return None
    if not data:
        return None
    # 只看第一个字节
    if data[0] == ord('0'):
        return 0
    if data[0] == ord('1'):
        return 1
    return None  # 数据畸形当作失败


# ──── P2-P4 fallback: libgpiod / gpioget / sysfs ────
def _read_via_gpiod_v1(chip: int, line: int) -> Optional[int]:
    """用官方 libgpiod v1 读 GPIO, 返回 0/1 或 None (失败)."""
    if _GPIOD is None:
        return None
    try:
        chip_obj = _GPIOD.Chip(f"/dev/gpiochip{chip}")
        line_obj = chip_obj.get_line(line)
        line_obj.request(
            consumer="aging-test-fan",
            type=_GPIOD.LINE_REQ_DIR_IN,
            default_val=0,
        )
        val = line_obj.get_value()
        line_obj.release()
        return val
    except Exception:
        return None


def _find_pi5_via_gpiod_v1() -> Optional[Tuple[int, int]]:
    """用 libgpiod v1 找标签为 'PI5' 的 line."""
    if _GPIOD is None:
        return None
    for chip in range(8):
        chip_path = f"/dev/gpiochip{chip}"
        if not os.path.exists(chip_path):
            continue
        try:
            chip_obj = _GPIOD.Chip(chip_path)
            for offset in range(chip_obj.num_lines()):
                try:
                    info = chip_obj.get_line_info(offset)
                    name = info.name or ""
                    if "PI5" in name:
                        return chip, offset
                except Exception:
                    break
        except Exception:
            continue
    return None


def _detect_pi5_gpio() -> Optional[Tuple[int, int]]:
    """自动探测 PI5 物理针脚 5 的 (chip, line) — 走 libgpiod 命令路径."""
    auto = _find_pi5_via_gpiod_v1()
    if auto:
        return auto
    gpioinfo = _find_tool("gpioinfo")
    if gpioinfo:
        import subprocess
        for chip in range(8):
            chip_path = f"/dev/gpiochip{chip}"
            if not os.path.exists(chip_path):
                continue
            try:
                out = subprocess.check_output(
                    # §11.2: /dev/gpiochip* 非特权用户读不到, 走命令级 sudo -n
                    with_sudo([gpioinfo, chip_path]),
                    stderr=subprocess.DEVNULL, timeout=2,
                ).decode(errors="replace")
            except Exception:
                continue
            for ln in out.splitlines():
                m = re.match(r'\s*line\s+(\d+):\s+(.*)', ln)
                if m and "PI5" in m.group(2):
                    return chip, int(m.group(1))
    return None


# ──── 风扇测试 ────
@register
class FanTest(TestCase):
    name = "fan"
    display_name = "风扇"
    description = "GPIO 读风扇使能状态 (无转速反馈)"

    async def run(self, ctx: TestContext) -> TestResult:
        result = TestResult()
        t_cfg = ctx.test_config
        duration = max(5, int(t_cfg.get("check_duration_sec", 30)))
        min_high = bool(t_cfg.get("min_gpio_high", True))

        fan_cfg = ctx.config.get("fan", {})
        procfs_path = fan_cfg.get("procfs_path", _DEFAULT_PROCFS_PATH)
        procfs_exists = os.path.exists(procfs_path)

        await ctx.on_progress(0, "探测 IO 接口")

        # ── 决定走哪条读路径 ──
        read_func, used_io, used_method = self._resolve_reader(
            fan_cfg, procfs_path, procfs_exists
        )

        # procfs 路径的话, read_func 是同步; 它路径是 async/sync 混搭
        is_async = asyncio.iscoroutinefunction(read_func)
        active = fan_cfg.get("active_level", "high").lower()

        result.metrics["io_method"] = used_io
        result.metrics["active_level"] = active
        result.metrics["procfs_path"] = procfs_path
        result.metrics["procfs_available"] = procfs_exists
        result.metrics["detect_method"] = used_method

        await ctx.on_log(f"[风扇] {used_method}")
        await ctx.on_log(f"[风扇] IO: {used_io}")
        await ctx.on_log(f"[风扇] 监测 {duration}s, active={active}")

        # ── 监测循环 ──
        all_active = True
        t0 = time.time()
        samples = []
        while time.time() - t0 < duration:
            if await ctx.is_cancelled():
                result.status = "stopped"
                return result
            try:
                v = await read_func() if is_async else read_func()
            except Exception as e:
                await ctx.on_log(f"[风扇] ⚠️ 读取失败: {e!r}")
                v = -1
            if v is None:
                v = -1
            samples.append(v)
            is_active = (v == 1) if active == "high" else (v == 0)
            if v == -1:
                await ctx.on_log(f"[风扇] ⚠️ 读失败")
            elif not is_active:
                all_active = False
                await ctx.on_log(f"[风扇] ⚠️ GPIO={v} (非 {active})")
            await ctx.on_metric("gpio_value", v)
            elapsed = time.time() - t0
            await ctx.on_progress(
                int(elapsed / duration * 100),
                f"{elapsed:.0f}s / {duration}s, GPIO={v}",
            )
            await asyncio.sleep(1)

        # ── 结果 ──
        # 注意: 这里的 if-else 是把"目标电平"算出来, 再用生成器 if 过滤.
        # 不能写成 `if (v == 1) if active == "high" else (v == 0)` — generator 里的 if 不接三元.
        target_val = 1 if active == "high" else 0
        ok = sum(1 for v in samples if v == target_val)
        bad = sum(1 for v in samples if v == -1)
        result.metrics["samples"] = len(samples)
        result.metrics["active_samples"] = ok
        result.metrics["error_samples"] = bad

        if bad == len(samples) and len(samples) > 0:
            # 全读失败, 大概率 IO 路径在运行时挂了
            result.status = "error"
            result.error = f"全部 {len(samples)} 次读都失败 (IO 路径: {used_io})"
        elif not all_active and min_high:
            result.status = "failed"
            result.error = (
                f"监测期间出现非 {active} 状态 "
                f"(active={ok}/{len(samples)}, error={bad})"
            )
        else:
            result.status = "passed"
        return result

    def _resolve_reader(self, fan_cfg, procfs_path, procfs_exists):
        """决定 IO 路径. 返回 (read_func, used_io_label, used_method_label).

        优先级:
          P1 /proc/rp_gpio/pi5 (推荐, 唯一 rootless 路径)
          P2 Python libgpiod v1
          P3 gpioget 命令
          P4 sysfs /sys/class/gpio
        """
        # ── P1: vendor 直通文件 ──
        if procfs_exists:
            def _proc_reader():
                return _read_via_procfs(procfs_path)
            used_io = f"vendor procfs ({procfs_path})"
            used_method = "vendor 直通 (推荐, 无依赖, 无需 root)"
            return _proc_reader, used_io, used_method

        # ── P2-P4: fallback ──
        auto = _detect_pi5_gpio()
        if auto:
            chip, line = auto
            used_chip, used_line = chip, line
            used_method = "自动探测 (PI5 label)"
        else:
            used_chip = int(fan_cfg.get("gpio_chip", 0))
            used_line = int(fan_cfg.get("gpio_line", 5))
            used_method = "config 兜底 (PI5 label 未找到)"

        # P2: Python libgpiod
        if _GPIOD is not None:
            chip, line = used_chip, used_line
            used_io = f"Python libgpiod (gpiochip{chip} line {line})"
            used_method += f" / {used_io}"

            def _gp_reader(chip=chip, line=line):
                return _read_via_gpiod_v1(chip, line)
            return _gp_reader, used_io, used_method

        # P3: gpioget 命令
        gpioget = _find_tool("gpioget")
        if gpioget:
            chip, line = used_chip, used_line

            async def _read_gpioget():
                proc = await asyncio.create_subprocess_exec(
                    # §11.2: /dev/gpiochip* 非特权用户读不到, 走命令级 sudo -n
                    *with_sudo([gpioget, f"gpiochip{chip}", str(line)]),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                try:
                    out, _ = await asyncio.wait_for(proc.communicate(), timeout=3)
                except asyncio.TimeoutError:
                    proc.kill()
                    return None
                txt = out.decode().strip()
                try:
                    return int(txt)
                except ValueError:
                    return None
            used_io = f"gpioget 命令 (gpiochip{chip} line {line})"
            used_method += f" / {used_io}"
            return _read_gpioget, used_io, used_method

        # P4: sysfs
        line = used_line
        sysfs = f"/sys/class/gpio/gpio{line}/value"
        if os.path.exists("/sys/class/gpio"):
            try:
                with open("/sys/class/gpio/export", "w") as f:
                    f.write(str(line))
                time.sleep(0.1)
                with open(f"/sys/class/gpio/gpio{line}/direction", "w") as f:
                    f.write("in")
            except Exception as e:
                # 返回错误哨兵: 让 run() 标 error
                def _err_reader(_e=e):
                    raise RuntimeError(
                        f"GPIO {line} sysfs 导出失败: {_e!r}. "
                        f"建议装 vendor 镜像 (含 /proc/rp_gpio/) "
                        f"或 pip install gpiod"
                    )
                return _err_reader, f"sysfs 导出失败: {e!r}", "sysfs fallback (导出失败)"

            def _read_sysfs(_sysfs=sysfs):
                with open(_sysfs) as f:
                    return int(f.read().strip())
            used_io = f"sysfs ({sysfs})"
            used_method += f" / {used_io}"
            return _read_sysfs, used_io, used_method

        # 全部不可用 → 返回异常 reader
        def _unavailable():
            raise RuntimeError(
                f"无法读 GPIO: /proc/rp_gpio/pi5 不存在, libgpiod 没装, "
                f"gpioget 命令无, sysfs 不存在. "
                f"请确认 vendor 镜像或装 python3-libgpiod"
            )
        return _unavailable, "(无可用 IO 路径)", "❌ 全部 IO 路径不可用"
