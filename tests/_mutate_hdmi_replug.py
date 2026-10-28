# 变异测试(后端): 故意把「HDMI 断开→恢复那一轮」改坏, 看
# tests/test_hdmi_replug.py 会不会红。
#
# 为什么要有这么个东西: 「测试全绿」本身说明不了什么 —— 断言写错了、
# 写松了、或者压根没跑到, 都是绿的。唯一能证明这测试**咬得动**的办法,
# 就是亲手把代码改坏, 看它是不是真的红。
#
# 这一项尤其需要: 断开→恢复这一轮最容易出的错 ——「关屏失败就判板子坏」
# 和「恢复不回来也算过」—— 都是**静默**的错: 界面照样绿, 报告照样出,
# 坏板子照样放行。所以必须有一份变异清单盯着它们。
#
# 做法: 把 server/ + 那个测试文件复制一份到临时目录, 在**副本**上做一处改动,
# 在副本目录里跑测试。每个变异跑完把副本还原。
#
# ⚠️ 只在**副本**上改 —— 仓库里 server/ 是产线在跑的那份, 改坏了没人知道。
# ⚠️ 副本里跑的是**假 xrandr**(假世界只记命令), 不会碰产线那块屏。
#
# 跑法: .venv/bin/python tests/_mutate_hdmi_replug.py
import os
import shutil
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(REPO, ".venv", "bin", "python")
TEST = "tests.test_hdmi_replug"
# 每一处变异都跑**这两个**测试: 前一个盯「这一轮怎么判」, 后一个盯
# 「界面大横幅这条通道通不通」(runner 有没有把 alert 广播出去)。
# 任意一个变红 = 这处变异被守住了。
TESTS = ["tests.test_hdmi_replug", "tests.test_runner_alert"]

# 每个变异: (文件, from, to, 说明)。from 必须在副本里**找得到**, 否则算失败。
MUTATIONS = [
    (
        "server/tests/hdmi.py",
        '        if round_state == "restore_failed":\n'
        '            result.status = "failed"',
        '        if round_state == "restore_failed":\n'
        '            result.status = "passed"',
        "HDMI: 关屏之后**恢复失败**也放过 (屏可能一直黑着, 工人还看不见)",
    ),
    (
        "server/tests/hdmi.py",
        '        if round_state == "mismatch":\n'
        '            result.status = "failed"',
        '        if round_state == "mismatch":\n'
        '            result.status = "passed"',
        "HDMI: 恢复后 xrandr 说这个口没输出(真矛盾)也放过",
    ),
    (
        "server/tests/hdmi.py",
        '        if round_state == "mismatch":\n'
        '            result.status = "failed"',
        '        if round_state in ("mismatch", "restored_degraded"):\n'
        '            result.status = "failed"',
        "HDMI: 恢复后**读不到** xrandr 也判失败 (拿判不准的读数杀好板子)",
    ),
    (
        "server/tests/hdmi.py",
        '        if round_state == "restore_failed":\n'
        '            result.status = "failed"',
        '        if round_state in ("restore_failed", "off_failed"):\n'
        '            result.status = "failed"',
        "HDMI: **关屏失败**也判失败 (那是桌面/X 会话不支持, 与板子无关)",
    ),
    (
        "server/tests/hdmi.py",
        '        if not _as_bool(_cfg(ctx, "replug_check", False), False):',
        '        if True:',
        "HDMI: 这一轮**永远不跑** (配置开了也没用)",
    ),
    (
        "server/tests/hdmi.py",
        '        if not _as_bool(_cfg(ctx, "replug_check", False), False):',
        '        if False:',
        "HDMI: **无视开关**一直跑 (工人没准备也被拉去黑屏)",
    ),
    (
        "server/tests/hdmi.py",
        '    defaults = ((getattr(ctx, "config", None) or {}).get("defaults") or {})\n'
        '    hdmi = defaults.get("hdmi") or {}\n'
        '    if key in hdmi:\n'
        '        return hdmi.get(key)\n'
        '    return default',
        '    return default',
        "HDMI: **不回落** config.yaml (工人忘了刷新页面 → 这一轮静默不跑)",
    ),
    (
        "server/tests/hdmi.py",
        '    if await _sleep(ctx, warn_sec, "⚠️ 即将黑屏", 50):',
        '    if False:',
        "HDMI: **不等提示**就关屏 (工人还没看清就要黑屏了)",
    ),
    (
        "server/tests/hdmi.py",
        '        if await _sleep(ctx, dwell, "🔌 黑屏中", 70):',
        '        if False:',
        "HDMI: **不等黑屏保持**就恢复 (关完马上开, 等于没断过)",
    ),
    (
        "server/tests/hdmi.py",
        '    out_name = base_cand["name"]',
        '    out_name = "DP-1"',
        "HDMI: 关的是**别的口**(不是基线判定用的那个)",
    ),
    (
        "server/tests/hdmi.py",
        '            if probe_r["state"] == "ok" or time.monotonic() >= deadline:\n'
        '                break',
        '            if True:\n'
        '                break',
        "HDMI: 恢复后**只读一次**就下结论 (桌面还没认回来 → 误杀好板子)",
    ),
    (
        "server/tests/hdmi.py",
        '        if turned_off and not restored:\n'
        '            await _run_xrandr_cmd(xrandr_bin, ["--output", out_name, "--auto"])',
        '        if False:\n'
        '            await _run_xrandr_cmd(xrandr_bin, ["--output", out_name, "--auto"])',
        "HDMI: 中途被打断时**不兜底恢复** (把工人留在一块黑屏前面)",
    ),
    (
        "server/tests/hdmi.py",
        '    if saved_mode:\n'
        '        attempts.append(["--output", out_name, "--mode", saved_mode])',
        '    if False:\n'
        '        attempts.append(["--output", out_name, "--mode", saved_mode])',
        "HDMI: 恢复**不试 --mode 兜底** (--auto 失败就没辙了)",
    ),
    (
        "server/tests/hdmi.py",
        '        return warn + dwell + restore + 10',
        '        return 60',
        "HDMI: 预计时长**编一个假的**(界面上显示的秒数跟实际对不上)",
    ),
    (
        "server/tests/hdmi.py",
        '    if isinstance(v, str):\n'
        '        return v.strip().lower() in ("1", "true", "yes", "on", "是")',
        '    if isinstance(v, str):\n'
        '        return bool(v)',
        "HDMI: 配置写成字符串 \"false\" 反而读成**开** (YAML 里手写字符串就中招)",
    ),
    (
        "server/tests/hdmi.py",
        '        # 恢复之后的模式才是**最后**这块屏在显示的东西 —— 报告上的\n'
        '        # 「当前分辨率」得跟着改, 不然报告写的跟屏上实际显示的对不上。\n'
        '        result.metrics["current_resolution"] = f"{cand[\'w\']}x{cand[\'h\']}"\n'
        '        result.metrics["current_refresh"] = cand["refresh"]',
        '        # (变异: 不更新指标)',
        "HDMI: 恢复后**不更新**「当前分辨率」(报告写的跟屏上实际显示的对不上)",
    ),
    # ── §6.7.3 界面大横幅 (2026-10-28): 提示得让人**看见** ──
    (
        "server/tests/hdmi.py",
        '        await ctx.on_alert(alert_text, "danger", warn_sec)',
        '        pass  # (变异: 不发横幅)',
        "HDMI: **不发横幅**(提示又缩回日志框里了, 工人看不见)",
    ),
    (
        "server/tests/hdmi.py",
        '        await ctx.on_alert(alert_text, "danger", warn_sec)',
        '        await ctx.on_alert(alert_text, "info", warn_sec)',
        "HDMI: 横幅颜色不是**重色**(「马上要黑屏」跟普通提醒一个样)",
    ),
    (
        "server/tests/hdmi.py",
        '        await ctx.on_alert(alert_text, "danger", warn_sec)',
        '        await ctx.on_alert(alert_text, "danger", 0)',
        "HDMI: 横幅**没有倒计时**(工人不知道还剩几秒)",
    ),
    (
        "server/tests/hdmi.py",
        '    if getattr(ctx, "on_alert", None):\n'
        '        await ctx.on_alert("", "warn", 0)\n',
        '    # (变异: 不收起横幅)\n',
        "HDMI: 横幅**不收起**(一条提示杵到下一项, 后面还误导人)",
    ),
    (
        "server/tests/hdmi.py",
        '    alert_text = (f"屏幕即将黑屏 —— 这块屏({out_name})会被自动关掉 {dwell:.0f} 秒, "',
        '    alert_text = (f"注意 —— 这块屏({out_name})会被自动关掉 {dwell:.0f} 秒, "',
        "HDMI: 横幅里**没说「黑屏」**(工人不知道马上会发生什么)",
    ),
    (
        "server/runner.py",
        '            on_alert=lambda text, level="warn", seconds=0, n=name:\n'
        '                self._on_alert(n, text, level, seconds),\n',
        '            # (变异: 没接上横幅通道)\n',
        "runner: **没把横幅通道接到测试项上**(ctx.on_alert 是空的)",
    ),
    (
        "server/runner.py",
        '        await self._broadcast({\n'
        '            "type": "alert", "name": name, "text": text,\n'
        '            "level": level or "warn", "seconds": seconds,\n'
        '        })',
        '        # (变异: 不广播)',
        "runner: 横幅**不广播**(前端永远收不到)",
    ),
]


def run_one(tmp):
    """跑一遍那几个测试 —— 任意一个红就算这处变异被守住。"""
    env = dict(os.environ)
    env["PYTHONPATH"] = tmp
    code = 0
    out = ""
    for mod in TESTS:
        proc = subprocess.run(
            [PY, "-m", mod],
            cwd=tmp, env=env, capture_output=True, text=True, timeout=300,
        )
        out += (proc.stdout or "") + (proc.stderr or "")
        if proc.returncode != 0:
            code = proc.returncode
    return code, out


def main():
    tmp = tempfile.mkdtemp(prefix="mut_hdmi_")
    # 复制一份: server/ + 那些测试文件
    shutil.copytree(os.path.join(REPO, "server"), os.path.join(tmp, "server"),
                    ignore=shutil.ignore_patterns("__pycache__"))
    os.makedirs(os.path.join(tmp, "tests"), exist_ok=True)
    for name in ("test_hdmi_replug.py", "test_hdmi_xrandr.py",
                 "test_runner_alert.py"):
        shutil.copy2(os.path.join(REPO, "tests", name),
                     os.path.join(tmp, "tests", name))
    print(f"副本在 {tmp}\n")

    bad = 0
    for rel, frm, to, desc in MUTATIONS:
        p = os.path.join(tmp, rel)
        with open(p, encoding="utf-8") as f:
            orig = f.read()
        if frm not in orig:
            print(f"\n✗ {desc}\n   → 变异点没找到(源文件变了?): {frm[:60]!r}")
            bad += 1
            continue
        with open(p, "w", encoding="utf-8") as f:
            f.write(orig.replace(frm, to))
        code, out = run_one(tmp)
        fails = out.count("✗")
        if code != 0:
            print(f"\n✓ {desc}\n   → 测试变红 (exit={code}, {fails} 项失败) —— 咬得动")
        else:
            print(f"\n✗ {desc}\n   → 测试还是绿的! 这个变异没被守住")
            bad += 1
        with open(p, "w", encoding="utf-8") as f:
            f.write(orig)      # 还原

    print("\n" + ("全部变异都被抓住了 ✓" if bad == 0
                  else f"有 {bad} 个变异没被抓住 ✗"))
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
