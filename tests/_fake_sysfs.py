"""测试用的**假 sysfs / 假 /proc/mounts**。

为什么单独放一个文件: 这些假数据的造法被好几个测试用
(`test_usb_export.py` / `test_usb_app_mode.py` …), 而测试脚本本身是
**顶层就开跑**的脚本 —— 从别的测试里 `import` 它会把那一整套跑一遍,
还会撞上它的 `sys.exit()`。所以共用的部分搬到这里, 谁来 import 都干净。

⚠️ **这些假 sysfs 不碰真硬件** —— 全在临时目录里造, 跑完自动删。
"""
import os


def make_sysfs(base, usb_disks=(), plain_disks=(), partitions=None):
    """造一棵假 sysfs。

    · `usb_disks` 里的盘, 祖先链里**有** `usbN` 这一层(真 USB 总线);
    · `plain_disks` 里的盘, 祖先链里**没有**(板载 eMMC 那种);
    · `partitions` = {盘名: [分区名]}。

    返回 **`/sys/block` 那个目录**(跟 `storage.SYS_BLOCK` 一个层级)。
    """
    partitions = partitions or {}
    sysfs = os.path.join(base, "sys")
    block = os.path.join(sysfs, "block")
    os.makedirs(block, exist_ok=True)
    os.makedirs(os.path.join(sysfs, "class", "usb"), exist_ok=True)

    def _chain(name):
        if name in usb_disks:
            return ["platform", "soc", "1f0000.usb", "usb1", "1-1", "1-1:1.0",
                    "host4", "target4:0:0", "4:0:0:0", "block"]
        return ["platform", "soc", "mmc_host", "mmc0", "mmc0:0001", "block"]

    for name in list(usb_disks) + list(plain_disks):
        chain = _chain(name)
        devdir = os.path.join(sysfs, "devices", *chain)
        os.makedirs(devdir, exist_ok=True)
        target = os.path.join(devdir, name)
        os.makedirs(target, exist_ok=True)
        link = os.path.join(block, name)
        if not os.path.exists(link):
            os.symlink(os.path.relpath(target, block), link)
        # 分区: <block>/<disk><n> → ../devices/.../block/<disk>/<disk><n>
        for part in partitions.get(name, []):
            pdir = os.path.join(target, part)
            os.makedirs(pdir, exist_ok=True)
            plink = os.path.join(block, part)
            if not os.path.exists(plink):
                os.symlink(os.path.relpath(pdir, block), plink)
    return block


def make_mounts(base, lines):
    """造假 /proc/mounts 文件, 返回路径。"""
    p = os.path.join(base, "mounts")
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return p
