"""回归测试: **回归套件不许把产线上正在跑的服务杀掉**。

起因(**真实停产事故**, 2026-11-11 13:56):
    我在产线板上跑整套回归, 回归跑到一半, **产线服务自己没了** ——
    uvicorn 优雅退出(`Finished server process [246118]`),
    但日志里**没有**本服务自己的退出理由行(那行只有「自己退出」才会打)
    ⇒ 说明**不是**服务的空闲自退, 是**被外面 SIGTERM 掉的**。
    盘上的 PID 文件还留着 246118(说明不是 `start.sh stop` 干的)。

    病根: `tests/test_usb_app_mode.py` 拿**临时目录里那份 start.sh**
    跑 `start` / `stop` —— 但**没设 PORT** ⇒ start.sh 用默认值 **8000**
    ⇒ 而 `stop_service()` 在「PID 文件不在」(临时目录里当然不在)时
    会走 **`kill_port_holder 8000`** ⇒ **把产线上占着 8000 的服务杀了**。

    为什么日志里查不到凶手: **凶手就是回归自己** —— 套件是**一个个测试
    模块串行跑**的, 跑完就散了, 谁也不会留下"我杀了 246118"的记录。

    为什么难查: uvicorn 收到 SIGTERM 走的是**优雅退出**, 打印出来跟
    「服务自己关的」几乎一样 ⇒ 光看日志会以为是正常退出。

这个测试守三件事:
  ① **动态**: `start.sh` 只会杀**像我们自己这个服务**的进程 ——
     别人占着端口 ⇒ **不动它**(不许「端口上是谁就杀谁」)。
     而**我们自己的旧进程**(cmdline 带 uvicorn)⇒ **照样要清**
     (这条自愈路径不能被加固改坏: 盘换了/PID 文件丢了也得能起得来)。
  ② **静态**: 套件里**凡是跑 start.sh 的地方**, 都必须**显式指定端口**,
     而且**不许**碰产线端口 8000。
  ③ **静态**: `tests/run_all.py` 里有**绊线** —— 跑之前/跑之后各看一次
     8000 上是谁, 不一样就**大声报出来**(而不是悄悄把产线服务弄没了)。

跑法: .venv/bin/python -m tests.test_suite_port_safety
"""
import os
import re
import socket
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests._tmp import mkdtemp                      # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# ⚠️ 变异脚本会把**改坏的那几份**指过来(AGING_START_SH / AGING_TESTS_DIR)
START_SH = os.environ.get("AGING_START_SH", os.path.join(REPO, "start.sh"))
TESTS_DIR = os.environ.get("AGING_TESTS_DIR", os.path.join(REPO, "tests"))
PROD_PORT = 8000

FAILS = []
PASSES = 0


def check(cond, msg, extra=""):
    global PASSES
    if cond:
        PASSES += 1
        print("  ✓ " + msg)
    else:
        FAILS.append(msg)
        print("  ✗ " + msg + ("  → " + str(extra) if extra else ""))


def section(title):
    print("\n" + title)


def free_port():
    """找一个空着的端口(不能是产线端口)。"""
    for _ in range(50):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("127.0.0.1", 0))
            p = s.getsockname()[1]
        finally:
            s.close()
        if p != PROD_PORT:
            return p
    raise RuntimeError("找不到可用端口")


def wait_port_listening(port, timeout=10.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.settimeout(0.5)
            s.connect(("127.0.0.1", port))
            return True
        except OSError:
            time.sleep(0.1)
        finally:
            s.close()
    return False


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def died(proc, timeout=5.0):
    """这个进程**真的退了**吗?

    ⚠️ 不能用 `os.kill(pid, 0)` 判 —— **僵尸进程也算「活着」**
       (信号 0 只是查存在性, 僵尸还没被父进程收走 ⇒ 存在)
       ⇒ 被 SIGTERM 掉的进程会留下一个僵尸 ⇒ 误判成「还活着」。
       ⇒ 改成**等它自己退出**(wait 会顺手把僵尸收掉)。
    """
    try:
        proc.wait(timeout=timeout)
        return True
    except subprocess.TimeoutExpired:
        return False


def run_start(cmd, workdir, port, timeout=120):
    env = dict(os.environ)
    env["PORT"] = str(port)
    env["AGING_DATA_DIR"] = os.path.join(workdir, "data")
    os.makedirs(env["AGING_DATA_DIR"], exist_ok=True)
    p = subprocess.run(["bash", START_SH] + cmd, cwd=workdir, env=env,
                       capture_output=True, text=True, timeout=timeout)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def make_listener(path, port, name=None):
    """起一个占着端口的进程, 返回 (pid, 端口)。

    `name` = 塞进 cmdline 的那个词 ⇒ 决定 start.sh 认不认它是「自己人」:
      · name=None ⇒ cmdline 里**既没有 uvicorn 也没有 server.main**
        ⇒ 模拟「别人的进程」(start.sh 不许动它);
      · name="uvicorn" ⇒ cmdline 里带 uvicorn
        ⇒ 模拟「本服务的旧进程」(start.sh 要清掉它)。
    """
    body = (
        "#!/usr/bin/env python3\n"
        "import socket, sys, time\n"
        "p = int(sys.argv[-1])\n"
        "s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n"
        "s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
        "s.bind(('127.0.0.1', p)); s.listen(8)\n"
        "sys.stderr.write('listening %d\\n' % p); sys.stderr.flush()\n"
        "time.sleep(600)\n"
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write(body)
    os.chmod(path, 0o755)
    argv = [path]
    if name:
        argv.append(name)
    argv += ["--port", str(port)]
    proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    return proc


def main():
    work = mkdtemp(prefix="aging-portsafety-")

    section("① start.sh 只会杀「像我们自己这个服务」的进程")
    # ── ①-A 别人占着端口 ⇒ 不许杀 ──
    port = free_port()
    proc = make_listener(os.path.join(work, "listener_other.py"), port)
    try:
        if not wait_port_listening(port):
            check(False, "哑进程没能占住端口 %d" % port)
            return finish()
        code, out = run_start(["stop"], work, port)
        check(not died(proc, timeout=2),
              "**不是本服务的进程占着端口 ⇒ 没被杀**(PID %d 还活着)" % proc.pid,
              "被杀了 —— 这就是 2026-11-11 那次停产事故的形状")
        check("不是本服务" in out or "不动它" in out,
              "而且**如实报了出来**(让人知道端口被谁占着)", out[-400:])
    finally:
        proc.kill()
        proc.wait(timeout=10)

    # ── ①-B 自己人占着端口 ⇒ 照样要清(自愈路径不能被改坏) ──
    port = free_port()
    proc = make_listener(os.path.join(work, "listener_uvicorn.py"), port,
                         name="uvicorn")
    try:
        if not wait_port_listening(port):
            check(False, "哑进程没能占住端口 %d" % port)
            return finish()
        code, out = run_start(["stop"], work, port)
        gone = died(proc, timeout=5)
        check(gone,
              "**像本服务的旧进程**占着端口 ⇒ 照样清掉(PID 文件丢了也要能起得来)",
              "还活着 —— 自愈路径被加固改坏了")
    finally:
        try:
            proc.kill()
            proc.wait(timeout=10)
        except Exception:
            pass

    section("② 静态: 套件里跑 start.sh 的地方必须显式指定端口")
    # ⚠️ 只认**把 start.sh 当命令跑**的写法(`bash start.sh …`)——
    #   只是**读它的文本**(查锚点/查版式)不算。
    RUN_RE = re.compile(r"""bash["']?\s*,\s*["'][^"']*start\.sh""")
    offenders = []
    runners = []
    for name in sorted(os.listdir(TESTS_DIR)):
        if not name.startswith("test_") or not name.endswith(".py"):
            continue
        src = open(os.path.join(TESTS_DIR, name), encoding="utf-8").read()
        if not RUN_RE.search(src):
            continue
        runners.append(name)
        # 跑 start.sh ⇒ 必须有 PORT 的赋值(否则就是默认 8000 = 产线端口)
        has_port = (re.search(r"""env\[["']PORT["']\]\s*=""", src)
                    or re.search(r"""\bPORT\s*=\s*str\(""", src)
                    or re.search(r"""["']PORT["']\s*:""", src)
                    or re.search(r"""\bPORT\s*=\s*["']?\$\{?""", src))
        if not has_port:
            offenders.append(name)
    check(not offenders,
          "每个**把 start.sh 当命令跑**的测试都显式指定了 PORT"
          "(不碰产线的 8000)",
          "跑 start.sh 的: %s / 没设 PORT 的: %s" % (runners, offenders))
    check("test_usb_app_mode.py" in runners,
          "出事的那一个确实被这条扫描盯上了", runners)

    # 具体盯住 2026-11-11 出事的那一个
    src = open(os.path.join(TESTS_DIR, "test_usb_app_mode.py"),
               encoding="utf-8").read()
    check(re.search(r'env\["PORT"\]\s*=\s*str\(', src) is not None,
          "**出事的那一个** (test_usb_app_mode.py) 现在显式设了 PORT", src[:200])
    check(re.search(r'PROD_PORT\s*=\s*8000', src) is not None,
          "而且它知道产线端口是 8000(PROD_PORT)")

    section("③ 静态: run_all.py 有绊线(端口占用者跑前跑后对不上 ⇒ 大声报)")
    src = open(os.path.join(TESTS_DIR, "run_all.py"), encoding="utf-8").read()
    check("8000" in src or "PROD_PORT" in src,
          "run_all.py 知道产线端口", src[:200])
    check(re.search(r"before", src) and re.search(r"after", src),
          "run_all.py 在跑套件**前后各取一次**端口占用者")
    check("产线" in src,
          "而且绊线用**人话**说明白(产线服务被动过了)")
    check(re.search(r"holder_before\s*!=\s*holder_after", src) is not None,
          "绊线**真的会比**前后两个占用者(不是摆在那儿好看的)",
          src[-800:])

    return finish()


def finish():
    print("\n" + "─" * 60)
    if FAILS:
        print("✗ %d 项失败 / 共 %d 项" % (len(FAILS), PASSES + len(FAILS)))
        for f in FAILS:
            print("   · " + f)
        sys.exit(1)
    print("✓ 全部通过 (%d 项)" % PASSES)


if __name__ == "__main__":
    main()
