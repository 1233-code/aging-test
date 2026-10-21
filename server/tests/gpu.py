"""GPU 渲染压测 - OpenGL ES 2 全屏渲染 (§6.3)

工具: glmark2-es2 --fullscreen (走 Wayland 全屏输出, 不需 X11)

设计要点 (严格按 §6.3):
  - **前置条件缺任一 → 本项 error 并明确报错, 不做静默降级**
    ① HDMI 显示器已连接  /sys/class/drm/*HDMI*/status == connected
    ② Wayland 会话已启动  WAYLAND_DISPLAY 非空 + compositor 进程在
    ③ DRM 节点存在        /dev/dri/card* + /dev/dri/renderD*
    产线常态接 HDMI, 因此不进入"未接显示器 → skipped"分支; 第 ① 条不满足
    属工位配置问题, 报 error 而非 skipped, 否则等于 GPU 静默漏测。
  - **通过判定** —— 跑完一轮: exit code == 0 且输出以 `glmark2 Score: <数字>` 收尾
  - **超时兜底** —— gpu.timeout_sec (默认 600s), 超时即 error + 记 stderr 末 N 行,
    防止个别机器跑飞挂死整个 run
  - **UI 无参数** —— glmark2 以"一轮完整跑完"为最小粒度, 本项不暴露 [次数]/[时长]
"""
import asyncio
import glob
import os
import re
import time
from typing import List, Optional, Tuple

from .base import TestCase, TestContext, TestResult, register
from ._tools import find_tool, with_sudo, sudo_hint_if_unavailable

# glmark2 一轮理论 2-5 分钟 (§6.3), 驱动差异导致波动大, 故超时给到 600s
_DEFAULT_TIMEOUT_SEC = 600

# 错误上下文保留的末尾行数
_TAIL_LINES = 20

# §10.1 推送频率硬约束: 静默阶段也要推心跳, ≤ 2s 一帧
_HEARTBEAT_SEC = 1.5

# 前置检查通过率 (进度条起点)
_PRECHECK_DONE_PCT = 5


def _precheck_display() -> Tuple[bool, str]:
    """前置 ①: HDMI 显示器已连接.

    读 /sys/class/drm/*HDMI*/status, 只要有一个 connector == connected 即通过。
    """
    connectors: List[str] = []
    connected = ""
    for status_path in sorted(glob.glob("/sys/class/drm/*HDMI*/status")):
        name = os.path.basename(os.path.dirname(status_path))
        try:
            with open(status_path, "r") as f:
                connectors.append(f"{name}={f.read().strip()}")
        except OSError as e:
            connectors.append(f"{name}=<读取失败 {e}>")
            continue
        with open(status_path, "r") as f:
            if f.read().strip() == "connected":
                connected = name
                break
    if connected:
        return True, f"HDMI 已连接: {connected}"
    if not connectors:
        return False, "未找到 HDMI connector (/sys/class/drm/*HDMI*/status 不存在)"
    return False, f"HDMI 未连接: {', '.join(connectors)}"


def _precheck_wayland() -> Tuple[bool, str]:
    """前置 ②: Wayland 会话已启动 (WAYLAND_DISPLAY 非空 + compositor 进程在).

    compositor 进程检测: 扫 /proc/*/environ 找带同名 WAYLAND_DISPLAY 的进程;
    兜底看 $XDG_RUNTIME_DIR/$WAYLAND_DISPLAY 这个 socket 是否存在。
    """
    display = os.environ.get("WAYLAND_DISPLAY", "").strip()
    if not display:
        return False, "WAYLAND_DISPLAY 为空 —— 无 GLES 上下文, glmark2 会直接退出"

    runtime_dir = os.environ.get("XDG_RUNTIME_DIR", "").strip()
    if runtime_dir:
        sock = os.path.join(runtime_dir, display)
        if os.path.exists(sock):
            return True, f"Wayland 会话在: WAYLAND_DISPLAY={display} (socket {sock})"

    # socket 没找到 (或没设 XDG_RUNTIME_DIR) —— 退到扫 compositor 进程
    target = f"WAYLAND_DISPLAY={display}".encode()
    for proc_dir in glob.glob("/proc/[0-9]*"):
        try:
            with open(os.path.join(proc_dir, "environ"), "rb") as f:
                if target in f.read().split(b"\0"):
                    pid = os.path.basename(proc_dir)
                    with open(os.path.join(proc_dir, "comm")) as cf:
                        return True, f"Wayland 会话在: WAYLAND_DISPLAY={display} (compositor pid={pid} {cf.read().strip()})"
        except (OSError, PermissionError):
            # 别的用户的进程读不到 environ 是正常的, 跳过
            continue
    return False, (
        f"WAYLAND_DISPLAY={display} 已设置, 但找不到 compositor 进程 / socket "
        f"(XDG_RUNTIME_DIR={runtime_dir or '未设置'})"
    )


def _precheck_dri() -> Tuple[bool, str]:
    """前置 ③: DRM 节点存在 (/dev/dri/card* + /dev/dri/renderD*)."""
    cards = sorted(glob.glob("/dev/dri/card*"))
    renders = sorted(glob.glob("/dev/dri/renderD*"))
    if cards and renders:
        return True, f"DRM 节点就位: {', '.join(cards + renders)}"
    missing = []
    if not cards:
        missing.append("/dev/dri/card*")
    if not renders:
        missing.append("/dev/dri/renderD*")
    return False, f"DRM 节点缺失: {', '.join(missing)} (无渲染设备)"


# (metric key, 中文名, 检查函数) —— 顺序即报错时的展示顺序
_PRECHECKS = (
    ("precheck_display", "HDMI 显示器", _precheck_display),
    ("precheck_wayland", "Wayland 会话", _precheck_wayland),
    ("precheck_dri", "DRM 节点", _precheck_dri),
)


@register
class GPUTest(TestCase):
    name = "gpu"
    display_name = "GPU 渲染"
    description = "OpenGL ES 2 全屏渲染压测 (glmark2-es2, Wayland 全屏, 跑满一轮)"

    def __init__(self):
        self.proc = None
        self.cancel_done = False
        self._pump_task: Optional[asyncio.Task] = None
        self._heartbeat_task: Optional[asyncio.Task] = None
        self.scenes: int = 0
        self._tail: List[str] = []

    async def cancel(self):
        if self.proc and self.proc.returncode is None and not self.cancel_done:
            self.cancel_done = True
            try:
                self.proc.terminate()
                try:
                    await asyncio.wait_for(self.proc.wait(), 2)
                except asyncio.TimeoutError:
                    try:
                        self.proc.kill()
                    except Exception:
                        pass
            except Exception:
                pass

    async def _pump_stdout(self, ctx: TestContext):
        """读 glmark2 输出: 逐行推日志 + 数完成的场景数.

        glmark2-es2 是持续写 stdout 的 (每个场景一行), 所以这里用逐行读;
        但 §11.5 要求的"不阻塞在 readline"由外层的挂钟 + 心跳兜底保证。
        """
        assert self.proc.stdout is not None
        while True:
            try:
                raw = await self.proc.stdout.readline()
            except Exception:
                break
            if not raw:
                break
            line = raw.decode(errors="replace").rstrip()
            if not line:
                continue
            if re.search(r"^Scene Number", line):
                self.scenes += 1
            self._tail.append(line)
            del self._tail[:-_TAIL_LINES]  # 只留末尾 N 行做失败上下文
            await ctx.on_log(f"[GPU] {line}")

    async def _heartbeat(self, ctx: TestContext, t0: float, timeout_sec: float):
        """§10.1: 静默阶段也周期性推心跳, 不让用户怀疑工具死锁."""
        while True:
            await asyncio.sleep(_HEARTBEAT_SEC)
            elapsed = time.time() - t0
            # 进度按"已用时 / 超时上限"估算, 封顶 90% —— 剩下的留给收尾判定
            pct = _PRECHECK_DONE_PCT + min(85, int(elapsed / timeout_sec * 85))
            scene = f" · 已完成 {self.scenes} 个场景" if self.scenes else ""
            await ctx.on_progress(pct, f"全屏渲染中 · 已用 {elapsed:.0f}s{scene}")

    async def run(self, ctx: TestContext) -> TestResult:
        result = TestResult()
        t_cfg = ctx.test_config or {}
        timeout_sec = int(t_cfg.get("timeout_sec", _DEFAULT_TIMEOUT_SEC))

        # ── 工具查找 (§11.1: 禁 which, 用 find_tool) ──
        glmark_bin = find_tool("glmark2-es2")
        if not glmark_bin:
            result.status = "error"
            result.error = (
                "未找到 glmark2-es2. 解决: sudo apt install glmark2-es2-wayland "
                "(Wayland 后端必备; 见 docs/需求设计.md §11.7)"
            )
            await ctx.on_log(f"[GPU] ✗ {result.error}")
            return result
        await ctx.on_log(f"[GPU] 工具: {glmark_bin}")

        if not sudo_hint_if_unavailable(result, "GPU 渲染"):
            await ctx.on_log(f"[GPU] ✗ {result.error}")
            return result

        # ── 前置检查 (§6.3: 缺任一 → error, 不静默降级) ──
        await ctx.on_log("[GPU] 前置检查...")
        failed: List[str] = []
        for key, label, checker in _PRECHECKS:
            ok, detail = checker()
            result.metrics[key] = ok
            await ctx.on_log(f"[GPU]   {'✓' if ok else '✗'} {label}: {detail}")
            if not ok:
                failed.append(f"{label} —— {detail}")
        if failed:
            result.status = "error"
            result.error = "GPU 前置条件不满足 (不静默降级, 见 §6.3):\n  - " + "\n  - ".join(failed)
            result.metrics["stderr_tail"] = ""
            await ctx.on_log(f"[GPU] ✗ {result.error}")
            return result

        await ctx.on_progress(_PRECHECK_DONE_PCT, "前置检查通过, 启动 glmark2-es2")

        # ── 构造命令 (§11.2 命令级 sudo -n; 会话环境变量必须透传) ──
        cmd = with_sudo([glmark_bin, "--fullscreen"], env=self._session_env())
        await ctx.on_log(f"[GPU] 命令: {' '.join(cmd)}")
        await ctx.on_log(f"[GPU] 超时上限: {timeout_sec}s (超时即 error)")

        # ── 起进程 ──
        try:
            self.proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except FileNotFoundError:
            result.status = "error"
            result.error = "启动 glmark2-es2 失败: FileNotFoundError"
            await ctx.on_log(f"[GPU] ✗ {result.error}")
            return result

        t0 = time.time()
        self._pump_task = asyncio.create_task(self._pump_stdout(ctx))
        self._heartbeat_task = asyncio.create_task(self._heartbeat(ctx, t0, timeout_sec))
        await ctx.on_log("[GPU] glmark2-es2 已启动, 全屏渲染中...")

        # ── 等待: 轮询挂钟, 每 0.5s 同时看超时 / 取消 (§11.5) ──
        timed_out = False
        cancelled = False
        while self.proc.returncode is None:
            await asyncio.sleep(0.5)
            if await ctx.is_cancelled():
                cancelled = True
                await self.cancel()
                break
            if time.time() - t0 > timeout_sec:
                timed_out = True
                await self.cancel()
                break

        elapsed = time.time() - t0
        # 收尾: 等读端和心跳退出
        for task in (self._pump_task, self._heartbeat_task):
            task.cancel()
        try:
            await asyncio.wait_for(
                asyncio.gather(self._pump_task, self._heartbeat_task, return_exceptions=True),
                timeout=3,
            )
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass

        out = "\n".join(self._tail)
        rc = self.proc.returncode if self.proc.returncode is not None else -1

        # ── 指标 (§6.3) ──
        score_m = re.search(r"glmark2 Score:\s*([\d.]+)", out)
        score = float(score_m.group(1)) if score_m else None
        result.metrics["elapsed_sec"] = round(elapsed, 1)
        result.metrics["exit_code"] = rc
        result.metrics["score"] = score if score is not None else 0
        result.metrics["scenes_completed"] = self.scenes
        # glmark2 不报逐帧计数, 用"跑完的场景数"作为渲染单元数的代理指标
        result.metrics["frames_rendered"] = self.scenes
        result.metrics["timeout_sec"] = timeout_sec

        if cancelled:
            result.status = "stopped"
            result.error = "用户中途取消"
            await ctx.on_log("[GPU] ⏹ 用户取消, 已终止 glmark2-es2")
            return result

        if timed_out:
            result.status = "error"
            result.error = (
                f"glmark2-es2 超过 {timeout_sec}s 未跑完一轮 (防跑飞挂死整个 run)。\n"
                f"超时前已完成 {self.scenes} 个场景。输出末尾:\n{out}"
            )
            result.metrics["stderr_tail"] = out
            await ctx.on_log(f"[GPU] ✗ 超时 {timeout_sec}s 未完成, 判 error")
            return result

        if rc != 0:
            result.status = "failed"
            result.error = f"glmark2-es2 异常退出, exit code = {rc}。输出末尾:\n{out}"
            result.metrics["stderr_tail"] = out
            await ctx.on_log(f"[GPU] ❌ exit code={rc}, 判 failed")
            return result

        # 通过判定: exit 0 **且** 输出以 glmark2 Score 收尾 (§6.3)
        if score is None:
            result.status = "failed"
            result.error = (
                f"exit code 0 但输出里没有 `glmark2 Score:` —— 一轮未完整跑完。输出末尾:\n{out}"
            )
            result.metrics["stderr_tail"] = out
            await ctx.on_log("[GPU] ❌ 缺 glmark2 Score, 一轮未跑完, 判 failed")
            return result

        result.status = "passed"
        result.log = out
        await ctx.on_progress(100, f"完成 · score {score:.0f} / {elapsed:.0f}s")
        await ctx.on_log(
            f"[GPU] ✅ 一轮跑完: glmark2 Score: {score:.0f}, "
            f"{self.scenes} 个场景, {elapsed:.0f}s, exit 0"
        )
        return result

    @staticmethod
    def _session_env() -> dict:
        """要透传给 sudo 的图形会话环境变量 (§11.2).

        sudo 默认 env_reset 会清掉这些变量 —— 没有 WAYLAND_DISPLAY /
        XDG_RUNTIME_DIR, glmark2 在 root 环境下连不上 compositor 必挂。
        """
        keys = ("WAYLAND_DISPLAY", "XDG_RUNTIME_DIR", "XDG_SESSION_TYPE",
                "DISPLAY", "XAUTHORITY", "XDG_SESSION_ID")
        return {k: os.environ[k] for k in keys if os.environ.get(k)}