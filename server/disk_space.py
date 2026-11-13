"""盘上空间够不够 —— **只在不够的时候才说话**。

**为什么要有它** (2026-11-13 工人提的)
    U 盘既是**启动盘**又是**数据根** —— 报告 PDF / 历史库 / 日志全落在它上面。
    盘写满时的失败是**静默丢数据**那一种:

    | 写不下的东西 | 后果 |
    |---|---|
    | 报告 PDF 导出 | 页面会报错, 工人**看得见** |
    | **`history.db`** | SQLite 在满盘上会报 `database or disk is full` ⇒ **那一轮的结果记不进库**, 极端情况把库写坏(库坏了有自愈: 改名隔离不删, 但那一轮数据还是没了) |

    ⇒ 板子明明测了, **记录却没了** —— 这才是最不该发生的一种。

**规矩(用户定的, 一条都不许违反)**

    · **只在空间不足的时候显示** —— 空间充足时**任何地方都不显示提醒**。
      不许挂「剩余 54 GB」这种仪表盘: 工人不看, 看了也只会麻木
      (狼过了 ⇒ 真出事那天他就不当回事了)。
    · **只管 U 盘版**(2026-11-13 用户定的) —— 本地/设备版部署**不要**这条逻辑:
      设备版的数据在设备自己的盘上, 那是厂里管的系统盘, 不归这条提醒管 ⇒
      设备版**一个字的提醒都不出**(哪怕那个盘快满了)。
    · **提醒, 不拦截** —— 空间不够也照样能开测。盘要是真满了还拦着不让测,
      那是另一种事故: 板子白烧一轮, 数据一样没存下来。
    · **读不到空间就当作够** —— 绝不能因为读不到就把测试搞挂
      (跟「写不进去就当没有」同一个套路)。
    · **先讲「现在能不能用」** —— 第一句就说「还能测」, 再说空间的事。
      (界面提示别吓人: 工人读到「磁盘不足」会以为这根盘废了。)

**阈值怎么定的**
    一台设备跑一轮占多少是**实测**出来的(2026-11-12, 64G 盘):
    报告 PDF **两份**(reports/pdf/ 一份 + AGING-REPORTS/ 自动导出一份)
    ≈ 1.56 MB + 历史库 ≈ 0.03 MB + 日志 ≈ 0.03 MB ⇒ **约 1.7 MB/台**。

    ⇒ 提醒不写「剩余多少 MB」, 写**「还能存约多少台设备」** —— 那才是
      工人能拿来做决定的数。
"""
import shutil
from typing import Optional

from .config import Config

MB = 1024 * 1024

#: 一台设备跑一轮大致占多少(MB) —— 实测值, 见上面的说明。
PER_DEVICE_MB = 1.7

#: 可用空间低于这个值 ⇒ 提醒一句(还够多少台)。
WARN_FREE_MB = 1024          # 1 GB —— 再往后写, 顶多还够 600 来台
#: 可用空间低于这个值 ⇒ 升级措辞(再写下去历史库就写不进去了)。
CRITICAL_FREE_MB = 300       # 约 176 台


def free_space_mb(path: Optional[str] = None) -> Optional[float]:
    """**数据根**那个文件系统的可用空间(MB); 读不到就返回 None。

    ⚠️ **查的是「数据根」那个盘, 不是 `/`、也不是「插在哪个口的那个盘」** ——
       U 盘版下数据根**就是**那根 U 盘(AGING_DATA_DIR 指到盘上);
       设备版下数据根在设备自己的文件系统上。
       ⇒ 一套代码两边都对, 不用先判断「是不是 U 盘版」。

    ⚠️ `shutil.disk_usage` 给的可用空间**已经扣掉了 ext4 的 5% root 保留**
       ⇒ 这个数可以直接给工人看, 不会骗人。
    """
    try:
        root = path or str(Config.data_root())
        return shutil.disk_usage(root).free / MB
    except Exception:
        # 盘刚拔了 / 权限不对 / 路径没了 —— 一律「读不到」
        return None


def _free_text(free_mb: float) -> str:
    """可用空间写成工人看得懂的样子(GB 或 MB)。"""
    if free_mb >= 1024:
        return f"{free_mb / 1024:.1f} GB"
    return f"{int(free_mb)} MB"


def _disk_name() -> str:
    """这根盘叫什么 —— U 盘版说「U 盘」。"""
    return "U 盘"


def usb_mode() -> bool:
    """这台机器上跑的是**U 盘版**吗 —— 判据跟 shell 脚本**是同一个**。

    ⚠️ **只用 U 盘版提醒**(2026-11-13 用户定的): 本地/设备版部署
       **不要**这条逻辑 ⇒ 设备版**一条提醒都不出**。
    ⇒ 判据取自 `server/tests/_tools.py::usb_mode()`(环境变量 `AGING_TOOL_DIR`
       **或**代码根的 `AGING_USB_MODE` 标记文件 ⇒ 两个判据**取或**,
       跟 `start.sh` / `check_deps.sh` 认的是同一件事)。
    """
    try:
        from .tests._tools import usb_mode as _m
        return bool(_m())
    except Exception:
        return False


def disk_warning(path: Optional[str] = None) -> Optional[dict]:
    """空间够 ⇒ **None**(界面上一个字都不显示); 不够 ⇒ 一条提醒。

    返回的 dict:
        {
            "level": "warn" | "critical",
            "free_mb": float,
            "devices_left": int,      # 按现在的用量, 还能存几台
            "message": str,           # 给工人看的那句话(后端出, 单一口径)
        }
    """
    free = free_space_mb(path)
    if free is None:
        return None          # 读不到 ⇒ 当作够(绝不能因读不到就把测试搞挂)
    if free >= WARN_FREE_MB:
        return None          # ⚠️ **够 ⇒ 一个字都不显示**(用户定的)
    if not usb_mode():
        # ⚠️ **只管 U 盘版**(用户定的) —— 设备版/本地部署**一条都不出**。
        #   设备版的数据在设备自己的系统盘上, 不归这条提醒管。
        return None

    level = "critical" if free < CRITICAL_FREE_MB else "warn"
    left = int(free / PER_DEVICE_MB)
    what = _disk_name()
    if level == "critical":
        message = (
            f"{what}只剩 {_free_text(free)} 了 —— 按现在这个用量, 大约还能存 "
            f"{left} 台设备。**现在还能正常测**, 但再写下去**这一轮的结果"
            f"可能记不下来**(历史库写不进去)⇒ 建议先清一下盘再测。"
        )
    else:
        message = (
            f"{what}剩余空间 {_free_text(free)} —— 按现在这个用量, 大约还能存 "
            f"{left} 台设备。**现在还能正常测**, 但报告快存不下了 ⇒ "
            f"建议先清一下盘再测。"
        )
    return {
        "level": level,
        "free_mb": round(free, 1),
        "devices_left": max(left, 0),
        "message": message,
    }


__all__ = [
    "PER_DEVICE_MB",
    "WARN_FREE_MB",
    "CRITICAL_FREE_MB",
    "free_space_mb",
    "disk_warning",
]
