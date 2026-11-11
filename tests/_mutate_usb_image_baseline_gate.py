# 变异测试: 故意把 U 盘版打包链的「基线闭包」那道闸改坏, 看
# tests/test_usb_image_baseline_gate.py 会不会红。
#
# 为什么要有这么个东西: 「测试全绿」本身说明不了什么 —— 断言写错了、
# 写松了、或者压根没跑到, 都是绿的。唯一能证明这个测试**咬得动**的办法,
# 就是亲手把代码改坏, 看它是不是真的红。
#
# 这一份守的是: U 盘版打包链那道「这个库在**别的板子**上还找得到吗」的闸
#   ① **基线**(`deb/image-baseline.txt`)不在 ⇒ 停手, 不是只警告;
#   ② 退出码 **3 = 下不了结论**要跟 **1 = 真问题**分开 ⇒ 别把「判不了」
#      说成「板子坏了」(工人看到就判废 ⇒ **好板子被返修**);
#   ③ 检查脚本压根不在 ⇒ 也要停手(以前是 warn ⇒ 坏镜像照样出厂)。
#
# 做法: **每个变异单开一个全新目录**, 把脚本复制进去, 在副本上改一处,
# 用 AGING_BUILD_USB_SH 指过去跑测试。
# 被改的那个文件从头到尾**只写一次** —— 全程不覆写任何已存在的文件。
#
# 跑法: .venv/bin/python tests/_mutate_usb_image_baseline_gate.py
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests._tmp import mkdtemp                       # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(REPO, ".venv", "bin", "python")
TEST = "tests.test_usb_image_baseline_gate"
TOOL_REL = "scripts/build_usb_image.sh"
# ⚠️ MUTATIONS 里那几处**必须写字面量** —— tests/_sweep_anchors.py 要静态查
#    锚点, 写成变量它就 literal_eval 不了 ⇒ 那条变异从来没被静态验过。

# 每个变异: (文件, from, to, 说明)。from 必须**找得到且唯一**。
MUTATIONS = [
    (
        "scripts/build_usb_image.sh",
        'if [ ! -f "$USB_BASELINE" ]; then\n    die "找不到出货镜像基线',
        'if [ ! -f "$USB_BASELINE" ]; then\n    warn "找不到出货镜像基线',
        "基线不在时只**警告**不拦 ⇒ 判据缺一半照样出镜像 ⇒ **假绿** ⇒ "
        "坏镜像出厂",
    ),
    (
        "scripts/build_usb_image.sh",
        '    if [ "$_rc" = "3" ]; then\n        die "「换台板子还能用吗」这一关'
        '**下不了结论**',
        '    if [ "$_rc" = "3" ]; then\n        die "「换台板子还能用吗」这一关'
        '**板子坏了**',
        "退出码 3 的消息改成「板子坏了」⇒ 工人/排障的人照着判废 ⇒ "
        "**好板子被返修**",
    ),
    (
        "scripts/build_usb_image.sh",
        '    if [ "$_rc" = "3" ]; then',
        '    if [ "$_rc" = "1" ]; then',
        "把「下不了结论(3)」的判断改成「真问题(1)」⇒ 两档混了 ⇒ "
        "真问题被当成判不了放过去",
    ),
    (
        "scripts/build_usb_image.sh",
        'else\n    die "找不到 scripts/check_usb_selfcontained.py ⇒ 「换台板子」这一关',
        'else\n    warn "找不到 scripts/check_usb_selfcontained.py ⇒ 「换台板子」这一关',
        "检查脚本压根不在时只**警告**⇒ 这一关压根没跑 ⇒ 坏镜像照样出厂",
    ),
]


def main():
    root = mkdtemp(prefix="mut_usbbasegate_")
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
        os.makedirs(os.path.join(work, os.path.dirname(rel)), exist_ok=True)
        # 被改的那份**只写一次** —— 全程不覆写任何已存在的文件。
        outp = os.path.join(work, rel)
        with open(outp, "w", encoding="utf-8") as f:
            f.write(orig.replace(frm, to))

        env = dict(os.environ)
        env["AGING_BUILD_USB_SH"] = outp
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
