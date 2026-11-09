"""U 盘「挂载点漂移」的回归测试。

**病根**(2026-11-05 实测): 盘被拔了再插, udisks2 可能挂到**别的路径**
(`/media/User/AGING` → `/media/User/AGING1`)—— 因为老目录**没被清掉**
(里面还留着文件)⇒ **正在跑的服务手里的路径当场失效**:

  · `history.db` 找不到 ⇒ `/api/history` 直接 500;
  · 时钟锚点写到**设备上**的残留目录 ⇒ 违背「拔盘即无、设备上不留东西」。

**修法**: **记设备(major:minor + 源设备), 不记路径** ⇒ 漂了能重新定位;
而且**跟丢的时候不往设备上写** ⇒ 老目录保持空 ⇒ udisks2 会把它清掉
⇒ 漂移不会**自我强化**。

跑法: .venv/bin/python tests/test_usb_mount_drift.py
"""
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from server import config as configmod                       # noqa: E402
from server.config import Config                             # noqa: E402

FAILS = []
PASSES = 0

# 假挂载点 —— 故意用 /tmp 底下, 免得跟真机上的 /media/User/AGING 撞
#
# ⚠️ **每轮跑用独立的目录**, 不能写死一个固定路径 ——
#   变异脚本会**故意**把 `storage_lost()` 改坏 ⇒ 那一跑**真的**往挂载点
#   目录里写了锚点文件 ⇒ 固定路径下留了残留 ⇒ **下一轮正常跑**就误判成
#   「设备上被留下了锚点文件」⇒ **变异脚本把自己的回归跑挂了**
#   (2026-11-06 真踩过: 变异跑完紧接全量回归 ⇒ 65 项里挂了 1 项)。
#   ⇒ 每轮一个全新目录 ⇒ 互不干扰, 也不用去删谁的东西。
_RUN = tempfile.mkdtemp(prefix="drift-stick-")
MP_OLD = os.path.join(_RUN, "stick")
MP_NEW = os.path.join(_RUN, "stick-again")
DEV = "8:16"
SRC = "/dev/sdb"


def check(cond, msg, extra=""):
    global PASSES
    if cond:
        PASSES += 1
        print("    ✓ " + msg)
    else:
        FAILS.append(msg)
        print("    ✗ " + msg + ("  → " + str(extra) if extra else ""))


class fake_mountinfo:
    """把 server.config._mountinfo 换成假的 —— 真机没法随便挂/拔盘。

    ⚠️ 顺手把 `_uuid_map` 也换成空的(默认)⇒ 测试**不碰真机**的
    /dev/disk/by-uuid ⇒ 不会因为「这台机器上恰好有 /dev/sdb」而串味。
    """

    def __init__(self, entries):
        self.entries = entries
        self._orig = None
        self._orig_uuid = None

    def __enter__(self):
        self._orig = configmod._mountinfo
        self._orig_uuid = configmod._uuid_map
        configmod._mountinfo = lambda: list(self.entries)
        configmod._uuid_map = lambda: {}
        _reset_stick()
        return self

    def __exit__(self, *a):
        configmod._mountinfo = self._orig
        configmod._uuid_map = self._orig_uuid
        _reset_stick()
        return False


class fake_uuid_map:
    """把 /dev/disk/by-uuid 的内容换成假的 —— {uuid: 设备路径}。"""

    def __init__(self, mapping):
        self.mapping = mapping
        self._orig = None

    def __enter__(self):
        self._orig = configmod._uuid_map
        configmod._uuid_map = lambda: dict(self.mapping)
        return self

    def __exit__(self, *a):
        configmod._uuid_map = self._orig
        return False


def _reset_stick():
    """把「记下来的设备身份」清掉 —— 每个用例都得从头来。"""
    Config._stick_mp = None
    Config._stick_dev = None
    Config._stick_src = None
    Config._stick_uuid = None
    Config._stick_cur = None
    Config._stick_reported = None


# ══════════════════════════════════════════════════════════
# ① 设备版一点不受影响
# ══════════════════════════════════════════════════════════
def device_mode_untouched():
    print("\n① 设备版(数据根是普通目录)—— 行为一个字不变")
    tmp = tempfile.mkdtemp(prefix="drift-dev-")
    try:
        with fake_mountinfo([("/", "8:2", "/dev/mmcblk0p2")]):
            os.environ["AGING_DATA_DIR"] = tmp
            root = Config.data_root()
            check(str(root) == tmp, "数据根原样返回", root)
            check(Config.storage_lost() is False,
                  "设备版永远不是「跟丢了」")
            check(str(Config.rebind(Path(tmp) / "x")) == str(Path(tmp) / "x"),
                  "设备版不做任何重新绑定")
    finally:
        os.environ.pop("AGING_DATA_DIR", None)


# ══════════════════════════════════════════════════════════
# ② 漂了 ⇒ 按设备重新定位
# ══════════════════════════════════════════════════════════
def drift_is_relocated():
    print("\n② 挂载点漂了 ⇒ 按设备重新定位")
    with fake_mountinfo([(MP_OLD, DEV, SRC)]):
        os.environ["AGING_DATA_DIR"] = MP_OLD
        root = Config.data_root()
        check(str(root) == MP_OLD, "起动时: 数据根就是挂载点", root)
        check(Config._stick_dev == DEV, "记下的是**设备身份**, 不是路径",
              Config._stick_dev)
        check(Config.storage_lost() is False, "盘在, 没跟丢")

        # ── 拔了再插, 挂到别的地方去了 ──
        configmod._mountinfo = lambda: [(MP_NEW, DEV, SRC)]
        root2 = Config.data_root()
        check(str(root2) == MP_NEW,
              "漂了之后数据根跟到新挂载点", root2)
        check(str(Config.rebind(Path(MP_OLD) / "tools"))
              == str(Path(MP_NEW) / "tools"),
              "盘上的子目录(tools/)也跟着走",
              Config.rebind(Path(MP_OLD) / "tools"))
        check(str(Config.rebind(Path(MP_OLD) / "config.yaml"))
              == str(Path(MP_NEW) / "config.yaml"),
              "盘上的文件(config.yaml)也跟着走")
        check(Config.storage_lost() is False,
              "换了地方但还是同一块盘 ⇒ 不算跟丢")
    os.environ.pop("AGING_DATA_DIR", None)


# ══════════════════════════════════════════════════════════
# ③ 盘真没了 ⇒ 认「跟丢」, 别往设备上写
# ══════════════════════════════════════════════════════════
def lost_is_detected():
    print("\n③ 盘真没了 ⇒ 认「跟丢」")
    with fake_mountinfo([(MP_OLD, DEV, SRC)]):
        os.environ["AGING_DATA_DIR"] = MP_OLD
        Config.data_root()
        configmod._mountinfo = lambda: []          # 盘拔了, 哪儿都没挂
        check(Config.storage_lost() is True, "认得出「跟丢了」")
        check(str(Config.rebind(Path(MP_OLD))) == MP_OLD,
              "找不回来就只能用原路径(不瞎猜)")

        # 换了一块**别的**盘 ⇒ 也不算同一块
        configmod._mountinfo = lambda: [("/tmp/other-stick", "8:32", "/dev/sdc")]
        check(Config.storage_lost() is True,
              "别的盘不算这块盘(设备身份对不上)")
    os.environ.pop("AGING_DATA_DIR", None)


# ══════════════════════════════════════════════════════════
# ④ 时钟锚点: 跟丢的时候**不写**(那是漂移的成因)
# ══════════════════════════════════════════════════════════
def anchor_not_written_when_lost():
    print("\n④ 跟丢的时候不往设备上写")
    from server import clock as clockmod
    with fake_mountinfo([(MP_OLD, DEV, SRC)]):
        os.environ["AGING_DATA_DIR"] = MP_OLD
        Config.data_root()
        configmod._mountinfo = lambda: []
        clockmod.enable_anchor(True)
        try:
            ok = clockmod.save_anchor()
            check(ok is False, "跟丢 ⇒ 锚点不写(返回 False)")
            anchor = Path(MP_OLD) / "logs" / "clock_anchor.json"
            check(not anchor.exists(),
                  "设备上没被留下锚点文件(挂载点目录保持空)",
                  anchor)
        finally:
            clockmod.enable_anchor(False)
    os.environ.pop("AGING_DATA_DIR", None)


# ══════════════════════════════════════════════════════════
# ⑤ 库路径 / 工具目录跟着盘走
# ══════════════════════════════════════════════════════════
def db_and_tool_dir_follow():
    print("\n⑤ 库路径 / 工具目录跟着盘走")
    from server import db as dbmod
    from server.tests import _tools as toolsmod

    with fake_mountinfo([(MP_OLD, DEV, SRC)]):
        os.environ["AGING_DATA_DIR"] = MP_OLD
        os.environ["AGING_TOOL_DIR"] = os.path.join(MP_OLD, "tools")
        Config.data_root()
        configmod._mountinfo = lambda: [(MP_NEW, DEV, SRC)]

        check(str(dbmod._db_path())
              == str(Path(MP_NEW) / "reports" / "history.db"),
              "库路径跟到新挂载点", dbmod._db_path())
        check(toolsmod.tool_dir() == os.path.join(MP_NEW, "tools"),
              "随包工具目录跟到新挂载点", toolsmod.tool_dir())

        # ⚠️ 回归测试会直接把 DB_PATH 改成临时路径 ⇒ **不能被顶掉**
        saved = dbmod.DB_PATH
        try:
            dbmod.DB_PATH = Path("/tmp/drift-test-db/history.db")
            check(str(dbmod._db_path()) == "/tmp/drift-test-db/history.db",
                  "测试改过的库路径以人为准(不被顶掉)", dbmod._db_path())
        finally:
            dbmod.DB_PATH = saved
    os.environ.pop("AGING_DATA_DIR", None)
    os.environ.pop("AGING_TOOL_DIR", None)


# ══════════════════════════════════════════════════════════
# ⑥ mountinfo 解析 —— 「可选项」数量不固定, 分隔符位置不能写死
# ══════════════════════════════════════════════════════════
def mountinfo_parsing():
    print("\n⑥ mountinfo 解析")
    parse = configmod._parse_mountinfo_line

    # 真机上的一行(带 shared:16 这个可选项)
    row = parse("36 25 8:16 / /media/User/AGING1 rw,relatime shared:16 "
                "- ext4 /dev/sdb rw")
    check(row == ("/media/User/AGING1", "8:16", "/dev/sdb"),
          "带可选项的行能认出来", row)

    # **没有**可选项的行 —— 分隔符的位置不一样
    row2 = parse("29 22 0:21 / / rw,relatime - ext4 /dev/root rw")
    check(row2 == ("/", "0:21", "/dev/root"),
          "没有可选项的行也能认出来(分隔符位置不一样)", row2)

    # 挂载点里的空格是转义过的
    row3 = parse("40 25 8:17 / /media/User/My\\040Stick rw - vfat /dev/sdc rw")
    check(row3 and row3[0] == "/media/User/My Stick",
          "挂载点里的 \\040 解回空格", row3)

    check(parse("这行不是 mountinfo") is None, "不像的行返回 None")


# ══════════════════════════════════════════════════════════
# ⑦ 回归: 跑测途中盘被重挂 ⇒ 这一项**不能**停在 running
# ══════════════════════════════════════════════════════════
def row_not_stuck_in_running():
    print("\n⑦ 跑测途中盘被重挂 ⇒ 这一项不能停在 running")
    import asyncio
    from server.db import Database

    tmp = tempfile.mkdtemp(prefix="drift-run-")
    real = os.path.join(tmp, "stick")            # 真的那个目录(盘)
    real2 = os.path.join(tmp, "stick-again")      # 同一个盘, 换了个挂载点
    os.makedirs(os.path.join(real, "reports"))
    os.symlink(real, real2)                      # 两个名字, 同一个盘
    try:
        with fake_mountinfo([(real, DEV, SRC)]):
            os.environ["AGING_DATA_DIR"] = real
            Config.data_root()

            async def _run():
                await Database.init()
                # ① 开跑时先落一行 running(这一步在盘还挂得好的时候)
                await Database.upsert_result(1, "ethernet", status="running")
                # ② 盘被重挂到别的地方去了(同一个盘, 换了挂载点)
                configmod._mountinfo = lambda: [(real2, DEV, SRC)]
                # ③ 这一项跑完 ⇒ 最终落库
                await Database.upsert_result(1, "ethernet", status="passed",
                                             finished_at=123.0, duration_sec=0.2,
                                             metrics={"link_detected": False},
                                             log="ok", error=None, attempt=1)

            asyncio.run(_run())

            import sqlite3
            con = sqlite3.connect(os.path.join(real, "reports", "history.db"))
            row = con.execute(
                "SELECT status, finished_at, length(log) FROM results "
                "WHERE run_id=1 AND test_name='ethernet'").fetchone()
            con.close()
            check(row and row[0] == "passed",
                  "重挂之后最终落库照样落到盘上(没停在 running)", row)
            check(row and row[1] is not None, "finished_at 也落上了", row)

            # ── 反证: 路径**没**跟着盘走 ⇒ 落库就失败 ──
            #    (生产上那次就是这样: 盘还在, 但服务手里的路径指向了
            #     eMMC 上的残留目录, 那儿没有 reports/ ⇒ 写不进去 ⇒
            #     那一行永远停在 running ⇒ 这一项从历史里消失)
            stale = os.path.join(tmp, "stale-leftover")
            os.makedirs(stale)
            os.environ["AGING_DATA_DIR"] = stale   # 服务手里的路径没跟上盘
            async def _run_stale():
                try:
                    await Database.upsert_result(2, "ethernet", status="passed")
                except Exception as e:
                    return type(e).__name__
                return None
            err = asyncio.run(_run_stale())
            check(err is not None,
                  "反证: 路径没跟着盘走 ⇒ 落库直接失败(那正是现场的机制)", err)
    finally:
        os.environ.pop("AGING_DATA_DIR", None)


# ══════════════════════════════════════════════════════════
#  ⑧ 设备名变了 ⇒ 靠 UUID 认回来(设备名不是稳定的身份)
# ══════════════════════════════════════════════════════════
def renamed_device_still_found():
    print("\n⑧ 设备名变了(major:minor 也变)⇒ 靠 UUID 认回来")
    uuids = {"uuid-A": SRC}
    with fake_mountinfo([(MP_OLD, DEV, SRC)]), fake_uuid_map(uuids):
        os.environ["AGING_DATA_DIR"] = MP_OLD
        Config.data_root()
        check(Config._stick_uuid == "uuid-A",
              "记下了文件系统 UUID(这块盘的真身份)", Config._stick_uuid)

        # ── 拔了再插: 同一根盘, 设备名和 major:minor 全变了 ──
        uuids["uuid-A"] = "/dev/sda"       # 同一个 UUID, 换了设备名
        configmod._mountinfo = lambda: [(MP_NEW, "8:0", "/dev/sda")]
        root = Config.data_root()
        check(str(root) == MP_NEW,
              "设备名变了也认得回来(靠 UUID, 不靠 /dev/sdX)", root)
        check(Config._stick_dev == "8:0", "身份顺手更新成现在的", Config._stick_dev)

        # ── 换成一块**别的**盘(设备身份和 UUID 都不是我们的)⇒ 跟丢 ──
        uuids.clear()
        configmod._mountinfo = lambda: [(MP_NEW, "8:32", "/dev/sdc")]
        check(Config.storage_lost() is True,
              "换成别的盘 ⇒ 认「跟丢」(不认错盘)")
    os.environ.pop("AGING_DATA_DIR", None)


# ══════════════════════════════════════════════════════════
#  ⑨ 没有 by-uuid ⇒ 退回按设备身份(老行为一个字不变)
# ══════════════════════════════════════════════════════════
def uuid_absent_falls_back_to_device():
    print("\n⑨ 读不到 by-uuid ⇒ 退回按设备身份")
    with fake_mountinfo([(MP_OLD, DEV, SRC)]), fake_uuid_map({}):
        os.environ["AGING_DATA_DIR"] = MP_OLD
        Config.data_root()
        check(Config._stick_uuid is None,
              "认不出 UUID 就不记(不强求)", Config._stick_uuid)
        configmod._mountinfo = lambda: [(MP_NEW, DEV, SRC)]
        root = Config.data_root()
        check(str(root) == MP_NEW,
              "设备身份没变时照样认得回来(老路径没坏)", root)
    os.environ.pop("AGING_DATA_DIR", None)


# ══════════════════════════════════════════════════════════
#  ⑩ 根文件系统**不算「一根盘」** —— 设备版走的就是它
# ══════════════════════════════════════════════════════════
def root_filesystem_is_not_a_stick():
    print("\n⑩ 根文件系统不算一根盘")
    with fake_mountinfo([("/", "8:2", "/dev/mmcblk0p2")]):
        os.environ["AGING_DATA_DIR"] = "/"
        Config.data_root()
        check(Config._stick_dev is None,
              "数据根是 / ⇒ 根文件系统没被当成 U 盘", Config._stick_dev)
        check(Config._stick_mp is None, "也没记住它的挂载点", Config._stick_mp)
        check(Config.storage_lost() is False,
              "设备版永远不是「跟丢了」")
    os.environ.pop("AGING_DATA_DIR", None)


def main():
    device_mode_untouched()
    drift_is_relocated()
    lost_is_detected()
    anchor_not_written_when_lost()
    db_and_tool_dir_follow()
    mountinfo_parsing()
    row_not_stuck_in_running()
    renamed_device_still_found()
    uuid_absent_falls_back_to_device()
    root_filesystem_is_not_a_stick()

    print("\n" + "─" * 60)
    if FAILS:
        print(f"✗ {len(FAILS)} 项失败 / 共 {PASSES + len(FAILS)} 项")
        for f in FAILS:
            print("   · " + f)
        return 1
    print(f"✓ 全部通过 ({PASSES} 项)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
