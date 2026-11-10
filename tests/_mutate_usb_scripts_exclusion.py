# 变异测试: 故意把「打包/部署类脚本不上盘」改坏, 看
# tests/test_usb_image_anchors.py 会不会红。
#
# 为什么要有这么个东西: 「测试全绿」本身说明不了什么 —— 断言写错了、
# 写松了、或者压根没跑到, 都是绿的。唯一能证明这测试**咬得动**的办法,
# 就是亲手把代码改坏, 看它是不是真的红。
#
# 这一份守的是 2026-11-10 那条: 产线盘上只留「运行时 + 诊断」脚本,
# 「打包/部署」那几类(build_deb.sh / build_usb_image.sh / verify_deb.sh /
# deploy.sh / install_desktop_icon.sh / prepare_for_firmware.sh /
# find_pi5_label.py / **install_deps.sh**) / probe_rp_gpio_write.py 不上盘。
#
#   ⚠️ 尤其 **install_deps.sh** 不能带: U 盘版缺工具时页面/报错给的是
#      「这版出厂镜像没带齐 ⇒ 改用设备版(deb)安装」⇒ 盘上要是**偏偏躺着**
#      那个脚本, 工人照着 `sudo bash scripts/install_deps.sh` 跑只会白跑
#      (产线取不到公网; 装上了也是装在板子上, 拔盘就走)。
#
# ⚠️ **锚点必须唯一** —— 不唯一的话, replace 会一次改掉好几处, 证据就不成立。
#    `tests/_sweep_anchors.py` 也会静态查这一条。
# ⚠️ **这些改动全部只发生在临时副本里** —— 仓库里的脚本一个字都不动。
#
# 跑法: ./.venv/bin/python tests/_mutate_usb_scripts_exclusion.py
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests._tmp import mkdtemp                       # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(REPO, ".venv", "bin", "python")
TEST_REL = "tests/test_usb_image_anchors.py"
SCRIPT_REL = "scripts/build_usb_image.sh"

#: 副本里必须带的文件 —— 测试自己按「自己所在位置」算 REPO, 缺一个文件
#: 那些锚点检查就会「找不到文件」而报错 ⇒ 每条变异都会「被抓住」但
#: **证据是假的**(抓的是缺文件, 不是抓的改坏)。⇒ 一个都不能少。
NEEDED = [
    TEST_REL,
    SCRIPT_REL,
    "scripts/usb_wrapper.sh.tpl",   # ANCHORS 里有它的三条
    "start.sh",                     # ANCHORS 里有它的五条
]

# 每个变异: (目标文件, 锚点原文, 改坏成什么, 说明)。
# ⚠️ 目标文件那一栏写**字面量**(扫描器用 ast.literal_eval 解析)。
MUTATIONS = [
    (
        "scripts/build_usb_image.sh",
        'if [ "$base" = "$x" ]; then',
        'if [ "$base" != "$x" ]; then',
        "排除的比较**反了**(⇒ 该留的不留、该排的排 ⇒ 「打包/部署类」又上盘)",
    ),
    (
        "scripts/build_usb_image.sh",
        "                skip=1",
        "                skip=0",
        "排除了也**没标记**(⇒ 一个都不排除 ⇒ 整目录原样上盘)",
    ),
    (
        "scripts/build_usb_image.sh",
        """        if [ "$skip" = "1" ]; then
            continue
        fi""",
        """        if [ "$skip" = "1" ]; then
            :
        fi""",
        "标记了但**没跳过**(⇒ 被排的文件照样拷过去)",
    ),
    (
        "scripts/build_usb_image.sh",
        "    install_deps.sh probe_rp_gpio_write.py",
        "    probe_rp_gpio_write.py",
        "排除清单里**少了 install_deps.sh**(⇒ 盘上躺着那个脚本 ⇒ "
        "工人照着跑一条死路)",
    ),
    (
        "scripts/build_usb_image.sh",
        "    install_desktop_icon.sh prepare_for_firmware.sh find_pi5_label.py \\",
        "    install_desktop_icon.sh \\",
        "排除清单里**少了几个打包脚本**(⇒ 下次打包它们又全都回盘上)",
    ),
    (
        "scripts/build_usb_image.sh",
        'copy_tree_except "$PROJECT_ROOT/scripts" "$OUT_DIR/scripts" \\',
        'copy_tree "$PROJECT_ROOT/scripts" "$OUT_DIR/scripts" \\',
        "scripts/ 那一处**改回整目录拷**(⇒ 排除机制整个失效)",
    ),
]


def main():
    print("== 变异测试: 「打包/部署类脚本不上盘」 ==\n")
    escaped = []
    broken = []
    for i, (tgt, old, new, why) in enumerate(MUTATIONS, 1):
        d = mkdtemp(prefix="aging-excl-mut-")
        for rel in NEEDED:
            dst = os.path.join(d, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(os.path.join(REPO, rel), dst)

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
