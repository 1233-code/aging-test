"""测试共享工具: 找二进制 / 跑子进程.

解决 PATH 不含 /sbin 的问题 (普通用户跑 uvicorn 时找不到 ethtool/iw/...).
"""
import asyncio
import os
import shutil
import signal
import time
from typing import List, Optional, Tuple


# ── 进程超时管理 (避免 rc=-15 误判) ──
async def run_proc_with_wallclock(
    cmd: List[str],
    duration_sec: float,
    grace_sec: float = 5.0,
) -> Tuple[Optional[asyncio.subprocess.Process], int, str, float, bool]:
    """启动子进程, 按挂钟等它自然退; 超时硬杀.

    返回 (proc, returncode, stdout, elapsed_sec, killed_by_us).
    - proc=None 表示启动失败 (FileNotFoundError)
    - killed_by_us=True 表示超过 duration_sec 我们杀的 (退出码 -15 等)

    调用方用 killed_by_us 区分:
      - 跑满挂钟 → 即使 rc=-15 也算 pass
      - 自然崩 (rc>0, elapsed<duration) → 算 fail
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except FileNotFoundError:
        return None, -1, "", 0.0, False

    t0 = time.time()
    killed_by_us = False
    try:
        await asyncio.wait_for(proc.wait(), timeout=duration_sec + grace_sec)
    except asyncio.TimeoutError:
        killed_by_us = True
        await kill_proc(proc, signal.SIGKILL)
    except asyncio.CancelledError:
        killed_by_us = True
        await kill_proc(proc, signal.SIGTERM, wait_sec=2.0)
        raise

    elapsed = time.time() - t0
    rc = proc.returncode if proc.returncode is not None else -1
    try:
        out, _ = await proc.communicate()
    except Exception:
        out = b""
    return proc, rc, out.decode(errors="replace"), elapsed, killed_by_us


def find_tool(name: str) -> Optional[str]:
    """在 PATH + /sbin + /usr/sbin + /usr/local/sbin 找可执行.

    返回绝对路径或 None.
    """
    p = shutil.which(name)
    if p:
        return p
    for d in ("/usr/local/sbin", "/usr/sbin", "/sbin", "/usr/local/bin"):
        full = os.path.join(d, name)
        if os.path.isfile(full) and os.access(full, os.X_OK):
            return full
    return None


async def run_cmd(
    cmd: List[str],
    timeout: float = 5.0,
    input_data: Optional[bytes] = None,
) -> Tuple[int, str, str]:
    """跑子进程, 返回 (returncode, stdout, stderr). 超时 kill."""
    binary = cmd[0]
    if find_tool(binary) is None:
        return -1, "", f"tool not found: {binary} (PATH 不含 /sbin, 试试 sudo)"

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.PIPE if input_data else asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(input=input_data), timeout=timeout)
        return (
            proc.returncode if proc.returncode is not None else -1,
            stdout.decode(errors="replace"),
            stderr.decode(errors="replace"),
        )
    except asyncio.TimeoutError:
        await kill_proc(proc, signal.SIGKILL, wait_sec=1.0)
        return -1, "", f"timeout after {timeout}s"


def is_root() -> bool:
    return os.geteuid() == 0


def sudo_available() -> bool:
    """sudo 是否可用 (对应 §11.2 的 `sudo -n true` 自检项)."""
    return find_tool("sudo") is not None


def with_sudo(cmd: List[str], env: Optional[dict] = None,
              timeout_sec: Optional[float] = None,
              kill_after_sec: float = 5.0) -> List[str]:
    """把命令包成命令级 sudo 提权 (§11.2)。

    统一走 ["sudo", "-n", ...] —— `-n` 非交互, 绝不弹密码提示, 否则产线会卡死。

    env: 需要透传给子进程的环境变量。**必须显式传** —— sudo 默认 env_reset
    会清掉 WAYLAND_DISPLAY / XDG_RUNTIME_DIR 等会话变量, GPU 全屏渲染这类
    依赖图形会话的命令在 root 环境下连不上 compositor 必挂。
    所以走 `sudo -n env VAR=... cmd` 而不是裸 `sudo -n cmd`。

    timeout_sec: **长跑命令必传**。见下面「为什么要 timeout」。

    ── 为什么要 timeout ──
    §11.2 要求一律 sudo, 于是子进程 real/effective/saved UID 全是 root, 而
    **服务本身仍以普通用户跑**。POSIX 规定非 root 进程对 root 进程 os.kill
    一律 EPERM (`PermissionError(1, 'Operation not permitted')`) —— 也就是
    **服务方根本没法自己收尾**, 只能在 root 领地内部安排 killer。

    `timeout` 就是那个 killer: `sudo -n timeout -k 5 60 memtester ...` 里
    timeout 和 memtester 同为 root, 到点它自己杀自己的子进程, 服务全程一个
    信号都不用发, EPERM 从结构上不存在。不这样做时实测: duration_sec=60 的
    内存测试收尾失败, memtester 已经跑到 537s、占 1.0 GB 还不退出。
    """
    if timeout_sec is not None:
        tmo = find_tool("timeout") or "/usr/bin/timeout"
        cmd = [tmo, "-k", str(kill_after_sec), str(timeout_sec)] + list(cmd)
    if is_root() and not env:
        # 已经是 root 且无需透传环境, 免一层 sudo (免密未配好时也不至于挂)
        return list(cmd)
    prefix = ["sudo", "-n"]
    if env:
        prefix += ["env"] + [f"{k}={v}" for k, v in env.items()]
    return prefix + list(cmd)


# ── 收尾 (with_sudo 的配套) ──

async def kill_proc(proc: Optional[asyncio.subprocess.Process],
                    sig: int = signal.SIGTERM,
                    wait_sec: float = 2.0,
                    ctx=None) -> bool:
    """终止子进程。返回 True = 已确认退出。

    **不发任何特权信号** —— 见上面 with_sudo 的「为什么要 timeout」。非 root 身份
    对 root 子进程发信号必然 EPERM; 与其绕道 `sudo kill` 提权, 不如从一开始就让
    root 领地内部的 `timeout` 负责收尾, 服务这边只处理"确实杀得掉"的普通子进程。

    返回 False = **没能确认进程已退出**, 两种情形:
      - 子进程是 sudo 起的 root 进程 → 发不了信号, 交给 `timeout` 到点收尾
      - 普通子进程但没在 wait_sec 内退出 → 调用方继续等或告警

    绝不抛异常: 收尾失败不该把一个跑满挂钟、本该 passed 的项打成 error。
    """
    if proc is None:
        return True
    try:
        proc.kill() if sig == signal.SIGKILL else proc.terminate()
    except ProcessLookupError:
        return True                          # 已经退了
    except PermissionError:
        await _note(ctx, f"[进程] 子进程 {proc.pid} 是 root (with_sudo 起的), "
                         f"普通用户发不了信号 —— 已交由 `timeout` 在其挂钟上限处"
                         f"自行收尾, 本次不等它")
        return False
    except Exception as e:
        await _note(ctx, f"[进程] 终止 {proc.pid} 异常: {e!r}")
        return False
    try:
        await asyncio.wait_for(proc.wait(), wait_sec)
        return True
    except asyncio.TimeoutError:
        return False


async def _note(ctx, msg: str) -> None:
    """ctx 是 TestContext (鸭子类型, 避免 _tools 反向 import base)。"""
    if ctx is None:
        return
    try:
        await ctx.on_log(msg)
    except Exception:
        pass



def sudo_hint_if_unavailable(result, test_name: str) -> bool:
    """检查 sudo 可用性; 不可用时把 result 置 error 并返回 False.

    §11.2 硬规则: sudo 不可用 → 该项判 **error**, **绝不静默降级成 skipped**。
    静默跳过会让工人看到"通过"而实际没测, 是产线上最危险的一类故障 (§9.3)。
    """
    if sudo_available():
        return True
    result.status = "error"
    result.error = (
        f"[{test_name}] sudo 不可用或未配免密 —— 需 NOPASSWD 配置 (docs/需求设计.md §11.2)。"
        "不静默降级为 skipped, 因为那会让工人看到『通过』而实际没测。"
    )
    return False


def need_root_hint(test_name: str) -> str:
    """给"需要 root"的测试生成统一提示 (§11.2 命令级 sudo 口径)."""
    return (
        f"[{test_name}] 此测试需要 root (改 IP / set interface / 切模式). "
        "已通过命令级 `sudo -n` 取权 (§11.2); 若报此提示, "
        "说明 sudo 未配 NOPASSWD, 请联系产线管理员 (见 docs/需求设计.md §11.2)."
    )


def find_wifi_iface() -> Optional[str]:
    """自动找系统的 wifi 接口 (常见名: wlan0, wlan1, wlp3s0, wlx00e1ce...).

    返回第一个 wireless 接口, 没有就 None.
    """
    net = "/sys/class/net"
    if not os.path.isdir(net):
        return None
    # 优先 wlan* (典型命名)
    for name in sorted(os.listdir(net)):
        if name.startswith("wlan") and os.path.isdir(f"{net}/{name}/wireless"):
            return name
    # 次优 wlp* (pci 命名)
    for name in sorted(os.listdir(net)):
        if name.startswith("wlp") and os.path.isdir(f"{net}/{name}/wireless"):
            return name
    # 兜底 wlx* (MAC 地址命名)
    for name in sorted(os.listdir(net)):
        if name.startswith("wlx") and os.path.isdir(f"{net}/{name}/wireless"):
            return name
    # 最后一个: 任意有 wireless 子目录的
    for name in sorted(os.listdir(net)):
        if os.path.isdir(f"{net}/{name}/wireless"):
            return name
    return None
