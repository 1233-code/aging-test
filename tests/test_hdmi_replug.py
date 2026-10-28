"""回归测试: HDMI 项的「断开→恢复」一轮(指令式, §6.7.3)。

背景 (2026-10-28): 第一版是让工人**物理拔线**再接回来 —— 产线反馈「这样的
逻辑不好」: 要人配合、要另开一台设备看界面、还得分不清「工人没拔线」和
「板子测不到热插拔」。改成**指令式**: 工具自己用
`xrandr --output HDMI-1 --off` 关一次屏, 等几秒, 再用 `--auto` 恢复, 然后
按**基线那套判定逻辑**判通过与否 —— 工人全程不用动手。

这个测试验二十件事:
    ① 关掉开关 → 不跑这一轮, 也不发任何写命令
    ② 正常一轮 → 通过, 且真的执行了 --off / --auto 两条命令
    ③ 提示阶段确实等了几秒(不是「马上就关」)
    ④ 黑屏那几秒确实等了(不是「关完马上开」)
    ⑤ 关屏失败 → **不判失败**(桌面/X 会话不支持, 与板子无关)
    ⑥ 恢复失败 → **failed**, 且日志写明「屏可能还黑着」
    ⑦ 恢复走 --mode 兜底 → 也能通过
    ⑧ 恢复后 xrandr 说这个口没输出 → **failed**(真矛盾)
    ⑨ 恢复后读不到 xrandr → 降级通过(不判失败)
    ⑩ 关屏后 xrandr 仍挂着旧模式 → 仅记录, 不判失败
    ⑪ 基线就没读出当前输出 → 跳过这一轮
    ⑫ 基线就矛盾 → 不跑这一轮
    ⑬ 页面没刷新 → 回落 yaml 的默认值, 这一轮照样跑
    ⑭ 中途被「停止」→ 兜底仍把屏恢复回来(不留黑屏给工人)
    ⑮ 恢复后的模式写回指标 + 预计时长算得出
    ⑯ 配置写成字符串也不读反
    ⑰ 等待期间有心跳帧
    ⑱ 桌面慢慢才认回来 → 要轮询等它认出来
    ⑲ 提示要显眼 → 界面上的**大横幅**(带倒计时, 关屏之前就收起)
    ⑳ 产线配置里提示就是 5 秒; 没有横幅通道也不影响测试

不碰真硬件: 连 `xrandr --off/--auto` 都是**假**的 —— 假世界只记命令,
绝不去动产线那块屏。
跑法: .venv/bin/python -m tests.test_hdmi_replug
"""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.tests import hdmi as H

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


# ── 假 sysfs ────────────────────────────────────────────────────
CONN_HDMI = {
    "name": "card0-HDMI-A-1",
    "status": "connected",
    "modes": ["1920x1080", "1280x720", "1024x768"],
}

# ── 假 xrandr 输出 ──────────────────────────────────────────────
XR_ON = """Screen 0: minimum 320 x 200, current 1920 x 1080, maximum 8192 x 8192
HDMI-1 connected primary 1920x1080+0+0 (normal left inverted right x axis y axis) 527mm x 296mm
   1920x1080_60.00  59.96*+
   1280x720_60.00   59.94
"""

# 关屏之后: 线还插着, 只是 X 不再驱动它了 → xrandr 报 disconnected
XR_OFF = """Screen 0: minimum 320 x 200, current 1920 x 1080, maximum 8192 x 8192
HDMI-1 disconnected (normal left inverted right x axis y axis) 0mm x 0mm
   1920x1080_60.00  59.96 +
"""

# 恢复之后桌面选了另一个模式(--auto 恢复的是 EDID 首选模式, 未必跟原来一样)
XR_RESTORED_OTHER = """Screen 0: minimum 320 x 200, current 2560 x 1440, maximum 8192 x 8192
HDMI-1 connected primary 2560x1440+0+0 (normal left inverted right x axis y axis) 527mm x 296mm
   2560x1440_60.00  59.94*+
"""


class World:
    """假世界 —— 连 `xrandr --off/--auto` 都是假的, 只记命令不碰真屏。

    · off_ok      —— `--off` 能不能成功(桌面/X 会话支不支持)
    · auto_ok     —— `--auto` 能不能成功
    · mode_ok     —— `--mode <原模式>` 这个兜底能不能成功
    · after_auto  —— **恢复之后** xrandr 报什么:
                     "on"(正常) / "off"(命令成功了但这个口还是没输出) /
                     "other"(桌面选了另一个模式) / "unavailable"(读不到了)
    · keep_mode   —— 关屏命令**没生效**(xrandr 仍报着旧模式)
    · slow_reads  —— 恢复之后, 前几次读**还认不出来**(桌面在慢慢认屏)

    ⚠️ `restore_attempted` 这道分界**必须有**: 「恢复前」和「恢复后」是
    两种完全不同的读数 —— 不分开的话, 基线那一读就可能拿到恢复后的值,
    测出来的东西全是假的。
    """

    def __init__(self, off_ok=True, auto_ok=True, mode_ok=False,
                 after_auto="on", keep_mode=False, slow_reads=0):
        self.off_ok = off_ok
        self.auto_ok = auto_ok
        self.mode_ok = mode_ok
        self.after_auto = after_auto
        self.keep_mode = keep_mode
        self.slow_reads = slow_reads
        self.is_off = False            # 现在屏是不是关着
        self.restore_attempted = False  # 恢复命令发出去过没有
        self.reads = 0                 # 读了几次 xrandr
        self.cmds = []                 # [(参数列表, 时刻), ...] —— 执行过的写命令

    def xrandr_text(self):
        if self.is_off and not self.keep_mode:
            return XR_OFF
        if self.restore_attempted:
            if self.slow_reads > 0:
                # 桌面还在慢慢认屏 —— 这一刻它确实还报不出当前模式
                self.slow_reads -= 1
                return XR_OFF
            if self.after_auto == "off":
                return XR_OFF        # 恢复命令成功了, 但这个口还是没输出
            if self.after_auto == "other":
                return XR_RESTORED_OTHER
        return XR_ON

    def wrote(self, flag):
        """执行过的写命令里有没有 --off / --auto。"""
        return any(flag in args for args, _ in self.cmds)

    def count(self, flag):
        return sum(1 for args, _ in self.cmds if flag in args)

    def cmd_texts(self):
        return [" ".join(a) for a, _ in self.cmds]

    def gap(self, a, b):
        """两条命令之间隔了多久(秒) —— a/b 是 "--off" / "--auto"。"""
        ta = tb = None
        for args, t in self.cmds:
            if a in args and ta is None:
                ta = t
            if b in args and tb is None:
                tb = t
        if ta is None or tb is None:
            return None
        return tb - ta


class FakeCtx:
    def __init__(self, target="1920x1080@60", test_config=None, config=None,
                 cancel_after=10**9):
        self.test_config = ({"target_resolution": target}
                            if test_config is None else test_config)
        self.config = config or {}
        self.cancel_after = cancel_after
        self.cancel_calls = 0
        self.logs = []
        self.progress = []
        # 界面上那条**大横幅**(§6.7.3): text 为空 = 收起
        self.alerts = []

    async def on_log(self, line): self.logs.append(line)

    async def on_progress(self, pct, msg): self.progress.append((time.monotonic(), pct, msg))

    async def on_metric(self, k, v): pass

    async def on_alert(self, text, level="warn", seconds=0):
        self.alerts.append({"text": text, "level": level,
                            "seconds": seconds, "t": time.monotonic()})

    async def is_cancelled(self):
        self.cancel_calls += 1
        return self.cancel_calls > self.cancel_after

    @property
    def text(self):
        return "\n".join(self.logs)


class FakeProc:
    def __init__(self, stdout_text="", returncode=0):
        self._out = (stdout_text or "").encode()
        self.returncode = returncode

    async def communicate(self):
        return self._out, b""

    def kill(self):
        pass

    async def wait(self):
        return self.returncode


FAST = {
    "replug_check": True,
    "replug_warn_sec": 0.05,
    "replug_off_dwell_sec": 0.1,
    "replug_restore_timeout_sec": 0.6,
}


def patch(world, xrandr_bin="/fake/xrandr", display=" :0.0"):
    """把外部依赖全换成假的: sysfs / xrandr 读 / xrandr 写命令。

    ⚠️ **读和写要分开**: 读是 `xrandr`(只有一个参数), 写是
    `xrandr --output X --off`(多个参数)。分不开的话, 假世界会把写命令
    当成「读一次 xrandr」返回, 整个测试就是在自欺欺人。
    """
    H._read_drm_sysfs = lambda: [CONN_HDMI]
    H.find_tool = lambda name: xrandr_bin
    H._RESTORE_RETRY_WAIT = 0.01        # 重试间隔本来是 0.8s, 测试里别真等
    H._POLL_SEC = 0.02                  # 轮询间隔本来是 0.5s, 测试里别真等

    async def fake_exec(*cmd, **kw):
        if len(cmd) == 1:               # ← 读
            world.reads += 1
            if world.after_auto == "unavailable" and world.restore_attempted:
                return FakeProc("", returncode=1)
            return FakeProc(world.xrandr_text())
        # ← 写
        args = list(cmd[1:])
        world.cmds.append((args, time.monotonic()))
        if "--off" in args:
            if not world.off_ok:
                return FakeProc("Error: cannot disable output", returncode=1)
            world.is_off = True
            return FakeProc("")
        if "--auto" in args:
            world.restore_attempted = True
            if not world.auto_ok:
                return FakeProc("Error: failed to set mode", returncode=1)
            world.is_off = False
            return FakeProc("")
        if "--mode" in args:
            world.restore_attempted = True
            if not world.mode_ok:
                return FakeProc("Error: no such mode", returncode=1)
            world.is_off = False
            return FakeProc("")
        return FakeProc("", returncode=1)

    H.asyncio.create_subprocess_exec = fake_exec

    if display is None:
        os.environ.pop("DISPLAY", None)
    else:
        os.environ["DISPLAY"] = display


async def run_hdmi(world=None, test_config=None, config=None, cancel_after=10**9,
                   use_fast=True):
    """跑一次 HDMI 项。

    ⚠️ `use_fast=False` = **模拟「页面是旧的」**: 前端那份里**不带**新键,
    只能靠后端回落 config.yaml。用 FAST 那份默认值去测回落是自欺欺人 ——
    前端那份本来就带着 replug_check, 回落那条路压根没走到。
    """
    world = world or World()
    patch(world)
    tc = dict(FAST, **(test_config or {})) if use_fast else dict(test_config or {})
    ctx = FakeCtx(test_config=tc, config=config, cancel_after=cancel_after)
    r = await H.HDMITest().run(ctx)
    return r, ctx, world


# ── 用例 ────────────────────────────────────────────────────────

async def case1():
    print("【1】开关关着 → 不跑这一轮, 也不发任何写命令")
    r, ctx, w = await run_hdmi(test_config={"replug_check": False})
    check(r.status == "passed", "判成 passed", r.status)
    check(r.metrics.get("replug_check") == "disabled", "指标记 disabled",
          r.metrics.get("replug_check"))
    check(w.cmds == [], "一条写命令都没发(没碰那块屏)", w.cmd_texts())
    check("未启用" in ctx.text, "日志里写了未启用")


async def case2():
    print("\n【2】正常一轮 → 通过, 且真的执行了 --off / --auto")
    r, ctx, w = await run_hdmi()
    check(r.status == "passed", "判成 passed", r.status)
    check(r.metrics.get("replug_state") == "restored", "指标记 restored",
          r.metrics.get("replug_state"))
    check(w.wrote("--off"), "执行了 --off")
    check(w.wrote("--auto"), "执行了 --auto")
    check("--output HDMI-1 --off" in w.cmd_texts(),
          "关的是**基线判定用的那个口**(HDMI-1)", w.cmd_texts())
    check("--output HDMI-1 --auto" in w.cmd_texts(),
          "恢复用的也是同一个口", w.cmd_texts())
    check("黑" in ctx.text and "正常现象" in ctx.text,
          "关屏前说了「会黑屏、属正常现象」", ctx.text[-300:])
    check("不用拔线" in ctx.text, "说了不用工人动手", ctx.text[-300:])


async def case3():
    print("\n【3】提示阶段确实等了几秒(不是「马上就关」)")
    warn = 0.3
    world = World()
    patch(world)
    # 心跳帧按 _HEARTBEAT_SEC 推 —— 提示阶段只有 0.3 秒, 心跳间隔 1.5 秒的话
    # 一帧都推不出来(那是对的, 0.3 秒不需要心跳)。这里把心跳调小, 好断言
    # 「提示期间确实有进度帧在动」。
    H._HEARTBEAT_SEC = 0.05
    ctx = FakeCtx(test_config=dict(FAST, replug_warn_sec=warn))
    t0 = time.monotonic()
    r = await H.HDMITest().run(ctx)
    t1 = time.monotonic()
    H._HEARTBEAT_SEC = 1.5
    check(r.status == "passed", "判成 passed", r.status)
    off_t = world.cmds[0][1]
    check(off_t - t0 >= warn * 0.8,
          f"关屏之前至少提示了 {warn} 秒(实际 {off_t - t0:.2f}s)", off_t - t0)
    check(any("即将黑屏" in m for _, _, m in ctx.progress),
          "提示期间推了「即将黑屏」的进度帧",
          [m for _, _, m in ctx.progress][:4])
    check("属正常现象" in ctx.text, "日志里说了「属正常现象」", ctx.text[-200:])
    check(t1 - t0 < 5, "整轮没拖成分钟级", t1 - t0)


async def case4():
    print("\n【4】黑屏那几秒确实等了(不是「关完马上开」)")
    r, ctx, w = await run_hdmi()
    gap = w.gap("--off", "--auto")
    check(gap is not None, "量到了关屏→恢复的间隔", gap)
    check(gap >= FAST["replug_off_dwell_sec"] * 0.9,
          f"黑屏至少 {FAST['replug_off_dwell_sec']} 秒", gap)


async def case5():
    print("\n【5】关屏失败 → **不判失败**(桌面/X 会话不支持, 与板子无关)")
    w = World(off_ok=False)
    r, ctx, w = await run_hdmi(world=w)
    check(r.status == "passed", "降级放行(不误杀好板子)", r.status)
    check(r.metrics.get("replug_state") == "off_failed", "指标记 off_failed",
          r.metrics.get("replug_state"))
    check(not w.wrote("--auto"), "屏没关过, 就不用恢复", w.cmd_texts())
    check("不判失败" in ctx.text, "日志里说明了为什么不判失败", ctx.text[-200:])


async def case6():
    print("\n【6】恢复失败 → **failed**, 且日志写明「屏可能还黑着」")
    w = World(auto_ok=False, mode_ok=False)
    r, ctx, w = await run_hdmi(world=w)
    check(r.status == "failed", "判成 failed", r.status)
    check(r.error and "没能恢复" in r.error, "错误原因写的是没恢复", r.error)
    check("黑" in ctx.text, "日志里提醒屏可能还黑着", ctx.text[-300:])
    check(w.count("--auto") >= 3,
          "兜底又补了一次 --auto(不留黑屏给工人)", w.cmd_texts())
    check(w.count("--mode") >= 1, "还试了 --mode 原模式", w.cmd_texts())


async def case7():
    print("\n【7】恢复走 --mode 兜底 → 也能通过")
    w = World(auto_ok=False, mode_ok=True)
    r, ctx, w = await run_hdmi(world=w)
    check(r.status == "passed", "判成 passed", r.status)
    check(r.metrics.get("replug_state") == "restored", "指标记 restored",
          r.metrics.get("replug_state"))
    check(any("--mode 1920x1080" in t for t in w.cmd_texts()),
          "用的是 --mode 1920x1080 兜底", w.cmd_texts())


async def case8():
    print("\n【8】恢复后 xrandr 说这个口没输出 → **failed**(真矛盾)")
    w = World(after_auto="off")
    r, ctx, w = await run_hdmi(world=w)
    check(r.status == "failed", "真矛盾判 failed", r.status)
    check(r.error and "点不亮" in r.error, "错误原因写的是点不亮", r.error)


async def case9():
    print("\n【9】恢复后读不到 xrandr → 降级通过(不判失败)")
    w = World(after_auto="unavailable")
    r, ctx, w = await run_hdmi(world=w)
    check(r.status == "passed", "降级放行", r.status)
    check(r.metrics.get("replug_state") == "restored_degraded",
          "指标记 restored_degraded", r.metrics.get("replug_state"))
    check("不判失败" in ctx.text, "日志里写了不判失败", ctx.text[-200:])


async def case10():
    print("\n【10】关屏后 xrandr 仍挂着旧模式 → 仅记录, 不判失败")
    w = World(keep_mode=True)
    r, ctx, w = await run_hdmi(world=w)
    check(r.status == "passed", "不判失败(桌面层的事)", r.status)
    check("仅记录" in ctx.text, "日志里写了「仅记录」", ctx.text[-300:])


async def case11():
    print("\n【11】基线就没读出当前输出 → 跳过这一轮")
    w = World()
    r, ctx, w = await run_hdmi(world=w)
    # 造一个「读不到 xrandr」的基线: 临时把读改成永远失败
    orig = H.create_subprocess_exec if hasattr(H, "create_subprocess_exec") else None

    async def always_fail(*cmd, **kw):
        if len(cmd) == 1:
            return FakeProc("", returncode=1)
        return FakeProc("", returncode=0)

    H.asyncio.create_subprocess_exec = always_fail
    ctx2 = FakeCtx(test_config=dict(FAST))
    r2 = await H.HDMITest().run(ctx2)
    check(r2.status == "passed", "基线降级 → 整项还是 passed", r2.status)
    check(r2.metrics.get("replug_state") == "skipped", "这一轮跳过了",
          r2.metrics.get("replug_state"))
    check("跳过这一轮" in ctx2.text, "日志里写了跳过", ctx2.text[-200:])


async def case12():
    print("\n【12】基线就矛盾 → 不跑这一轮")
    from tests.test_hdmi_xrandr import XR_DISCONNECTED, patch as xpatch
    H._read_drm_sysfs = lambda: [CONN_HDMI]
    H.find_tool = lambda name: "/fake/xrandr"

    async def fake_exec(*cmd, **kw):
        if len(cmd) == 1:
            return FakeProc(XR_DISCONNECTED)
        return FakeProc("", returncode=0)

    H.asyncio.create_subprocess_exec = fake_exec
    ctx = FakeCtx(test_config=dict(FAST))
    r = await H.HDMITest().run(ctx)
    check(r.status == "failed", "基线矛盾 → failed", r.status)
    check("replug_state" not in r.metrics, "根本没进这一轮", r.metrics)


async def case13():
    print("\n【13】页面没刷新 → 回落 yaml 的默认值, 这一轮照样跑")
    w = World()
    yaml_cfg = {"defaults": {"hdmi": {"replug_check": True,
                                      "replug_warn_sec": 0.05,
                                      "replug_off_dwell_sec": 0.1,
                                      "replug_restore_timeout_sec": 0.6}}}
    # ⚠️ use_fast=False: 前端那份**只有** target_resolution(页面是旧的,
    # 没带上新键) → 后端必须回落 config.yaml 的默认值
    r, ctx, w = await run_hdmi(world=w, use_fast=False,
                               test_config={"target_resolution": "1920x1080@60"},
                               config=yaml_cfg)
    check(r.status == "passed", "判成 passed", r.status)
    check(r.metrics.get("replug_state") == "restored", "这一轮照样跑了",
          r.metrics.get("replug_state"))
    check(w.wrote("--off"), "确实关过一次屏", w.cmd_texts())


async def case14():
    print("\n【14】中途被「停止」→ 兜底仍把屏恢复回来(不留黑屏给工人)")
    w = World()
    # 第 2 次 is_cancelled 才返回 True → 取消发生在**黑屏期间**
    r, ctx, w = await run_hdmi(world=w, cancel_after=1)
    check(r.status == "stopped", "判成 stopped", r.status)
    check(w.wrote("--auto"), "兜底把屏恢复回来了", w.cmd_texts())
    check(not w.is_off, "屏不是关着的(没把工人留在一块黑屏前)", w.is_off)


async def case15():
    print("\n【15】恢复后的模式写回指标 + 预计时长算得出")
    w = World(after_auto="other")
    r, ctx, w = await run_hdmi(world=w)
    check(r.status == "passed", "判成 passed", r.status)
    check(r.metrics.get("replug_resolution") == "2560x1440",
          "指标记的是恢复后的模式", r.metrics.get("replug_resolution"))
    check(r.metrics.get("current_resolution") == "2560x1440",
          "「当前分辨率」也跟着改成恢复后的(报告不能写旧值)",
          r.metrics.get("current_resolution"))
    check("与断屏前不同" in ctx.text, "日志里说明了模式变了", ctx.text[-300:])

    t = H.HDMITest()
    check(t.expected_seconds({"replug_check": True, "replug_warn_sec": 5,
                              "replug_off_dwell_sec": 3,
                              "replug_restore_timeout_sec": 10}) == 28,
          "预计时长算得出(5+3+10+10)", t.expected_seconds({"replug_check": True}))
    check(t.expected_seconds({"replug_check": False}) is None,
          "没开那一轮就不显示预计时间")


async def case16():
    print("\n【16】配置写成字符串也不读反(YAML 里手写字符串就中招)")
    w = World()
    r, ctx, w = await run_hdmi(world=w, test_config={"replug_check": "false"})
    check(r.metrics.get("replug_check") == "disabled", "字符串 \"false\" → 关",
          r.metrics.get("replug_check"))
    check(w.cmds == [], "没发写命令", w.cmd_texts())

    w2 = World()
    r2, ctx2, w2 = await run_hdmi(world=w2, test_config={"replug_check": "true"})
    check(r2.metrics.get("replug_check") == "enabled", "字符串 \"true\" → 开",
          r2.metrics.get("replug_check"))
    check(w2.wrote("--off"), "这一轮跑了", w2.cmd_texts())


async def case17():
    print("\n【17】等待期间有心跳帧(§10.1: 每 ≤2 秒至少 1 帧)")
    w = World()
    H._HEARTBEAT_SEC = 0.02
    r, ctx, w = await run_hdmi(world=w)
    frames = ctx.progress
    check(len(frames) >= 2, "推了多帧 progress", len(frames))
    gaps = [b[0] - a[0] for a, b in zip(frames, frames[1:])]
    check(all(g <= 2.0 for g in gaps), "相邻两帧间隔都 ≤2 秒", gaps)


async def case18():
    print("\n【18】桌面慢慢才认回来 → 要**轮询**等它认出来(读一次就下结论会误杀)")
    w = World(slow_reads=3)
    r, ctx, w = await run_hdmi(world=w)
    check(r.status == "passed", "等到了 → 判成 passed", r.status)
    check(r.metrics.get("replug_state") == "restored", "指标记 restored",
          r.metrics.get("replug_state"))
    check(w.reads >= 4, "轮询了不止一次才拿到 ok", w.reads)


async def case19():
    print("\n【19】提示要**显眼**: 界面进度条底下摆大横幅(不只是日志一行)")
    w = World()
    r, ctx, w = await run_hdmi(world=w)
    banners = [a for a in ctx.alerts if a["text"]]
    check(len(banners) >= 1, "发了横幅", ctx.alerts)
    if not banners:
        return
    b = banners[0]
    check("黑屏" in b["text"], "横幅里写了「黑屏」", b["text"])
    check("不用" in b["text"], "横幅里说了不用工人操作", b["text"])
    check("正常现象" in b["text"], "横幅里说了「属正常现象」", b["text"])
    check(b["level"] == "danger", "横幅是重色(danger)", b["level"])
    check(b["seconds"] == FAST["replug_warn_sec"],
          "横幅带倒计时(前端据此显示 3/2/1)", b["seconds"])

    off_t = w.cmds[0][1]
    check(b["t"] < off_t, "横幅在关屏**之前**就摆出来了")
    clears = [a for a in ctx.alerts if not a["text"]]
    check(len(clears) >= 1, "横幅被收起来了(不能一直杵着)")
    check(clears[0]["t"] < off_t,
          "收起发生在关屏之前(屏马上要黑, 横幅留着也没人看得见)")


async def case20():
    print("\n【20】产线配置里提示就是 5 秒; 没有横幅通道也不影响测试")
    import yaml
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "config.yaml"), encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    hd = cfg["defaults"]["hdmi"]
    check(hd.get("replug_warn_sec") == 5,
          "config.yaml 的 replug_warn_sec = 5(产线: 3 秒提示都看不完)",
          hd.get("replug_warn_sec"))
    check(hd.get("replug_off_dwell_sec") == 3,
          "config.yaml 的 replug_off_dwell_sec = 3", hd.get("replug_off_dwell_sec"))

    # 老 runner / 别的调用方没有 on_alert → 这一项照跑不误
    w = World()
    patch(w)
    ctx = FakeCtx(test_config=dict(FAST))
    ctx.on_alert = None
    r = await H.HDMITest().run(ctx)
    check(r.status == "passed", "没有横幅通道也判成 passed", r.status)
    check(w.wrote("--off") and w.wrote("--auto"), "这一轮照跑", w.cmd_texts())


async def main():
    await case1()
    await case2()
    await case3()
    await case4()
    await case5()
    await case6()
    await case7()
    await case8()
    await case9()
    await case10()
    await case11()
    await case12()
    await case13()
    await case14()
    await case15()
    await case16()
    await case17()
    await case18()
    await case19()
    await case20()
    print("\n" + ("全部通过" if not FAILS else f"有失败项 ({len(FAILS)})"))
    for f in FAILS:
        print("  - " + f)
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
