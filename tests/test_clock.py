"""回归测试: 板子重启后**时钟倒跳**, 归档不许乱 (§9.3.4)。

**起因 (2026-10-30)**
    产线板子**没有可靠的时钟源**。同型号板子上实测出来的机制:
      · 开机时真正在设时钟的是 `fake-hwclock`(`/sbin/fake-hwclock load`)——
        它把系统时钟设成 `/etc/fake-hwclock.data` 里记的那一刻;
      · **那个文件只在「干净关机」时写**(service 的 ExecStop = `fake-hwclock save`)
        —— **硬复位 / 掉电走不到关机流程**;
      · ⇒ 板子每硬复位一次, 下次开机就回到「上次干净关机」那一刻
        (新板实测: **倒跳 42 天**);
      · 唯一能扳回来的是 `ntp.service` —— **产线网通不了外网就没人扳**。

    ⇒ 报告上的时间是错的 —— 归档按时间排序会倒挂, 纸质追溯对不上。

**这个测试盯住的三条保证**
    ① **时间戳单调不减** —— 就算墙钟是错的, 归档序列也不倒挂;
    ② **哪一轮的时间不可信, 报告上说得清楚** —— 横幅, 不是默默地错;
    ③ **时长永远是对的** —— 时长用单调钟算, 墙钟跳变算不出负数。

**这个测试不碰真库、不碰真硬件**: 数据目录换成临时目录, 墙钟/单调钟换成假的。
跑法: .venv/bin/python -m tests.test_clock
"""
import asyncio
import json
import os
import sys
import time
import unittest.mock as mock
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import clock as clock_mod                  # noqa: E402
from server import report as report_mod                # noqa: E402
from tests._tmp import mkdtemp                         # noqa: E402

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


# ══════════════════════════════════════════════════════════════════════
# 数据目录换成临时目录 —— 绝不能把锚点/旁挂文件写进仓库, 更不能碰真数据
DATA_DIR = mkdtemp(prefix="aging-clock-")
os.environ["AGING_DATA_DIR"] = DATA_DIR


# ── clock 模块是**模块级状态**(一次开机一份) ──
# 测试要反复模拟「重新开机」⇒ 得能把状态拨回去。下面两个helper 就是干这个的。
def _reset_clock():
    """拨回「刚开机、什么都没判定」的状态。"""
    clock_mod._mode = "wall"
    clock_mod._last_observed = 0.0
    clock_mod._ref_wall = 0.0
    clock_mod._ref_mono = 0.0
    clock_mod._skew_sec = 0.0
    clock_mod._initialized = False


def _trustworthy_clock():
    """把状态拨成「时钟可信」(锚点写成此刻) —— A1 只在可信时才存。"""
    _reset_clock()
    clock_mod._last_observed = REAL_WALL
    clock_mod.save_anchor()
    _reset_clock()
    clock_mod.init()


def _restore_clock(saved):
    for k, v in saved.items():
        setattr(clock_mod, k, v)


_SAVED = {k: getattr(clock_mod, k) for k in
          ("_mode", "_last_observed", "_ref_wall", "_ref_mono",
           "_skew_sec", "_initialized", "_anchor_writes")}


class FakeTime:
    """假的墙钟/单调钟 —— 让测试能精确地制造「时钟倒退」。

    只替换 `clock` 模块看到的 `time` —— 别处(报告格式化等)用真 time。
    """

    def __init__(self, wall, mono=1000.0):
        self.wall = wall
        self.mono = mono

    def time(self):
        return self.wall

    def monotonic(self):
        return self.mono

    def localtime(self, ts):
        return time.localtime(ts)

    def strftime(self, fmt, t=None):
        return time.strftime(fmt, t if t is not None else time.localtime(self.wall))


REAL_WALL = time.time()
DAY = 86400.0


# ══════════════════════════════════════════════════════════════════════
section("【1】锚点文件落在数据目录下, 存得进也读得回")
# ══════════════════════════════════════════════════════════════════════
_reset_clock()
anchor = clock_mod.anchor_path()
check(str(anchor).startswith(DATA_DIR),
      "锚点在临时数据目录里(不是仓库里)", anchor)
check(anchor.name == "clock_anchor.json", "文件名认得出来", anchor)
check(anchor.parent.name == "logs", "跟证据文件同处(logs/)", anchor.parent)

# ⚠️ **没开「维护锚点」时不许写** —— 开发时数据根 = 代码根, 开着服务跑一轮
#    或者跑一次回归, 都会往**仓库里**落一个 logs/clock_anchor.json
#    (实测 19 个回归用例都会落) —— 仓库不是存放运行时数据的地方。
_reset_clock()
clock_mod._last_observed = REAL_WALL
check(clock_mod.save_anchor() is False,
      "没开维护锚点 ⇒ 写都不写(回归测试不会往仓库里落文件)")
check(not anchor.exists(), "仓库/数据目录里没落文件")

clock_mod.enable_anchor()
ok = clock_mod.save_anchor()
check(ok, "开了维护锚点 ⇒ 写得住")
# ⚠️ 读不回来时要**报失败**, 不能抛异常 —— 崩了不算「被断言咬住」。
data = {}
read_err = ""
try:
    data = json.loads(anchor.read_text(encoding="utf-8"))
except Exception as e:
    read_err = repr(e)
check(not read_err, "锚点真的落盘了(读得回来)", read_err)
check(abs(float(data.get("last_wall", 0)) - REAL_WALL) < 5,
      "锚点里记的是「最近一次见到的墙钟」", data.get("last_wall"))
check("updated_at" in data, "锚点带人话时间(人要看)", list(data))
check(int(data.get("version", 0)) == clock_mod.ANCHOR_VERSION,
      "锚点带格式版本(以后改结构时好认)", data.get("version"))


# ══════════════════════════════════════════════════════════════════════
section("【2】开机发现时钟倒退 ⇒ 判定成「推算值」")
# ══════════════════════════════════════════════════════════════════════
# 模拟: 上一次关机前记的锚点在**42 天之后**(也就是墙钟被退回到了 42 天前)
_reset_clock()
clock_mod._last_observed = REAL_WALL
clock_mod.save_anchor()
anchor.write_text(json.dumps({
    "version": 1, "last_wall": REAL_WALL + 42 * DAY,
    "updated_at": "2099-01-01 00:00:00", "mode": "wall", "skew_sec": 0.0,
}), encoding="utf-8")

_reset_clock()
st = clock_mod.init()
check(st["skew_detected"], "开机就发现系统时钟倒退 ⇒ 判定成推算值", st)
check(st["mode"] == "estimated", "模式是 estimated", st["mode"])
check(abs(st["skew_days"] - 42) < 0.01,
      "倒退了多少天算得出来(42 天)", st["skew_days"])
check("天" in clock_mod.skew_text(), "有一句人话能印在报告上",
      clock_mod.skew_text())
check(clock_mod.is_estimated(), "is_estimated() 跟状态一致")

# 推算值不能比锚点还早 —— 归档序列不许倒挂
n1 = clock_mod.now()
check(n1 >= REAL_WALL + 42 * DAY - 5,
      "推算值不早于锚点(归档顺序不倒挂)", (n1, REAL_WALL + 42 * DAY))
# 锚点里存的必须是**推算值** —— 存歪的墙钟的话, 下次开机连锚点都是错的,
# 一次比一次歪(等于把板子时钟锁死在 42 天前)。
saved = {}
try:
    saved = json.loads(anchor.read_text(encoding="utf-8"))
except Exception as e:
    saved = {"last_wall": -1, "err": repr(e)}
check(float(saved.get("last_wall", 0)) >= REAL_WALL + 42 * DAY - 5,
      "锚点里存的是推算值(不是歪的墙钟)—— 否则下次开机也被带歪",
      saved.get("last_wall"))


# ══════════════════════════════════════════════════════════════════════
section("【3】时钟没倒退 ⇒ 不该乱标「推算值」")
# ══════════════════════════════════════════════════════════════════════
# 锚点在**过去**(正常) ⇒ 不算倒退
_reset_clock()
clock_mod._last_observed = REAL_WALL
clock_mod.save_anchor()
_reset_clock()
st = clock_mod.init()
check(not st["skew_detected"], "锚点是过去的时间 ⇒ 不算倒退", st)
check(clock_mod.skew_text() == "", "没有倒退 ⇒ 没有黄条文案")

# 锚点比墙钟只快一点点(NTP 校时的正常误差) ⇒ 也不算倒退
_reset_clock()
clock_mod._last_observed = REAL_WALL
clock_mod.save_anchor()
d = json.loads(anchor.read_text(encoding="utf-8"))
d["last_wall"] = REAL_WALL + 60          # 只快 60 秒
anchor.write_text(json.dumps(d), encoding="utf-8")
_reset_clock()
st = clock_mod.init()
check(not st["skew_detected"],
      "只快一点点(60 秒)不算倒退 —— NTP 校时不该把真时间标成不可信", st)


# ══════════════════════════════════════════════════════════════════════
section("【4】时间戳单调不减 —— 归档序列永远不倒挂")
# ══════════════════════════════════════════════════════════════════════
# 墙钟正常运行 ⇒ 取两次, 第二次不能比第一次早
_reset_clock()
fake = FakeTime(REAL_WALL)
clock_mod.time = fake
clock_mod.init()
a = clock_mod.now()
fake.wall = REAL_WALL - 500          # 墙钟往回拨 500 秒(超过容差)
b = clock_mod.now()
check(b >= a, "墙钟往回拨 ⇒ 给出的时间没往回退", (a, b))
check(clock_mod.is_estimated(), "并且当场切成推算模式(而不是默默用错的墙钟)")

# 推算模式下继续走 ⇒ 一直往前
prev = b
for _ in range(5):
    fake.mono += 1.0
    cur = clock_mod.now()
    check(cur >= prev, "推算值只往前, 不往回", (prev, cur))
    prev = cur

# 小幅回拨(容差内)也不许往回退
_reset_clock()
fake2 = FakeTime(REAL_WALL)
clock_mod.time = fake2
clock_mod.init()
x = clock_mod.now()
fake2.wall = REAL_WALL - 3           # 只回拨 3 秒
y = clock_mod.now()
check(y >= x, "小幅回拨也不许让时间戳倒退", (x, y))
clock_mod.time = time


# ══════════════════════════════════════════════════════════════════════
section("【5】时长用单调钟 —— 墙钟跳变算不出负数时长")
# ══════════════════════════════════════════════════════════════════════
runner_src = open(os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "server", "runner.py"), encoding="utf-8").read()
check("time.monotonic() - t0" in runner_src,
      "这一项的时长是「单调钟 − 单调钟」算的")
check("clock.now() - t0" not in runner_src,
      "**不是**用墙钟算时长(墙钟跳变会算出负数)")

db_src = open(os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "server", "db.py"), encoding="utf-8").read()
check("time.time()" not in db_src,
      "落库的时间戳不再直接用 time.time()(全部走 clock.now())")

report_src = open(os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "server", "report.py"), encoding="utf-8").read()
check("generated_at=_fmt_time(clock.now())" in report_src,
      "报告的生成时间走 clock.now()")


# ══════════════════════════════════════════════════════════════════════
section("【6】按轮留旁挂文件 —— 报告据此打横幅")
# ══════════════════════════════════════════════════════════════════════
# 时钟正常 ⇒ 什么都不写(没有就不留空壳文件)
_reset_clock()
clock_mod.init()
p = clock_mod.write_run_skew(99)
check(p is None, "时钟正常时不写旁挂文件", p)
check(not clock_mod.run_skew_path(99).exists(), "也没留下空壳文件")
check(clock_mod.read_run_skew(99) == "", "读回来是空串(不是异常)")

# 推算值 ⇒ 写一份
_reset_clock()
clock_mod._last_observed = REAL_WALL + 42 * DAY
clock_mod._mode = "estimated"
clock_mod._skew_sec = 42 * DAY
clock_mod._initialized = True
p = clock_mod.write_run_skew(99)
check(p is not None, "推算值时写出旁挂文件", p)
check(p is not None and p.exists(), "文件真的在")
check(p is not None and p.name == "clock_skew_run99.txt", "文件名带轮号", p)
txt = clock_mod.read_run_skew(99)
check("推算值" in txt, "里面写明了这是推算值", txt[:80])
check("42" in txt, "写明了倒退了多少", txt[:120])
check(not clock_mod.run_skew_path(None).exists(),
      "不留 clock_skew_runNone.txt 这种鬼文件")


# ══════════════════════════════════════════════════════════════════════
section("【7】报告上有横幅 —— 纸面归档的人得知道时间不可信")
# ══════════════════════════════════════════════════════════════════════
def make_run():
    return {
        "id": 99,
        "sn": "SN-CLOCK-0001",
        "operator": "产线甲",
        "status": "stopped",
        "started_at": 1000,
        "finished_at": None,
        "summary": {"total": 3, "passed": 2, "failed": 0, "error": 0,
                    "skipped": 0, "not_run": 1, "planned": 4},
        "config": {"tests": {}},
        "error": "服务重启, 这一轮中途夭折(没跑完)",
    }


html = report_mod.render_html(make_run(), [], "x.pdf", clock_skew=txt)
check("推算值" in html, "报告上有「推算值」横幅")
check("不能当准点用" in html, "横幅说清了「归档顺序可信但不是准点」")
check("42" in html, "横幅里带着倒退了多少")

html_no = report_mod.render_html(make_run(), [], "x.pdf", clock_skew="")
check("推算值" not in html_no,
      "没有旁挂文件 ⇒ **不打横幅**(别给正常轮次也挂个黄条)")

# 默认按 run_id 自己读 —— 导出那条路不用每个调用点都记得传
html_auto = report_mod.render_html(make_run(), [], "x.pdf")
check("推算值" in html_auto, "默认按轮次 id 自己读旁挂文件(导出时不会漏)")


# ══════════════════════════════════════════════════════════════════════
section("【8】喂进一个「已知正确」的时间 ⇒ 推算模式结束")
# ══════════════════════════════════════════════════════════════════════
_reset_clock()
clock_mod._last_observed = REAL_WALL + 42 * DAY
clock_mod._mode = "estimated"
clock_mod._skew_sec = 42 * DAY
clock_mod._initialized = True
ok = clock_mod.note_wall_clock(REAL_WALL + 43 * DAY)
check(ok, "接受了一个更靠后的墙钟(以后接 NTP/手动校时就走这里)")
check(not clock_mod.is_estimated(), "推算模式结束 ⇒ 回到真时钟")
ok2 = clock_mod.note_wall_clock(REAL_WALL - 10 * DAY)
check(not ok2, "比已知值还早的时间被拒收(那还是歪的)")


# ══════════════════════════════════════════════════════════════════════
section("【9】A1: 让板子自己把时间存下来(fake-hwclock save)")
# ══════════════════════════════════════════════════════════════════════


class _FakeProc:
    def __init__(self, rc):
        self.returncode = rc

    async def wait(self):
        return self.returncode


def _recorder(called, rc=0):
    """假的 create_subprocess_exec —— 记下调用参数, 返回一个假进程。"""
    async def _exec(*args, **kw):
        called.append(list(args))
        return _FakeProc(rc)
    return _exec


# ⚠️ **推算值时绝对不许存** —— 存进去的是「系统时钟此刻的值」; 系统时钟
#    歪着(倒退 42 天)的时候存进去, 下次开机就从 42 天前开始 ⇒ 越歪越歪。
_reset_clock()
clock_mod._mode = "estimated"
clock_mod._skew_sec = 42 * DAY
clock_mod._initialized = True
called = []
with mock.patch("asyncio.create_subprocess_exec", new=_recorder(called)):
    r = asyncio.run(clock_mod.save_hwclock())
check(r is None, "时钟是推算值 ⇒ **不存**(存进去只会越歪越歪)", r)
check(not called, "根本没去调那条命令", called)

# 没有 sudo ⇒ 不存(不是「失败」)
_trustworthy_clock()
with mock.patch.object(clock_mod, "find_tool", lambda n: None):
    r = asyncio.run(clock_mod.save_hwclock())
check(r is None, "没有 sudo ⇒ 不存", r)

# 命令失败 ⇒ False, **不抛异常**(存时间失败绝不能把服务搞挂)
_trustworthy_clock()
called = []
with mock.patch.object(clock_mod, "find_tool", lambda n: "/usr/bin/sudo"), \
        mock.patch("asyncio.create_subprocess_exec", new=_recorder(called, rc=1)):
    r = asyncio.run(clock_mod.save_hwclock())
check(r is False, "命令失败 ⇒ 返回 False(不抛异常)", r)
check(called and called[0][:3] == ["sudo", "-n", "/sbin/fake-hwclock"],
      "调的是 `sudo -n /sbin/fake-hwclock ...`", called)
check(called and called[0][3] == "save", "调的是 **save**(存时间), 不是 load", called)

# 成功 ⇒ True
_trustworthy_clock()
called = []
with mock.patch.object(clock_mod, "find_tool", lambda n: "/usr/bin/sudo"), \
        mock.patch("asyncio.create_subprocess_exec", new=_recorder(called, rc=0)):
    r = asyncio.run(clock_mod.save_hwclock())
check(r is True, "存上了 ⇒ True", r)

# 服务那边: 每 5 分钟调一次
main_src = open(os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "server", "main.py"), encoding="utf-8").read()
check("await clock.save_hwclock()" in main_src,
      "服务定时调它(每 5 分钟一次)")
check("tick % 5 == 0" in main_src, "节奏是 5 分钟(不是每分钟 —— 那太勤了)")


# ══════════════════════════════════════════════════════════════════════
section("【10】导出那条路自己带上旁挂文件 —— 不靠每个调用点都记得传")
# ══════════════════════════════════════════════════════════════════════
# 【7】验的是 `render_html` 自己按轮次 id 读旁挂文件。但**导出**走的是
# `export_pdf` —— 它得把读到的旁挂文件**传下去**; 哪一步要是把它换成空串,
# 报告上那条黄条就**静悄悄地没了**(§9.3.4 的保证②)。
#
# ⚠️ 这条是 2026-11-04 补上的**覆盖漏洞**: 以前没人测 export_pdf 这一层 ⇒
#   把「自动带上旁挂文件」改成「一律传空串」, 【7】照样全绿(实测: 变异脚本
#   报「测试还是绿的! 这个变异没被守住」)。而 export_pdf 是**自动导出、
#   手动重导、父轮合并重导**三条路共同走的最后一个关口。
#
# ⚠️ 不真跑 chromium —— 只把「打印成 PDF」那一步换掉, HTML 照样是真的。
_html_seen = {}


async def _fake_print(chromium, html_path, pdf_path):
    """假的打印 —— 把 HTML 留下来, 再造一个占位的 PDF。"""
    _html_seen["html"] = Path(html_path).read_text(encoding="utf-8")
    Path(pdf_path).write_bytes(b"%PDF-1.4 fake\n")


with mock.patch.object(report_mod, "find_chromium", lambda: "/bin/true"), \
        mock.patch.object(report_mod, "_print_pdf", _fake_print), \
        mock.patch.object(report_mod, "verify_cjk", lambda p: None):
    _pdf = asyncio.run(report_mod.export_pdf(make_run(), []))

check(bool(_pdf), "导出了一份 PDF", _pdf)
_html = _html_seen.get("html", "")
check("推算值" in _html,
      "导出的报告上也有「推算值」横幅(export_pdf 自己带上了旁挂文件)")
check("不能当准点用" in _html, "横幅内容也带进去了")


# ── 收尾: 把模块状态还原, 别影响后面跑的用例 ──
clock_mod.enable_anchor(False)
_restore_clock(_SAVED)

print("\n" + "─" * 60)
if FAILS:
    print(f"✗ {len(FAILS)} 个失败 / 共 {PASSES + len(FAILS)} 个:")
    for f in FAILS:
        print("   - " + f)
    sys.exit(1)
print(f"✓ 全部通过 ({PASSES} 个)")
