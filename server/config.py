"""配置加载 - 读取 config.yaml"""
import os
import re
from pathlib import Path
from typing import Any, Optional
import yaml


PROJECT_ROOT = Path(__file__).parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"


# ══════════════════════════════════════════════════════════════
#  U 盘挂载点漂移 —— 「记设备, 不记路径」
# ══════════════════════════════════════════════════════════════
#
# 现象(2026-11-05 实测): 标签 `AGING` 的盘本该挂在 `/media/User/AGING`,
# 实际挂到了 **`/media/User/AGING1`** —— 因为那个目录**没被清掉**(里面
# 还留着文件), udisks2 就往后排了个 "1"。
#
# ⇒ **正在跑的服务手里的路径当场失效**:
#   · history.db 找不到 ⇒ `/api/history` 直接 500;
#   · 时钟锚点写到**设备上**的残留目录 ⇒ 违背「拔盘即无、设备上不留东西」。
#
# ⇒ 修法: **记设备(major:minor + 源设备), 不记路径** ——
#   路径漂了 ⇒ 按设备重新找它现在挂在哪儿, 数据继续跟着盘走。
#
# ⚠️ **设备版一点不受影响**: 只有当「数据根本身是个挂载点」时才会记设备
#   —— 设备版的数据根是 /var/lib/aging-test(普通目录)⇒ 永远不记 ⇒
#   下面那几条分支一次都不会走。


def _unescape_mount(field: str) -> str:
    """mountinfo 里的挂载点把空格/制表符转义成了 \\040 / \\011 …"""
    return (field.replace("\\040", " ").replace("\\011", "\t")
                 .replace("\\012", "\n").replace("\\134", "\\"))


def _parse_mountinfo_line(line: str):
    """一行 mountinfo → (挂载点, major:minor, 源设备); 认不出 → None。

    ⚠️ 行格式:
        id parent major:minor root 挂载点 选项 [可选项…] - 类型 源 超级选项
    「可选项」的数量**不固定**(shared:xx / master:xx …)⇒ **分隔符 "-" 的位置
    不能写死**, 得自己找(写死第 6 个字段会把整行判废 —— 真机上就栽过)。
    """
    p = line.split(" ")
    try:
        sep = p.index("-")
    except ValueError:
        return None
    if len(p) < sep + 3 or len(p) < 5:
        return None
    return (_unescape_mount(p[4]), p[2], p[sep + 2])


def _mountinfo() -> list:
    """[(挂载点, major:minor, 源设备)] —— 从 /proc/self/mountinfo 读。"""
    out = []
    try:
        with open("/proc/self/mountinfo", "r", encoding="utf-8") as f:
            for line in f:
                row = _parse_mountinfo_line(line)
                if row:
                    out.append(row)
    except OSError:
        pass
    return out


#: `/dev/disk/by-uuid/` 里的软链: {uuid: 设备真实路径}
#:
#: **为什么要有它**: 设备名**不是稳定的身份** —— 同一根盘拔插一次, 很可能
#: 从 `/dev/sdb` 变成 `/dev/sda`(枚举顺序变了), `major:minor` 也跟着变
#: (实测: 8:16 → 8:0)⇒ **光认设备身份, 重插之后就跟丢了这块盘**。
#: 而**文件系统的 UUID 是跟盘走的**(跟插在哪个口、叫什么名字都无关)⇒
#: 它才是这块盘的**真身份**。
UUID_DIR = "/dev/disk/by-uuid"


def _uuid_map() -> dict:
    """{uuid: 设备路径} —— 读 /dev/disk/by-uuid 的软链(读不到就空)。"""
    out = {}
    try:
        names = os.listdir(UUID_DIR)
    except OSError:
        return out
    for name in names:
        try:
            out[name] = os.path.realpath(os.path.join(UUID_DIR, name))
        except OSError:
            pass
    return out


def _disk_of(dev: str) -> str:
    """设备路径 → 它所在的**整盘**路径(`/dev/sda1` → `/dev/sda`)。"""
    m = re.match(r"^(/dev/(?:sd[a-z]+|vd[a-z]+|xvd[a-z]+))\d+$", dev or "")
    return m.group(1) if m else dev


def _uuid_of(dev: str) -> Optional[str]:
    """这个设备上的文件系统 UUID(认不出 → None)。

    ⚠️ 认的是**整个文件系统**(不是分区表)⇒ 整盘 ext4 和分区 ext4 都认得出。
    """
    if not dev:
        return None
    try:
        real = os.path.realpath(dev)
    except OSError:
        return None
    disk = _disk_of(real)
    for uuid, target in _uuid_map().items():
        if target == real:
            return uuid
        # 分区的情况: by-uuid 指向分区, 而 mountinfo 可能给的是整盘
        if _disk_of(target) == disk and disk != real:
            return uuid
    return None


#: **可写数据**的根目录 —— history.db / 报告 PDF / 服务日志都落这儿。
#:
#: 为什么要有它: 打包成 deb 之后代码装在 /usr/lib/aging-test/ (root 所有),
#: 而跑测试的工人是**普通用户**, 写不进去 —— 但历史记录和报告恰恰是产线
#: 要天天写的东西。所以数据必须与代码分家, 落到 /var/lib/aging-test/。
#:
#: 开发时不分家(默认 = 代码根), 行为一个字都不变, 回归套件也不用跟着改。
DATA_DIR_ENV = "AGING_DATA_DIR"

#: 配置文件位置。打包后是 /etc/aging-test/config.yaml (dpkg conffile,
#: 升级不覆盖); 开发时还是代码根下的 config.yaml。
CONFIG_PATH_ENV = "AGING_CONFIG"
SYSTEM_CONFIG_PATH = Path("/etc/aging-test/config.yaml")

#: 打包安装后的数据目录 —— 代码根写不动时的回落位置。
SYSTEM_DATA_PATH = Path("/var/lib/aging-test")


class Config:
    _data: dict = {}
    _path: Path = DEFAULT_CONFIG_PATH

    # ── U 盘挂载点漂移: 记的是**设备身份**, 不是路径 ──
    _stick_mp: Optional[str] = None      # 记下来时那个挂载点(如 /media/User/AGING)
    _stick_dev: Optional[str] = None     # major:minor(如 8:16)
    _stick_src: Optional[str] = None     # 源设备(如 /dev/sdb)
    _stick_uuid: Optional[str] = None    # 文件系统 UUID —— **这块盘的真身份**
    _stick_cur: Optional[str] = None     # 它**现在**挂在哪儿(漂了就换)
    _stick_reported: Optional[str] = None  # 已经报过漂移的那个位置(别刷屏)

    @classmethod
    def _capture_stick(cls, root: Path) -> None:
        """记下「这块盘」的身份 —— **记设备, 不记路径**。

        ⚠️ **只在数据根落在某个挂载点里时才记** ⇒ 设备版的数据根
        (/var/lib/aging-test 那种普通目录)**永远不记** ⇒ 后面那几个分支
        一次都不会走 ⇒ 行为一个字不变。
        """
        if cls._stick_dev:
            return
        s = str(root)
        best = None
        for mp, dev, src in _mountinfo():
            mp = mp.rstrip("/") or "/"
            if mp == "/":
                continue          # 根文件系统不算「一根盘」—— 设备版走的就是它
            if s == mp or s.startswith(mp + os.sep):
                if best is None or len(mp) > len(best[0]):
                    best = (mp, dev, src)
        if best:
            cls._stick_mp, cls._stick_dev, cls._stick_src = best
            # ⚠️ 设备名会变 ⇒ 顺手把**文件系统 UUID** 也记下来(真身份)
            cls._stick_uuid = _uuid_of(best[2])

    @classmethod
    def _note_stick(cls, mp: str, dev: str = "", src: str = "") -> str:
        """记下「找到它了」—— 顺便把身份更新成现在的(免得下次又认不出)。"""
        if mp != cls._stick_mp and mp != cls._stick_reported:
            print(f"[config] ⚠ U 盘挂载点漂移: {cls._stick_mp} → {mp}"
                  f"(按设备重新定位, 数据继续跟着盘走)")
            cls._stick_reported = mp
        cls._stick_cur = mp
        if dev:
            cls._stick_dev = dev
        if src:
            cls._stick_src = src
        return mp

    @classmethod
    def _stick_mountpoint(cls) -> Optional[str]:
        """这块盘**现在**挂在哪儿(漂了就按设备重新找)。"""
        if not cls._stick_dev:
            return None
        # 快路径: 上次找到的位置还挂着(一次 stat, 很便宜)
        if cls._stick_cur and os.path.ismount(cls._stick_cur):
            return cls._stick_cur
        # 老位置还挂着 ⇒ 没漂
        if cls._stick_mp and os.path.ismount(cls._stick_mp):
            cls._stick_cur = cls._stick_mp
            return cls._stick_mp
        rows = _mountinfo()
        # ① 按**设备身份**认 —— 便宜(major:minor / 源设备)
        for mp, dev, src in rows:
            if dev == cls._stick_dev or (cls._stick_src and src == cls._stick_src):
                return cls._note_stick(mp, dev, src)
        # ② ⚠️ **设备名不是稳定的身份** —— 同一根盘拔插一次, 可能从
        #    `/dev/sdb` 变成 `/dev/sda`(枚举顺序变了), major:minor 也跟着变
        #    (实测 8:16 → 8:0)⇒ 光认设备身份, **重插之后就跟丢了这块盘**。
        #    ⇒ 再用**文件系统 UUID** 认一遍(UUID 跟盘走, 跟名字无关)。
        if cls._stick_uuid:
            for mp, dev, src in rows:
                if _uuid_of(src) == cls._stick_uuid:
                    return cls._note_stick(mp, dev, src)
        return None

    @classmethod
    def rebind(cls, path: Path) -> Path:
        """把「旧挂载点下的路径」重新绑到「同一块盘现在的挂载点」下。

        不是 U 盘模式 / 没漂 / 路径不在这块盘上 ⇒ 原样返回。
        """
        if not cls._stick_dev or not cls._stick_mp:
            return path
        cur = cls._stick_mountpoint()
        if cur is None or cur == cls._stick_mp:
            return path
        s = str(path)
        if s == cls._stick_mp:
            return Path(cur)
        if s.startswith(cls._stick_mp + os.sep):
            return Path(cur) / s[len(cls._stick_mp) + 1:]
        return path

    @classmethod
    def storage_lost(cls) -> bool:
        """数据根**跟丢了**吗(U 盘被拔了, 且找不到它现在挂在哪儿)。

        ⚠️ 设备版永远 False(没记设备 ⇒ 没有「跟丢」这回事)。

        ⇒ **跟丢了就别再往设备上写** —— 那正是挂载点漂移的**成因**:
        盘拔了以后服务照着旧路径往挂载点目录里写 ⇒ 那目录永远非空 ⇒
        udisks2 下次就往后排 ⇒ 漂移会**自我强化**。
        """
        if not cls._stick_dev:
            return False
        return cls._stick_mountpoint() is None

    @classmethod
    def data_root(cls) -> Path:
        """可写数据的根目录 (history.db / PDF / 日志都落这儿)。

        优先级: 环境变量 > 代码根(写得动时) > /var/lib/aging-test。

        ⚠️ **代码根写得动就用代码根** —— 这样开发机上的行为一个字不变,
        回归套件也不用改。装成 deb 之后代码归 root、工人写不动, 自动
        落到 /var/lib/aging-test, 不需要谁记得设环境变量。

        ⚠️ U 盘模式下这个值**会跟着挂载点走**(见上面的「记设备, 不记路径」)。
        """
        env = os.environ.get(DATA_DIR_ENV)
        if env:
            p = Path(env)
            cls._capture_stick(p)
            return cls.rebind(p)
        if os.access(PROJECT_ROOT, os.W_OK):
            return PROJECT_ROOT
        return SYSTEM_DATA_PATH

    @classmethod
    def config_path(cls) -> Path:
        """配置文件路径: 环境变量 > 系统安装位置 > 代码根。"""
        env = os.environ.get(CONFIG_PATH_ENV)
        if env:
            return cls.rebind(Path(env))
        if SYSTEM_CONFIG_PATH.exists():
            return SYSTEM_CONFIG_PATH
        return DEFAULT_CONFIG_PATH

    @classmethod
    def load(cls, path: Path = None) -> dict:
        cls._path = path or cls.config_path()
        if not cls._path.exists():
            raise FileNotFoundError(f"配置文件不存在: {cls._path}")
        with open(cls._path, "r", encoding="utf-8") as f:
            cls._data = yaml.safe_load(f) or {}
        return cls._data

    @classmethod
    def get(cls, key: str, default: Any = None) -> Any:
        """支持点号路径,如 'server.port'"""
        if not cls._data:
            cls.load()
        keys = key.split(".")
        val = cls._data
        for k in keys:
            if isinstance(val, dict) and k in val:
                val = val[k]
            else:
                return default
        return val

    @classmethod
    def all(cls) -> dict:
        if not cls._data:
            cls.load()
        return cls._data

    @classmethod
    def project_root(cls) -> Path:
        """代码根 —— U 盘模式下它也在盘上 ⇒ 挂载点漂了要跟着走。"""
        return cls.rebind(PROJECT_ROOT)
