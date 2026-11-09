# 变异测试: 故意把「U 盘镜像的两道闸门」改坏, 看 tests/test_usb_image_cpu_gate.py
# 会不会红。
#
# 为什么要有这么个东西: 「测试全绿」本身说明不了什么 —— 断言写错了、
# 写松了、或者压根没跑到, 都是绿的。唯一能证明这测试**咬得动**的办法,
# 就是亲手把代码改坏, 看它是不是真的红。
#
# 这一份守的是 2026-11-09 那次事故:
#   ① 出厂断言 —— 镜像**不许**能压到**超过核数**(2026-11-09 晚口径改了:
#      原来是「不许 7 核以上」; 现在上限放回满核 ⇒ 断言守的是「配置写坏」
#      + 一条满核警告; ⚠️ **2026-11-09 晚更晚: 出厂默认改成 4 核之后, 那条
#      满核警告改看「用户拧得到几核」(min(页面旋钮 max, 代码硬上限))**);
#   ② console script 闸门 —— start.sh 不许直接调 venv 里的 console script
#      (shebang 写死构建路径 ⇒ 换设备必坏)。
#
# ⚠️ **锚点必须唯一** —— 不唯一的话, replace 会一次改掉好几处, 证据就不成立。
#    `tests/_sweep_anchors.py` 也会静态查这一条(⇒ 目标文件那一栏要写**字面量**,
#    写成变量的话扫描器解析不出来会直接崩)。
# ⚠️ **这些改动全部只发生在临时副本里** —— 仓库里的脚本一个字都不动。
#
# 跑法: ./.venv/bin/python tests/_mutate_usb_image_cpu_gate.py
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests._tmp import mkdtemp                       # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(REPO, ".venv", "bin", "python")
TEST_REL = "tests/test_usb_image_cpu_gate.py"
SCRIPT_REL = "scripts/build_usb_image.sh"

# 每个变异: (目标文件, 锚点原文, 改坏成什么, 说明)。
# ⚠️ 目标文件那一栏写**字面量**(扫描器用 ast.literal_eval 解析)。
MUTATIONS = [
    (
        "scripts/build_usb_image.sh",
        'if [ -n "$_bad" ]; then',
        'if true; then',
        "console script 闸门恒为真(就是原来那个 `| head` 的坑)"
        "(⇒ 每次打包都在最后一步死, 新镜像出不来 ⇒ 产线一直用旧盘)",
    ),
    (
        "scripts/build_usb_image.sh",
        """        | grep -nE '\\$VENV_DIR"?/bin/' \\""",
        """        | grep -nE '\\$VENV_DIR/bin/' \\""",
        "闸门的 pattern 不认带引号的写法(真实 start.sh 里就是 `\"$VENV_DIR\"/bin/…`)"
        "(⇒ 闸门没有牙齿, 真调了 console script 也不拦)",
    ),
    (
        "scripts/build_usb_image.sh",
        """        | grep -v '/bin/python' | head -5)\"""",
        """        | head -5)\"""",
        "闸门把 `bin/python3 -m uvicorn` 也当成 console script(误伤 ⇒ 又变回打不出镜像)",
    ),
    (
        "scripts/build_usb_image.sh",
        'if [ "$_eff" -gt 8 ] 2>/dev/null; then',
        'if [ "$_eff" -gt 99 ] 2>/dev/null; then',
        "出厂断言的阈值放到 99 ⇒ 连「比核数还多」的镜像都能打出来",
    ),
    (
        "scripts/build_usb_image.sh",
        'if [ "$_reach" -ge 8 ] 2>/dev/null; then',
        'if false; then',
        "满核那一档不再警告(⇒ 产线以为 8 核跟 6 核一样, 直接按默认开跑)",
    ),
    (
        "scripts/build_usb_image.sh",
        'if [ "$_code_cap" -gt 8 ] 2>/dev/null; then',
        'if false; then',
        "代码里的硬上限写坏了(比核数还多)也不拦",
    ),
    (
        "scripts/build_usb_image.sh",
        """| grep -oE 'max="[0-9]+"'""",
        """| grep -oE 'max="[0-9][0-9]+"'""",
        "读不出页面上的旋钮上限(退回硬上限 ⇒ 旋钮只到 4 核的镜像也会误报满核)",
    ),
    (
        "scripts/build_usb_image.sh",
        'if [ "$_code_cap" -gt 0 ] 2>/dev/null && [ "$_code_cap" -lt "$_eff" ]; then _eff=$_code_cap; fi',
        ':',
        "出厂断言忽略代码里的硬上限 ⇒ 配置写 8 核就按 8 核算",
    ),
    (
        "scripts/build_usb_image.sh",
        'if [ "$_code_cap" -eq 0 ] 2>/dev/null; then',
        'if false; then',
        "没有硬上限时不再警告(旋钮一改就能压到 8 核)",
    ),
    (
        "scripts/build_usb_image.sh",
        'cat > "$OUT_DIR/版本.txt" <<EOF',
        'cat > /dev/null <<EOF',
        "版本戳不生成(产线那边又分不清手上这根盘是哪一版)",
    ),
]


def main():
    print("== 变异测试: U 盘镜像的两道闸门 ==\n")
    escaped = []
    broken = []
    for i, (tgt, old, new, why) in enumerate(MUTATIONS, 1):
        d = mkdtemp(prefix="aging-gate-mut-")
        # 副本必须带上测试自己要的布局(它按自己所在位置算 REPO)
        os.makedirs(os.path.join(d, "tests"))
        os.makedirs(os.path.join(d, "scripts"))
        shutil.copy2(os.path.join(REPO, SCRIPT_REL), os.path.join(d, SCRIPT_REL))
        shutil.copy2(os.path.join(REPO, TEST_REL), os.path.join(d, TEST_REL))

        target = os.path.join(d, tgt)
        with open(target, encoding="utf-8") as f:
            txt = f.read()
        n = txt.count(old)
        if n != 1:
            print("  ⚠ 变异 %d: 锚点出现 %d 次(要求唯一)⇒ 证据不成立 —— %s"
                  % (i, n, why))
            broken.append(why)
            continue
        with open(target, "w", encoding="utf-8") as f:
            f.write(txt.replace(old, new))

        r = subprocess.run([PY, os.path.join(d, TEST_REL)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            print("  ✓ 变异 %d 被抓住: %s" % (i, why))
        else:
            print("  ✗ 变异 %d **逃了**: %s" % (i, why))
            escaped.append(why)

    print("")
    if broken:
        print("⚠️ %d 条变异因为锚点不唯一没验成(证据不成立)" % len(broken))
    if escaped:
        print("✗ %d 条变异逃了(测试咬不动)" % len(escaped))
        for e in escaped:
            print("    - " + e)
        return 1
    print("✅ 全部 %d 条变异都被抓住了" % len(MUTATIONS))
    return 0


if __name__ == "__main__":
    sys.exit(main())
