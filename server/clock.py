"""统一时间源 —— **板子时钟不可信时, 归档也不能乱**。

**为什么要有这个模块** (2026-10-30)
    产线板子**开机时的系统时钟是假的**。同型号板子上实测出来的机制:
      · 开机时真正在设时钟的是 `fake-hwclock`(`/sbin/fake-hwclock load`)——
        它读 `/etc/fake-hwclock.data`, 把系统时钟设成那个时刻;
      · **那个文件只在「干净关机」时写**(service 的 ExecStop = `fake-hwclock save`)
        —— **硬复位 / 掉电根本走不到关机流程**;
      · ⇒ 板子每硬复位一次, 下次开机就回到「上次干净关机」那一刻
        (新板实测: **倒跳 42 天**);
      · 唯一能把它扳回来的是 `ntp.service` —— **产线网要是通不了外网,
        就没人扳了**。
    ⇒ **报告上的时间是错的** —— 归档按时间排序会倒挂, 纸质追溯对不上,
    SPC 拿这批数据算出来的过程能力全是废数。

**这个模块保证什么**(三条, **都不依赖 root、不依赖网络**)
    ① **时间戳单调不减** —— 就算墙钟是错的, 归档序列也不倒挂;
    ② **哪一轮的时间不可信, 说得清楚** —— 报告上明写「推算值」,
       不让人拿错时间去签字;
    ③ **时长永远是对的** —— 时长用单调钟算, 墙钟中途跳变不会算出**负数时长**。

**怎么做到的**
    维护一个**锚点**(`<数据目录>/logs/clock_anchor.json`): 存「此刻最可信
    的墙钟」。服务运行期间**每分钟存一次** ⇒ 硬复位最多丢掉「关机前
    一分钟内」那点时间, 而不是 42 天。下次开机发现墙钟比锚点还早 ⇒
    判定「时钟倒退了」, 之后所有时间戳用 `锚点 + (单调钟 − 锚点时的单调钟)`
    往后推 ⇒ 单调不减。

    ⚠️ **锚点必须存「现在」**(2026-11-06 实测): 存「最后一次有人问时间」
       的话, 服务空转的那段时间锚点会**冻着** ⇒ 下一轮服务的推算基准就
       停在几小时前 ⇒ 整个应用的时间比真实时间慢一大截(实测 2 小时 28 分)
       ⇒ 界面读秒直接变成「2 个多小时」。

**不做什么**(边界, 别越界)
    · **不改系统时钟** —— 那是 root 的事(`hwclock --hctosys` / NTP),
      要单独授权, 不该由一个报告模块偷偷干;
    · **不猜「真实时间」** —— 推算值只保证「不倒退」; 板子**关机那段时间**
      到底是多久**猜不出来**。所以报告上明写「推算值」, 不让人拿它当准点。
"""
import asyncio
import json
import os
import time
from pathlib import Path
from typing import Optional

from .config import Config
from .tests._tools import find_tool


#: **允许的时钟倒退容差**(秒) —— 小于这个就算「没倒退」。
#:
#: 为什么要有容差: NTP 校时偶尔会把时钟往回拨零点几秒(频率微调/闰秒),
#: 那种不该触发「倒退 42 天」那套推算 —— 否则每次校时都会把报告打成
#: 「推算值」, 反而把真时间标成了不可信。
SKEW_TOLERANCE_SEC = 300.0

#: 锚点文件里的格式版本 —— 以后改结构时好认。
ANCHOR_VERSION = 1

#: `fake-hwclock` 的路径 —— 板子用它**存/取**时间(见 `save_hwclock`)。
FAKE_HWCLOCK = "/sbin/fake-hwclock"

# ──────────────────── 模块状态 ────────────────────
#
# 「服务级别」的状态 —— **一次开机一份**。板子重启 = 服务重启 = 重新判定,
# 所以「这一轮跑的时候时钟歪没歪」这个结论, 天然就是开机级别的。
_mode = "wall"           # "wall"(系统时钟可信) | "estimated"(推算)
_last_observed = 0.0     # 见过/产出过的**最大**墙钟 —— 永不回退
_ref_wall = 0.0          # 判定为「倒退」那一刻的参考墙钟
_ref_mono = 0.0          # 判定那一刻的单调钟
_skew_sec = 0.0          # 倒退了多久(秒)
_initialized = False

#: **要不要由这个进程维护锚点** —— 默认**关**。
#:
#: 为什么默认关: 锚点是**服务**的运行时状态(每分钟存一次), 不是库该随手
#: 写的东西。开发时数据根 = 代码根 ⇒ 「服务开着跑一轮」和「跑一次回归
#: 测试」会往**仓库里**落一个 `logs/clock_anchor.json` —— 仓库不是存放
#: 运行时数据的地方(实测 19 个回归用例都会落一个)。所以**只有服务进程
#: 明确开了它**才写; 读永远可以(读不写)。
#:
#: ⇒ 回归测试跑完, 仓库里一个文件都不多。
_anchor_writes = False


# ==================== 锚点文件 ====================
def anchor_path() -> Path:
    """锚点文件位置 —— 数据目录下的 `logs/`(和证据文件同处)。"""
    return Config.data_root() / "logs" / "clock_anchor.json"


def _read_anchor() -> Optional[float]:
    """读锚点里的「最后墙钟」—— 读不到就当没有(返回 None)。"""
    try:
        data = json.loads(anchor_path().read_text(encoding="utf-8"))
    except Exception:
        return None
    try:
        v = float(data.get("last_wall"))
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def enable_anchor(writes: bool = True) -> None:
    """让这个进程**负责维护锚点**(服务启动时开)。

    开了之后: 每分钟存一次 ⇒ 硬复位最多丢「关机前一分钟」那点时间。
    不开: 只读不写 —— 回归测试跑完, 仓库里一个文件都不多。
    """
    global _anchor_writes
    _anchor_writes = bool(writes)


def anchor_writes_enabled() -> bool:
    return _anchor_writes


def save_anchor(cur: Optional[float] = None) -> bool:
    """把「最近一次见到的墙钟」写进锚点文件。

    **每分钟存一次** —— 硬复位最多丢掉「关机前一分钟」那点时间, 而不是
    「上次干净关机」那 42 天。

    `cur` = **显式指定**要记的值(不给就算「此刻」)。`note_wall_clock()`
    喂进来一个「已知正确」的时间时用它 —— 那条路上**不能**再走 `now()`
    (它会拿板子自己的墙钟去比, 判定「时钟又倒退了」⇒ 把刚认下的真时间
    又推翻回去 ⇒ 推算模式永远结束不了)。

    写不进去就当没有(返回 False) —— 锚点只是保险, 写不进去**绝不能**
    把整个服务搞挂。
    """
    if not _anchor_writes:
        return False        # 没开维护 ⇒ 只读不写(见上面的 _anchor_writes)
    # ⚠️ **U 盘跟丢了 ⇒ 别往设备上写** —— 这正是「挂载点漂移」的**成因**:
    #   盘拔了以后服务照着旧路径往挂载点目录里写 ⇒ 那个目录永远非空 ⇒
    #   udisks2 下次插盘就往后排(AGING → AGING1)⇒ 漂移**自我强化**。
    #   ⇒ 跟丢的时候宁可不写(锚点只是保险), 也不能在设备上留东西。
    if Config.storage_lost():
        return False
    p = anchor_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        # ⚠️ **存「此刻的可信墙钟」, 不是「最后一次有人问时间」**
        #     (2026-11-06 实测, 症状: 界面读秒变成「2 个多小时」):
        #     `_last_observed` 只在有人调 `now()` 时才往前走 ⇒ 服务空转的
        #     那段时间它就**冻着** ⇒ 每分钟写一次的锚点也跟着冻着。
        #     实测: 服务 11:44 起, 跑完最后一轮是 11:56, 之后没再跑过测试
        #     ⇒ 锚点停在 `11:56:21` ⇒ 下一轮服务(14:24 起)拿这个当基准
        #     ⇒ **整个应用的时间比真实时间慢了 2 小时 28 分** ⇒ 顶栏读秒
        #     (浏览器自己的墙钟 − 后端的推算起点) 直接显示「2 个多小时」。
        #     ⇒ 锚点必须记「现在」: 推算模式下就是 `锚点 + 单调钟推出来的值`
        #     (它才是此刻最可信的墙钟), 墙钟模式下就是系统时钟本身。
        #
        # ⚠️ **模块还没判定过时**(`_initialized` 还是 False)⇒ 「可信墙钟」
        #     这个概念还不存在 ⇒ 用**原始墙钟**顶上。
        #     ⚠️ 之所以不直接调 `now()`: `now()` 在没判定过时会去调 `init()`,
        #     而 `init()` 是**读锚点**判时钟的 —— 「写锚点」去触发「读锚点」
        #     会绕成: 写锚点 → 读锚点 → 判成倒退 → 写锚点 ⇒ 存进去的值
        #     取决于**盘上那份旧锚点**, 而不是此刻。
        #     (服务启动时 `init()` 先跑, 它末尾会用判完的值重写一遍 ⇒
        #      落盘的最终值永远是对的。)
        if cur is None:
            cur = now() if _initialized else time.time()
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps({
            "version": ANCHOR_VERSION,
            "last_wall": round(cur, 3),
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S",
                                        time.localtime(cur)),
            "mode": _mode,
            "skew_sec": round(_skew_sec, 3),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(str(tmp), str(p))
        return True
    except Exception:
        return False


# ==================== 判定 ====================
def _skewed(raw: float) -> bool:
    """墙钟是不是比「已知值」还早 —— **判据只有这一处**。

    开机时比的是锚点(关机前最后一次记录), 运行中是比「见过的最大墙钟」;
    两条路共用这个判据 ⇒ 不会出现「开机认得出、运行中认不出」那种漏网。

    容差之内(≤ `SKEW_TOLERANCE_SEC`)不算倒退 —— NTP 校时拨零点几秒是
    正常现象, 不该把真时间标成不可信。
    """
    return raw < _last_observed - SKEW_TOLERANCE_SEC


def _enter_estimated(raw: float) -> None:
    """进入「推算模式」—— 从**最后一次可信的墙钟**往后推。"""
    global _mode, _ref_wall, _ref_mono, _skew_sec
    _mode = "estimated"
    _ref_wall = _last_observed
    _ref_mono = time.monotonic()
    _skew_sec = max(_skew_sec, _last_observed - raw)


def init() -> dict:
    """服务启动时调一次 —— 读锚点、判定系统时钟是不是倒退过。

    判定结果同时决定「这一轮的时间戳可不可信」⇒ 报告据此打不打黄条。
    """
    global _mode, _last_observed, _ref_wall, _ref_mono, _skew_sec, _initialized
    raw = time.time()
    anchor = _read_anchor()
    # 先把「已知值」摆好 —— 锚点(关机前最后一次记录)比墙钟可信。
    _last_observed = anchor if anchor else raw
    _skew_sec = 0.0
    if _skewed(raw):
        # 开机就发现墙钟比「关机前最后一次记录」还早 ⇒ 板子硬复位过,
        # 系统时间被退回到了上次干净关机那一刻 ⇒ 后面全用推算值。
        _enter_estimated(raw)
    else:
        _mode = "wall"
        _ref_wall = raw
        _ref_mono = time.monotonic()
    _initialized = True
    save_anchor()
    st = status()
    if st["skew_detected"]:
        print(f"[clock] ⚠ 系统时钟比上次记录倒退 {st['skew_days']:.1f} 天 "
              f"(板子硬复位过) ⇒ 本次运行的时间戳用推算值(只保证不倒退)",
              flush=True)
    return st


# ==================== 取时间 ====================
def now() -> float:
    """**可信的墙钟**(秒) —— 时钟倒退时给推算值。

    **所有要落库/上报告的时间戳都必须走这里**, 别直接用 `time.time()`。
    """
    global _last_observed
    if not _initialized:
        init()
    if _mode == "estimated":
        val = _ref_wall + (time.monotonic() - _ref_mono)
    else:
        raw = time.time()
        if _skewed(raw):
            # **运行中途**时钟被往回拨了(又复位了一次 / NTP 回拨)
            _enter_estimated(raw)
            val = _ref_wall + (time.monotonic() - _ref_mono)
        elif raw > _last_observed:
            val = raw
        else:
            # 小幅回拨或持平 —— 不许往回退(归档序列不许倒挂)
            val = _last_observed
    if val > _last_observed:
        _last_observed = val
    return val


def monotonic() -> float:
    """单调钟 —— **算时长只能用这个**。

    墙钟在运行中途跳变(哪怕只是 NTP 拨了零点几秒)都会让
    `结束 − 开始` 变成负数; 单调钟不会。
    """
    return time.monotonic()


def raw_now() -> float:
    """原始 `time.time()` —— 只用来**展示**「系统时钟本身歪了」, 别拿来记时间戳。"""
    return time.time()


def note_wall_clock(ts: float) -> bool:
    """登记一个「已知正确」的墙钟 —— 只接受往前的。

    以后要是接上 NTP / 手动校时, 用这个把真时间喂进来, 锚点立刻跟上,
    推算模式随即结束。喂进来的时间比已知值还早 ⇒ 忽略(那还是歪的)。
    """
    global _mode, _last_observed, _ref_wall, _ref_mono, _skew_sec
    try:
        ts = float(ts)
    except (TypeError, ValueError):
        return False
    if ts <= 0 or ts <= _last_observed:
        return False
    _mode = "wall"
    _last_observed = ts
    _ref_wall = ts
    _ref_mono = time.monotonic()
    _skew_sec = 0.0
    # ⚠️ **显式写 `ts`** —— 不能走 `save_anchor()` 默认那条路(它算「此刻」
    #   会调 `now()`, 而 `now()` 拿**板子自己的墙钟**去比 `_last_observed`
    #   ⇒ 刚认下的这个真时间比墙钟靠后 ⇒ 被判成「时钟又倒退了」⇒ 又回到
    #   推算模式 ⇒ **推算模式永远结束不了**(这正是 `note_wall_clock()`
    #   要干的事)。
    save_anchor(cur=ts)
    return True


# ==================== 状态 ====================
def _status_dict(now_val: float) -> dict:
    return {
        "mode": _mode,
        "skew_detected": _mode == "estimated",
        "skew_sec": round(_skew_sec, 3),
        "skew_days": round(_skew_sec / 86400.0, 4),
        "raw": time.time(),
        "now": now_val,
        "anchor": _last_observed,
    }


def status() -> dict:
    """当前时间状态(给 UI/日志/报告用)。"""
    if not _initialized:
        init()
    return _status_dict(now())


def is_estimated() -> bool:
    """现在用的是不是「推算值」。"""
    if not _initialized:
        init()
    return _mode == "estimated"


def skew_text() -> str:
    """给报告用的一句人话(正常时是空串)。"""
    if not is_estimated():
        return ""
    days = _skew_sec / 86400.0
    if days >= 1:
        return f"系统时钟倒退了 {days:.1f} 天"
    if _skew_sec >= 3600:
        return f"系统时钟倒退了 {_skew_sec / 3600.0:.1f} 小时"
    return f"系统时钟倒退了 {int(_skew_sec)} 秒"


# ==================== 每轮的时钟旁挂文件 ====================
def run_skew_path(run_id: int) -> Path:
    """某一轮的时钟旁挂文件 —— 跟证据文件同处(旁挂, 不写历史库)。"""
    return Config.data_root() / "logs" / f"clock_skew_run{run_id}.txt"


def write_run_skew(run_id: int) -> Optional[Path]:
    """**时钟正常时什么都不写** —— 只有推算值才留旁挂文件。

    为什么按轮留一份: 报告是**可以事后重导**的(§8.2)。如果只在服务级别
    记一个「现在歪不歪」, 那时钟后来被扳正之后重导一份旧报告, 横幅就没了
    —— 而那一轮的时间戳**本来就是推算值**。按轮留一份, 报告才说得准。
    """
    if not _anchor_writes:
        return None        # 服务没在维护锚点 ⇒ 不留旁挂文件(见 _anchor_writes)
    if not is_estimated():
        return None
    p = run_skew_path(run_id)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            "推算值 —— 本轮报告上的时间戳不是系统时钟的真实值\n"
            f"{skew_text()}({int(_skew_sec)} 秒)\n"
            f"推算基准: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(_ref_wall))}"
            "(关机前最后一次记录)\n"
            "※ 归档顺序可信(时间戳单调不减), 但**不能当准点用**。\n",
            encoding="utf-8")
        return p
    except Exception:
        return None


def read_run_skew(run_id: int) -> str:
    """读某一轮的时钟旁挂文件 —— 读不到/没写过就是空串。

    ⚠️ **必须容错解码, 不能严格解码** (2026-11-11 实测踩到):
        产线 U 盘上的旁挂文件**会被写坏** —— 实测盘上
        `logs/clock_skew_run20.txt` 整个 258 字节全是乱码
        (开头 4 个字节是 `ff ff ff ff`, 后面全是随机二进制),
        同一批盘上的 `cpu_evidence_run20.txt` 尾巴有 173 个 NUL 字节。
        ⇒ `read_text(encoding="utf-8")` **抛 UnicodeDecodeError**
        ⇒ 旧代码 `except Exception: return ""` ⇒ **整份旁挂文件静默消失**
        ⇒ 报告上**「本轮时间戳是推算值」那条横幅不见了**
        ⇒ 工人以为时间戳是真的 ⇒ **拿推算值当准点用**。

        ⇒ 跟 `boot_evidence._read_text_lenient` 一个口径:
           读字节 + `errors="replace"` ⇒ 坏的那几个字节变成 �,
           **前面的正经内容照样读得到**。

    ⚠️ **文件在、内容空 ⇒ 结论照样成立, 横幅不许消失** (2026-11-11 实测):
        产线盘上另有 **3 个 0 字节**的旁挂文件(`clock_skew_run19/36/38.txt`)
        —— `write_run_skew()` 是 `write_text()`, **先建/截断文件再写**,
        写到一半断电 ⇒ 留下一个**空文件**。
        ⇒ 空文件**仍然说明「这一轮是推算值」**(时钟正常时压根不写这个文件)
        ⇒ 但旧代码读回 `""` ⇒ **横幅照样消失** ⇒ **同一个后果**。
        ⇒ 所以: **文件不存在 ⇒ 空串(不打横幅)**; **文件在但读不出内容 ⇒
        照样给一句话**(横幅在, 只是细节丢了)。
        ⚠️ **别改成「先写 .tmp 再 rename」那种原子写法** —— 那样断电时
        主文件压根不会出现 ⇒ 「这一轮是推算值」这个信号**一起没了**
        ⇒ 又变成静默消失。
    """
    try:
        raw = run_skew_path(run_id).read_bytes()
    except OSError:
        return ""
    text = raw.decode("utf-8", errors="replace").strip()
    if text:
        return text
    # 文件在, 但内容是空的 ⇒ 这一轮**确实写过**旁挂文件 ⇒ 时间戳是推算值
    # ⇒ 横幅必须还在(细节丢了就说细节丢了, 不许连结论一起丢)
    return ("本轮时间戳是推算值 —— 但旁挂文件内容是空的(写入时中断), "
            "推算细节已丢失; 归档顺序可信, 但**不能当准点用**。")


# ==================== A1: 让板子自己把时间存下来 ====================
async def save_hwclock() -> Optional[bool]:
    """让**板子自己**把时间存下来(A1) —— `sudo -n /sbin/fake-hwclock save`。

    **为什么需要它**: `fake-hwclock` **只在干净关机时**存时间 ⇒ 硬复位 /
    掉电之后开机, 板子回到「上次干净关机」那一刻(实测**倒跳 42 天**)。
    让它**每 5 分钟存一次**, 硬复位最多丢 5 分钟, 而不是 42 天。

    ⚠️ **只在时钟可信时才存** —— 存进去的是「系统时钟此刻的值」。系统时钟
    本身歪着(倒退了 42 天)的时候存进去, 下次开机就从 42 天前开始 ⇒
    **越歪越歪**。所以推算模式下**不许存**。

    ⚠️ 存不进去就当没有 —— 那条 sudoers 规则没配就没配, **绝不能**因为
    存时间失败把服务搞挂(那才是真事故)。

    返回: **True** = 存上了; **False** = 试了但没成(sudoers 没配/命令失败);
    **None** = 不该存(时钟是推算值, 或者压根没有 sudo)。
    """
    if is_estimated():
        return None            # 时钟不可信 ⇒ 存进去只会越歪越歪
    if find_tool("sudo") is None:
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            "sudo", "-n", FAKE_HWCLOCK, "save",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=10)
        return proc.returncode == 0
    except Exception:
        return False
