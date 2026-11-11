"""回归测试: 蓝牙项的**判定口径**与**只读纪律**(§6.4.1)

**为什么判定是反的**(需求原文, 2026-11-03 记下 / 2026-11-04 实现):

    「新增一个测试模块, 蓝牙的测试, 但是这个判定结果要反过来, 因为我们不需要
    蓝牙, 所以**蓝牙不工作就判定 pass**, 这个模块**放到 wifi 测试模块后面**。」

这台设备**不需要蓝牙** ⇒ 「蓝牙坏了 / 压根没有 / 射频关着」都是**正常状态**,
判 `failed` 是冤枉它; 反过来, **蓝牙还在正常工作**才是要查的事(跟产品
「这台设备没有蓝牙」的规格对不上)。

**这个测试守的是**

    ① 「在工作」= 有活动的控制器 **且** 射频没被屏蔽(两个信号都要满足)
    ② 三种「不工作」都判 `passed`, 但**日志/指标要说清是哪一种**
       (压根没硬件 / 硬件在但射频被屏蔽 / 硬件在但没有控制器)
    ③ 蓝牙**在工作** ⇒ `failed`, 报错里要点名「跟本机规格对不上」
    ④ **只读**: 一条命令都不许发(不许 `rfkill unblock` / `bluetoothctl power on`
       / 不许启停 `bluetooth` 服务)—— 这一项的**目的恰恰是确认蓝牙没在工作**,
       你去把它打开再关上, 测出来的东西不作数(还可能把板子带崩, §6.4 的教训)
    ⑤ **不写 `result.error`** 的合格项 —— 那个字段在页面和报告里是**红字错误**,
       合格项往那儿塞说明等于自己给自己报警
    ⑥ 探测不到系统状态 ⇒ 判 `error` **而不是 `passed`**(§9.3)
    ⑦ 位置: **紧跟在 WiFi 后面**, 都在重载之前
    ⑧ 文案: 「蓝牙: 已关闭(符合本机规格)」—— **绝不**写「蓝牙: 通过」

**不碰真硬件、不碰生产库**: 探测用的 sysfs 树全是**假的**(临时目录里现搭),
`/sys/class`、`/dev` 这些路径全部换掉 —— 跑这个测试不会动本机任何蓝牙状态。

跑法: .venv/bin/python -m tests.test_bluetooth_verdict
"""
import ast
import asyncio
import os
import re
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.runner import RECOMMENDED_ORDER                  # noqa: E402
from server.tests import bluetooth as B                      # noqa: E402
from server.tests.base import TestResult                     # noqa: E402
from server.tests.registry import get_all_tests              # noqa: E402

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

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


# ── 假的 ctx ────────────────────────────────────────────────────────

class FakeCtx:
    def __init__(self, test_config=None):
        self.config = {}
        self.test_config = test_config or {}
        self.logs = []
        self.progress = []

    async def on_log(self, line): self.logs.append(line)

    async def on_progress(self, pct, msg): self.progress.append((pct, msg))

    async def on_metric(self, k, v): pass

    async def is_cancelled(self): return False


# ── 假的板子: 搭一棵假的 sysfs 树 ───────────────────────────────────

class FakeBoard:
    """在一棵**假的** sysfs 树里摆出各种蓝牙状态。

    bt_rfkill  None = 压根没有 type=bluetooth 的 rfkill 条目
                否则 = dict(name=..., soft=..., hard=...)
    hci_sysfs  /sys/class/bluetooth 里的控制器名列表
    hci_dev    /dev/hci* 的名字列表
    wifi_rfkill 摆一条 type=wifi 的 rfkill 条目(用来验「按 type 过滤」)
    masked     True = bluetooth.service 是指向 /dev/null 的符号链接
                False = 普通文件
                None = 没有这个覆盖文件
    no_sys_class  True = /sys/class 压根不存在(模拟 sysfs 没挂上)
    """

    def __init__(self, bt_rfkill=None, hci_sysfs=(), hci_dev=(),
                 wifi_rfkill=False, masked=None, no_sys_class=False):
        self.bt_rfkill = bt_rfkill
        self.hci_sysfs = list(hci_sysfs)
        self.hci_dev = list(hci_dev)
        self.wifi_rfkill = wifi_rfkill
        self.masked = masked
        self.no_sys_class = no_sys_class
        self.root = tempfile.mkdtemp(prefix="fakebt-")
        self._build()

    def _build(self):
        r = self.root
        if not self.no_sys_class:
            os.makedirs(os.path.join(r, "class", "rfkill"), exist_ok=True)
            os.makedirs(os.path.join(r, "class", "bluetooth"), exist_ok=True)
        os.makedirs(os.path.join(r, "dev"), exist_ok=True)
        os.makedirs(os.path.join(r, "etc", "systemd", "system"), exist_ok=True)

        if self.bt_rfkill is not None:
            self._mk_rfkill("rfkill0", "bluetooth", self.bt_rfkill)
        if self.wifi_rfkill:
            self._mk_rfkill("rfkill1", "wifi",
                            {"name": "wlan0", "soft": 0, "hard": 0})
        for h in self.hci_sysfs:
            os.makedirs(os.path.join(r, "class", "bluetooth", h), exist_ok=True)
        for h in self.hci_dev:
            with open(os.path.join(r, "dev", h), "w") as f:
                f.write("")
        unit = os.path.join(r, "etc", "systemd", "system", "bluetooth.service")
        if self.masked is True:
            os.symlink("/dev/null", unit)
        elif self.masked is False:
            with open(unit, "w") as f:
                f.write("[Service]\n")

    def _mk_rfkill(self, dirname, typ, vals):
        d = os.path.join(self.root, "class", "rfkill", dirname)
        os.makedirs(d, exist_ok=True)
        for key, val in (("type", typ), ("name", vals.get("name", "")),
                         ("soft", vals.get("soft", 0)), ("hard", vals.get("hard", 0))):
            with open(os.path.join(d, key), "w") as f:
                f.write(str(val))

    # ── 路径 ──
    @property
    def sys_class(self): return os.path.join(self.root, "class")

    @property
    def rfkill(self): return os.path.join(self.root, "class", "rfkill")

    @property
    def bt_class(self): return os.path.join(self.root, "class", "bluetooth")

    @property
    def dev(self): return os.path.join(self.root, "dev")

    @property
    def unit(self):
        return os.path.join(self.root, "etc", "systemd", "system",
                            "bluetooth.service")

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)


class Patch:
    """把模块里的路径常量换成假的, 退出时还原。"""

    def __init__(self, board):
        self.board = board
        self.saved = {}

    def __enter__(self):
        for k, v in (("SYS_CLASS_DIR", self.board.sys_class),
                     ("RFKILL_DIR", self.board.rfkill),
                     ("BLUETOOTH_CLASS_DIR", self.board.bt_class),
                     ("DEV_DIR", self.board.dev),
                     ("BT_SERVICE_UNIT", self.board.unit)):
            self.saved[k] = getattr(B, k)
            setattr(B, k, v)
        return self

    def __exit__(self, *exc):
        for k, v in self.saved.items():
            setattr(B, k, v)
        return False


async def run_board(board, test_config=None):
    """在一棵假板子上跑一遍蓝牙项, 返回 (result, ctx)。"""
    ctx = FakeCtx(test_config or {})
    with Patch(board):
        result = await B.BluetoothTest().run(ctx)
    return result, ctx


def run(board, test_config=None):
    return asyncio.run(run_board(board, test_config))


# ══════════════════════════════════════════════════════════════════════
section("【1】板上压根没有蓝牙硬件 ⇒ passed(这台设备本来就没有)")

board = FakeBoard()
result, ctx = run(board)

check(result.status == "passed", "判 passed", result.status)
check(result.metrics.get("bt_present") is False, "bt_present = False",
      result.metrics.get("bt_present"))
check(result.metrics.get("bt_working") is False, "bt_working = False",
      result.metrics.get("bt_working"))
check(result.metrics.get("bt_rf_state") == "absent", "bt_rf_state = absent",
      result.metrics.get("bt_rf_state"))
check(result.metrics.get("bt_hci_devices") == "(无)", "没有控制器",
      result.metrics.get("bt_hci_devices"))
check(result.metrics.get("bt_verdict") == B.VERDICT_OFF,
      "结论写的是「已关闭(符合本机规格)」", result.metrics.get("bt_verdict"))
check("探不到蓝牙硬件" in (result.metrics.get("bt_detail") or ""),
      "说清了是**哪一种**不工作(压根没硬件)",
      result.metrics.get("bt_detail"))
board.cleanup()


section("【2】硬件在 / 射频没屏蔽 / 没有控制器 ⇒ passed(**实测那块板子的状态**)")

# 这就是真机上的样子: rfkill0 = sunxi-bt (soft=0, hard=0), 没有 hci 设备,
# bluetooth.service 被镜像 mask 掉了。
board = FakeBoard(bt_rfkill={"name": "sunxi-bt", "soft": 0, "hard": 0},
                  masked=True)
result, ctx = run(board)

check(result.status == "passed", "判 passed", result.status)
check(result.metrics.get("bt_present") is True, "bt_present = True",
      result.metrics.get("bt_present"))
check(result.metrics.get("bt_rf_state") == "unblocked", "射频没被屏蔽",
      result.metrics.get("bt_rf_state"))
check(result.metrics.get("bt_rf_name") == "sunxi-bt", "记下了射频的名字",
      result.metrics.get("bt_rf_name"))
check(result.metrics.get("bt_working") is False, "bt_working = False",
      result.metrics.get("bt_working"))
check(result.metrics.get("bt_service_masked") is True,
      "记下了「蓝牙服务被镜像屏蔽」", result.metrics.get("bt_service_masked"))
check("没有活动的控制器" in (result.metrics.get("bt_detail") or ""),
      "说清了是「硬件在但没有控制器」", result.metrics.get("bt_detail"))
check(any("sunxi-bt" in ln for ln in ctx.logs),
      "日志里把 rfkill 条目摆出来了",
      [ln for ln in ctx.logs if "rfkill" in ln])
board.cleanup()


section("【3】射频被屏蔽(soft / hard)⇒ 算「不工作」⇒ passed")

for which in ("soft", "hard"):
    board = FakeBoard(bt_rfkill={"name": "sunxi-bt", "soft": 0, "hard": 0})
    board._mk_rfkill("rfkill0", "bluetooth",
                     {"name": "sunxi-bt",
                      "soft": 1 if which == "soft" else 0,
                      "hard": 1 if which == "hard" else 0})
    result, ctx = run(board)
    check(result.status == "passed", f"{which} 被屏蔽 ⇒ 判 passed",
          result.status)
    check(result.metrics.get("bt_rf_state") == "blocked",
          f"{which} 被屏蔽 ⇒ bt_rf_state = blocked",
          result.metrics.get("bt_rf_state"))
    check("屏蔽" in (result.metrics.get("bt_detail") or ""),
          f"{which} 被屏蔽 ⇒ 日志/指标说清是「被屏蔽」",
          result.metrics.get("bt_detail"))
    board.cleanup()


section("【4】蓝牙**在工作**(有控制器 + 射频没屏蔽)⇒ failed")

board = FakeBoard(bt_rfkill={"name": "sunxi-bt", "soft": 0, "hard": 0},
                  hci_sysfs=["hci0"])
result, ctx = run(board)

check(result.status == "failed", "判 failed", result.status)
check(result.metrics.get("bt_working") is True, "bt_working = True",
      result.metrics.get("bt_working"))
check(result.metrics.get("bt_hci_devices") == "hci0", "记下了控制器名字",
      result.metrics.get("bt_hci_devices"))
check(result.metrics.get("bt_verdict") == B.VERDICT_ON,
      "结论写的是「在工作」", result.metrics.get("bt_verdict"))
check(result.error and "对不上" in result.error,
      "**报错里点名**「跟本机规格对不上」", result.error)
check(any("✗" in ln for ln in ctx.logs), "日志里是 ✗ 失败行",
      [ln for ln in ctx.logs if "✗" in ln])
board.cleanup()


section("【5】被屏蔽 + 有控制器 ⇒ 还是「不工作」(屏蔽优先)")

board = FakeBoard(bt_rfkill={"name": "sunxi-bt", "soft": 1, "hard": 0},
                  hci_sysfs=["hci0"])
result, ctx = run(board)
check(result.status == "passed", "判 passed(射频被屏蔽着就不算在工作)",
      result.status)
check(result.metrics.get("bt_working") is False, "bt_working = False",
      result.metrics.get("bt_working"))
board.cleanup()


section("【6】只有 /dev/hci0(没有 sysfs 里的 hci)⇒ **不算**「在工作」")

# ⚠️ **2026-11-11 改**: 以前 `/dev/hci0` 也算「有活动的控制器」⇒
#   一块**蓝牙本来就该关着**的板子(射频也没屏蔽着, 只是 /dev 里留着
#   udev 建的老节点)会被判「蓝牙在工作」⇒ **failed ⇒ 冤枉好板子**
#   (本机规格就是「不需要蓝牙」⇒ 它本来就合格)。
#   ⇒ 「活动的控制器」只看**内核认得的** `/sys/class/bluetooth/hci*`。
board = FakeBoard(bt_rfkill={"name": "sunxi-bt", "soft": 0, "hard": 0},
                  hci_dev=["hci0"])
result, ctx = run(board)
check(result.status == "passed",
      "/dev 里那个残留节点不算「在工作」⇒ 判 passed(蓝牙确实没在工作)",
      result.status)
check(result.metrics.get("bt_working") is False, "bt_working = False",
      result.metrics.get("bt_working"))
check(result.metrics.get("bt_hci_devices") == "(无)",
      "「蓝牙设备」报的是**活动的控制器** ⇒ 是空的(/dev 里那个残留节点"
      "**不算控制器**, 报上去只会跟「已关闭」自相矛盾)",
      result.metrics.get("bt_hci_devices"))
check("没有活动的控制器" in (result.metrics.get("bt_detail") or ""),
      "详情写明是「没有活动的控制器」那一类",
      result.metrics.get("bt_detail"))
check("hci0" in (result.metrics.get("bt_detail") or ""),
      "详情里把 /dev 里那个残留节点点名说明(信息不藏, 但说清它不算)",
      result.metrics.get("bt_detail"))
board.cleanup()


section("【7】rfkill 里只有 **wifi** 条目 ⇒ 不许当成「蓝牙硬件在」")

board = FakeBoard(wifi_rfkill=True)
result, ctx = run(board)
check(result.metrics.get("bt_present") is False,
      "bt_present = False(wifi 条目不算蓝牙)", result.metrics.get("bt_present"))
check(result.metrics.get("bt_rf_name") == "(无)", "没记成 wlan0",
      result.metrics.get("bt_rf_name"))
check(result.status == "passed", "判 passed", result.status)
board.cleanup()


section("【8】/sys/class 读不到 ⇒ 判 **error**, 不是 passed")

board = FakeBoard(no_sys_class=True)
result, ctx = run(board)
check(result.status == "error", "判 error(这一项没测成)", result.status)
check(result.error and "没测成" in result.error,
      "报错里说清「没测成」", result.error)
check(result.metrics.get("bt_working") is None,
      "没有判成「不工作」—— 我们其实什么都不知道", result.metrics)
board.cleanup()


section("【9】⚠️ **只读**: 一条命令都不许发")

# ① 动态: 把子进程入口换成「一被调用就炸」
_calls = []


async def _boom(*a, **kw):
    _calls.append(a)
    raise AssertionError("蓝牙项不许起任何子进程!")


_orig_exec = asyncio.create_subprocess_exec
asyncio.create_subprocess_exec = _boom
try:
    board = FakeBoard(bt_rfkill={"name": "sunxi-bt", "soft": 0, "hard": 0})
    result, ctx = run(board)
    board.cleanup()
finally:
    asyncio.create_subprocess_exec = _orig_exec

check(not _calls, "全程没起过任何子进程", _calls)
check(result.status == "passed", "只读探测照常出结论", result.status)

# ② 静态: 源码里(去掉注释和 docstring 之后)不许出现这些调用/命令
_src = open(os.path.join(_REPO, "server", "tests", "bluetooth.py"),
            encoding="utf-8").read()
_tree = ast.parse(_src)

# 收集所有 docstring 的节点 id —— docstring 里**提到**这些命令是允许的
# (那是在解释「不许做」), 出现在代码里才要拦。
_docstrings = set()
for _node in ast.walk(_tree):
    if isinstance(_node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                           ast.ClassDef)):
        _body = getattr(_node, "body", None)
        if (_body and isinstance(_body[0], ast.Expr)
                and isinstance(_body[0].value, ast.Constant)
                and isinstance(_body[0].value.value, str)):
            _docstrings.add(id(_body[0].value))

_bad_calls = []
_FORBIDDEN_CALLS = {
    "os.system", "os.popen", "os.execv", "os.execvp", "os.spawnl",
    "subprocess.run", "subprocess.call", "subprocess.check_call",
    "subprocess.check_output", "subprocess.Popen",
    "asyncio.create_subprocess_exec", "asyncio.create_subprocess_shell",
}
for _node in ast.walk(_tree):
    if isinstance(_node, ast.Call):
        _f = _node.func
        _name = ""
        if isinstance(_f, ast.Name):
            _name = _f.id
        elif isinstance(_f, ast.Attribute):
            _parts = []
            while isinstance(_f, ast.Attribute):
                _parts.append(_f.attr)
                _f = _f.value
            if isinstance(_f, ast.Name):
                _parts.append(_f.id)
            _name = ".".join(reversed(_parts))
        if _name in _FORBIDDEN_CALLS:
            _bad_calls.append(_name)

check(not _bad_calls, "源码里没有起进程 / 跑命令的调用", _bad_calls)

# ⚠️ 用**词边界**匹配 —— 否则 "unblocked"(状态名)会被 "unblock" 误伤。
# ⚠️ 只拦**命令的写法** —— 光秃秃的 "rfkill" 是路径 /sys/class/rfkill 的
#    一部分, 那是**该出现的**。
_bad_words = ["unblock", "bluetoothctl", "hciconfig", "btmgmt", "power on",
              "systemctl", "rfkill unblock"]
_bad_strings = []
for _node in ast.walk(_tree):
    if (isinstance(_node, ast.Constant) and isinstance(_node.value, str)
            and id(_node) not in _docstrings):
        for _w in _bad_words:
            if re.search(r"\b" + re.escape(_w) + r"\b", _node.value):
                _bad_strings.append((_w, _node.value[:60]))
check(not _bad_strings,
      "代码(不含注释/docstring)里没有「开/关蓝牙」的命令字样", _bad_strings)


section("【10】合格项**不写 error**(那个字段是红字错误栏)")

board = FakeBoard(bt_rfkill={"name": "sunxi-bt", "soft": 0, "hard": 0})
result, ctx = run(board)
check(result.status == "passed", "判 passed", result.status)
check(not result.error, "**result.error 是空的**", result.error)
check(any("已关闭(符合本机规格)" in ln for ln in ctx.logs),
      "日志里写的是「蓝牙: 已关闭(符合本机规格)」",
      [ln for ln in ctx.logs if "蓝牙" in ln])
check("蓝牙: 通过" not in "\n".join(ctx.logs),
      "**绝不**写「蓝牙: 通过」(那会让人以为蓝牙是好的)")
board.cleanup()


section("【11】位置: **紧跟在 WiFi 后面**, 都在重载之前")

_i_wifi = list(RECOMMENDED_ORDER).index("wifi")
_i_bt = list(RECOMMENDED_ORDER).index("bluetooth")
_i_gpu = list(RECOMMENDED_ORDER).index("gpu")
check(_i_bt == _i_wifi + 1, "蓝牙紧跟在 WiFi 后面", list(RECOMMENDED_ORDER))
check(_i_bt < _i_gpu, "蓝牙排在 GPU 之前", list(RECOMMENDED_ORDER))

_reg = [t["name"] for t in get_all_tests()]
check(_reg.index("bluetooth") == _reg.index("wifi") + 1,
      "注册表清单里蓝牙也紧跟 WiFi", _reg)

check(B.BluetoothTest.display_name == "蓝牙 (应关闭)",
      "项名带「(应关闭)」—— 别让人以为通过=蓝牙是好的",
      B.BluetoothTest.display_name)


section("【12】默认勾上 + 指标齐全")

_cfgp = os.path.join(_REPO, "config.yaml")
with open(_cfgp, encoding="utf-8") as f:
    _cfg = f.read()
check("bluetooth:" in _cfg and "enabled: true" in _cfg,
      "config.yaml 里有 defaults.bluetooth.enabled")

board = FakeBoard(bt_rfkill={"name": "sunxi-bt", "soft": 0, "hard": 0})
result, ctx = run(board)
for _k in ("bt_present", "bt_rf_state", "bt_working", "bt_hci_devices",
           "bt_rf_name", "bt_service_masked", "bt_verdict", "bt_detail"):
    check(_k in result.metrics, f"指标 {_k} 在", sorted(result.metrics))
board.cleanup()


print("\n" + ("全部通过" if not FAILS else f"有失败项 ({len(FAILS)})"))
for f in FAILS:
    print("  ✗ " + f)
sys.exit(0 if not FAILS else 1)
