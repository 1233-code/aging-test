"""测试共享工具: 找二进制 / 跑子进程.

解决 PATH 不含 /sbin 的问题 (普通用户跑 uvicorn 时找不到 ethtool/iw/...).
"""
import asyncio
import os
import shutil
import signal
import time
from typing import Awaitable, Callable, List, Optional, Tuple


# ── 终端还原: 把子进程"原地重绘"的输出还原成屏幕上的样子 ──

class TtyDemangler:
    """把子进程用退格符/回车做的**原地重绘**还原成屏幕上最终的样子。

    有些工具为了在终端上显示进度, **不换行**, 而是用退格符(\\b)把刚写的
    几个字擦掉重画。memtester 4.x 就是这样:

        Stuck Address       : <b>setting   0<b>testing   0<b>setting   1...

    "Random Value" 那一项画的是一个 -\\|/ 的转轮, 每转一格就用 3 个退格符
    擦掉上一格。后果: **一行**原始输出里能塞进几万字符 (1024MB 那次光这一
    项就 50KB+), 整轮下来十几 MB。

    直接存进日志, 报告里就是好几页的 "-\\|/" —— 一个信息都没有, 却把真
    正要看的部分挤到后面去 (产线反馈: 「好几页都是这个」)。

    还原规则就是终端自己的规矩: 退格符擦掉前一个字符, 回车清空整行, 换行
    收尾。还原后 memtester 的每个测试项变成干净的一行:

        Stuck Address       : ok
        Random Value        : ok

    被擦掉的是**重绘的中间态**(同一件事被反复显示), 不是内容 —— 每个测试
    项的最终结论一个字都不会少。

    ⚠️ 喂进来的原始文本**不能**先 rstrip(): Python 认为 \\b 是空白字符,
    rstrip 会把行尾的退格符吃掉, 还原出来就不对了。
    """

    def __init__(self):
        self._cur: List[str] = []      # 当前行还没定稿的可见字符
        self._done: List[str] = []     # 已经定稿的行

    def feed(self, text: str) -> List[str]:
        """喂一段原始文本, 返回**已经定稿**的行 (没换行的尾巴先留着)。"""
        for ch in text:
            if ch == "\b":
                if self._cur:
                    self._cur.pop()
            elif ch == "\r":
                self._cur = []
            elif ch == "\n":
                self._done.append("".join(self._cur))
                self._cur = []
            else:
                self._cur.append(ch)
        done, self._done = self._done, []
        return done

    def flush(self) -> List[str]:
        """进程被掐掉、最后一段没有换行时, 把尾巴也交出来。"""
        if self._cur:
            self._done.append("".join(self._cur))
            self._cur = []
        done, self._done = self._done, []
        return done


def demangle_tty(text: str) -> str:
    """一次性还原一整段输出 (给测试用)。"""
    d = TtyDemangler()
    return "\n".join(d.feed(text) + d.flush())


# ── 带进度的子进程等待 ──

async def run_proc_with_progress(
    proc: asyncio.subprocess.Process,
    duration_sec: float,
    ctx=None,
    on_tick: Optional[Callable[[float], Awaitable[None]]] = None,
    tick_sec: float = 1.0,
    hard_limit_sec: Optional[float] = None,
) -> Tuple[int, str, bool]:
    """按挂钟等子进程自然退出, 期间每 tick_sec 回调一次。返回 (rc, 输出, 是否被我们杀掉)。

    **为什么不能用 `communicate()` 一把等到底** —— 那是一次阻塞到进程退出,
    中间既不报进度也不查取消: 工人看着进度条不动, 点了「停止」也没反应,
    要等到工具自己跑完。存储类测试动辄几分钟, 这个体验等于卡死。

    **为什么不能靠 `readline()` 驱动循环** —— 只有 stdout 吐行时才会醒。
    stress-ng 绝大部分时间不吐行, 于是进度条和温度采样跟着一起僵住
    (CPU 项踩过这个坑: 进度条看上去不动, 其实 CPU 满载跑了三分钟)。

    所以: stdout 用独立 task 抽干, 主循环只管计时和查取消。两边互不拖累。
    """
    lines: List[str] = []

    async def drain():
        try:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                lines.append(line.decode(errors="replace").rstrip())
                # 只留尾部 —— 但**上限给得很大**。
                #
                # 原来这里是 400 行, 而这份输出是要**原样进报告**的(CPU 项把
                # 它逐行推给 on_log, runner 留档 → 报告的日志附录)。截到 400
                # 行, 报告里的 CPU 日志就只有尾巴那一段 —— 看着像日志, 实际
                # 缺了头, 比没有更糟。
                # 400 行也不够挡住"跑几小时把内存吃光": 一行几十字节, 十万行
                # 也就几 MB, 而 storage 类测试都有 timeout 兜底。
                if len(lines) > 50000:
                    del lines[:-50000]
        except Exception:
            pass

    drain_task = asyncio.create_task(drain())
    wait_task = asyncio.create_task(proc.wait())

    t0 = time.monotonic()
    killed = False
    while True:
        done, _ = await asyncio.wait({wait_task}, timeout=tick_sec)
        if done:
            break
        elapsed = time.monotonic() - t0

        # 硬上限: 超了就我们自己杀 (正常情况下 with_sudo() 里的 `timeout`
        # 会先到点, 走不到这里; 这道是给 `timeout` 自己都不在了的场合兜底)
        if hard_limit_sec is not None and elapsed > hard_limit_sec:
            killed = True
            await kill_proc(proc, wait_sec=3.0, ctx=ctx)
            break

        if ctx is not None and await ctx.is_cancelled():
            killed = True
            await kill_proc(proc, wait_sec=3.0, ctx=ctx)
            break

        if on_tick:
            await on_tick(elapsed)

    # 收尾: 等 stdout 抽干, 但不无限等 —— 进程退了管道也该 EOF 了
    try:
        await asyncio.wait_for(drain_task, timeout=3.0)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        drain_task.cancel()
    except Exception:
        pass

    rc = proc.returncode if proc.returncode is not None else -1
    return rc, "\n".join(lines), killed


def fio_runtime_args(duration_sec: float, size_mb: int) -> List[str]:
    """让 fio **真的跑满** duration_sec —— 光给 --size 它跑完一遍就退了。

    fio 默认按「把 --size 写/读一遍」结束, 不管花多久。eMMC/USB/TF 都很快,
    一个 64-256MB 的文件几秒就完事 —— 于是 UI 上配的「时长」形同虚设,
    工人看到的是「设了 60s 却十几秒就跑了」, 反而怀疑工具是不是有问题。

    `--time_based` 把语义从「跑满 size」换成「跑满 runtime」, 此时 size 变成
    工作集大小(反复覆写), 不再影响实际耗时。两个参数必须**一起**给 ——
    只给 --runtime 而不加 --time_based 的话, fio 会无视 runtime 直接退出。
    """
    d = max(1, int(duration_sec))
    return [
        f"--runtime={d}",
        f"--size={int(size_mb)}M",
        "--time_based=1",
    ]


def fio_timeout_sec(duration_sec: float) -> int:
    """fio 的挂钟兜底上限(秒)。

    runtime 到点 fio 自己会退; 但坏块 / 驱动异常时它可能根本退不出来,
    那种情况只能靠同为 root 的 `timeout` 到点杀 (我们是普通用户, 发不了
    信号给 root 进程, 见 with_sudo 的说明)。留足余量, 别把正常跑完的
    测试当成卡死杀掉 —— 那个误判会让一个好端端的盘被判 failed。
    """
    return max(30, int(duration_sec)) + 120


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

    ℹ️ **提权的例外在 GPU 项那边, 不在这里** —— glmark2 是全屏渲染, 那个 root
    进程会一直占着屏幕, 光靠 `timeout` 到点收尾意味着「按停止」之后窗口还留着
    (工人只能按 Esc 手动关)。GPU 项因此有自己的一条**提权**收尾路径
    (`sudo -n kill -<pgid>`, 以 root 身份整组收掉), 见
    `server/tests/gpu.py` 的「提权收尾」一节 —— 那是**用户点名授权**的
    单点例外, 范围仅限 GPU 项自己起的那一组。**这个通用函数保持不提权。**
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
