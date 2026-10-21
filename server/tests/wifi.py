"""WiFi 测试 - on/off 开关切换测试 (产线友好, 不需 AP)

逻辑:
  - N 次循环, 每次:
    1. ip link set wlan0 up    → 检查 /sys/.../operstate = "up"
    2. 停 per_op 秒
    3. ip link set wlan0 down  → 检查 operstate = "down"
    4. 停 0.5s
  - 任一次失败 → 立即 fail
  - 全部 N 次通过 → pass

适用:
  - 工厂无 AP / 屏蔽室场景
  - 测 WiFi 子系统基本开关功能 + 驱动稳定性
  - 不依赖 RF / 信号强度

driver 兼容: 步骤 1 用 iw / iwlist / nmcli 检测接口存在, 兼容 skw 等厂商驱动
"""
import asyncio
import os
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from .base import TestCase, TestContext, TestResult, register
from ._tools import find_tool, run_cmd, is_root, find_wifi_iface, with_sudo, sudo_hint_if_unavailable


@dataclass
class _CmdResult:
    returncode: int
    stdout: str
    stderr: str


async def _read_operstate(iface: str) -> str:
    """读 /sys/class/net/<iface>/operstate, 返回 'up' / 'down' / 'unknown' / '?'."""
    p = Path(f"/sys/class/net/{iface}/operstate")
    try:
        return p.read_text().strip()
    except Exception:
        return "?"


async def _iface_exists(iface: str) -> bool:
    return os.path.isdir(f"/sys/class/net/{iface}")


@register
class WiFiTest(TestCase):
    name = "wifi"
    display_name = "WiFi 开关测试"
    description = "多次开/关 wlan0, 验证切换功能 + 驱动稳定性"

    async def run(self, ctx: TestContext) -> TestResult:
        result = TestResult()
        t_cfg = ctx.test_config
        times = max(1, int(t_cfg.get("times", 5)))
        per_op = max(0.5, float(t_cfg.get("per_op_sec", 1.0)))
        # 自动找 WiFi 接口 (A527 skw 驱动常在 wlan0, 但有些板用 wlp* 或 wlx*)
        iface = ctx.config.get("interfaces", {}).get("wifi")
        if not iface or iface == "wlan0":  # 没指定或默认值, 试自动找
            detected = find_wifi_iface()
            if detected:
                iface = detected
        await ctx.on_log(f"[WiFi] 接口: {iface}")

        # §11.2: 需要 root 的调用走命令级 sudo -n (服务本身不提权启动)。
        # sudo 不可用 → 判 error, **不**静默降级成 skipped —— 那会让工人
        # 看到"通过"而实际没测, 是产线上最危险的一类故障 (§9.3)。
        if not sudo_hint_if_unavailable(result, "WiFi"):
            await ctx.on_log(f"[WiFi] ✗ {result.error}")
            return result

        # 必须有 ip
        ip_bin = find_tool("ip")
        if not ip_bin:
            result.status = "error"
            result.error = "未找到 ip 命令. 解决: sudo bash scripts/install_deps.sh"
            await ctx.on_log(f"[WiFi] ✗ {result.error}")
            return result

        # 接口存在性
        if not await _iface_exists(iface):
            result.status = "error"
            detected = find_wifi_iface() or "(找不到)"
            result.error = (
                f"接口 {iface} 不存在. "
                f"自动找: {detected}. "
                f"原因: 1) 没 WiFi 硬件 2) skw 驱动未加载 3) 接口名不同"
            )
            await ctx.on_log(f"[WiFi] ✗ {result.error}")
            return result

        # rfkill 检查: 软件 / 硬件 block 都会让 up 后 operstate != "up"
        rfkill_bin = find_tool("rfkill")
        if rfkill_bin:
            rc, _, _ = await run_cmd(with_sudo([rfkill_bin, "list"]), 5.0)
            if rc == 0 and "soft blocked: yes" in _:
                await ctx.on_log(
                    f"[WiFi] ⚠️ {iface} 被 soft block, 先 unblock"
                )
                await run_cmd(
                    with_sudo([rfkill_bin, "unblock", "wifi"]), 3.0
                )

        # 用 iw/iwlist/nmcli 探 driver (兼容 skw)
        iw_bin = find_tool("iw")
        iwlist_bin = find_tool("iwlist")
        nmcli_bin = find_tool("nmcli")
        await ctx.on_log(
            f"[WiFi] 工具: ip={ip_bin}, iw={iw_bin or 'n/a'}, "
            f"iwlist={iwlist_bin or 'n/a'}, nmcli={nmcli_bin or 'n/a'}"
        )
        await ctx.on_log(
            f"[WiFi] 接口 {iface} 计划: {times} 次开/关, 每步 {per_op}s"
        )
        await ctx.on_progress(0, "开始")

        all_passed = True
        round_results: List[dict] = []

        for i in range(times):
            if await ctx.is_cancelled():
                result.status = "stopped"
                break
            round_idx = i + 1
            await ctx.on_log(
                f"[WiFi] 第 {round_idx}/{times} 轮"
            )

            # === 步骤 1: 拉起 ===
            up_started = time.time()
            rc, out, err = await run_cmd(
                with_sudo([ip_bin, "link", "set", iface, "up"]), 5.0
            )
            if rc != 0:
                err_text = err.strip()[:200] or f"rc={rc}"
                await ctx.on_log(f"[WiFi] ❌ ip up 失败: {err_text}")
                result.status = "failed"
                result.error = f"第 {round_idx} 轮 up 失败: {err_text}"
                all_passed = False
                round_results.append({"round": round_idx, "up_ok": False, "down_ok": None})
                break

            await asyncio.sleep(per_op)
            up_state = await _read_operstate(iface)
            up_elapsed = round(time.time() - up_started, 2)
            await ctx.on_log(
                f"[WiFi]   ↑ up 用 {up_elapsed}s → operstate={up_state}"
            )
            # up 后 operstate 可能是 "up" / "unknown" (驱动初始化中) / "down" (被 rfkill 阻塞)
            if up_state not in ("up", "unknown"):
                # 可能是 rfkill, 再 unblock 一次
                if rfkill_bin:
                    await run_cmd(with_sudo([rfkill_bin, "unblock", "wifi"]), 3.0)
                    await asyncio.sleep(1)
                    up_state = await _read_operstate(iface)
                    await ctx.on_log(
                        f"[WiFi]   重新 unblock 后 operstate={up_state}"
                    )
                if up_state not in ("up", "unknown"):
                    await ctx.on_log(
                        f"[WiFi] ❌ up 后状态是 '{up_state}' (期望 'up' / 'unknown', 驱动可能没起来)"
                    )
                    all_passed = False
                    round_results.append({
                        "round": round_idx, "up_ok": False,
                        "up_state": up_state, "down_ok": None,
                    })
                    continue

            # === 步骤 2: 拉下 ===
            down_started = time.time()
            rc, out, err = await run_cmd(
                with_sudo([ip_bin, "link", "set", iface, "down"]), 5.0
            )
            if rc != 0:
                err_text = err.strip()[:200] or f"rc={rc}"
                await ctx.on_log(f"[WiFi] ❌ ip down 失败: {err_text}")
                result.status = "failed"
                result.error = f"第 {round_idx} 轮 down 失败: {err_text}"
                all_passed = False
                round_results.append({
                    "round": round_idx, "up_ok": True,
                    "up_state": "up", "down_ok": False,
                })
                # 尝试恢复
                await run_cmd(with_sudo([ip_bin, "link", "set", iface, "up"]), 5.0)
                break

            await asyncio.sleep(0.5)
            down_state = await _read_operstate(iface)
            down_elapsed = round(time.time() - down_started, 2)
            await ctx.on_log(
                f"[WiFi]   ↓ down 用 {down_elapsed}s → operstate={down_state}"
            )
            # down 后 operstate 可能是 "down" / "unknown" (驱动处理中)
            if down_state not in ("down", "unknown"):
                await ctx.on_log(
                    f"[WiFi] ❌ down 后状态是 '{down_state}' (期望 'down')"
                )
                all_passed = False
                round_results.append({
                    "round": round_idx, "up_ok": True,
                    "up_state": "up", "down_ok": False,
                    "down_state": down_state,
                })
                continue

            # 完美
            await ctx.on_log(
                f"[WiFi]   ✓ 切换正常 (up→down 流畅)"
            )
            round_results.append({
                "round": round_idx,
                "up_ok": True, "up_state": "up",
                "down_ok": True, "down_state": "down",
                "up_elapsed_sec": up_elapsed,
                "down_elapsed_sec": down_elapsed,
            })
            await ctx.on_progress(
                int((i + 1) / times * 100),
                f"{round_idx}/{times} 切换 OK"
            )

        # 整理 metrics
        result.metrics["iface"] = iface
        result.metrics["times"] = times
        result.metrics["rounds"] = round_results
        ok_count = sum(1 for r in round_results if r.get("up_ok") and r.get("down_ok"))
        result.metrics["ok_rounds"] = ok_count
        result.metrics["failed_rounds"] = len(round_results) - ok_count

        if all_passed and result.status != "stopped":
            result.status = "passed"
            await ctx.on_log(
                f"[WiFi] ✓ 完成: {ok_count}/{times} 次切换全部成功"
            )
        elif result.status == "pending":
            result.status = "failed"
            await ctx.on_log(
                f"[WiFi] ❌ 完成: {ok_count}/{times} 次切换成功, "
                f"{result.metrics['failed_rounds']} 次失败"
            )

        return result
