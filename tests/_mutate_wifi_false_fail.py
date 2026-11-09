# 变异测试: 故意把 2026-11-06 修的那批「WiFi 误判」的行为改坏, 看
# tests/test_wifi_radio_cycle.py 会不会红。
#
# 为什么要有这么个东西: 「测试全绿」本身说明不了什么 —— 断言写错了、
# 写松了、或者压根没跑到, 都是绿的。唯一能证明这测试**咬得动**的办法,
# 就是亲手把代码改坏, 看它是不是真的红。
#
# 这一份守的是「两台好板子的 WiFi 项被判 failed」换来的规矩:
#   ① 归 **NetworkManager** 管的接口 ⇒ 不去跟它抢地盘;
#   ② 开关之前接口就不是 UP ⇒ 不去开关它(量到的是「还没初始化好」);
#   ③ 「force」档 ⇒ 无条件做(点名要复现故障的人才用);
#   ④ 「我们没动过它, 它就是 DOWN」⇒ 判 **skipped**, 不是 failed
#      (判 failed = 好板子被送去返修);
#   ⑤ 「我们动过它, 就得我们松手」⇒ 收尾一律把接口恢复成 UP
#      (失败那条路 + 通过那条路都要恢复 —— 不恢复就是自我强化);
#   ⑥ 「接口起没起来」看**管理状态**(flags 里有没有 UP), **不看链路状态**
#      —— 没关联上 AP 时链路状态是 DORMANT, 而产线上没网的工位**必然**是
#      这个状态 ⇒ 拿它当判据 ⇒ 这一项永远判「未测」(实测 2026-11-06 产线
#      报告: WiFi 判 skipped, 原因「无线接口 wlan0 起不来(始终 DORMANT」))。
#
# ⚠️ **这些改动全部只发生在临时副本里** —— 仓库里 server/tests/wifi.py
#   是产线在跑的那份, 一个字都不动。副本跑完就扔, 不用还原。
#
# 跑法: .venv/bin/python tests/_mutate_wifi_false_fail.py
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests._tmp import mkdtemp                       # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(REPO, ".venv", "bin", "python")
TEST = "tests.test_wifi_radio_cycle"

# 每个变异: (文件, from, to, 说明)。from 必须在源文件里**找得到**, 否则算失败。
MUTATIONS = [
    (
        "server/tests/wifi.py",
        "        elif nm_managed and not force:",
        "        elif False:",
        "WIFI: 不认 NetworkManager 托管的接口(去跟它抢地盘 ⇒ 好板子被判 failed)",
    ),
    (
        "server/tests/wifi.py",
        '        elif pre_state != "UP" and not force:',
        "        elif False:",
        "WIFI: 不看开关前的接口状态(对「还没起来」的接口做开关 ⇒ 量到的是没起来)",
    ),
    (
        "server/tests/wifi.py",
        '            force = mode in ("force", "always")',
        "            force = False",
        "WIFI: force 档失效(点名要「无条件做」也不做)",
    ),
    (
        "server/tests/wifi.py",
        '            result.status = "skipped"',
        '            result.status = "failed"',
        "WIFI: 没测的东西判成不合格(好板子被扔进 failed ⇒ 白返修一台)",
    ),
    (
        "server/tests/wifi.py",
        '                await ctx.on_progress(100, "失败")\n'
        "                return await _finish()\n"
        "            await skip_because(",
        '                await ctx.on_progress(100, "失败")\n'
        "                return result\n"
        "            await skip_because(",
        "WIFI: ② 失败之后不把接口恢复回去"
        "(⇒ 下一轮看到「空闲射频」⇒ 又开关 ⇒ 又失败 ⇒ 自我强化)",
    ),
    (
        "server/tests/wifi.py",
        '        await ctx.on_progress(100, "完成")\n'
        "        # 收尾: 动过接口就把它恢复回去(见 `_finish` 的说明)\n"
        "        return await _finish()",
        '        await ctx.on_progress(100, "完成")\n'
        "        # 收尾: 动过接口就把它恢复回去(见 `_finish` 的说明)\n"
        "        return result",
        "WIFI: 通过的那条路不走收尾(接口停在 DOWN ⇒ 下一轮又得当成空闲射频)",
    ),
    # ── 2026-11-06 第二轮: 「没网的工位也能测」──
    #   判「接口起没起来」必须看**管理状态**(flags 里有没有 UP), 不能看
    #   **链路状态**(state)—— 没关联上 AP 时 state 是 DORMANT, 而产线上
    #   没网的工位**必然**是这个状态 ⇒ 拿它当判据 ⇒ 这一项永远判「未测」。
    (
        "server/tests/wifi.py",
        '        if admin != "UP":\n'
        '            if await ctx.is_cancelled():\n'
        '                result.status = "stopped"\n'
        '                await ctx.on_log("[WiFi] ⏹ 用户取消 (轮询等 UP 期间)")\n'
        "                return await _finish()\n"
        "            # 没动过 ⇒ 先试着拉起来",
        '        if carrier != "UP":\n'
        '            if await ctx.is_cancelled():\n'
        '                result.status = "stopped"\n'
        '                await ctx.on_log("[WiFi] ⏹ 用户取消 (轮询等 UP 期间)")\n'
        "                return await _finish()\n"
        "            # 没动过 ⇒ 先试着拉起来",
        "WIFI: ② 又拿「链路状态」当判据"
        "(没网 ⇒ DORMANT ⇒ 这一项在产线上永远判「未测」)",
    ),
    (
        "server/tests/wifi.py",
        '            flags = [f.strip() for f in fm.group(1).split(",")]',
        '            flags = [fm.group(1)]',
        "WIFI: flags 不按逗号切成整块(⇒ 管理态永远认不出来, 整项判未测)",
    ),
    (
        "server/tests/wifi.py",
        '            admin = "UP" if "UP" in flags else "DOWN"',
        '            admin = "UP"',
        "WIFI: 管理态恒判 UP(接口真的关着也说起来了 ⇒ 把没测到的当测到了)",
    ),
]


def build_copy(dst, rel, new_text):
    """把 server/ + config.yaml + 那个测试文件复制到 dst, 并把 rel 写成 new_text。

    每个文件**只写一次** —— 被变异的那个根本不复制进来, 直接写变异后的
    内容。全程不覆写任何已存在的文件, 所以跑完不需要还原。
    """
    src = os.path.join(REPO, "server")
    for root, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        relroot = os.path.relpath(root, src)
        outroot = (os.path.join(dst, "server") if relroot == "."
                   else os.path.join(dst, "server", relroot))
        os.makedirs(outroot, exist_ok=True)
        for fn in files:
            relfile = os.path.normpath(os.path.join(relroot, fn))
            if relfile == rel:
                continue          # 这个下面单独写
            shutil.copy2(os.path.join(root, fn), os.path.join(outroot, fn))

    shutil.copy2(os.path.join(REPO, "config.yaml"),
                 os.path.join(dst, "config.yaml"))
    os.makedirs(os.path.join(dst, "tests"), exist_ok=True)
    shutil.copy2(os.path.join(REPO, "tests", "test_wifi_radio_cycle.py"),
                 os.path.join(dst, "tests", "test_wifi_radio_cycle.py"))

    outp = os.path.join(dst, rel)
    os.makedirs(os.path.dirname(outp), exist_ok=True)
    with open(outp, "w", encoding="utf-8") as f:
        f.write(new_text)


def run_one(work):
    env = dict(os.environ)
    env["PYTHONPATH"] = work
    proc = subprocess.run(
        [PY, "-m", TEST],
        cwd=work, env=env, capture_output=True, text=True, timeout=300,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def main():
    root = mkdtemp(prefix="mut_wifi_false_fail_")
    print(f"副本都在 {root} 下面\n")

    bad = 0
    for i, (rel, frm, to, desc) in enumerate(MUTATIONS):
        srcp = os.path.join(REPO, rel)
        with open(srcp, encoding="utf-8") as f:
            orig = f.read()
        if frm not in orig:
            print(f"\n✗ {desc}\n   → 变异点没找到(源文件变了?): {frm[:60]!r}")
            bad += 1
            continue

        work = os.path.join(root, f"m{i:02d}")
        os.makedirs(work, exist_ok=True)
        build_copy(work, rel, orig.replace(frm, to))

        code, out = run_one(work)
        fails = out.count("✗")
        if code != 0 and fails == 0:
            # ⚠️ **红了但一个 ✗ 都没有** —— 那多半不是断言把它咬住的, 而是
            #   测试自己崩了(语法错/导入错/异常)。这不算「咬得动」。
            print(f"\n✗ {desc}\n   → 红了, 但一个 ✗ 都没有 (exit={code}) —— "
                  f"这是**崩了**, 不是被断言咬住的, 不算数:\n{out[-800:]}")
            bad += 1
        elif code != 0:
            print(f"\n✓ {desc}\n   → 测试变红 (exit={code}, {fails} 项失败) —— 咬得动")
        else:
            print(f"\n✗ {desc}\n   → 测试还是绿的! 这个变异没被守住")
            bad += 1

    print("\n" + ("全部变异都被抓住了 ✓" if bad == 0
                  else f"有 {bad} 个变异没被抓住 ✗"))
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
