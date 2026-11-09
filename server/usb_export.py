"""§8.2.3 报告自动存 U 盘 —— 测完就把报告放到 U 盘上, 拔盘走人。

**规矩**(docs/需求设计.md §8.2.3, 定死的, 这里只实现不改动):
  · **自动, 不问工人** —— 工人插了 U 盘, 测完就**主动**把报告放进去;
  · 拷的是**同一份 PDF** —— run 结束 → 导出 PDF → 拷**那一份**。
    **不重新生成**(免得两份对不上, 对不上比不拷更糟);
  · U 盘上的目录 = 根目录下的 `AGING-REPORTS/`;
  · U 盘上的文件名 = `{SN}_{YYYYmmdd_HHMMSS}.pdf` —— **跟板子上那份一模一样**
    ⇒ 两边对得上, 不会认错哪份是哪一轮;
  · 同目录再写一份 `INDEX.csv`(UTF-8 **带 BOM** ⇒ Excel 双击直接开, 不乱码);
  · **没有 U 盘 / 写不进去 ⇒ 只记一条 warning, 绝不阻断测试结果**。

⚠️ **本模块的对外函数绝不抛异常** —— 调用方(runner 收尾、手动重导)是在
   「已经跑完、已经入库」之后调它的, 抛异常会把那一轮掀翻。§8.2 的硬规则:
   报告是**附加产物**, 附加产物出问题**不许**改写测试结论。

⚠️ **「怎么算 U 盘」是最容易写错的一条** —— 判据松了, 报告会写到**板子
   自己**身上(工人拔了 U 盘空手而归), 或者写到**正在被写的盘**上(自伤)。
   三条判据见 `find_report_stick()`。
"""
import asyncio
import csv
import os
import shutil
from typing import Callable, List, NamedTuple, Optional

from . import storage
from .report import fmt_time, status_conclusion

#: U 盘上的报告目录名(根目录下)。
USB_REPORT_DIRNAME = "AGING-REPORTS"

#: 索引文件名 + 表头。**列定死了**(§8.2.3): SN / 起止时间 / 整体状态 /
#: 汇总 / 文件名。
INDEX_FILENAME = "INDEX.csv"
INDEX_HEADER = ("SN", "起止时间", "整体状态", "汇总", "文件名")

#: **系统路径** —— 挂载点落在这儿(或它底下)就是**板子自己**, 不是 U 盘。
#:
#: ⚠️ `/media` 和 `/run/media` **故意不在表里** —— 那正是可移动介质该待的
#:    地方(udisks2 默认就挂到 `/media/<用户>/<卷标>`), 把它们排除掉等于
#:    把这个功能判死。
#:
#: ⚠️ `/` 在表里, 但只**精确匹配** —— 根包含一切, 用前缀匹配会把
#:    `/media/...` 全误判成系统路径(见 `_is_system_path`)。
SYSTEM_MOUNT_PREFIXES = (
    "/", "/bin", "/boot", "/etc", "/home", "/lib", "/opt", "/root",
    "/sbin", "/srv", "/usr", "/var",
)


class UsbStick(NamedTuple):
    """认出来的那一个 U 盘。"""
    mountpoint: str      # 挂载点(如 /media/user/USB)
    device: str          # 挂着的块设备(如 /dev/sda1)
    disk: str            # 内核盘名(如 sda)
    fstype: str          # vfat / ext4 ...


class UsbExportResult(NamedTuple):
    """拷贝结果。**ok=False 绝不意味着测试失败** —— 只是没存成。"""
    ok: bool
    directory: str       # 报告写到哪儿了(ok 时才有)
    filename: str        # 拷过去的文件名
    message: str         # 给工人看的一句话


# ── 「怎么算 U 盘」 ──────────────────────────────────────────────────

def _is_system_path(mountpoint: str) -> bool:
    """这个挂载点是不是**板子自己的系统路径**。

    `/` 只做**精确匹配** —— 根包含一切, 用前缀匹配会把 `/media/...` 全
    误判掉(可移动介质恰恰就挂在那儿)。
    """
    mp = (mountpoint or "").rstrip("/") or "/"
    for p in SYSTEM_MOUNT_PREFIXES:
        if mp == p:
            return True
        if p == "/":
            continue
        if mp.startswith(p + "/"):
            return True
    return False


def _stick_from_mount(m: storage.MountEntry,
                      sys_block: str = storage.SYS_BLOCK) -> Optional[UsbStick]:
    """这个挂载点算不算一个**合格的 U 盘**(三条判据同时满足)。

    抽出来是为了让「找 U 盘」和「认应用自己所在的那根盘」用**同一套**判据 ——
    两处各写一遍, 迟早会出现「导出用的判据」和「导出选中的盘」不一致的情况。
    """
    if not m.is_block_device:
        return None
    if m.readonly:
        return None
    if _is_system_path(m.mountpoint):
        return None
    disk = storage.disk_name(m.device)
    if not disk:
        return None
    if not storage.is_usb_disk(disk, sys_block):
        return None
    if not storage.is_writable(m.mountpoint):
        return None
    return UsbStick(m.mountpoint, m.device, disk, m.fstype)


def find_stick_for_path(path: str,
                         mounts: Optional[List[storage.MountEntry]] = None,
                         sys_block: str = storage.SYS_BLOCK) -> Optional[UsbStick]:
    """这个路径所在的**那个 U 盘**(应用就装在 U 盘上跑的时候用)。

    「U 盘版应用」: 应用、代码、历史库、报告全在 U 盘上 ⇒ 报告**当然也该
    跟着这根盘走** —— 拔盘即走。如果还按「枚举 U 盘取第一个」来选, 插了
    两根盘(一根跑应用、一根给 USB 项写测试)时, 报告可能被拷到**另一根**盘上
    ⇒ 工人拔错盘, 报告没在手边。
    """
    if mounts is None:
        mounts = storage.read_mounts()
    m = storage.mount_point_for_path(path, mounts)
    if not m:
        return None
    return _stick_from_mount(m, sys_block)


def find_report_stick(mounts: Optional[List[storage.MountEntry]] = None,
                      sys_block: str = storage.SYS_BLOCK) -> Optional[UsbStick]:
    """找出「报告该写到哪儿」的那个 U 盘挂载点。

    三条判据**同时**满足(§8.2.3「⚠️ 怎么算 U 盘」):
      ① 挂载点**不在**系统路径里(`/`、`/boot`、`/var`、`/home` … 一律排除);
      ② 挂载它的块设备**挂在 USB 总线上**;
      ③ 文件系统**可写**(只读挂载的一律不算 —— 拷进去也白搭)。

    多个 U 盘时按 sysfs 枚举顺序取**第一个**合格的 —— 结果可预期; 而且
    **实际写到哪个路径会报给工人**(不许悄悄写到另一块盘上)。

    找不到 → None(调用方按「没插 U 盘」处理, **不阻断**)。
    """
    if mounts is None:
        mounts = storage.read_mounts()
    for disk in storage.usb_disks(sys_block):
        for m in storage.mounts_for_disk(disk, mounts):
            stick = _stick_from_mount(m, sys_block)
            if stick is not None:
                return stick
    return None


def _preferred_paths() -> List[str]:
    """「报告优先跟着走」的路径 —— 应用代码根 + 数据根。

    「U 盘版应用」时这两个都在 U 盘上 ⇒ 报告存到**同一根**盘。
    设备版时它们在 eMMC 上 ⇒ 这条不命中, 行为一个字不变。
    """
    out: List[str] = []
    try:
        from .config import Config
    except Exception:
        return out
    for p in (Config.project_root(), Config.data_root()):
        try:
            s = str(p)
        except Exception:
            continue
        if s and s not in out:
            out.append(s)
    return out


# ── 挂载(插着但没挂) ────────────────────────────────────────────────

async def _try_mount_usb_disks(log: Optional[Callable[[str], None]] = None,
                               mounts: Optional[List[storage.MountEntry]] = None,
                               sys_block: str = storage.SYS_BLOCK) -> bool:
    """插着但**没挂**的 U 盘 —— 试着用 udisksctl 挂上(不用 root)。

    这块板子上**没有任何东西会自动挂 U 盘**(没有 udev 自动挂载规则,
    `/run/media` 压根不存在, `/mnt` 是空的)⇒ 不主动挂, 这个功能就永远是
    死的 —— 工人插好 U 盘, 报告照样只落在板子上。

    ⚠️ **只用 udisks, 不用 sudo 兜底** —— 挂载是改共享主机状态的动作, 而
    「挂哪个设备」这件事**没有点名授权过**。TF 卡那套的 sudo 兜底能走,
    是因为 `/dev/mmcblk1p1` 是点名授权过的设备; U 盘没有 ⇒ 不提权。
    """
    from .tests._tools import run_cmd

    disks = storage.usb_disks(sys_block)
    if not disks:
        return False
    mounted_any = False
    for disk in disks:
        if storage.mounts_for_disk(disk, mounts):
            continue            # 这块盘已经挂着(哪怕挂的是只读/系统路径)
        for part in storage.disk_partitions(disk, sys_block):
            dev = f"/dev/{part}"
            if log:
                log(f"[U盘] {dev} 没挂 → 试 udisks 挂载 (不用 root)")
            rc, out, err = await run_cmd(["udisksctl", "mount", "-b", dev],
                                         timeout=20)
            msg = (err or out or f"rc={rc}").strip()
            if log:
                log(f"[U盘] udisksctl mount {dev} rc={rc} · {msg[:200]}")
            if rc == 0:
                mounted_any = True
    return mounted_any


# ── INDEX.csv ───────────────────────────────────────────────────────

def _index_key(row: List[str]) -> str:
    """一行的身份 = **SN + 开始时间**(§8.2.3: 同一条 run 重导时替换那一行)。

    起止时间那一列是「开始 ~ 结束」, 取前半截当开始时间。
    """
    sn = row[0] if row else ""
    span = row[1] if len(row) > 1 else ""
    return f"{sn}|{span.split(' ~ ')[0]}"


def read_index(path: str) -> List[List[str]]:
    """读出 INDEX.csv 现有的行(读不到就当空表)。"""
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            return [r for r in csv.reader(f) if r]
    except (OSError, UnicodeDecodeError, csv.Error):
        return []


def write_index(path: str, rows: List[List[str]]) -> None:
    """把整张索引写回去(临时文件 + os.replace ⇒ 原子, 断电不留半截)。"""
    tmp = path + ".tmp"
    # ⚠️ `utf-8-sig` = **带 BOM** —— Excel 双击直接开不乱码(§8.2.3)。
    with open(tmp, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(list(INDEX_HEADER))
        for r in rows:
            w.writerow(r)
    os.replace(tmp, path)


def update_index(report_dir: str, row: List[str],
                 index_name: str = INDEX_FILENAME) -> str:
    """把一行写进 INDEX.csv —— **追加**语义 + 同一条 run 重导时替换自己那行。

    · 读出已有的行;
    · key 相同(SN + 开始时间)的那一行 → **原地替换**(位置不变, 不会把
      同一轮挪到末尾, 索引的顺序 = 归档顺序);
    · 找不到 → 追加到末尾;
    · **旧的报告永远不删**(§8.2.3: 产线归档是累积的, 清理是工人自己的事)。

    返回索引文件的完整路径。
    """
    path = os.path.join(report_dir, index_name)
    rows = read_index(path)
    # ⚠️ 读回来的**第一行是表头**(写的时候写进去的) ⇒ 写回前要先摘掉,
    #    否则每写一次表头就多一行(第二次写完表头就有两行了)。
    if rows and list(rows[0]) == list(INDEX_HEADER):
        rows = rows[1:]
    key = _index_key(row)
    replaced = False
    for i, old in enumerate(rows):
        if _index_key(old) == key:
            rows[i] = row
            replaced = True
            break
    if not replaced:
        rows.append(row)
    write_index(path, rows)
    return path


def build_index_row(run: dict, filename: str) -> List[str]:
    """INDEX.csv 的一行 —— 列: SN / 起止时间 / 整体状态 / 汇总 / 文件名。

    「整体状态」用的是**报告里同一句中文结论**(`report.status_conclusion`)
    —— 两处各写一份迟早走歪, 纸上和索引里对不上, 工人会以为有一份是错的。

    「汇总」的口径跟报告的通过率那一行**一致**: 通过 X/Y(已测 N 项 /
    共勾选 M 项 —— 未测项不计入)。
    """
    sn = (run.get("sn") or "unknown").strip() or "unknown"
    s = run.get("summary") or {}
    total = s.get("total", 0) or 0
    passed = s.get("passed", 0) or 0
    planned = s.get("planned", 0) or 0
    return [
        sn,
        f"{fmt_time(run.get('started_at'))} ~ {fmt_time(run.get('finished_at'))}",
        status_conclusion(run.get("status") or ""),
        f"通过 {passed}/{total} (已测 {total} 项 / 共勾选 {planned} 项)",
        filename,
    ]


# ── 主流程 ──────────────────────────────────────────────────────────

async def export_report_to_usb(pdf_path: str, run: dict,
                               log: Optional[Callable[[str], None]] = None,
                               mounts: Optional[List[storage.MountEntry]] = None,
                               sys_block: str = storage.SYS_BLOCK
                               ) -> UsbExportResult:
    """把刚导出的那份 PDF 拷到 U 盘, 并更新 INDEX.csv。

    **本函数绝不抛异常** —— 拷不进去只回一个 ok=False 的结果, 由调用方
    记一条 warning。§8.2: 报告是附加产物, **不许**因为它出问题改写测试结论。

    `mounts` / `sys_block` 只给**测试**用(指到假的 /proc/mounts 和假
    sysfs 上)⇒ 生产路径永远走真系统。
    """
    def _log(msg: str) -> None:
        if log:
            try:
                log(msg)
            except Exception:
                pass

    try:
        if not pdf_path:
            return UsbExportResult(False, "", "", "没有 PDF 可拷")
        if not os.path.isfile(pdf_path):
            return UsbExportResult(False, "", "", f"PDF 不在了: {pdf_path}")

        stick = None
        # ① **应用自己就在 U 盘上跑** ⇒ 报告跟着这根盘走(拔盘即走)。
        for p in _preferred_paths():
            stick = find_stick_for_path(p, mounts, sys_block)
            if stick is not None:
                _log(f"[U盘] 应用自己在这根盘上 ⇒ 报告存这儿: {stick.mountpoint}")
                break
        if stick is None:
            # ② 否则按「枚举 U 盘取第一个合格的」来选。
            stick = find_report_stick(mounts, sys_block)
        if stick is None:
            # 插着但没挂? 主动挂一下(不用 root) —— 这板子不自动挂 U 盘。
            if await _try_mount_usb_disks(_log, mounts, sys_block):
                stick = find_report_stick(mounts, sys_block)
        if stick is None:
            return UsbExportResult(
                False, "", "",
                "未检测到 U 盘 —— 报告仍在本机 reports/pdf/")

        report_dir = os.path.join(stick.mountpoint, USB_REPORT_DIRNAME)
        try:
            os.makedirs(report_dir, exist_ok=True)
        except OSError as e:
            return UsbExportResult(
                False, "", "",
                f"U 盘上建不了 {USB_REPORT_DIRNAME}/ 目录: {e}")

        filename = os.path.basename(pdf_path)
        dest = os.path.join(report_dir, filename)
        try:
            shutil.copyfile(pdf_path, dest)
        except OSError as e:
            return UsbExportResult(False, "", "", f"报告拷不进 U 盘: {e}")
        _log(f"[U盘] 报告已拷到 {dest}")

        try:
            update_index(report_dir, build_index_row(run, filename))
        except Exception as e:
            # 索引写不进去 —— **PDF 已经在盘上了**, 不算没存成(只是索引缺
            # 一行)。这时回 ok=True, 但把话说清楚。
            _log(f"[U盘] ⚠ 索引写不进去: {e}")
            return UsbExportResult(
                True, report_dir, filename,
                f"报告已存到 U 盘: {report_dir} (但 INDEX.csv 没写成: {e})")

        return UsbExportResult(
            True, report_dir, filename,
            f"报告已存到 U 盘: {report_dir}")
    except Exception as e:
        # 兜底: 本函数**任何情况下都不许**把调用方的收尾流程掀翻。
        return UsbExportResult(False, "", "", f"存 U 盘失败: {e!r}")
