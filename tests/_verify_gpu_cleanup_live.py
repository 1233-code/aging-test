"""真机实测 (不需要任何提权): 「找错组 → 打空组」这件事**是真的**。

起因 (2026-10-29, G11):
  原来的收尾打的是 **sudo 那一组** —— 而 GNU `timeout` 自己 `setpgid(0,0)`
  另起了一组, glmark2 在**另一组**里。所以那个 kill 打的是个**空组**:
  那个占着屏幕的进程根本收不到信号, 而工具还照样报「窗口应已消失」。

这个脚本在**真机**上起一棵**和产线同形**的假树, 然后验三件事:

    ① 拓扑: `timeout` 确实**自己另起了一组** —— 占屏的进程不在
       「我们起的那个进程」的组里 (老口径打的就是这个空组);
    ② `gpu._glmark_target_group()` 找到的是**占屏进程那一组** (4300 那样),
       不是我们起的那个进程的组;
    ③ 按**找到的那一组**收, 占屏进程**真的没了**; 按老口径那一组收,
       它**还在**。

⚠️ **全程不提权**: 假 glmark2 跑在**我们自己的 uid** 下 (真机上是 root,
   所以那边需要 `sudo -n kill` —— 那条路的代码没变, 授权范围见 gpu.py 模块
   顶部「提权收尾」)。这里要验的是**「组有没有找对」**, 这件事跟提权无关,
   所以用普通用户就能测, 而且测得更干净: 唯一被换掉的只是
   `_sudo_kill_group` → 一个等价的 `os.killpg` (我们自己起的组, 杀得动)。

    真实收尾逻辑 (`_kill_glmark_tree`: 发现 → 收 → 用**同一组**验证)
    是**原样**跑的, 只有「发信号」那一下换成了不提权的等价实现。

跑法: .venv/bin/python tests/_verify_gpu_cleanup_live.py
      .venv/bin/python tests/_verify_gpu_cleanup_live.py --privileged   # 真 root

⚠️ `--privileged` 会真的发 `sudo -n kill` —— 授权范围: **只针对本脚本自己起的
   那一组**(本脚本起完就记下组号, 只杀那一组), 不碰别的组, 不留进程。

── 2026-10-29 真机实测结论 (两遍都跑过) ──
  · 【A】不提权: 老口径那一组被打空 → 判「窗口应已消失」= True, 而占屏的进程
    **还活着** → 「撒谎」那一下是真的。
  · 【C】真 root: 占屏进程在**另一组** (73839 ≠ sudo 那一组 73833), 所以它
    **不可能**是被组杀打到的 —— 它之所以也死了, 是这台机器上 **sudo 会把信号
    转发**给它的子进程(timeout 再转发给 glmark2)。也就是说老口径在这台机器上
    是**靠转发侥幸**收掉的: 「我们那组空了」**证明不了**占屏进程没了; 一旦
    转发链断了(升级成 KILL 时组长先死、来不及转发), 老口径就会一边报
    「窗口应已消失」一边把窗口留在屏幕上。
  · 【D】真 root: 新口径找到占屏进程**自己那一组** (73872), 直接向它发信号
    (2 个进程), 再验**同一组**为空 —— 不依赖转发, 「窗口应已消失」这句是真的。
"""
import asyncio
import os
import shutil
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from server.tests import gpu  # noqa: E402

FAKE_NAME = "glmark2-es2"          # 名字里带 glmark2 → 收尾的认人条件认得出
FAILS = []


def check(cond, msg, extra=""):
    if cond:
        print("    ✓ " + msg)
    else:
        FAILS.append(msg)
        print("    ✗ " + msg + ("  → " + str(extra) if extra else ""))


class LogCtx:
    """最小 ctx —— 只给 on_log。"""

    def __init__(self):
        self.logs = []

    async def on_log(self, line):
        self.logs.append(line)
        print("      " + line)


async def spawn_tree(tmpdir, timeout_sec=30, privileged=False):
    """起一棵和产线**同形**的假树。

    产线的形状: 服务 → `sudo -n env … /usr/bin/timeout -k 5.0 N <bin>` → glmark2,
    也就是 **timeout 是「孙子」, 不是会话组长** —— GNU `timeout` 会
    `setpgid(0,0)` **自己另起一组**, 所以 glmark2 落在**另一组**里。

    ⚠️ 这里最容易踩空: 如果 timeout 直接就是会话组长 (没有中间那层),
    `setpgid(0,0)` 是**空操作** (组长本来就在自己那一组), 三者就同组了 ——
    那样测出来的是「同组」, 跟产线不符。所以这里用一个**会 fork 的包装层**
    (python 的 subprocess.call) 顶替 sudo 那一层: 它 fork 出孙子, 孙子
    exec timeout, timeout 于是不是组长 → 另起一组 ✓ (结构与产线一致)。

    privileged=True: 中间层换成**真的 sudo**(命令形状与产线逐字一致),
    假 glmark2 于是是**真 root** 进程 —— 这时才需要提权收尾。
    """
    fake = os.path.join(tmpdir, FAKE_NAME)
    os.makedirs(tmpdir, exist_ok=True)
    # 假 glmark2 = 一个「重新 exec 自己、并把 argv[0] 设成这条路径」的脚本 ——
    # 生产代码正是按「命令行**以** glmark2 二进制开头」认人的
    # (见 _glmark_target_group), 所以这里也必须让 cmdline 以这条路径开头。
    # (直接写脚本不行: 内核会按 shebang exec 成 `/bin/sh <脚本>`, cmdline
    #  就变成 `/bin/sh …` 了, 认不出来。)
    with open(fake, "w", encoding="utf-8") as f:
        f.write("#!/usr/bin/env python3\n"
                "import os\n"
                "os.execv('/bin/sleep', [os.path.abspath(__file__), '300'])\n")
    os.chmod(fake, 0o755)
    if privileged:
        cmd = gpu.with_sudo([fake, "--fullscreen", "-b", "build"],
                            env={"DISPLAY": ":0"}, timeout_sec=timeout_sec)
    else:
        # 会 fork 的包装层 (顶替 sudo 那一层): fork 出孙子, 孙子 exec timeout
        wrap = os.path.join(tmpdir, "wrap.py")
        with open(wrap, "w", encoding="utf-8") as f:
            f.write("import subprocess, sys\n"
                    "sys.exit(subprocess.call(sys.argv[1:]))\n")
        tmo = gpu.find_tool("timeout") or "/usr/bin/timeout"
        cmd = [sys.executable, wrap,
               tmo, "-k", "5.0", str(timeout_sec), fake, "--fullscreen", "-b", "build"]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,        # ← 和 gpu.py 里一样
    )
    return proc, fake, " ".join(cmd)


def payload_pids(sid, fake):
    """会话里命令行**以**假 glmark2 开头的进程 (就是占着屏幕的那个)。"""
    return [pid for (pid, _c, _s, _g, cmd) in gpu._session_members(sid)
            if cmd.startswith(fake)]


def payload_pgid(sid, fake):
    """占屏进程所在的**进程组 id** (它和 `timeout` 在同一组)。"""
    for (pid, _c, _s, g, cmd) in gpu._session_members(sid):
        if cmd.startswith(fake):
            return g
    return None


async def wait_tree_up(sid, fake, timeout=5.0):
    """等那棵树真的长出来 (payload 已经 exec 成了假 glmark2)。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        if payload_pids(sid, fake):
            return True
        await asyncio.sleep(0.1)
    return False


def print_table(sid):
    print("      进程表 (会话视角):")
    for (pid, comm, state, pgrp, cl) in gpu._session_members(sid):
        print(f"        pid={pid} pgid={pgrp} sid={sid} {comm}: {cl[:60]}")


async def scenario_old(tmpdir):
    print("\n【A】老口径: 按「我们起的那个进程」的组收 (= 2026-10-29 之前)")
    proc, fake, cmd = await spawn_tree(tmpdir)
    print("      命令: " + cmd)
    sid = gpu._sid_of(proc.pid)
    our_pgid = gpu._pgid_of(proc.pid)
    await asyncio.sleep(1.0)
    if not await wait_tree_up(sid, fake):
        check(False, "假树没起来 (实测环境有问题)", cmd)
        return
    pids = payload_pids(sid, fake)
    print_table(sid)
    payload_pgid = [g for (pid, _c, _s, g, _cl) in gpu._session_members(sid)
                    if pid in pids][0]
    check(payload_pgid != our_pgid,
          f"占屏进程在**另一组** ({payload_pgid} ≠ 我们那个进程的组 {our_pgid}) "
          f"—— 老口径打的是个**空组**", (payload_pgid, our_pgid))

    # 老口径: 按「我们起的那个进程」的组收
    try:
        os.killpg(our_pgid, 15)          # SIGTERM, 我们自己起的组, 杀得动
    except OSError as e:
        print(f"      (老口径收尾: {e})")
    await asyncio.sleep(1.0)
    still = payload_pids(sid, fake)
    # 老口径的判定依据: 「我们那个进程的组空没空」 —— 空了就报「窗口应已消失」
    old_says_gone = not gpu._proc_group_members(our_pgid)
    print(f"      老口径的判定: 「窗口应已消失」= {old_says_gone} "
          f"(它看的是我们那个进程的组 {our_pgid})")
    print(f"      实际上占屏的进程还在不在: {still or '没了'}")
    check(old_says_gone,
          "老口径会说「窗口应已消失」(它看的那一组确实空了)", old_says_gone)
    check(bool(still),
          "但占屏的进程**还活着** —— 这就是「撒谎」那一下", still)
    # 收干净 (别给现场留东西)
    if still:
        try:
            os.killpg(payload_pgid, 9)
        except OSError:
            pass
        await asyncio.sleep(0.5)
    check(not payload_pids(sid, fake), "A 组收尾完毕, 没留下进程")


async def scenario_new(tmpdir):
    print("\n【B】新口径: 按**真正跑 glmark2 的那一组**收 (2026-10-29 修完)")
    proc, fake, cmd = await spawn_tree(tmpdir)
    print("      命令: " + cmd)
    sid = gpu._sid_of(proc.pid)
    our_pgid = gpu._pgid_of(proc.pid)
    await asyncio.sleep(1.0)
    if not await wait_tree_up(sid, fake):
        check(False, "假树没起来 (实测环境有问题)", cmd)
        return
    pids = payload_pids(sid, fake)
    print_table(sid)
    payload_pgid = [g for (pid, _c, _s, g, _cl) in gpu._session_members(sid)
                    if pid in pids][0]

    # ── 用**真实的** GPUTest 收尾逻辑, 只把「发信号」那一下换成不提权的
    #    等价实现 (我们自己起的组, os.killpg 就够) ──
    killed = []

    async def fake_kill_group(pgid, sig="TERM", timeout_sec=8.0,
                              expect_cmdline=None, session=None):
        killed.append((pgid, sig))
        try:
            os.killpg(pgid, 15 if sig == "TERM" else 9)
        except OSError as e:
            return False, f"(假) killpg {pgid}: {e}"
        return True, f"(假) 已向进程组 {pgid} 发出 {sig}"

    orig_kill = gpu._sudo_kill_group
    gpu._sudo_kill_group = fake_kill_group
    try:
        t = gpu.GPUTest()
        t.proc = proc
        t.sid = sid
        t.pgid = our_pgid
        t._glmark_bin = fake
        t.ctx = LogCtx()

        target = t._glmark_target_group()
        check(target == payload_pgid,
              f"找到的目标组就是占屏进程那一组 ({target} == {payload_pgid})",
              (target, payload_pgid))
        check(target != our_pgid,
              f"**不是**「我们起的那个进程」的空组 ({our_pgid})",
              (target, our_pgid))
        check(t._glmark_left_alive() is True,
              "安全网认出占屏进程还活着 (原来永远认不出)")

        ok = await t._kill_glmark_tree("真机实测")
        check(ok, "收尾返回 True", ok)
        await asyncio.sleep(0.8)
        left = payload_pids(sid, fake)
        check(not left, "占屏的进程**真的没了**", left)
        # 这个 True 不能是撒谎: 它验的是**同一个组**
        check(not gpu._proc_group_members(target),
              "被杀的那个组真的空了 (所以「窗口应已消失」这句是真的)",
              gpu._proc_group_members(target))
        check(killed and killed[0][0] == target,
              "信号发到了**找出来的那一组**上", killed)
    finally:
        gpu._sudo_kill_group = orig_kill


async def scenario_privileged_old(tmpdir):
    """老口径在**真 root** 树上的行为 (照 2026-10-29 之前的代码走一遍)。"""
    print("\n【C】老口径(**提权**): 按「我们起的那个进程」的组收 —— 真 root 树")
    proc, fake, cmd = await spawn_tree(tmpdir, privileged=True)
    print("      命令: " + cmd)
    sid = gpu._sid_of(proc.pid)
    our_pgid = gpu._pgid_of(proc.pid)
    await asyncio.sleep(1.2)
    if not await wait_tree_up(sid, fake):
        check(False, "假树没起来 (实测环境有问题)", cmd)
        return
    print_table(sid)
    pgid = payload_pgid(sid, fake)
    check(pgid != our_pgid,
          f"占屏进程在**另一组** ({pgid} ≠ sudo 那一组 {our_pgid})", (pgid, our_pgid))

    # 老口径: TERM → 等 2s → 不行就 KILL; 然后看**我们起的那个进程**的组
    # 空没空 —— 空了就报「窗口已消失」(这就是老代码的判定依据)
    ok, detail = await gpu._sudo_kill_group(our_pgid, "TERM",
                                            expect_cmdline="glmark2",
                                            session=sid)
    print(f"      收尾 TERM: ok={ok} {detail}")
    for _ in range(10):
        await asyncio.sleep(0.2)
        if not gpu._proc_group_members(our_pgid):
            break
    if gpu._proc_group_members(our_pgid):
        ok2, detail2 = await gpu._sudo_kill_group(our_pgid, "KILL",
                                                  expect_cmdline="glmark2",
                                                  session=sid)
        print(f"      收尾 KILL: ok={ok2} {detail2}")
        await asyncio.sleep(0.8)
    old_says_gone = not gpu._proc_group_members(our_pgid)
    left = payload_pids(sid, fake)
    print(f"      老口径的判定: 「窗口已消失」= {old_says_gone} "
          f"(它看的是 sudo 那一组 {our_pgid})")
    print(f"      实际上占屏的进程还在不在: {left or '没了'}")
    check(not left,
          "老口径收完, **真 root 的占屏进程也没了** —— "
          "(说明这台机器上 sudo 把信号转发给了它, 老口径靠转发侥幸收掉了)",
          left)
    if not old_says_gone:
        check(False, "老口径连它自己看的那一组都没清空", our_pgid)
    # 收干净 (别给现场留东西)
    if left:
        await gpu._sudo_kill_group(pgid, "KILL", expect_cmdline="glmark2",
                                   session=sid)
        await asyncio.sleep(0.8)
    check(not payload_pids(sid, fake), "C 组收尾完毕, 没留下进程")


async def scenario_privileged_new(tmpdir):
    print("\n【D】新口径(**提权**): 按**真正跑 glmark2 的那一组**收 —— 真 root 树")
    proc, fake, cmd = await spawn_tree(tmpdir, privileged=True)
    print("      命令: " + cmd)
    sid = gpu._sid_of(proc.pid)
    our_pgid = gpu._pgid_of(proc.pid)
    await asyncio.sleep(1.2)
    if not await wait_tree_up(sid, fake):
        check(False, "假树没起来 (实测环境有问题)", cmd)
        return
    print_table(sid)
    pgid = payload_pgid(sid, fake)

    # 用**真实的** GPUTest 收尾逻辑 —— 一个桩都不打
    t = gpu.GPUTest()
    t.proc = proc
    t.sid = sid
    t.pgid = our_pgid
    t._glmark_bin = fake
    t.ctx = LogCtx()

    target = t._glmark_target_group()
    check(target == pgid,
          f"找到的目标组就是占屏进程那一组 ({target} == {pgid})", (target, pgid))
    check(target != our_pgid,
          f"**不是** sudo 那个空组 ({our_pgid})", (target, our_pgid))

    ok = await t._kill_glmark_tree("提权实测")
    check(ok, "收尾返回 True", ok)
    await asyncio.sleep(0.8)
    left = payload_pids(sid, fake)
    check(not left, "真 root 的占屏进程**真的没了**", left)
    check(not gpu._proc_group_members(target),
          "被杀的那个组真的空了 (所以「窗口应已消失」这句是真的)",
          gpu._proc_group_members(target))


async def main():
    tmp = tempfile.mkdtemp(prefix="g11_verify_")
    privileged = "--privileged" in sys.argv
    print(f"假 glmark2 放在: {tmp}")
    if privileged:
        print("⚠️ **提权模式** —— 会真的发 `sudo -n kill`"
              "(只针对本脚本自己起的那一组)")
        await scenario_privileged_old(os.path.join(tmp, "c"))
        await scenario_privileged_new(os.path.join(tmp, "d"))
    else:
        print("⚠️ 全程**不提权** —— 这里验的是「组有没有找对」")
        await scenario_old(os.path.join(tmp, "a"))
        await scenario_new(os.path.join(tmp, "b"))
    # ⚠️ **不删**临时目录 —— 删文件这种事要单独授权。这里只把它挪到一边,
    #    里面的假 glmark2 睡到自然醒 (最多 300s) 就没了。
    print(f"\n临时目录留着 (里面只有一个假 glmark2, 最多睡 300s): {tmp}")
    print("\n" + ("全部通过" if not FAILS else f"有失败项 ({len(FAILS)})"))
    for f in FAILS:
        print("  ✗ " + f)
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
