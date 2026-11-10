"""回归测试: 缺工具时的**补救办法**必须**按形态分开说**(2026-11-10 修的真缺陷)。

**这个缺陷是什么**

    以前 cpu / 内存 / 网口 / WiFi 四个测试项 + 两个诊断脚本都把
    「sudo bash scripts/install_deps.sh」**写死**在报错里 ⇒ 但
    **U 盘版的盘上根本没有这个脚本**(2026-11-10 清理产线盘时, 它被归到
    「打包/部署类」挪走了)⇒ 工人/排障的人照着做 ⇒ 「No such file or
    directory」⇒ **补救办法是死的** —— 越是在出事的时候越没用。

**两条都不能退**

    · **U 盘版不许再说那条命令**: ① 盘上根本没有; ② 产线网络取不到公网,
      就算有也装不上; ③ 装到板子上拔盘就走, 白装 ⇒ 该说的是
      「这版出厂镜像没带齐, 请改用设备版(deb)安装」。
    · **设备版(deb)必须保留那条命令**: deb 包里**确实带着**
      `scripts/install_deps.sh`(build_deb.sh 的白名单里有它)⇒ 命令有效。

**这个测试不碰真硬件**: 只 import 四个模块(不跑任何一条测试项的加压),
诊断脚本是**复制到临时目录**跑的(还把「工具在不在」那一处改成恒假,
好让缺工具的分支**一定**走到 —— 那正是要验的地方)。

跑法: ./.venv/bin/python tests/test_deps_fix_hint.py
"""
import asyncio
import os
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.tests import _tools                     # noqa: E402
from server.tests import cpu as cpu_mod             # noqa: E402
from server.tests import ethernet as eth_mod        # noqa: E402
from server.tests import memory as mem_mod          # noqa: E402
from server.tests import wifi as wifi_mod           # noqa: E402
from server.tests.base import TestContext           # noqa: E402
from tests._tmp import mkdtemp                      # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

FAILS = []
PASSES = 0

# 那条命令的字面量 —— 本测试到处拿它当**反面教材**
CMD = "sudo bash scripts/install_deps.sh"
# 脚本名本身(不带 sudo bash 前缀)—— 白名单扫描用
SCRIPT_NAME = "install_deps.sh"


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


def make_ctx(test_config):
    async def on_progress(pct, msg):
        pass

    async def on_log(line):
        pass

    async def is_cancelled():
        return False

    async def on_metric(key, value):
        pass

    return TestContext(run_id=1, config={}, test_config=test_config,
                       on_progress=on_progress, on_log=on_log,
                       is_cancelled=is_cancelled, on_metric=on_metric)


class usb_mode:
    """上下文管理器: 切「U 盘版 / 设备版」。

    ⚠️ **2026-11-10**: `usb_mode()` 现在认**两个**信号 ——
    环境变量 `AGING_TOOL_DIR` **或** 代码根目录的 `AGING_USB_MODE` 标记
    ⇒ 这个管理器**两个都管**:
      · `tool_dir=True`  ⇒ 设上环境变量(模拟「盘上带着 tools/」);
      · `marker=True`   ⇒ 把代码根换成一个临时目录, 里面**放**标记文件。
    ⇒ 这样才测得到「**标记在、工具目录不在**」那种配置 ——
    那正是两个判据曾经打架、Python 会误判成设备版的地方。

    ⚠️ `_code_root()` **每次都现算**(不缓存)⇒ 换掉就立刻生效。
    """

    def __init__(self, on, tool_dir=True, marker=None):
        self.on = on
        self.tool_dir = tool_dir
        # marker=None ⇒ **跟着 on 走**(设备版就不放标记; U 盘版就放)
        self.marker = on if marker is None else marker
        self.old_env = None
        self.old_root = None
        self.tmp = None

    def __enter__(self):
        self.old_env = os.environ.get(_tools.TOOL_DIR_ENV)
        self.old_root = _tools._code_root
        if self.on and self.tool_dir:
            os.environ[_tools.TOOL_DIR_ENV] = "/tmp/aging-fake-tool-dir"
        else:
            os.environ.pop(_tools.TOOL_DIR_ENV, None)
        # 代码根 → 一个临时目录(标记文件放不放由 marker 决定)
        self.tmp = mkdtemp(prefix="aging-usbmarker-")
        if self.marker:
            with open(os.path.join(self.tmp, "AGING_USB_MODE"), "w") as f:
                f.write("1\n")
        _tools._code_root = lambda: Path(self.tmp)
        return self

    def __exit__(self, *a):
        _tools._code_root = self.old_root
        if self.old_env is None:
            os.environ.pop(_tools.TOOL_DIR_ENV, None)
        else:
            os.environ[_tools.TOOL_DIR_ENV] = self.old_env
        return False


# ══════════════════════════════════════════════════════════════════════
section("【1】`deps_fix_hint()` 两种形态说**两种话**")

with usb_mode(False):
    dev_hint = _tools.deps_fix_hint()
check(CMD in dev_hint,
      "设备版(deb): 补救办法**保留那条命令**(deb 包里确实带着那个脚本)",
      dev_hint)

with usb_mode(True):
    usb_hint = _tools.deps_fix_hint()
check(SCRIPT_NAME not in usb_hint,
      "U 盘版: 补救办法**不许**再提 install_deps.sh(盘上根本没有它)",
      usb_hint)
check("设备版" in usb_hint,
      "U 盘版: 补救办法指向「设备版(deb)」", usb_hint)
check(dev_hint != usb_hint,
      "两种形态说的**不是同一句话**", (dev_hint, usb_hint))

# ⚠️⚠️ **2026-11-10 加: 「标记在、工具目录不在」也得是 U 盘版**
#   —— `start.sh:37` 那行是**有条件**导出的(`[ -d tools/bin ] && export …`)
#   ⇒ 盘上有标记、但 `tools/bin` 不在时, **环境变量不导出**
#   ⇒ 以前 Python 只看环境变量 ⇒ 会误判成**设备版** ⇒ 又把
#   「sudo bash scripts/install_deps.sh」端给工人 ⇒ **而盘上根本没有**
#   ⇒ 刚修掉的那个 P0 在另一种配置下**原样复活**。
with usb_mode(True, tool_dir=False):
    marker_only_hint = _tools.deps_fix_hint()
    marker_only_mode = _tools.usb_mode()
check(marker_only_mode,
      "只有标记文件、没有 AGING_TOOL_DIR ⇒ **仍然算 U 盘版**",
      marker_only_mode)
check(SCRIPT_NAME not in marker_only_hint,
      "只有标记文件时, 补救办法**也是 U 盘版那句**(不是那条死命令)",
      marker_only_hint)
check(marker_only_hint == usb_hint,
      "两种 U 盘版配置(带不带环境变量)说的**是同一句话**",
      (usb_hint, marker_only_hint))

# ⚠️ **反过来那一半也要测**: 只有环境变量、没有标记文件 ⇒ 也算 U 盘版
#   (判据是**取或** ⇒ 两半都得站得住 ⇒ 拆掉任何一半都该被抓住)
with usb_mode(True, marker=False):
    env_only_hint = _tools.deps_fix_hint()
    env_only_mode = _tools.usb_mode()
check(env_only_mode,
      "只有 AGING_TOOL_DIR、没有标记文件 ⇒ **仍然算 U 盘版**",
      env_only_mode)
check(SCRIPT_NAME not in env_only_hint,
      "只有环境变量时, 补救办法**也是 U 盘版那句**", env_only_hint)

# ── 三处拷贝**逐字一致**(2026-11-10: 同一句曾经有四份拷贝/三种措辞) ──
def _shell_usb_text(rel):
    """从 shell 脚本里抠出 U 盘版那句话(它是两行 echo, 拼起来才是完整一句)。"""
    with open(os.path.join(REPO, rel), encoding="utf-8") as f:
        lines = f.read().splitlines()
    out = []
    started = False
    for ln in lines:
        m = re.search(r'echo\s+"(.*)"\s*$', ln)
        if not m:
            continue
        txt = m.group(1)
        if not started:
            idx = txt.find("U 盘版:")
            if idx < 0:
                continue
            out.append(txt[idx:].strip())
            started = True
        else:
            out.append(txt.strip())
        if "设备版(deb)安装" in out[-1]:
            break
    return " ".join(out)


for rel in ("scripts/check_deps.sh", "scripts/check_eth.sh"):
    sh_text = _shell_usb_text(rel)
    check(sh_text == usb_hint,
          "%s: U 盘版那句话跟 Python 的**逐字一致**(以前是两份拷贝/三种措辞 ⇒ "
          "改一处忘一处, 页面和报错说的不是同一句话)" % rel,
          (sh_text, usb_hint))

# ══════════════════════════════════════════════════════════════════════
section("【2】四个测试项**不再写死**那条命令(静态)")

FOUR = [
    ("cpu", cpu_mod, os.path.join(REPO, "server", "tests", "cpu.py")),
    ("memory", mem_mod, os.path.join(REPO, "server", "tests", "memory.py")),
    ("ethernet", eth_mod, os.path.join(REPO, "server", "tests", "ethernet.py")),
    ("wifi", wifi_mod, os.path.join(REPO, "server", "tests", "wifi.py")),
]
for name, mod, path in FOUR:
    with open(path, encoding="utf-8") as f:
        src = f.read()
    check(SCRIPT_NAME not in src,
          "%s.py: 源码里**没有** install_deps.sh 的字面量(写死 = 两种形态"
          "共用一条命令 = 有一半是死的)" % name)
    check("deps_fix_hint" in src,
          "%s.py: 走的是 `deps_fix_hint()`(补救办法按形态走)" % name)

# ══════════════════════════════════════════════════════════════════════
section("【3】测试项**真报出来的错**里, 补救办法跟着形态走")


def _missing_tool_error(mod, cls, test_config):
    """把该模块的「找工具」打桩成**一个都找不到** ⇒ 逼它走缺工具的报错分支。

    ⚠️ **2026-11-10**: 原来这里是
    `mod.__dict__[[k for k in mod.__dict__ if k.endswith("Test")][0]]`
    —— 靠**字典插入顺序**挑一个以 "Test" 结尾的名字 ⇒ 模块里再多一个
    这样的名字(哪怕是 import 进来的)就会挑错 ⇒ 改成**点名**。
    """
    old = mod.find_tool
    mod.find_tool = lambda name: None
    try:
        ctx = make_ctx(test_config)
        res = asyncio.run(cls().run(ctx))
        return getattr(res, "error", "") or ""
    finally:
        mod.find_tool = old


for name, mod, cls, tcfg in (
        ("memory", mem_mod, mem_mod.MemoryTest, {"times": 1, "timeout_sec": 10}),
        ("ethernet", eth_mod, eth_mod.EthTest, {"times": 1}),
        ("wifi", wifi_mod, wifi_mod.WiFiTest, {"times": 1, "iface": "wlan0"})):
    with usb_mode(False):
        dev_err = _missing_tool_error(mod, cls, tcfg)
    with usb_mode(True):
        usb_err = _missing_tool_error(mod, cls, tcfg)
    check(CMD in dev_err,
          "%s: 设备版的报错里**有**那条命令" % name, dev_err)
    check(SCRIPT_NAME not in usb_err,
          "%s: U 盘版的报错里**没有** install_deps.sh" % name, usb_err)
    check("设备版" in usb_err,
          "%s: U 盘版的报错指向「设备版」" % name, usb_err)

# ══════════════════════════════════════════════════════════════════════
section("【4】两个诊断脚本(check_deps.sh / check_eth.sh)按形态分开说")

# ⚠️ **只复制到临时目录里跑**, 而且把「工具在不在」那一处改成**恒假**
#    ⇒ 缺工具的分支**一定**走到(那正是要验的地方)⇒
#    ① 不会在这台板子上误判「工具齐了」而走不到那条分支;
#    ② 仓库里那个脚本一个字都不动。
_SCRIPT_PATCHES = {
    "check_deps.sh": (
        'for cmd in "${!CRITICAL[@]}"; do\n'
        '    if command -v "$cmd" > /dev/null 2>&1; then',
        'for cmd in "${!CRITICAL[@]}"; do\n    if false; then',
    ),
    "check_eth.sh": (
        'if ! command -v ethtool > /dev/null 2>&1; then',
        'if true; then',
    ),
}


def run_script_patched(name, usb, extra_args=(), env=False):
    """把脚本复制到临时目录(打了补丁)跑一遍, 返回它的输出。

    ⚠️ **2026-11-10**: `env=True` ⇒ **不放标记文件**, 改用环境变量
       `AGING_TOOL_DIR` 表明「这是 U 盘版」⇒ 测的是「**只有环境变量**」
       那一半判据 —— 跟 Python 侧 `usb_mode()` 的「取或」对齐。
    """
    src_path = os.path.join(REPO, "scripts", name)
    with open(src_path, encoding="utf-8") as f:
        txt = f.read()
    old, new = _SCRIPT_PATCHES[name]
    if txt.count(old) != 1:
        raise AssertionError("锚点不唯一/失效: %s (%d 次)" % (name, txt.count(old)))
    txt = txt.replace(old, new)

    d = mkdtemp(prefix="aging-depshint-")
    os.makedirs(os.path.join(d, "scripts"))
    with open(os.path.join(d, "scripts", name), "w", encoding="utf-8") as f:
        f.write(txt)
    if usb:
        with open(os.path.join(d, "AGING_USB_MODE"), "w") as f:
            f.write("1\n")
    import subprocess
    run_env = None
    if env:
        run_env = dict(os.environ)
        run_env["AGING_TOOL_DIR"] = os.path.join(d, "tools")
    p = subprocess.run(["/bin/bash", os.path.join(d, "scripts", name)]
                       + list(extra_args),
                       capture_output=True, text=True, timeout=120,
                       env=run_env)
    return (p.stdout or "") + (p.stderr or "")


for name, extra in (("check_deps.sh", ("--packages-only",)),
                    ("check_eth.sh", ())):
    usb_out = run_script_patched(name, True, extra)
    dev_out = run_script_patched(name, False, extra)
    check(SCRIPT_NAME not in usb_out,
          "%s: U 盘版**不提** install_deps.sh" % name, usb_out[-400:])
    check("设备版" in usb_out,
          "%s: U 盘版指向「设备版(deb)」" % name, usb_out[-400:])
    check(CMD in dev_out,
          "%s: 设备版**保留**那条命令" % name, dev_out[-400:])
    # ⚠️ **2026-11-10**: 判据统一 ⇒ shell 侧也是「取或」——
    #   **只有环境变量、没有标记文件** ⇒ 也要算 U 盘版(跟 Python 侧一致)
    env_out = run_script_patched(name, False, extra, env=True)
    check(SCRIPT_NAME not in env_out,
          "%s: 只有环境变量、没有标记 ⇒ 也算 U 盘版(**不提** install_deps.sh)"
          % name, env_out[-400:])
    check("设备版" in env_out,
          "%s: 只有环境变量、没有标记 ⇒ 指向「设备版(deb)」" % name,
          env_out[-400:])

# ══════════════════════════════════════════════════════════════════════
section("【5】仓库里**只有**该出现的地方出现那条命令(白名单)")

# ⚠️ 白名单里的每一处都是**故意**的:
#   · `server/main.py`         —— /api/config 的 fix_command(**早就**按 usb_mode 分开);
#   · `server/tests/_tools.py` —— `deps_fix_hint()`(设备版那一句);
#   · `scripts/check_deps.sh`  —— 设备版分支;
#   · `scripts/check_eth.sh`   —— 设备版分支;
#   · `scripts/install_deps.sh`—— 脚本自己;
#   · `scripts/build_deb.sh`   —— deb 白名单(deb **确实带着**它);
#   · `scripts/prepare_for_firmware.sh` —— 出货镜像要带的脚本清单;
#   · `scripts/build_usb_image.sh` —— ⚠️ **排除清单**(2026-11-10 加):
#     U 盘镜像**故意不带**它 ⇒ 那里的字面量是「不带上盘」, 不是「让人去跑」。
ALLOWED = {
    "server/main.py",
    "server/tests/_tools.py",
    "scripts/check_deps.sh",
    "scripts/check_eth.sh",
    "scripts/install_deps.sh",
    "scripts/build_deb.sh",
    "scripts/prepare_for_firmware.sh",
    "scripts/build_usb_image.sh",
}
hits = []
for sub in ("server", "static", "scripts"):
    root = os.path.join(REPO, sub)
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in ("__pycache__", ".git")]
        for fn in files:
            p = os.path.join(dirpath, fn)
            rel = os.path.relpath(p, REPO)
            if rel in ALLOWED:
                continue
            try:
                with open(p, encoding="utf-8") as f:
                    if SCRIPT_NAME in f.read():
                        hits.append(rel)
            except (UnicodeDecodeError, OSError):
                pass
check(not hits,
      "白名单之外**没有**任何文件再提 install_deps.sh"
      "(新增一处 = 又多一个形态不对就变死的补救办法)",
      hits)

# 白名单里的那几处, **确实**都是「设备版分支」或「脚本自己」——
# 逐个查: 文件里出现这条命令时, 上下文里必须有 usb_mode/USB_MODE 的判断。
for rel in ("server/main.py", "scripts/check_deps.sh", "scripts/check_eth.sh"):
    with open(os.path.join(REPO, rel), encoding="utf-8") as f:
        txt = f.read()
    if rel == "server/main.py":
        ok = '"fix_command": "" if usb_mode else "sudo bash scripts/install_deps.sh"' in txt
    else:
        ok = "USB_MODE" in txt
    check(ok, "%s: 那条命令**确实**被形态判断罩着(不是无条件输出)" % rel)

# ── 「U 盘版那句话」在 Python 侧只有一份 ──────────────────────────────
#   2026-11-10 之前 server/main.py 里另抄了一份(还多个句号)⇒ 两份拷贝
#   各改各的 ⇒ 页面上显示的和报错里说的会不一样。
#   现状: Python 侧那句话在 server/tests/_tools.py 一处, main.py 调函数。
USB_SENTENCE = "U 盘版: 盘上自带 fio"
py_hits = []
for sub in ("server", "scripts"):
    root = os.path.join(REPO, sub)
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in ("__pycache__", ".git")]
        for fn in files:
            if not fn.endswith(".py"):
                continue
            p = os.path.join(dirpath, fn)
            rel = os.path.relpath(p, REPO)
            try:
                with open(p, encoding="utf-8") as f:
                    if USB_SENTENCE in f.read():
                        py_hits.append(rel)
            except (UnicodeDecodeError, OSError):
                pass
check(py_hits == ["server/tests/_tools.py"],
      "Python 侧「U 盘版那句话」只有一份(server/tests/_tools.py)",
      py_hits)

# ⚠️ `scripts/build_usb_image.sh` 里那处 install_deps.sh 是**排除清单**
#   (U 盘镜像**故意不带**它)⇒ 不是「让人去跑」的建议 ⇒ 查它在不在排除清单里。
with open(os.path.join(REPO, "scripts", "build_usb_image.sh"), encoding="utf-8") as f:
    _img_src = f.read()
_img_excl = ""
_i = _img_src.find('copy_tree_except "$PROJECT_ROOT/scripts"')
if _i >= 0:
    _j = _img_src.index("\n", _i)
    while _img_src[_j - 1] == "\\":
        _j = _img_src.index("\n", _j + 1)
    _img_excl = _img_src[_i:_j]
check(SCRIPT_NAME in _img_excl,
      "build_usb_image.sh: install_deps.sh 在**排除清单**里(U 盘镜像不带它 ⇒ "
      "盘上那句补救办法不会变成「No such file」)", _img_excl[-120:])
with open(os.path.join(REPO, "server", "main.py"), encoding="utf-8") as f:
    _main_src = f.read()
check("deps_fix_hint()" in _main_src,
      "server/main.py: fix_hint 走的是 deps_fix_hint()")
check('"fix_hint": deps_fix_hint() if usb_mode else ""' in _main_src,
      "server/main.py: fix_hint 的取值是 deps_fix_hint()")


print("\n" + ("全部通过" if not FAILS else f"有失败项 ({len(FAILS)})"))
for f in FAILS:
    print("  ✗ " + f)
sys.exit(0 if not FAILS else 1)
