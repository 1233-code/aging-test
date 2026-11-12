"""回归测试: 「板子死在哪**一项**上」的现场记录。

**起因 (2026-11-12 产线反馈)**
    另一台设备上跑老化: 第一次测试, **GPU 项**跑着的时候板子弹出内核崩溃画面
    (`crashdump now, please connect to tigerdump tool and keep power on`) ⇒
    板子挂死 ⇒ 工人断电重上 ⇒ 那一轮卡在「运行中」。
    第二次测试, 内存项的日志里「时间突然飙升」(那是**校时** —— 见
    `tests/test_memory_progress.py` 的第 7/8 例)。

**这个测试盯住的那条通道**
    板子死在 GPU / 内存项上时, 现有两条通道都答不上来:
      ① 「上一次启动的 journal 尾巴里看得见我们拉起来的工具」——
         `glmark2` / `memtester` 几乎不说话, 内核 oops 里的 `Comm: glmark2`
         那几行又常被挤出尾巴 ⇒ 认不出来;
      ② 「CPU 加压现场记录」—— **只管 CPU 那一项**。
    ⇒ 「板子死在 GPU 那一项上」压根没有证据 ⇒ 那一项只能判 `stopped`
    ⇒ **坏板子被放行**(§9.3: 静默的「通过」比明确的「失败」有害得多)。

    ⇒ 这里补一条**通用**通道: 每一项**起跑**写一行、**跑完**写一行,
      逐行 fsync, 落在 U 盘上(板子挂死它还在)⇒ 收尾时能说出
      「板子死在 GPU 这一项上」⇒ 那一项判 failed。

**它自己必须带的那道关**: 「板子**真的**重启过没有」(开机 ID 变了没有)。
    板子没重启 ⇒ 是**服务**自己没了 ⇒ **不许**赖到板子头上(冤枉好板)。

**这个测试不碰真库、不碰真硬件**: 数据目录换成临时目录, 开机 ID 换成假的。
跑法: .venv/bin/python -m tests.test_item_marker
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ⚠️ 数据目录必须先换成临时的 —— 绝不能往产线数据根(/media/User/AGING1)里写
from tests._tmp import mkdtemp                         # noqa: E402

DATA_DIR = mkdtemp(prefix="aging-item-marker-")
os.environ["AGING_DATA_DIR"] = DATA_DIR

from server import item_marker                          # noqa: E402

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


def write_marker(run_id, lines, boot_id="boot-A"):
    """造一份现场记录(代替真机跑出来的)。"""
    p = item_marker.marker_path(run_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    body = [f"{item_marker.MARK_BOOT_ID_MARK} {boot_id}", f"# 轮次: {run_id}"]
    body.extend(lines)
    p.write_text("\n".join(body) + "\n", encoding="utf-8")
    return p


def with_boot_id(fake, fn):
    """把 `current_boot_id()` 换成假的, 跑完还原。"""
    orig = item_marker.current_boot_id
    item_marker.current_boot_id = lambda: fake
    try:
        return fn()
    finally:
        item_marker.current_boot_id = orig


# ══════════════════════════════════════════════════════════════════════
section("【1】写端: 起跑/结束各一行, 头里记开机 ID")
# ══════════════════════════════════════════════════════════════════════
m = item_marker.ItemMarker(9001)
m.start("memory")
m.start("gpu")
m.done("gpu")
m.close()
text = item_marker.read_marker(9001)
check("起跑 memory" in text, "起跑写了一行", text)
check("结束 gpu" in text, "跑完写了一行", text)
check(item_marker.MARK_BOOT_ID_MARK in text, "头里记了开机 ID", text)
# 每一行都要**落盘**(fsync) —— 板子硬复位时页缓存整个丢, 只 flush 的没了
check(item_marker.marker_path(9001).exists(), "文件真的落盘了")

# 写端不许因为写不进去就把测试搞挂
m2 = item_marker.ItemMarker(None)
m2.start("x")
m2.done("x")
m2.close()
check(True, "run_id 为 None 时写端不抛异常(写不了就当没有)")

# ══════════════════════════════════════════════════════════════════════
section("【2】「起跑没结束」+ 板子真的重启过 ⇒ 说出死在哪一项上")
# ══════════════════════════════════════════════════════════════════════
write_marker(9002, ["起跑 memory", "结束 memory", "起跑 gpu"])
got = with_boot_id("boot-B", lambda: item_marker.marker_cut_short(9002))
check(got == "gpu", "认出「死在 gpu 这一项上」", got)

# ══════════════════════════════════════════════════════════════════════
section("【3】最后一行是「结束」⇒ 这一项跑完了 ⇒ 不赖它")
# ══════════════════════════════════════════════════════════════════════
write_marker(9003, ["起跑 gpu", "结束 gpu"])
got = with_boot_id("boot-B", lambda: item_marker.marker_cut_short(9003))
check(got is None, "这一项跑完了 ⇒ 不说它死了", got)

# 板子死在**两项之间**(上一项跑完了, 下一项没起跑)
write_marker(9004, ["起跑 memory", "结束 memory"])
got = with_boot_id("boot-B", lambda: item_marker.marker_cut_short(9004))
check(got is None, "死在两项之间 ⇒ 不赖到任何一项头上", got)

# ══════════════════════════════════════════════════════════════════════
section("【4】开机 ID 没变 ⇒ 是**我们**的进程没了 ⇒ 不许赖板子")
# ══════════════════════════════════════════════════════════════════════
write_marker(9005, ["起跑 gpu"], boot_id="boot-A")
got = with_boot_id("boot-A", lambda: item_marker.marker_cut_short(9005))
check(got is None, "板子没重启过 ⇒ 判 None(冤枉好板比漏判边缘板更糟)", got)

# 读不出开机 ID ⇒ 「不知道」⇒ 也不猜
write_marker(9006, ["起跑 gpu"], boot_id="")
got = with_boot_id("", lambda: item_marker.marker_cut_short(9006))
check(got is None, "读不到开机 ID ⇒ 判 None(不乱猜)", got)

# ══════════════════════════════════════════════════════════════════════
section("【5】压根没有这份记录 ⇒ 判 None")
# ══════════════════════════════════════════════════════════════════════
got = with_boot_id("boot-B", lambda: item_marker.marker_cut_short(999999))
check(got is None, "没有文件 ⇒ 判 None", got)

# ══════════════════════════════════════════════════════════════════════
section("【6】接线: runner 写、main 读 —— 少一处都不算数")
# ══════════════════════════════════════════════════════════════════════
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
runner_src = open(os.path.join(HERE, "server", "runner.py"), encoding="utf-8").read()
main_src = open(os.path.join(HERE, "server", "main.py"), encoding="utf-8").read()

check("self._marker.start(name)" in runner_src,
      "runner 在**每一项起跑时**写「起跑」")
check("self._marker.done(name)" in runner_src,
      "runner 在**每一项跑完后**写「结束」")
# ⚠️ 顺序: 「起跑」必须写在真正跑起来**之前** —— 板子死在半路时,
#    后面那些行压根没机会写。
i_start = runner_src.find("self._marker.start(name)")
i_run = runner_src.find("result = await instance.run(ctx)")
check(0 < i_start < i_run, "「起跑」写在真正跑这一项**之前**", (i_start, i_run))

check("item_marker.marker_cut_short" in main_src,
      "启动收尾用上了这条通道")
check("import item_marker" in main_src or "from . import item_marker" in main_src,
      "main.py 引了这个模块")

# ⚠️ **必须 fsync** —— 板子是被硬复位打死的 ⇒ 页缓存整个丢 ⇒ 只 flush 的
#    内容跟着板子一起没 ⇒ 恰恰是最该有证据的一次, 证据没了。
marker_src = open(os.path.join(HERE, "server", "item_marker.py"), encoding="utf-8").read()
check('            self._fh.write(line + "\\n")\n'
      '            self._fh.flush()\n'
      '            os.fsync(self._fh.fileno())\n' in marker_src,
      "每一行都 fsync(硬复位带不走)")

# ══════════════════════════════════════════════════════════════════════
section("【7】journal 尾巴也要认得 GPU/内存这两个工具")
# ══════════════════════════════════════════════════════════════════════
from server import boot_evidence                       # noqa: E402

check(boot_evidence.our_run_at_death("Nov 12 07:25:17 host sudo: COMMAND=/usr/bin/glmark2 --fullscreen"),
      "尾巴里看得见 glmark2 ⇒ 认得「我们在跑」")
check(boot_evidence.our_run_at_death("Nov 12 07:25:17 host sudo: COMMAND=/usr/bin/memtester 1024M 1"),
      "尾巴里看得见 memtester ⇒ 认得「我们在跑」")
check(boot_evidence.our_run_at_death("Nov 12 07:25:17 host kernel: CPU: 0 PID: 12 Comm: glmark2 Not tainted"),
      "内核 oops 的 `Comm: glmark2` 也认")

print("\n" + ("全部通过" if not FAILS else f"有失败项 ({len(FAILS)})"))
sys.exit(0 if not FAILS else 1)
