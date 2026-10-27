"""TF 卡测试 - 探卡 + 自动挂载 (§6.9)

⚠️ 2026-10-27 重写。原来这一项探的是「**挂载点**在不在」, 探不到就判
skipped + 弹「未检测到 TF 卡, 请插入」—— 那是**错的**:
  · 这台板子上**没有任何东西会自动挂 TF 卡**(没有 udev 自动挂载规则,
    /mnt 是空的, /run/media 压根不存在)⇒ 卡**插着**也永远探不到挂载点;
  · 于是工人看到「未插卡」的框, 插好了点「已插好重测」→ 还是同一个框
    ⇒ **死循环**; 而卡明明插着(产线反馈: 「我现在是插了卡的, 但是没
    识别到」)。

现在探的是「**卡在不在**」—— 也就是**块设备在不在**:
  · 卡在(SD 卡 → 冒出了新的 mmc 设备)→ 工具**自己挂**上再测, 测完
    自己卸(不碰用户自己挂好的桌面挂载);
  · 卡在但挂不上 → status=**error**, 报挂载失败原因, **不弹「请插卡」**
    —— 卡就在槽里, 让人再插一遍是把他引到错的方向;
  · 卡真不在(卡槽空的)→ status=skipped + metrics.no_card, 弹框让工人
    选「已插好重测 / 跳过」。

**「插了卡」和「挂了载」是两件事**, 这一项以前把它们当成一件了。

── 特权边界 (§11.2 + 用户点名授权) ──
挂载/卸载是**改共享主机状态**的动作, 所以范围卡死在用户点名的那个设备
上(板子 31175c3d01850801f56): TF 卡 = /dev/mmcblk1p1, 兜底挂载点 =
/mnt/tfcard。检测逻辑能认出别的卡(换板子、卡插在别的槽位), 但那不在
授权范围内 —— 那种情况只走 udisks(不用 root), 走不通就判 error,
**不擅自提权去挂一个没人授权过的设备**。
"""
import asyncio
import os
import re
import time
from typing import List, NamedTuple, Optional

from .base import TestCase, TestContext, TestResult, register
from ._tools import (
    is_sudo_password_error,
    run_cmd,
    sudo_hint_if_unavailable,
    sudo_nopasswd_hint,
    with_sudo,
)
from .emmc import _run_fio_test


SYS_BLOCK = "/sys/block"
SYS_DEV_BLOCK = "/sys/dev/block"
PROC_MOUNTS = "/proc/mounts"
PROC_MOUNTINFO = "/proc/self/mountinfo"

# 特权挂载的**点名授权范围**: 只有这个具体设备允许走 sudo 兜底挂载。
# 换板子 / 换槽位时这个表要跟着改 —— 没改就不提权, 只走 udisks。
AUTHORIZED_MOUNT_DEVICES = ("/dev/mmcblk1p1",)

# sudo 兜底挂载用的**固定**挂载点 —— 不用随机目录, 因为测完要能确定地
# 把它卸掉(§6.9 的收尾)。
TF_FALLBACK_MOUNT = "/mnt/tfcard"


# ── 探卡 (纯函数, 好测) ─────────────────────────────────────────────

def _read_text(path: str) -> Optional[str]:
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return None


def _read_lines(path: str) -> List[str]:
    txt = _read_text(path)
    if not txt:
        return []
    return [ln for ln in txt.splitlines() if ln.strip()]


def _disk_of_device(device_path: str) -> Optional[str]:
    """'/dev/mmcblk1p1' → 'mmcblk1'; 认不出来(比如 '/dev/root')→ None。"""
    name = os.path.basename(device_path or "")
    m = re.match(r"^(mmcblk\d+)", name)
    return m.group(1) if m else None


def _root_disk() -> Optional[str]:
    """根文件系统所在的那块盘(板载 eMMC)的内核名, 如 'mmcblk0'。

    ⚠️ 不能直接看 /proc/mounts: 这块板子的根挂的是 **'/dev/root'**
    (fstab 里就这么写的, 不是真实设备名), 从名字里认不出是哪块盘。
    走 /proc/self/mountinfo: 根那一行的第 3 个字段是**主:次设备号**
    (如 '179:4'), 拿它去 /sys/dev/block/179:4 反查 —— 那儿是个指向
    ../../block/mmcblk0/mmcblk0p4 的软链, 于是知道根在 mmcblk0p4 上
    ⇒ 盘是 mmcblk0。
    """
    for line in _read_lines(PROC_MOUNTINFO):
        parts = line.split()
        if len(parts) < 10:
            continue
        # mountinfo 字段: id parent maj:min root mountpoint opts ...
        if parts[4] != "/":
            continue
        link = os.path.join(SYS_DEV_BLOCK, parts[2])
        if not os.path.exists(link):
            continue
        disk = _disk_of_device(os.path.basename(os.path.realpath(link)))
        if disk:
            return disk
    return None


def _mmc_disks() -> List[str]:
    """/sys/block 下的 mmc 块设备 (mmcblk0 / mmcblk1 ...)。"""
    try:
        names = os.listdir(SYS_BLOCK)
    except OSError:
        return []
    return sorted(n for n in names if re.match(r"^mmcblk\d+$", n))


def _card_disks(root_disk: Optional[str] = None) -> List[str]:
    """板子上**插着**的 TF 卡对应的块设备名列表 (没插 → 空表)。

    两条判据:
      ① 是 mmc 设备 —— USB 读卡器不算(§6.9 管的是板载 TF 卡槽);
      ② 它的 `device/type` 是 **SD** —— 板载 eMMC 是 **MMC**。
         内核把这两类分得很清: eMMC 焊死在板上, TF 卡是可插拔的 SD 卡。
         这一条比「排除根盘那块」更准 —— 万一哪天根盘不是 eMMC, 也不会
         把板载存储当成 TF 卡去挂(挂坏了很麻烦)。
         type 读不到时(个别内核路径不同)才退回「只要不是根盘那块」。

    root_disk 传 None = 自己去找根盘。
    """
    if root_disk is None:
        root_disk = _root_disk()
    out: List[str] = []
    for name in _mmc_disks():
        if root_disk and name == root_disk:
            continue
        t = _read_text(os.path.join(SYS_BLOCK, name, "device", "type"))
        if t is not None and t.strip().upper() != "SD":
            continue          # 板载 eMMC, 不是 TF 卡
        out.append(name)
    return out


def _disk_partitions(disk: str) -> List[str]:
    """盘上的分区: 'mmcblk1' → ['mmcblk1p1']。没分区 → 空表。"""
    try:
        names = os.listdir(os.path.join(SYS_BLOCK, disk))
    except OSError:
        return []
    return sorted(n for n in names if n.startswith(disk + "p"))


def _find_mount(disks: List[str]) -> Optional[str]:
    """这些盘(或它们的分区)现在挂在哪。没挂 → None。

    按**内核块设备名**比, 不按设备路径 —— udisks 挂出来的是
    /dev/mmcblk1p1, 手工挂的可能写成 /dev/disk/by-uuid/xxx, 但内核里
    是同一个设备。所以先把路径 realpath 一下再取名字。
    """
    want = set(d for d in disks if d)
    if not want:
        return None
    for line in _read_lines(PROC_MOUNTS):
        parts = line.split()
        if len(parts) < 3:
            continue
        device, mountpoint = parts[0], parts[1]
        if not mountpoint.startswith("/"):
            continue
        try:
            real = os.path.realpath(device)
        except OSError:
            real = device
        disk = _disk_of_device(real) or _disk_of_device(device)
        if disk in want:
            return mountpoint
    return None


def _is_readonly(mountpoint: str) -> bool:
    """这个挂载点是不是只读的。"""
    for line in _read_lines(PROC_MOUNTS):
        parts = line.split()
        if len(parts) < 4:
            continue
        if parts[1] == mountpoint:
            return "ro" in parts[3].split(",")
    return False


def _mount_authorized(dev: str) -> bool:
    """这个设备允不允许走**特权**挂载。

    挂载是改共享主机状态的动作, 所以特权那一步只对点名过的具体设备
    开放(见 AUTHORIZED_MOUNT_DEVICES)。不在名单里的设备 —— 换板子、
    换槽位、或者探卡逻辑认错了 —— 一律**不提权**, 只走 udisks 那条
    不用 root 的路。
    """
    return dev in AUTHORIZED_MOUNT_DEVICES


# 挂载失败有两种性质, 判定口径**完全不同**:
#   · **卡本身坏了** → failed(不合格, 换卡)—— 产线上最常见的就是这种
#     (坏卡/假卡的 NTFS 索引是烂的, ntfs-3g 直接报 corrupt);
#   · **环境不行** → error(这一项没法测, 查环境 / 重测)—— 没装 ntfs
#     驱动、没权限、polkit 不放行、超时……
# §9.1: failed = 不满足任一通过条件(「挂载成功」是 TF 项的通过条件之一);
#        error  = 实现/环境本身异常。**工具缺失是「这一项没法测」, 不是
#        「机器有病」; 但卡里的文件系统是烂的, 那就是「卡有病」。**
_CARD_DEFECT_MARKERS = (
    "corrupt",            # ntfs-3g: Corrupt index block signature
    "invalid argument",   # ntfs-3g: ..._fixup_warn: ...: Invalid argument
    "magic:",             # ntfs-3g: 卷引导扇区的 magic 对不上
    "bad superblock",
    "damaged",
)


def _is_card_defect(reason: str) -> bool:
    """挂载失败是不是**卡本身坏了**(而不是环境问题)。

    卡坏了 → 判 failed(这张卡不合格, 换一张); 环境不行 → 判 error。
    """
    low = (reason or "").lower()
    return any(m in low for m in _CARD_DEFECT_MARKERS)


async def _wait_for_dev(dev: str, timeout_sec: float = 3.0) -> bool:
    """等 /dev 下的设备节点出现。

    内核一插卡就有块设备了, 但 udev 建 /dev 节点可能慢零点几秒 —— 卡是
    刚插上就点「已插好重测」的时候, 节点可能还没出来。那种时候说「没插
    卡」是冤枉人, 所以先等一等。
    """
    t0 = time.time()
    while True:
        if os.path.exists(dev):
            return True
        if time.time() - t0 >= timeout_sec:
            return False
        await asyncio.sleep(0.2)


# ── 挂载 / 卸载 ─────────────────────────────────────────────────────

class _MountInfo(NamedTuple):
    path: Optional[str]      # 挂载点(成功才有)
    error: Optional[str]     # 失败原因(失败才有)
    method: str              # 'existing' / 'udisks' / 'sudo' —— 决定测完要不要卸


async def _ensure_mounted(ctx, disk: str, dev: str) -> _MountInfo:
    """确保卡挂上了。返回挂载点, 挂不上就返回失败原因。

    三步, 顺序不能换:
      ① **已经挂着** → 直接用(method='existing', 测完**不卸** ——
        那是用户自己挂的, 我们没资格替他卸);
      ② `udisksctl mount -b <dev>` —— 不碰 root、不弹密码, 桌面会话里
        最干净的一条路(udisks 的 polkit 规则 filesystem-mount 是
        allow_active=yes);
      ③ `sudo -n mount <dev> /mnt/tfcard` 兜底 —— udisks 挂不上(或者
        服务不在活动桌面会话里, polkit 不放行)时用这条, 但**只对
        AUTHORIZED_MOUNT_DEVICES 里的设备**(§11.2 点名授权)。
    """
    existing = _find_mount([disk])
    if existing:
        await ctx.on_log(f"[TF] ✓ 卡已经挂在 {existing}, 直接用 (测完不动它)")
        return _MountInfo(existing, None, "existing")

    # ② udisks —— 不用 root
    await ctx.on_log(f"[TF] 卡在槽里但没挂载 → 试 udisks 挂载 (不用 root): {dev}")
    rc, out, err = await run_cmd(["udisksctl", "mount", "-b", dev], timeout=20)
    udisks_msg = (err or out or f"rc={rc}").strip()
    await ctx.on_log(f"[TF] udisksctl mount rc={rc} · {udisks_msg[:200]}")
    m = _find_mount([disk])
    if m:
        await ctx.on_log(f"[TF] ✓ udisks 挂载成功: {m}")
        return _MountInfo(m, None, "udisks")

    # ③ sudo 兜底 —— 只对点名过的设备提权
    if not _mount_authorized(dev):
        await ctx.on_log(
            f"[TF] udisks 没挂上, 而 {dev} 不在特权挂载的授权范围内"
            f" {AUTHORIZED_MOUNT_DEVICES} → 不提权, 判挂载失败"
        )
        return _MountInfo(
            None,
            f"udisks 挂载失败({udisks_msg[:160]}), "
            f"且 {dev} 不在特权挂载授权范围内({', '.join(AUTHORIZED_MOUNT_DEVICES)})",
            "",
        )

    await ctx.on_log(
        f"[TF] udisks 没挂上 → 兜底: sudo -n mount (固定挂到 {TF_FALLBACK_MOUNT})"
    )
    rc1, out1, err1 = await run_cmd(
        with_sudo(["mkdir", "-p", TF_FALLBACK_MOUNT]), timeout=10
    )
    rc2, out2, err2 = await run_cmd(
        with_sudo(["mount", dev, TF_FALLBACK_MOUNT]), timeout=30
    )
    m = _find_mount([disk])
    if m:
        await ctx.on_log(f"[TF] ✓ sudo 挂载成功: {m}")
        return _MountInfo(m, None, "sudo")

    # 两条路都挂了 → 把**两边**的原因都带上。udisks 的报错是
    # 「Unknow error when mounting ...」这种废话, 真正的原因在
    # mount.ntfs 的输出里 —— 只报 udisks 那句的话, 连「卡是不是坏了」
    # 都判断不出来。
    sudo_msg = (err2 or out2 or err1 or out1 or f"rc={rc2}").strip()
    reason = f"udisks: {udisks_msg[:160]} | sudo mount: {sudo_msg[:300]}"
    if is_sudo_password_error(sudo_msg):
        reason = sudo_nopasswd_hint(sudo_msg)
    return _MountInfo(None, reason, "")


async def _unmount(ctx, disk: str, dev: str, path: str, method: str) -> None:
    """测完把**我们自己挂的**卡卸下来。

    之前就挂着的(method='existing')**不动** —— 那是用户自己的挂载。
    卸载失败只记日志, 不影响本次判定 —— 卡的读写结果已经拿到了,
    卸不干净是收尾问题, 不是这一项的结论。
    """
    if not method or method == "existing":
        return
    if method == "udisks":
        cmd = ["udisksctl", "unmount", "-b", dev]
    else:
        # 特权卸载只碰那个固定挂载点 —— 跟挂载时是同一个名字, 不会卸错。
        cmd = with_sudo(["umount", TF_FALLBACK_MOUNT])
    rc, out, err = await run_cmd(cmd, timeout=20)
    if rc == 0:
        await ctx.on_log(f"[TF] ✓ 已卸载 {path} (测完自动收拾)")
    else:
        await ctx.on_log(
            f"[TF] ⚠ 卸载 {path} 失败 (不影响本次结果): rc={rc} "
            f"{(err or out or '').strip()[:200]}"
        )


async def _remove_test_file(ctx, path: str) -> None:
    """删掉测试文件。

    普通删不掉的时候(挂载点是 root 建的, 文件归 root)再用 sudo 删一次
    —— 留着几十 MB 的垃圾在客户的卡上不像话。
    """
    try:
        if os.path.exists(path):
            os.unlink(path)
        return
    except OSError:
        pass
    rc, out, err = await run_cmd(with_sudo(["rm", "-f", path]), timeout=15)
    if rc != 0:
        await ctx.on_log(
            f"[TF] ⚠ 测试文件没删掉 ({path}): {(err or out or '').strip()[:120]}"
        )


@register
class TFCardTest(TestCase):
    name = "tfcard"
    display_name = "TF 卡"
    description = "探卡(看卡在不在)· 自己挂载自己收拾 · 卡槽空才弹提示"

    def expected_seconds(self, test_config: dict) -> Optional[float]:
        # duration_sec 是**每个阶段**的时长, 一轮 = 写 + 读 = ×2
        times = max(1, int(test_config.get("times", 1)))
        duration = max(10, int(test_config.get("duration_sec", 30)))
        return times * duration * 2

    async def run(self, ctx: TestContext) -> TestResult:
        result = TestResult()
        t_cfg = ctx.test_config
        times = max(1, int(t_cfg.get("times", 1)))
        min_read = float(t_cfg.get("min_read_mbps", 10))

        await ctx.on_log("[TF] 探卡 (看的是**卡在不在**, 不是挂载点在不在)")
        await ctx.on_progress(5, "探卡")

        # === 步骤 1: 探卡 ——
        disks = _card_disks()
        if not disks:
            # 卡槽是空的 → 停下来等工人插卡。这才是「料没到位」。
            await ctx.on_log("[TF] ❌ 没找到 TF 卡设备 (卡槽是空的)")
            await ctx.on_log("[TF] 提示 UI 弹窗: 工人可选「已插好(重测)」或「跳过」")
            result.status = "skipped"
            result.metrics["no_card"] = True
            result.metrics["skipped_reason"] = "no_card"
            result.metrics["detection_method"] = "block_device_probe"
            result.error = "未检测到 TF 卡 (卡槽是空的, 等待工人选择)"
            await ctx.on_progress(100, "等待工人选择")
            return result

        disk = disks[0]
        parts = _disk_partitions(disk)
        dev = f"/dev/{parts[0]}" if parts else f"/dev/{disk}"
        result.metrics["device"] = dev
        result.metrics["detection_method"] = "block_device_probe"
        await ctx.on_log(f"[TF] ✓ 探到 TF 卡: {disk} (设备 {dev})")

        if not await _wait_for_dev(dev):
            # 卡在, 但 /dev 节点没出来 —— 这是「卡还没就绪」, 不是「没插卡」
            result.status = "error"
            result.error = (
                f"检测到 TF 卡 ({disk}), 但设备节点 {dev} 还没出现 (udev 没建好) —— "
                "请拔插一次 TF 卡后重测"
            )
            await ctx.on_log(f"[TF] ❌ {result.error}")
            await ctx.on_progress(100, "设备未就绪")
            return result

        # === 步骤 2: 挂载 (卡在但没挂 —— 工具自己挂) ===
        await ctx.on_progress(10, "挂载")
        mi = await _ensure_mounted(ctx, disk, dev)
        if not mi.path:
            # 卡在槽里却挂不上 → 两种性质, 判定完全不同(§9.1):
            #   · 卡本身坏了(corrupt/坏块)→ **failed**: 这张卡不合格, 换卡;
            #   · 环境不行(没驱动/没权限)→ error: 这一项没法测, 查环境重测。
            # ⚠️ 两种都**不能**弹「请插卡」—— 卡就在槽里, 让人再插一遍是
            # 把他引到错的方向(这正是原来那个 bug 的另一半)。
            result.metrics["mount_failed"] = True
            if _is_card_defect(mi.error):
                result.status = "failed"
                result.error = (
                    f"检测到 TF 卡 ({dev}), 但**卡里的文件系统是坏的**, 挂不上 —— "
                    f"这张卡不合格, 请换一张。详情: {mi.error}"
                )
            else:
                result.status = "error"
                result.error = f"检测到 TF 卡 ({dev}), 但挂载失败: {mi.error}"
            await ctx.on_log(f"[TF] ❌ {result.error}")
            await ctx.on_progress(100, "挂载失败")
            return result

        mount = mi.path
        result.metrics["mount_method"] = mi.method
        if _is_readonly(mount):
            # 挂上了但写不进去 → 判 error(重测), 不是 failed:
            # NTFS 上次没正常卸载(dirty)会让 udisks 只读挂载, **卡本身
            # 未必坏**, 让工人先在电脑上安全弹出再重插更合适。
            result.status = "error"
            result.error = (
                f"TF 卡挂上了但是**只读** ({mount}) —— 写不进去, 测不了。"
                "多半是 NTFS 上次没正常卸载(dirty), 请在电脑上安全弹出"
                "(而不是直接拔卡)后重插再测"
            )
            await ctx.on_log(f"[TF] ❌ {result.error}")
            await ctx.on_progress(100, "只读挂载")
            await _unmount(ctx, disk, dev, mount, mi.method)
            return result

        # 探到卡才需要写权限 —— §11.2 命令级 sudo -n (挂载点写入需 root)
        if not sudo_hint_if_unavailable(result, "TF 卡"):
            await ctx.on_log(f"[TF] ✗ {result.error}")
            await _unmount(ctx, disk, dev, mount, mi.method)
            return result

        await ctx.on_log(f"[TF] ✓ 挂载点: {mount}")
        await ctx.on_progress(15, f"挂载点 {mount}")

        # === 步骤 3: 跑 fio 测试 ===
        # duration_sec: 写、读各自跑满多少秒。TF 卡可能很慢 (低速卡 / 接触不良
        # 正是要挑出来的那类), 所以工作集比 eMMC 小, 但**时长照跑** ——
        # 以前这里写死 64MB 且不带 --runtime, 一张卡几秒就测完了, 配的时长形同虚设。
        size_mb = max(16, int(t_cfg.get("size_mb", 64)))
        duration = max(10, int(t_cfg.get("duration_sec", 30)))
        round_sec = duration * 2
        test_file = os.path.join(mount, ".aging_test_tf.bin")
        all_passed = True
        await ctx.on_log(
            f"[TF] {times} 轮 × (写 {duration}s + 读 {duration}s)"
            f" = 约 {round_sec * times}s, 工作集 {size_mb}MB"
        )
        await ctx.on_progress(15, f"准备写入 · 预计 {round_sec * times}s")

        try:
            for i in range(times):
                if await ctx.is_cancelled():
                    result.status = "stopped"
                    all_passed = False
                    break

                round_idx = i + 1
                await ctx.on_log(f"[TF] 第 {round_idx}/{times} 轮")

                async def tick(phase: str, phase_elapsed: float, phase_total: float,
                               i=i, round_idx=round_idx):
                    done_sec = (0 if phase == "write" else phase_total) + phase_elapsed
                    frac = min(1.0, done_sec / round_sec)
                    await ctx.on_progress(
                        int(15 + (i + frac) / times * 85),
                        f"第 {round_idx}/{times} 轮 · {'写' if phase == 'write' else '读'}"
                        f" {phase_elapsed:.0f}s / {phase_total:.0f}s",
                    )

                t0 = time.time()
                m = await _run_fio_test(
                    test_file, size_mb, ctx, duration,
                    on_metric=ctx.on_metric, on_progress=tick,
                )
                elapsed = time.time() - t0

                read_mbps = m.get("read_mbps", 0)
                write_mbps = m.get("write_mbps", 0)
                await ctx.on_log(
                    f"[TF] 读 {read_mbps:.1f} MB/s, 写 {write_mbps:.1f} MB/s"
                    f" (耗时 {elapsed:.1f}s)"
                )

                if read_mbps < min_read:
                    await ctx.on_log(f"[TF] ❌ 读速度 {read_mbps:.1f} < {min_read} MB/s")
                    all_passed = False

                result.metrics[f"round_{round_idx}_read_mbps"] = read_mbps
                result.metrics[f"round_{round_idx}_write_mbps"] = write_mbps
                if not all_passed:
                    break
        except FileNotFoundError:
            result.status = "error"
            result.error = "未安装 fio"
            await ctx.on_progress(100, "缺 fio")
            return result
        except Exception as e:
            result.status = "error"
            result.error = f"异常: {e!r}"
            await ctx.on_progress(100, "异常")
            return result
        finally:
            await _remove_test_file(ctx, test_file)
            # 我们自己挂的 → 测完自己卸; 之前就挂着的 → 不动 (§6.9 收尾)
            await _unmount(ctx, disk, dev, mount, mi.method)

        if all_passed and result.status == "pending":
            result.status = "passed"
        elif result.status == "pending":
            result.status = "failed"

        await ctx.on_progress(100, "完成")
        return result

    def operator_prompt(self, result: TestResult):
        """**卡槽是空的** → 停下来等工人插卡 (§7.3)。

        卡槽空不是设备缺陷, 也不是「测完了」—— 它是「料没到位」。
        工人只有两个选择: 插上卡重测, 或者跳过。选完之前整轮必须停住,
        否则下一项会在他犹豫的时候开跑, 屏幕就跑到别的地方去了。

        ⚠️ 只有「卡槽真的空的」(metrics.no_card)才弹。卡在槽里但挂不上
        的那种是 error, 不弹 —— 弹了就是让工人去干一件没用的事(他插了
        也一样挂不上)。
        """
        if result.status == "skipped" and (result.metrics or {}).get("no_card"):
            return {"text": "未检测到 TF 卡(卡槽是空的)。请插入 TF 卡后选「已插好重测」, 或选「跳过」。"}
        return None
