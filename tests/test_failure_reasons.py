"""回归测试: **每一项失败都必须说清楚为什么**(2026-11-12 全量排查)。

起因(用户的原话):
    「还有一点要仔细检查清楚, 就是每一次失败的原因不管哪一个模块, 失败
     原因都想清楚的描述, 因为如果只有报错日志的话普通工人看不懂。我看
     现在有些报错原因是有的, 但是不知道其他的是不是也有, 所以要仔细检查
     一遍」

查出来的实情:
    · 11 个测试项里, **只有一部分**会写 `result.error`(工人看得到的那一栏);
    · CPU 项里 stress-ng **退出码非 0** 那条路**一个字都没写** ⇒ 报告上
      这一项是「不合格」+ 原因栏是空的 ⇒ 模板只能印「(未记录原因 —— 属
      实现缺陷)」⇒ 工人不知道是**板子扛不住**还是**工具自己挂了**;
    · 好几个模块的「缺工具」补救办法**写死成** `sudo apt install …`
      ⇒ **U 盘版上这条命令是死的**(盘上自带 fio / glmark2, 其余工具用
      板子出厂那份)⇒ 照着做也装不上;
    · 有一批原因直接塞**裸异常**(`f"{e}"` / `f"{e!r}"`)⇒ 工人看到的是
      「FileNotFoundError: [Errno 2] …」这种没法读的东西。

这个测试守六件事:
  ① **静态**: 任何一条判 failed/error 的路径, 都必须同时写 `result.error`
     —— 用**路径敏感**的 AST 扫描(能看出「某个分支里 status=failed 但
     error 没赋值」), 而不是简单 grep;
  ② **静态**: 不许再出现裸异常 repr(`{e!r}`)直接塞进原因;
  ③ **静态**: 缺工具的补救办法不许写死 `sudo apt install` —— 必须走
     `deps_fix_hint()`(U 盘版 / 设备版两种说法);
  ④ **动态**: 三个助手函数(`humanize_exception` / `tool_missing_reason` /
     `tool_exit_reason`)的措辞 —— 说得清「现在能不能用」「谁的责任」
     「下一步怎么办」;
  ⑤ **动态**: CPU 项假 stress-ng 退出码非 0 ⇒ **必须有原因**(原始缺陷
     的回归); 顺带把「没跑完 ≠ 不合格」这句话盯住;
  ⑥ **静态**: 报告模板不再印「(未记录原因 —— 属实现缺陷)」。

不碰真硬件: stress-ng / 温度 / 进程全是假的; 数据根指向临时目录
(`AGING_DATA_DIR`), 不写生产库、不写 reports/pdf/。
跑法: .venv/bin/python -m tests.test_failure_reasons
"""
import ast
import asyncio
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ⚠️ **必须先把数据根挪到临时目录** —— CPU 项会写「加压现场记录」旁挂
#   文件, 默认落在生产数据根里(那是产线板上的真数据, 谁也不许碰)。
from tests._tmp import mkdtemp                      # noqa: E402

os.environ["AGING_DATA_DIR"] = mkdtemp(prefix="aging-failreason-")

from server.tests import cpu as cpu_mod             # noqa: E402
from server.tests import _tools as T                # noqa: E402
from server.tests.base import TestContext           # noqa: E402

FAILS = []
PASSES = 0

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS_DIR = os.path.join(REPO, "server", "tests")
RUNNER = os.path.join(REPO, "server", "runner.py")
TEMPLATE = os.path.join(REPO, "server", "templates", "report.html.j2")

#: 11 个测试项模块(以 `server/tests/` 里带 `@register` 的为准;
#: base / registry / _tools / gpu_benchmarks 不产生判定, 不在这儿)。
ITEM_MODULES = [
    "cpu.py", "memory.py", "gpu.py", "emmc.py", "tfcard.py", "usb.py",
    "ethernet.py", "wifi.py", "hdmi.py", "fan.py", "bluetooth.py",
]


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


# ══════════════════════════════════════════════════════════════════
# 【1】路径敏感的 AST 扫描
# ══════════════════════════════════════════════════════════════════
#
# 状态 = (all_passed, status, error已赋值)
# analyze() 返回 (走到函数尾的状态, break 出去的状态, return 出去的状态)。
#
# 为什么不用简单 grep「每个 failed 分支旁边都有 error」: 那样会把
# 「error 在**外层**赋的」也算成缺(假阳性满天飞), 也看不出
# 「分支 A 有原因、分支 B 没有」。路径敏感才是工人真正看到的那条路。
#
# ⚠️ 这个分析是**保守**的: 循环按「跑 0 次 / 跑完 / break 出来」三种
#    可能合并(没法关联 break)。⇒ 它可能**多报**, 不会漏报。

_BAD_STATUS = ("failed", "error")
_ALL_PASSED_NAMES = ("all_passed", "all_ok", "ok")


def _guard(test, st):
    """把一个条件表达式拆成「满足时」/「不满足时」两组状态。"""
    if isinstance(test, ast.Compare) and len(test.ops) == 1 \
            and isinstance(test.ops[0], ast.Eq):
        left, right = test.left, test.comparators[0]
        if isinstance(left, ast.Attribute) and left.attr == "status" \
                and isinstance(right, ast.Constant):
            return ({x for x in st if x[1] == right.value},
                    {x for x in st if x[1] != right.value})
    if isinstance(test, ast.Attribute) and test.attr == "error":
        return ({x for x in st if x[2]}, {x for x in st if not x[2]})
    if isinstance(test, ast.Name) and test.id in _ALL_PASSED_NAMES:
        return ({x for x in st if x[0] is True},
                {x for x in st if x[0] is not True})
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        yes, no = _guard(test.operand, st)
        return no, yes
    if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.And):
        yes_parts, no_parts = [], []
        for value in test.values:
            y, n = _guard(value, st)
            yes_parts.append(y)
            no_parts.append(n)
        both = set(st)
        for y in yes_parts:
            both &= y
        either = set()
        for n in no_parts:
            either |= n
        return both, either
    return set(st), set(st)


def _analyze(body, state):
    """返回 (end, breaks, returned) 三组状态。"""
    end = set(state)
    breaks = set()
    returned = set()
    for node in body:
        if isinstance(node, ast.Break):
            breaks |= end
            end = set()
            continue
        if isinstance(node, ast.Continue):
            continue
        if isinstance(node, ast.Return):
            returned |= end
            end = set()
            continue
        kind = type(node)
        if kind is ast.Assign:
            for tgt in node.targets:
                if isinstance(tgt, ast.Attribute) and tgt.attr == "status":
                    if isinstance(node.value, ast.Constant):
                        end = {(a, node.value.value, e) for (a, _, e) in end}
                elif isinstance(tgt, ast.Attribute) and tgt.attr == "error":
                    # 赋成 None / 空串 ⇒ **等于没写原因**
                    empty = (isinstance(node.value, ast.Constant)
                             and node.value.value in (None, ""))
                    if not empty:
                        end = {(a, s, True) for (a, s, _) in end}
                elif isinstance(tgt, ast.Name) \
                        and tgt.id in _ALL_PASSED_NAMES:
                    if isinstance(node.value, ast.Constant):
                        end = {(node.value.value, s, e) for (_, s, e) in end}
        elif kind is ast.If:
            y_in, n_in = _guard(node.test, end)
            y_end, y_brk, y_ret = _analyze(node.body, y_in)
            if node.orelse:
                n_end, n_brk, n_ret = _analyze(node.orelse, n_in)
            else:
                n_end, n_brk, n_ret = set(n_in), set(), set()
            end = y_end | n_end
            breaks |= y_brk | n_brk
            returned |= y_ret | n_ret
        elif kind in (ast.For, ast.AsyncFor, ast.While):
            b_end, b_brk, b_ret = _analyze(node.body, end)
            end = end | b_end | b_brk        # 跑 0 次 / 跑完 / break 出来
            breaks |= b_ret
            returned |= b_ret
        elif kind is ast.Try:
            b_end, b_brk, b_ret = _analyze(node.body, end)
            h_end, h_brk, h_ret = set(), set(), set()
            for handler in node.handlers:
                he, hb, hr = _analyze(handler.body, end)
                h_end |= he
                h_brk |= hb
                h_ret |= hr
            if node.orelse:
                o_end, o_brk, o_ret = _analyze(node.orelse, b_end)
            else:
                o_end, o_brk, o_ret = b_end, set(), set()
            f_end, f_brk, f_ret = _analyze(node.finalbody, o_end | h_end)
            end = b_end | h_end | o_end | f_end
            breaks |= b_brk | h_brk | o_brk | f_brk
            returned |= b_ret | h_ret | o_ret | f_ret
        elif kind in (ast.With, ast.AsyncWith):
            w_end, w_brk, w_ret = _analyze(node.body, end)
            end = w_end
            breaks |= w_brk
            returned |= w_ret
    return end, breaks, returned


def scan_missing_reason(path):
    """扫一个文件, 返回「判了 failed/error 却没写原因」的路径清单。"""
    tree = ast.parse(open(path, encoding="utf-8").read())
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        end, brk, ret = _analyze(node.body, {(True, "pending", False)})
        for a, s, e in sorted(end | brk | ret, key=lambda x: (str(x[1]), x[2])):
            if s in _BAD_STATUS and not e:
                hits.append((path, node.name, s))
    return hits


section("【1】静态: 判了 failed/error 的路径, 一条都不许漏原因")

# ⚠️ **看护范围要跟着测试项走** —— 上面那份清单是手写的 ⇒ 哪天新增了
#    一个测试项却忘了把它加进来 ⇒ 它就**不受看护**了 ⇒ 那正是这个测试
#    存在的理由被架空的地方。⇒ 这里拿 `@register` 跟清单对账。
registered = sorted(
    fn for fn in os.listdir(TESTS_DIR)
    if fn.endswith(".py")
    and "@register" in open(os.path.join(TESTS_DIR, fn),
                            encoding="utf-8").read()
)
check(registered == sorted(ITEM_MODULES),
      "看护清单跟实际注册了的测试项**对得上**"
      "(新增测试项必须一并纳入看护)",
      f"实际={registered} 清单={sorted(ITEM_MODULES)}")

missing_modules = []
for name in ITEM_MODULES:
    path = os.path.join(TESTS_DIR, name)
    if not os.path.exists(path):
        continue
    hits = scan_missing_reason(path)
    if hits:
        missing_modules.extend(hits)
check(not missing_modules,
      f"11 个测试项模块里, **没有**「判了 failed/error 却不写原因」的路径",
      "\n      ".join(f"{os.path.basename(p)}:{fn} status={s}" for p, fn, s in missing_modules))

# runner.py 的兜底: 它用 TestResult(status=..., error=...) 构造, 不是属性
# 赋值 ⇒ AST 扫不到 ⇒ 单独查一句。
runner_src = open(RUNNER, encoding="utf-8").read()
check('status="error"' in runner_src and "humanize_exception" in runner_src,
      "runner 的通用兜底: 出意外时也把原因翻成人话(humanize_exception)")
check(re.search(r'status="(failed|error)"[^\n]*\n?[^\n]*error=', runner_src)
      is not None,
      "runner 里凡是判 failed/error 的构造, 都带着 error=")
check(re.search(r'status in \("stopped",\s*"skipped"\)\s*and not result\.error:'
                 r'\s*\n\s*result\.error =',
                 runner_src) is not None,
      "runner 兜底: stopped/skipped 但没写原因时, **统一补一句**"
      "(报告的「未测项明细」表要有一句能读的话)")

# 其它模块(不在 11 项里但也判结果的)顺手扫一遍
for extra in ("base.py",):
    path = os.path.join(TESTS_DIR, extra)
    if os.path.exists(path):
        hits = scan_missing_reason(path)
        check(not hits, f"{extra} 里也没有漏原因的路径", hits)


section("【2】静态: 不许再把裸异常 repr 塞给工人看")

# 找 `f"...{e!r}..."` 这种(把异常**用 repr** 印出来 —— 出来的是
# `<Foo object at 0x7f…>` 或者一堆转义, 工人读不懂)。
#
# ⚠️ 用 **AST** 找, 不用正则 —— 正则会把**注释和文档字符串**里那句
#   「以前各处都是 f"异常: {e!r}"」也算成一处(那是**在解释旧毛病**,
#   不是在犯这个毛病)。AST 天然看不见注释和文档字符串。
#
# ⚠️ 只盯**异常名**: `!r` 用在普通值上是合理的(比如配置值要原样印出来),
#   全局禁掉会误伤。⇒ 只查 e / exc / err / ex / error / exception 这些。
_EXC_NAMES = ("e", "exc", "err", "ex", "error", "exception")


def _expr_base(node):
    """取表达式的「根名字」—— `a.b.c` / `f(x)` / `a[0]` 都归到 a。"""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return _expr_base(node.value)
    if isinstance(node, ast.Call):
        return _expr_base(node.func)
    if isinstance(node, ast.Subscript):
        return _expr_base(node.value)
    return ""


repr_hits = []
for name in ITEM_MODULES + ["_tools.py"]:
    path = os.path.join(TESTS_DIR, name)
    if not os.path.exists(path):
        continue
    tree = ast.parse(open(path, encoding="utf-8").read())
    for node in ast.walk(tree):
        if isinstance(node, ast.FormattedValue) and node.conversion == 114:
            base = _expr_base(node.value)
            if base in _EXC_NAMES:
                repr_hits.append(f"{name}:{node.lineno}  {{{base}!r}}")
check(not repr_hits,
      "没有任何一处把异常用 repr({e!r}) 印出来给工人看", repr_hits)

# 也不许把异常对象**整个**赋给 result.error
whole_exc = []
for name in ITEM_MODULES:
    path = os.path.join(TESTS_DIR, name)
    if not os.path.exists(path):
        continue
    tree = ast.parse(open(path, encoding="utf-8").read())
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Attribute) and tgt.attr == "error" \
                        and isinstance(node.value, ast.Name) \
                        and node.value.id in ("e", "exc", "err", "ex"):
                    whole_exc.append(f"{name}:{node.lineno}")
check(not whole_exc,
      "没有任何一处把异常对象整个赋给 result.error(工人看到的是一段代码)",
      whole_exc)


section("【3】静态: 缺工具的补救办法不许写死")

# U 盘版上 `sudo apt install …` 是**死的**(盘上自带 fio / glmark2,
# 其余工具用板子出厂那份)⇒ 补救办法必须走 deps_fix_hint()。
hardcoded = []
for name in ITEM_MODULES + ["_tools.py"]:
    path = os.path.join(TESTS_DIR, name)
    if not os.path.exists(path):
        continue
    src = open(path, encoding="utf-8").read()
    for i, line in enumerate(src.split("\n"), 1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue           # 注释里提一嘴不算
        if "apt install" in line or "apt-get install" in line:
            hardcoded.append(f"{name}:{i}: {stripped[:70]}")
check(not hardcoded,
      "测试项模块里没有写死的 `apt install`(必须走 deps_fix_hint())",
      "\n      ".join(hardcoded))

# 每个「缺工具」的模块都得真的调 deps_fix_hint()
for name in ["cpu.py", "memory.py", "emmc.py", "tfcard.py", "usb.py",
             "ethernet.py", "wifi.py", "gpu.py"]:
    path = os.path.join(TESTS_DIR, name)
    if not os.path.exists(path):
        continue
    src = open(path, encoding="utf-8").read()
    check("deps_fix_hint" in src,
          f"{name} 的缺工具补救走 deps_fix_hint()(U 盘版 / 设备版两种说法)")


section("【4】动态: 三个助手函数的措辞")

# ── humanize_exception ──
msg = T.humanize_exception(FileNotFoundError(2, "No such file"),
                            item="内存项", stage="第 1/3 轮")
check("内存项" in msg and "第 1/3 轮" in msg,
      "humanize_exception 说得出「哪一项」「哪一步」", msg)
check("没测成" in msg,
      "说得出「这一项没测成」", msg)
check("不代表被测设备不合格" in msg,
      "说得出「没测成 ≠ 不合格」(§9.3 的口径)", msg)
check("FileNotFoundError" in msg,
      "原始异常信息**留着**(排障的人要看)", msg)
check("不存在" in msg,
      "常见异常翻成了人话(不用 worker 自己认异常类名)", msg)

msg2 = T.humanize_exception(TimeoutError("timed out"))
check("超时" in msg2, "TimeoutError 翻成「超时」", msg2)
msg3 = T.humanize_exception(OSError(13, "Permission denied"))
check("权限不够" in msg3, "PermissionError / EACCES 翻成「权限不够」", msg3)

# ── tool_missing_reason ──
hint = T.deps_fix_hint()
miss = T.tool_missing_reason("fio", hint,
                              note="PATH 不含 /sbin, 或这台机器上压根没装这个工具")
check("fio" in miss, "tool_missing_reason 点名缺的是哪个工具", miss)
check("没测成" in miss and "不是「板子不合格」" in miss,
      "tool_missing_reason 说清「这一项没测成, 不是板子坏了」", miss)
check(hint in miss, "tool_missing_reason 带着**当前形态**的补救办法", miss)
check("怎么办" in miss, "tool_missing_reason 告诉工人下一步做什么", miss)

# ── tool_exit_reason ──
for rc, word in ((124, "timeout"), (126, "不能执行"), (127, "找不到"),
                  (137, "SIGKILL"), (1, "自己报错")):
    t = T.tool_exit_reason(rc, tool="stress-ng", stage="第 1/2 轮",
                            log_tail="最后一行输出")
    check(word in t, f"退出码 {rc} 翻成了人话", t)
te = T.tool_exit_reason(1, tool="stress-ng", stage="第 1/2 轮",
                         log_tail="stress-ng: unknown option")
check("stress-ng" in te and "第 1/2 轮" in te,
      "tool_exit_reason 说得出「哪个工具」「哪一步」", te)
check("stress-ng: unknown option" in te,
      "tool_exit_reason 附上原始输出的尾巴(排障要看)", te)
check("第 1/2 轮" not in T.rc_meaning(1),
      "rc_meaning 只是退出码的释义, 不带上下文")


section("【5】动态: CPU 项假 stress-ng 退出码非 0 ⇒ 必须有原因")

# 假的外部依赖 —— 一个真进程都不许起(2026-11-03 14:59 实测踩过:
# 回归测试自己起了 8 个 8 线程 stress-ng, 把产线板子压到当场硬复位)。
_saved = {
    "temp": cpu_mod._read_cpu_temp,
    "zones": cpu_mod._read_all_zones,
    "stat": cpu_mod._read_cpu_stat,
    "freq": cpu_mod._read_cpu_freq,
    "find": cpu_mod.find_tool,
    "sudo": cpu_mod.sudo_hint_if_unavailable,
    "spawn": cpu_mod._spawn_proc,
    "run_proc": cpu_mod.run_proc_with_progress,
    "interval": cpu_mod.EVIDENCE_SAMPLE_INTERVAL_SEC,
}
_orig_create_exec = asyncio.create_subprocess_exec
_TRIPWIRE = []


async def _fake_temp():
    return 60.0


async def _fake_zones():
    return {"cpul": 58.0, "cpub": 60.0, "gpu": 45.0}


_stat = {"v": 0}


def _fake_stat():
    _stat["v"] += 400
    return (_stat["v"] * 3 // 4, _stat["v"])


async def _fake_spawn(cmd):
    """假的起进程 —— 只记命令, **什么都不起**。"""
    return object()


class _ProcOut:
    rc = 1
    text = ("stress-ng: stress-ng: unknown option -- '--metrics-brief'\n"
            "stress-ng: failed to initialize stressor")
    killed = False
    reason = ""


async def _fake_run_proc(proc, duration, ctx=None, on_tick=None,
                         hard_limit_sec=None, **kw):
    if on_tick:
        for e in (1.0, 2.0):
            await asyncio.sleep(0.01)
            await on_tick(e)
    return _ProcOut()


async def _guarded_create_exec(*args, **kw):
    """绊线 —— 谁绕开 `_spawn_proc` 直接起 stress-ng, 当场拦下。"""
    flat = [str(a) for a in args] + [str(v) for v in kw.values()]
    if any("stress-ng" in a for a in flat):
        _TRIPWIRE.append(flat)
        raise RuntimeError("❌ 绊线被踩: 绕开 _spawn_proc 直接起了 stress-ng")
    return await _orig_create_exec(*args, **kw)


cpu_mod._read_cpu_temp = _fake_temp
cpu_mod._read_all_zones = _fake_zones
cpu_mod._read_cpu_stat = _fake_stat
cpu_mod._read_cpu_freq = lambda: 1800.0
cpu_mod.find_tool = lambda name: "/usr/bin/" + name
cpu_mod.sudo_hint_if_unavailable = lambda result, name: True
cpu_mod._spawn_proc = _fake_spawn
cpu_mod.run_proc_with_progress = _fake_run_proc
cpu_mod.EVIDENCE_SAMPLE_INTERVAL_SEC = 0.01
asyncio.create_subprocess_exec = _guarded_create_exec

LOGS = []


async def _noop_progress(pct, msg):
    pass


async def _on_log(line):
    LOGS.append(line)


async def _not_cancelled():
    return False


async def _noop_metric(key, value):
    pass


ctx = TestContext(
    run_id=9101,
    config={},
    test_config={"times": 1, "duration_sec": 10, "slice_sec": 5, "threads": 2},
    on_progress=_noop_progress,
    on_log=_on_log,
    is_cancelled=_not_cancelled,
    on_metric=_noop_metric,
)

_loop = asyncio.new_event_loop()
asyncio.set_event_loop(_loop)
try:
    cpu_result = _loop.run_until_complete(cpu_mod.CPUTest().run(ctx))
finally:
    _loop.close()

check(not _TRIPWIRE, "整项跑完, 绊线一次都没被踩(没起真 stress-ng)", _TRIPWIRE)

err = cpu_result.error or ""
check(cpu_result.status == "failed",
      "stress-ng 退出码非 0 ⇒ 这一项判 failed(§6.2 口径没动)",
      cpu_result.status)
check(bool(err.strip()),
      "⚠️ **这一项有原因**(这正是 2026-11-12 修的那个洞)", repr(err))
check("退出码 1" in err,
      "原因里点名了退出码", err)
check("stress-ng" in err,
      "原因里点名了是哪个工具", err)
check("没跑完" in err,
      "原因里说得出「这一轮加压没跑完」", err)
check("板子坏" in err or "不等于板子坏了" in err,
      "原因里说得出「退出码 ≠ 板子坏了」", err)
check("原始输出" in err,
      "原因里附上了原始输出(排障的人要看那几行)", err)
check("unknown option" in err,
      "原始输出真的附上了, 不是空话", err)

# 恢复现场
cpu_mod._read_cpu_temp = _saved["temp"]
cpu_mod._read_all_zones = _saved["zones"]
cpu_mod._read_cpu_stat = _saved["stat"]
cpu_mod._read_cpu_freq = _saved["freq"]
cpu_mod.find_tool = _saved["find"]
cpu_mod.sudo_hint_if_unavailable = _saved["sudo"]
cpu_mod._spawn_proc = _saved["spawn"]
cpu_mod.run_proc_with_progress = _saved["run_proc"]
cpu_mod.EVIDENCE_SAMPLE_INTERVAL_SEC = _saved["interval"]
asyncio.create_subprocess_exec = _orig_create_exec


section("【6】静态: 报告模板不再印「(未记录原因 —— 属实现缺陷)」")

tpl = open(TEMPLATE, encoding="utf-8").read()
check("属实现缺陷" not in tpl,
      "报告模板里那条「属实现缺陷」的兜底印字**去掉了**",
      [l for l in tpl.split("\n") if "属实现缺陷" in l][:3])
check("（未记录原因）" not in tpl,
      "模板里那句光秃秃的「（未记录原因）」也没了(两处都换成了能读的话)",
      tpl.count("（未记录原因）"))

print("\n" + "=" * 60)
print(f"通过 {PASSES} 项, 失败 {len(FAILS)} 项")
for f in FAILS:
    print("  ✗ " + f)
sys.exit(1 if FAILS else 0)
