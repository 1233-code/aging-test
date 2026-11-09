"""build_usb_image.sh 的两道闸门 —— 行为测试(沙盒跑, 不执行镜像里的任何东西)

背景(2026-11-09): 打包脚本最后那道「start.sh 不许调 venv 里的 console script」
的闸门写成了

    if grep -n '...' "$OUT_DIR/start.sh" | head -5; then die ...; fi

⇒ 判的是**管道最后一个命令(head)的退出码**, 它**永远成功** ⇒
**没匹配也会进 then 分支** ⇒ 这道闸门**恒为真** ⇒
**每次打包都在最后一步 die 掉** —— 而镜像是好的、start.sh 是干净的 ⇒
**新镜像打不出来** ⇒ 产线一直用旧盘(满核 8 线程)⇒ **板子成批硬复位**。

本文件把那两道闸门**从脚本里原样抠出来**, 在沙盒里跑, 正反对照:
  ① 出厂断言 —— 这镜像**不许**能压到**超过核数**(8 核以上)
  ② console script 闸门 —— start.sh 不许直接调 venv 里的 console script

⚠️ **2026-11-09 晚: ① 的口径改了(用户授权)** —— 原来是「不许能压到 7 核
   以上」(那时上限是 6 核, 满核 8 线程 = 过应力 ⇒ 板子成批硬复位)。现在
   加压上限放回 **8 核 = 满核**(用户要先自己在页面上找平衡点)⇒ 断言改成
   「**不许超过核数**」(配置写坏了才会触发), 外加一条**满核警告**。

⚠️ **2026-11-09 晚(更晚): 出厂默认改成 4 核, 上限仍是 8**(用户:
   「确定了, 默认四核, 但是像现在这样支持用户自己选择核数, 四核比较保险」)
   ⇒ **「满核警告」必须改看「用户拧得到几核」**(min(页面旋钮 max, 代码硬
   上限)), **不能继续看出厂默认** —— 默认变成 4 之后, 照旧看默认的话那条
   警告就**永远不触发**了, 而旋钮照样能拧到 8 ⇒ 产线收不到「别往上调」。

⚠️ **只读文本、只写文本**: 沙盒里跑的是抠出来的 shell 片段, 碰不到镜像里的
   任何二进制(跟 §⑩ 自检那两道「真跑 fio/glmark2」的检查无关)。

跑法: ./.venv/bin/python tests/test_usb_image_cpu_gate.py
"""
import os
import re
import shlex
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, "scripts", "build_usb_image.sh")

FAILS = []
PASSES = 0

# start.sh 干净的样子: 全是 `python3 -m xxx`, 一个 console script 都不调。
CLEAN_START_SH = '''#!/bin/bash
VENV_DIR="$SCRIPT_DIR/venv"
    "$VENV_DIR"/bin/python3 -m pip install -q -r requirements.txt
setsid nohup "$VENV_DIR"/bin/python3 -m uvicorn server.main:app \\
    --host 0.0.0.0 --port 8000 &
'''


def check(cond, msg, extra=""):
    global PASSES
    if cond:
        PASSES += 1
        print("    ✓ " + msg)
    else:
        FAILS.append(msg)
        print("    ✗ " + msg + (("  —— " + extra) if extra else ""))


def read_script():
    with open(SCRIPT, "r", encoding="utf-8") as f:
        return f.read()


def slice_between(txt, start, end):
    i = txt.index(start)
    j = txt.index(end, i) + len(end)
    return txt[i:j]


def block_factory(txt):
    """把两道闸门从脚本里原样抠出来。"""
    # ① 出厂断言 + 版本戳
    a = slice_between(txt, "# ── 版本戳",
                      'log "   写了「版本.txt」(产线一眼能认出手上这根盘是哪一版)"')
    # ② console script 闸门
    i = txt.index("# 应用**不许**调 venv 里的 console script")
    j = txt.index("\nfi", i) + len("\nfi")
    b = txt[i:j]
    return a, b


def run_block(block, out_dir):
    """在沙盒里跑抠出来的片段: die → 退出码 9; 其余 → 0。

    ⚠️ 环境变量要跟真脚本一致(ARCH/PYV 是脚本开头定义的) —— 少定义一个,
       `set -u` 就会在**版本戳那段**炸掉(unbound variable)。
    """
    harness = (
        "set -eu\n"
        "OUT_DIR=%s\n"
        "PROJECT_ROOT=%s\n"
        "ARCH=\"$(uname -m)\"\n"
        "PYV=\"$(python3 -c 'import sys; print(\"%%d.%%d\" %% sys.version_info[:2])' 2>/dev/null || echo '?')\"\n"
        "log()  { printf 'LOG: %%s\\n' \"$*\"; }\n"
        "warn() { printf 'WARN: %%s\\n' \"$*\"; }\n"
        "die()  { printf 'DIE: %%s\\n' \"$*\"; exit 9; }\n"
        "%s\n"
    ) % (shlex.quote(out_dir), shlex.quote(REPO), block)
    p = subprocess.run(["bash", "-c", harness], capture_output=True, text=True)
    return p.returncode, p.stdout + p.stderr


def make_fixture(root, threads="8", cap="8", knob="8", start_sh=CLEAN_START_SH,
                dead_guard=True):
    """造一个假的镜像目录(config.yaml + server/tests/cpu.py + static/index.html + start.sh)。

    ⚠️ **`knob` = 页面上那个「线程数」输入框的 max** —— 打包脚本要从镜像里的
    index.html 读它, 才算得出「用户拧得到几核」(满核警告看的是这个, 不是
    出厂默认)。所以不造这个文件的话, 脚本读不到 ⇒ 那条警告就测不到。
    """
    os.makedirs(os.path.join(root, "server", "tests"), exist_ok=True)
    os.makedirs(os.path.join(root, "static"), exist_ok=True)
    cfg = "  cpu:\n"
    if threads is not None:
        # ⚠️ 注释里也带数字 —— 抠代码必须先剥注释再取数, 否则会取成 "666"
        cfg += ("    threads: %s              # %s = 默认 %s 核(上限 8, 旋钮可自选); "
                "0 = 自动\n" % (threads, threads, threads))
    with open(os.path.join(root, "config.yaml"), "w", encoding="utf-8") as f:
        f.write(cfg)
    cpu = ""
    if cap:
        cpu += "MAX_STRESS_THREADS = %s\n" % cap
    if dead_guard:
        cpu += "def cpu_evidence_cut_short(run_id: int) -> bool:\n    return False\n"
    with open(os.path.join(root, "server", "tests", "cpu.py"), "w", encoding="utf-8") as f:
        f.write(cpu)
    with open(os.path.join(root, "static", "index.html"), "w", encoding="utf-8") as f:
        f.write('<input type="number" v-model.number="tests[t.name].threads" '
                'min="0" max="%s" class="num-input" title="默认 4 核(上限 8)">\n' % knob)
    with open(os.path.join(root, "start.sh"), "w", encoding="utf-8") as f:
        f.write(start_sh)
    return root


def main():
    print("== build_usb_image.sh 两道闸门 ==")
    txt = read_script()
    block_a, block_b = block_factory(txt)

    # ── 静态: 判据不许再拿「管道最后一个命令」的退出码当条件 ──────────
    #    ⚠️ **只看真代码行** —— 脚本里那段「别这么写」的注释本身就写着
    #    `if grep ... | head -5; then` ⇒ 连注释一起扫就是自己告自己。
    code_lines = "\n".join(
        ln for ln in txt.splitlines() if not ln.lstrip().startswith("#"))
    bad_pat = re.findall(r"if\s+[^\n]*\|\s*head[^\n]*;\s*then", code_lines)
    check(not bad_pat,
          "脚本里没有「if … | head …; then」这种判据(管道退出码恒为 0)",
          "找到: %r" % (bad_pat,))

    # ── ① 出厂断言 ──────────────────────────────────────────────────
    with tempfile.TemporaryDirectory() as td:
        d = make_fixture(os.path.join(td, "a"))
        rc, out = run_block(block_a, d)
        check(rc != 9, "现状(threads 8 + 上限 8)不被拦下", out)
        check("出厂默认 8 核" in out, "出厂默认核数是 8", out)
        check("旋钮上限 8 核" in out, "旋钮能拧到的上限是 8", out)
        stamp = os.path.join(d, "版本.txt")
        check(os.path.exists(stamp), "版本.txt 生成了")
        if os.path.exists(stamp):
            with open(stamp, encoding="utf-8") as f:
                st = f.read()
            check("threads: 8" in st, "版本戳里写了自查命令")
            check("8 核" in st, "版本戳里写了核数")

    with tempfile.TemporaryDirectory() as td:
        d = make_fixture(os.path.join(td, "b"), threads="0", cap=None)
        rc, out = run_block(block_a, d)
        check(rc == 9, "危险的旧状态(threads 0 + 没有硬上限)被拦下", out)
        check("比核数还多" in out, "拦下时说明了为什么(比核数还多)", out)

    with tempfile.TemporaryDirectory() as td:
        d = make_fixture(os.path.join(td, "c"), threads="8", cap="6")
        rc, out = run_block(block_a, d)
        check(rc != 9, "threads 8 但有 6 核硬上限 ⇒ 出厂默认 6 ⇒ 不拦", out)
        check("出厂默认 6 核" in out, "出厂默认取的是上限(6)不是配置(8)", out)
        check("旋钮上限 6 核" in out, "旋钮也到不了 8(硬上限 6 卡着)", out)

    with tempfile.TemporaryDirectory() as td:
        d = make_fixture(os.path.join(td, "d"), threads="6", cap=None)
        rc, out = run_block(block_a, d)
        check(rc != 9, "threads 6 但没有硬上限 ⇒ 不拦(只 warn)", out)
        check("WARN" in out and "MAX_STRESS_THREADS" in out,
              "没有硬上限时给出警告(旋钮一改就能到 8 核)", out)

    # ── ①bis 满核那一档: **能打出来, 但必须警告**(2026-11-09 晚)──────
    with tempfile.TemporaryDirectory() as td:
        d = make_fixture(os.path.join(td, "d2"), threads="8", cap="8")
        rc, out = run_block(block_a, d)
        check(rc != 9, "满核 8 核的镜像**能**打出来(用户要在页面上自己找平衡点)", out)
        check("WARN" in out and "满核" in out,
              "但会警告「满核 = 过应力那一档, 开跑前先把线程数调低」", out)

    # ── ①ter 比核数还多 ⇒ 配置写坏了 ⇒ 拦下 ──────────────────────────
    with tempfile.TemporaryDirectory() as td:
        d = make_fixture(os.path.join(td, "d3"), threads="12", cap="12")
        rc, out = run_block(block_a, d)
        check(rc == 9, "比核数还多(threads 12 + 上限 12)被拦下", out)
        check("比核数还多" in out, "拦下时说明了为什么(配置写坏了)", out)

    # ── ①quater 出厂默认改成 4 之后: **满核警告必须还在**(2026-11-09 晚更晚)
    #    ⚠️ **这条最容易漏**: 那条警告原来看的是「出厂默认」⇒ 默认一变 4,
    #    它就**永远不触发**了 —— 而旋钮**照样能拧到 8** ⇒ 产线就收不到
    #    「别往上调」的提醒。⇒ 警告必须看**用户拧得到几核**。
    with tempfile.TemporaryDirectory() as td:
        d = make_fixture(os.path.join(td, "d4"), threads="4", cap="8", knob="8")
        rc, out = run_block(block_a, d)
        check(rc != 9, "出厂默认 4 核 + 上限 8 的镜像**能**打出来", out)
        check("出厂默认 4 核" in out, "出厂默认是 4 核", out)
        check("旋钮上限 8 核" in out, "旋钮能拧到的上限还是 8", out)
        check("WARN" in out and "满核" in out,
              "**满核警告照样触发**(它看的是「旋钮能拧到几核」, 不是出厂默认)", out)
        stamp = os.path.join(d, "版本.txt")
        if os.path.exists(stamp):
            with open(stamp, encoding="utf-8") as f:
                st = f.read()
            check("threads: 4" in st, "版本戳里的自查命令写的是 threads: 4")
            check("旋钮上限" in st, "版本戳里分别写了「出厂默认」和「旋钮上限」")

    # ── ①quinquies 旋钮拧不到 8 ⇒ 就**不该**报满核 ────────────────────
    with tempfile.TemporaryDirectory() as td:
        d = make_fixture(os.path.join(td, "d5"), threads="4", cap="8", knob="4")
        rc, out = run_block(block_a, d)
        check(rc != 9, "旋钮只到 4 核的镜像不被拦下", out)
        check("旋钮上限 4 核" in out, "旋钮上限算的是 4(取旋钮 max 与硬上限的较小者)", out)
        check("满核" not in out, "旋钮拧不到 8 ⇒ 不该报满核警告", out)

    # ── ①sextans 代码里的硬上限写坏了(比核数还多)⇒ 拦下 ───────────────
    with tempfile.TemporaryDirectory() as td:
        d = make_fixture(os.path.join(td, "d6"), threads="4", cap="12", knob="8")
        rc, out = run_block(block_a, d)
        check(rc == 9, "硬上限比核数还多(MAX_STRESS_THREADS = 12)被拦下", out)
        check("比核数还多" in out, "拦下时说明了为什么(代码写坏了)", out)

    # ── ② console script 闸门 ───────────────────────────────────────
    with tempfile.TemporaryDirectory() as td:
        d = make_fixture(os.path.join(td, "e"))
        rc, out = run_block(block_b, d)
        check(rc != 9, "干净的 start.sh 不再被误报(2026-11-09 那个假阳性)", out)

    with tempfile.TemporaryDirectory() as td:
        sh = CLEAN_START_SH + '    "$VENV_DIR"/bin/uvicorn server.main:app &\n'
        d = make_fixture(os.path.join(td, "f"), start_sh=sh)
        rc, out = run_block(block_b, d)
        check(rc == 9, "真调了 console script(uvicorn)照样被拦", out)
        check("DIE" in out, "拦下时打的是 DIE 而不是别的", out)

    with tempfile.TemporaryDirectory() as td:
        sh = CLEAN_START_SH + '    "$VENV_DIR"/bin/pip install -r requirements.txt\n'
        d = make_fixture(os.path.join(td, "g"), start_sh=sh)
        rc, out = run_block(block_b, d)
        check(rc == 9, "真调了 console script(pip)照样被拦", out)

    with tempfile.TemporaryDirectory() as td:
        sh = '# 记住: 不能用 "$VENV_DIR"/bin/uvicorn, 得用 python3 -m uvicorn\n' + CLEAN_START_SH
        d = make_fixture(os.path.join(td, "h"), start_sh=sh)
        rc, out = run_block(block_b, d)
        check(rc != 9, "注释里提到 console script 不算(别误伤)", out)

    print("\n%d 项通过, %d 项失败" % (PASSES, len(FAILS)))
    if FAILS:
        for f in FAILS:
            print("  ✗ " + f)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
