"""测试共享工具: 找二进制 / 跑子进程.

解决 PATH 不含 /sbin 的问题 (普通用户跑 uvicorn 时找不到 ethtool/iw/...).
"""
import asyncio
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import Awaitable, Callable, List, NamedTuple, Optional, Tuple

from ..config import Config


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

class ProcOutcome(NamedTuple):
    """run_proc_with_progress 的结果。

    `reason` 说明「**为什么停下来的**」—— 这一条是 2026-10-28 那次事故的
    产物: 以前只回一个 `killed: bool`, 于是「用户按了停止」和「设备卡死
    超时」被混成同一句 RuntimeError, 工人自己按的暂停在报告里变成了
    「失败」, 整轮也跟着判 failed。

      · ""            —— 进程自己跑完
      · "cancelled"   —— 用户中途按了「停止」
      · "hard_limit"  —— 到了我们自己的硬上限, 我们主动收的
    """
    rc: int
    text: str
    killed: bool
    reason: str = ""


async def run_proc_with_progress(
    proc: asyncio.subprocess.Process,
    duration_sec: float,
    ctx=None,
    on_tick: Optional[Callable[[float], Awaitable[None]]] = None,
    tick_sec: float = 1.0,
    hard_limit_sec: Optional[float] = None,
) -> ProcOutcome:
    """按挂钟等子进程自然退出, 期间每 tick_sec 回调一次。返回 ProcOutcome。

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
    reason = ""
    while True:
        done, _ = await asyncio.wait({wait_task}, timeout=tick_sec)
        if done:
            break
        elapsed = time.monotonic() - t0

        # 硬上限: 超了就我们自己杀 (正常情况下 with_sudo() 里的 `timeout`
        # 会先到点, 走不到这里; 这道是给 `timeout` 自己都不在了的场合兜底)
        if hard_limit_sec is not None and elapsed > hard_limit_sec:
            killed = True
            reason = "hard_limit"
            await kill_proc(proc, wait_sec=3.0, ctx=ctx)
            break

        if ctx is not None and await ctx.is_cancelled():
            killed = True
            reason = "cancelled"
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
    return ProcOutcome(rc, "\n".join(lines), killed, reason)


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


def loader_error_message(rc: int, text: str) -> str:
    """工具**起不来**(不是「跑了但失败」)时, 给人话; 不是这类问题返回 ""。

    ⚠️ **为什么非把它跟「跑失败」分开**(2026-11-06 实测):
    U 盘版里 fio 是 wrapper, 靠 LD_LIBRARY_PATH 找盘里的 .so。依赖没带齐时
    它是 rc=127 + `error while loading shared libraries` ⇒ 报告里写
    「fio write 失败」⇒ **看着像板子坏了 ⇒ 白白返修一台好板子**。
    真实原因: 盘里 fio 的依赖没打全(那次是缺 `libceph-common.so.2`)。

    所以这类要单独说一句「**工具起不来, 不是板子坏了**」。
    """
    if rc != 127 and "error while loading shared libraries" not in (text or ""):
        return ""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    detail = ""
    for ln in lines:
        if "error while loading shared libraries" in ln:
            detail = ln
            break
    if not detail:
        detail = lines[-1] if lines else f"rc={rc}"
    # 只要出得去缺失的库名, 工人/排障的人一眼就知道该补什么
    # (报错行长这样: `<binary>: error while loading shared libraries:
    #  libceph-common.so.2: cannot open shared object file: ...`
    #  ⇒ 库名是第 3 段, 不是第 1 段 —— 第 1 段是二进制自己的路径)
    lib = ""
    for part in detail.split(": "):
        part = part.strip()
        if ".so" in part and " " not in part:
            lib = part
            break
    return (
        "工具起不来(缺共享库), 不是板子坏了 —— "
        + (f"缺 {lib}: " if lib else "")
        + detail
        + "。U 盘版的工具靠盘里的 .so 运行 ⇒ 这根盘的工具没带全, 换一根完整的盘重跑。"
    )


def _code_root() -> Path:
    """代码根(仓库根 / 盘根)—— 会跟着 U 盘挂载点漂移走。

    ⚠️ **单独拎出来一个函数**, 是为了让测试能把它**换掉**(测「标记文件在
    哪」时不必去动真仓库)—— 见 `tests/test_deps_fix_hint.py`。
    """
    return Config.project_root()


def usb_mode() -> bool:
    """这台机器上跑的是**U 盘版**吗? —— 判据跟 shell 脚本**是同一个**。

    ⚠️ **2026-11-10 修**: 以前 Python 侧只认 `AGING_TOOL_DIR` 有没有值,
    而 `scripts/check_deps.sh` / `check_eth.sh` 认的是**盘根目录的
    `AGING_USB_MODE` 标记文件** ⇒ 两个判据**不是同一个** ⇒ 它们能
    同时对不上:
      · `start.sh:37` 那行是**有条件**的 ——
        `[ -d "$SCRIPT_DIR/tools/bin" ] && export AGING_TOOL_DIR=...`
        ⇒ 盘上有标记、但 `tools/bin` 不在时, **环境变量不导出**
        ⇒ Python 以为自己是**设备版** ⇒ 又把
        「sudo bash scripts/install_deps.sh」端给工人
        ⇒ **而盘上根本没有那个脚本**(打包脚本只带运行时+诊断脚本)
        ⇒ 刚修掉的那个 P0 在另一种配置下**原样复活**。

    ⇒ 现在两个判据**取或**: 环境变量在、**或**代码根有标记文件。
    """
    if os.environ.get(TOOL_DIR_ENV, "").strip():
        return True
    try:
        root = _code_root()
        return bool(root and (Path(root) / "AGING_USB_MODE").is_file())
    except OSError:
        return False


def deps_fix_hint() -> str:
    """缺工具/装不上时, 给工人的**补救办法**(两种形态不一样)。

    ⚠️ **2026-11-10 修的真缺陷**: 以前各处都把
    「sudo bash scripts/install_deps.sh」**写死**在报错里 ⇒ 但
    **U 盘版的盘上根本没有这个脚本**(打包脚本只带运行时+诊断脚本,
    `install_deps.sh` 属于「打包/部署类」,**不带**)⇒ 工人/排障的人
    照着做 ⇒ 「No such file or directory」⇒ **补救办法是死的**。

    ⇒ **两种形态分开说**:
      · **U 盘版**(`usb_mode()` 为真): 盘上自带
        fio / glmark2-es2, 其余工具用板子出厂那份 ⇒ 缺工具或工具起不来
        **说明这版出厂镜像没带齐** ⇒ 该改用设备版(deb)装。
        ⚠️ **别再让工人去 apt 装**: ① 产线网络取不到公网;
        ② 装到板子上拔盘就走, 白装。
      · **设备版**(deb 装的, 脚本在安装目录里): 给命令。

    ⚠️ **设备版的文案一个字没改** —— deb 包里**确实带着**
    `scripts/install_deps.sh`(build_deb.sh 的白名单里有它)⇒ 命令有效。

    ⚠️ **这句文案全仓只此一份** —— `server/main.py` 的 `/api/config`
    也用这个函数(以前那边**另抄了一份**, 改一处忘一处 ⇒ 页面和报错
    说的不是同一句话)。
    """
    if usb_mode():
        return ("U 盘版: 盘上自带 fio / glmark2-es2, 其余工具用板子出厂那份 ⇒ "
                "缺工具或工具起不来, 说明这版出厂镜像没带齐, 请改用设备版(deb)安装")
    return "请运行 sudo bash scripts/install_deps.sh"


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


#: **U 盘版应用**: 随包工具目录(环境变量名)。
#:
#: 「应用装在 U 盘上跑」时, `fio` / `glmark2-es2` 这些**不是**装在板子上的
#: —— 它们在 U 盘的 `tools/` 里。`start.sh` 把这个目录指出来, `find_tool()`
#: 就先去那儿找。
#:
#: 目录长这样(由 `scripts/build_usb_image.sh` 生成):
#:     <U 盘>/tools/bin/<名字>      ← wrapper(设好 LD_LIBRARY_PATH 再 exec)
#:     <U 盘>/tools/lib/…           ← 那 10 个运行时库(.so)
#:     <U 盘>/tools/libexec/<名字>.bin ← 真二进制
#:     <U 盘>/tools/share/glmark2/  ← glmark2 的场景数据
#:
#: ⚠️ **没设这个变量时行为一个字不变** —— 设备版(deb 装)走的是系统 PATH。
TOOL_DIR_ENV = "AGING_TOOL_DIR"


def tool_dir() -> Optional[str]:
    """随包工具目录(`AGING_TOOL_DIR`), 没设 → None。

    ⚠️ U 盘挂载点漂移时这个目录会**跟着盘换位置**(`/media/User/AGING/tools`
    → `/media/User/AGING1/tools`)⇒ 按**设备**重新绑定(见 `Config.rebind`)。
    """
    d = os.environ.get(TOOL_DIR_ENV, "").strip()
    if not d:
        return None
    return str(Config.rebind(Path(d)))


def find_tool(name: str) -> Optional[str]:
    """在 **随包工具目录** + PATH + /sbin + /usr/sbin + /usr/local/sbin 找可执行.

    返回绝对路径或 None.

    ⚠️ **随包工具目录排在最前面** —— 「U 盘版」时盘上那份才是这一版的工具,
    板子上碰巧也装了一份(旧版)也不该用它。
    """
    d = tool_dir()
    if d:
        cand = os.path.join(d, "bin", name)
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    p = shutil.which(name)
    if p:
        return p
    for dd in ("/usr/local/sbin", "/usr/sbin", "/sbin", "/usr/local/bin"):
        full = os.path.join(dd, name)
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


# ── 收尾: 删文件 (走子进程) / 找出没收干净的进程 ──────────────────

async def remove_file_async(path: str, timeout: float = 10.0) -> bool:
    """删掉一个文件 —— **走子进程**, 绝不在调用者的线程里直接 unlink。

    为什么不能直接 `os.unlink(path)`:
      卡一旦挂死(设备对读写没有应答), unlink 会卡在内核的写信号量上
      **永远不返回**(D 态, 不可中断)。而这个调用是**同步**的, 跑在
      asyncio 的事件循环线程里 ⇒ 整个事件循环就此冻结 ⇒ 整个服务死掉。

      2026-10-28 实测: 主线程死在
      `unlinkat → vfs_unlink → rwsem_down_write_slowpath`, uvicorn 从此
      不再响应任何请求(`/api/health 都超时), 只能拔卡复位才缓过来。

    走子进程的话, 卡死的是**那个子进程**(而且它会被 timeout 收掉),
    事件循环照跑, 服务活着 —— 卡坏了顶多这一项判不合格。

    删不掉(文件归 root, 因为 fio 是被提权起的)时再用 `sudo -n rm -f`
    补一次 —— 跟老逻辑一致, 只是**全程没有同步 I/O**。

    返回 True = 删掉了(或本来就没有)。
    """
    if not path:
        return True
    # 先用普通用户删一次(文件是 root 的话会失败, 那再走 sudo)
    rc, _out, err = await run_cmd(["rm", "-f", path], timeout=timeout)
    if rc == 0:
        return True
    rc2, _out2, err2 = await run_cmd(
        with_sudo(["rm", "-f", path]), timeout=timeout)
    if rc2 == 0:
        return True
    # 两个都没删掉 —— 但**不能**因此把整项判失败: 卡挂死时删不掉是常态,
    # 真正要报的是「设备无响应」, 不是「文件删不掉」。
    return False


def leftover_fio_pids(marker: str) -> List[int]:
    """找出**还活着的** fio 进程(命令行里带 marker 的), 返回 PID 列表。

    为什么需要它: 设备卡死时, fio 会卡在内核的不可中断状态(D 态) ——
    **任何信号都杀不掉它**(SIGTERM/SIGKILL 都被内核挂起), 连同为 root 的
    `timeout` 到点也收不掉它。于是它变成一个孤儿进程(PPID=1), 继续往
    那张死卡上写。

    工具必须**如实告诉操作员**「这几个进程没收掉」, 否则人会以为工具已经
    清理干净了, 拔卡重插时那些进程还在写 —— 卡永远缓不过来。
    """
    out: List[int] = []
    try:
        pids = os.listdir("/proc")
    except OSError:
        return out
    for pid in pids:
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmd = f.read().replace(b"\0", b" ").decode(errors="replace")
        except OSError:
            continue
        if "fio" in cmd and marker and marker in cmd:
            out.append(int(pid))
    return sorted(out)


def sudo_available() -> bool:
    """sudo 是否**真的**可用 (§11.2 的 `sudo -n true` 自检项)。

    ⚠️ 2026-10-27: 以前这里只 `find_tool("sudo") is not None` —— 那只说明
    **装了 sudo 二进制**, 不说明**配了免密**。sudo 在、但没配 NOPASSWD 时,
    这儿照样返回 True 放行, 于是运行期 `sudo -n <cmd>` 才失败, 那时该项已经
    判成 **failed**(设备不合格)而不是 **error**(环境配置问题) —— 工人会把
    产线的权限配置的锅算到板子上。

    现在真跑一次 `sudo -n true`:
      · 退出码 0 → 免密可用
      · 非 0 / 找不到 / 超时 → 不可用
    开销 ~10ms, 一项测一次, 可以接受。
    """
    sudo_bin = find_tool("sudo")
    if not sudo_bin:
        return False
    try:
        r = subprocess.run(
            [sudo_bin, "-n", "true"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=10,
        )
        return r.returncode == 0
    except Exception:
        return False


# §11.2: sudo 在、但**没配免密**时的特征报错。
# 这些串是 sudo 自己吐的, 一条一条对过:
#   "sudo: a password is required"                    ← -n 模式下没免密
#   "sudo: a terminal is required to read the password"
#   "sudo: no tty present and no askpass program specified"
_SUDO_PASSWORD_ERRORS = (
    "a password is required",
    "a terminal is required",
    "no tty present",
    "sorry, try again",
)


def is_sudo_password_error(text: str) -> bool:
    """这段报错是不是「sudo 要密码」(= 没配 NOPASSWD)?"""
    if not text:
        return False
    low = text.lower()
    return any(s in low for s in _SUDO_PASSWORD_ERRORS)


# ── §11.2 补充: sudo **不许执行这条命令** 时的特征报错 ──
#
#   "sudo: user aging is not allowed to execute '/usr/bin/timeout'"
#
# ⚠️ **跟「要密码」是两回事** —— 免密是配了的, 但 sudoers 的命令白名单里
#   **没有这一条**。症状一样是「命令跑不起来」, 药方完全不同:
#     · 要密码      → 补 NOPASSWD;
#     · 不许执行    → 补**这条命令**的白名单。
#
# ⚠️ **被拒的会是 `/usr/bin/timeout`(或 `/usr/bin/env`), 一般不会是盘上的工具** ——
#   `with_sudo()` 一律先包一层 timeout(§11.2 的「服务不提权, 只提权单条命令」),
#   而 **sudo 只认最外层那条命令**, 后面的都是它的参数:
#     sudo -n /usr/bin/timeout -k 5 60 /mnt/aging/tools/bin/fio --name=...
#                    ^^^^^^^^^^^^^^^ sudo 授权的是这个; 盘上的 fio 只是参数
#   ⇒ 「U 盘版」**不需要**在产线的 sudoers 里额外放行盘上的工具路径:
#     设备版和 U 盘版被授权的命令**完全一样** ⇒ 现有的白名单继续有效。
#   (2026-11-05 实测: 两种模式下 fio/glmark2/memtester 的 sudo 命令逐项比对,
#    被授权的那条命令一模一样 —— 见 tests/test_usb_app_mode.py 的
#    「sudo 被授权的永远是系统路径」那个测试, 它是这条结论的绊线。)
#
# ⚠️ 不认它的话, 这一项会判 **failed**(工人眼里 = 板子坏了, 送修)——
#   实际是产线的权限配置漏了这条命令。§11.2: 报错口径不能混。
_SUDO_NOT_ALLOWED_ERRORS = (
    "not allowed to execute",
)


def is_sudo_not_allowed_error(text: str) -> bool:
    """这段报错是不是「sudo 不许执行这条命令」(白名单里没有)?"""
    if not text:
        return False
    low = text.lower()
    return any(s in low for s in _SUDO_NOT_ALLOWED_ERRORS)


def sudo_not_allowed_hint(text: str = "") -> str:
    """统一文案: sudoers 白名单里没有这条命令 —— 产线环境配置问题。"""
    tail = f" 原始报错: {text.strip()[:200]}" if text else ""
    return (
        "sudoers 的免密白名单里没有这条命令(免密是配了的, 但没覆盖到它)。"
        "这是**产线环境配置问题**, 不是被测设备不合格 —— 请产线管理员把"
        "这条命令加进 sudoers 的 NOPASSWD 名单。" + tail
    )


def sudo_nopasswd_hint(text: str = "") -> str:
    """统一文案: 免密没配 —— 这是**产线环境配置问题**, 不是设备不合格。

    报错口径不能混: 「failed」在工人眼里是「板子坏了, 送修」; 「error」才
    是「这一项没测成, 查环境」。sudo 要密码属于后者。
    """
    tail = f" 原始报错: {text.strip()[:200]}" if text else ""
    return (
        "sudo 需免密配置(NOPASSWD): 运行 `sudo -n <命令>` 时 sudo 要密码。"
        "这是**产线环境配置问题**, 不是被测设备不合格 —— 请联系产线管理员"
        "给服务账号配好 NOPASSWD 后重测 (见 docs/需求设计.md §11.2)。" + tail
    )


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
