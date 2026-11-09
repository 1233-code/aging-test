"""GPU 渲染压测 - OpenGL ES 2 全屏渲染 (§6.3)

工具: glmark2 变体 --fullscreen —— **按当前图形会话自动选**(见下)

设计要点 (严格按 §6.3):
  - **前置条件缺任一 → 本项 error 并明确报错, 不做静默降级**
    ① HDMI 显示器已连接  /sys/class/drm/*HDMI*/status == connected
    ② 图形会话可用       按所选变体: Wayland 要 WAYLAND_DISPLAY, X11 要 DISPLAY
    ③ DRM 节点存在        /dev/dri/card* + /dev/dri/renderD*

## ⚠️ 为什么改成了「按会话自动选变体」

glmark2 有多个变体, **各要不同的图形会话**:

| 变体 | 提供包 | 需要的会话 | 库依赖 |
|---|---|---|---|
| `glmark2-es2-wayland` | `glmark2-es2-wayland` | `WAYLAND_DISPLAY` | libwayland-* |
| `glmark2-es2` | `glmark2-es2` | `DISPLAY` | **libX11** |

2026-10-22 实测发现的矛盾: §6.3 原文写「工具用 `glmark2-es2`」**同时**要求
「必须有 Wayland 会话」—— 但这两个是**互相排斥**的: `glmark2-es2` 是 X11 变体,
根本不用 Wayland。本机 `XDG_SESSION_TYPE=x11`、无 Wayland, 于是 GPU 项在
GPU 完全正常的板子上(run 41 / run 48)被自己的前置条件挡死, 一轮都没跑。

另有一处文档错误: 原文称 `glmark2-es2-wayland` 包「提供 `glmark2-es2` 可执行」——
**不对**, 该包提供的是 `/usr/bin/glmark2-es2-wayland`, 另一个文件
(md5 不同)。实测本机 `/usr/bin/glmark2-es2` 时间戳 2026-10-16、不属于任何
dpkg 包, 是手工放进去的, 重刷镜像即丢。

故改为: **探测实际会话 → 选对应变体 → 前置条件跟着该变体的真实需求走**。
开发板与产线机器会话类型不一致也不会挂。可用 `gpu.variant` 强制指定。
    产线常态接 HDMI, 因此不进入"未接显示器 → skipped"分支; 第 ① 条不满足
    属工位配置问题, 报 error 而非 skipped, 否则等于 GPU 静默漏测。
  - **通过判定** —— 每轮都完整跑完: exit code == 0 **且** 输出以
    `glmark2 Score: <数字>` 收尾 **且** **渲染出的场景数与清单一致**。
    第三条是 2026-10-26 加的: glmark2 跑到一半被掐断时**照样打印分数**
    (历史 run 124 只跑 2 个场景就出分 198, 报告上写着「GPU 通过」)。
    光看分数判断不出跑没跑全 —— 分数是加权平均, 漏掉的往往是分数低的重场景。
    场景集**显式点名** (`-b`, 见 gpu_benchmarks.py), 所以「该跑几个」我们自己
    知道, 跑少了就判得出来。
  - **三种「没跑完」分开判** (§9.3: 静默的「通过」比明确的「失败」有害得多)
        用户按「停止」        → stopped (主动中止, 不算失败)
        glmark2 自己退了      → failed  (这一轮没有结论)
        挂钟到点被 timeout 杀 → error   (没有结论, 且披露跑了多少)
        场景和清单对不上      → error   (清单过时了, 要重新标定)
  - **超时兜底** —— gpu.timeout_sec (config.yaml 里 240s, **单轮**上限而非整项
    上限), 超时即 error + 记 stderr 末 N 行, 防止个别机器跑飞挂死整个 run。
    这个值是**下限不是定值**: 实际生效的单轮上限 = max(配置值, 一轮预计时长 + 20s),
    免得把 scene_sec 调大后每一轮都被自己的兜底掐断 (§6.3 方案A 的参数联动)
  - **轮次 `gpu.rounds`** —— 默认 1 (= 原单轮行为, 出货判定口径不变), 可手动调大。
    每轮独立进程 / 独立挂钟 / 独立超时, 不共享上下文, 这样各轮分数才可比;
    任一轮失败即整项 failed 并记 `failed_round`。多轮时额外检测分数衰减趋势
    (末轮比首轮跌超 20% 就提示) —— 各轮都判通过但分数一路下滑, 单轮永远看不出来。
  - **单轮时长 `gpu.scene_sec`** —— §6.3 方案A: 场景集不变, 只把每个场景的
    渲染时长压到 3 秒 → 一轮 33 × 3s ≈ 102 秒 (原来跑默认套件是 336 秒)。
  - **收尾能自己关掉全屏窗口** —— glmark2 是 §11.2 提权起的 **root** 进程,
    服务(普通用户)对它发信号必然 EPERM, 所以「按停止」之后那个全屏窗口会
    一直占着屏幕到本轮超时上限 (工人看着像工具卡死)。现在起进程时用
    `start_new_session=True` 把这一组圈进**我们自己新建的**进程组, 收尾时
    以 root 身份整组收掉, 窗口立刻消失。授权范围与安全前提见下面
    「提权收尾」一节 —— 校验不过就绝不动手。
"""
import asyncio
import glob
import os
import re
import time
from typing import List, NamedTuple, Optional, Tuple

from .base import TestCase, TestContext, TestResult, register
from ._tools import (find_tool, kill_proc, run_cmd, sudo_hint_if_unavailable,
                     with_sudo)
from .gpu_benchmarks import (
    BENCHMARKS, build_bench_args, expected_identities, identity as scene_identity,
)

# glmark2 一轮理论 2-5 分钟 (§6.3), 驱动差异导致波动大, 故超时给到 600s
# (config.yaml 里的 gpu.timeout_sec 现在是 240s, 见下面「兜底下限」的说明)
_DEFAULT_TIMEOUT_SEC = 600

# 单轮超时兜底的**余量**: 实际生效的单轮上限 = max(配置值, 预计时长 + 这个数)。
# 为什么要有这个联动: scene_sec 在 UI 上能调到 60s (33×60 = 1980s), 配置里
# 写死的 240s 会把每一轮都掐断 —— 兜底反而成了主因。兜底的本意是「防跑飞」,
# 不是「卡时长」, 所以它不能比正常跑完所需的时间还短。
_TIMEOUT_MARGIN_SEC = 20

# 每个场景组合的渲染时长 (秒) —— §6.3 方案A
#
# 原来跑的是 glmark2 的**默认套件**: 每个场景 10 秒, 一轮 336 秒 (实测 run 57),
# 产线嫌长。方案A 把**场景集原样保留**, 只把每个场景的时长压到 3 秒:
# 33 个场景 × 3s ≈ 102 秒 ≈ 1 分 42 秒, 正好落在「一轮两分钟」里。
#
# 为什么不能直接跑默认套件再加个超时 (§6.3 曾经的方案C):
#   glmark2 跑到一半被掐断时**照样打印 `glmark2 Score:`** —— 历史 run 50 只跑了
#   26 个场景就出分 142, 跑满 33 个的 run 57 出分 148。**分数高的那个反而是
#   没跑全的那个**, 分数高低根本反映不出跑没跑全。所以场景必须**显式点名**,
#   「这一轮该跑几个」我们自己知道, 跑少了才判得出来。
_DEFAULT_SCENE_SEC = 3.0

# 每轮的**固定开销** (秒) —— 场景装载模型/纹理、编译着色器、创建上下文。
# glmark2 的 `duration` 只算**渲染**时间, 这些开销另算。
# 实测 (本机 A527 / Mali-G57, 33 个场景, scene_sec=3): 标称 99s, 实测 105.2s。
_ROUND_SETUP_SEC = 6.0

# 错误上下文保留的末尾行数
_TAIL_LINES = 20
# 指标表里**不存**的原始输出字段(它们归日志附录, 见 run() 里 rounds_detail
# 那段注释)。只挡已知的几个 —— 将来新增字段, 别忘了往这里加。
_DETAIL_RAW_KEYS = ("tail",)

# §10.1 推送频率硬约束: 静默阶段也要推心跳, ≤ 2s 一帧
_HEARTBEAT_SEC = 1.5

# 前置检查通过率 (进度条起点)
_PRECHECK_DONE_PCT = 5

# ── glmark2 输出解析 ─────────────────────────────────────────────
# 场景行 (glmark2 2021.02 的实际格式, 逐字节照抄):
#     [build] use-vbo=false: FPS: 199 FrameTime: 5.025 ms
#     [jellyfish] <default>: FPS: 110 FrameTime: 9.091 ms
#     [buffer] columns=200:interleave=false:update-dispersion=0.9:...: FPS: 91 ...
#
# 选项之间用**冒号**分隔, 值里可以带逗号和分号 (effect2d 的 kernel 就是
# "0,1,0;1,-4,1;0,1,0;")。所以分界取 ": FPS:" —— 选项名和值里不会出现 "FPS:"。
#
# ⚠️ 老代码数的是 `^Scene Number` —— **那行从来不存在** (是另一个工具的输出),
#    所以 scenes_completed / frames_rendered 一直是 0, 场景数从来没被数过。
_SCENE_RE = re.compile(
    r"^\[(?P<scene>[^\]]+)\]\s*(?P<opts>.*?):\s*FPS:\s*(?P<fps>[\d.]+)"
    r"\s+FrameTime:\s*(?P<ft>[\d.]+)\s*ms"
)

# 分数行: "glmark2 Score: 142"
_SCORE_RE = re.compile(r"glmark2 Score:\s*([\d.]+)")

# ── 提权收尾: 把「我们自己起的那一组进程」连 root 的那几个一起收掉 ──
#
# ⚠️ **授权范围 (2026-10-26, 用户点名授权)**:
#   允许本工具在**本机产线板子 (SN 31175c3d01850801f56 —— 工具就跑在这块
#   板子上)** 上用 `sudo -n kill -TERM/-KILL -<pgid>` 杀掉
#   **由本工具自己发起的、且组内进程命令行含 glmark2 路径的那一个进程组**。
#   **仅限 GPU 压测项调用** —— 所以这两个函数就放在 gpu.py 里, 不进 _tools.py
#   (放进去就等于其它测试项也能调, 超出授权范围)。其它测试项一律不碰。
#   任何一条校验不过 → **绝不动手**。
#
# 背景: §11.2 要求命令级 sudo, 于是 glmark2 这个子进程是 **root** 的, 而服务
# 是**普通用户**。POSIX 规定非 root 进程对 root 进程 os.kill 一律 EPERM ——
# 所以「工人按了停止」之后, 服务方**自己**收不掉那个全屏窗口, 只能等命令里
# 的 `timeout` 到点收尾, 窗口就一直占着屏幕到那一刻 (工人看着像工具卡死)。
# 以 root 身份把整组一起杀掉, root 就能给 root 进程发信号, 这一下就干净了。
#
# 真机实测的进程树 (2026-10-29 重新实测 —— **推翻 2026-10-26 那次的结论**;
# 注意 `timeout` 在 sudo **里面** —— 它是 root 的, 所以超时兜底本来就是 root
# 在收尾):
#     pid    pgid   sid     user    命令
#     21788  21788  21788   User    sudo -n env DISPLAY=… /usr/bin/timeout -k 5.0 305 \
#                                        /usr/bin/glmark2-es2 --fullscreen -b …
#     21794  21794  21788   root      timeout -k 5.0 305 /usr/bin/glmark2-es2 …
#     21795  21794  21788   root        glmark2-es2 --fullscreen -b …  ← 真正占屏的那个
#   ① 整棵树在**同一个会话**里 (我们用 start_new_session=True 建的) ——
#      **会话才是这棵树的可靠标识**;
#   ② ⚠️ **但不在同一个进程组**: GNU `timeout` 自己 `setpgid(0,0)` 另起了一组,
#      所以 glmark2 的 pgid (21794) **不等于** sudo 的 pgid (21788)。
#      2026-10-26 那次实测把 timeout/glmark2 的 pgid 记成了 167798(其实就是
#      sudo 的 pid), 于是得出了「整棵树在一个组里、按 sudo 的组杀就够」这个
#      **错误**结论 —— 实际打过去的是个空组, root 的 glmark2 根本收不到信号,
#      而工具还会报「窗口应已消失」。2026-10-29 修: 按**会话**找到真正跑
#      glmark2 的那一组, 杀**那一组**, 并用**同一组**验证它真的空了。
#   ③ 服务自己在另一个会话 —— 杀我们那个会话里的组不会误伤服务;
#   ④ 组里那个 root 的 glmark2, 普通用户对它发信号必然 EPERM,
#      「以 root 身份按组杀」才能一次收干净。

def _pgid_of(pid: int) -> Optional[int]:
    """取某进程的进程组 id (取不到就 None) —— 抽成函数是为了单测能替换。"""
    try:
        return os.getpgid(pid)
    except OSError:
        return None


def _sid_of(pid: int) -> Optional[int]:
    """取某进程的**会话 id** (取不到就 None) —— 抽成函数是为了单测能替换。

    会话才是「我们自己起的这棵进程树」的可靠标识 (2026-10-29): 进程组不行,
    因为 GNU `timeout` 会自己另起一组 (见模块顶部「真机实测的进程树」)。
    """
    try:
        return os.getsid(pid)
    except OSError:
        return None


def _own_pgid() -> int:
    """服务自己所在的进程组 id —— 绝不能拿它去整组杀 (会自杀)。"""
    try:
        return os.getpgrp()
    except OSError:
        return -1


def _scan_proc() -> List[Tuple[int, str, str, int, int, str]]:
    """扫 /proc, 把**所有活进程**列出来。

    返回 [(pid, comm, state, pgrp, session, cmdline), ...]。
    **僵尸 (state='Z') 不算** —— 僵尸已经不占 X 连接、不占屏幕, 只是还没被
    父进程收走, 没必要再杀。

    为什么扫 /proc 而不用 `os.killpg(pgid, 0)` 判断组还在不在:
    组里只要有一个 root 进程, 我们(普通用户)对那个组发信号就是 EPERM,
    `killpg(0)` 分不开「组空了」和「组里还剩个杀不动的」。/proc 扫一遍是实打实的。
    """
    out: List[Tuple[int, str, str, int, int, str]] = []
    try:
        entries = os.listdir("/proc")
    except OSError:
        return out
    for name in entries:
        if not name.isdigit():
            continue
        pid = int(name)
        try:
            with open(f"/proc/{pid}/stat", "rb") as f:
                data = f.read().decode("utf-8", "replace")
        except OSError:
            continue
        # comm 字段可能含空格和括号, 所以按**最后一个** ')' 切
        lp = data.find("(")
        rp = data.rfind(")")
        if lp < 0 or rp < lp:
            continue
        comm = data[lp + 1:rp]
        rest = data[rp + 1:].split()
        # stat 字段: pid (comm) state ppid pgrp session ...
        if len(rest) < 4:
            continue
        state = rest[0]
        try:
            pgrp = int(rest[2])
            sid = int(rest[3])
        except ValueError:
            continue
        if state == "Z":
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmdline = f.read().decode("utf-8", "replace").replace("\0", " ").strip()
        except OSError:
            cmdline = ""
        out.append((pid, comm, state, pgrp, sid, cmdline))
    return out


def _proc_group_members(pgid: int) -> List[Tuple[int, str, str, str]]:
    """列出进程组 `pgid` 里的**活**进程 → [(pid, comm, state, cmdline), ...]。"""
    return [(pid, comm, state, cmdline)
            for (pid, comm, state, pgrp, _sid, cmdline) in _scan_proc()
            if pgrp == pgid]


def _session_members(sid: int) -> List[Tuple[int, str, str, int, str]]:
    """列出**会话** `sid` 里的活进程 → [(pid, comm, state, pgrp, cmdline), ...]。

    ⚠️ **会话才是「我们自己起的这棵进程树」的唯一可靠标识** (2026-10-29 修):
    我们用 `start_new_session=True` 建了一个**新会话**, 里面只有我们自己的
    子孙进程 —— 别的进程进不来 (进会话要么 setsid 另开一个, 要么从会话内的
    进程 fork 出来)。所以「会话 id 相同」= 「是我们自己起的」, 而且顺带
    防掉了 pid 复用 (被复用的 pid 属于别的会话)。

    而**进程组不行** —— GNU `timeout` 会自己 `setpgid(0,0)` 另起一组, 所以
    「我们起的那个进程的组」里往往只有 `sudo` 一个, glmark2 在**另一组**里
    (实测见模块顶部「真机实测的进程树」)。
    """
    return [(pid, comm, state, pgrp, cmdline)
            for (pid, comm, state, pgrp, s, cmdline) in _scan_proc()
            if s == sid]


async def _sudo_kill_group(
    pgid: int,
    sig: str = "TERM",
    timeout_sec: float = 8.0,
    expect_cmdline: Optional[str] = None,
    session: Optional[int] = None,
) -> Tuple[bool, str]:
    """**以 root 身份** (`sudo -n kill`) 杀掉整个进程组 (见上面授权范围)。

    ⚠️ **安全前提, 少一条都不许执行**:
      ① `session` 必须是**我们自己用 start_new_session=True 建的那个会话 id**
         —— 组 id 会过期、会撞车, **会话 id 不会**: 会话里只有我们自己的
         子孙进程, 别人进不来 (要进来只能 setsid 另开一个, 或者从会话内的
         进程 fork 出来);
      ② `pgid` 不能是 0 / 1 (init 的组)、不能是**服务自己的组**
         (`os.getpgrp()`) —— 否则会连服务自己一起杀掉;
      ③ 组里必须**确实有**活进程;
      ④ **组里每个进程都必须属于 `session` 那个会话** —— 这一条专门防
         pid 复用: 组 id 过期后可能被系统分给毫不相干的新进程, 那种进程
         属于别的会话, 撞上了就绝不能动手;
      ⑤ 若指定了 `expect_cmdline`, 成员里还必须**至少有一个**命令行含这个
         字符串 —— 再叠一道「确实是我们在跑的那个东西」。

    任何一条不满足 → **什么都不做**, 返回 (False, 原因)。
    """
    if not isinstance(pgid, int) or pgid <= 1:
        return False, f"pgid {pgid!r} 不合法, 拒绝"
    if not isinstance(session, int) or session <= 1:
        return False, "没说是哪个会话的进程组, 拒绝 (会话才是防 pid 复用的依据)"
    own_group = _own_pgid()
    if pgid == own_group:
        return False, f"pgid {pgid} 是服务自己的进程组, 拒绝 (否则等于自杀)"

    members = [(pid, comm, state, pgrp, sid, cmdline)
               for (pid, comm, state, pgrp, sid, cmdline) in _scan_proc()
               if pgrp == pgid]
    if not members:
        return False, "进程组里已经没有活进程了, 不用杀"
    outsiders = [m for m in members if m[4] != session]
    if outsiders:
        names = ", ".join(f"{m[0]}({m[1]}, sid={m[4]})" for m in outsiders[:5])
        return False, (f"组里有不属于我们会话 {session} 的进程 ({names}) —— "
                       f"拒绝 (防 pid 复用撞车)")
    if expect_cmdline and not any(expect_cmdline in m[5] for m in members):
        names = ", ".join(f"{m[0]}({m[1]})" for m in members[:5])
        return False, (f"组里的进程 ({names}) 不是我们要杀的那个, 拒绝 "
                       f"(防 pid 复用撞车)")

    kill_bin = find_tool("kill") or "/bin/kill"
    cmd = with_sudo([kill_bin, f"-{sig}", f"-{pgid}"], timeout_sec=timeout_sec)
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except FileNotFoundError:
        return False, f"起不来 {kill_bin} (FileNotFoundError)"
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_sec + 3)
    except asyncio.TimeoutError:
        return False, f"`{' '.join(cmd)}` 没在 {timeout_sec + 3}s 内返回"
    text = (out or b"").decode("utf-8", "replace").strip()
    rc = proc.returncode if proc.returncode is not None else -1
    if rc != 0:
        return False, f"`sudo kill -{sig} -{pgid}` 退出码 {rc}: {text or '（无输出）'}"
    return True, f"已以 root 身份向进程组 {pgid} 发出 {sig} ({len(members)} 个进程)"


# ── glmark2 变体表 ────────────────────────────────────────────────
# 变体名: (二进制名, 该变体依赖的会话环境变量, 该变量的中文名)
# 理由见模块顶部「为什么改成了按会话自动选变体」
_VARIANTS = {
    "wayland": ("glmark2-es2-wayland", "WAYLAND_DISPLAY", "Wayland 会话"),
    "x11":     ("glmark2-es2",         "DISPLAY",         "X11 会话"),
}


def _pick_variant(pref: str = "auto") -> Tuple[Optional[str], Optional[str], str]:
    """按当前图形会话选 glmark2 变体。

    返回 (变体名, 二进制绝对路径 or None, 选它的理由)。变体名为 None 表示
    连二进制都没找到。

    auto 的判据: **谁的会话环境变量在, 且对应二进制装了, 就用谁**; 两个会话
    都在时优先 wayland(§6.3 的原始设计意图)。会话变量一个都没有时, 退而按
    「装了哪个二进制」选 —— 这样前置条件能报出具体缺什么, 而不是笼统地说
    「未找到工具」。
    """
    pref = (pref or "auto").strip().lower()
    if pref not in ("auto", "wayland", "x11"):
        pref = "auto"

    if pref != "auto":
        binname, envvar, label = _VARIANTS[pref]
        path = find_tool(binname)
        reason = f"配置 gpu.variant={pref} 强制指定"
        if path:
            reason += f", 按 {label} ({envvar}) 检查前置"
        return pref, path, reason

    for name, (binname, envvar, label) in _VARIANTS.items():
        val = os.environ.get(envvar, "").strip()
        path = find_tool(binname)
        if val and path:
            return name, path, f"{label}在 ({envvar}={val})"

    for name, (binname, _envvar, label) in _VARIANTS.items():
        path = find_tool(binname)
        if path:
            return name, path, (
                f"未检测到任何图形会话变量, 按已装的 {binname} 选 {label} —— "
                f"前置条件会指出具体缺哪个"
            )

    return None, None, "glmark2-es2 与 glmark2-es2-wayland 都没找到"


async def _precheck_display() -> Tuple[bool, str]:
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


async def _precheck_wayland() -> Tuple[bool, str]:
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


async def _precheck_dri() -> Tuple[bool, str]:
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


def _judge_round(
    *,
    rc: Optional[int],
    score: Optional[float],
    rendered: List[str],
    expected: List[str],
    cancelled: bool,
    timed_out: bool,
    timeout_sec: int,
    tag: str,
    out: str,
    tree_killed: Optional[bool] = None,
) -> Tuple[str, Optional[str]]:
    """一轮跑完之后的判定 —— 纯函数, 方便单测 (§9.3)。

    返回 (status, error)。status ∈ {passed, failed, error, stopped}。

    **几种「没跑完」必须分开判** (2026-10-26, 产线反馈「停掉的测试也记 pass」):

    | 情形                        | 判      | 依据                                  |
    |-----------------------------|---------|---------------------------------------|
    | 用户在页面上按了「停止」     | stopped | ctx.is_cancelled() —— 主动中止, 不算失败 |
    | glmark2 自己退了 (exit 0)   | stopped | 场景数是清单的**前缀** —— 人误关窗口/按 Esc 打断了(2026-10-29 改) |
    | **一个场景都没渲染出来**     | error   | 工具/环境没跑起来 —— 这一项没测成(2026-10-29 改) |
    | 渲染过场景后异常退出         | failed  | 板子真的崩过 —— 真证据, 不冤枉也不放过 |
    | 挂钟到点被 timeout 杀掉     | error   | 超时 —— 没有结论, 且要披露跑了多少     |

    以前「用户按停止」和「glmark2 自己退了」这两种都记成了 **passed** ——
    工人把全屏窗口关掉 / 按了 Esc, 工具照样报「GPU 通过」。历史 run
    50/52/54/56/57/59/78/100/112/120/122/123/124 全是这样: 整轮 passed、
    GPU 项 passed、GPU 轮 passed, rc=0, 但只渲染了 1~26 个场景 (应该 33 个)。

    ⚠️ **2026-10-29 再改两处判 `failed` → `error`/`stopped`**: 「工具没跑起来」
    和「人误关了窗口」以前都算在板子头上 —— 前者是环境问题、后者是人为打断,
    都不是设备不合格。判 failed = 冤枉被测设备(好板子被送去修), 这跟
    §9.3 的「不冤枉被测设备」直接冲突。
    """
    if cancelled:
        if tree_killed:
            window = ("✅ 已以 root 身份把那一组渲染进程整组收掉 —— "
                      "**全屏窗口应该已经消失**。")
        else:
            window = (f"⚠ glmark2 是**提权起的 root 进程**, 普通用户对它发不了信号 —— "
                      f"工具这次没能把它收掉 (提权收尾失败或未执行), "
                      f"**全屏窗口可能还占着屏幕直到本轮超时上限 ({timeout_sec}s)**。"
                      f"按 Esc 可以立刻关掉它。")
        return "stopped", (
            f"{tag}用户中途取消 (本轮不计入结论)。\n"
            f"{window}"
        )

    if timed_out:
        extra = ""
        if tree_killed:
            extra = "\n渲染进程已整组收掉 (全屏窗口应该已经消失)。"
        return "error", (
            f"{tag}glmark2 超过 {timeout_sec}s 未跑完一轮 (防跑飞挂死整个 run)。\n"
            f"超时前渲染了 {len(rendered)}/{len(expected) or '?'} 个场景 —— "
            f"**这一轮没有结论**。输出末尾:\n{out}{extra}"
        )

    # ── 工具根本没跑起来 → error (不是 failed) ──
    #
    # ⚠️ **一个场景都没渲染出来**, 说明 glmark2 压根没跑起来 (sudo 提权没成功 /
    # 二进制起不来 / 图形会话连不上)。这跟「板子不合格」是两回事:
    # 判 failed = **冤枉被测设备**(好板子被送去修); 判 error = 这一项没测成,
    # 该查环境 / 重测(§9.3)。所以这一条排在 `rc != 0` **前面** —— 后者
    # 判 failed 的前提是「它真的跑起来过、渲染过场景、然后崩了」(那是真证据)。
    if not rendered and score is None:
        return "error", (
            f"{tag}glmark2 **一个场景都没渲染出来**就退出了 (exit code = {rc}) —— "
            f"**工具/环境没跑起来, 这一项没测成**。\n"
            f"常见原因: sudo 提权没成功 / 图形会话连不上 / 二进制起不来。\n"
            f"**不是「板子不合格」** —— 不判 failed(§9.3: 不冤枉被测设备)。\n"
            f"输出末尾:\n{out}"
        )

    if rc != 0:
        return "failed", (
            f"{tag}glmark2 异常退出, exit code = {rc}。输出末尾:\n{out}"
        )

    if score is None:
        return "failed", (
            f"{tag}exit code 0 但输出里没有 `glmark2 Score:` —— 一轮未完整跑完。"
            f"输出末尾:\n{out}"
        )

    # ── 场景数对不上 → 这一轮没有结论 (§9.3) ──
    if expected:
        if len(rendered) < len(expected):
            missing = expected[len(rendered):]
            names = sorted({m.split("|")[0] for m in missing})
            if rendered == expected[:len(rendered)]:
                # 跑的是清单的**前缀** → 中途被打断
                #
                # ⚠️ 判 `stopped` 不判 `failed` (2026-10-29 定): exit code 0
                # + 场景数正好是清单的**前缀** = 「按顺序渲染到第 N 个时被外力
                # 打断」—— 产线上最常见的是**工人误关了全屏窗口 / 按了 Esc**。
                # 那是**人打断了测试, 不是板子不合格** —— 判 failed = 冤枉被测
                # 设备(好板子被送去修)。§9.3: stopped = 没跑完 ≠ 不合格,
                # 而且 stopped 的项**能重测**(§7.7.0)。
                return "stopped", (
                    f"{tag}glmark2 **提前退出了** (exit code 0), 只渲染了 "
                    f"{len(rendered)}/{len(expected)} 个场景 —— "
                    f"**这一轮没有结论**, 记 `stopped` (不是 failed)。\n"
                    f"没跑到的场景: {', '.join(names)}\n"
                    f"最常见的原因: 全屏渲染窗口被**手动关掉** / 按了 Esc "
                    f"中断了 glmark2 —— **人打断了测试, 不是板子不合格**, "
                    f"所以不判 failed。\n"
                    f"要主动中止请按页面上的「停止」按钮; 这一项也能重测 "
                    f"(§7.7.0: 没跑完的项补上料/复位之后就该能再测)。\n"
                    f"输出末尾:\n{out}"
                )
            return "error", (
                f"{tag}渲染出来的场景和内置清单对不上 (跑了 {len(rendered)} 个, "
                f"清单 {len(expected)} 个, 而且不是清单的前缀) —— "
                f"**不是「跑了一半」, 是清单过时了**。\n"
                f"本机 glmark2 的场景集与标定时不一致, 需要重新标定 "
                f"(见 server/tests/gpu_benchmarks.py)。\n"
                f"输出末尾:\n{out}"
            )
        if rendered != expected:
            return "error", (
                f"{tag}渲染出来的场景和内置清单不一致 (跑了 {len(rendered)} 个, "
                f"清单 {len(expected)} 个, 顺序或内容对不上) —— 清单过时了, "
                f"需要重新标定 (见 server/tests/gpu_benchmarks.py)。\n"
                f"输出末尾:\n{out}"
            )

    return "passed", None


# ══════════════════════════════════════════════════════════════════════
# X11 会话探测 —— 「会话能不能用」得**真连一次**, 不是看某个文件在不在
# ══════════════════════════════════════════════════════════════════════
#
# ⚠️ 2026-10-29 新板**第一轮** GPU 判 error 的真正原因(2026-11-02 查实):
#
#   当时的判据是「XAUTHORITY 指的那个文件存不存在」, 不存在就判「认证不过」。
#   但那台板子上(老板子实测, 同一套镜像):
#     · X server 是 `/usr/bin/X11/X -nolisten tcp -auth /var/run/slim.auth`
#       —— 真正在用的 cookie 是 **/var/run/slim.auth**(root 独占 0600)
#     · `~/.Xauthority` 是**另一份** cookie(不是 server 那份), 而且是厂商
#       那套 x11vnc/xhost 启动脚本**开机十来秒之后**才建的
#       ⇒ 第一轮跑的时候它压根还不存在
#     · **拿一个假 cookie、甚至完全不设 XAUTHORITY, xdpyinfo 都照样连上**
#       ⇒ 这个 X server 压根没强制 MIT cookie 认证
#
#   所以「找不到 cookie 文件」**不等于**「连不上」。拿路径存在与否当判据,
#   等于拿一个跟「会话能不能用」毫无关系的事实去否决一台好板子 —— 于是
#   GPU 项在好板子上判 error, glmark2 **一次都没起来过**(§6.3)。
#
#   改成: **真连一次**。有 xdpyinfo 就用 xdpyinfo 连(厂商 watchdog.sh 的
#   real_display_available() 也是这么干的), 连上了就算数。
#   顺带把 X server **自己命令行里的 -auth** 当成 cookie 的权威来源
#   (厂商那招), 免得再去猜 ~/.Xauthority —— 猜的那份可能是**错的**。

# X server 的可执行文件名 —— 取 /proc/<pid>/cmdline 的 argv[0] 的 basename
_XSERVER_NAMES = ("X", "Xorg", "Xorg-server", "Xwayland", "Xephyr", "Xdummy")


class _XAuth(NamedTuple):
    """X 认证文件(cookie)的探测结果。"""
    path: Optional[str]             # 找到的 cookie 路径(没找到就是 None)
    source: str                     # 判据说明(报告里原样显示)
    server_found: bool              # 有没有找到正在跑的 X server
    server_has_auth: bool           # X server 启动时带没带 -auth
    server_display: Optional[str]   # X server 自己声明的 display(没有就是 None)
    tried: List[str]                # 试过的路径(报错时列出来让人自己看)


def _xserver_argvs(proc_root: str = "/proc") -> List[List[str]]:
    """把正在跑的 X server 的 argv **逐个参数**抓出来。

    扫 `<proc_root>/<pid>/cmdline` —— 那是 NUL 分隔的**精确** argv(路径里有
    空格也不会被切开), 而且 `/proc/<pid>/cmdline` 是 0444, 普通用户也读得到。
    厂商 watchdog.sh 用的是 `ps -eo args=`(空格会被切开), 效果一样但粗。

    认 X server 的办法: argv[0] 的 basename 在 `_XSERVER_NAMES` 里。

    `proc_root` 是给测试用的 —— 指向一个假的 proc 根, 免得测到开发机上
    真跑着的那个 X server。
    """
    argvs: List[List[str]] = []
    try:
        pids = [p for p in os.listdir(proc_root) if p.isdigit()]
    except OSError:
        return argvs
    for pid in pids:
        try:
            with open(os.path.join(proc_root, pid, "cmdline"), "rb") as f:
                raw = f.read()
        except OSError:
            continue
        argv = [a.decode("utf-8", "replace") for a in raw.split(b"\0") if a]
        if not argv:
            continue
        if os.path.basename(argv[0]) in _XSERVER_NAMES:
            argvs.append(argv)
    return argvs


def _parse_xserver_argv(argv: List[str]) -> Tuple[Optional[str], Optional[str]]:
    """从 X server 的 argv 里抠出两样东西。

    返回 (`-auth` 指向的 cookie 路径, X server 自己声明的 display)。

    **没有** `-auth` ⇒ 这个 server 启动时压根没开访问控制 ⇒ 不需要 cookie。
    """
    auth: Optional[str] = None
    display: Optional[str] = None
    for i, arg in enumerate(argv):
        if arg == "-auth" and i + 1 < len(argv):
            auth = argv[i + 1]
        elif arg.startswith("-auth="):
            auth = arg.split("=", 1)[1]
        elif arg.startswith(":") and not arg.startswith("::"):
            display = arg
    return auth, display


def _pick_xserver(argvs: List[List[str]],
                  display: Optional[str]) -> Optional[List[str]]:
    """从若干个 X server 里挑出跟 DISPLAY 对得上的那一个。

    ⚠️ 这台板子上 X server 的 argv **没有** display 参数
    (`/usr/bin/X11/X -nolisten tcp -auth /var/run/slim.auth`) —— display 号
    是 X 自己按 `:0` 起的, 不写在命令行里。所以: 谁带 display 参数就按它
    对, 谁都不带就取第一个(板上只跑一个 X server)。
    """
    if not argvs:
        return None
    if display:
        want = display.split(".", 1)[0]
        for argv in argvs:
            _auth, tok = _parse_xserver_argv(argv)
            if tok and tok.split(".", 1)[0] == want:
                return argv
    return argvs[0]


def _discover_xauth(display: Optional[str] = None,
                    runtime_dir: Optional[str] = None,
                    home: Optional[str] = None,
                    argvs: Optional[List[List[str]]] = None,
                    proc_root: Optional[str] = None) -> _XAuth:
    """找 cookie —— **X server 自己命令行里的 `-auth` 最权威**。

    优先级:
      ① X server 的 `-auth <路径>` —— 这是 server 本尊在用的那份, 别人
         说什么都是二手的(2026-11-02 老板子实测: env 里的 ~/.Xauthority
         跟 server 的 /var/run/slim.auth 是**两份不同的** cookie)
      ② env 里的 XAUTHORITY
      ③ ~/.Xauthority
      ④ XDG_RUNTIME_DIR 及其 gdm/ 目录下的 X 认证文件

    候选里**真的存在**的那一份就是它; 一个都不存在就返回 path=None,
    但把试过的路径都带回来(报错时要让人自己看)。

    `argvs` / `proc_root` 可以从外面塞进来 —— 测试用, 免得测到本机真跑着
    的 X server。
    """
    if argvs is None:
        argvs = _xserver_argvs(proc_root or "/proc")
    picked = _pick_xserver(argvs, display)
    server_found = picked is not None
    server_auth: Optional[str] = None
    server_display: Optional[str] = None
    if picked is not None:
        server_auth, server_display = _parse_xserver_argv(picked)
    server_has_auth = bool(server_auth)

    tried: List[str] = []

    def _take(path: Optional[str], why: str) -> Optional[_XAuth]:
        if not path or path in tried:
            return None
        tried.append(path)
        if os.path.exists(path):
            return _XAuth(path, why, server_found, server_has_auth,
                          server_display, list(tried))
        return None

    # ① X server 自己的 -auth —— 最权威
    if server_auth:
        hit = _take(server_auth, "X server 自己命令行里的 -auth")
        if hit:
            return hit
    # ② env 里的 XAUTHORITY
    hit = _take(os.environ.get("XAUTHORITY", "").strip(), "env 里的 XAUTHORITY")
    if hit:
        return hit
    # ③ ~/.Xauthority
    if home is None:
        home = os.path.expanduser("~")
    if home:
        hit = _take(os.path.join(home, ".Xauthority"), "~/.Xauthority")
        if hit:
            return hit
    # ④ gdm / lightdm 把 cookie 放在 XDG_RUNTIME_DIR 底下(名字不一定叫 Xauthority)
    if runtime_dir is None:
        runtime_dir = (os.environ.get("XDG_RUNTIME_DIR", "").strip()
                       or f"/run/user/{os.getuid()}")
    if runtime_dir and os.path.isdir(runtime_dir):
        for d in (runtime_dir, os.path.join(runtime_dir, "gdm")):
            try:
                names = sorted(os.listdir(d))
            except OSError:
                continue
            for name in names:
                if "xauthority" in name.lower():
                    hit = _take(os.path.join(d, name), f"{d} 里的 {name}")
                    if hit:
                        return hit

    return _XAuth(None, "候选路径里没有一份存在的", server_found,
                  server_has_auth, server_display, list(tried))


def _build_session_env(info: Optional[_XAuth] = None) -> dict:
    """要透传给 sudo 的图形会话环境变量 (§11.2)。

    sudo 默认 env_reset 会清掉这些变量 —— 没有 WAYLAND_DISPLAY /
    XDG_RUNTIME_DIR, glmark2 在 root 环境下连不上 compositor 必挂。

    ⚠️ X11 的 XAUTHORITY **不能原样透传**: env 里那个路径常常指向一个
    不存在的文件(新板实测), 原样传下去 = 前置检查放行了、glmark2 照样
    连不上。这里换成**探测到的**那份(优先用 X server 自己 `-auth` 的),
    一个都探测不到就把这个不存在的路径**去掉** —— 传一个根本不存在的
    路径, 只会让报告里出现一个误导人的路径。
    """
    keys = ("WAYLAND_DISPLAY", "XDG_RUNTIME_DIR", "XDG_SESSION_TYPE",
            "DISPLAY", "XAUTHORITY", "XDG_SESSION_ID")
    env = {k: os.environ[k] for k in keys if os.environ.get(k)}
    if info is None:
        info = _discover_xauth(
            display=os.environ.get("DISPLAY", "").strip() or None)
    if info.path:
        env["XAUTHORITY"] = info.path
    else:
        env.pop("XAUTHORITY", None)
    return env


async def _probe_x11(display: str, env: dict) -> Tuple[Optional[bool], str]:
    """**真连一次** X server —— 「会话能不能用」的权威判据。

    返回 (True/False/None, 说明):
      True  —— 连上了, 会话可用
      False —— 连不上, 说明里带 X server 自己的原话
               (如 "No protocol specified" / "Authorization required")
      None  —— 本机没有探测工具, 判不了(调用方退回静态判断)

    **按 glmark2 实际的样子去连**: 同样提权、同样透传会话环境 ——
    「前置检查放行、实际却连不上」是最坑人的一种错。
    """
    for tool, args in (("xdpyinfo", ["-display", display]),
                       ("xset", ["-display", display, "q"])):
        path = find_tool(tool)
        if path is None:
            continue
        cmd = with_sudo([path, *args], env=env, timeout_sec=10)
        try:
            rc, out, err = await run_cmd(cmd, timeout=12)
        except Exception as e:      # 探测失败不该掀翻整个测试项
            return False, f"{tool} 探测出错: {e}"
        if rc == 0:
            return True, f"{tool} 实连成功({display})"
        lines = (err or out).strip().splitlines()
        tail = lines[0][:200] if lines else f"退出码 {rc}"
        return False, f"{tool} 连不上 {display}: {tail}"
    return None, "本机没有 xdpyinfo / xset, 探测不了"


async def _precheck_x11() -> Tuple[bool, str]:
    """前置 ② (x11 变体): X11 会话**真能用**。

    对应 `glmark2-es2` —— 该二进制 `ldd` 出来链的是 libX11, 不用 Wayland。

    判据是**实连一次**(xdpyinfo), 不是「某个 cookie 文件在不在」——
    理由见本节开头的注释(2026-10-29 新板第一轮就是这么被误判的)。
    拿不准的时候, 实连说了算。
    """
    display = os.environ.get("DISPLAY", "").strip()
    if not display:
        return False, (
            "DISPLAY 为空 —— 没有 X11 会话, glmark2-es2 拿不到 GL 上下文。"
            "若这台机器跑的是 Wayland 会话, 应改用 glmark2-es2-wayland"
            "(在 config.yaml 设 gpu.variant: wayland)"
        )

    disp_no = display.split(":", 1)[1].split(".", 1)[0] if ":" in display else "0"
    sock = f"/tmp/.X11-unix/X{disp_no}"
    detail = f"X11 会话在: DISPLAY={display}"
    if os.path.exists(sock):
        detail += f" (socket {sock})"

    info = _discover_xauth(display=display)
    env = _build_session_env(info)
    probed, why = await _probe_x11(display, env)
    if probed is True:
        tail = (f", XAUTHORITY={info.path}({info.source})" if info.path
                else ", 未开访问控制, 不需要 cookie")
        return True, detail + f", {why}" + tail
    if probed is False:
        hint = ""
        if info.path:
            hint = f"(用的 cookie: {info.path} —— {info.source})"
        elif info.tried:
            hint = "(试过的路径都不存在: " + ", ".join(info.tried) + ")"
        return False, f"{detail}, 但**实连失败**: {why} {hint}".strip()

    # 本机没有探测工具 ⇒ 退回静态判断
    if info.path:
        return True, detail + f", XAUTHORITY={info.path}({info.source})"
    # ⚠️ 顺序要紧: X server 自己**没带 -auth** ⇒ 它压根没开访问控制 ⇒ 不需要
    # cookie ⇒ env 里那个不存在的路径是**无害**的, 不能拿它判失败。
    if info.server_found and not info.server_has_auth:
        return True, (detail + ", X server 启动时没带 -auth ⇒ 没开访问控制, "
                            "不需要 cookie(⚠ 本机没有 xdpyinfo, 连不了实)")
    if info.server_found and info.server_has_auth:
        return False, (
            f"DISPLAY={display} 但 X server 的 cookie 文件不存在"
            f"(试过的: {', '.join(info.tried)}) —— 提权后的进程多半认证不过, "
            f"glmark2 会连不上。确认服务启动用户对该文件有读权限。"
        )
    env_path = os.environ.get("XAUTHORITY", "").strip()
    if env_path:
        return False, (
            f"DISPLAY={display} 但 XAUTHORITY={env_path} 不存在, 也没找到别的"
            f"X 认证文件({info.source}) —— 提权后的进程认证不过 X server, "
            f"glmark2 会连不上。确认服务启动用户对该文件有读权限。"
        )
    # env 里压根没有 XAUTHORITY —— 不一定坏: X server 未开访问控制时不需要 cookie
    return True, detail + ", ⚠ 找不到 X 认证文件(X server 若开访问控制会认证失败)"


async def _precheck_session(variant: str) -> Tuple[bool, str]:
    """前置 ②: 所选变体所需的图形会话可用。"""
    if variant == "x11":
        return await _precheck_x11()
    return await _precheck_wayland()


def _build_prechecks(variant: str) -> Tuple[Tuple[str, str, object], ...]:
    """按所选变体组装前置检查表 —— 顺序即报错时的展示顺序。

    ② 的标签和检查函数都跟着变体走: wayland 查 `WAYLAND_DISPLAY`,
    x11 查 `DISPLAY`。metric key 统一叫 `precheck_session`。

    检查函数都是 **async** —— X11 那一项要**实连一次** xdpyinfo 才判得了
    「会话能不能用」(2026-10-29 新板第一轮 GPU 误判的教训)。
    """
    _binname, envvar, label = _VARIANTS[variant]
    return (
        ("precheck_display", "HDMI 显示器", _precheck_display),
        ("precheck_session", f"{label} ({envvar})",
         lambda: _precheck_session(variant)),
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
        self.scene_ids: List[str] = []      # 每个渲染出来的场景的身份
        self._tail: List[str] = []
        # 本轮 glmark2 那棵进程树的**会话 id** (起进程时用 start_new_session
        # 新建的)。收尾时靠它认出「这棵树是我们自己起的」—— 然后按会话找到
        # **真正跑着 glmark2 的那一组**, 整组收掉 (组里那个 glmark2 是 root
        # 的, 我们普通用户直接发信号必然 EPERM)。
        #
        # ⚠️ **为什么记会话而不是记进程组** (2026-10-29): GNU `timeout` 会自己
        # `setpgid(0,0)` 另起一组, glmark2 在 timeout 那一组里, 和 sudo 那一组
        # **不是同一组** —— 原来按 sudo 的组杀, 打的是个空组, root 的 glmark2
        # 收不到信号, 而工具还照样报「窗口应已消失」(撒谎)。详见模块顶部。
        self.sid: Optional[int] = None
        # sudo 自己那一组的组 id —— 只在「还没等它把 glmark2 拉起来」时作兜底
        # (见 _kill_glmark_tree)。
        self.pgid: Optional[int] = None
        self._glmark_bin: str = ""        # 本轮用的 glmark2 二进制路径
        self._tree_killed: Optional[bool] = None   # 提权收尾的结果 (给提示语用)

    def expected_seconds(self, test_config: dict) -> Optional[float]:
        """本项预计多少秒 = 轮数 × (场景数 × 每场景时长 + 每轮固定开销)。

        2026-10-26 之前这一项返回 None —— 理由是「单轮时长由 GL 驱动决定,
        算不出来」。那是**没量过**, 不是算不出: 方案A 之后场景集和每个场景的
        时长都是我们自己定的, 预计时长就成了能算的数。

        实测 (本机 A527 / Mali-G57, 33 个场景, `scene_sec=3`): 标称 99s,
        实测 **105.2s** —— 差的 6s 是每轮装载模型/纹理、编译着色器的开销。
        这个值只给界面参考(§7.5), **不参与任何判定**。
        """
        t_cfg = test_config or {}
        scene_sec = float(t_cfg.get("scene_sec", _DEFAULT_SCENE_SEC))
        rounds = max(1, int(t_cfg.get("rounds", 1)))
        return rounds * (len(BENCHMARKS) * scene_sec + _ROUND_SETUP_SEC)

    async def cancel(self):
        if self.proc and self.proc.returncode is None and not self.cancel_done:
            self.cancel_done = True
            # ① 先杀我们自己起的这棵树的顶端 (`sudo` 进程, 我们的 uid, 杀得动)
            await kill_proc(self.proc, wait_sec=2.0)
            # ② 再**以 root 身份**把真正占着屏幕的那一组收掉 —— 组里那个
            #    glmark2 是 root 的, 普通用户对它发信号必然 EPERM (with_sudo
            #    的说明)。不收的话, 那个全屏窗口会一直占着屏幕到本轮超时上限,
            #    工人看着像工具卡死。
            #    授权范围见模块中部「提权收尾」—— 校验不过就绝不动手。
            self._tree_killed = await self._kill_glmark_tree("用户取消")

    def _glmark_left_alive(self) -> bool:
        """那个 root 的 glmark2 是不是还活着 (命令行以 glmark 二进制开头)。

        区分它和同组的 `timeout` / `sudo`: 那两个是我们自己起的、正规退出的
        壳子, 留着无所谓; 只有 glmark2 本体活着才说明全屏窗口还占着屏幕。

        ⚠️ **在会话里找, 不在进程组里找** (2026-10-29 修): 原来扫的是
        `self.pgid` (sudo 那一组) —— 而 glmark2 在 `timeout` 另起的那一组里,
        所以这个安全网**永远不可能触发**: root 的 glmark2 赖着不走时, 根本
        没人去收它, 全屏窗口就一直占着屏幕。
        """
        if not self.sid or not self._glmark_bin:
            return False
        bin_path = self._glmark_bin
        for _pid, _comm, _state, _pgrp, cmdline in _session_members(self.sid):
            if cmdline.startswith(bin_path):
                return True
        return False

    def _glmark_target_group(self) -> Optional[int]:
        """找出**真正跑着 glmark2 的那个进程组**的 pgid (找不到就 None)。

        ⚠️ **这里原来返回的是 `self.pgid`** —— 也就是 **sudo 那一组**。
        而 GNU `timeout` 自己 `setpgid(0,0)` 另起了一组, glmark2 在**那一组**
        里 (实测见模块顶部)。所以原来的「整组杀」打的是个**空组** —— root
        的 glmark2 收不到信号, 而工具还会报「窗口应已消失」。

        现在按**会话**找 (会话里只有我们自己起的子孙进程, 别人进不来):
          ① 命令行**以** glmark2 二进制开头的那一个 —— 那就是本体, 它在哪
             一组就杀哪一组;
          ② 次选: 命令行**含** glmark2 路径、但不是 sudo 自己的那一个 ——
             那就是 `timeout` 包装层, 它和 glmark2 在同一组, 而且它还在就
             说明 glmark2 还能被拉起来, 得收;
          ③ 都没有 → 还没起来 / 已经跑完了 → None。
        """
        if not self.sid or not self._glmark_bin:
            return None
        bin_path = self._glmark_bin
        members = _session_members(self.sid)
        # ① 本体: 命令行以 glmark2 二进制开头
        for _pid, _comm, _state, pgrp, cmdline in members:
            if cmdline.startswith(bin_path):
                return pgrp
        # ② timeout 包装层 (排除 sudo 自己 —— 它那一组是空壳)
        for pid, _comm, _state, pgrp, cmdline in members:
            if self.proc and pid == self.proc.pid:
                continue
            if bin_path in cmdline:
                return pgrp
        return None

    async def _adopt_session(self, ctx: TestContext, tag: str = "") -> Optional[int]:
        """记下这一轮**我们自己建的会话 id**, 并校验它确实是我们自己的。

        为什么按**会话**认这棵进程树 (2026-10-29 修): GNU `timeout` 会自己
        `setpgid(0,0)` 另起一组, glmark2 在 timeout 那一组里 —— 和 sudo 那一组
        **不是同一组**。而会话是 `start_new_session=True` 建的, 里面**只有
        我们自己的子孙进程** —— 别的进程进不来 (要进来只能 setsid 另开一个,
        或者从会话内的进程 fork 出来)。所以「会话 id 相同」= 「是我们自己
        起的」, 比「组 id 相同」强得多, 还顺带防掉了 pid 复用。

        返回 sid, 或 None —— 拿不准就不提权收尾:
        **宁可让窗口多留一会, 也不能杀错组**。
        """
        pid = self.proc.pid if self.proc else None
        if not pid:
            return None
        real_pgid = _pgid_of(pid)
        real_sid = _sid_of(pid)
        if real_pgid != pid or real_sid != pid:
            await ctx.on_log(
                f"[GPU] {tag}⚠ 进程没落在预期的独立会话里 "
                f"(pid={pid}, pgid={real_pgid}, sid={real_sid}) —— "
                f"收尾时不提权整组杀 (宁可窗口多留一会, 也不杀错组)"
            )
            return None
        if real_pgid == _own_pgid():
            await ctx.on_log(
                f"[GPU] {tag}⚠ 进程组与服务自己的组相同 —— 拒绝整组杀"
            )
            return None
        self.pgid = real_pgid
        await ctx.on_log(
            f"[GPU] {tag}会话: {real_sid} (收尾时按会话找到真正占屏的那个"
            f"进程组, 连 root 那个 glmark2 一起收掉)"
        )
        return real_sid

    async def _kill_glmark_tree(self, reason: str) -> bool:
        """以 root 身份把**真正占着屏幕的那一组**彻底收掉 (授权范围见模块中部)。

        返回 True = 那一组里已经没有活进程了 (窗口应该已经消失)。

        ⚠️ **2026-10-29 重写**: 原来杀的是 **sudo 那一组**(空的), root 的
        glmark2 收不到信号, 而这里还照样报「窗口已消失」—— 撒谎。现在:
          ① 先按**会话**找到真正跑 glmark2 的那一组 (见 _glmark_target_group);
          ② 杀**那一组**, 并用**同一组**验证它真的空了;
          ③ 收不掉就**如实说没收掉**, 绝不报「窗口应已消失」。
        """
        ctx = self.ctx
        if not self.sid:
            if ctx:
                await ctx.on_log(
                    f"[GPU] {reason}: 拿不到我们自己的会话 id —— 不提权收尾 "
                    f"(宁可窗口多留一会, 也不杀错组)"
                )
            return False
        # TERM → 等 → 不死就 KILL; 每一轮**重新发现**目标组
        for sig, wait_rounds in (("TERM", 10), ("KILL", 5)):
            target = self._glmark_target_group()
            if target is None:
                # 找不到跑着渲染进程的组 —— 但我们的 sudo 可能还活着, 它随时
                # 能把 glmark2 拉起来 (那就白收了) → 把 sudo 那一组也收掉。
                if self.proc and self.proc.returncode is None and self.pgid:
                    target = self.pgid
                else:
                    if ctx:
                        await ctx.on_log(
                            f"[GPU] {reason}: 已经找不到跑着的渲染进程, 无需收尾")
                    return True
            ok, detail = await _sudo_kill_group(
                target, sig, expect_cmdline="glmark2", session=self.sid)
            if not ok:
                if ctx:
                    await ctx.on_log(f"[GPU] {reason}: 整组收尾({sig})没成功 —— {detail}")
                return False
            if ctx:
                await ctx.on_log(f"[GPU] {reason}: {detail}")
            for _ in range(wait_rounds):
                await asyncio.sleep(0.2)
                if not _proc_group_members(target):
                    if ctx:
                        await ctx.on_log(
                            f"[GPU] {reason}: 进程组 {target} 已清空 —— "
                            f"全屏渲染进程不在了, 窗口应已消失")
                    return True
        # 两轮都没收干净 → 如实说
        left = self._glmark_left_alive()
        if ctx:
            await ctx.on_log(
                f"[GPU] {reason}: ⚠ 收尾没能收干净 —— "
                + ("glmark2 还活着 (全屏窗口可能还占着屏幕)"
                   if left else "已确认没有活着的渲染进程"))
        return not left

    async def _pump_stdout(self, ctx: TestContext, tag: str = ""):
        """读 glmark2 输出: 逐行推日志 + 数渲染了几个场景.

        glmark2-es2 是持续写 stdout 的 (每个场景一行), 所以这里用逐行读;
        但 §11.5 要求的"不阻塞在 readline"由外层的挂钟 + 心跳兜底保证。

        **场景数是这一项的判定依据** (§9.3: 静默的「通过」比明确的「失败」
        危害大得多) —— glmark2 跑到一半被掐断时**照样打印分数**, 光看分数
        看不出这一轮跑全了没有 (历史 run 50: 只跑 26 个场景出分 142;
        run 57: 跑满 33 个出分 148 —— 分数高的那个反而是没跑全的那个)。
        所以这里数的是**真的渲染出来的场景行**, 并记下每个场景的身份
        (场景名 + 选项串), 判定时用来核对「跑的是不是清单里的那些」。
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
            m = _SCENE_RE.match(line)
            if m:
                self.scenes += 1
                self.scene_ids.append(
                    scene_identity(m.group("scene"), m.group("opts"))
                )
            self._tail.append(line)
            del self._tail[:-_TAIL_LINES]  # 只留末尾 N 行做失败上下文
            await ctx.on_log(f"[GPU] {tag}{line}")

    async def _heartbeat(self, ctx: TestContext, t0: float, expect_sec: float,
                         scenes_total: int,
                         pct_lo: int = _PRECHECK_DONE_PCT, span: int = 95):
        """§10.1: 静默阶段也周期性推心跳, 不让用户怀疑工具死锁.

        进度按"本轮已用时 / 本轮预计时长"估算, 映射到整项的 [pct_lo, pct_lo+span)
        区间 —— 多轮时每轮各占一段, 不会每轮都从头冲到 90%。

        分母用**预计时长**而不是超时上限: 超时上限是防跑飞的兜底 (默认比一轮
        长得多), 拿它当分母的话条永远走不到头, 工人只看到条卡在 20%。
        """
        while True:
            await asyncio.sleep(_HEARTBEAT_SEC)
            elapsed = time.time() - t0
            # 留最后一点给收尾判定, 所以封到 span-10
            pct = pct_lo + min(max(span - 10, 1),
                               int(elapsed / max(expect_sec, 1.0) * (span - 10)))
            scene = ""
            if scenes_total:
                scene = f" · 场景 {self.scenes}/{scenes_total}"
            await ctx.on_progress(
                pct, f"全屏渲染中 · 已用 {elapsed:.0f}s / 预计 {expect_sec:.0f}s{scene}"
            )

    async def run(self, ctx: TestContext) -> TestResult:
        result = TestResult()
        # 存一份 ctx: runner 调 cancel() 时**不给** ctx, 但收尾(提权整组收掉
        # 那个 root 的 glmark2)要写日志说明。
        self.ctx = ctx
        t_cfg = ctx.test_config or {}
        configured_timeout = int(t_cfg.get("timeout_sec", _DEFAULT_TIMEOUT_SEC))
        rounds = max(1, int(t_cfg.get("rounds", 1)))   # §6.3: 默认 1 轮 = 原单轮行为

        # ── 变体选择 (§11.1: 禁 which, 用 find_tool) ──
        gpu_cfg = ctx.config.get("gpu", {}) or {}
        variant, glmark_bin, why = _pick_variant(
            t_cfg.get("variant", gpu_cfg.get("variant", "auto"))
        )
        if not glmark_bin:
            result.status = "error"
            result.error = (
                f"未找到 glmark2 (原因: {why}). 解决: "
                "sudo apt install glmark2-es2 glmark2-es2-wayland "
                "(两个变体都装上, 由本测试按当前会话自动选; "
                "见 docs/需求设计.md §11.7)"
            )
            await ctx.on_log(f"[GPU] ✗ {result.error}")
            return result
        result.metrics["glmark_variant"] = variant
        result.metrics["glmark_bin"] = glmark_bin
        await ctx.on_log(f"[GPU] 变体: {variant} —— {why}")
        await ctx.on_log(f"[GPU] 工具: {glmark_bin}")

        if not sudo_hint_if_unavailable(result, "GPU 渲染"):
            await ctx.on_log(f"[GPU] ✗ {result.error}")
            return result

        # ── 前置检查 (§6.3: 缺任一 → error, 不静默降级) ──
        await ctx.on_log("[GPU] 前置检查...")
        failed: List[str] = []
        for key, label, checker in _build_prechecks(variant):
            ok, detail = await checker()
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
        result.metrics["timeout_sec"] = configured_timeout
        result.metrics["rounds_detail"] = []
        await ctx.on_progress(_PRECHECK_DONE_PCT, "前置检查通过, 启动 glmark2-es2")

        # ── 方案A: 场景集不变, 每个场景压到 scene_sec (§6.3) ──
        # 场景**显式点名** (-b), 这样「这一轮该跑几个场景」我们自己知道,
        # 跑少了才判得出来 (以前跑默认套件, 跑一半被掐断照样记 passed)。
        scene_sec = float(
            t_cfg.get("scene_sec", gpu_cfg.get("scene_sec", _DEFAULT_SCENE_SEC))
        )
        bench_args = build_bench_args(scene_sec)
        expected = expected_identities()
        expect_sec = len(BENCHMARKS) * scene_sec + _ROUND_SETUP_SEC
        result.metrics["scene_sec"] = scene_sec
        result.metrics["scenes_expected"] = len(expected)
        result.metrics["expected_round_sec"] = round(expect_sec, 1)

        # 兜底下限: 单轮上限不能比「正常跑完这一轮」还短, 否则 scene_sec 调大后
        # 每一轮都被自己的兜底掐断 (兜底的本意是防跑飞, 不是卡时长)。
        timeout_sec = int(max(configured_timeout, expect_sec + _TIMEOUT_MARGIN_SEC))
        result.metrics["timeout_sec"] = timeout_sec
        await ctx.on_log(
            f"[GPU] 计划: 跑 {rounds} 轮, 每轮 {len(BENCHMARKS)} 个场景 × "
            f"{scene_sec:g}s = 预计 {expect_sec:.0f}s (单轮超时上限 {timeout_sec}s)"
        )
        if timeout_sec > configured_timeout:
            await ctx.on_log(
                f"[GPU] ⚠ 配的单轮超时上限 {configured_timeout}s 比这一轮的预计时长 "
                f"{expect_sec:.0f}s 还短, 已自动抬到 {timeout_sec}s "
                f"(兜底只防跑飞, 不卡正常时长)"
            )

        # ── 轮次循环: 每轮独立跑完整一轮, 任一轮失败即整项失败 ──
        # 多轮时每轮各占进度条的一段 (§10.1) —— 别每轮都从头冲到 90%
        span_total = 100 - _PRECHECK_DONE_PCT
        per = span_total / rounds
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
                pct_lo=_PRECHECK_DONE_PCT + int((idx - 1) * per),
                pct_hi=_PRECHECK_DONE_PCT + int(idx * per),
                scene_sec=scene_sec,
                bench_args=bench_args,
                expected=expected,
                expect_sec=expect_sec,
            )
            # ⚠️ **指标里只留量化字段, 原始输出不存**。
            #
            # 这一轮的结果字典里带着 "tail"(glmark2 输出的**末尾 20 行**,
            # 给自己留的失败上下文)。它进指标表有两个坏处:
            #   ① 报告里会被 repr 成**一个巨大的单元格**(整段输出, 一行几千
            #      字符, 比一页还高)→ 浏览器只能把这行推到下一页, 指标表跟
            #      前面的行**断开**(§8.2.2 的排版问题);
            #   ② 它是**截断副本** —— 完整的那份**每行都走 ctx.on_log**, 在
            #      报告末尾的「日志附录」里一字不少。同一份东西两处, 一处还是
            #      截断的: 读者会把它当成这一项的完整输出(§8.2「日志只有一处,
            #      残缺副本比没有更糟」)。
            # 所以这里只把量化字段(分数/场景数/耗时/退出码/状态)存进指标。
            result.metrics["rounds_detail"].append(
                {k: v for k, v in round_result.items() if k not in _DETAIL_RAW_KEYS}
            )
            # 各轮分数 —— 压成一个**标量**指标。多轮时一眼看得出衰减趋势,
            # 界面上也是个小方块, 不用去翻 JSON。
            result.metrics["rounds_scores"] = " / ".join(
                f"{d.get('score', 0):.0f}" for d in result.metrics["rounds_detail"]
            )

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
                # ⚠️ 这里**不要**再放 "stderr_tail" 之类的半截日志 ——
                #   报告的指标表是给「量化结论」看的, 往里塞一段截断过的
                #   日志, 纸面读者会把它当成这一项的完整输出。现在日志只有
                #   一处 —— 报告末尾的「日志附录」, 每一项的完整日志。
                return result

        # 全部轮次跑完 —— 额外报一次分数趋势 (rounds>1 才有意义)
        detail = result.metrics["rounds_detail"]
        result.status = "passed"
        await ctx.on_progress(100, f"完成 · {rounds} 轮全过")
        scores = " / ".join(f"{d['score']:.0f}" for d in detail)
        await ctx.on_log(
            f"[GPU] ✅ {rounds} 轮全部跑完 ({len(BENCHMARKS)} 个场景/轮), "
            f"各轮 score: {scores}"
        )
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
        scene_sec: float = _DEFAULT_SCENE_SEC,
        bench_args: Optional[List[str]] = None,
        expected: Optional[List[str]] = None,
        expect_sec: float = 0.0,
    ) -> dict:
        """跑**一轮**完整 glmark2, 返回本轮的指标字典 (§6.3)。

        每轮独立进程、独立挂钟、独立超时 —— 不共享任何上下文,
        这样第 2 轮的分数才和第 1 轮可比 (能看出衰减趋势)。
        """
        # 每轮重置 —— 上一轮的会话/收尾结果不能带进这一轮
        # (会话是**每轮新建**的, 收尾时按会话找到那一组再整组杀,
        #  见模块中部「提权收尾」)
        self.scenes = 0
        self.scene_ids = []
        self._tail = []
        self.cancel_done = False
        self.sid = None
        self.pgid = None
        self._glmark_bin = glmark_bin
        self._tree_killed = None
        # 轮次进度映射到整项的 [pct_lo, pct_hi] 区间
        span = max(1, pct_hi - pct_lo)
        bench_args = bench_args or []
        expected = expected or []

        # ── 构造命令 (§11.2 命令级 sudo -n; 会话环境变量必须透传) ──
        # **必须带 timeout_sec**: glmark2 在 GPU 挂死 / 驱动卡住时会一直不退出。
        # 它被提权成 root 进程后, 我们 (普通用户) 发不了信号去收它 —— 只能让
        # 同为 root 的 `timeout` 到点自己杀它, 否则测试任务永不结束, 连服务
        # 关机都会被拖住。这里 timeout 给得比内部判定宽裕几秒。
        #
        # `-b` 把每个场景**显式点名** (§6.3 方案A): 不点名就跑 glmark2 的默认
        # 套件, 那个套件跑一半被掐断时照样出分数, 我们连「该跑几个」都不知道。
        cmd = with_sudo([glmark_bin, "--fullscreen", *bench_args],
                        env=self._session_env(), timeout_sec=timeout_sec + 15)
        tag = f"第 {idx}/{total} 轮 · " if total > 1 else ""
        await ctx.on_log(f"[GPU] {tag}命令: {' '.join(cmd)}")
        await ctx.on_log(f"[GPU] {tag}单轮超时上限: {timeout_sec}s (超时即 error)")

        # ── 起进程 ──
        #
        # `start_new_session=True`: 给这棵进程树 (sudo → timeout → glmark2)
        # 建一个**属于我们自己的**新会话 (会话 id == 这个进程的 pid, 它同时
        # 也是自己那一组的组长)。
        # 为什么要自己建: 收尾时我们要以 root 身份**整组**杀掉它 (组里那个
        # glmark2 是 root 的, 普通用户对它发信号必然 EPERM, 见 with_sudo)。
        # 而**会话 id 必须可预期** —— 否则「按会话找那棵树」就可能找到别人的。
        # ⚠️ 注意: 建了会话**不等于**整棵树在一个进程组里 —— GNU `timeout`
        # 自己还会 `setpgid(0,0)` 另起一组 (实测见模块顶部), 所以收尾时
        # **不能**直接按 sudo 那一组杀, 得按会话找到真正跑 glmark2 的那一组。
        try:
            self.proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
        except FileNotFoundError:
            return {
                "round": idx, "status": "error", "score": 0.0, "scenes": 0,
                "elapsed_sec": 0.0, "exit_code": -1, "tail": "",
                "error": "启动 glmark2-es2 失败: FileNotFoundError",
            }
        self.sid = await self._adopt_session(ctx, tag)

        t0 = time.time()
        self._pump_task = asyncio.create_task(self._pump_stdout(ctx, tag))
        self._heartbeat_task = asyncio.create_task(
            self._heartbeat(ctx, t0, expect_sec or timeout_sec, len(expected),
                            pct_lo, span)
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

        # ── 安全网: 这个 root 的 glmark2 绝不能活过它那一轮 ──
        # 它要是还活着, 全屏窗口就一直占着屏幕 (下一轮也起不来), 而它是我们
        # 普通用户身份杀不掉的 (EPERM) —— 只能以 root 身份整组收掉。
        if self._glmark_left_alive():
            await self._kill_glmark_tree("本轮收尾")

        out = "\n".join(self._tail)
        rc = self.proc.returncode if self.proc.returncode is not None else -1
        score_m = _SCORE_RE.search(out)
        score = float(score_m.group(1)) if score_m else None

        base = {
            "round": idx,
            "score": score if score is not None else 0.0,
            "scenes": self.scenes,
            "scenes_expected": len(expected),
            "elapsed_sec": round(elapsed, 1),
            "exit_code": rc,
            "tail": out,
        }

        # ── 判定 (§9.3: 静默的「通过」比明确的「失败」有害得多) ──
        status, error = _judge_round(
            rc=rc, score=score, rendered=list(self.scene_ids),
            expected=expected, cancelled=cancelled, timed_out=timed_out,
            timeout_sec=timeout_sec, tag=tag, out=out,
            tree_killed=self._tree_killed,
        )
        if status != "passed":
            icon = {"stopped": "⏹", "error": "✗", "failed": "❌"}[status]
            await ctx.on_log(f"[GPU] {icon} {tag}判 {status}: {error}")
            return {**base, "status": status, "error": error}

        await ctx.on_log(
            f"[GPU] ✅ {tag}跑完: glmark2 Score: {score:.0f}, "
            f"{self.scenes}/{len(expected) or self.scenes} 个场景, "
            f"{elapsed:.0f}s, exit 0"
        )
        return {**base, "status": "passed", "score": score, "error": None}

    @staticmethod
    def _session_env() -> dict:
        """要透传给 sudo 的图形会话环境变量 (§11.2) —— 见 `_build_session_env`。

        cookie 那一份是**探测到的**(优先 X server 自己 `-auth` 的), 不是
        env 里那个常常指向不存在文件的路径 —— 见 `_build_session_env`。
        """
        return _build_session_env()