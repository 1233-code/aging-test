"""存储定位 —— 认盘、认挂载点、认「这块盘在不在 USB 总线上」。

只**读** /proc/mounts、/proc/self/mountinfo、/sys/block —— 不碰 sudo、
不改任何系统状态。给两处用:
  · `server/usb_export.py` —— 找「哪个挂载点才是 U 盘」(§8.2.3);
  · `server/tests/usb.py` —— 把「板子自己正在用的盘」从 USB 写测试的目标里
    剔掉(往正在用的盘上 fio = 把系统写坏)。

**为什么单独一个模块**: 这两处都要「路径 → 块设备」「块设备 → 是不是 USB」
这套换算, 各写一遍迟早走歪 —— 而这两种走歪都是**不可逆**的:
  · USB 判宽了 ⇒ 报告写到**板子自己**身上, 工人拔了 U 盘空手而归;
  · USB 判宽了 ⇒ USB 写测试拿**系统盘/报告盘**当目标, 写坏机器。

**所有读系统状态的函数都把路径做成参数**(默认是真路径) ⇒ 测试拿一棵假
sysfs + 假 /proc/mounts 就能跑, **不碰真硬件**。
"""
import os
import re
from typing import List, NamedTuple, Optional

SYS_BLOCK = "/sys/block"
SYS_DEV_BLOCK = "/sys/dev/block"
PROC_MOUNTS = "/proc/mounts"
PROC_MOUNTINFO = "/proc/self/mountinfo"


class MountEntry(NamedTuple):
    """/proc/mounts 的一行。"""
    device: str          # /dev/sda1 (可能是 /dev/disk/by-uuid/xxx 这种软链)
    mountpoint: str      # 挂载点
    fstype: str          # vfat / ext4 / tmpfs ...
    options: str         # rw,relatime ... (含 "ro" 就是只读)

    @property
    def readonly(self) -> bool:
        return "ro" in self.options.split(",")

    @property
    def is_block_device(self) -> bool:
        """是不是挂着一个**真正的块设备**(排除 tmpfs / overlay / proc …)。"""
        return self.device.startswith("/dev/")


# ── /proc/mounts ────────────────────────────────────────────────────

def _unescape(s: str) -> str:
    """/proc/mounts 用 \\040 这种八进制转义表示空格等字符。"""
    out = []
    i = 0
    while i < len(s):
        c = s[i]
        if c == "\\" and i + 3 < len(s):
            try:
                out.append(chr(int(s[i + 1:i + 4], 8)))
                i += 4
                continue
            except ValueError:
                pass
        out.append(c)
        i += 1
    return "".join(out)


def read_mounts(path: str = PROC_MOUNTS) -> List[MountEntry]:
    """读 /proc/mounts。读不到就当没有挂载(不抛)。"""
    try:
        with open(path) as f:
            lines = f.read().splitlines()
    except OSError:
        return []
    out: List[MountEntry] = []
    for line in lines:
        parts = line.split()
        if len(parts) < 4:
            continue
        out.append(MountEntry(
            _unescape(parts[0]),
            _unescape(parts[1]),
            _unescape(parts[2]),
            parts[3],
        ))
    return out


# ── 设备名 → 盘名 ───────────────────────────────────────────────────

#: 分区名 → 盘名 的还原规则。
#:   /dev/sda1 → sda;  /dev/mmcblk1p1 → mmcblk1;  /dev/nvme0n1p2 → nvme0n1
_DISK_PATTERNS = (
    re.compile(r"^(mmcblk\d+)p\d+$"),
    re.compile(r"^(nvme\d+n\d+)p\d+$"),
    re.compile(r"^(mmcblk\d+)$"),
    re.compile(r"^(nvme\d+n\d+)$"),
    re.compile(r"^(sd[a-z]+)\d+$"),
    re.compile(r"^(sd[a-z]+)$"),
    re.compile(r"^(hd[a-z]+)\d+$"),
    re.compile(r"^(hd[a-z]+)$"),
    re.compile(r"^(vd[a-z]+)\d+$"),
    re.compile(r"^(vd[a-z]+)$"),
    re.compile(r"^(xvd[a-z]+)\d+$"),
    re.compile(r"^(xvd[a-z]+)$"),
)


def disk_name(device: str) -> Optional[str]:
    """'/dev/mmcblk1p1' → 'mmcblk1'; '/dev/sda1' → 'sda'; 认不出 → None。"""
    name = os.path.basename(str(device or "").rstrip("/"))
    if not name:
        return None
    for pat in _DISK_PATTERNS:
        m = pat.match(name)
        if m:
            return m.group(1)
    return None


# ── sysfs: 这块盘在不在 USB 总线上 ───────────────────────────────────

#: USB 总线在 sysfs 里就叫 `usbN` (usb1 / usb2 …)。
#:
#: ⚠️ **不能拿「路径里有没有 usb 这个子串」当判据** —— 板子上 USB **控制器**
#:    自己就在 `platform/soc/1f0000.usb` 这条路径上, 子串搜法分不清
#:    「USB 控制器」和「挂在 USB 上的设备」。`usbN` 这个名字才是总线本身。
_USB_BUS_NAME = re.compile(r"^usb\d+$")


def _sysfs_ancestors(disk: str, sys_block: str = SYS_BLOCK) -> List[str]:
    """块设备在 sysfs 里的祖先链(从块设备自己往上)。"""
    base = os.path.join(sys_block, str(disk or ""))
    try:
        real = os.path.realpath(base)
    except OSError:
        return []
    out = []
    path = real
    for _ in range(16):
        out.append(path)
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
    return out


def _is_usb_subsystem(link: str) -> bool:
    """这个目录的 `subsystem` 软链是不是指向 usb (/sys/class/usb)。"""
    if not os.path.islink(link):
        return False
    try:
        target = os.path.realpath(link)
    except OSError:
        return False
    return target.endswith(os.sep + "class" + os.sep + "usb")


def is_usb_disk(disk: str, sys_block: str = SYS_BLOCK) -> bool:
    """这块盘是不是**挂在 USB 总线上**的。

    两条判据, 满足任一即可:
      ① 祖先链里有目录名是 `usbN` —— 那就是 USB 总线本身;
      ② 祖先链里有目录的 `subsystem` 指向 `/sys/class/usb` —— 那是个 USB 设备。

    ⚠️ 判据 ② 在**假 sysfs** 上不好造(软链要能解析), 判据 ① 简单可靠 ——
    两条都留着, 哪条先命中算哪条。
    """
    for p in _sysfs_ancestors(disk, sys_block):
        if _USB_BUS_NAME.match(os.path.basename(p)):
            return True
        if _is_usb_subsystem(os.path.join(p, "subsystem")):
            return True
    return False


def usb_disks(sys_block: str = SYS_BLOCK) -> List[str]:
    """所有挂在 USB 总线上的**整盘**(内核名, 如 'sda')。按名字排序 ⇒ 可预期。

    ⚠️ **分区不算盘** —— 分区(``sda1`` / ``mmcblk0p1``)的祖先链跟整盘
    一模一样, 判「在不在 USB 上」的结果也一模一样 ⇒ 一并列出来等于把
    同一块盘数了两遍。整盘才拿去挂载(mount 一个分区才有意义)。
    """
    try:
        names = sorted(os.listdir(sys_block))
    except OSError:
        return []
    disks = [n for n in names if is_usb_disk(n, sys_block)]
    whole = []
    for n in disks:
        is_partition = False
        for d in disks:
            if d == n:
                continue
            if (n.startswith(d) and n[len(d):].isdigit()) or \
               (n.startswith(d + "p") and n[len(d) + 1:].isdigit()):
                is_partition = True
                break
        if not is_partition:
            whole.append(n)
    return whole


# ── 路径 → 挂载点 / 块设备 ──────────────────────────────────────────

def _read_lines(path: str) -> List[str]:
    try:
        with open(path) as f:
            return [ln for ln in f.read().splitlines() if ln.strip()]
    except OSError:
        return []


def root_disk(mountinfo: str = PROC_MOUNTINFO,
              sys_dev_block: str = SYS_DEV_BLOCK) -> Optional[str]:
    """根文件系统所在的那块盘(内核名, 如 'mmcblk0')。

    ⚠️ **不能直接看 /proc/mounts** —— 这块板子的根挂的是 **'/dev/root'**
    (fstab 里就那么写, 不是真实设备名), 从名字里认不出是哪块盘。
    走 /proc/self/mountinfo: 根那一行的第 3 个字段是**主:次设备号**
    (如 '179:4'), 拿它去 /sys/dev/block/179:4 反查 —— 那儿是个指向
    ../../block/mmcblk0/mmcblk0p4 的软链 ⇒ 盘是 mmcblk0。
    """
    for line in _read_lines(mountinfo):
        parts = line.split()
        if len(parts) < 10:
            continue
        if parts[4] != "/":
            continue
        link = os.path.join(sys_dev_block, parts[2])
        if not os.path.exists(link):
            continue
        try:
            target = os.path.realpath(link)
        except OSError:
            continue
        d = disk_name(os.path.basename(target))
        if d:
            return d
    return None


def mount_point_for_path(path: str,
                         mounts: Optional[List[MountEntry]] = None) -> Optional[MountEntry]:
    """这个路径落在哪个挂载点上(**最长匹配**)。找不到 → None。"""
    if mounts is None:
        mounts = read_mounts()
    try:
        p = os.path.abspath(str(path or ""))
    except (TypeError, ValueError):
        return None
    best: Optional[MountEntry] = None
    for m in mounts:
        mp = m.mountpoint
        if p == mp or p.startswith(mp.rstrip("/") + "/"):
            if best is None or len(mp) > len(best.mountpoint):
                best = m
    return best


def block_device_for_path(path: str,
                          mounts: Optional[List[MountEntry]] = None) -> Optional[str]:
    """这个路径落在哪个**块设备**上。返回 /dev/xxx(已解析软链); 不是块设备 → None。"""
    m = mount_point_for_path(path, mounts)
    if not m or not m.is_block_device:
        return None
    try:
        return os.path.realpath(m.device)
    except OSError:
        return m.device


def disk_for_path(path: str,
                  mounts: Optional[List[MountEntry]] = None) -> Optional[str]:
    """这个路径落在哪块**盘**上(如 'sda')。不是块设备 → None。

    ⚠️ 根挂的是 `'/dev/root'`(fstab 里就那么写)时, 名字里认不出盘 ——
    那时走 mountinfo 反查(见 `root_disk`)。
    """
    m = mount_point_for_path(path, mounts)
    if not m:
        return None
    d = disk_name(m.device)
    if d:
        return d
    if m.mountpoint == "/":
        return root_disk()
    return None


def mounts_for_disk(disk: str,
                    mounts: Optional[List[MountEntry]] = None) -> List[MountEntry]:
    """这块盘(或它的分区)现在挂在哪。"""
    if mounts is None:
        mounts = read_mounts()
    out = []
    for m in mounts:
        if disk_name(m.device) == disk:
            out.append(m)
    return out


def disk_partitions(disk: str, sys_block: str = SYS_BLOCK) -> List[str]:
    """盘上的分区(内核名): 'sda' → ['sda1', 'sda2']; 'mmcblk0' → ['mmcblk0p1']。

    没有分区 → 空表(有些盘整盘就是一个文件系统)。
    """
    try:
        names = os.listdir(os.path.join(sys_block, str(disk or "")))
    except OSError:
        return []
    pat = re.compile(re.escape(str(disk or "")) + r"p?\d+$")
    return sorted(n for n in names if pat.match(n))


def is_writable(path: str) -> bool:
    """这个目录能不能写(还要能进去 —— 只判 W_OK 会漏掉 x 位)。"""
    try:
        return os.access(path, os.W_OK | os.X_OK)
    except OSError:
        return False
