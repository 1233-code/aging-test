"""回归测试: §8.2.3「报告自动存 U 盘」+ USB 写测试的「别写正在用的盘」。

这个需求拆成两半, 缺一不可:
  ① **报告自动落 U 盘** —— 工人插了 U 盘, 测完报告就在盘上, 拔盘走人;
  ② **USB 写测试别自伤** —— 报告(乃至整个应用)可以**就在 U 盘上**, 拿那块
     盘往死里写 = 一边写报告一边把报告所在的盘擦掉。

⚠️ **「怎么算 U 盘」是最容易写错的一条** —— 判据松了, 报告会写到**板子
自己**身上(工人拔了 U 盘空手而归)。所以这里守的是**三条判据同时满足**:
  ① 挂载点不在系统路径里;
  ② 挂载它的块设备**挂在 USB 总线上**;
  ③ 文件系统可写。

不碰真硬件: sysfs 和 /proc/mounts 都是临时目录里造出来的假文件; 报告写在
临时目录里; **不写仓库里的 reports/ 和 history.db**(那是产线在跑的真数据)。

跑法: .venv/bin/python -m tests.test_usb_export
"""
import asyncio
import csv
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import storage
from server import usb_export as U
from server.tests import usb as UT
from tests._fake_sysfs import make_mounts, make_sysfs
from tests._tmp import mkdtemp

FAILS = []
PASSES = 0


def check(cond, msg, extra=""):
    global PASSES
    if cond:
        PASSES += 1
        print("    ✓ " + msg)
    else:
        FAILS.append(msg)
        print("    ✗ " + msg + ("  → " + str(extra) if extra else ""))


def section(title):
    print("\n" + title)


# ── 假 sysfs / 假 /proc/mounts ──────────────────────────────────────
#
# 搬到 tests/_fake_sysfs.py 了 —— 别的测试也要用, 而**测试脚本是顶层就
# 开跑的脚本**: 从这儿 import 它会把那一整套跑一遍(还会撞上它的
# sys.exit)。所以共用的部分单独放一个干净模块。


RUN = {
    "id": 12,
    "sn": "SN20260812",
    "status": "passed",
    "started_at": 1762310400.0,
    "finished_at": 1762312200.0,
    "summary": {"total": 6, "passed": 6, "failed": 0, "error": 0,
                "skipped": 0, "not_run": 0, "planned": 6},
}


def make_pdf(directory, name="SN20260812_20251105_120000.pdf", body=b"%PDF-1.4 fake"):
    os.makedirs(directory, exist_ok=True)
    p = os.path.join(directory, name)
    with open(p, "wb") as f:
        f.write(body)
    return p


# ════════════════════════════════════════════════════════════════════
section("① storage: 设备名 → 盘名")

check(storage.disk_name("/dev/sda1") == "sda", "sda1 → sda",
      storage.disk_name("/dev/sda1"))
check(storage.disk_name("/dev/sda") == "sda", "sda → sda",
      storage.disk_name("/dev/sda"))
check(storage.disk_name("/dev/mmcblk1p1") == "mmcblk1", "mmcblk1p1 → mmcblk1",
      storage.disk_name("/dev/mmcblk1p1"))
check(storage.disk_name("/dev/nvme0n1p2") == "nvme0n1", "nvme0n1p2 → nvme0n1",
      storage.disk_name("/dev/nvme0n1p2"))
check(storage.disk_name("/dev/root") is None,
      "'/dev/root' 认不出盘名 (根得走 mountinfo 反查)",
      storage.disk_name("/dev/root"))


section("② storage: 认「这块盘在不在 USB 总线上」")

tmp = mkdtemp(prefix="aging-usbexp-")
sysfs = make_sysfs(tmp, usb_disks=("sda",), plain_disks=("mmcblk0",),
                    partitions={"sda": ["sda1"]})

check(storage.is_usb_disk("sda", sysfs), "U 盘 (祖先链里有 usb1) → 是 USB 盘")
check(not storage.is_usb_disk("mmcblk0", sysfs),
      "板载 eMMC (祖先链里没有 usb1) → 不是 USB 盘")
check(not storage.is_usb_disk("sdb", sysfs), "压根不存在的盘 → 不是 USB 盘")
check(storage.usb_disks(sysfs) == ["sda"],
      "usb_disks 只认得出 sda", storage.usb_disks(sysfs))
check(storage.disk_partitions("sda", sysfs) == ["sda1"],
      "sda 的分区是 sda1", storage.disk_partitions("sda", sysfs))

# ⚠️ 判据不能是「路径里有没有 usb 这个**子串**」—— 板子上 USB **控制器**
#    自己就在 `1f0000.usb` 这条路径上, 子串搜法分不清控制器和设备。
ctrl_root = mkdtemp(prefix="aging-usbctrl-")
ctrl = make_sysfs(ctrl_root, plain_disks=())
ctrl_dev = os.path.join(ctrl_root, "sys", "devices", "platform", "soc",
                         "1f0000.usb", "host4", "target4:0:0", "4:0:0:0", "block")
os.makedirs(ctrl_dev, exist_ok=True)
os.symlink(os.path.relpath(os.path.join(ctrl_dev, "sdz"), ctrl),
           os.path.join(ctrl, "sdz"))
check(not storage.is_usb_disk("sdz", ctrl),
      "只在 USB **控制器**路径上(没有 usbN 总线层) → 不算 USB 盘")


section("③ storage: /proc/mounts 与路径 → 挂载点")

mp = make_mounts(tmp, [
    "/dev/root / ext4 rw,relatime",
    "proc /proc proc rw,nosuid,nodev,noexec,relatime",
    "/dev/mmcblk0p5 /userdata ext4 rw,relatime",
    "/dev/sda1 /media/user/USB vfat rw,relatime",
])
mounts = storage.read_mounts(mp)
check(len(mounts) == 4, "解析出 4 条挂载", len(mounts))
check(mounts[3].device == "/dev/sda1" and mounts[3].mountpoint == "/media/user/USB",
      "U 盘那条解析对了", mounts[3])
check(not mounts[2].readonly, "rw 的挂载不是只读")
check(storage.read_mounts(os.path.join(tmp, "没有这个文件")) == [],
      "读不到 /proc/mounts → 空表(不抛)")

hit = storage.mount_point_for_path("/media/user/USB/AGING-REPORTS/x.pdf", mounts)
check(hit is not None and hit.mountpoint == "/media/user/USB",
      "长路径落到最长的那个挂载点上", hit)
check(storage.disk_for_path("/media/user/USB/AGING-REPORTS", mounts) == "sda",
      "U 盘上的路径 → 盘名 sda",
      storage.disk_for_path("/media/user/USB/AGING-REPORTS", mounts))
check(storage.disk_for_path("/userdata/x", mounts) == "mmcblk0",
      "eMMC 上的路径 → 盘名 mmcblk0",
      storage.disk_for_path("/userdata/x", mounts))


section("④ usb_export: 「怎么算 U 盘」的三条判据")

check(not U._is_system_path("/media/user/USB"),
      "/media/user/USB 不是系统路径 (U 盘就挂这儿)")
check(not U._is_system_path("/run/media/user/USB"),
      "/run/media/user/USB 不是系统路径")
check(not U._is_system_path("/mnt/usb"), "/mnt/usb 不是系统路径")
check(U._is_system_path("/"), "/ 是系统路径")
check(U._is_system_path("/boot"), "/boot 是系统路径")
check(U._is_system_path("/var/lib/aging-test"), "/var/lib/aging-test 是系统路径")
check(U._is_system_path("/home/user"), "/home/user 是系统路径")

stick_dir = os.path.join(tmp, "media", "user", "USB")
os.makedirs(stick_dir, exist_ok=True)
mp_found = make_mounts(tmp, [
    "/dev/root / ext4 rw,relatime",
    f"/dev/sda1 {stick_dir} vfat rw,relatime",
])
s = U.find_report_stick(storage.read_mounts(mp_found), sysfs)
check(s is not None and s.disk == "sda",
      "U 盘挂在一个普通目录(不是系统路径) → 认得出来", s)
check(s is not None and s.mountpoint == stick_dir,
      "认出来的是那个挂载点", s)

# ① 判据: 挂载点在系统路径里 → 不认
mp_sys = make_mounts(tmp, [
    "/dev/root / ext4 rw,relatime",
    "/dev/sda1 /var/lib/aging-test vfat rw,relatime",
])
s = U.find_report_stick(storage.read_mounts(mp_sys), sysfs)
check(s is None, "U 盘挂在 /var/lib/aging-test (系统路径) → 不认", s)

# ② 判据: 块设备不在 USB 总线上 → 不认
mp_nonusb = make_mounts(tmp, [
    "/dev/root / ext4 rw,relatime",
    "/dev/mmcblk0p5 /media/user/USB ext4 rw,relatime",
])
s = U.find_report_stick(storage.read_mounts(mp_nonusb), sysfs)
check(s is None, "挂在 /media 下的 eMMC → 不认(不是 USB 总线上的)", s)

# ③ 判据: 只读 → 不认
mp_ro = make_mounts(tmp, [
    "/dev/root / ext4 rw,relatime",
    "/dev/sda1 /media/user/USB vfat ro,relatime",
])
s = U.find_report_stick(storage.read_mounts(mp_ro), sysfs)
check(s is None, "只读挂载 → 不认(拷进去也白搭)", s)


section("⑤ usb_export: 报告拷过去 + INDEX.csv")

# 用一个真的、可写的临时目录当「U 盘挂载点」
usb_mount = os.path.join(tmp, "media_user_USB")
os.makedirs(usb_mount, exist_ok=True)
mp_real = make_mounts(tmp, [
    "/dev/root / ext4 rw,relatime",
    f"/dev/sda1 {usb_mount} vfat rw,relatime",
])
real_mounts = storage.read_mounts(mp_real)

pdf_dir = os.path.join(tmp, "reports")
pdf = make_pdf(pdf_dir)
res = asyncio.run(U.export_report_to_usb(pdf, RUN, mounts=real_mounts,
                                          sys_block=sysfs))
check(res.ok, "拷成功了", res)
check(res.directory == os.path.join(usb_mount, "AGING-REPORTS"),
      "落在 U 盘根下的 AGING-REPORTS/ 里", res.directory)

dest = os.path.join(usb_mount, "AGING-REPORTS", os.path.basename(pdf))
check(os.path.isfile(dest), "PDF 确实拷过去了", dest)
with open(dest, "rb") as f:
    copied = f.read()
with open(pdf, "rb") as f:
    original = f.read()
check(copied == original, "拷过去的和原来那份**一模一样**(不是重新生成的)")

index = os.path.join(usb_mount, "AGING-REPORTS", "INDEX.csv")
check(os.path.isfile(index), "INDEX.csv 也在", index)
with open(index, "rb") as f:
    raw = f.read()
check(raw.startswith(b"\xef\xbb\xbf"), "INDEX.csv 带 UTF-8 BOM (Excel 双击不乱码)",
      raw[:6])
rows = list(csv.reader(open(index, newline="", encoding="utf-8-sig")))
check(rows[0] == list(U.INDEX_HEADER), "表头是定死的那五列", rows[0])
check(len(rows) == 2, "表头 + 一行", len(rows))
check(rows[1][0] == "SN20260812", "第一列 = SN", rows[1])
check(" ~ " in rows[1][1], "第二列 = 起止时间", rows[1][1])
check(rows[1][2] == "✅ 已测项目全部通过",
      "第三列 = 整体状态(跟报告里同一句中文结论)", rows[1][2])
check("通过 6/6" in rows[1][3] and "共勾选 6 项" in rows[1][3],
      "第四列 = 汇总(口径跟报告的通过率一致)", rows[1][3])
check(rows[1][4] == os.path.basename(pdf), "第五列 = 文件名", rows[1][4])

# 第二轮 → 追加
RUN2 = dict(RUN)
RUN2 = {**RUN, "id": 13, "status": "failed", "started_at": 1762314000.0,
        "finished_at": 1762315800.0,
        "summary": {**RUN["summary"], "passed": 5, "failed": 1}}
pdf2 = make_pdf(pdf_dir, "SN20260812_20251105_130000.pdf")
res2 = asyncio.run(U.export_report_to_usb(pdf2, RUN2, mounts=real_mounts,
                                           sys_block=sysfs))
check(res2.ok, "第二轮也拷成功了", res2)
rows = list(csv.reader(open(index, newline="", encoding="utf-8-sig")))
check(len(rows) == 3, "索引变成 3 行(表头 + 两轮)", len(rows))
check(rows[1][4].endswith("120000.pdf") and rows[2][4].endswith("130000.pdf"),
      "两轮按顺序排着", [r[4] for r in rows[1:]])

# 同一轮重导 → **替换**那一行, 不多出一行
RUN["status"] = "stopped"
res3 = asyncio.run(U.export_report_to_usb(pdf, RUN, mounts=real_mounts,
                                           sys_block=sysfs))
check(res3.ok, "同一轮重导也成功", res3)
rows = list(csv.reader(open(index, newline="", encoding="utf-8-sig")))
check(len(rows) == 3, "重导后还是 3 行(没多出一行)", len(rows))
check(rows[1][2] == "🛑 用户中途停止 —— 已完成的项按自身判定, 未跑的不算数",
      "重导那一行的内容**被替换**了(位置不变)", rows[1][2])

# 旧的报告不删
check(os.path.isfile(os.path.join(usb_mount, "AGING-REPORTS",
                                  "SN20260812_20251105_130000.pdf")),
      "旧报告还在(归档是累积的, 不清理)")


section("⑥ usb_export: 没有 U 盘 / 拷不进去 —— 绝不阻断")

mp_none = make_mounts(tmp, ["/dev/root / ext4 rw,relatime"])
res = asyncio.run(U.export_report_to_usb(pdf, RUN,
                                          mounts=storage.read_mounts(mp_none),
                                          sys_block=sysfs))
check(not res.ok, "没有 U 盘 → ok=False", res)
check("未检测到 U 盘" in res.message, "说清楚是「没插 U 盘」", res.message)
check("reports/pdf" in res.message, "告诉工人报告还在本机", res.message)

res = asyncio.run(U.export_report_to_usb(os.path.join(tmp, "没有这个.pdf"), RUN,
                                          mounts=real_mounts, sys_block=sysfs))
check(not res.ok, "PDF 不在了 → ok=False(不抛异常)", res)

res = asyncio.run(U.export_report_to_usb("", RUN, mounts=real_mounts,
                                          sys_block=sysfs))
check(not res.ok, "空路径 → ok=False(不抛异常)", res)

# 挂载点不可写 → ok=False, 不抛
ro_dir = os.path.join(tmp, "ro")
os.makedirs(ro_dir, exist_ok=True)
os.chmod(ro_dir, 0o500)
mp_ro2 = make_mounts(tmp, [f"/dev/sda1 {ro_dir} vfat rw,relatime"])
try:
    res = asyncio.run(U.export_report_to_usb(pdf, RUN,
                                              mounts=storage.read_mounts(mp_ro2),
                                              sys_block=sysfs))
    check(not res.ok, "挂载点不可写 → ok=False(不抛异常)", res)
finally:
    os.chmod(ro_dir, 0o700)


section("⑦ USB 写测试: 别拿「正在用着的盘」当目标")

kept, dropped = UT._split_protected(["/dev/sda1"], {"sda": "报告输出目录"})
check(kept == [] and dropped == ["/dev/sda1"],
      "在用的盘被剔掉", (kept, dropped))
kept, dropped = UT._split_protected(["/dev/sda1", "/dev/sdb1"],
                                     {"sda": "报告输出目录"})
check(kept == ["/dev/sdb1"] and dropped == ["/dev/sda1"],
      "只剔在用的那个, 别的照测", (kept, dropped))
kept, dropped = UT._split_protected(["/dev/sda1"], {})
check(kept == ["/dev/sda1"] and dropped == [],
      "没有在用的盘 → 一个都不剔", (kept, dropped))
kept, dropped = UT._split_protected(["/dev/odd-name"], {"sda": "x"})
check(kept == ["/dev/odd-name"],
      "认不出盘名的设备**保留**(宁可漏剔一个, 也不能全剔掉)", (kept, dropped))

prot = UT._protected_disks()
check(isinstance(prot, dict) and prot, "本机上认出了在用的盘", prot)
rd = storage.root_disk()
if rd:
    check(rd in prot, "根盘在保护名单里", (rd, prot))
check(any("报告" in v or "数据" in v or "应用" in v or "根" in v
          for v in prot.values()),
      "保护名单里说明了各是什么", prot)


section("⑧ 接线: runner / main 真的会去存 U 盘")

import inspect
from server import runner as runner_mod

src = inspect.getsource(runner_mod.TestRunner._export_pdf)
check("_export_to_usb" in src,
      "runner 收尾导完 PDF 会去存 U 盘", src[-300:])
src = inspect.getsource(runner_mod.TestRunner._export_to_usb)
check("usb_export.export_report_to_usb" in src,
      "…调的是 usb_export.export_report_to_usb(不是另写一份)")
src = inspect.getsource(runner_mod.TestRunner._merge_into_parent)
check("_export_to_usb" in src,
      "合并重导父轮那份也会同步到 U 盘"
      "(不然盘上留着的是重测**之前**那版, 跟库里对不上)")

from server import main as main_mod

src = inspect.getsource(main_mod.history_reexport_pdf)
check("usb_export.export_report_to_usb" in src,
      "手动补导 PDF 也会存 U 盘")


# ── 汇总 ─────────────────────────────────────────────────────────────
print("\n" + "=" * 60)
if FAILS:
    print(f"✗ {len(FAILS)} 项失败 / 共 {PASSES + len(FAILS)} 项")
    for f in FAILS:
        print("   - " + f)
    sys.exit(1)
print(f"✓ 全部通过 ({PASSES} 项)")
sys.exit(0)
