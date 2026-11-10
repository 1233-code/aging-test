# 变异测试: 故意把「U 盘版部署作业手册」改坏, 看
# tests/test_usb_image_anchors.py 的【作业手册跟脚本对得上】那一节会不会红。
#
# 为什么要有这么个东西: 「测试全绿」本身说明不了什么 —— 断言写错了、
# 写松了、或者压根没跑到, 都是绿的。唯一能证明这测试**咬得动**的办法,
# 就是亲手把东西改坏, 看它是不是真的红。
#
# 这一份守的是: 产线照着 `docs/U盘版部署作业手册.md` 烧盘、照着它验收。
# 手册跟脚本/跟红线各说各话 = 工人照着一份过期的手册干活 ⇒
#   · 把格式写成 FAT ⇒ venv 建不起来(整根盘是废的);
#   · 少写一个「不上盘」的脚本 ⇒ 下一根盘上就躺着那条死命令;
#   · 把验收的期望值写错 ⇒ 工人照着手册验收, 照着错的值签字。
#
# ⚠️ **锚点必须唯一** —— 不唯一的话, replace 会一次改掉好几处, 证据就不成立。
#    `tests/_sweep_anchors.py` 也会静态查这一条。
# ⚠️ **这些改动全部只发生在临时副本里** —— 仓库里的手册一个字都不动。
#
# 跑法: ./.venv/bin/python tests/_mutate_usb_manual.py
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests._tmp import mkdtemp                       # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(REPO, ".venv", "bin", "python")
TEST_REL = "tests/test_usb_image_anchors.py"
DOC_REL = "docs/U盘版部署作业手册.md"

#: 副本里必须带的文件 —— 测试自己按「自己所在位置」算 REPO, 缺一个文件
#: 那些检查就会「找不到文件」而报错 ⇒ 每条变异都会「被抓住」但**证据是假的**
#: (抓的是缺文件, 不是抓的改坏)。⇒ 一个都不能少。
NEEDED = [
    TEST_REL,
    DOC_REL,
    "scripts/build_usb_image.sh",
    "scripts/usb_wrapper.sh.tpl",
    "start.sh",
]

# 每个变异: (目标文件, 锚点原文, 改坏成什么, 说明)。
# ⚠️ 目标文件那一栏写**字面量**(扫描器用 ast.literal_eval 解析)。
MUTATIONS = [
    (
        "docs/U盘版部署作业手册.md",
        "sudo /sbin/mkfs.ext4 -L AGING /dev/sdX      # ② 格式化（/sbin 要写全路径）",
        "sudo /sbin/mkfs.vfat -n AGING /dev/sdX      # ② 格式化（/sbin 要写全路径）",
        "手册把格式写成 **FAT** 了(FAT 没有符号链接/权限位 ⇒ venv 建不起来 ⇒ "
        "整根盘是废的)",
    ),
    (
        "docs/U盘版部署作业手册.md",
        "install_deps.sh\nbuild_deb.sh\nbuild_usb_image.sh",
        "build_deb.sh\nbuild_usb_image.sh",
        "手册里**不再点名** install_deps.sh 不上盘(⇒ 照着手册烧出来的盘上"
        "躺着那个脚本 ⇒ 工人照着一条死命令跑)",
    ),
    (
        "docs/U盘版部署作业手册.md",
        '"mode": "usb"',
        '"mode": "device"',
        "手册里**验收那一条的期望值写错了**(工人照着错的值签字)",
    ),
    (
        "docs/U盘版部署作业手册.md",
        '| 4 | 出厂默认核数 | `grep -m1 "threads:" <盘>/config.yaml` | `threads: 4`',
        '| 4 | 出厂默认核数 | `grep -m1 "threads:" <盘>/config.yaml` | `threads: 8`',
        "手册里**出厂默认核数写成 8**(⇒ 旧盘那个满核值被当成标准 ⇒ "
        "过应力那一档被签字放行)",
    ),
    (
        "docs/U盘版部署作业手册.md",
        '`min="1"`（0 = 「0 = 自动 = 满核」那条路还开着）',
        '`min="0"`（0 = 「0 = 自动 = 满核」那条路还开着）',
        "手册里**旋钮下界写成 0**(⇒ 「0 = 自动 = 满核」那条会把板子压死的路"
        "被当成正常)",
    ),
]


def main():
    print("== 变异测试: U 盘版部署作业手册 ==\n")
    escaped = []
    broken = []
    for i, (tgt, old, new, why) in enumerate(MUTATIONS, 1):
        d = mkdtemp(prefix="aging-manual-mut-")
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
