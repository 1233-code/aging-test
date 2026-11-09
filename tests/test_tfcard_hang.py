"""回归测试: 卡挂死(设备无响应)时的判定与收尾 (2026-10-28 产线事故)

起因 (现场实况, 一字不改地记在这儿):
  工人插上 TF 卡重测 → fio 开始写 → **卡不应答了**, 一直不结束出不来结果 →
  工人手动按了「暂停」→ 结果那一轮在报告上变成了 **fail** → 再点一次重测 →
  **整个服务冻死**了(/api/health 都不再响应), 现场还留下两个杀不掉的 fio
  进程在往那张死卡上写。

查下来是**两件事**:
  · 卡确实坏了(CID 的厂商 ID 是白牌卡的 0xFE、第一次 1MiB 直写就永远
    不返回) —— 那是「这张卡不合格」;
  · 但**「fail」这个结论是工具给的, 不是卡给的** —— 卡压根没被判过,
    读都还没开始测。而工具在卡挂死时的反应有三个 bug:
      ① 「用户按暂停」被记成 **error** → 整轮判 failed(§9.3 明写: 用户中途
         停止 → 这一项记 stopped, 不计入 total);
      ② 卡对读写**没应答**这件事没人认出来, 笼统一句
         `RuntimeError("fio write 被中断 (取消或超时)")` —— 分不清是**工具
         坏了**还是**卡坏了**, 而这两种的产线动作完全相反(修工具 / 换卡);
      ③ 收尾时在**事件循环里**直接 `os.unlink()` 卡上的测试文件 —— 卡挂死时
         这个调用永远不返回(D 态, 内核不可中断), 于是**整个事件循环冻结,
         整个服务死掉**。内核栈实测:
             `unlinkat → vfs_unlink → rwsem_down_write_slowpath`。

这个测试守的:
  ① 卡无响应 → 这一项判 **failed**, 报错里明说「无响应」, 并列出没收掉的
     进程(那些进程拔卡复位前还在写卡, 操作员必须知道);
  ② 用户按「停止」→ 这一项判 **stopped**(不是 error, 也不是 failed);
  ③ fio 的三种收尾(跑完 / 用户中止 / 到点没应答)分得开;
  ④ 收尾**不许**在事件循环里直接摸卡 —— 凡是碰卡路径的同步文件操作一律
     不许出现(这就是③号 bug 的根);
  ⑤ 好卡不误判(回归: 正常卡还是 passed);
  ⑥ 整轮口径: 人工暂停的那一轮**不该**判 failed。

不碰真硬件: 卡、挂载、fio、清理全是假的; 也不写生产库、不写 reports/pdf/。
跑法: .venv/bin/python -m tests.test_tfcard_hang
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.tests import tfcard as F
from server.tests import emmc as E
from server.tests import _tools as T
from server.tests.base import (
    TestCase, TestResult, TestCancelled, DeviceUnresponsive,
)

FAILS = []
PASSES = 0

MOUNT = "/media/User/SDCARD"          # 假的卡挂载点
TEST_FILE = os.path.join(MOUNT, ".aging_test_tf.bin")


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


# ── 假的 ctx / 假的挂载层 ──────────────────────────────────────────────

class FakeCtx:
    def __init__(self, test_config=None, cancelled=False):
        self.config = {}
        self.test_config = test_config or {}
        self.logs = []
        self.progress = []
        self.cancelled = cancelled

    async def on_log(self, line): self.logs.append(line)

    async def on_progress(self, pct, msg): self.progress.append((pct, msg))

    async def on_metric(self, k, v): pass

    async def is_cancelled(self): return self.cancelled


class Patch:
    """把模块里的几个名字换掉, 退出时还原。"""

    def __init__(self, mod, **kw):
        self.mod = mod
        self.kw = kw
        self.saved = {}

    def __enter__(self):
        for k, v in self.kw.items():
            self.saved[k] = getattr(self.mod, k)
            setattr(self.mod, k, v)
        return self

    def __exit__(self, *exc):
        for k, v in self.saved.items():
            setattr(self.mod, k, v)
        return False


async def fake_wait_for_dev(dev, timeout_sec=3.0):
    return True


# ── 「不许在事件循环里直接摸卡」的哨兵 ───────────────────────────────

class SyncCardIO(Exception):
    """有人在事件循环里对卡做了同步文件操作 —— 就是那次事故的根。"""


class NoSyncCardIO:
    """把 os.unlink / os.remove / os.path.exists / os.stat 换成会喊的版本。

    只要有人拿**卡挂载点下面**的路径调这些函数, 就抛 —— 因为卡挂死时
    这些调用会永远不返回(D 态), 而它们是同步的, 跑在事件循环线程里,
    整个服务就此冻结。

    挂载点**外面**的路径(/dev/mmcblk1p1、/sys/... 等)不拦 —— 那些不碰
    卡的文件系统, 不会卡。
    """

    NAMES = ("unlink", "remove", "stat", "lstat")

    def __enter__(self):
        self.saved = {}
        for name in self.NAMES:
            orig = getattr(os, name, None)
            if orig is None:
                continue
            self.saved[name] = orig
            setattr(os, name, self._make_guard(name, orig))
        # os.path.exists 内部调 os.stat, 单独也拦一道(拦的是「exists」这个
        # 入口, 因为有些代码路径会先 exists 再 unlink)
        self.saved_exists = os.path.exists
        os.path.exists = self._make_guard("exists", self.saved_exists)
        return self

    def __exit__(self, *exc):
        for name, orig in self.saved.items():
            setattr(os, name, orig)
        os.path.exists = self.saved_exists
        return False

    def _make_guard(self, name, orig):
        def guard(path, *a, **kw):
            try:
                p = os.fspath(path)
            except TypeError:
                p = path
            if isinstance(p, str) and p.startswith(MOUNT):
                raise SyncCardIO(
                    f"事件循环里对卡做了同步文件操作: {name}({p!r}) —— "
                    f"卡挂死时这个调用永远不返回, 会把整个服务冻死")
            return orig(path, *a, **kw)
        return guard


# ── 假的清理层: 记下发了什么命令 ─────────────────────────────────────

class FakeRunCmd:
    """顶掉 _tools.run_cmd —— 记录命令, 不真的跑。"""

    def __init__(self, rc=0):
        self.cmds = []
        self.rc = rc

    async def __call__(self, cmd, timeout=5.0, input_data=None):
        self.cmds.append(list(cmd))
        return self.rc, "", ""


async def run_tf_item(fio_impl, cancelled=False, mount_info=None):
    """跑一次 TF 卡项: 挂载层和 fio 层都是假的, 清理层记命令。

    返回 (result, ctx, cmds)。
    """
    mi = mount_info or F._MountInfo(MOUNT, None, "existing")

    async def _ensure(ctx, disk, dev):
        return mi

    fake = FakeRunCmd()
    async def fake_fio(test_file, size_mb, ctx, duration_sec,
                       on_metric=None, on_progress=None):
        return await fio_impl()

    with Patch(F, _ensure_mounted=_ensure, _wait_for_dev=fake_wait_for_dev,
               _run_fio_test=fake_fio,
               _card_disks=lambda root_disk=None: ["mmcblk1"],
               sudo_hint_if_unavailable=lambda result, name: True):
        # 清理层用的是 _tools.remove_file_async → 它内部调 _tools.run_cmd
        with Patch(T, run_cmd=fake):
            ctx = FakeCtx({"times": 1, "duration_sec": 10, "min_read_mbps": 10},
                          cancelled=cancelled)
            res = await F.TFCardTest().run(ctx)
            return res, ctx, fake.cmds


# ══════════════════════════════════════════════════════════════════════
section("【1】卡对读写没应答 → 这一项判 failed, 而且明说「无响应」")

LEFTOVER_MSG = (
    "fio write 跑到挂钟上限(150s)仍未结束 —— 设备没有应答; "
    "⚠ 还有 2 个 fio 进程没收掉(PID 227024, 228388): "
    "设备卡死时进程会卡在内核不可中断状态, 任何信号都杀不掉它, "
    "需要把卡拔出来复位才能清掉"
)


async def _hang():
    raise DeviceUnresponsive(LEFTOVER_MSG)


res, ctx, cmds = asyncio.run(run_tf_item(_hang))
check(res.status == "failed",
      "卡没应答 → 这一项判 **failed** (这张卡不合格, 要换卡)", res.status)
check("无响应" in (res.error or ""),
      "报错里明说「无响应」—— 工人一眼能看出是卡的问题, 不是工具的问题",
      res.error)
check("换一张" in (res.error or ""),
      "报错里给出产线动作: 这张卡不合格, 请换一张", res.error)
check("227024" in (res.error or "") and "228388" in (res.error or ""),
      "报错里列出**没收掉的 fio 进程 PID** —— 它们还在往死卡上写, "
      "操作员必须知道", res.error)
check(any("无响应" in ln for ln in ctx.logs),
      "日志里也写了「无响应」", ctx.logs[-3:])
check(res.metrics.get("no_card") is None,
      "不是「没插卡」(卡明明在槽里)", res.metrics)

section("【2】用户按「停止」→ 这一项判 stopped, **不是** error/failed")


async def _cancelled():
    raise TestCancelled("fio write 被用户中止")


res, ctx, cmds = asyncio.run(run_tf_item(_cancelled))
check(res.status == "stopped",
      "用户中止 → 判 **stopped** (§9.3: 没跑完 ≠ 不合格)", res.status)
check(res.status not in ("error", "failed"),
      "绝不是 error/failed —— 那是 2026-10-28 那次「fail」的来源", res.status)
check("用户中途停止" in (res.error or ""),
      "报错写明是人工中止", res.error)
check(any("stopped" in ln for ln in ctx.logs),
      "日志里写明「记 stopped (不计失败)」", ctx.logs[-3:])

section("【3】fio 的三种收尾要分得开 (emmc._run_fio_test 这一层)")


class FakeProc:
    """假装是个子进程 —— 只为了让 create_subprocess_exec 不真起进程。"""

    def __init__(self):
        self.returncode = 0
        self.pid = 4242
        self.stdout = self

    async def readline(self):
        return b""

    async def wait(self):
        return self.returncode


async def call_one_phase(outcome, leftovers=()):
    """让 _run_fio_test 跑一个阶段, 返回它抛的异常(没有就返回 None)。"""
    import server.tests.emmc as emmc_mod

    async def fake_create(*a, **kw):
        return FakeProc()

    async def fake_progress(proc, duration_sec, ctx=None, on_tick=None,
                            tick_sec=1.0, hard_limit_sec=None):
        return outcome

    saved_create = asyncio.create_subprocess_exec
    asyncio.create_subprocess_exec = fake_create
    try:
        with Patch(emmc_mod, run_proc_with_progress=fake_progress,
                   find_tool=lambda name: "/usr/bin/fio",
                   leftover_fio_pids=lambda marker: list(leftovers)):
            ctx = FakeCtx()
            try:
                await emmc_mod._run_fio_test(TEST_FILE, 64, ctx, 30)
            except (TestCancelled, DeviceUnresponsive, RuntimeError) as e:
                return e
            return None
    finally:
        asyncio.create_subprocess_exec = saved_create


# ① 跑完了
exc = asyncio.run(call_one_phase(T.ProcOutcome(0, "WRITE: bw=30MiB/s", False, "")))
check(exc is None, "fio 正常跑完 → 不抛异常", exc)

# ② 用户按了停止
exc = asyncio.run(call_one_phase(
    T.ProcOutcome(-15, "", True, "cancelled")))
check(isinstance(exc, TestCancelled),
      "用户中止 → 抛 TestCancelled (→ 这一项记 stopped)", exc)

# ③ 到点没应答(我们自己的硬上限)
exc = asyncio.run(call_one_phase(
    T.ProcOutcome(-9, "", True, "hard_limit"), leftovers=(227024, 228388)))
check(isinstance(exc, DeviceUnresponsive),
      "到硬上限 → 抛 DeviceUnresponsive (→ 这一项记 failed)", exc)
check("227024" in str(exc) and "228388" in str(exc),
      "DeviceUnresponsive 里带上没收掉的进程 PID", str(exc))

# ④ `timeout` 到点(退出码 124)—— 这是「设备没应答」最典型的信号
exc = asyncio.run(call_one_phase(
    T.ProcOutcome(124, "", False, ""), leftovers=(227024,)))
check(isinstance(exc, DeviceUnresponsive),
      "`timeout` 到点(rc=124) → 抛 DeviceUnresponsive", exc)
check("没有应答" in str(exc),
      "报错里说的是「设备没有应答」, 不是笼统的「异常」", str(exc))

# ⑤ fio 自己报错 → 普通的 error
exc = asyncio.run(call_one_phase(T.ProcOutcome(1, "boom", False, "")))
check(isinstance(exc, RuntimeError) and not isinstance(
        exc, (TestCancelled, DeviceUnresponsive)),
      "fio 自己报错 → 普通 RuntimeError (→ 这一项记 error)", exc)

# ⑥ 遗留进程的说明
note = E._leftover_note([1, 2])
check("2 个 fio 进程没收掉" in note and "PID 1, 2" in note,
      "遗留进程的说明写清了几个、哪些 PID", note)
check(E._leftover_note([]) == "",
      "没有遗留进程时不啰嗦", repr(E._leftover_note([])))

section("【4】收尾不许在事件循环里直接摸卡 (那是把整个服务冻死的根)")


async def _good():
    return {"read_mbps": 50.0, "write_mbps": 30.0}


# ⚠️ 哨兵喊了要**接住**, 不能让它把整个测试搞崩 —— 崩了只能证明「跑不起来」,
#    证明不了「断言抓得住这个缺陷」(这是变异测试的规矩)。
sync_io_error = None
res = cmds = None
try:
    with NoSyncCardIO():
        res, ctx, cmds = asyncio.run(run_tf_item(_good))
except SyncCardIO as e:
    sync_io_error = e
check(sync_io_error is None,
      "收尾全程**没有**在事件循环里对卡做同步文件操作 "
      "(卡挂死时那个调用永远不返回, 会把整个服务冻死)", sync_io_error)
check(res is not None and res.status == "passed",
      "正常卡 → 判 passed (收尾改走子进程没有把正常路径弄坏)",
      res.status if res else None)
rm_cmds = [c for c in (cmds or []) if c[:2] == ["rm", "-f"]]
check(len(rm_cmds) == 1 and rm_cmds[0][-1] == TEST_FILE,
      "测试文件是**用子进程**删的 (rm -f), 不是在事件循环里 os.unlink", cmds)
check(not any(c[:1] == ["sudo"] for c in rm_cmds),
      "第一次删就用普通用户试 (文件在用户可写的挂载点里, 通常就够)", cmds)

# 收尾这一步单独再验一次: 挂载点下的同步操作一个都不许有
sync_io_error2 = None
try:
    with NoSyncCardIO():
        asyncio.run(F._remove_test_file(FakeCtx(), TEST_FILE))
except SyncCardIO as e:
    sync_io_error2 = e
check(sync_io_error2 is None,
      "收尾这一步单独跑也没有碰卡的同步文件操作 (哨兵没喊)", sync_io_error2)

section("【5】好卡不误判 (回归)")

res, ctx, cmds = asyncio.run(run_tf_item(_good))
check(res.status == "passed", "好卡 → passed", res.status)
check(res.metrics.get("round_1_read_mbps") == 50.0,
      "读速照常记进指标", res.metrics)
check(res.metrics.get("round_1_write_mbps") == 30.0,
      "写速照常记进指标", res.metrics)

section("【6】整轮口径: 人工暂停的那一轮**不该**判 failed")

from server import runner as R

FINISHED = []
WRITTEN = []


class FakeDb:
    @staticmethod
    async def create_run(config, meta=None): return 1

    @staticmethod
    async def create_mini_run(pid, config, meta=None): return 99

    @staticmethod
    async def get_run(rid): return None

    @staticmethod
    async def upsert_result(run_id, test_name, **fields):
        WRITTEN.append((run_id, test_name, fields))

    @staticmethod
    async def get_result_log(run_id, test_name): return None

    @staticmethod
    async def finish_run(run_id, status, summary, error=None):
        FINISHED.append((run_id, status, summary))

    @staticmethod
    async def set_pdf_path(*a, **k): pass


class FakeReport:
    @classmethod
    async def export_pdf(cls, run, results): return "/fake/report.pdf"

    @staticmethod
    def find_chromium(): return "/fake/chromium"

    @staticmethod
    def output_dir(): return None


R.Database = FakeDb
R.report = FakeReport
R.INTERVAL_BETWEEN_TESTS_SEC = 0


def make_test(name, status, error=None, on_run=None):
    class T(TestCase):
        def __init__(self):
            self.name = name
            self.display_name = f"假测试项-{name}"

        async def run(self, ctx):
            if on_run:
                await on_run(ctx)
            if status == "raise-cancelled":
                raise TestCancelled("工人按了停止")
            return TestResult(status=status, error=error)

    T.name = name
    T.display_name = f"假测试项-{name}"
    return T


async def run_round(specs):
    FINISHED.clear()
    WRITTEN.clear()
    enabled = [(sp["name"], make_test(sp["name"], sp.get("status", "passed"),
                                      sp.get("error"), sp.get("on_run")))
               for sp in specs]
    rn = R.TestRunner()
    rn.run_id = 1
    rn.status = "running"
    cfg = {"tests": {n: {"enabled": True} for n, _ in enabled}, "project_root": ""}
    await rn._run_tests_body(enabled, cfg, write_to_parent=False)
    summary = FINISHED[-1][2] if FINISHED else {}
    status = FINISHED[-1][1] if FINISHED else None
    return rn, summary, status


# ① 工人按了停止 → 那一项 stopped → 整轮**不是** failed
rn, summary, status = asyncio.run(run_round([
    {"name": "cpu", "status": "passed"},
    {"name": "tfcard", "status": "stopped", "error": "用户中途停止 (本项未跑完)"},
]))
check(status != "failed",
      "有一项是人工暂停的 → 整轮**不判 failed** (2026-10-28 就是这么判成 fail 的)",
      status)
check(summary.get("total") == 1,
      "stopped 不计入 total (§9.2: total = passed + failed + error)", summary)
check(summary.get("not_run") == 1,
      "stopped 单列在 not_run 里, 不混进 skipped", summary)

# ② 卡无响应 → 这一项 failed → 整轮判 failed (这是**应该**failed 的那种)
rn, summary, status = asyncio.run(run_round([
    {"name": "cpu", "status": "passed"},
    {"name": "tfcard", "status": "failed",
     "error": "TF 卡无响应: …… —— 这张卡不合格, 请换一张"},
]))
check(status == "failed",
      "卡无响应 → 整轮判 failed (设备不合格, 该拦下来)", status)
check(summary.get("failed") == 1, "failed = 1", summary)

# ③ 一项都没跑 → skipped, 不是 passed
rn, summary, status = asyncio.run(run_round([
    {"name": "a", "status": "skipped"},
]))
check(status == "skipped", "全跳过 → 整轮 skipped (不是 passed)", status)

# ④ 测试项**抛出** TestCancelled(而不是自己判 stopped)→ runner 也要认
rn, summary, status = asyncio.run(run_round([
    {"name": "cpu", "status": "passed"},
    {"name": "tfcard", "status": "raise-cancelled"},
]))
check(status != "failed",
      "测试项抛 TestCancelled → 整轮也不该判 failed", status)
stopped_rows = [w for w in WRITTEN if w[1] == "tfcard"
                 and w[2].get("status") == "stopped"]
check(len(stopped_rows) == 1,
      "测试项抛 TestCancelled → 落库那一行记 **stopped**(不是 error)",
      [(w[1], w[2].get("status")) for w in WRITTEN])

# ── 汇总 ─────────────────────────────────────────────────────────────
print("\n" + "=" * 60)
if FAILS:
    print(f"✗ {len(FAILS)} 项失败 / 共 {PASSES + len(FAILS)} 项")
    for f in FAILS:
        print("   - " + f)
    sys.exit(1)
print(f"✓ 全部通过 ({PASSES} 项)")
sys.exit(0)
