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

§6.7.3 新增 **断开→恢复一轮**(**指令式, 工人全程不用动手**): 接上显示器先按
上面这套判一遍(基线), 再用 `xrandr --output HDMI-1 --off` **自动关掉**这块
屏, 等几秒, 再用 `--auto` **恢复**, 按**同一套判定逻辑**再判一遍 —— 这一轮
判的是「断一下之后还能不能点亮」。

⚠️ 执行前会先**提示几秒**: 关屏期间这块屏是黑的(约 3 秒), **属正常现象**,
工人**不用拔线、不用操作**, 等它自己恢复就行 —— 所以这一轮可以在产线上
「一个人一只手」地跑, 不用另外找人配合。
"""
import asyncio
import os
import re
import time
from pathlib import Path
from typing import List, Optional

from .base import TestCase, TestContext, TestResult, register
from ._tools import find_tool

# §6.7.3 断开→恢复一轮的两个节奏
_POLL_SEC = 0.5        # 恢复后轮询 xrandr 的间隔(等桌面重新认屏)
_HEARTBEAT_SEC = 1.5   # §10.1: 等待期间每 ≤2 秒至少推 1 帧 progress 或 log
_RESTORE_RETRY_WAIT = 0.8   # 恢复命令失败后隔多久再试一次


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


# ── §6.7.3 断开→恢复一轮用到的几个小工具 ──────────────────────────────

def _as_bool(v, default: bool = False) -> bool:
    """配置值转 bool —— 顺手挡掉 YAML 里写成字符串的 "false"(那是 True)。"""
    if isinstance(v, bool):
        return v
    if v is None:
        return default
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on", "是")
    return bool(v)


def _num(v, default: float) -> float:
    """配置值转 float —— **0 是合法值**, 不能用 `v or default` 那种写法
    (那会把「等 0 秒」变成「等 3 秒」, 配置改了不生效还没人发现)。"""
    if v is None:
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _cfg(ctx: TestContext, key: str, default=None):
    """读 HDMI 项的配置键 —— 前端那份优先, 再回落到 config.yaml 的 defaults.hdmi。

    为什么要有回落: 前端是把 `defaults.hdmi` **整个抄一份**发给后端的
    (§7.2), 所以新加的键要等**页面刷新**之后才会带上。工人忘了刷新 →
    新功能静默不跑, 而界面上看不出任何区别 —— 那一轮就少测了一截, 却
    没人知道。回落一层 yaml 默认值, 免得「没刷新页面」= 「少测一截」。
    """
    tc = getattr(ctx, "test_config", None) or {}
    if key in tc:
        return tc.get(key)
    defaults = ((getattr(ctx, "config", None) or {}).get("defaults") or {})
    hdmi = defaults.get("hdmi") or {}
    if key in hdmi:
        return hdmi.get(key)
    return default


async def _sleep(ctx: TestContext, seconds: float, what: str, pct: int) -> bool:
    """可被取消打断的等待, 期间每 ≤2 秒推一帧 progress(§10.1)。

    返回 True = 期间被取消。
    分片睡而不是一觉睡到底 —— 否则用户点「停止」还得等满整个间隔才生效。
    """
    deadline = time.monotonic() + seconds
    t0 = time.monotonic()
    last_beat = t0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        if await ctx.is_cancelled():
            return True
        now = time.monotonic()
        if (now - last_beat) >= _HEARTBEAT_SEC:
            last_beat = now
            await ctx.on_progress(
                pct, f"{what} (已等 {now - t0:.1f}/{seconds:.1f}s)"
            )
        await asyncio.sleep(min(0.2, remaining))


async def _run_xrandr_cmd(xrandr_bin: str, args: list,
                          timeout: int = 10) -> tuple:
    """执行一条 xrandr 命令, 返回 (rc, 输出文本)。

    ⚠️ **rc=0 不等于成功**: xrandr 在 RANDR 被禁用 / 没有权限时会**打印一行
    错误**, 有的情况下还返回 0。所以返回文本也一并带出来, 由调用方判断
    「命令到底成没成」—— 判定失败时得让人看得见到底报了什么。
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            xrandr_bin, *args,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
    except (FileNotFoundError, PermissionError) as e:
        return 127, str(e)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()
        return 124, "命令超时"
    return proc.returncode, (out or b"").decode(errors="replace").strip()


async def _probe_output(ctx: TestContext, active: dict, prefix: str,
                        xrandr_bin: Optional[str], tag: str = "",
                        sysfs_status: str = "connected",
                        log: bool = True) -> dict:
    """读一次「当前实际输出」, 并按 **xrandr 这一侧** 分三态(§6.7.2 同口径)。

    返回 {"state": "ok" | "unavailable" | "mismatch",
          "case":  "ok" | "no_xrandr" | "no_output" | "no_mode",
          "xr": xrandr 解析结果或 None,
          "cand": 对应的输出口或 None,
          "why": 不是 ok 时的原因}

    分态只看 xrandr 这一侧 —— sysfs 那侧由调用方自己看:
      ok          —— 找到对应输出口, 且它 connected 且有当前模式
      unavailable —— 查不到 xrandr(没装/无 DISPLAY/超时) 或 xrandr 里没有对应口
      mismatch    —— xrandr 跑通了, 却说这个口没连 / 没当前模式

    `case` 是给「指标写什么」用的: 「没装 xrandr」(no_xrandr)与「xrandr 里没有
    对应的口」(no_output)在三态里**都是 unavailable**, 但指标上要分得开 ——
    前者是「读不到当前分辨率(未验证)」, 后者是「xrandr 无对应输出口」。

    基线与断开→恢复轮**共用这一个函数** —— 「加上现有的逻辑来判定」不是嘴上说说:
    判断开恢复之后能不能点亮, 用的是判基线那套一模一样的三态, 不是另写一套。
    """
    xr = None
    cand = None
    state = "unavailable"
    case = "no_xrandr"
    why = ""

    if xrandr_bin:
        xr = await _read_xrandr(xrandr_bin)
    if xr is None:
        if not os.environ.get("DISPLAY", "").strip():
            why = "服务无 DISPLAY 环境变量"
        elif not xrandr_bin:
            why = "未找到 xrandr"
        else:
            why = "xrandr 执行失败或超时"
    else:
        # sysfs 叫 `card0-HDMI-A-1`, xrandr 叫 `HDMI-1` —— 按前缀认, 别按全名
        cands = ([o for o in xr["outputs"] if o["name"].upper().startswith(prefix)]
                 if prefix else [])
        cands.sort(key=lambda o: (not o["connected"], not o["primary"]))
        cand = cands[0] if cands else None
        if cand is None:
            case = "no_output"
            why = "xrandr 无对应输出口"
        elif (not cand["connected"] or not cand["mode"]
                or not (cand["w"] and cand["h"])):
            state = "mismatch"
            case = "no_mode"
            why = f"{cand['name']} 无当前模式"
        else:
            state = "ok"
            case = "ok"

    if log:
        if case == "no_output":
            await ctx.on_log(
                f"[HDMI] {tag}⚠️ xrandr 里没有与 {active['name']} 对应的输出口"
                f"(现有: {[o['name'] for o in xr['outputs']]})"
            )
        elif state == "unavailable":
            await ctx.on_log(
                f"[HDMI] {tag}⚠️ 查不到当前分辨率 ({why}) —— 本次只按 EDID 判定"
            )
        elif state == "mismatch":
            await ctx.on_log(
                f"[HDMI] {tag}❌ sysfs 说 {sysfs_status}, 但 xrandr 报 "
                f"{cand['name']} connected={cand['connected']} "
                f"mode={cand['mode'] or '(无)'}"
            )
        else:
            await ctx.on_log(
                f"[HDMI] {tag}✓ 当前输出: {cand['name']} {cand['w']}x{cand['h']}"
                f" @ {cand['refresh']:.2f}Hz (xrandr 真值, 屏 {xr['screen']})"
            )
    return {"state": state, "case": case, "xr": xr, "cand": cand, "why": why}


async def _restore_output(xrandr_bin: str, out_name: str,
                          saved_mode: str = "") -> tuple:
    """尽力把输出恢复回来 —— 返回 (成功?, 用的哪条命令, 输出文本)。

    为什么要「尽力」: **关屏成功而恢复失败 = 工人面前是一块黑屏**, 后面
    什么测试都做不了, 产线只能找人去修显示器。所以这一段要多试几种:
      ① `--auto`   —— 按 EDID 里的首选模式恢复(首选)
      ② `--mode <断屏前的模式>` —— 按原样恢复(上面不行时的兜底)
    每种再重试一次 —— 第一次可能是桌面还在处理关屏, 稍等一下就好。

    ⚠️ `--auto` 恢复的是 EDID 的**首选**模式, 未必跟断屏前那个一模一样
    (EDID 里首选模式不等于当前用的模式)。所以恢复后的实际模式要**重新读
    一遍** xrandr, 不能拿断屏前的值充数。
    """
    attempts = [["--output", out_name, "--auto"]]
    if saved_mode:
        attempts.append(["--output", out_name, "--mode", saved_mode])
    cmd = ""
    last_out = ""
    for args in attempts:
        cmd = "xrandr " + " ".join(args)
        for _ in range(2):
            rc, out = await _run_xrandr_cmd(xrandr_bin, args)
            last_out = out
            if rc == 0:
                return True, cmd, out
            await asyncio.sleep(_RESTORE_RETRY_WAIT)
    return False, cmd, last_out


async def _replug_round(ctx: TestContext, result: TestResult, active: dict,
                        prefix: str, xrandr_bin: Optional[str],
                        base_cand: Optional[dict]) -> str:
    """§6.7.3 断开→恢复一轮(**指令式**): `--off` → 等几秒 → `--auto`。

    工人**全程不用动手** —— 不拔线、不用另开一台设备、不用配合。

    返回: "restored" / "restored_degraded" / "off_failed" / "restore_failed" /
          "mismatch" / "skipped" / "cancelled"

    **判定完全沿用基线那套逻辑**(_probe_output + §6.7.2 三态表):
      · 恢复之后 xrandr 必须重新报出当前模式 → 否则是**真故障**(failed);
      · xrandr 读不到(unavailable) → 与基线同口径: 降级, 按 sysfs+EDID 判,
        **不判失败**(拿判不准的读数杀好板子, 比不判更糟)。

    ⚠️ **关屏命令失败不判失败**: `xrandr --off` 关不掉, 是**桌面/X 会话**
    那边不支持(RANDR 没开、没有权限、只有一个屏不让关), 跟板子好坏无关 ——
    判失败 = 把一块好板子送去返修。所以降级放行, 只记指标。
    """
    warn_sec = _num(_cfg(ctx, "replug_warn_sec", 5), 5)
    dwell = _num(_cfg(ctx, "replug_off_dwell_sec", 3), 3)
    restore_timeout = _num(_cfg(ctx, "replug_restore_timeout_sec", 10), 10)

    # 连「关哪个口」都不知道 → 这一轮没法做(基线没读出当前输出)
    if not base_cand or not base_cand.get("name"):
        await ctx.on_log(
            "[HDMI] 断开→恢复轮: 基线没读出当前输出 —— 跳过这一轮(不判失败)"
        )
        return "skipped"
    out_name = base_cand["name"]
    saved_mode = (f"{base_cand['w']}x{base_cand['h']}"
                  if base_cand.get("w") and base_cand.get("h") else "")

    # ── ① 提示: 让人知道马上要黑屏了(但**不用动手**) ──
    #
    # ⚠️ 提示**不能只写进日志**: 马上要黑屏的那一块, 就是工人正盯着的那
    # 一块 —— 日志在屏幕最下面, 工人此刻的注意力在**进度条**上。所以同一句
    # 话要**两个地方**都说: 界面上一条**显眼的大横幅**(on_alert, 大字+重色+
    # 倒计时), 日志里再留一份(留档、复盘看)。
    alert_text = (f"屏幕即将黑屏 —— 这块屏({out_name})会被自动关掉 {dwell:.0f} 秒, "
                  f"属正常现象; 不用拔线、不用操作")
    if getattr(ctx, "on_alert", None):
        await ctx.on_alert(alert_text, "danger", warn_sec)
    await ctx.on_log(
        f"[HDMI] ⚠️ 接下来会**自动关掉这块屏**({out_name}) —— 黑屏约 {dwell:.0f} 秒, "
        f"**属正常现象**; **不用拔线、不用操作**, 等它自己恢复就行。"
    )
    if await _sleep(ctx, warn_sec, "⚠️ 即将黑屏", 50):
        # 被打断 → 横幅也得收起来(否则会一直挂在界面上)
        if getattr(ctx, "on_alert", None):
            await ctx.on_alert("", "warn", 0)
        return "cancelled"

    # 倒计时走完 → **收起横幅**, 紧接着就是真的关屏。
    # (横幅留到黑屏之后没意义: 那会儿屏已经是黑的, 谁也看不见。)
    if getattr(ctx, "on_alert", None):
        await ctx.on_alert("", "warn", 0)

    # ── ② 关屏 ──
    await ctx.on_progress(60, "🔌 正在关闭输出")
    rc, out = await _run_xrandr_cmd(xrandr_bin, ["--output", out_name, "--off"])
    result.metrics["replug_off_cmd"] = f"xrandr --output {out_name} --off"
    if rc != 0:
        await ctx.on_log(
            f"[HDMI] ⚠️ 关不掉输出(xrandr 返回 {rc}: {(out or '无输出')[:120]}) —— "
            f"跳过这一轮(**不判失败**: 桌面/X 会话不支持这一操作, 与板子好坏无关)"
        )
        return "off_failed"
    await ctx.on_log(f"[HDMI] ✓ 已关闭输出 {out_name} —— 这块屏现在是黑的")

    turned_off = True
    restored = False
    t_black = time.monotonic()      # 黑屏窗口的起点
    try:
        # 确认一下是不是真的断了。⚠️ 注意: 这里 xrandr 说「这个口没输出」
        # 是**预期之中**的 —— 线还插着, 只是我们让 X 别再驱动它了
        # (sysfs 的 status 仍然是 connected)。所以这一段**只作记录**,
        # 绝不能当成「矛盾」去判失败。
        probe_d = await _probe_output(
            ctx, active, prefix, xrandr_bin, tag="🔌 关屏后 · ", log=False,
        )
        if probe_d["state"] == "mismatch":
            await ctx.on_log("[HDMI] ✓ xrandr 已确认这个口没有输出了(关屏生效)")
        elif probe_d["state"] == "ok":
            await ctx.on_log(
                "[HDMI] · 关屏命令返回成功, 但 xrandr 仍报着旧模式 —— 仅记录"
            )
        else:
            await ctx.on_log(
                f"[HDMI] · 关屏后读不到当前输出({probe_d['why']}) —— 仅记录"
            )

        # 黑屏保持几秒
        if await _sleep(ctx, dwell, "🔌 黑屏中", 70):
            return "cancelled"

        # ── ③ 恢复 ──
        await ctx.on_progress(85, "🔌 正在恢复输出")
        # 黑屏窗口有多长(从 --off 到 --auto 之间)—— 这是「断了多久」的事实
        result.metrics["replug_off_sec"] = round(time.monotonic() - t_black, 2)
        ok, cmd, out = await _restore_output(xrandr_bin, out_name, saved_mode)
        result.metrics["replug_restore_cmd"] = cmd
        if not ok:
            await ctx.on_log(
                f"[HDMI] ❌ 断开→恢复轮: 关屏之后**没能恢复**({cmd}: "
                f"{(out or '无输出')[:160]}) —— 这块屏可能还是黑的, "
                f"请**手动**接好显示器 / 重启桌面会话"
            )
            return "restore_failed"
        restored = True
        await ctx.on_log(f"[HDMI] ✓ 已恢复输出({cmd})")

        # ── ④ 恢复之后, 还能不能点亮 ──
        # 桌面要重新认屏(读 EDID、给 CRTC 配模式), 这要一会儿 → **轮询**
        # xrandr, 直到它报出当前模式, 而不是读一次就下结论(读一次必然读不到)。
        deadline = time.monotonic() + restore_timeout
        probe_r = None
        while True:
            probe_r = await _probe_output(
                ctx, active, prefix, xrandr_bin, tag="🔌 恢复后 · ", log=False,
            )
            if probe_r["state"] == "ok" or time.monotonic() >= deadline:
                break
            if await ctx.is_cancelled():
                return "cancelled"
            await ctx.on_progress(
                90, f"🔌 等桌面重新认屏 (最多 {restore_timeout:.0f}s)"
            )
            await asyncio.sleep(_POLL_SEC)
    finally:
        # ⚠️ **兜底: 屏一定要恢复回来**。哪怕这一轮判失败、哪怕中途被
        # 「停止」打断 —— 也不能把工人留在一块**黑屏**前面(那后面什么都
        # 做不了, 只能找人去修)。所以只要关过, 就再补一次 `--auto`。
        if turned_off and not restored:
            await _run_xrandr_cmd(xrandr_bin, ["--output", out_name, "--auto"])

    cand = probe_r["cand"]
    if probe_r["state"] == "ok":
        result.metrics["replug_resolution"] = f"{cand['w']}x{cand['h']}"
        result.metrics["replug_refresh"] = cand["refresh"]
        # 恢复之后的模式才是**最后**这块屏在显示的东西 —— 报告上的
        # 「当前分辨率」得跟着改, 不然报告写的跟屏上实际显示的对不上。
        result.metrics["current_resolution"] = f"{cand['w']}x{cand['h']}"
        result.metrics["current_refresh"] = cand["refresh"]
        await ctx.on_log(
            f"[HDMI] ✓ 断开→恢复轮: 恢复后输出正常 —— {cand['name']} "
            f"{cand['w']}x{cand['h']} @ {cand['refresh']:.2f}Hz"
        )
        if (base_cand.get("w"), base_cand.get("h")) != (cand["w"], cand["h"]):
            await ctx.on_log(
                f"[HDMI] · 恢复后的模式与断屏前不同"
                f"({base_cand['w']}x{base_cand['h']} → {cand['w']}x{cand['h']}) "
                f"—— 以恢复后的为准"
            )
        return "restored"
    if probe_r["state"] == "mismatch":
        # 恢复命令执行成功了, 桌面却说这个口没有输出 —— 与基线同口径的**真矛盾**
        await ctx.on_log(
            f"[HDMI] ❌ 断开→恢复轮: 恢复命令执行成功, 但 xrandr 报 {cand['name']} "
            f"connected={cand['connected']} mode={cand['mode'] or '(无)'} —— "
            f"断一下就点不亮了"
        )
        return "mismatch"
    # unavailable: 与基线同口径 —— 读不准就不据此判失败
    await ctx.on_log(
        f"[HDMI] ⚠️ 断开→恢复轮: 恢复了, 但读不到当前输出({probe_r['why']}) —— "
        f"按 sysfs+EDID 判定(不判失败)"
    )
    return "restored_degraded"


@register
class HDMITest(TestCase):
    name = "hdmi"
    display_name = "HDMI 输出"
    description = "读 EDID 连接状态 + xrandr 读当前实际输出 (+ 自动断开/恢复一轮)"

    # 📌 **没有 cancel() 覆盖**(2026-10-23): 以前覆盖它是为了杀掉 modetest 那个
    # 无限循环的 root 进程; 模式切换整块移除后, 本项**再没有长驻子进程** ——
    # 唯一的外部命令 xrandr 自带 5s 超时。留着 `self.proc` / `cancel()` 就是
    # 永远为 None 的死代码。基类默认实现是 `pass`, 不覆盖即可。

    def expected_seconds(self, test_config: dict) -> Optional[float]:
        """本项预计多少秒 —— 只有**开了断开→恢复那一轮**才算得出来。

        没开那一轮时本项是纯读(毫秒级), 返回 None(界面不显示预计时间:
        显示一个编出来的秒数比不显示更糟)。开了以后才有一个说得准的
        上限: 提示几秒 + 黑屏几秒 + 等桌面重新认屏 + 一点余量。
        """
        if not _as_bool((test_config or {}).get("replug_check"), False):
            return None
        warn = _num((test_config or {}).get("replug_warn_sec"), 5)
        dwell = _num((test_config or {}).get("replug_off_dwell_sec"), 3)
        restore = _num((test_config or {}).get("replug_restore_timeout_sec"), 10)
        return warn + dwell + restore + 10

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
        await ctx.on_progress(10, "读 EDID + 当前实际输出")
        probe = await _probe_output(
            ctx, active, prefix, xrandr_bin,
            sysfs_status=active["status"],
        )
        xr_state = probe["state"]
        xr = probe["xr"]
        cand = probe["cand"]
        if xr is not None:
            result.metrics["screen"] = xr["screen"]
        if probe["case"] == "no_xrandr":
            result.metrics["current_resolution"] = "(未验证)"
        elif probe["case"] == "no_output":
            result.metrics["current_resolution"] = "(xrandr 无对应输出口)"
        elif probe["case"] == "no_mode":
            result.metrics["current_resolution"] = f"({cand['name']} 无当前模式)"
        else:
            result.metrics["current_resolution"] = f"{cand['w']}x{cand['h']}"
            result.metrics["current_refresh"] = cand["refresh"]

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

        # ── §6.7.3 断开→恢复一轮 (指令式: --off → 等几秒 → --auto) ──
        # 放在**基线判完之后**: 基线已经 failed(真矛盾)就没必要再折腾一轮 ——
        # 那一轮的结论不会把 failed 翻成 passed, 只是白折腾一块屏。
        # 判定用**同一套逻辑**: _probe_output + §6.7.2 三态表, 一个字没改。
        if not _as_bool(_cfg(ctx, "replug_check", False), False):
            result.metrics["replug_check"] = "disabled"
            await ctx.on_log(
                "[HDMI] 断开→恢复轮: 未启用 (配置 defaults.hdmi.replug_check)"
            )
            await ctx.on_progress(100, "完成")
            result.status = "passed"
            return result
        result.metrics["replug_check"] = "enabled"

        # ⚠️ 只关**基线判定用的那个口**(授权范围): 那一轮判的就是这个口,
        # 关别的口没有意义, 还会把没在用的输出口折腾坏。
        round_state = await _replug_round(
            ctx, result, active, prefix, xrandr_bin, cand,
        )
        result.metrics["replug_state"] = round_state
        if round_state == "cancelled":
            result.status = "stopped"
            result.error = "断开→恢复那一轮等待期间被停止"
            return result
        if round_state == "restore_failed":
            result.status = "failed"
            result.error = (
                f"{active['name']} 的输出关掉之后**没能恢复** —— "
                "xrandr --auto / --mode 都失败, 这块屏可能还黑着, "
                "请手动接好显示器或重启桌面会话"
            )
            return result
        if round_state == "mismatch":
            result.status = "failed"
            result.error = (
                f"{active['name']} 断开再恢复之后点不亮 —— "
                "xrandr 说这个输出口没有当前模式, 与 sysfs 的 connected 矛盾"
            )
            return result
        await ctx.on_progress(100, "完成")
        result.status = "passed"
        return result
