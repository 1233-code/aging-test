# 变异测试: 故意把「出厂镜像提供」的判据改回**看打包机的 /lib**(2026-11-11
# 查出来的假绿), 看 tests/test_usb_selfcontained_baseline.py 会不会红。
#
# 为什么要有这么个东西: 「测试全绿」本身说明不了什么 —— 断言写错了、
# 写松了、或者压根没跑到, 都是绿的。唯一能证明这测试**咬得动**的办法,
# 就是亲手把代码改坏, 看它是不是真的红。
#
# 这一份守的是: 「这个库在**这台打包机**的 /lib 里」≠「这个库在**出厂镜像**
# 里」⇒ 拿前者当判据 = **假绿** ⇒ 换台板子 fio rc=127 ⇒ **好板子被返修**。
#
# 做法: **每个变异单开一个全新目录**, 把脚本复制进去, 在副本上改一处,
# 用 AGING_USB_SELF_CONTAINED_TOOL 指过去跑测试。
# 被改的那个文件从头到尾**只写一次** —— 全程不覆写任何已存在的文件。
#
# 跑法: .venv/bin/python tests/_mutate_usb_selfcontained_baseline.py
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests._tmp import mkdtemp                       # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(REPO, ".venv", "bin", "python")
TEST = "tests.test_usb_selfcontained_baseline"
TOOL_REL = os.path.join("scripts", "check_usb_selfcontained.py")
# ⚠️ MUTATIONS 里那几处**必须写字面量** —— tests/_sweep_anchors.py 要静态查
#    锚点, 写成变量它就 literal_eval 不了 ⇒ 那条变异从来没被静态验过。

# 每个变异: (文件, from, to, 说明)。from 必须在源文件里**找得到且唯一**。
MUTATIONS = [
    (
        "scripts/check_usb_selfcontained.py",
        "        if pkg in baseline:\n"
        '            return "board", "出厂镜像(%s)" % pkg\n'
        '        return "notimage", "%s(不在出厂镜像基线里)" % pkg',
        '        return "board", "出厂镜像"\n',
        "USB: 判据退回「这台机器上有就算」⇒ **不在基线里的库也判成"
        "「出厂镜像提供」** ⇒ 假绿 ⇒ 换台板子就断",
    ),
    (
        "scripts/check_usb_selfcontained.py",
        "        if image_owner is None or baseline is None:",
        "        if False:",
        "USB: 判据不全时(拿不到基线/归属包)也敢判「出厂镜像提供」⇒ 假绿",
    ),
    (
        "scripts/check_usb_selfcontained.py",
        "    if bad_runpath or missing or notimage:",
        "    if bad_runpath or missing:",
        "USB: 「镜像里没有」这一档**不算问题** ⇒ 退出码 0 ⇒ 坏镜像出厂",
    ),
    (
        "scripts/check_usb_selfcontained.py",
        '            return "unknown", "判据不全(拿不到基线或归属包)⇒ 下不了结论"',
        '            return "notimage", "判据不全(拿不到基线或归属包)⇒ 下不了结论"',
        "USB: 判据不全时**乱扣帽子**说成「有问题」(notimage)⇒ 在产线板子上"
        "跑盘上这份脚本 ⇒ 明明判不了, 却报成「板子坏了」",
    ),
    (
        "scripts/check_usb_selfcontained.py",
        "    if unknown:\n        return 3",
        "    if unknown:\n        return 0",
        "USB: 判据不全(下不了结论)竟退出码 0 ⇒ **假绿** ⇒ 坏镜像出厂",
    ),
]


def main():
    root = mkdtemp(prefix="mut_usbbase_")
    print(f"副本都在 {root} 下面\n")

    bad = 0
    for i, (rel, frm, to, desc) in enumerate(MUTATIONS):
        srcp = os.path.join(REPO, rel)
        with open(srcp, encoding="utf-8") as f:
            orig = f.read()
        if orig.count(frm) != 1:
            print(f"\n✗ {desc}\n   → 锚点出现 {orig.count(frm)} 次(要恰好 1 次): "
                  f"{frm[:70]!r}")
            bad += 1
            continue

        work = os.path.join(root, "m%02d" % i)
        os.makedirs(os.path.join(work, "scripts"), exist_ok=True)
        # 被改的那份**只写一次** —— 全程不覆写任何已存在的文件。
        outp = os.path.join(work, rel)
        with open(outp, "w", encoding="utf-8") as f:
            f.write(orig.replace(frm, to))

        env = dict(os.environ)
        env["AGING_USB_SELF_CONTAINED_TOOL"] = outp
        proc = subprocess.run([PY, "-m", TEST], cwd=REPO, env=env,
                              capture_output=True, text=True, timeout=600)
        out = (proc.stdout or "") + (proc.stderr or "")
        fails = out.count("✗")
        if proc.returncode != 0 and fails == 0:
            print(f"\n✗ {desc}\n   → 红了, 但一个 ✗ 都没有 (exit="
                  f"{proc.returncode}) —— 这是**崩了**, 不算数:\n{out[-600:]}")
            bad += 1
        elif proc.returncode != 0:
            print(f"\n✓ {desc}\n   → 测试变红 (exit={proc.returncode}, "
                  f"{fails} 项失败) —— 咬得动")
        else:
            print(f"\n✗ {desc}\n   → 测试还是绿的! 这个变异没被守住")
            bad += 1

    print("\n" + ("全部变异都被抓住了 ✓" if bad == 0
                  else f"有 {bad} 个变异没被抓住 ✗"))
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
