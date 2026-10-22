"""HDMI 测试 - EDID 读取 + 模式切换 (modetest)

优先级:
  1. /sys/class/drm/ 读 connectors 状态 + 分辨率 (不需 root)
  2. modetest (DRM/KMS 模式设置,需 root + /dev/dri/ 存在)
"""
import asyncio
import os
import re
import signal
import time
from pathlib import Path
from typing import List, Tuple

from .base import TestCase, TestContext, TestResult, register
from ._tools import find_tool, is_root, with_sudo, kill_proc


def _read_drm_sysfs() -> list:
    """读 /sys/class/drm/ 下所有 HDMI connector 的状态和 modes.

    返回 list[dict] = [{"name": "card0-HDMI-A-1", "status": "connected",
                        "modes": ["1920x1080", "1280x720", ...]}]
    """
    drm_dir = Path("/sys/class/drm")
    if not drm_dir.exists():
        return []
    out = []
    for entry in drm_dir.iterdir():
        name = entry.name
        # 只关心 HDMI/DP connector
        if "HDMI" not in name.upper() and "DP" not in name.upper():
            continue
        status_file = entry / "status"
        modes_file = entry / "modes"
        if not (status_file.exists() and modes_file.exists()):
            continue
        try:
            status = status_file.read_text().strip()
        except Exception:
            continue
        try:
            modes = [
                m.strip() for m in modes_file.read_text().splitlines()
                if m.strip() and "x" in m
            ]
        except Exception:
            modes = []
        out.append({
            "name": name,
            "status": status,
            "modes": modes,
        })
    return out


async def _run_modetest(modetest_bin: str, args: List[str], timeout: int = 15) -> Tuple[int, str, str]:
    """跑 modetest, 返回 (rc, stdout, stderr).

    §11.2: modetest 需 root 切模式, 走命令级 sudo -n。

    **必须带 timeout_sec**: modetest 默认**无限循环**刷模式, 不会自己退出。
    而它是 sudo 起的 root 进程, 服务这边发不了信号 (EPERM), 所以收尾只能靠
    root 领地内部的 `timeout` —— 见 _tools.with_sudo 的「为什么要 timeout」。
    """
    cmd = with_sudo([modetest_bin, *args], timeout_sec=timeout)
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout + 8)
    except asyncio.TimeoutError:
        await kill_proc(proc, signal.SIGKILL, wait_sec=1.0)
        return -1, "", "timeout"
    return proc.returncode, out.decode(errors="replace"), err.decode(errors="replace")


@register
class HDMITest(TestCase):
    name = "hdmi"
    display_name = "HDMI 输出"
    description = "读取 EDID + 切换显示模式 (modetest)"

    def __init__(self):
        self.proc = None
        self.cancel_done = False

    async def cancel(self):
        if self.proc and self.proc.returncode is None and not self.cancel_done:
            self.cancel_done = True
            # 不用裸 terminate/kill —— 子进程是 sudo 起的 root 进程, 普通用户
            # 对它发信号必然 EPERM, 裸调会把收尾自己炸掉, 还让 root 测试进程
            # 泄漏在后台。见 _tools.kill_proc 的说明。
            await kill_proc(self.proc, wait_sec=2.0)

    async def run(self, ctx: TestContext) -> TestResult:
        result = TestResult()
        t_cfg = ctx.test_config
        times = max(1, int(t_cfg.get("times", 1)))
        target_res = t_cfg.get("target_resolution", "1920x1080@60")

        # Step 1: 先读 /sys/class/drm/ (不需要 root)
        sysfs_conns = _read_drm_sysfs()
        await ctx.on_log(f"[HDMI] sysfs: 找到 {len(sysfs_conns)} 个 HDMI/DP connector")
        for c in sysfs_conns:
            n_modes = len(c["modes"])
            await ctx.on_log(
                f"  - {c['name']}: {c['status']} ({n_modes} modes)"
            )
            if n_modes > 0 and n_modes <= 6:
                for m in c["modes"][:6]:
                    await ctx.on_log(f"      · {m}")

        connected = [c for c in sysfs_conns if c["status"] == "connected"]
        if not connected:
            if not sysfs_conns:
                result.status = "skipped"
                result.error = (
                    "/sys/class/drm 无 HDMI/DP connector. 可能: 板子 DTS 未启用显示节点. "
                    "建议: 跳过此测试项"
                )
                await ctx.on_log(f"[HDMI] ⏭ 跳过: {result.error[:100]}")
                return result
            result.status = "skipped"
            result.error = "HDMI connector 存在但全部未连接 (未接显示器?)"
            await ctx.on_log(f"[HDMI] ⏭ 跳过: {result.error}")
            return result

        active = connected[0]
        result.metrics["connector"] = active["name"]
        result.metrics["sysfs_status"] = active["status"]
        result.metrics["modes_count"] = len(active["modes"])
        if active["modes"]:
            result.metrics["current_resolution"] = active["modes"][0]
            await ctx.on_log(
                f"[HDMI] ✓ 连接: {active['name']}  当前模式: {active['modes'][0]}"
            )
        else:
            result.metrics["current_resolution"] = "(无 mode)"
            await ctx.on_log(f"[HDMI] ⚠️ {active['name']} 已连接但无 mode")

        # 解析目标分辨率验证
        tw, th = 1920, 1080
        tr = 60
        m = re.match(r"(\d+)x(\d+)(?:@(\d+))?", target_res)
        if m:
            tw, th = int(m.group(1)), int(m.group(2))
            tr = int(m.group(3)) if m.group(3) else 60
        target_mode = f"{tw}x{th}@{tr}"
        target_match = any(target_mode in m for m in active["modes"])
        result.metrics["target_mode_in_modes"] = target_match
        if target_match:
            await ctx.on_log(f"[HDMI] 目标模式 {target_mode} 在支持列表中 ✓")
        else:
            await ctx.on_log(
                f"[HDMI] ⚠️ 目标模式 {target_mode} 不在支持列表, 当前屏报告 {len(active['modes'])} 个 mode"
            )

        # Step 2: 试 modetest 切模式 (可选, 需要 root + /dev/dri/ 存在)
        modetest_bin = find_tool("modetest")
        if modetest_bin and os.path.exists("/dev/dri"):
            await ctx.on_log(f"[HDMI] 尝试 modetest 设置模式 {tw}x{th}@{tr}")
            conn_id_part = active["name"].split("-")[-2] if "-" in active["name"] else "0"
            rc, out, err = await _run_modetest(
                modetest_bin,
                ["-s", f"{conn_id_part}@0:{tw}x{th}@{tr}"],
                timeout=10,
            )
            if rc == 0:
                await ctx.on_log("[HDMI] ✓ modetest 模式设置成功")
                result.metrics["modetest_ok"] = True
            else:
                err_l = err.lower()
                if "no device" in err_l or "no crtc" in err_l or "permission" in err_l:
                    await ctx.on_log(
                        f"[HDMI] ⏭ modetest 不能用 (sysfs 已确认 HDMI 在, 不算 fail): {err[:100]}"
                    )
                else:
                    await ctx.on_log(f"[HDMI] ⚠️ modetest 失败: {err[:100]}")
        else:
            await ctx.on_log(
                "[HDMI] ⏭ 跳过 modetest 切模式 (无 modetest 或 /dev/dri 不存在)"
            )

        if active["modes"]:
            result.status = "passed"
        else:
            result.status = "failed"
            result.error = "HDMI connected 但无 mode"
        return result
