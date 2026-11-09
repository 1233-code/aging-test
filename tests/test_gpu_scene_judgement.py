"""回归测试: GPU 压测的判定必须看**场景数**, 不能只看分数。

起因 (2026-10-26): 产线反馈「有很多次是我主动暂停的测试」, 查历史库发现
**那些被掐断的测试全都被记成了 passed**:

    run   整轮状态   GPU项状态  GPU轮状态  分数   实际渲染的场景数
    50    passed     passed     passed     142    26/33  ← 少跑了 7 个
    52    passed     passed     passed     195     1/33
    54    running    passed     passed     165     1/33
    57    passed     passed     passed     148    33/33  ← 跑全了
    124   passed     passed     passed     198     2/33

根因: glmark2 跑到一半被掐断时**照样打印 `glmark2 Score:`** —— 分数是各场景
的加权平均, 漏掉的往往正是分数低的重场景, 所以**分数高的那个反而是没跑全
的那个**。而 GPU 项的通过判定只看「exit code == 0 且输出里有分数」, 于是
「被人按了 Esc / 关了窗口」的半截测试, 报告上写着「GPU 通过」。

而且老代码数场景数的那行是**死的** (`^Scene Number` —— glmark2 从来不打印
这行), 所以 scenes_completed 一直是 0, 就算想判也判不了。

改法 (server/tests/gpu.py):
  · 数**真的渲染出来的场景行** ([场景] <选项>: FPS: N FrameTime: X ms);
  · 场景集**显式点名** (-b), 「该跑几个」我们自己知道;
  · 判定分开几种「没跑完」:
        用户按「停止」        → stopped (主动中止, 不算失败)
        glmark2 自己退了      → stopped (场景数是清单的**前缀** = 人误关了窗口)
        一个场景都没渲染出来   → error   (工具/环境没跑起来, 这一项没测成)
        渲染过之后异常退出      → failed  (板子真的崩过 —— 真证据)
        挂钟到点被 timeout 杀 → error   (没有结论, 且披露跑了多少)
    清单和实际对不上 (不是前缀) → error (清单过时了, 要重新标定)

⚠️ 2026-10-29 再改两处判 `failed` → `error` / `stopped`: 「工具没跑起来」
   和「人误关了全屏窗口」以前都算在板子头上 —— 前者是环境问题、后者是人为
   打断, 都不是设备不合格。判 failed = 冤枉被测设备(好板子被送去修)。

跑法: .venv/bin/python tests/test_gpu_scene_judgement.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.tests import gpu
from server.tests import gpu_benchmarks as gb

FAILS = []
PASSES = 0


def check(cond, msg, extra=""):
    global PASSES
    if cond:
        PASSES += 1
        print("    ✓ " + msg)
    else:
        FAILS.append(msg)
        print("    ✗ " + msg + ("  → " + str(extra) if extra else ""))


# ── 真机 glmark2 2021.02 的输出行 (逐字节照抄) ──────────────────
LINES_FULL = [
    "[build] use-vbo=false: FPS: 199 FrameTime: 5.025 ms",
    "[build] use-vbo=true: FPS: 198 FrameTime: 5.051 ms",
    "[texture] texture-filter=nearest: FPS: 1188 FrameTime: 0.842 ms",
    "[jellyfish] <default>: FPS: 110 FrameTime: 9.091 ms",
    "[terrain] <default>: FPS: 8 FrameTime: 125.000 ms",
]
SCORE_BANNER = [
    "=======================================================",
    "                                  glmark2 Score: 198",
    "=======================================================",
]


def full_scene_lines():
    """用清单里的身份反推一份「跑全了」的输出行。"""
    lines = []
    for scene, opts in gb.BENCHMARKS:
        o = "" if opts == "<default>" else opts
        lines.append(f"[{scene}] {o}: FPS: 100 FrameTime: 10.000 ms")
    return lines + SCORE_BANNER


# ── 假进程 / 假上下文 ──────────────────────────────────────────

class FakeProc:
    """按顺序吐行的假 glmark2。

    最后一行送完**之后**才把 returncode 设上 —— 这样读端一定先把分数行
    处理掉了, 收尾时取消读任务不会把分数弄丢 (真进程也是先写完再退出的)。

    hang=True: 吐完行**不退出**, 读端一直挂着 —— 模拟「进程还在跑」,
    这样「按停止」的收尾路径才会真的被执行 (否则 returncode 已置 0,
    cancel() 的守卫会直接跳过, 收尾根本没跑过)。
    """

    def __init__(self, lines, rc=0, hang=False):
        self._lines = list(lines)
        self.stdout = self
        self.returncode = None
        self._rc = rc
        self._hang = hang
        self.pid = 4242

    async def readline(self):
        if not self._lines:
            if self._hang:
                await asyncio.sleep(3600)      # 还活着, 读端挂着
                return b""
            self.returncode = self._rc
            return b""
        return self._lines.pop(0).encode()

    async def wait(self):
        if not self._hang:
            self.returncode = self._rc
        return self.returncode

    def terminate(self):
        self.returncode = -15

    def kill(self):
        self.returncode = -9


class FakeCtx:
    def __init__(self, cfg=None, config=None, cancelled=False, cancel_after=None):
        self.test_config = cfg or {}
        self.config = config or {}
        self.logs = []
        self.progress = []
        self._cancelled = cancelled
        # cancel_after: 第几次 is_cancelled() 调用开始返回 True
        # (轮次开始前会先查一次, 所以要 ≥ 2 才是「轮内取消」)
        self._cancel_after = cancel_after
        self._checks = 0

    async def on_log(self, line):
        self.logs.append(line)

    async def on_progress(self, pct, msg):
        self.progress.append((pct, msg))

    async def on_metric(self, k, v):
        pass

    async def is_cancelled(self):
        self._checks += 1
        if self._cancelled:
            return True
        if self._cancel_after is not None:
            return self._checks >= self._cancel_after
        return False


_ORIGINALS = {}
_SUDO_CALLS = []


class FakeGroup:
    """假进程表 —— 模拟「我们自己起的那一棵进程树」。

    ⚠️ **拓扑照 2026-10-29 真机实测摆** (这是 G11 的要害):

        sudo    pid 4242  pgid 4242  sid 4242
          └─ timeout  pid 4300  pgid 4300  sid 4242   ← timeout 自己另起了一组
               └─ glmark2  pid 4301  pgid 4300  sid 4242  ← 真正占着屏幕的

    也就是: **sudo 那一组 (4242) 里只有 sudo 一个** —— 照着它整组杀,
    root 的 glmark2 根本收不到信号 (老代码就是这么干的, 然后还报
    「窗口应已消失」)。真正该杀的是 **4300** 那一组。

    `killed` 里记下 (pgid, sig), 用来断言收尾动作。
    """

    BIN = "/fake/glmark2-es2"

    def __init__(self, alive=True):
        self.alive = alive
        self.killed = []
        self.sudo_pid = 4242
        self.sid = 4242
        self.sudo_pgid = 4242
        self.payload_pgid = 4300
        self._proc = [
            (4242, "sudo", "S", 4242, 4242,
             f"sudo -n env DISPLAY=:0 /usr/bin/timeout -k 5.0 305 "
             f"{self.BIN} --fullscreen -b build"),
            (4300, "timeout", "S", 4300, 4242,
             f"/usr/bin/timeout -k 5.0 305 {self.BIN} --fullscreen -b build"),
            (4301, "glmark2-es2", "S", 4300, 4242,
             f"{self.BIN} --fullscreen -b build:use-vbo=false"),
        ]

    def list_proc(self):
        return list(self._proc) if self.alive else []

    async def kill(self, pgid, sig="TERM", timeout_sec=8.0, expect_cmdline=None,
                  session=None):
        self.killed.append((pgid, sig))
        if sig == "TERM":
            self.alive = False          # TERM 就退了, 不用 KILL
        return True, f"(假) 已向进程组 {pgid} 发出 {sig}"


def patch(lines, rc=0, cancelled=False, cancel_after=None, cfg=None, group=None,
          hang=False):
    """把 GPU 项的外部依赖全换成假的。"""
    _ORIGINALS["find_tool"] = gpu.find_tool
    _ORIGINALS["sudo_hint"] = gpu.sudo_hint_if_unavailable
    _ORIGINALS["with_sudo"] = gpu.with_sudo
    _ORIGINALS["exec"] = gpu.asyncio.create_subprocess_exec
    _ORIGINALS["display"] = gpu._precheck_display
    _ORIGINALS["session"] = gpu._precheck_session
    _ORIGINALS["dri"] = gpu._precheck_dri

    gpu.find_tool = lambda name: FakeGroup.BIN
    gpu.sudo_hint_if_unavailable = lambda result, label: True
    # 记下 with_sudo 拿到的 timeout_sec —— 用来断言「生效后的那个上限」
    # 真的传到了命令上 (不是配的那个更小的值)
    def _fake_with_sudo(cmd, env=None, timeout_sec=None):
        _SUDO_CALLS.append({"cmd": list(cmd), "timeout_sec": timeout_sec})
        return list(cmd)

    gpu.with_sudo = _fake_with_sudo
    # ⚠️ 前置检查现在是 **async** 的 —— X11 那一项要实连一次 xdpyinfo 才判得了
    # 「会话能不能用」(2026-10-29 新板第一轮 GPU 误判的教训), 所以桩也得是
    # async 的, 否则 `await checker()` 会炸。
    async def _fake_display():
        return True, "测试桩"

    async def _fake_session(variant):
        return True, "测试桩"

    async def _fake_dri():
        return True, "测试桩"

    gpu._precheck_display = _fake_display
    gpu._precheck_session = _fake_session
    gpu._precheck_dri = _fake_dri

    async def fake_exec(*cmd, **kw):
        return FakeProc(lines, rc=rc, hang=hang)

    gpu.asyncio.create_subprocess_exec = fake_exec
    _SUDO_CALLS.clear()
    if group is not None:
        _ORIGINALS["_pgid_of"] = gpu._pgid_of
        _ORIGINALS["_sid_of"] = gpu._sid_of
        _ORIGINALS["_own_pgid"] = gpu._own_pgid
        _ORIGINALS["_scan_proc"] = gpu._scan_proc
        _ORIGINALS["_sudo_kill_group"] = gpu._sudo_kill_group
        # 假进程表: 我们起的进程 (pid 4242) 自己就是会话 leader (sid == pid)
        gpu._pgid_of = lambda pid: pid
        gpu._sid_of = lambda pid: pid
        gpu._own_pgid = lambda: 999999
        gpu._scan_proc = group.list_proc
        gpu._sudo_kill_group = group.kill
    return FakeCtx(cancelled=cancelled, cancel_after=cancel_after, cfg=cfg)


def restore():
    gpu.find_tool = _ORIGINALS["find_tool"]
    gpu.sudo_hint_if_unavailable = _ORIGINALS["sudo_hint"]
    gpu.with_sudo = _ORIGINALS["with_sudo"]
    gpu.asyncio.create_subprocess_exec = _ORIGINALS["exec"]
    gpu._precheck_display = _ORIGINALS["display"]
    gpu._precheck_session = _ORIGINALS["session"]
    gpu._precheck_dri = _ORIGINALS["dri"]
    for name in ("_pgid_of", "_sid_of", "_own_pgid", "_scan_proc",
                 "_sudo_kill_group"):
        if name in _ORIGINALS:
            setattr(gpu, name, _ORIGINALS[name])
            _ORIGINALS.pop(name)


# ── ① 场景行解析 ───────────────────────────────────────────────

def case1():
    print("【1】场景行要数得对 (老代码数的是不存在的 `^Scene Number`)")
    ids = []
    for line in LINES_FULL:
        m = gpu._SCENE_RE.match(line)
        if m:
            ids.append(gb.identity(m.group("scene"), m.group("opts")))
    check(len(ids) == 5, "5 行场景行数出 5 个场景", len(ids))
    check(ids[0] == "build|use-vbo=false", "第 1 个是 build/use-vbo=false", ids[0])
    check(ids[2] == "texture|texture-filter=nearest", "texture 的身份带选项",
          ids[2])
    check(ids[3] == "jellyfish|", "`<default>` 归一化成空", ids[3])
    # 分数行 / 分隔线**不是**场景行
    for line in SCORE_BANNER:
        check(gpu._SCENE_RE.match(line) is None,
              f"分数行不算场景: {line[:30]!r}")
    # 老代码数的那行, 新的也得能数 (虽然是死代码, 但别让人以为还在用)
    check(gpu._SCENE_RE.match("Scene Number 1") is None,
          "老格式 `Scene Number` 不再被当成场景行")


# ── ② 判定: 三种「没跑完」必须分开 ─────────────────────────────

def case2():
    print("\n【2】判定: 场景数是清单的前缀 → stopped (人误关了窗口), 不是 failed")
    expected = gb.expected_identities()
    # 只渲染了 2 个场景就出分 (历史 run 124 的原样)
    rendered = ["build|use-vbo=false", "build|use-vbo=true"]
    st, err = gpu._judge_round(
        rc=0, score=198.0, rendered=rendered, expected=expected,
        cancelled=False, timed_out=False, timeout_sec=600, tag="", out="",
    )
    check(st == "stopped", f"只跑 2/{len(expected)} 个场景 → stopped (不是 failed)", st)
    check(st != "failed", "**不是** failed (不冤枉被测设备)", st)
    check("没有结论" in (err or ""), "错误信息明说「没有结论」", err)
    check("2/" in (err or ""), "错误信息带上场景数", err)
    check("Esc" in (err or "") or "关" in (err or ""),
          "错误信息提示常见原因 (窗口被关/按了 Esc)", err)
    check("不是板子不合格" in (err or ""),
          "错误信息明说「不是板子不合格」", err)

    print("\n【2b】判定: 一个场景都没渲染出来 → error (工具没跑起来, 不是 failed)")
    st, err = gpu._judge_round(
        rc=1, score=None, rendered=[], expected=expected,
        cancelled=False, timed_out=False, timeout_sec=600, tag="", out="",
    )
    check(st == "error", "0 场景 + 非 0 退出码 → error", st)
    check(st != "failed", "**不是** failed", st)
    check("没跑起来" in (err or ""), "错误信息明说「工具/环境没跑起来」", err)
    check("不是「板子不合格」" in (err or ""),
          "错误信息明说不是板子不合格", err)

    print("\n【2c】判定: 渲染过场景之后才崩 → failed (真证据, 别放过)")
    st, err = gpu._judge_round(
        rc=139, score=None, rendered=rendered, expected=expected,
        cancelled=False, timed_out=False, timeout_sec=600, tag="", out="",
    )
    check(st == "failed", "渲染过之后异常退出 → failed (板子真的崩过)", st)

    print("\n【3】判定: 用户按停止 → stopped (主动中止 ≠ 失败)")
    st, err = gpu._judge_round(
        rc=0, score=198.0, rendered=rendered, expected=expected,
        cancelled=True, timed_out=False, timeout_sec=600, tag="", out="",
    )
    check(st == "stopped", "用户取消 → stopped", st)

    print("\n【4】判定: 挂钟超时 → error + 披露跑了多少")
    st, err = gpu._judge_round(
        rc=-9, score=None, rendered=rendered, expected=expected,
        cancelled=False, timed_out=True, timeout_sec=120, tag="", out="",
    )
    check(st == "error", "超时 → error", st)
    check("2/" in (err or ""), "错误信息披露超时前跑了几个场景", err)
    check("没有结论" in (err or ""), "错误信息明说「没有结论」", err)

    print("\n【5】判定: 场景和清单对不上 (不是前缀) → error, 不是 failed")
    st, err = gpu._judge_round(
        rc=0, score=100.0, rendered=["build|use-vbo=false", "jellyfish|"],
        expected=expected, cancelled=False, timed_out=False,
        timeout_sec=600, tag="", out="",
    )
    check(st == "error", "不是清单的前缀 → error (清单过时了)", st)
    check("标定" in (err or ""), "错误信息提示要重新标定", err)

    print("\n【6】判定: 跑全了 → passed")
    st, err = gpu._judge_round(
        rc=0, score=148.0, rendered=list(expected), expected=expected,
        cancelled=False, timed_out=False, timeout_sec=600, tag="", out="",
    )
    check(st == "passed", "跑满清单 → passed", (st, err))
    check(err is None, "通过时没有错误信息", err)


# ── ③ 场景清单自洽 ─────────────────────────────────────────────

def case3():
    print("\n【7】清单自洽: 拼出来的 -b 参数要能还原出同一批场景")
    args = gb.build_bench_args(3.0)
    specs = [args[i + 1] for i in range(0, len(args), 2)]
    check(all(args[i] == "-b" for i in range(0, len(args), 2)),
          "参数都是成对的 -b <场景>")
    check(len(specs) == len(gb.BENCHMARKS),
          "-b 的条数 = 清单的条数", (len(specs), len(gb.BENCHMARKS)))
    check(all("duration=3" in s for s in specs), "每个场景都带上了 duration=3")
    # 从 -b 串里把身份反解出来, 必须和清单一致
    back = []
    for s in specs:
        parts = s.split(":")
        scene = parts[0]
        opts = ":".join(p for p in parts[1:] if not p.startswith("duration="))
        back.append(gb.identity(scene, opts))
    check(back == gb.expected_identities(),
          "从 -b 参数反解出的身份 == 清单", (back[:2], gb.expected_identities()[:2]))
    # 场景名不能带冒号/空格 (那是 -b 的分隔符)
    check(all(":" not in s and " " not in s for s, _ in gb.BENCHMARKS),
          "场景名里没有分隔符")


# ── ④ 端到端: 真的跑一遍 GPUTest.run() ─────────────────────────

async def case4():
    print("\n【8】端到端: 半截的测试不能记成 passed, 也不该记成 failed")
    expected = gb.expected_identities()
    # 假装只跑了 2 个场景就退出了 (exit 0, 照样出分) —— 历史 run 124 的原样
    lines = LINES_FULL[:2] + SCORE_BANNER
    ctx = patch(lines)
    try:
        res = await gpu.GPUTest().run(ctx)
    finally:
        restore()
    check(res.status == "stopped", "整项判 stopped (人关了窗口, 不是板子不合格)",
          res.status)
    check(res.status != "passed", "不是 passed", res.status)
    check(res.status != "failed", "不是 failed", res.status)
    check(res.metrics.get("scenes_completed") == 2, "场景数被数出来了 (2)",
          res.metrics.get("scenes_completed"))
    check(res.metrics.get("scenes_expected") == len(expected),
          "指标里有「应该跑几个」", res.metrics.get("scenes_expected"))
    check("没有结论" in (res.error or ""), "错误信息明说「没有结论」", res.error)
    # 分数明明是 198, 但**不能**判通过
    check(res.metrics.get("score") == 198.0, "分数照样记下来 (供排查)",
          res.metrics.get("score"))

    print("\n【8b】端到端: 一个场景都没渲染出来 → error (工具没跑起来)")
    # 假进程一行都不吐就退出 (rc=1) —— 模拟 sudo 提权失败 / 会话连不上
    ctx = patch([], rc=1)
    try:
        res = await gpu.GPUTest().run(ctx)
    finally:
        restore()
    check(res.status == "error", "整项判 error", res.status)
    check(res.status != "failed", "不是 failed (不冤枉被测设备)", res.status)
    check("没跑起来" in (res.error or ""), "错误信息明说工具/环境没跑起来",
          res.error)
    check(res.metrics.get("scenes_completed") == 0, "场景数 = 0",
          res.metrics.get("scenes_completed"))

    print("\n【9】端到端: 跑全了才判 passed")
    full_lines = full_scene_lines()
    ctx = patch(full_lines)
    try:
        res = await gpu.GPUTest().run(ctx)
    finally:
        restore()
    check(res.status == "passed", "跑满 → passed", (res.status, res.error))
    check(res.metrics.get("scenes_completed") == len(expected),
          "场景数 = 清单长度", res.metrics.get("scenes_completed"))

    # ── 指标里**不许**存原始输出 (§8.2 排版, 2026-10-26) ──
    #
    # rounds_detail 以前带着每一轮的 glmark2 输出(末尾 20 行)。它进报告
    # 会被 repr 成**一个巨大的单元格**(比一页还高)→ 指标表跟前面的行断开;
    # 而且它是**截断副本**, 完整的那份在日志附录里。指标里只留量化字段。
    detail = res.metrics.get("rounds_detail") or []
    check(len(detail) == 1, "rounds_detail 有一轮的记录", detail)
    check(all("tail" not in d for d in detail),
          "rounds_detail 里没有原始输出(tail 被剔掉)", detail)
    check(all(isinstance(d.get("score"), (int, float)) for d in detail),
          "rounds_detail 里留着量化字段(分数)", detail)
    check(res.metrics.get("rounds_scores"),
          "另有一个标量指标 rounds_scores(各轮分数, 界面上也好看)",
          res.metrics.get("rounds_scores"))


async def case5():
    print("\n【10】端到端: 跑到一半按「停止」→ stopped, 不是 failed 也不是 passed")
    # 取消时 glmark2 可能已经渲染了几个场景 —— 判定看的是「用户主动中止」,
    # 不是「场景数不足」, 所以哪怕只跑了 1 个场景也该是 stopped。
    lines = LINES_FULL[:1] + SCORE_BANNER
    # cancel_after=2: 轮次开始前查第 1 次(不算取消), 轮内第 2 次才取消
    ctx = patch(lines, cancel_after=2)
    try:
        res = await gpu.GPUTest().run(ctx)
    finally:
        restore()
    check(res.status == "stopped", "整项判 stopped", res.status)
    check("Esc" in (res.error or ""), "提示工人怎么让全屏窗口立刻消失",
          res.error)
    check("timeout" in (res.error or "").lower()
          or "超时上限" in (res.error or ""),
          "提示窗口可能还会留着 (root 进程发不了信号)", res.error)


async def case6():
    print("\n【11】超时兜底得跟着 scene_sec 走 (兜底是防跑飞, 不是卡时长)")
    # scene_sec=10 (历史全量套件的时长) → 一轮预计 33×10+6 = 336s。
    # 配置里写死的 240s 兜底会**把每一轮都掐断** —— 兜底反而成了主因。
    want = len(gb.BENCHMARKS) * 10 + gpu._ROUND_SETUP_SEC + gpu._TIMEOUT_MARGIN_SEC
    ctx = patch(full_scene_lines(),
                cfg={"scene_sec": 10, "timeout_sec": 240, "rounds": 1})
    try:
        res = await gpu.GPUTest().run(ctx)
    finally:
        restore()
    check(res.metrics.get("timeout_sec") == want,
          f"单轮上限自动抬到 {want}s (配的 240s 短于预计 336s)",
          res.metrics.get("timeout_sec"))
    check(any("自动抬到" in l for l in ctx.logs),
          "日志里说明了为什么抬", ctx.logs[:4])
    check(res.status == "passed", "抬完之后这一轮正常判通过",
          (res.status, res.error))
    # 抬后的上限必须**真的传到了命令上** (命令超时 = 单轮上限 + 15s 收尾余量)
    check(len(_SUDO_CALLS) == 1, "起了 1 条 glmark2 命令", len(_SUDO_CALLS))
    check(_SUDO_CALLS and _SUDO_CALLS[0]["timeout_sec"] == int(want) + 15,
          "命令超时用的是抬后的上限 (+15s 余量), 不是配的 240s",
          _SUDO_CALLS[0]["timeout_sec"] if _SUDO_CALLS else None)
    # 命令里 33 条 -b 都在, 且每条都带上了 duration=10
    cmd = _SUDO_CALLS[0]["cmd"] if _SUDO_CALLS else []
    specs = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-b"]
    check(len(specs) == len(gb.BENCHMARKS),
          f"命令里有 {len(gb.BENCHMARKS)} 条 -b", len(specs))
    check(all("duration=10" in s for s in specs),
          "每条 -b 都带上了调大后的 duration=10")

    # 默认档 (scene_sec=3, 预计 105s) 配 240s → 够长, 不该动
    ctx = patch(full_scene_lines(),
                cfg={"scene_sec": 3, "timeout_sec": 240, "rounds": 1})
    try:
        res = await gpu.GPUTest().run(ctx)
    finally:
        restore()
    check(res.metrics.get("timeout_sec") == 240,
          "够长时保持配置值不动", res.metrics.get("timeout_sec"))


async def case7():
    print("\n【12】按「停止」→ 以 root 身份**整组收掉真正占屏的那一组**")
    grp = FakeGroup()
    ctx = patch(full_scene_lines()[:1] + SCORE_BANNER, cancel_after=2, group=grp,
                hang=True)
    try:
        res = await gpu.GPUTest().run(ctx)
    finally:
        restore()
    check(res.status == "stopped", "整项判 stopped", res.status)
    check(len(grp.killed) == 1, "只收了一次 (TERM 就退了, 不多杀)",
          grp.killed)
    check(grp.killed and grp.killed[0][1] == "TERM", "先发 TERM", grp.killed)
    # ⚠️ **G11 的要害**: GNU timeout 自己另起了一组, glmark2 在 **4300** 那一组,
    #    sudo 那一组 (4242) 里只有 sudo 一个。杀 4242 = 打空组 = root 的
    #    glmark2 收不到信号, 而工具还照样报「窗口应已消失」—— 撒谎。
    check(grp.killed and grp.killed[0][0] == 4300,
          "杀的是**真正跑着 glmark2 的那一组** (4300)", grp.killed)
    check(grp.killed and grp.killed[0][0] != 4242,
          "**没有**去杀 sudo 那个空组 (4242 —— 打过去打不到 root 的 glmark2)",
          grp.killed)
    check("整组收掉" in (res.error or "") and "Esc" not in (res.error or ""),
          "提示语说窗口已被收掉, 不用再让工人按 Esc", res.error)
    check(any("进程组" in l for l in ctx.logs),
          "日志里记下了收尾用的那个进程组", ctx.logs[:6])


async def case8():
    print("\n【13】提权收尾的安全前提: 校验不过就绝不动手")
    # 这一组里全是**别的东西** (防 pid 复用撞车) —— 必须拒绝, 且一条命令
    # 都不许发出去。
    # ⚠️ 这里把 `with_sudo` 和 `create_subprocess_exec` **都**换成假的:
    #    万一哪天哪道校验失效了, 发出去的也只是**假命令** —— 真命令要是漏出去,
    #    那就是在真机上杀进程组了, 测试不该有这权限。
    orig_scan = gpu._scan_proc
    orig_own = gpu._own_pgid
    orig_with_sudo = gpu.with_sudo
    orig_exec = gpu.asyncio.create_subprocess_exec
    sent = []

    def spy(cmd, env=None, timeout_sec=None):
        sent.append(list(cmd))
        return list(cmd)

    class FakeKillProc:
        returncode = 0

        async def communicate(self):
            return b"", b""

    async def fake_exec(*cmd, **kw):
        sent.append(list(cmd))
        return FakeKillProc()

    gpu.with_sudo = spy
    gpu.asyncio.create_subprocess_exec = fake_exec
    try:
        # ① 组里没有活进程
        gpu._scan_proc = lambda: []
        ok, why = await gpu._sudo_kill_group(4242, "TERM", session=4242)
        check(not ok and "没有活进程" in why, "空组 → 拒绝, 不用杀", why)

        # ② 组里有别的进程 (不含 glmark2) → 拒绝 (pid 复用撞车)
        gpu._scan_proc = lambda: [
            (1234, "sleep", "S", 4242, 4242, "/bin/sleep 100")]
        ok, why = await gpu._sudo_kill_group(4242, "TERM",
                                             expect_cmdline="glmark2",
                                             session=4242)
        check(not ok and "不是我们要杀的那个" in why,
              "组里不是 glmark2 → 拒绝", why)

        # ③ 组里混进了**别的会话**的进程 → 拒绝 (2026-10-29 新增的那道)
        #    组 id 会过期、会撞车; **会话 id 不会** —— 这是新加的防撞依据
        gpu._scan_proc = lambda: [
            (1234, "glmark2-es2", "S", 4242, 9999,
             "/usr/bin/glmark2-es2 --fullscreen"),
            (1235, "sleep", "S", 4242, 9999, "/bin/sleep 100")]
        ok, why = await gpu._sudo_kill_group(4242, "TERM",
                                             expect_cmdline="glmark2",
                                             session=4242)
        check(not ok and "不属于我们会话" in why,
              "组里混进别的会话的进程 → 拒绝 (防 pid 复用撞车)", why)

        # ④ 没说清是哪个会话 → 拒绝 (会话才是认「这棵树是我们起的」的依据)
        gpu._scan_proc = lambda: [
            (1234, "glmark2-es2", "S", 4242, 4242,
             "/usr/bin/glmark2-es2 --fullscreen")]
        ok, why = await gpu._sudo_kill_group(4242, "TERM",
                                             expect_cmdline="glmark2")
        check(not ok and "哪个会话" in why, "没给会话 → 拒绝", why)

        # ⑤ 我们自己的进程组 → 拒绝 (否则等于自杀)
        gpu._own_pgid = lambda: 4242
        ok, why = await gpu._sudo_kill_group(4242, "TERM", session=4242)
        check(not ok and "自杀" in why, "服务自己的组 → 拒绝", why)
        gpu._own_pgid = orig_own

        # ⑥ 非法 pgid
        ok, why = await gpu._sudo_kill_group(0, "TERM", session=4242)
        check(not ok and "不合法" in why, "pgid=0 → 拒绝", why)
        ok, why = await gpu._sudo_kill_group(1, "TERM", session=4242)
        check(not ok and "不合法" in why, "pgid=1 (init) → 拒绝", why)
    finally:
        gpu._scan_proc = orig_scan
        gpu._own_pgid = orig_own
        gpu.with_sudo = orig_with_sudo
        gpu.asyncio.create_subprocess_exec = orig_exec
    check(not sent, "上面六种情况一条提权命令都没发出去", sent)


def case9():
    print("\n【14】按会话找那棵树: 认的是**真正跑 glmark2 的那一组**")
    grp = FakeGroup()
    orig_scan = gpu._scan_proc
    gpu._scan_proc = grp.list_proc
    try:
        t = gpu.GPUTest()
        t.sid = grp.sid
        t._glmark_bin = FakeGroup.BIN
        t.proc = FakeProc([])              # sudo 自己 (pid 4242)
        check(t._glmark_target_group() == 4300,
              "找到的是 payload 那一组 (4300), 不是 sudo 那一组 (4242)",
              t._glmark_target_group())
        check(t._glmark_left_alive() is True,
              "安全网: 认出 root 的 glmark2 还活着 (原来永远认不出)")
        # glmark2 还没起来, 只有 timeout 包装层 → 也认它那一组
        grp._proc = [p for p in grp._proc if p[0] != 4301]
        check(t._glmark_target_group() == 4300,
              "glmark2 还没起来时, 认 timeout 那一组 (4300) —— "
              "它还在就说明 glmark2 还能被拉起来",
              t._glmark_target_group())
        # 一个都没了 → 找不到
        grp._proc = []
        check(t._glmark_target_group() is None, "都没了 → 找不到 (None)",
              t._glmark_target_group())
        check(t._glmark_left_alive() is False, "安全网不再误报")
    finally:
        gpu._scan_proc = orig_scan


async def case10():
    print("\n【15】收不掉就**如实说收不掉** —— 绝不谎报「窗口应已消失」")
    # 假的收尾: 信号发出去了, 但那个占屏的进程**赖着不走** (D 态 / 卡死的
    # 那种)。这时工具必须说「没能收掉、可能还占着屏幕」——
    # 谎报「窗口已经消失」会让工人不去按 Esc, 屏幕就一直占着。
    class StubbornGroup(FakeGroup):
        async def kill(self, pgid, sig="TERM", timeout_sec=8.0,
                      expect_cmdline=None, session=None):
            self.killed.append((pgid, sig))
            return True, f"(假) 已向进程组 {pgid} 发出 {sig} (但它没死)"

    grp = StubbornGroup()
    ctx = patch(full_scene_lines()[:1] + SCORE_BANNER, cancel_after=2, group=grp,
                hang=True)
    try:
        res = await gpu.GPUTest().run(ctx)
    finally:
        restore()
    check(len(grp.killed) >= 2, "TERM 不行就升级到 KILL", grp.killed)
    check(grp.killed and grp.killed[-1][1] == "KILL", "最后一轮是 KILL",
          grp.killed)
    check(res.status == "stopped", "整项仍是 stopped", res.status)
    msg = res.error or ""
    check("可能还占着屏幕" in msg or "没能把它收掉" in msg,
          "提示语明说「没能收掉 / 可能还占着屏幕」", msg)
    check("窗口应该已经消失" not in msg, "**没有**谎报「窗口已经消失」", msg)
    check(not any("窗口应已消失" in l for l in ctx.logs),
          "日志里也没有谎报「窗口应已消失」", ctx.logs)


async def main():
    case1()
    case2()
    case3()
    await case4()
    await case5()
    await case6()
    await case7()
    await case8()
    case9()
    await case10()
    print("\n" + ("全部通过" if not FAILS else f"有失败项 ({len(FAILS)})"))
    for f in FAILS:
        print("  ✗ " + f)
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
