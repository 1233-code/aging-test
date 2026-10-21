"""测试共享工具: 找二进制 / 跑子进程.

解决 PATH 不含 /sbin 的问题 (普通用户跑 uvicorn 时找不到 ethtool/iw/...).
"""
import asyncio
import os
import shutil
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
        try:
            proc.kill()
        except Exception:
            pass
        try:
            await asyncio.wait_for(proc.wait(), 2)
        except asyncio.TimeoutError:
            pass
    except asyncio.CancelledError:
        killed_by_us = True
        try:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), 2)
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except Exception:
                    pass
        except Exception:
            pass
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
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return -1, "", f"timeout after {timeout}s"


def is_root() -> bool:
    return os.geteuid() == 0


def sudo_available() -> bool:
    """sudo 是否可用 (对应 §11.2 的 `sudo -n true` 自检项)."""
    return find_tool("sudo") is not None


def with_sudo(cmd: List[str], env: Optional[dict] = None) -> List[str]:
    """把命令包成命令级 sudo 提权 (§11.2).

    统一走 ["sudo", "-n", ...] —— `-n` 非交互, 绝不弹密码提示, 否则产线会卡死。

    env: 需要透传给子进程的环境变量。**必须显式传** —— sudo 默认 env_reset
    会清掉 WAYLAND_DISPLAY / XDG_RUNTIME_DIR 等会话变量, GPU 全屏渲染这类
    依赖图形会话的命令在 root 环境下连不上 compositor 必挂。
    所以走 `sudo -n env VAR=... cmd` 而不是裸 `sudo -n cmd`。

    注意: 服务本身以普通用户身份启动, 只有这里包出来的子进程是 root。
    """
    if is_root() and not env:
        # 已经是 root 且无需透传环境, 免一层 sudo (免密未配好时也不至于挂)
        return list(cmd)
    prefix = ["sudo", "-n"]
    if env:
        prefix += ["env"] + [f"{k}={v}" for k, v in env.items()]
    return prefix + list(cmd)


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
