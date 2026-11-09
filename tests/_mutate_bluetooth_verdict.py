# 变异测试: 故意把「蓝牙项的判定口径」改坏, 看 tests/test_bluetooth_verdict.py
# 会不会红。
#
# 为什么要有这么个东西: 「测试全绿」本身说明不了什么 —— 断言写错了、
# 写松了、或者压根没跑到, 都是绿的。唯一能证明这测试**咬得动**的办法,
# 就是亲手把判定改坏, 看它是不是真的红。
#
# 这一份守的是 2026-11-04 实现的规矩(§6.4.1):
#   ① 判定是**反的**: 蓝牙**不工作** = 合格, **在工作** = 不合格;
#   ② 「在工作」= 有活动的控制器 **且** 射频没被屏蔽(两个信号都要满足);
#   ③ 三种「不工作」都判 passed, 但**要说清是哪一种**;
#   ④ **只读**: 一条命令都不许发;
#   ⑤ 合格项**不写 result.error**(那个字段在页面/报告里是红字错误栏);
#   ⑥ 探测不到系统状态 ⇒ 判 error, 不是 passed;
#   ⑦ 位置: 紧跟在 WiFi 后面。
#
# 做法: **每个变异单开一个全新目录**, 把 server/ + static/ + docs/ +
# config.yaml + 那个测试文件复制进去, 在**副本**上做一处改动, 在副本目录里
# 跑测试。被改的那个文件是从头到尾**只写一次**(直接写变异后的内容), 别的
# 文件原样复制 —— 所以整个过程不覆写任何已存在的文件, 跑完也不用还原。
#
# ⚠️ 只在**副本**上改 —— 仓库里 server/ 是产线在跑的那份, 改坏了没人知道。
#
# 跑法: .venv/bin/python tests/_mutate_bluetooth_verdict.py
import os
import shutil
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(REPO, ".venv", "bin", "python")
TEST = "tests.test_bluetooth_verdict"

# 需要整份复制过去的目录/文件(除了被变异的那一个)。
_COPY_DIRS = ("server", "static", "docs")
_COPY_FILES = ("config.yaml",)
_COPY_TEST = "tests/test_bluetooth_verdict.py"

# 每个变异: (文件, from, to, 说明)。from 必须在源文件里**找得到**, 否则算失败。
MUTATIONS = [
    (
        "server/tests/bluetooth.py",
        '    working = bool(controllers) and rf_state != "blocked"',
        '    working = False',
        "判定方向: 永远说「没在工作」⇒ 蓝牙真的在工作时也判合格",
    ),
    (
        "server/tests/bluetooth.py",
        '    working = bool(controllers) and rf_state != "blocked"',
        '    working = True',
        "判定方向: 永远说「在工作」⇒ 板子上压根没蓝牙也判不合格",
    ),
    (
        "server/tests/bluetooth.py",
        '    working = bool(controllers) and rf_state != "blocked"',
        '    working = bool(controllers)',
        "屏蔽不看: 射频被屏蔽着(soft/hard block)也算「在工作」",
    ),
    (
        "server/tests/bluetooth.py",
        '        if _read_text(os.path.join(d, "type")) != "bluetooth":\n'
        '            continue',
        '        if False:\n'
        '            continue',
        "rfkill 不按 type 过滤 ⇒ WiFi 的射频条目被当成「蓝牙硬件在」",
    ),
    (
        "server/tests/bluetooth.py",
        "    hw_present = bool(rfkill_entries) or bool(controllers)",
        "    hw_present = True",
        "bt_present 恒为 True ⇒ 压根没有蓝牙硬件的板子也说「有」",
    ),
    (
        "server/tests/bluetooth.py",
        '        result.metrics["bt_working"] = v["working"]',
        '        result.metrics["bt_working"] = False',
        "指标 bt_working 恒为 False ⇒ 报告里看不出蓝牙在工作",
    ),
    (
        "server/tests/bluetooth.py",
        '        result.metrics["bt_service_masked"] = (\n'
        '            "unknown" if masked is None else masked\n'
        '        )',
        '        result.metrics["bt_service_masked"] = False',
        "服务被 mask 这件事没记下来(报告里看不出蓝牙为什么没起来)",
    ),
    (
        "server/tests/bluetooth.py",
        "        if not os.path.isdir(SYS_CLASS_DIR):",
        "        if False:",
        "sysfs 读不到时也往下走 ⇒ 什么都不知道却判「合格」",
    ),
    (
        "server/tests/bluetooth.py",
        '        result.status = "passed"\n'
        '        await ctx.on_log(f"[蓝牙] ✓ 蓝牙: {VERDICT_OFF}',
        '        result.status = "passed"\n'
        '        result.error = "蓝牙已关闭(符合本机规格)"\n'
        '        await ctx.on_log(f"[蓝牙] ✓ 蓝牙: {VERDICT_OFF}',
        "合格项也往 result.error 里塞东西 ⇒ 页面上弹出**红色**错误栏",
    ),
    (
        "server/tests/bluetooth.py",
        'VERDICT_OFF = "已关闭(符合本机规格)"',
        'VERDICT_OFF = "通过"',
        "文案退回「通过」⇒ 工人以为「通过」= 蓝牙是好的",
    ),
    (
        "server/tests/bluetooth.py",
        '            result.error = (\n'
        '                "蓝牙**在工作**(有活动的控制器, 射频也没被屏蔽) —— 跟本机规格"\n'
        '                "(这台设备不需要蓝牙)对不上, 要点名。⚠️ 本项只做只读探测, "\n'
        '                "**没有**去动蓝牙的状态。"\n'
        '            )',
        '            result.error = None',
        "不合格时不写报错 ⇒ 报告里看不出「跟本机规格对不上」",
    ),
    (
        "server/tests/bluetooth.py",
        '    if not v["hw_present"]:\n'
        '        return "板上探不到蓝牙硬件 —— 这台设备本来就没有蓝牙"',
        '    if not v["hw_present"]:\n'
        '        return "不工作"',
        "「不工作」说不清是哪一种(压根没硬件 / 被屏蔽 / 没控制器)",
    ),
    (
        "server/tests/bluetooth.py",
        '            "[蓝牙] 只读探测 —— 不动蓝牙的任何状态(不解除射频屏蔽 / 不给蓝牙上电 / "\n'
        '            "不启停蓝牙服务)"',
        '            "[蓝牙] 只读探测 —— 不动蓝牙的任何状态(不发 rfkill unblock / 不启停蓝牙服务)"',
        "日志里混进了「开/关蓝牙」的命令字样(只读纪律的静态检查要抓住)",
    ),
    (
        "server/tests/bluetooth.py",
        "        result = TestResult()\n\n        await ctx.on_log(",
        "        result = TestResult()\n"
        "        import subprocess\n"
        '        subprocess.run(["echo", "mutation"], capture_output=True)\n\n'
        "        await ctx.on_log(",
        "偷偷起了子进程(只读纪律的静态检查要抓住)",
    ),
    (
        "server/runner.py",
        '    "wifi", "bluetooth",',
        '    "wifi",',
        "位置: 蓝牙项从整轮里掉了(§6.4.1: 必须排在 WiFi 后面)",
    ),
    (
        "server/runner.py",
        '    "wifi", "bluetooth",\n'
        '    # ④ 重载收尾 (产热最高)\n'
        '    "gpu", "cpu",',
        '    "wifi",\n'
        '    # ④ 重载收尾 (产热最高)\n'
        '    "gpu", "cpu",\n'
        '    # 蓝牙被挪到整轮最后\n'
        '    "bluetooth",',
        "位置: 蓝牙被挪到 GPU/CPU 后面(跑成重载之后的中间态了)",
    ),
]


def build_copy(dst, rel, new_text):
    """把 server/ + static/ + docs/ + config.yaml + 那个测试文件复制到 dst,
    并把 rel 写成 new_text。

    每个文件**只写一次** —— 被变异的那个根本不复制进来, 直接写变异后的
    内容。全程不覆写任何已存在的文件, 所以跑完不需要还原。
    """
    for d in _COPY_DIRS:
        src = os.path.join(REPO, d)
        out = os.path.join(dst, d)
        shutil.copytree(src, out,
                        ignore=shutil.ignore_patterns("__pycache__"),
                        dirs_exist_ok=True)
    for f in _COPY_FILES:
        shutil.copy2(os.path.join(REPO, f), os.path.join(dst, f))
    os.makedirs(os.path.join(dst, "tests"), exist_ok=True)
    shutil.copy2(os.path.join(REPO, _COPY_TEST), os.path.join(dst, _COPY_TEST))

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
    root = tempfile.mkdtemp(prefix="mut_bt_")
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
        build_copy(work, rel, orig.replace(frm, to, 1))

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
