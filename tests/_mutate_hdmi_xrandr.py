# 变异测试(后端): 故意把「HDMI 基线判定」改坏, 看 tests/test_hdmi_xrandr.py
# 会不会红。
#
# 为什么要有这么个东西: 「测试全绿」本身说明不了什么 —— 断言写错了、
# 写松了、或者压根没跑到, 都是绿的。唯一能证明这测试**咬得动**的办法,
# 就是亲手把代码改坏, 看它是不是真的红。
#
# 2026-10-28: 基线那三态判定被抽成了 `_probe_output`(§6.7.3 的拔插轮要
# **复用**它), 抽函数最容易把原来那些「碰巧对」的细节改掉 —— 所以这一项
# 也得有一份变异清单盯着。
#
# ⚠️ 只在**副本**上改 —— 仓库里 server/ 是产线在跑的那份, 改坏了没人知道。
#
# 跑法: .venv/bin/python tests/_mutate_hdmi_xrandr.py
import os
import shutil
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(REPO, ".venv", "bin", "python")
TEST = "tests.test_hdmi_xrandr"

MUTATIONS = [
    (
        "server/tests/hdmi.py",
        '        if probe["case"] == "no_xrandr":\n'
        '            result.metrics["current_resolution"] = "(未验证)"\n'
        '        elif probe["case"] == "no_output":\n'
        '            result.metrics["current_resolution"] = "(xrandr 无对应输出口)"',
        '        if probe["case"] == "no_xrandr":\n'
        '            result.metrics["current_resolution"] = "(未验证)"\n'
        '        elif probe["case"] == "no_output":\n'
        '            result.metrics["current_resolution"] = "(未验证)"',
        "HDMI: 「xrandr 里没有对应输出口」也写成「未验证」 (两种情况在报告上分不开了)",
    ),
    (
        "server/tests/hdmi.py",
        '        if xr_state == "mismatch":\n'
        '            result.status = "failed"',
        '        if xr_state == "mismatch":\n'
        '            result.status = "passed"',
        "HDMI: **真矛盾**(sysfs 说连着、xrandr 说没输出)也放过 (没点亮的屏放行)",
    ),
    (
        "server/tests/hdmi.py",
        '        cands = ([o for o in xr["outputs"] if o["name"].upper().startswith(prefix)]\n'
        '                 if prefix else [])',
        '        cands = ([o for o in xr["outputs"] if o["name"].upper() == prefix]\n'
        '                 if prefix else [])',
        "HDMI: 按**全名**认口而不是前缀 (sysfs 叫 card0-HDMI-A-1、xrandr 叫 HDMI-1, "
        "必然对不上 → 真矛盾被静默降级放行)",
    ),
    (
        "server/tests/hdmi.py",
        '            if (cand["w"] == tw and cand["h"] == th\n'
        '                    and abs(cand["refresh"] - tr) <= 1.0):',
        '            if (cand["w"] == tw and cand["h"] == th\n'
        '                    and abs(cand["refresh"] - tr) <= 0.001):',
        "HDMI: 刷新率不留 ±1Hz 容差 (59.96 对 60 永远判不一致)",
    ),
    (
        "server/tests/hdmi.py",
        '        if not active["modes"]:\n'
        '            result.status = "failed"',
        '        if not active["modes"]:\n'
        '            result.status = "passed"',
        "HDMI: connected 但 EDID 报不出 mode 也放过 (坏口/坏屏测不出来)",
    ),
    (
        "server/tests/hdmi.py",
        '    if not os.environ.get("DISPLAY", "").strip():\n'
        '        return None',
        '    if False:\n'
        '        return None',
        "HDMI: 没 DISPLAY 也照样 exec xrandr (无桌面会话的板子上会拖住整项)",
    ),
    (
        "server/tests/hdmi.py",
        '            state = "mismatch"\n'
        '            case = "no_mode"',
        '            state = "unavailable"\n'
        '            case = "no_mode"',
        "HDMI: 「矛盾」降级成「查不到」(真矛盾被当读数不准放行)",
    ),
    (
        "server/tests/hdmi.py",
        '        target_res_only = f"{tw}x{th}"',
        '        target_res_only = target_mode',
        "HDMI: 拿 `1920x1080@60` 去做 EDID 支持列表的 `in` (sysfs 的 modes "
        "**不带刷新率**, 这个指标会永远 False)",
    ),
]


def run_one(tmp):
    env = dict(os.environ)
    env["PYTHONPATH"] = tmp
    proc = subprocess.run(
        [PY, "-m", TEST],
        cwd=tmp, env=env, capture_output=True, text=True, timeout=300,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def main():
    tmp = tempfile.mkdtemp(prefix="mut_hdmi_base_")
    shutil.copytree(os.path.join(REPO, "server"), os.path.join(tmp, "server"),
                    ignore=shutil.ignore_patterns("__pycache__"))
    os.makedirs(os.path.join(tmp, "tests"), exist_ok=True)
    shutil.copy2(os.path.join(REPO, "tests", "test_hdmi_xrandr.py"),
                 os.path.join(tmp, "tests", "test_hdmi_xrandr.py"))
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
