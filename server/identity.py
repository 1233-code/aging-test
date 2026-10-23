"""设备身份 (docs §2.2) —— 提取并常驻被测设备 SN

SN 是产线追溯的锚点: **每条 run 记录 + 每份报告 + 每份 PDF 都必须携带它**。
提取源按优先级两个:
    1. `snum=`                (本板实测值, 18 位十六进制)
    2. `androidboot.serialno=` (回落; 本板两条同值, 可互为校验)
两个源都取不到 → 记 `unknown`, **照常跑测试不阻断** (§2.2), 由 UI 顶部转黄提示。

**只在服务启动时读一次**(§4.4「启动时读取 SN」), 之后常驻内存 —— SN 是硬件属性,
一轮烧机里不会变, 每次 run 再去读 `/proc/cmdline` 只是白花开销。
"""
from pathlib import Path
from typing import Optional

CMDLINE = Path("/proc/cmdline")

#: 取不到时的哨兵值。**刻意用普通字符串而不是 None** —— 这个值最终会出现在
#: 报告和文件名里, 让它有个明确可打印的写法比到处判 None 少一层出错的地方。
UNKNOWN = "unknown"

#: 优先级顺序即此列表顺序 (§2.2: 主源 snum=, 回落 androidboot.serialno=)
_KEYS = ("snum=", "androidboot.serialno=")

_sn: Optional[str] = None


def _read_cmdline() -> str:
    """读 /proc/cmdline 成一行文本。读不到返回空串(不是抛异常)。"""
    try:
        raw = CMDLINE.read_bytes()
    except OSError:
        return ""
    return raw.decode("utf-8", errors="replace")


def extract_sn(cmdline: str = None) -> str:
    """从 cmdline 文本里提 SN。取不到返回 `UNKNOWN`。

    等价于 §2.2 那两条命令
        grep -oP 'snum=\\K[^ ]*' /proc/cmdline
        grep -oP 'androidboot.serialno=\\K[^ ]*' /proc/cmdline
    但直接在 Python 里按空白切 —— 不必为一个 `-P` 正则依赖 GNU grep, 也不会
    少掉 `snum=` 与 `androidboot.serialno=` 的优先级差别。

    ⚠️ NUL → 空格的归一化**必须放在这里**, 不能只在 `_read_cmdline` 里做:
    这个函数是唯一对外的入口(测试直接喂文本进来), 归一化放在调用方就等于
    「只有走真实读取那条路才对」—— 我第一版就是这么写的, 被一条
    `snum=AAA\x00androidboot.serialno=BBB` 的用例当场打出来, 提出来的 SN
    是一整串 `AAA\x00androidboot.serialno=BBB`。

    ⚠️ 匹配用**整 token 前缀**而不是 `in` 子串找: `"snum=" in line` 这种写法
    在 cmdline 里恰好也躲得过(androidboot.serialno= 不含 snum=), 但只要将来
    出现任何 `xsnum=` 之类的键就会**误命中别人的值** —— 追溯字段认错机器,
    比读不到严重得多。
    """
    if cmdline is None:
        cmdline = _read_cmdline()
    # 内核给的是 NUL 分隔的 argv; 换成空格后按空白切, 两种分隔都覆盖到。
    tokens = cmdline.replace("\x00", " ").split()
    for key in _KEYS:
        for tok in tokens:
            if tok.startswith(key):
                val = tok[len(key):].strip()
                if val:
                    return val
    return UNKNOWN


def init() -> str:
    """服务启动时调一次, 把 SN 读进内存。返回值直接拿去打启动日志。"""
    global _sn
    _sn = extract_sn()
    return _sn


def get_sn() -> str:
    """取常驻的 SN。**懒兜底**: 万一启动钩子没跑到(比如测试直接 import 本模块),
    这里补读一次, 而不是让前端拿到空串显示成「读不到」—— 那会把一次没初始化
    误报成设备身份缺失。"""
    if _sn is None:
        return init()
    return _sn