"""HDMI 测试 - EDID 连接状态 + xrandr 读当前实际输出

本项**不做模式切换**(2026-10-23 实测结论, 详见 docs §6.7.1):
  - 曾经记的「modetest 打不开 sunxi-drm 设备」是错的 —— `-M sunxi-drm` 能打开;
  - 但切模式仍做不成, 真因是**桌面会话持有 DRM master**, 产线永远走不通;
  - 且 modetest 在三种情况下都返回 rc=0, 退出码不可信;
  - 留着的话, 在无桌面会话(headless)的板子上 `-s` 会真的改掉屏幕 —— 测试项
    不该有这种副作用。

改为三层只读检查:
  1. /sys/class/drm/ 读 connector 状态 + EDID (不需 root)
  2. xrandr 读**当前**实际输出分辨率/刷新率 (需 DISPLAY, 同 §6.3 GPU)
  3. 两者矛盾才判 failed
"""
import asyncio
import os
import re
from pathlib import Path
from typing import List, Optional

from .base import TestCase, TestContext, TestResult, register
from ._tools import find_tool


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


def _parse_xrandr(text: str) -> dict:
    """解析 `xrandr` 输出, 只取两样: 屏幕当前尺寸 + 各输出口的**当前**模式。

    认得的行格式 (本机实测):
      Screen 0: minimum 320 x 200, current 1920 x 1080, maximum 8192 x 8192
      HDMI-1 connected primary 1920x1080+0+0 (normal ...) 527mm x 296mm
         1920x1080_60.00  59.96*+          <- 带 `*` 的那条是**现在生效**的
    """
    screen = ""
    outputs: List[dict] = []
    cur: Optional[dict] = None
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line:
            continue
        m = re.match(r"^Screen\s+\d+:.*\bcurrent\s+(\d+)\s*x\s*(\d+)", line)
        if m:
            screen = f"{m.group(1)}x{m.group(2)}"
            continue
        m = re.match(r"^(\S+)\s+(connected|disconnected)\b(.*)$", line)
        if m:
            name, conn, rest = m.group(1), m.group(2), m.group(3)
            g = re.search(r"(\d+)x(\d+)\+(-?\d+)\+(-?\d+)", rest)
            cur = {
                "name": name,
                "connected": conn == "connected",
                "primary": "primary" in rest.split(),
                "w": int(g.group(1)) if g else 0,
                "h": int(g.group(2)) if g else 0,
                "mode": "",
                "refresh": 0.0,
            }
            outputs.append(cur)
            continue
        # 缩进行 = 上面那个输出口的 mode 列表
        m = re.match(r"^\s+(\S+)\s+([\d.]+)(\S*)\s*$", line)
        if m and cur is not None and "*" in m.group(3):
            cur["mode"] = m.group(1)
            cur["refresh"] = float(m.group(2))
    return {"screen": screen, "outputs": outputs}


async def _read_xrandr(xrandr_bin: str, timeout: int = 5) -> Optional[dict]:
    """跑 xrandr 读**当前**实际输出。跑不了 / 没 DISPLAY 一律返回 None。

    为什么非它不可: connector 的 `modes` 是 EDID 报的**支持**列表, 它的第一条
    **不是「现在是什么模式」** —— 老代码把它当 current_resolution 上报, 一直报
    的是错的(本机 EDID 第一条恰好也是 1920x1080, 所以错得看不出来; 换一台显示器
    或改一次 EDID 就露馅)。xrandr 读的是 X server 当前 CRTC 的实际输出, 是真值。

    前提与 gpu.py 相同(§6.3): 服务由桌面图标启动, 继承会话的 DISPLAY。
    **必须带 timeout**: X server 挂住时 xrandr 会跟着卡死, 不能拖住整项测试。
    """
    if not os.environ.get("DISPLAY", "").strip():
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            xrandr_bin,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
    except (FileNotFoundError, PermissionError):
        # find_tool 刚查过还在, 到 exec 时没了 (被卸载/换软链) —— 降级, 不让整项崩
        return None
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()
        return None
    if proc.returncode != 0:
        return None
    parsed = _parse_xrandr(out.decode(errors="replace"))
    return parsed if parsed["outputs"] else None


@register
class HDMITest(TestCase):
    name = "hdmi"
    display_name = "HDMI 输出"
    description = "读 EDID 连接状态 + xrandr 读当前实际输出"

    # 📌 **没有 cancel() 覆盖**(2026-10-23): 以前覆盖它是为了杀掉 modetest 那个
    # 无限循环的 root 进程; 模式切换整块移除后, 本项**再没有长驻子进程** ——
    # 唯一的外部命令 xrandr 自带 5s 超时。留着 `self.proc` / `cancel()` 就是
    # 永远为 None 的死代码。基类默认实现是 `pass`, 不覆盖即可。

    async def run(self, ctx: TestContext) -> TestResult:
        result = TestResult()
        t_cfg = ctx.test_config
        # 注: 本项**没有「次数」旋钮**。原先 config 里的 `hdmi.times` 是死键 ——
        # 代码读了不用, 而 §7.2 会把它渲染成一个能填的输入框, 工人填 5 却什么都没
        # 发生。切**同一个**模式本就是幂等的, 循环 times 遍第 2 轮起全是 no-op,
        # 多测不出任何东西。已于 2026-10-23 连配置键带输入框一并删除(见 docs §6.7.1)。
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
        await ctx.on_log(
            f"[HDMI] ✓ {active['name']} 已连接 "
            f"(sysfs status={active['status']}), EDID 报 {len(active['modes'])} 个支持模式"
        )

        # ── 当前分辨率: xrandr 查 X server 的**实际**输出 ──
        # ⚠️ 不要再用 active["modes"][0] —— 那是 EDID **支持列表的第一条**, 不是
        #    「现在是什么模式」。本机两条恰好都是 1920x1080 所以一直看不出错,
        #    换个显示器 / EDID 变一下就会报出一个根本没在显示的分辨率。
        prefix = ("HDMI" if "HDMI" in active["name"].upper()
                  else "DP" if "DP" in active["name"].upper() else "")
        xrandr_bin = find_tool("xrandr")
        xr = await _read_xrandr(xrandr_bin) if xrandr_bin else None
        cand = None
        if xr is None:
            if not os.environ.get("DISPLAY", "").strip():
                why = "服务无 DISPLAY 环境变量"
            elif not xrandr_bin:
                why = "未找到 xrandr"
            else:
                why = "xrandr 执行失败或超时"
            result.metrics["current_resolution"] = "(未验证)"
            await ctx.on_log(f"[HDMI] ⚠️ 查不到当前分辨率 ({why}) —— 本次只按 EDID 判定")
            xr_state = "unavailable"
        else:
            result.metrics["screen"] = xr["screen"]
            # sysfs 叫 `card0-HDMI-A-1`, xrandr 叫 `HDMI-1` —— 按前缀认, 别按全名
            cands = ([o for o in xr["outputs"] if o["name"].upper().startswith(prefix)]
                     if prefix else [])
            cands.sort(key=lambda o: (not o["connected"], not o["primary"]))
            cand = cands[0] if cands else None
            if cand is None:
                result.metrics["current_resolution"] = "(xrandr 无对应输出口)"
                await ctx.on_log(
                    f"[HDMI] ⚠️ xrandr 里没有与 {active['name']} 对应的输出口"
                    f"(现有: {[o['name'] for o in xr['outputs']]})"
                )
                xr_state = "unavailable"
            elif not cand["connected"] or not cand["mode"] or not (cand["w"] and cand["h"]):
                result.metrics["current_resolution"] = f"({cand['name']} 无当前模式)"
                await ctx.on_log(
                    f"[HDMI] ❌ sysfs 说 connected, 但 xrandr 报 {cand['name']} "
                    f"connected={cand['connected']} mode={cand['mode'] or '(无)'}"
                )
                xr_state = "mismatch"
            else:
                xr_state = "ok"
                result.metrics["current_resolution"] = f"{cand['w']}x{cand['h']}"
                result.metrics["current_refresh"] = cand["refresh"]
                await ctx.on_log(
                    f"[HDMI] ✓ 当前输出: {cand['name']} {cand['w']}x{cand['h']}"
                    f" @ {cand['refresh']:.2f}Hz (xrandr 真值, 屏 {xr['screen']})"
                )

        # 解析目标分辨率验证
        tw, th = 1920, 1080
        tr = 60
        m = re.match(r"(\d+)x(\d+)(?:@(\d+))?", target_res)
        if m:
            tw, th = int(m.group(1)), int(m.group(2))
            tr = int(m.group(3)) if m.group(3) else 60
        target_mode = f"{tw}x{th}@{tr}"
        # ⚠️ sysfs 的 `modes` 文件里**只有分辨率、没有刷新率**(实测 15 行全是
        # `1920x1080` / `1280x720` 这样的裸值, 同一个分辨率按刷新率重复好几行)。
        # 所以拿 `1920x1080@60` 去做 `in` **永远 False** —— 这个指标一直是死的,
        # 还每跑一次就误报一条 ⚠️「目标模式不在支持列表」。
        # 刷新率那一半交给上面的 xrandr 真值单独比(带 ±1Hz 容差)。
        target_res_only = f"{tw}x{th}"
        target_match = target_res_only in active["modes"]
        result.metrics["target_mode_in_modes"] = target_match
        if target_match:
            await ctx.on_log(f"[HDMI] 目标分辨率 {target_res_only} 在 EDID 支持列表中 ✓")
        else:
            await ctx.on_log(
                f"[HDMI] ⚠️ 目标分辨率 {target_res_only} 不在 EDID 支持列表, "
                f"当前屏报告 {len(active['modes'])} 个 mode"
            )

        # 当前实际模式 vs 目标 —— **只提示, 不参与判定**(同 §6.5.1 USB 的口径:
        # 分辨率不符不等于板子坏, 但产线要看得见)。刷新率必须留 ±1Hz 容差:
        # xrandr 把 1920x1080_60.00 报成 59.96, 严格比 "60" 会永远不符。
        if xr_state == "ok" and cand is not None:
            if (cand["w"] == tw and cand["h"] == th
                    and abs(cand["refresh"] - tr) <= 1.0):
                await ctx.on_log(f"[HDMI] 当前模式与目标 {target_mode} 一致 ✓")
            else:
                await ctx.on_log(
                    f"[HDMI] ⚠️ 当前 {cand['w']}x{cand['h']}@{cand['refresh']:.2f} "
                    f"与目标 {target_mode} 不同 —— 仅提示, 不判失败"
                )

        # ── 模式切换: **2026-10-23 已整块移除, 不再调用 modetest** ──
        # 移除前实测过的四件事, 结论留给下一个要做这件事的人(详见 docs §6.7.1):
        #   ① 曾经记的「modetest 打不开 sunxi-drm 设备」**是错的** —— 那份写死的
        #      驱动表只是**默认集合**, `-M sunxi-drm` 可以覆盖。实测带上它**能正常
        #      打开设备**并列出 connector(id 133 / **CRTC 94, 不是 0**)。
        #   ② 但切模式在本板**依然做不成**, 真因是**桌面会话持有 DRM master**:
        #      实测 xfwm4 / xfce4-session 开着 /dev/dri/card0, 外部进程哪怕是 root
        #      也只拿到 `failed to set mode: Permission denied`。产线工位上桌面是在
        #      跑的 → 这条路在产线**永远**走不通。
        #   ③ `-s` 的语法有两处易错: 垂直刷新率前面是**减号**不是 @
        #      (`<mode>[-<vrefresh>]`), 且 connector 名要去掉 `card<N>-` 前缀。
        #   ④ ⚠️ modetest 在**参数错 / modeset 被拒 / 真成功** 三种情况下
        #      **全都返回 rc=0** —— 它的退出码**完全不可信**。
        # 移除的理由不只是「反正也会失败」: 留着的话, 在一台**没有桌面会话**的板子
        # 上(SSH headless)`-s` 会**真的改掉屏幕**, 而工人对此毫无准备 ——
        # 测试项不该有这种副作用。**验证当前输出由上面的 xrandr 负责。**

        if not active["modes"]:
            result.status = "failed"
            result.error = "HDMI connected 但 EDID 没报出任何 mode"
            return result
        # 判定三态 (2026-10-23):
        #   ok           sysfs connected + xrandr 有当前模式  → pass
        #   unavailable   查不到 xrandr (无 DISPLAY / 没装 / 超时) → 按 EDID 判 pass,
        #                 日志已注明「当前分辨率未验证」。**不因此误杀好板子** ——
        #                 拿一个判不准的读数当失败依据, 比不判更糟(§6.7.1 同样的教训)。
        #   mismatch      xrandr 跑通了, 却说这个输出口没连 / 没当前模式 → fail。
        #                 这是**真矛盾**: sysfs 说连着, X 说没输出。
        if xr_state == "mismatch":
            result.status = "failed"
            result.error = (
                f"sysfs 报 {active['name']} connected, 但 xrandr 查不到它的当前模式 —— "
                "X 会话与 HDMI 实际输出不一致, 屏幕可能没真正点亮"
            )
            return result
        result.status = "passed"
        return result
