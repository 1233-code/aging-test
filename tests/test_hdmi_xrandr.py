"""回归测试: HDMI 项的判定三态 + 双输出口 + 刷新率容差 (§6.7.2)。

起因: §6.7.2 写着「已用 8 个端到端用例覆盖上述三态 + 双输出口 + 断开 +
刷新率容差边界」—— 查遍 `tests/` 目录, **一个 HDMI 用例都没有**。文档吹的
覆盖是假的, 而这一项恰好是「最容易静默判错」的那种:

  · **真值来源选错** —— sysfs 的 `modes` 是 EDID 报的**支持列表**, 不是
    「现在是什么模式」。拿它的第一条当当前分辨率上报, 换台显示器就露馅。
  · **两个名字对不上** —— sysfs 叫 `card0-HDMI-A-1`, xrandr 叫 `HDMI-1`,
    按全名匹配必然对不上, 于是「查不到对应输出口」被当成「查不到 xrandr」
    降级放行 —— **真矛盾被静默放过**。
  · **三态判错** —— 拿判不准的读数当失败依据, 会误杀好板子; 反过来把真
    矛盾当「查不到」放行, 会放过没点亮的屏。两种错都是「静默的通过」。

这个测试验九件事:
    ① 三态 ok      —— sysfs connected + xrandr 有当前模式 → passed, 上报真值
    ② 三态 unavailable(无 DISPLAY) → passed(降级), 当前分辨率标「未验证」
    ③ 三态 unavailable(没装 xrandr) → passed(降级)
    ④ 三态 mismatch(xrandr 说没连) → **failed**(真矛盾, 不放过)
    ⑤ 双输出口     —— 按**前缀**认口, 不能被「另一个口是 primary」带跑
    ⑥ 断开         —— sysfs 全 disconnected → skipped(不是 failed)
    ⑦ 没有 connector → skipped
    ⑧ connected 但 EDID 没报出 mode → failed
    ⑨ 刷新率容差   —— 59.96 对 60 算一致; 50.00 只提示、**不判失败**

不碰真硬件: sysfs 和 xrandr 的输出全是喂进去的假数据。
跑法: .venv/bin/python -m tests.test_hdmi_xrandr
"""
import asyncio
import os
import sys

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
CONN_HDMI_NO_MODES = {
    "name": "card0-HDMI-A-1",
    "status": "connected",
    "modes": [],
}
CONN_DISCONNECTED = {
    "name": "card0-HDMI-A-1",
    "status": "disconnected",
    "modes": ["1920x1080"],
}

# ── 假 xrandr 输出 (照本机实测格式抄的) ─────────────────────────
XR_HDMI_ONLY = """Screen 0: minimum 320 x 200, current 1920 x 1080, maximum 8192 x 8192
HDMI-1 connected primary 1920x1080+0+0 (normal left inverted right x axis y axis) 527mm x 296mm
   1920x1080_60.00  59.96*+
   1280x720_60.00   59.94
"""

# 双输出口: DP-1 才是 primary —— 按全名/按 primary 认都会挑错口
XR_TWO_OUTPUTS = """Screen 0: minimum 320 x 200, current 1920 x 1080, maximum 8192 x 8192
DP-1 connected primary 1920x1080+0+0 (normal left inverted right x axis y axis) 527mm x 296mm
   2560x1440_60.00  59.94*+
HDMI-1 connected 1920x1080+0+0 (normal left inverted right x axis y axis) 527mm x 296mm
   1920x1080_60.00  59.96*+
"""

# xrandr 说这个口没连 —— 与 sysfs 的 connected **矛盾**
XR_DISCONNECTED = """Screen 0: minimum 320 x 200, current 1920 x 1080, maximum 8192 x 8192
HDMI-1 disconnected (normal left inverted right x axis y axis)
   1920x1080_60.00  59.96 +
"""

# 刷新率 50.00 —— 跟目标 60 差 10Hz, 超出 ±1Hz 容差
XR_REFRESH_50 = """Screen 0: minimum 320 x 200, current 1920 x 1080, maximum 8192 x 8192
HDMI-1 connected primary 1920x1080+0+0 (normal left inverted right x axis y axis) 527mm x 296mm
   1920x1080_50.00  50.00*+
"""

# xrandr 里**只有 DP-1**, 没有 HDMI 口 —— 而 sysfs 说 HDMI 是 connected。
# 这是「xrandr 里没有对应输出口」, 不是「读不到 xrandr」: 三态里都是
# unavailable(降级放行), 但**指标上要分得开** —— 否则报告上写着「未验证」,
# 看报告的人以为压根没查, 而不是「查了, 但 xrandr 里没有这个口」。
XR_NO_HDMI = """Screen 0: minimum 320 x 200, current 1920 x 1080, maximum 8192 x 8192
DP-1 connected primary 1920x1080+0+0 (normal left inverted right x axis y axis) 527mm x 296mm
   1920x1080_60.00  59.96*+
"""


class FakeCtx:
    def __init__(self, target="1920x1080@60"):
        self.test_config = {"target_resolution": target}
        self.logs = []

    async def on_log(self, line): self.logs.append(line)

    async def on_progress(self, pct, msg): pass

    async def on_metric(self, k, v): pass

    async def is_cancelled(self): return False

    @property
    def text(self):
        return "\n".join(self.logs)


class FakeProc:
    """假子进程 —— 只给 xrandr 用。"""

    def __init__(self, stdout_text, returncode=0):
        self._out = (stdout_text or "").encode()
        self.returncode = returncode

    async def communicate(self):
        return self._out, b""

    def kill(self):
        pass

    async def wait(self):
        return self.returncode


def patch(conns, xrandr_text=None, xrandr_bin="/fake/xrandr", display=" :0.0"):
    """把 HDMI 项的三个外部依赖全换成假的。

    · _read_drm_sysfs —— 假 sysfs
    · create_subprocess_exec —— 假 xrandr 子进程。**不换掉 _read_xrandr 本尊** ——
      那个函数自己管「无 DISPLAY 就返回 None」「超时 kill」「rc≠0 视为失败」,
      换掉它就等于把要测的判定逻辑也一起换掉了
    · find_tool        —— 假二进制路径 (None = 没装)
    """
    H._read_drm_sysfs = lambda: conns
    H.find_tool = lambda name: xrandr_bin

    async def fake_exec(*cmd, **kw):
        if xrandr_text is None:
            return FakeProc("", returncode=1)
        return FakeProc(xrandr_text)

    H.asyncio.create_subprocess_exec = fake_exec

    if display is None:
        os.environ.pop("DISPLAY", None)
    else:
        os.environ["DISPLAY"] = display


async def run_hdmi(conns, xrandr_text=None, xrandr_bin="/fake/xrandr",
                  display=" :0.0", target="1920x1080@60"):
    patch(conns, xrandr_text, xrandr_bin, display)
    ctx = FakeCtx(target)
    r = await H.HDMITest().run(ctx)
    return r, ctx


async def case1():
    print("【1】三态 ok: sysfs connected + xrandr 有当前模式 → passed, 上报真值")
    r, ctx = await run_hdmi([CONN_HDMI], XR_HDMI_ONLY)
    check(r.status == "passed", "判成 passed", r.status)
    check(r.metrics.get("current_resolution") == "1920x1080",
          "当前分辨率取的是 xrandr 真值 (不是 EDID 支持列表第一条)",
          r.metrics.get("current_resolution"))
    check(abs((r.metrics.get("current_refresh") or 0) - 59.96) < 0.01,
          "当前刷新率 = 59.96 (xrandr 报的, 不是配置的 60)",
          r.metrics.get("current_refresh"))
    check(r.metrics.get("screen") == "1920x1080", "屏尺寸也带上了",
          r.metrics.get("screen"))
    check(r.metrics.get("connector") == "card0-HDMI-A-1",
          "connector 记的是 sysfs 那个名字", r.metrics.get("connector"))
    check(r.metrics.get("target_mode_in_modes") is True,
          "目标分辨率在 EDID 支持列表里",
          r.metrics.get("target_mode_in_modes"))


async def case2():
    print("\n【2】三态 unavailable: 无 DISPLAY → passed(降级), 当前分辨率标「未验证」")
    r, ctx = await run_hdmi([CONN_HDMI], XR_HDMI_ONLY, display=None)
    check(r.status == "passed", "降级放行 (不误杀好板子)", r.status)
    check(r.metrics.get("current_resolution") == "(未验证)",
          "当前分辨率标「未验证」, 不是拿 EDID 列表冒充",
          r.metrics.get("current_resolution"))
    check("查不到当前分辨率" in ctx.text, "日志里明写了没验证", ctx.text[-200:])


async def case3():
    print("\n【3】三态 unavailable: 没装 xrandr → 同样降级放行")
    r, ctx = await run_hdmi([CONN_HDMI], None, xrandr_bin=None)
    check(r.status == "passed", "降级放行", r.status)
    check(r.metrics.get("current_resolution") == "(未验证)",
          "当前分辨率标「未验证」", r.metrics.get("current_resolution"))
    check("xrandr" in ctx.text, "日志里点名说是 xrandr 的问题", ctx.text[-200:])


async def case4():
    print("\n【4】三态 mismatch: xrandr 说没连 (与 sysfs 矛盾) → **failed**")
    r, ctx = await run_hdmi([CONN_HDMI], XR_DISCONNECTED)
    check(r.status == "failed", "真矛盾判 failed (不放过)", r.status)
    check(r.error and "connected" in r.error,
          "错误原因写清了矛盾在哪", r.error)
    check(r.metrics.get("current_resolution") == "(HDMI-1 无当前模式)",
          "指标写明这个口没有当前模式", r.metrics.get("current_resolution"))


async def case5():
    print("\n【5】双输出口: 按**前缀**认口, 不能被「另一个口是 primary」带跑")
    r, ctx = await run_hdmi([CONN_HDMI], XR_TWO_OUTPUTS)
    check(r.status == "passed", "判成 passed", r.status)
    check(r.metrics.get("current_resolution") == "1920x1080",
          "取的是 HDMI 口的分辨率, 不是 DP 口的 2560x1440",
          r.metrics.get("current_resolution"))
    check(abs((r.metrics.get("current_refresh") or 0) - 59.96) < 0.01,
          "刷新率也是 HDMI 口的 (59.96, 不是 DP 口的 59.94)",
          r.metrics.get("current_refresh"))


async def case6():
    print("\n【6】断开: sysfs 全部 disconnected → skipped (不是 failed)")
    r, ctx = await run_hdmi([CONN_DISCONNECTED], XR_DISCONNECTED)
    check(r.status == "skipped", "判成 skipped", r.status)
    check(r.error and "未连接" in r.error, "错误原因写的是未接显示器", r.error)


async def case7():
    print("\n【7】根本没有 HDMI/DP connector → skipped")
    r, ctx = await run_hdmi([], XR_HDMI_ONLY)
    check(r.status == "skipped", "判成 skipped", r.status)
    check(r.error and "connector" in (r.error or ""),
          "错误原因写的是没有 connector", r.error)


async def case8():
    print("\n【8】connected 但 EDID 没报出任何 mode → failed")
    r, ctx = await run_hdmi([CONN_HDMI_NO_MODES], XR_HDMI_ONLY)
    check(r.status == "failed", "判成 failed", r.status)
    check(r.error and "mode" in r.error, "错误原因写的是没 mode", r.error)


async def case9():
    print("\n【9】刷新率容差: 59.96 算一致; 50.00 只提示、**不判失败**")
    r, ctx = await run_hdmi([CONN_HDMI], XR_HDMI_ONLY)
    check(r.status == "passed", "59.96 对 60 (差 0.04) → passed", r.status)
    check("一致" in ctx.text, "日志里说与目标一致", ctx.text[-200:])

    r2, ctx2 = await run_hdmi([CONN_HDMI], XR_REFRESH_50)
    check(r2.status == "passed", "50.00 差 10Hz 也**不判失败** (只提示)",
          r2.status)
    check("仅提示" in ctx2.text or "不同" in ctx2.text,
          "日志里明写了不一致", ctx2.text[-200:])


async def case10():
    print("\n【10】xrandr 里**没有对应输出口** → 降级 pass, 指标要跟「读不到 xrandr」分得开")
    r, ctx = await run_hdmi([CONN_HDMI], XR_NO_HDMI)
    check(r.status == "passed", "降级放行 (不误杀好板子)", r.status)
    check(r.metrics.get("current_resolution") == "(xrandr 无对应输出口)",
          "指标写的是「无对应输出口」, 不是笼统的「未验证」",
          r.metrics.get("current_resolution"))
    check("没有与" in ctx.text, "日志里点名说是没有对应输出口", ctx.text[-200:])


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
    print("\n" + ("全部通过" if not FAILS else f"有失败项 ({len(FAILS)})"))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
