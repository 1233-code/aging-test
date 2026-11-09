"""回归测试: 锚点要记「**此刻**」, 前端读秒要用**后端的时钟**。

**起因 (2026-11-06, 产线原话)**
    「测试的总时间对不上, 每次都是开头一直是 0, 后面变两个多小时,
      然后测试结果上的时间又对得上。」

    两个症状, **一个病根** —— 前端和后端不在同一个时间尺度上:

      · **后端**: 板子的系统时钟被硬复位后退回 51.8 天前 ⇒ 后端的时间戳
        全走 `clock.now()` 的「锚点 + 单调钟」**推算**值(§9.3.4);
      · **前端**: 拿 `Date.now()`(浏览器看到的**系统时钟**)去减后端给的
        `started_at`(推算时间)⇒ **跨尺度相减**:
          - 系统时钟慢 ⇒ 差值是负数 ⇒ 被 clamp 成 00:00:00(整轮冻在 0);
          - 系统时钟被校正(前跳 51.8 天)之后 ⇒ 差值变成几十天;
          - 锚点要是停在几小时前 ⇒ 差值正好是那几小时 ⇒「2 个多小时」。

    报告上的时间**是对的**, 因为它只用后端的时间戳 ⇒ 「报告对得上、
    界面对不上」正是这个病根的指纹。

**修法分两头**
    ① **后端**: 锚点必须记「此刻最可信的墙钟」, 不是「最后一次有人问
       时间」—— `_last_observed` 只在有人调 `now()` 时才往前走, 服务空转
       的那段时间它**冻着** ⇒ 每分钟写一次的锚点也跟着冻着(实测: 锚点
       停在 `11:56:21`, 14:24 那轮拿它当基准 ⇒ 整个应用慢了 **2 小时
       28 分**);
    ② **后端 → 前端**: 每条事件 + 快照都带 `server_now`;
    ③ **前端**: 一律用 `server_now` 打底, 用浏览器的**单调钟**
       (`performance.now()`)往前推 —— 不受系统时钟跳变影响。

**这里守的是**
    ① 服务空转时锚点也得往前走(不能冻着);
    ② 墙钟模式下锚点记的就是系统时钟本身(不是「还没人问过」的 0);
    ③ 后端每条事件 + 快照都带 `server_now`;
    ④ 前端拿到 `server_now` 之后, 读秒不再用浏览器自己的墙钟。

**不碰真库、不碰真硬件**: 数据目录换成临时目录, 时钟状态自己拨。
跑法: .venv/bin/python -m tests.test_clock_anchor_fresh
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import clock as clock_mod                  # noqa: E402
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


# ── 数据目录换成临时目录 —— 绝不能把锚点写进仓库 ──────────────────
DATA_DIR = mkdtemp(prefix="aging-clock-fresh-")
os.environ["AGING_DATA_DIR"] = DATA_DIR

REAL_WALL = time.time()
DAY = 86400.0

#: 实测差的那一刀: **2 小时 28 分**(锚点停在 11:56:21, 那轮 14:24 起)
IDLE = 2 * 3600 + 28 * 60


def _reset_clock():
    """拨回「刚开机、什么都没判定」的状态。"""
    clock_mod._mode = "wall"
    clock_mod._last_observed = 0.0
    clock_mod._ref_wall = 0.0
    clock_mod._ref_mono = 0.0
    clock_mod._skew_sec = 0.0
    clock_mod._initialized = False


_SAVED = {k: getattr(clock_mod, k) for k in
          ("_mode", "_last_observed", "_ref_wall", "_ref_mono",
           "_skew_sec", "_initialized", "_anchor_writes")}


def _restore(saved):
    for k, v in saved.items():
        setattr(clock_mod, k, v)


anchor = clock_mod.anchor_path()


def _read_anchor():
    try:
        return json.loads(anchor.read_text(encoding="utf-8"))
    except Exception as e:
        return {"err": repr(e)}


# ══════════════════════════════════════════════════════════════════════
section("【1】服务空转 ⇒ 锚点也得往前走(**不能冻着**)")
# ══════════════════════════════════════════════════════════════════════
# 复现 2026-11-06 那台板子:
#   · 推算模式下(系统时钟倒退 51.8 天);
#   · 服务 11:44 起, 最后一轮跑完是 11:56 ⇒ 之后**没人再问过时间**
#     (`_last_observed` 从此冻在 11:56:21);
#   · 每分钟写一次的锚点也跟着冻 ⇒ 14:24 那轮拿它当基准
#     ⇒ 整个应用慢了 2 小时 28 分 ⇒ 顶栏读秒显示「2 个多小时」。
_reset_clock()
clock_mod._mode = "estimated"
clock_mod._skew_sec = 51.8 * DAY
clock_mod._ref_wall = REAL_WALL
clock_mod._ref_mono = time.monotonic()
clock_mod._last_observed = REAL_WALL
clock_mod._initialized = True
clock_mod.enable_anchor()

# 服务空转: 单调钟走了 IDLE 秒, 这期间**没人调过 now()**
clock_mod._ref_mono -= IDLE

ok = clock_mod.save_anchor()
saved = _read_anchor()
check(ok, "锚点写下去了", ok)
check("err" not in saved, "锚点读得回来", saved.get("err"))
check(float(saved.get("last_wall", 0)) >= REAL_WALL + IDLE - 5,
      "锚点记的是「**此刻**」(空转了 2 小时 28 分, 锚点也得往前走)",
      (saved.get("last_wall"), REAL_WALL + IDLE))
check(saved.get("mode") == "estimated",
      "锚点记了当时是推算模式(下次开机知道该按推算算)", saved.get("mode"))

# 再空转一段, 锚点还得再往前走 —— 不是「写一次就固定了」
clock_mod._ref_mono -= 600
clock_mod.save_anchor()
saved2 = _read_anchor()
check(float(saved2.get("last_wall", 0)) >= float(saved.get("last_wall", 0)) + 600 - 5,
      "再空转 10 分钟 ⇒ 锚点又往前走了",
      (saved.get("last_wall"), saved2.get("last_wall")))


# ══════════════════════════════════════════════════════════════════════
section("【2】墙钟模式下, 锚点记的就是**系统时钟本身**")
# ══════════════════════════════════════════════════════════════════════
# 「还没人问过时间」的时候(`_last_observed` 还是 0), 锚点也不许写 0 ——
# 写 0 等于把板子的时钟锁死在 1970 年。
_reset_clock()
clock_mod._mode = "wall"
clock_mod._last_observed = 0.0
clock_mod._ref_wall = REAL_WALL
clock_mod._ref_mono = time.monotonic()
clock_mod._initialized = True
clock_mod.enable_anchor()

ok = clock_mod.save_anchor()
saved = _read_anchor()
check(ok, "锚点写下去了", ok)
check(abs(float(saved.get("last_wall", 0)) - REAL_WALL) < 5,
      "锚点记的是系统时钟此刻的值(不是「还没人问过」的 0)",
      saved.get("last_wall"))
check(float(saved.get("last_wall", 0)) > 0,
      "锚点绝不是 0(0 = 把板子锁死在 1970 年)", saved.get("last_wall"))


# ══════════════════════════════════════════════════════════════════════
section("【3】后端每条事件 + 快照都带 `server_now`")
# ══════════════════════════════════════════════════════════════════════
# 前端要拿后端的时钟当基准 ⇒ 后端**必须**把它发下去; 少一处, 那一处
# 走的就还是浏览器自己的系统时钟 ⇒ 「2 个多小时」照旧。
runner_src = open(os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "server", "runner.py"), encoding="utf-8").read()

check('event.setdefault("server_now", clock.now())' in runner_src,
      "广播的每条事件都带上了后端自己的「现在」")
check('"server_now": clock.now(),' in runner_src,
      "快照也带上了(刷新页面/WS 重连之后靠它恢复计时)")
check("clock.now()" in runner_src.split("def snapshot")[0],
      "用的是 clock.now()(推算模式下的可信墙钟), 不是 time.time()")

# 动态验一把: 快照里真的有这个键
from server import runner as runner_mod                # noqa: E402

r = runner_mod.TestRunner()
snap = r.snapshot()
check("server_now" in snap, "TestRunner().snapshot() 里真的有 server_now",
      sorted(snap)[:8])
check(float(snap.get("server_now", 0)) > 0,
      "server_now 是个像样的时间戳", snap.get("server_now"))


# ══════════════════════════════════════════════════════════════════════
section("【4】前端: 拿到 `server_now` 之后, 读秒不再用浏览器自己的墙钟")
# ══════════════════════════════════════════════════════════════════════
app_src = open(os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "static", "app.js"), encoding="utf-8").read()

check("syncServerClock" in app_src, "前端有一个「用后端时钟重新打基准」的函数")
check("if (msg.server_now) syncServerClock(msg.server_now);" in app_src,
      "快照那条路**真的**在对表(不是被短路掉的)")
check("if (msg && msg.server_now) syncServerClock(msg.server_now);" in app_src,
      "WS 事件那条路**真的**在对表")
check(app_src.count("syncServerClock(msg.server_now)") >= 2,
      "快照和 WS 事件**两条路**都同步了 "
      f"(现在有 {app_src.count('syncServerClock(msg.server_now)')} 处)")
check("serverNow.value + (perfNow() - serverNowPerf.value) / 1000" in app_src,
      "读秒是「后端基准 + 浏览器单调钟往前推」(不受系统时钟跳变影响)")
check("perfNow" in app_src and "performance.now" in app_src,
      "单调钟用的是 performance.now()")
check("const elapsedSec = computed" in app_src
      and "clockNow.value" in app_src,
      "顶栏读秒算的是 clockNow(后端尺度), 不是 Date.now()")
# 「有后端基准就别用本机墙钟」这条分支必须还在, 但**只在没有基准时**
# 才允许退回去。
check("serverNow.value\n                    ? serverNow.value" in app_src
      or "serverNow.value ?" in app_src,
      "没有后端基准时才退回本机墙钟(有基准就绝不用)")


# ══════════════════════════════════════════════════════════════════════
section("【5】写锚点不许反过来**读**锚点(两个绕圈的坑)")
# ══════════════════════════════════════════════════════════════════════
# ⚠️ 这两个坑都是 2026-11-06 改 `save_anchor()` 时**踩出来的** ——
#    「锚点要记此刻」这个改法本身会把两条绕圈的路径带出来:
#
# ⓪ **写锚点 → 读锚点**: `save_anchor()` 若去调 `now()`, 而 `now()` 在
#    模块还没判定过时会去调 `init()`, 而 `init()` 是**读锚点**判时钟的
#    ⇒ 写锚点变成了「按盘上那份旧锚点重新算一遍」⇒ 存进去的值取决于
#    **上一轮**的锚点, 不是此刻。
#
# ⓪ **认下真时间又被推翻**: `note_wall_clock()`(NTP / 手动校时那条路)
#    喂进来一个「已知正确」的时间, 目的就是**结束推算模式**; 若它写
#    锚点时又走 `now()`, `now()` 会拿板子自己的墙钟去比 —— 刚认下的真
#    时间比墙钟靠后 ⇒ 被判成「时钟又倒退了」⇒ 又回到推算模式 ⇒
#    **推算模式永远结束不了**。
# ⇒ 两条都守在这里。

# ⓪ 先在盘上放一份「42 天之后」的旧锚点(模拟上一轮留下的)
_reset_clock()
clock_mod._initialized = True
clock_mod.enable_anchor()
old = _read_anchor()
old["last_wall"] = REAL_WALL + 42 * DAY
old["mode"] = "estimated"
anchor.write_text(json.dumps(old), encoding="utf-8")

# 模块「还没判定过」⇒ save_anchor() 不许拿那份旧锚点去算
_reset_clock()
clock_mod.enable_anchor()
ok = clock_mod.save_anchor()
saved = _read_anchor()
check(ok, "锚点写下去了", ok)
check(abs(float(saved.get("last_wall", 0)) - REAL_WALL) < 60,
      "模块没判定过时, 写进去的是**此刻**(不是盘上那份 42 天后的旧锚点)",
      (saved.get("last_wall"), REAL_WALL))

# ⓪ note_wall_clock(): 认下真时间 ⇒ 推算模式结束, 锚点里存的是那个真时间
_reset_clock()
clock_mod._last_observed = REAL_WALL + 42 * DAY
clock_mod._mode = "estimated"
clock_mod._skew_sec = 42 * DAY
clock_mod._initialized = True
TRUTH = REAL_WALL + 43 * DAY
ok = clock_mod.note_wall_clock(TRUTH)
check(ok, "接受了一个更靠后的墙钟(NTP / 手动校时走这条路)", ok)
check(not clock_mod.is_estimated(),
      "认下真时间之后**推算模式结束了**(没被又推翻回去)",
      clock_mod.status())
saved = _read_anchor()
check(abs(float(saved.get("last_wall", 0)) - TRUTH) < 5,
      "锚点里存的是**刚认下的那个真时间**(不是『此刻』的墙钟)",
      (saved.get("last_wall"), TRUTH))
check(saved.get("mode") == "wall",
      "锚点记了当时是墙钟模式(下次开机知道不用推算)", saved.get("mode"))


# ── 收尾: 把模块状态还原, 别影响后面跑的用例 ──────────────────────
clock_mod.enable_anchor(False)
_restore(_SAVED)

print("\n" + "─" * 60)
if FAILS:
    print(f"✗ {len(FAILS)} 个失败 / 共 {PASSES + len(FAILS)} 个:")
    for f in FAILS:
        print("   - " + f)
    sys.exit(1)
print(f"✓ 全部通过 ({PASSES} 个)")
