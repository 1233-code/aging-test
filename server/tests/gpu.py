"""GPU 渲染压测 - OpenGL ES 2 全屏渲染 (§6.3)

工具: glmark2-es2 --fullscreen (走 Wayland 全屏输出, 不需 X11)

设计要点 (严格按 §6.3):
  - **前置条件缺任一 → 本项 error 并明确报错, 不做静默降级**
    ① HDMI 显示器已连接  /sys/class/drm/*HDMI*/status == connected
    ② Wayland 会话已启动  WAYLAND_DISPLAY 非空 + compositor 进程在
    ③ DRM 节点存在        /dev/dri/card* + /dev/dri/renderD*
    产线常态接 HDMI, 因此不进入"未接显示器 → skipped"分支; 第 ① 条不满足
    属工位配置问题, 报 error 而非 skipped, 否则等于 GPU 静默漏测。
  - **通过判定** —— 每轮都完整跑完: exit code == 0 且输出以 `glmark2 Score: <数字>` 收尾
  - **超时兜底** —— gpu.timeout_sec (默认 600s, **单轮**上限而非整项上限),
    超时即 error + 记 stderr 末 N 行, 防止个别机器跑飞挂死整个 run
  - **轮次 `gpu.rounds`** —— 默认 1 (= 原单轮行为, 出货判定口径不变), 可手动调大。
    每轮独立进程 / 独立挂钟 / 独立超时, 不共享上下文, 这样各轮分数才可比;
    任一轮失败即整项 failed 并记 `failed_round`。多轮时额外检测分数衰减趋势
    (末轮比首轮跌超 20% 就提示) —— 各轮都判通过但分数一路下滑, 单轮永远看不出来。
    **不提供 [时长]**: 单轮时长由 GL 驱动决定, 给工人调时长没有意义。
"""
import asyncio
import glob
import os
import re
import time
from typing import List, Optional, Tuple

from .base import TestCase, TestContext, TestResult, register
from ._tools import find_tool, with_sudo, kill_proc, sudo_hint_if_unavailable

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


# 多轮时末轮分数比首轮跌得超过这个比例就提示 —— 衰减趋势是间歇性故障的信号
_SCORE_DRIFT_PCT = 20.0


def _score_drift(detail: List[dict]) -> Optional[str]:
    """多轮分数是否出现明显衰减。

    单轮给不了这个信号 —— 第 1 轮 800、第 3 轮 200 说明 GPU 在跑的过程中
    劣化了, 而每一轮单独看都是「通过」。只在 rounds ≥ 2 且首尾都有分时判定。
    """
    scores = [d["score"] for d in detail if d.get("score")]
    if len(scores) < 2 or scores[0] <= 0:
        return None
    drop = (scores[0] - scores[-1]) / scores[0] * 100
    if drop < _SCORE_DRIFT_PCT:
        return None
    return (
        f"分数从第 1 轮 {scores[0]:.0f} 跌到第 {len(scores)} 轮 {scores[-1]:.0f} "
        f"(跌 {drop:.0f}%, 超 {_SCORE_DRIFT_PCT:.0f}%) —— 各轮都判通过, "
        f"但这个衰减趋势可能指向 GPU 劣化, 建议复测并送工程师排查"
    )


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
    description = "OpenGL ES 2 全屏渲染压测 (glmark2-es2, Wayland 全屏, 可跑多轮)"

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
            # 不用裸 terminate/kill —— 子进程是 sudo 起的 root 进程, 普通用户
            # 对它发信号必然 EPERM, 裸调会把收尾自己炸掉, 还让 root 测试进程
            # 泄漏在后台。见 _tools.kill_proc 的说明。
            await kill_proc(self.proc, wait_sec=2.0)

    async def _pump_stdout(self, ctx: TestContext, tag: str = ""):
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
            await ctx.on_log(f"[GPU] {tag}{line}")

    async def _heartbeat(self, ctx: TestContext, t0: float, timeout_sec: float,
                         pct_lo: int = _PRECHECK_DONE_PCT, span: int = 95):
        """§10.1: 静默阶段也周期性推心跳, 不让用户怀疑工具死锁.

        进度按"本轮已用时 / 本轮超时上限"估算, 映射到整项的 [pct_lo, pct_lo+span)
        区间 —— 多轮时每轮各占一段, 不会每轮都从头冲到 90%。
        """
        while True:
            await asyncio.sleep(_HEARTBEAT_SEC)
            elapsed = time.time() - t0
            # 留最后一点给收尾判定, 所以封到 span-10
            pct = pct_lo + min(max(span - 10, 1), int(elapsed / timeout_sec * (span - 10)))
            scene = f" · 已完成 {self.scenes} 个场景" if self.scenes else ""
            await ctx.on_progress(pct, f"全屏渲染中 · 已用 {elapsed:.0f}s{scene}")

    async def run(self, ctx: TestContext) -> TestResult:
        result = TestResult()
        t_cfg = ctx.test_config or {}
        timeout_sec = int(t_cfg.get("timeout_sec", _DEFAULT_TIMEOUT_SEC))
        rounds = max(1, int(t_cfg.get("rounds", 1)))   # §6.3: 默认 1 轮 = 原单轮行为

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

        result.metrics["rounds"] = rounds
        result.metrics["timeout_sec"] = timeout_sec
        result.metrics["rounds_detail"] = []
        await ctx.on_progress(_PRECHECK_DONE_PCT, "前置检查通过, 启动 glmark2-es2")
        await ctx.on_log(
            f"[GPU] 计划: 跑 {rounds} 轮完整 glmark2 (每轮独立计时, 单轮超时 {timeout_sec}s)"
        )

        # ── 轮次循环: 每轮独立跑完整一轮, 任一轮失败即整项失败 ──
        total_t0 = time.time()
        for idx in range(1, rounds + 1):
            if await ctx.is_cancelled():
                result.status = "stopped"
                result.error = f"用户中途取消 (已完成 {idx - 1}/{rounds} 轮)"
                await ctx.on_log(f"[GPU] ⏹ 用户取消, 已终止 (第 {idx} 轮未开始)")
                return result

            round_result = await self._run_one_round(
                ctx, glmark_bin, timeout_sec,
                idx, rounds,
                pct_lo=_PRECHECK_DONE_PCT,
                pct_hi=100,
            )
            result.metrics["rounds_detail"].append(round_result)

            # 最后一轮的标量指标 = 本项对外口径 (§6.3: score 取最后一轮)
            result.metrics["score"] = round_result["score"]
            result.metrics["elapsed_sec"] = round(total_t0 and (time.time() - total_t0), 1)
            result.metrics["exit_code"] = round_result["exit_code"]
            result.metrics["scenes_completed"] = round_result["scenes"]
            # glmark2 不报逐帧计数, 用"跑完的场景数"作为渲染单元数的代理指标
            result.metrics["frames_rendered"] = round_result["scenes"]
            result.log = round_result["tail"]

            if round_result["status"] != "passed":
                result.status = round_result["status"]
                result.error = round_result["error"]
                result.metrics["failed_round"] = idx
                result.metrics["stderr_tail"] = round_result["tail"]
                return result

        # 全部轮次跑完 —— 额外报一次分数趋势 (rounds>1 才有意义)
        detail = result.metrics["rounds_detail"]
        result.status = "passed"
        await ctx.on_progress(100, f"完成 · {rounds} 轮全过")
        scores = " / ".join(f"{d['score']:.0f}" for d in detail)
        await ctx.on_log(f"[GPU] ✅ {rounds} 轮全部跑完, 各轮 score: {scores}")
        if rounds > 1:
            drift = _score_drift(detail)
            if drift:
                await ctx.on_log(f"[GPU] ⚠ {drift}")
        return result

    async def _run_one_round(
        self,
        ctx: TestContext,
        glmark_bin: str,
        timeout_sec: int,
        idx: int,
        total: int,
        pct_lo: int,
        pct_hi: int,
    ) -> dict:
        """跑**一轮**完整 glmark2, 返回本轮的指标字典 (§6.3)。

        每轮独立进程、独立挂钟、独立超时 —— 不共享任何上下文,
        这样第 2 轮的分数才和第 1 轮可比 (能看出衰减趋势)。
        """
        self.scenes = 0
        self._tail = []
        self.cancel_done = False
        # 轮次进度映射到整项的 [pct_lo, pct_hi] 区间
        span = max(1, pct_hi - pct_lo)

        # ── 构造命令 (§11.2 命令级 sudo -n; 会话环境变量必须透传) ──
        # **必须带 timeout_sec**: glmark2 在 GPU 挂死 / 驱动卡住时会一直不退出。
        # 它被提权成 root 进程后, 我们 (普通用户) 发不了信号去收它 —— 只能让
        # 同为 root 的 `timeout` 到点自己杀它, 否则测试任务永不结束, 连服务
        # 关机都会被拖住。这里 timeout 给得比内部判定宽裕几秒。
        cmd = with_sudo([glmark_bin, "--fullscreen"],
                        env=self._session_env(), timeout_sec=timeout_sec + 15)
        tag = f"第 {idx}/{total} 轮 · " if total > 1 else ""
        await ctx.on_log(f"[GPU] {tag}命令: {' '.join(cmd)}")
        await ctx.on_log(f"[GPU] {tag}单轮超时上限: {timeout_sec}s (超时即 error)")

        # ── 起进程 ──
        try:
            self.proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except FileNotFoundError:
            return {
                "round": idx, "status": "error", "score": 0.0, "scenes": 0,
                "elapsed_sec": 0.0, "exit_code": -1, "tail": "",
                "error": "启动 glmark2-es2 失败: FileNotFoundError",
            }

        t0 = time.time()
        self._pump_task = asyncio.create_task(self._pump_stdout(ctx, tag))
        self._heartbeat_task = asyncio.create_task(
            self._heartbeat(ctx, t0, timeout_sec, pct_lo, span)
        )
        await ctx.on_log(f"[GPU] {tag}glmark2-es2 已启动, 全屏渲染中...")

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
        score_m = re.search(r"glmark2 Score:\s*([\d.]+)", out)
        score = float(score_m.group(1)) if score_m else None

        base = {
            "round": idx,
            "score": score if score is not None else 0.0,
            "scenes": self.scenes,
            "elapsed_sec": round(elapsed, 1),
            "exit_code": rc,
            "tail": out,
        }

        if cancelled:
            return {**base, "status": "stopped",
                    "error": f"用户中途取消 (第 {idx}/{total} 轮)"}
        if timed_out:
            await ctx.on_log(f"[GPU] ✗ {tag}超时 {timeout_sec}s 未完成, 判 error")
            return {**base, "status": "error", "error": (
                f"{tag}glmark2-es2 超过 {timeout_sec}s 未跑完一轮 (防跑飞挂死整个 run)。\n"
                f"超时前已完成 {self.scenes} 个场景。输出末尾:\n{out}"
            )}
        if rc != 0:
            await ctx.on_log(f"[GPU] ❌ {tag}exit code={rc}, 判 failed")
            return {**base, "status": "failed", "error": (
                f"{tag}glmark2-es2 异常退出, exit code = {rc}。输出末尾:\n{out}"
            )}
        # 通过判定: exit 0 **且** 输出以 glmark2 Score 收尾 (§6.3)
        if score is None:
            await ctx.on_log(f"[GPU] ❌ {tag}缺 glmark2 Score, 一轮未跑完, 判 failed")
            return {**base, "status": "failed", "error": (
                f"{tag}exit code 0 但输出里没有 `glmark2 Score:` —— 一轮未完整跑完。输出末尾:\n{out}"
            )}

        await ctx.on_log(
            f"[GPU] ✅ {tag}跑完: glmark2 Score: {score:.0f}, "
            f"{self.scenes} 个场景, {elapsed:.0f}s, exit 0"
        )
        return {**base, "status": "passed", "score": score, "error": None}

    @staticmethod
    def _session_env() -> dict:
        """要透传给 sudo 的图形会话环境变量 (§11.2).

        sudo 默认 env_reset 会清掉这些变量 —— 没有 WAYLAND_DISPLAY /
        XDG_RUNTIME_DIR, glmark2 在 root 环境下连不上 compositor 必挂。
        """
        keys = ("WAYLAND_DISPLAY", "XDG_RUNTIME_DIR", "XDG_SESSION_TYPE",
                "DISPLAY", "XAUTHORITY", "XDG_SESSION_ID")
        return {k: os.environ[k] for k in keys if os.environ.get(k)}