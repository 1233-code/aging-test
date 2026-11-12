"""「板子死在哪**一项**上」的现场记录 —— 逐行 fsync, 板子死了也带不走。

**为什么要有它** (2026-11-12 产线反馈: 另一台设备上, 板子在 **GPU 项**跑着
的时候弹出内核崩溃画面 `crashdump now, please connect to tigerdump tool and
keep power on`, 板子挂死 ⇒ 工人断电重上 ⇒ 那一轮卡在「运行中」):

  现有的两条「板子死了」通道, 对 GPU / 内存这两项**都答不上来**:
    ① 「上一次启动的 journal 尾巴里看得见我们拉起来的工具」
       (`boot_evidence.our_run_at_death`) —— GPU 项拉的是 `glmark2`、内存项
       拉的是 `memtester`, 这两个工具**几乎不说话**; 就算内核 oops 打了
       `Comm: glmark2`, 那一行也常常被 oops 的后半截挤出尾巴 ⇒ 认不出来;
    ② 「CPU 加压现场记录」(`boot_evidence.cpu_evidence_cut_short`)——
       **只管 CPU 那一项**, GPU / 内存死在半路它一声不响。

  ⇒ 「板子死在 GPU 那一项上」这件事**压根没有任何证据** ⇒ 那一项只能判
    `stopped`(没跑完)⇒ **板子被放行**(§9.3: 静默的「通过」比明确的「失败」
    有害得多 —— 这正是 2026-11-04 在 CPU 项上踩过一遍的坑)。

  ⇒ 这里补一条**通用**通道: 每项**起跑**时写一行「起跑 <项名>」, **跑完**
    时写一行「结束 <项名>」, **每一行都 fsync**(硬复位带不走 —— 文件在
    **U 盘**上, 板子挂了它还在)。收尾时看**最后一行**:
      · 最后一行是「起跑」⇒ 板子死在**这一项**上;
      · 再对一下**开机 ID**: 变了 ⇒ 板子真的重启过 ⇒ 赖板子;
        没变 ⇒ 是**服务**自己没了(工人重启服务 / 事件循环冻死)
        ⇒ **不赖**板子(冤枉好板比漏判边缘板更糟)。

  ⚠️ **开机 ID 跟时钟无关** —— 这台板子的墙钟会倒退几十天, 拿时间戳比是
     比不出来的(见 `server/clock.py` 的说明)。

  ⚠️ **落文件不落库** —— 跟「加压现场记录」「时钟旁挂文件」同处(旁挂,
     不写历史库): 板子死在半路时, 指标还没落库(指标是**跑完**才写的),
     跟着进程一起没了。
"""
import os
from pathlib import Path
from typing import Optional

from .boot_evidence import current_boot_id, evidence_boot_id
from .config import Config

#: 「这一项起跑了」那一行 —— 每项**起跑**时写。
MARK_START = "起跑 "
#: 「这一项跑完了」那一行 —— 每项**跑完**时写。
MARK_DONE = "结束 "
#: 文件头里记开机 ID 的那一行 —— 跟「加压现场记录」**同一个口径**
#: (`boot_evidence.evidence_boot_id` 就认这一行)。
MARK_BOOT_ID_MARK = "# 本次开机 ID:"


def marker_path(run_id: int) -> Path:
    """某一轮的「板子死在哪一项上」现场记录文件。"""
    return Config.data_root() / "logs" / f"item_marker_run{run_id}.txt"


def _read_lenient(path: Path) -> str:
    """容错读 —— 这份文件是板子死的那一刻写的, 尾巴很可能是半行。

    严格 UTF-8 解码会抛 UnicodeDecodeError ⇒ **整份证据全丢**(跟
    `boot_evidence._read_text_lenient` 同一个坑, 2026-11-02 实测踩过)。
    """
    try:
        raw = path.read_bytes()
    except OSError:
        return ""
    return raw.decode("utf-8", errors="replace")


def read_marker(run_id: int) -> str:
    """读回某一轮的现场记录(没有 / 读不了就空串)。"""
    if run_id is None:
        return ""
    return _read_lenient(marker_path(run_id))


def marker_cut_short(run_id: int) -> Optional[str]:
    """板子死在**哪一项**上 —— 返回那一项的名字; 没有就返回 None。

    判据(跟 CPU 现场记录**完全同口径**, 只是把「加压」换成「项」):
      ① 文件里**最后**一个标记是「起跑 <项名>」⇒ 这一项没跑完;
      ② **开机 ID 变了** ⇒ 板子真的重启过 ⇒ 是板子的锅, 不是我们进程的锅。

    ⚠️ 两条都满足才返回项名 —— 少一条都**不赖**板子:
      · 板子没重启过 ⇒ 是**服务**自己没了 ⇒ 冤枉好板;
      · 最后一行是「结束」⇒ 这一项跑完了 ⇒ 板子死在**别处**(比如两项之间),
        不赖到这一项头上。
    """
    text = read_marker(run_id)
    if not text:
        return None
    last = None
    for line in text.splitlines():
        if line.startswith(MARK_DONE):
            last = ("done", line[len(MARK_DONE):].strip())
        elif line.startswith(MARK_START):
            last = ("start", line[len(MARK_START):].strip())
    if not last or last[0] != "start":
        return None
    rec_boot = evidence_boot_id(text)
    cur_boot = current_boot_id()
    if not rec_boot or not cur_boot:
        return None        # 读不到开机 ID ⇒ 「不知道」⇒ 不乱猜
    if rec_boot == cur_boot:
        return None        # 板子没重启过 ⇒ 是我们自己的进程没了
    return last[1] or None


class ItemMarker:
    """「板子死在哪一项上」的**写**端 —— 每项起跑/跑完各写一行, 立刻 fsync。

    ⚠️ **为什么必须 fsync**: 板子是被**硬复位**打死的(掉电 / PMIC 复位)
       ⇒ **内存里的页缓存整个丢失** ⇒ 只 `flush()` 的内容跟着板子一起没
       ⇒ 恰恰是最该有证据的一次, 证据没了(跟 2026-11-02 在 CPU 现场记录上
       踩的一模一样)。**只有 `fsync()` 才把它写进存储**, 硬复位才带不走。

    ⚠️ **写不了就当没有** —— 记录证据是**加分项**, 绝不能因为写不进去就把
       整个测试搞挂(那才是真事故)。
    """

    def __init__(self, run_id: Optional[int]) -> None:
        self.path: Optional[Path] = None
        self._fh = None
        if run_id is None:
            return
        try:
            p = marker_path(run_id)
            p.parent.mkdir(parents=True, exist_ok=True)
            # "w" = 覆盖 —— 同一轮重跑(§7.3 原地重跑同一项)继续往后追加。
            self._fh = open(p, "w", encoding="utf-8")
            self.path = p
            boot = current_boot_id()
            self._fh.write(f"{MARK_BOOT_ID_MARK} {boot or '未知'}\n")
            self._fh.write(f"# 轮次: {run_id}\n")
            self._fh.flush()
            os.fsync(self._fh.fileno())
        except Exception:
            self._fh = None

    def start(self, name: str) -> None:
        """记「这一项起跑了」。"""
        self._write(f"{MARK_START}{name}")

    def done(self, name: str) -> None:
        """记「这一项跑完了」。"""
        self._write(f"{MARK_DONE}{name}")

    def _write(self, line: str) -> None:
        if self._fh is None:
            return
        try:
            self._fh.write(line + "\n")
            self._fh.flush()
            os.fsync(self._fh.fileno())
        except Exception:
            pass        # 写不进去就当没有 —— 绝不能把测试搞挂

    def close(self) -> None:
        if self._fh is None:
            return
        try:
            self._fh.flush()
            os.fsync(self._fh.fileno())
            self._fh.close()
        except Exception:
            pass
        self._fh = None


__all__ = [
    "ItemMarker",
    "marker_path",
    "marker_cut_short",
    "read_marker",
    "MARK_START",
    "MARK_DONE",
    "MARK_BOOT_ID_MARK",
]
