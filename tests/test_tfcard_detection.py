"""回归测试: TF 卡项「插了卡却认不出来」(§6.9, 2026-10-27)

起因 (产线提的): 「tf卡的检测是否插了卡是有问题吧, 我现在是插了卡的, 但是没
识别到」。

根因: 这一项探的是「**挂载点**在不在」(`/proc/mounts` 里有没有 mmcblk1 的
挂载点), 探不到就判 skipped + 弹「未检测到 TF 卡, 请插入」。可是**这台板子
上根本没有任何东西会自动挂 TF 卡**(没有 udev 自动挂载规则, /mnt 是空的,
/run/media 压根不存在)⇒
  · 卡**插着**也永远探不到挂载点;
  · 工人插好了点「已插好重测」→ 还是同一个框 ⇒ **死循环**;
  · 而卡明明插着 —— 弹的那句话本身是错的。

改法: 探「**卡在不在**」(SD 卡的块设备在不在), 挂载交给工具自己(udisks
优先, sudo 兜底), 测完自己卸。**卡槽真的空的**才弹框; **卡在但挂不上**判
error 并且**不弹「请插卡」**(卡就在槽里, 让工人再插一遍是把他引到错的方向)。

这里守的是:
  ① 探卡认的是**块设备**, 而且认得出 eMMC(MMC)和 SD 卡(SD)的区别;
  ② 卡槽空 → skipped + no_card + 弹框;
  ③ 卡在但挂不上 → error + **不弹框**;
  ④ 卡在且挂上了 → 真跑 fio, 测完**把工具自己挂的卸掉**(用户自己挂的不动);
  ⑤ 特权挂载只对**点名授权的那个设备**开放。

不碰真硬件: /sys/block、/proc/mounts、/proc/self/mountinfo 全指向临时目录
里造出来的假文件; 挂载和 fio 也都是假的。最后一节在**真板子**上只读地
看一眼探卡结果(不挂载、不写盘)。

跑法: .venv/bin/python -m tests.test_tfcard_detection
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.tests import tfcard as F
from server.tests.base import TestResult

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


# ── 假文件系统: 一棵 /sys/block + /proc ─────────────────────────────

class FakeBoard:
    """按给定的盘表造一棵假的 /sys/block + /proc/mounts + /proc/self/mountinfo。

    disks  = {'mmcblk0': 'MMC', 'mmcblk1': 'SD'}   (盘名 → device/type)
    parts  = {'mmcblk1': ['mmcblk1p1']}            (盘 → 分区名)
    mounts = ['/dev/mmcblk1p1 /media/x ntfs rw,relatime 0 0']  (/proc/mounts 的行)
    root   = 'mmcblk0'                             (根文件系统所在的盘)
    """

    def __init__(self, disks, parts=None, mounts=None, root=None):
        self.root = tempfile.mkdtemp(prefix="tf_fake_")
        self.disks = disks
        self.parts = parts or {}
        self.mounts = mounts or []
        self.root_disk = root

        self.sys_block = os.path.join(self.root, "sys", "block")
        self.sys_dev_block = os.path.join(self.root, "sys", "dev", "block")
        self.proc_mounts = os.path.join(self.root, "proc", "mounts")
        self.proc_mountinfo = os.path.join(self.root, "proc", "self", "mountinfo")
        self._build()

    def _build(self):
        os.makedirs(self.sys_block, exist_ok=True)
        for name, dev_type in self.disks.items():
            dev = os.path.join(self.sys_block, name, "device")
            os.makedirs(dev, exist_ok=True)
            with open(os.path.join(dev, "type"), "w") as f:
                f.write(dev_type + "\n")
            for p in self.parts.get(name, []):
                os.makedirs(os.path.join(self.sys_block, name, p), exist_ok=True)

        if self.root_disk:
            # 根盘的挂载点写的是 '/dev/root'(不是真设备名) —— 必须靠
            # mountinfo 里的主:次设备号反查, 这正是板子上的实际情况。
            os.makedirs(self.sys_dev_block, exist_ok=True)
            os.symlink(
                os.path.join(self.root, "fake", "block", self.root_disk + "p4"),
                os.path.join(self.sys_dev_block, "179:4"),
            )
            os.makedirs(os.path.dirname(self.proc_mountinfo), exist_ok=True)
            with open(self.proc_mountinfo, "w") as f:
                f.write(
                    "17 1 179:4 / / rw,relatime shared:1 - ext4 /dev/root rw\n"
                )

        os.makedirs(os.path.dirname(self.proc_mounts), exist_ok=True)
        with open(self.proc_mounts, "w") as f:
            # ⚠️ /proc/mounts 的格式是 **device mountpoint fstype options
            # dump pass** —— fstype 少写一列, options 就跑到第 4 位去了,
            # 「是不是只读」就永远判不出来。
            f.write("proc /proc proc rw,relatime 0 0\n")
            for m in self.mounts:
                f.write(m + "\n")

    def install(self):
        """把这棵树接到模块上 (测完还原)。"""
        self._saved = (F.SYS_BLOCK, F.SYS_DEV_BLOCK,
                       F.PROC_MOUNTS, F.PROC_MOUNTINFO)
        F.SYS_BLOCK = self.sys_block
        F.SYS_DEV_BLOCK = self.sys_dev_block
        F.PROC_MOUNTS = self.proc_mounts
        F.PROC_MOUNTINFO = self.proc_mountinfo

    def restore(self):
        (F.SYS_BLOCK, F.SYS_DEV_BLOCK,
         F.PROC_MOUNTS, F.PROC_MOUNTINFO) = self._saved


# ── 假 ctx / 假的挂载与 fio ──────────────────────────────────────────

class FakeCtx:
    def __init__(self, test_config=None):
        self.config = {}
        self.test_config = test_config or {}
        self.logs = []
        self.progress = []

    async def on_log(self, line): self.logs.append(line)

    async def on_progress(self, pct, msg): self.progress.append((pct, msg))

    async def on_metric(self, k, v): pass

    async def is_cancelled(self): return False


class Patch:
    """把模块里的几个函数换掉, 退出时还原。"""

    def __init__(self, **kw):
        self.kw = kw
        self.saved = {}

    def __enter__(self):
        for k, v in self.kw.items():
            self.saved[k] = getattr(F, k)
            setattr(F, k, v)
        return self

    def __exit__(self, *exc):
        for k, v in self.saved.items():
            setattr(F, k, v)
        return False


async def fake_wait_for_dev(dev, timeout_sec=3.0):
    return True


def fake_fio(metrics=None):
    async def _run(test_file, size_mb, ctx, duration_sec,
                  on_metric=None, on_progress=None):
        return dict(metrics or {"read_mbps": 50.0, "write_mbps": 30.0})
    return _run


# ══════════════════════════════════════════════════════════════════════
section("【1】探卡: 认的是「卡在不在」, 而且分得清 eMMC 和 SD 卡")

b = FakeBoard(
    disks={"mmcblk0": "MMC", "mmcblk1": "SD"},
    parts={"mmcblk1": ["mmcblk1p1"]},
    root="mmcblk0",
)
b.install()
try:
    cards = F._card_disks()
    check(cards == ["mmcblk1"],
          "只认出 SD 卡 (mmcblk1), 板载 eMMC (mmcblk0, type=MMC) 不算卡",
          cards)
    check("mmcblk0" not in cards, "根盘那块 (eMMC) 一定不在卡的列表里", cards)
    check(F._disk_partitions("mmcblk1") == ["mmcblk1p1"],
          "分区认得出: mmcblk1 → mmcblk1p1", F._disk_partitions("mmcblk1"))
    check(F._find_mount(["mmcblk1"]) is None,
          "没挂载时 _find_mount 返回 None (而不是编一个点)")
finally:
    b.restore()

# 卡拔了
b = FakeBoard(disks={"mmcblk0": "MMC"}, root="mmcblk0")
b.install()
try:
    check(F._card_disks() == [], "卡拔了 → 卡的列表是空的", F._card_disks())
finally:
    b.restore()

# type 读不到(个别内核路径不同) → 退回「只要不是根盘那块」
b = FakeBoard(disks={"mmcblk0": "MMC", "mmcblk1": "SD"}, root="mmcblk0")
os.remove(os.path.join(b.sys_block, "mmcblk1", "device", "type"))
b.install()
try:
    cards = F._card_disks()
    check("mmcblk0" not in cards,
          "读不到 type 时退回按根盘排除, 根盘仍然不会被当成卡", cards)
finally:
    b.restore()

section("【2】挂载点查找 + 只读判定")

b = FakeBoard(
    disks={"mmcblk0": "MMC", "mmcblk1": "SD"},
    mounts=["/dev/mmcblk1p1 /media/User/TF ntfs rw,relatime 0 0"],
    root="mmcblk0",
)
b.install()
try:
    check(F._find_mount(["mmcblk1"]) == "/media/User/TF",
          "挂载中的卡能查到挂载点", F._find_mount(["mmcblk1"]))
    check(F._is_readonly("/media/User/TF") is False,
          "rw 挂载 → 不是只读")
    check(F._find_mount(["mmcblk2"]) is None,
          "没插的那张盘查不到挂载点")
finally:
    b.restore()

b = FakeBoard(
    disks={"mmcblk0": "MMC", "mmcblk1": "SD"},
    mounts=["/dev/mmcblk1p1 /media/User/TF ntfs ro,relatime 0 0"],
    root="mmcblk0",
)
b.install()
try:
    check(F._is_readonly("/media/User/TF") is True,
          "ro 挂载 → 认得出只读 (NTFS 没正常卸载就是这种)")
finally:
    b.restore()

section("【3】特权挂载只对点名授权的设备开放")

check(F._mount_authorized("/dev/mmcblk1p1") is True,
      "点名授权的设备 /dev/mmcblk1p1 允许走特权挂载")
check(F._mount_authorized("/dev/mmcblk2p1") is False,
      "没授权的设备不走特权挂载 (换板子/换槽位时不擅自提权)")
check(F._mount_authorized("/dev/mmcblk0") is False,
      "板载 eMMC 绝不在特权挂载名单里")


# ══════════════════════════════════════════════════════════════════════
section("【4】项级行为: 卡槽空 vs 卡在但挂不上 vs 卡在且挂上了")

TFC = F.TFCardTest()


async def run_item(mount_info, disks=None):
    """跑一次 TF 卡项, 挂载层和 fio 层都是假的。

    ⚠️ **不替换 `_unmount`** —— 那个函数自己就知道「用户自己挂的不卸」,
    把它换掉就等于把这个判断绕过去了。改成替换更底层的 `run_cmd`,
    记录真正发出去的命令 —— 这样验的是「到底有没有发卸载命令」。
    """
    if disks is None:
        disks = ["mmcblk1"]

    async def _ensure(ctx, disk, dev):
        return mount_info

    cmds = []

    async def fake_run_cmd(cmd, timeout=5.0, input_data=None):
        cmds.append(list(cmd))
        return 0, "", ""

    removed = []

    async def _remove(ctx, path):
        removed.append(path)

    with Patch(_ensure_mounted=_ensure, run_cmd=fake_run_cmd,
               _remove_test_file=_remove, _wait_for_dev=fake_wait_for_dev,
               _run_fio_test=fake_fio(),
               sudo_hint_if_unavailable=lambda result, name: True):
        # 探卡: 直接返回指定的盘 (不依赖真板子)
        with Patch(_card_disks=lambda root_disk=None: list(disks)):
            ctx = FakeCtx({"times": 1, "duration_sec": 10, "min_read_mbps": 10})
            res = await TFC.run(ctx)
            return res, ctx, cmds, removed


# ① 卡槽是空的
res, ctx, cmds, removed = asyncio.run(
    run_item(F._MountInfo(None, "x", ""), disks=[]))
check(res.status == "skipped", "卡槽空 → skipped (不是 failed, 也不是 error)",
      res.status)
check(res.metrics.get("no_card") is True,
      "卡槽空 → metrics.no_card = True", res.metrics)
check(res.metrics.get("skipped_reason") == "no_card",
      "卡槽空 → metrics.skipped_reason = 'no_card' (界面横幅认这个)", res.metrics)
check("no_mount" not in res.metrics,
      "旧的那个指标 no_mount 已经不用了 (它说的是「挂载点」, 不是「卡」)",
      res.metrics)
prompt = TFC.operator_prompt(res)
check(prompt is not None and "TF 卡" in prompt.get("text", ""),
      "卡槽空 → 弹框让工人选「已插好重测 / 跳过」", prompt)

# ② 卡在, 但挂不上 —— 分两种性质, 判定不同 (§9.1)
res, ctx, cmds, removed = asyncio.run(
    run_item(F._MountInfo(None, "mount: wrong fs type", "")))
check(res.status == "error",
      "卡在但**环境**挂不上 → error (这一项没法测, 查环境重测)", res.status)
check(res.metrics.get("mount_failed") is True,
      "卡在但挂不上 → metrics.mount_failed = True", res.metrics)
check(res.metrics.get("no_card") is None,
      "卡在但挂不上 → **不**置 no_card (卡明明在)", res.metrics)
check(TFC.operator_prompt(res) is None,
      "卡在但挂不上 → **不弹「请插卡」** (卡就在槽里, 让工人再插一遍是"
      "把他引到错的方向)")
check("挂载失败" in (res.error or ""), "报错里写明是挂载失败", res.error)

# ②b 卡里的文件系统是烂的 → failed(这张卡不合格, 换卡), 不是 error
res, ctx, cmds, removed = asyncio.run(run_item(F._MountInfo(
    None,
    "udisks: Unknow error | sudo mount: ntfs_mst_post_read_fixup_warn: "
    "magic: 0x43425355 ...: Invalid argument / Corrupt index block signature",
    "")))
check(res.status == "failed",
      "卡在但**卡本身坏了**(corrupt)→ failed (不合格, 换卡)", res.status)
check("换一张" in (res.error or ""),
      "卡坏了 → 报错里明说「这张卡不合格, 请换一张」", res.error)
check(TFC.operator_prompt(res) is None, "卡坏了 → 也不弹「请插卡」")

# 卡坏了 / 环境不行, 两种都要判得出来
check(F._is_card_defect("Corrupt index block signature: vcn 0 inode 5") is True,
      "ntfs 的 'Corrupt index block signature' 认得出是卡坏了")
check(F._is_card_defect("ntfs_mst_post_read_fixup_warn: ... Invalid argument")
      is True, "ntfs 的 fixup_warn 认得出是卡坏了")
check(F._is_card_defect("mount: unknown filesystem type 'ntfs'") is False,
      "「不认得这个文件系统」是环境问题, 不是卡坏")
check(F._is_card_defect("mount: permission denied") is False,
      "「没权限」是环境问题, 不是卡坏")

# ③ 卡在, 已经挂好了(用户自己挂的) → 跑 fio, 测完**不**卸载
res, ctx, cmds, removed = asyncio.run(
    run_item(F._MountInfo("/media/User/TF", None, "existing")))
check(res.status == "passed", "卡在且已挂载 → 跑完 fio 判 passed", res.status)
check(res.metrics.get("round_1_read_mbps") == 50.0,
      "fio 的读速写进指标了", res.metrics)
check(res.metrics.get("mount_method") == "existing",
      "记下挂载方式 (existing)", res.metrics)
check([c for c in cmds if "umount" in c] == [],
      "用户自己挂的卡, 测完**不卸载** (没资格替人卸盘)", cmds)
check(len(removed) == 1, "测试文件被删掉了", removed)

# ④ 卡在, 是**我们**挂的 → 测完卸掉
res, ctx, cmds, removed = asyncio.run(
    run_item(F._MountInfo("/media/User/TF", None, "udisks")))
check(res.status == "passed", "卡在且工具挂上了 → 判 passed", res.status)
uds = [c for c in cmds if c[:2] == ["udisksctl", "unmount"]]
check(len(uds) == 1 and uds[0][-1] == "/dev/mmcblk1p1",
      "工具自己挂的卡, 测完**卸载掉** (udisksctl unmount, 收尾不留痕迹)", cmds)

res, ctx, cmds, removed = asyncio.run(
    run_item(F._MountInfo(F.TF_FALLBACK_MOUNT, None, "sudo")))
suds = [c for c in cmds if "umount" in c]
check(len(suds) == 1 and suds[0][-1] == F.TF_FALLBACK_MOUNT,
      "sudo 兜底挂的也是测完卸载, 而且卸的是那个固定挂载点", cmds)

# ⑤ 卡在, 但 /dev 节点还没出来 → error(不是「没插卡」)
async def no_dev(dev, timeout_sec=3.0):
    return False

async def _ensure_never(ctx, disk, dev):
    raise AssertionError("设备节点都没有, 不该走到挂载那一步")

with Patch(_ensure_mounted=_ensure_never, _wait_for_dev=no_dev,
           _card_disks=lambda root_disk=None: ["mmcblk1"]):
    ctx = FakeCtx({"times": 1})
    res = asyncio.run(TFC.run(ctx))
check(res.status == "error", "设备节点没出来 → error", res.status)
check(res.metrics.get("no_card") is None,
      "设备节点没出来 → 不置 no_card (卡是在的, 只是没就绪)", res.metrics)
check(TFC.operator_prompt(res) is None,
      "设备节点没出来 → 不弹「请插卡」")


# ══════════════════════════════════════════════════════════════════════
section("【5】真板子活体检 (只读: 只看 /sys 和 /proc, 不挂载不写盘)")

real_root = F._root_disk()
real_cards = F._card_disks()
print(f"    · 根盘 = {real_root!r}, 探到的卡 = {real_cards!r}")
check(real_root not in real_cards,
      "真板子上: 根盘永远不在卡的列表里", (real_root, real_cards))
for d in real_cards:
    t = ""
    try:
        with open(os.path.join(F.SYS_BLOCK, d, "device", "type")) as f:
            t = f.read().strip()
    except OSError:
        pass
    check(t.upper() == "SD",
          f"真板子上: {d} 的 device/type 是 SD (是卡, 不是板载存储)", t)

# 老代码里那个「只查挂载点」的探法还在不在 —— 留着的话迟早有人又用回去
check(not hasattr(F, "_find_tfcard_mount"),
      "旧的「只探挂载点」的函数已经删了 (它就是 bug 的来源)")


# ── 汇总 ─────────────────────────────────────────────────────────────
print("\n" + "=" * 60)
if FAILS:
    print(f"✗ {len(FAILS)} 项失败 / 共 {PASSES + len(FAILS)} 项")
    for f in FAILS:
        print("   - " + f)
    sys.exit(1)
print(f"✓ 全部通过 ({PASSES} 项)")
sys.exit(0)
