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
  · 判定分开三种「没跑完」:
        用户按「停止」        → stopped (主动中止, 不算失败)
        glmark2 自己退了      → failed  (这一轮没有结论)
        挂钟到点被 timeout 杀 → error   (没有结论, 且披露跑了多少)
    清单和实际对不上 (不是前缀) → error (清单过时了, 要重新标定)

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
    """假进程组 —— 模拟「我们起的那一组」里还活着一个 root 的 glmark2。

    killed 里记下 (pgid, sig), 用来断言收尾动作。
    """

    def __init__(self, alive=True, members=None):
        self.alive = alive
        self.members = members or [
            (4242, "glmark2-es2", "S",
             "/usr/bin/glmark2-es2 --fullscreen -b build:use-vbo=false"),
        ]
        self.killed = []

    def list_members(self, pgid):
        return self.members if self.alive else []

    async def kill(self, pgid, sig="TERM", timeout_sec=8.0, expect_cmdline=None):
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

    gpu.find_tool = lambda name: "/fake/glmark2-es2"
    gpu.sudo_hint_if_unavailable = lambda result, label: True
    # 记下 with_sudo 拿到的 timeout_sec —— 用来断言「生效后的那个上限」
    # 真的传到了命令上 (不是配的那个更小的值)
    def _fake_with_sudo(cmd, env=None, timeout_sec=None):
        _SUDO_CALLS.append({"cmd": list(cmd), "timeout_sec": timeout_sec})
        return list(cmd)

    gpu.with_sudo = _fake_with_sudo
    gpu._precheck_display = lambda: (True, "测试桩")
    gpu._precheck_session = lambda v: (True, "测试桩")
    gpu._precheck_dri = lambda: (True, "测试桩")

    async def fake_exec(*cmd, **kw):
        return FakeProc(lines, rc=rc, hang=hang)

    gpu.asyncio.create_subprocess_exec = fake_exec
    _SUDO_CALLS.clear()
    if group is not None:
        _ORIGINALS["_pgid_of"] = gpu._pgid_of
        _ORIGINALS["_own_pgid"] = gpu._own_pgid
        _ORIGINALS["_proc_group_members"] = gpu._proc_group_members
        _ORIGINALS["_sudo_kill_group"] = gpu._sudo_kill_group
        # 假进程组: 进程组 id == 我们的进程 pid (start_new_session 建的那种)
        gpu._pgid_of = lambda pid: pid
        gpu._own_pgid = lambda: 999999
        gpu._proc_group_members = group.list_members
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
    for name in ("_pgid_of", "_own_pgid", "_proc_group_members", "_sudo_kill_group"):
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
    print("\n【2】判定: 被掐断 → failed, 不是 passed")
    expected = gb.expected_identities()
    # 只渲染了 2 个场景就出分 (历史 run 124 的原样)
    rendered = ["build|use-vbo=false", "build|use-vbo=true"]
    st, err = gpu._judge_round(
        rc=0, score=198.0, rendered=rendered, expected=expected,
        cancelled=False, timed_out=False, timeout_sec=600, tag="", out="",
    )
    check(st == "failed", f"只跑 2/{len(expected)} 个场景 → failed (不是 passed)", st)
    check("没有结论" in (err or ""), "错误信息明说「没有结论」", err)
    check("2/" in (err or ""), "错误信息带上场景数", err)
    check("Esc" in (err or "") or "关" in (err or ""),
          "错误信息提示常见原因 (窗口被关/按了 Esc)", err)

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
    print("\n【8】端到端: 半截的测试不能再记成 passed")
    expected = gb.expected_identities()
    # 假装只跑了 2 个场景就退出了 (exit 0, 照样出分) —— 历史 run 124 的原样
    lines = LINES_FULL[:2] + SCORE_BANNER
    ctx = patch(lines)
    try:
        res = await gpu.GPUTest().run(ctx)
    finally:
        restore()
    check(res.status == "failed", "整项判 failed", res.status)
    check(res.metrics.get("scenes_completed") == 2, "场景数被数出来了 (2)",
          res.metrics.get("scenes_completed"))
    check(res.metrics.get("scenes_expected") == len(expected),
          "指标里有「应该跑几个」", res.metrics.get("scenes_expected"))
    check("没有结论" in (res.error or ""), "错误信息明说「没有结论」", res.error)
    # 分数明明是 198, 但**不能**判通过
    check(res.metrics.get("score") == 198.0, "分数照样记下来 (供排查)",
          res.metrics.get("score"))

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
    print("\n【12】按「停止」→ 以 root 身份整组收掉 (全屏窗口立刻消失)")
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
    check(grp.killed and grp.killed[0][0] == 4242,
          "杀的是我们自己那一组 (pgid == 我们起的进程 pid)", grp.killed)
    check("整组收掉" in (res.error or "") and "Esc" not in (res.error or ""),
          "提示语说窗口已被收掉, 不用再让工人按 Esc", res.error)
    check(any("进程组" in l for l in ctx.logs),
          "日志里记下了这一组进程的组 id", ctx.logs[:6])


async def case8():
    print("\n【13】提权收尾的安全前提: 校验不过就绝不动手")
    # 这一组里全是**别的东西** (防 pid 复用撞车) —— 必须拒绝, 且一条命令
    # 都不许发出去
    orig_members = gpu._proc_group_members
    orig_own = gpu._own_pgid
    sent = []

    async def spy(cmd, env=None, timeout_sec=None):
        sent.append(cmd)
        return list(cmd)

    orig_with_sudo = gpu.with_sudo
    gpu.with_sudo = spy
    try:
        # ① 组里没有活进程
        gpu._proc_group_members = lambda pgid: []
        ok, why = await gpu._sudo_kill_group(4242, "TERM")
        check(not ok and "没有活进程" in why, "空组 → 拒绝, 不用杀", why)

        # ② 组里有别的进程 (不含 glmark2) → 拒绝 (pid 复用撞车)
        gpu._proc_group_members = lambda pgid: [
            (1234, "sleep", "S", "/bin/sleep 100")]
        ok, why = await gpu._sudo_kill_group(4242, "TERM",
                                             expect_cmdline="glmark2")
        check(not ok and "不是我们要杀的那个" in why,
              "组里不是 glmark2 → 拒绝", why)

        # ③ 我们自己的进程组 → 拒绝 (否则等于自杀)
        gpu._own_pgid = lambda: 4242
        ok, why = await gpu._sudo_kill_group(4242, "TERM")
        check(not ok and "自杀" in why, "服务自己的组 → 拒绝", why)
        gpu._own_pgid = orig_own

        # ④ 非法 pgid
        ok, why = await gpu._sudo_kill_group(0, "TERM")
        check(not ok and "不合法" in why, "pgid=0 → 拒绝", why)
        ok, why = await gpu._sudo_kill_group(1, "TERM")
        check(not ok and "不合法" in why, "pgid=1 (init) → 拒绝", why)
    finally:
        gpu._proc_group_members = orig_members
        gpu._own_pgid = orig_own
        gpu.with_sudo = orig_with_sudo
    check(not sent, "上面四种情况一条提权命令都没发出去", sent)


async def main():
    case1()
    case2()
    case3()
    await case4()
    await case5()
    await case6()
    await case7()
    await case8()
    print("\n" + ("全部通过" if not FAILS else f"有失败项 ({len(FAILS)})"))
    for f in FAILS:
        print("  ✗ " + f)
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
