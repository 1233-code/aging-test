"""「启动就绪判定」的回归测试 —— U 盘版换设备双击没反应的第二个病根。

**现场(2026-11-05 新设备实测)**: 服务**明明起来了**
(服务日志里有 "Uvicorn running on http://0.0.0.0:8000"), 可 `start.sh`
的就绪检查连着 15 秒都说「没起来」⇒ 脚本 `return 1` ⇒

  · **打开浏览器那段永远执行不到** ⇒ 工人看到的就是「点了没反应」;
  · 启动器弹出「启动失败」⇒ 工人以为程序坏了。

**毛毛不在服务, 在检查这一侧**: 证据是那台设备的**服务日志里一条
`GET /api/health` 都没进来** ⇒ 请求压根没发出去。三种可能, 都会踩:

  ① 新设备**没装 curl**(检查直接静默失败);
  ② 有 curl, 但环境里**有 http_proxy** ⇒ 代理把 127.0.0.1 也一起带走;
  ③ `localhost` 解析到 `::1`, 而服务只听 `0.0.0.0`(IPv4)。

⇒ 修法: 判据改成三层 —— **python 探针**(显式禁代理 + 只打 127.0.0.1)
  → curl(`--noproxy '*'`) → **端口在听就算起来了**。

跑法: .venv/bin/python tests/test_usb_startup_ready.py
"""
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
START_SH = REPO / "start.sh"

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


# ══════════════════════════════════════════════════════════
#  把 start.sh 里的 shell 函数抠出来, 单独拿去跑
# ══════════════════════════════════════════════════════════
def extract_func(name):
    """从 start.sh 里原样抠出一个 shell 函数(连注释一起)。"""
    text = START_SH.read_text(encoding="utf-8")
    m = re.search(rf"^{re.escape(name)}\(\) \{{\n.*?^\}}$", text, re.S | re.M)
    return m.group(0) if m else ""


NEEDED_FUNCS = ("_health_probe", "port_listening", "service_ready", "service_up")


def build_probe(tmp, port, venv_dir, path_env, extra_env=None):
    """写一个只含这几个函数的探针脚本, 返回它的路径。"""
    body = []
    for fn in NEEDED_FUNCS:
        src = extract_func(fn)
        if not src:
            return None
        body.append(src)
    sh = tmp / "probe.sh"
    sh.write_text(
        "#!/bin/bash\n"
        f"VENV_DIR={shlex.quote(str(venv_dir))}\n"
        f"PORT={int(port)}\n"
        f"URL={shlex.quote('http://127.0.0.1:%d' % port)}\n"
        + "\n\n".join(body)
        + "\n\n\"${1:-service_ready}\"\n",
        encoding="utf-8",
    )
    sh.chmod(0o755)
    return sh


def run_probe(sh, func, path_env, extra_env=None):
    env = {"PATH": str(path_env), "HOME": "/tmp"}
    if extra_env:
        env.update(extra_env)
    p = subprocess.run(["/bin/bash", str(sh), func], env=env,
                       capture_output=True, text=True, timeout=60)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def make_bin(tmp, with_curl=None, with_python3=False):
    """造一个「尽量干净」的 PATH 目录: 基础工具有, curl/python3 按需给。"""
    d = tmp / "bin"
    d.mkdir(parents=True, exist_ok=True)
    for tool in ("awk", "cat", "grep", "head", "tr", "sed", "date", "sleep"):
        src = shutil.which(tool)
        if src and not (d / tool).exists():
            os.symlink(src, d / tool)
    if with_curl is not None:
        f = d / "curl"
        f.write_text(with_curl, encoding="utf-8")
        f.chmod(0o755)
    if with_python3:
        os.symlink(sys.executable, d / "python3")
    return d


HEALTH_SERVER = r'''
import http.server, sys
port = int(sys.argv[1]); code = int(sys.argv[2])
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = b'{"ok":true,"status":"idle"}' if code == 200 else b'{"detail":"boom"}'
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a):
        pass
http.server.HTTPServer(("127.0.0.1", port), H).serve_forever()
'''

# 只接受连接、永远不回 HTTP 的假服务
SILENT_SERVER = r'''
import socket, sys
s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("127.0.0.1", int(sys.argv[1]))); s.listen(8)
conns = []
while True:
    c, _ = s.accept(); conns.append(c)
'''


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def wait_port(port, timeout=8.0):
    end = time.time() + timeout
    while time.time() < end:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                return True
        except OSError:
            time.sleep(0.05)
    return False


def start_server(script, port, *args):
    p = subprocess.Popen([sys.executable, "-c", script, str(port)] + [str(a) for a in args],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    wait_port(port)
    return p


# ══════════════════════════════════════════════════════════
#  ① 静态锚点 —— 判据的形状
# ══════════════════════════════════════════════════════════
def static_anchors():
    print("\n① 静态锚点(就绪判定的形状)")
    text = START_SH.read_text(encoding="utf-8")

    check("_health_probe" in text, "有 python 探针(不依赖 curl 的那条判据)")
    check("ProxyHandler({})" in text,
          "探针**显式禁用代理**(http_proxy 那个坑)")
    check("127.0.0.1" in extract_func("_health_probe"),
          "探针只打 127.0.0.1(localhost 解析到 ::1 那个坑)")
    check("--noproxy" in extract_func("service_ready"),
          "curl 那条也加了 --noproxy")
    check("/proc/net/tcp" in extract_func("port_listening"),
          "端口兜底不依赖 lsof/fuser(新设备上不一定装了)")
    check('URL="http://127.0.0.1:$PORT"' in text,
          "浏览器/接口地址用 127.0.0.1, 不用 localhost")
    check(re.search(r"for _ in \{1\.\.6\d\};", text) is not None,
          "就绪等待窗口 ≥ 30 秒(原来 15 秒太紧)")
    check("if service_up; then" in text,
          "启动后的成败判定用 service_up(端口在听就不算失败)")
    # ⚠️ 光看「出现过 service_up」不够 —— 等待循环和**事后判定**两处都得用
    #   service_up, 少一处就回到「端口在听也报失败」的老毛病
    check(text.count("if service_up; then") >= 2,
          "等待循环**和**事后判定两处都用 service_up",
          text.count("if service_up; then"))
    # 判据顺序: python 探针必须排在 curl 前面
    sr = extract_func("service_ready")
    check(sr.find("_health_probe") < sr.find("curl"),
          "python 探针排在 curl 前面(curl 在新设备上可能压根没有)")


# ══════════════════════════════════════════════════════════
#  ② 行为 —— 三个坑逐个复现
# ══════════════════════════════════════════════════════════
def behaviour():
    print("\n② 行为(新设备上真实踩到的三个坑)")
    tmp = Path(tempfile.mkdtemp(prefix="ready-"))
    try:
        # 假 venv: bin/python3 是个软链(**跟盘上的 venv 一样**)
        venv = tmp / "venv"
        (venv / "bin").mkdir(parents=True)
        os.symlink(sys.executable, venv / "bin" / "python3")

        port = free_port()
        srv = start_server(HEALTH_SERVER, port, 200)
        try:
            sh = build_probe(tmp, port, venv, "/usr/bin:/bin")
            if sh is None:
                check(False, "能从 start.sh 里抠出那几个函数")
                return

            # ① curl 压根不存在(新设备上可能没装)
            bin_nocurl = make_bin(tmp / "b1")
            rc, out = run_probe(sh, "service_ready", bin_nocurl)
            check(rc == 0, "没装 curl 也认得出服务起来了", (rc, out))

            # ② curl 在, 但它自己坏了(静默失败)
            bin_badcurl = make_bin(tmp / "b2", with_curl="#!/bin/sh\nexit 7\n")
            rc, out = run_probe(sh, "service_ready", bin_badcurl)
            check(rc == 0, "curl 自己坏掉也还有别的判据顶着", (rc, out))

            # ③ ⚠️ **http_proxy 劫持** —— 代理把本机也一起带走
            dead = free_port()
            bin_proxy = make_bin(tmp / "b3")
            rc, out = run_probe(sh, "service_ready", bin_proxy,
                                extra_env={"http_proxy": "http://127.0.0.1:%d" % dead,
                                           "HTTP_PROXY": "http://127.0.0.1:%d" % dead})
            check(rc == 0, "有 http_proxy 也照样认得出服务起来了", (rc, out))
        finally:
            srv.terminate()
            srv.wait(timeout=5)

        # ④ 压根没有服务 ⇒ 不能误报「起来了」
        port2 = free_port()
        sh2 = build_probe(tmp, port2, venv, "/usr/bin:/bin")
        rc, out = run_probe(sh2, "service_ready", "/usr/bin:/bin")
        check(rc != 0, "没有服务时不会误判成起来了", (rc, out))
        rc, out = run_probe(sh2, "service_up", "/usr/bin:/bin")
        check(rc != 0, "没有服务时 service_up 也说没起来", (rc, out))

        # ⑤ 服务在听, 但 /api/health 报 500
        #    ⇒ service_ready 该说「不健康」, service_up 该说「起来了」
        port3 = free_port()
        srv3 = start_server(HEALTH_SERVER, port3, 500)
        try:
            sh3 = build_probe(tmp, port3, venv, "/usr/bin:/bin")
            rc, out = run_probe(sh3, "service_ready", "/usr/bin:/bin")
            check(rc != 0, "健康接口 500 ⇒ service_ready 说不健康", (rc, out))
            rc, out = run_probe(sh3, "service_up", "/usr/bin:/bin")
            check(rc == 0, "端口在听 ⇒ service_up 说起来了(不再误报失败)", (rc, out))
        finally:
            srv3.terminate()
            srv3.wait(timeout=5)

        # ⑥ 端口在听但一个字节都不回(健康接口哑了)
        port4 = free_port()
        srv4 = start_server(SILENT_SERVER, port4)
        try:
            sh4 = build_probe(tmp, port4, venv, "/usr/bin:/bin")
            rc, out = run_probe(sh4, "service_up", "/usr/bin:/bin")
            check(rc == 0, "端口在听但接口哑了 ⇒ service_up 仍认它起来了", (rc, out))
        finally:
            srv4.terminate()
            srv4.wait(timeout=5)

        # ⑦ 判据不能「永远成功」—— venv 的 python 没了、PATH 里也没 python3
        #    ⇒ 就该老老实实说「没起来」
        empty_venv = tmp / "empty-venv"
        (empty_venv / "bin").mkdir(parents=True)
        port5 = free_port()
        bin_b4 = make_bin(tmp / "b4")
        sh5 = build_probe(tmp, port5, empty_venv, bin_b4)
        rc, out = run_probe(sh5, "service_ready", bin_b4)
        check(rc != 0, "没有 python 也没有 curl ⇒ 老老实实说没起来", (rc, out))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    static_anchors()
    behaviour()

    print("\n" + "─" * 60)
    if FAILS:
        print(f"✗ {len(FAILS)} 项失败 / 共 {PASSES + len(FAILS)} 项")
        for f in FAILS:
            print("   · " + f)
        return 1
    print(f"✓ 全部通过 ({PASSES} 项)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
