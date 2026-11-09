"""回归测试: GPU 项的 **X 会话探测** —— 别让一台好板子被前置条件挡死。

**起因 (2026-10-29 新板第一轮实测)**
    新板那轮 GPU 项判 **error**, 报错写着:

        GPU 前置条件不满足 (不静默降级, 见 §6.3):
        - X11 会话 (DISPLAY) —— DISPLAY=:0.0 但
          XAUTHORITY=/home/User/.Xauthority 不存在

    那一轮是**第一轮** —— 也就是**开机后头一次**跑。

**查实 (2026-11-02, 老板子, 同一套镜像)**
    · X server 是 `/usr/bin/X11/X -nolisten tcp -auth /var/run/slim.auth`
      —— 真正在用的 cookie 是 **/var/run/slim.auth**(root 独占 0600)
    · `~/.Xauthority` 是**另一份** cookie(不是 server 那份), 而且是厂商
      那套 x11vnc/xhost 启动脚本**开机十来秒之后**才建的
      ⇒ **第一轮跑的时候它压根还不存在**
    · **拿一个假 cookie、甚至完全不设 XAUTHORITY, xdpyinfo 都照样连上**
      ⇒ 这个 X server 压根没强制 MIT cookie 认证

    ⇒ 「找不到 cookie 文件」**不等于**「连不上」。旧判据拿一个跟「会话能不能
    用」毫无关系的事实去否决一台好板子 —— 于是 GPU 项判 error,
    glmark2 **一次都没起来过**(§6.3)。

**修法 (两条)**
    ① cookie 的**权威来源**改成 X server **自己命令行里的 -auth**(厂商
       watchdog.sh 就是这么找的), 免得去猜 ~/.Xauthority —— 猜的那份
       可能是**错的**(它跟 server 那份根本不是同一个 cookie)
    ② 前置检查改成**实连一次**(xdpyinfo): 连上了就算数; 连不上就把 X
       server 的**原话**写进报告。判据从「路径存不存在」换成「连不连得上」。

这个测试**不碰真硬件、也不连真 X server** —— X server 的 argv 和实连结果
都是塞进去的假货(开发机上真的跑着一个 X server, 不堵住它这个测试就不准)。

跑法: .venv/bin/python -m tests.test_gpu_xauthority
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.tests import gpu as gpu_mod          # noqa: E402
from tests._tmp import mkdtemp                   # noqa: E402

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


class EnvGuard:
    """把 os.environ 的改动**原样还回去** —— 测试不该污染后面的用例。"""

    def __init__(self, **kw):
        self.new = kw
        self.old = {}

    def __enter__(self):
        for k, v in self.new.items():
            self.old[k] = os.environ.get(k)
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return self

    def __exit__(self, *a):
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return False


class FakeX:
    """把「X server 的 argv」和「实连结果」换成假货。

    ⚠️ 必须堵: 开发机上真的跑着一个 X server(`/usr/bin/X11/X -auth
    /var/run/slim.auth`), 不堵的话这个测试测的是开发机, 不是被测板子。
    """

    def __init__(self, argvs, probe=(True, "xdpyinfo 实连成功(:0.0)")):
        self.argvs = argvs
        self.probe = probe
        self._old = {}

    def __enter__(self):
        self._old["argvs"] = gpu_mod._xserver_argvs
        self._old["probe"] = gpu_mod._probe_x11
        gpu_mod._xserver_argvs = lambda *a, **kw: list(self.argvs)

        async def _fake_probe(display, env):
            return self.probe

        gpu_mod._probe_x11 = _fake_probe
        return self

    def __exit__(self, *a):
        gpu_mod._xserver_argvs = self._old["argvs"]
        gpu_mod._probe_x11 = self._old["probe"]
        return False


def make_cookie(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write("fake cookie\n")
    return path


base = mkdtemp(prefix="aging-xauth-")
runtime_dir = os.path.join(base, "run", "user", "1000")
# 三种家目录, 各管各的, 别互相串:
#   empty_home   —— 里面**没有** .Xauthority (「一个 cookie 都没有」用)
#   cookie_home  —— 里面有 .Xauthority (~/.Xauthority 那条分支用)
empty_home = os.path.join(base, "home_empty")
cookie_home = os.path.join(base, "home_cookie")
os.makedirs(empty_home, exist_ok=True)
os.makedirs(cookie_home, exist_ok=True)
gdm_cookie = make_cookie(os.path.join(runtime_dir, "gdm", "Xauthority"))
home_cookie = make_cookie(os.path.join(cookie_home, ".Xauthority"))
# X server 自己 `-auth` 指的那份 —— 2026-11-02 老板子上它就是
# /var/run/slim.auth(root 独占 0600), 这里拿临时目录代一份
server_cookie = make_cookie(os.path.join(base, "run_slim.auth"))
stale = os.path.join(empty_home, ".Xauthority")      # env 指的就是它, 但**不存在**
no_runtime = os.path.join(base, "nothing")          # 不存在的 runtime 目录

# 各种 X server 的 argv
ARGV_WITH_AUTH = [["/usr/bin/X11/X", "-nolisten", "tcp", "-auth", server_cookie]]
ARGV_NO_AUTH = [["/usr/bin/X11/X", "-nolisten", "tcp"]]


# ══════════════════════════════════════════════════════════════════════
section("【1】cookie 的权威来源 = X server 自己命令行里的 -auth")

with EnvGuard(XAUTHORITY=home_cookie, XDG_RUNTIME_DIR=runtime_dir,
              HOME=cookie_home), FakeX(ARGV_WITH_AUTH):
    info = gpu_mod._discover_xauth(display=":0.0")
    check(info.path == server_cookie,
          "用的是 server 自己 -auth 那份(不是 env 里的)", info.path)
    check("-auth" in info.source, "判据说清了来源", info.source)

section("【2】第一轮的真实场景: env 指向不存在的文件、~/.Xauthority 也没有")

with EnvGuard(XAUTHORITY=stale, XDG_RUNTIME_DIR=runtime_dir,
              HOME=empty_home), FakeX(ARGV_WITH_AUTH):
    info = gpu_mod._discover_xauth(display=":0.0")
    check(info.path == server_cookie,
          "照样找得到 cookie —— 找到的是 server 那份", info.path)

section("【3】前置检查: 实连成功 ⇒ 放行(哪怕一个 cookie 都没有)")

with EnvGuard(XAUTHORITY=stale, XDG_RUNTIME_DIR=runtime_dir, DISPLAY=":0.0",
              HOME=empty_home), FakeX(ARGV_NO_AUTH):
    ok, detail = asyncio.run(gpu_mod._precheck_x11())
    check(ok, "放行 —— 会话真的连得上, 就该放行", detail)
    check("实连成功" in detail, "详情里写的是实连结果", detail)

section("【4】前置检查: 实连失败 ⇒ 判不过, 且把 X server 的原话写进报告")

with EnvGuard(XAUTHORITY=stale, XDG_RUNTIME_DIR=runtime_dir, DISPLAY=":0.0",
              HOME=empty_home), FakeX(
                  ARGV_WITH_AUTH,
                  probe=(False, "xdpyinfo 连不上 :0.0: No protocol specified")):
    ok, detail = asyncio.run(gpu_mod._precheck_x11())
    check(not ok, "判不过", detail)
    check("No protocol specified" in detail, "把 X server 的原话带进报告", detail)
    check("实连失败" in detail, "说清了是实连失败(不是「文件不存在」)", detail)

section("【5】X server 启动时没带 -auth ⇒ 没开访问控制 ⇒ 放行")

with EnvGuard(XAUTHORITY=stale, XDG_RUNTIME_DIR=no_runtime, DISPLAY=":0.0",
              HOME=empty_home), FakeX(
                  ARGV_NO_AUTH,
                  probe=(None, "本机没有 xdpyinfo / xset, 探测不了")):
    ok, detail = asyncio.run(gpu_mod._precheck_x11())
    check(ok, "放行 —— 没开访问控制就不需要 cookie", detail)
    check("没带 -auth" in detail, "说清了原因", detail)

section("【6】X server 带 -auth 但那份 cookie 不存在 ⇒ 判不过")

missing_auth = os.path.join(base, "no_such_cookie")
with EnvGuard(XAUTHORITY=None, XDG_RUNTIME_DIR=no_runtime, DISPLAY=":0.0",
              HOME=empty_home), FakeX(
                  [["/usr/bin/X11/X", "-nolisten", "tcp", "-auth", missing_auth]],
                  probe=(None, "本机没有 xdpyinfo / xset, 探测不了")):
    ok, detail = asyncio.run(gpu_mod._precheck_x11())
    check(not ok, "判不过", detail)
    check(missing_auth in detail, "点名那个不存在的 cookie 文件", detail)

section("【7】注入给提权子进程的 cookie = 探测到的那份(server 的)")

with EnvGuard(XAUTHORITY=stale, XDG_RUNTIME_DIR=runtime_dir, DISPLAY=":0.0",
              HOME=empty_home), FakeX(ARGV_WITH_AUTH):
    env = gpu_mod._build_session_env()
    check(env.get("XAUTHORITY") == server_cookie,
          "子进程拿到的是 server 那份 cookie", env.get("XAUTHORITY"))
    check(env.get("DISPLAY") == ":0.0", "会话变量照常透传", env)
    check(env.get("XDG_RUNTIME_DIR") == runtime_dir, "runtime 目录也透传", env)

section("【8】一个 cookie 都探测不到 ⇒ 别塞一个根本不存在的路径进去")

with EnvGuard(XAUTHORITY=stale, XDG_RUNTIME_DIR=no_runtime, DISPLAY=":0.0",
              HOME=empty_home), FakeX([]):
    env = gpu_mod._build_session_env()
    check("XAUTHORITY" not in env,
          "一个 cookie 都找不到时**不要**塞一个假路径进去", env)

section("【9】没有 DISPLAY ⇒ 判不过 (这个跟 cookie 无关)")

with EnvGuard(DISPLAY=None, HOME=empty_home), FakeX(ARGV_WITH_AUTH):
    ok, detail = asyncio.run(gpu_mod._precheck_x11())
    check(not ok, "没 DISPLAY 判不过", detail)
    check("DISPLAY" in detail, "说清了是 DISPLAY 的问题", detail)

section("【10】跑着好几个 X server ⇒ 按 display 挑对的那一个")

c0 = make_cookie(os.path.join(base, "x0.auth"))
c1 = make_cookie(os.path.join(base, "x1.auth"))
with EnvGuard(XAUTHORITY=None, XDG_RUNTIME_DIR=no_runtime, DISPLAY=":0.0",
              HOME=empty_home), FakeX([
                  ["/usr/bin/X11/X", ":1", "-auth", c1],
                  ["/usr/bin/X11/X", ":0", "-auth", c0],
              ]):
    info = gpu_mod._discover_xauth(display=":0.0")
    check(info.path == c0, "挑的是 display 对得上的那个", info.path)

section("【11】_probe_x11 本身: rc=0 ⇒ 连上了; rc≠0 ⇒ 连不上(带原话)")

_old_find, _old_run, _old_sudo = (gpu_mod.find_tool, gpu_mod.run_cmd,
                                  gpu_mod.with_sudo)
try:
    gpu_mod.with_sudo = lambda cmd, env=None, timeout_sec=None: list(cmd)
    gpu_mod.find_tool = lambda name: ("/usr/bin/xdpyinfo"
                                       if name == "xdpyinfo" else None)

    async def _rc0(cmd, timeout=5.0, input_data=None):
        return 0, "name of display:    :0.0", ""

    gpu_mod.run_cmd = _rc0
    ok, why = asyncio.run(gpu_mod._probe_x11(":0.0", {}))
    check(ok is True, "rc=0 ⇒ 连上了", why)

    async def _rc1(cmd, timeout=5.0, input_data=None):
        return 1, "", "No protocol specified\n"

    gpu_mod.run_cmd = _rc1
    ok, why = asyncio.run(gpu_mod._probe_x11(":0.0", {}))
    check(ok is False, "rc≠0 ⇒ 连不上", why)
    check("No protocol specified" in why, "把 X server 的原话带出来", why)

    gpu_mod.find_tool = lambda name: None
    ok, why = asyncio.run(gpu_mod._probe_x11(":0.0", {}))
    check(ok is None, "本机没有探测工具 ⇒ 判不了(None)", why)
finally:
    gpu_mod.find_tool, gpu_mod.run_cmd, gpu_mod.with_sudo = (_old_find,
                                                              _old_run,
                                                              _old_sudo)

section("【12】GPUTest._session_env() 用的也是探测到的那份")

with EnvGuard(XAUTHORITY=stale, XDG_RUNTIME_DIR=runtime_dir, DISPLAY=":0.0",
              HOME=empty_home), FakeX(ARGV_WITH_AUTH):
    env = gpu_mod.GPUTest._session_env()
    check(env.get("XAUTHORITY") == server_cookie,
          "拿到的是 server 那份 cookie", env.get("XAUTHORITY"))


section("【13】扫 /proc 找 X server: 只认 X server, 别的进程不当数")

fake_proc = os.path.join(base, "fake_proc")


def _fake_pid(pid, argv):
    d = os.path.join(fake_proc, pid)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "cmdline"), "wb") as f:
        f.write(b"\0".join(a.encode() for a in argv) + b"\0")


_fake_pid("123", ["/usr/bin/X11/X", "-nolisten", "tcp", "-auth", server_cookie])
_fake_pid("456", ["/usr/sbin/sshd", "-D"])
_fake_pid("789", ["/usr/bin/X11/Xorg"])
_fake_pid("999", [])                      # 内核线程(cmdline 是空的)
os.makedirs(os.path.join(fake_proc, "self"), exist_ok=True)   # 非数字目录, 要跳过

argvs = gpu_mod._xserver_argvs(proc_root=fake_proc)
check(len(argvs) == 2, "认出 2 个 X server(别的进程不当数)", argvs)
check(any(server_cookie in a for a in argvs),
      "argv 是**逐个参数**切开的(NUL 分隔, 路径里有空格也不会切坏)", argvs)
check(not any("sshd" in a[0] for a in argvs), "sshd 之类不当 X server", argvs)

info = gpu_mod._discover_xauth(display=":0.0", proc_root=fake_proc)
check(info.path == server_cookie, "从(假的) /proc 里也能找出 cookie", info.path)


print("\n" + ("全部通过" if not FAILS else f"有失败项 ({len(FAILS)})"))
for f in FAILS:
    print("  ✗ " + f)
sys.exit(0 if not FAILS else 1)
