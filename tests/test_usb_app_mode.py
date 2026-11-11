"""回归测试: 「U 盘版应用」—— 整个老化测试工具装在 U 盘上跑。

这个需求是 §8.2.3(报告进 U 盘)的**下一步**(docs/部署方式对比.md §13.4):
设备上什么都不装, 插上 U 盘就能用, 拔掉就什么都不剩, **功能跟装在设备上一样**。

守的是四件事:
  ① **工具找得到** —— fio / glmark2 在 U 盘上, 应用得先去那儿找
     (`AGING_TOOL_DIR`), 而不是去系统 PATH 里找;
  ② **数据落在 U 盘上** —— start.sh 看到 U 盘标记就把数据根指到 U 盘,
     拔盘即走, 设备上不留数据;
  ③ **报告跟着应用自己那根盘走** —— 插了两根盘(一根跑应用、一根给 USB 项
     写测试)时, 报告不能被拷到另一根上去;
  ④ **sudo 白名单漏了盘上工具时**, 别把它当成「板子坏了」。

⚠️ **本测试不碰 build/deps/ 里那些 .deb** —— 那是别人打的包(外来产物),
   解开它们、跑它们都得产线那边点头。跟镜像打包有关的验证
   (wrapper 的 LD_LIBRARY_PATH、glmark2 的 --data-path、核心库守卫)
   放在**镜像构建脚本自带的 §⑩ 自检**里, 在板子上构建时跑。

不碰真硬件: 假 sysfs / 假 /proc/mounts / 临时目录里的假 U 盘;
**不写仓库里的 reports/ 和 history.db**(那是产线在跑的真数据)。

跑法: .venv/bin/python -m tests.test_usb_app_mode
"""
import asyncio
import os
import shutil
import stat
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import usb_export as U
from server.tests import _tools as T
from tests._tmp import mkdtemp

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
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


def run(cmd, cwd=None, env=None, timeout=300):
    e = dict(os.environ)
    if env:
        e.update(env)
    p = subprocess.run(cmd, cwd=cwd, env=e, capture_output=True,
                       text=True, timeout=timeout)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def fake_bin(path, body):
    """造一个**本测试自己写的**假可执行文件。"""
    with open(path, "w") as f:
        f.write(body)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)


# ⚠️ **产线端口 —— 谁也不许在回归里碰它**。
#   产线服务就跑在 8000 上(本机就是那块板子, 服务从 U 盘上跑)。
PROD_PORT = 8000


def free_port():
    """找一个**空着的**端口, 保证不给产线服务(8000)招灾。

    ⚠️ **2026-11-11 血的教训**: 这个测试原来**不设 PORT** ⇒ `start.sh`
       用默认值 8000 ⇒ 而 `stop_service()` 在「PID 文件不在」时会去
       **`kill_port_holder 8000`** ⇒ 回归套件**亲手把产线服务杀了**
       (SIGTERM ⇒ uvicorn 优雅退出 ⇒ 产线当场断服务, 且日志里查不到是谁干的
       —— 因为干活的正是测试自己)。
       ⇒ 本测试**必须**显式指定一个自己的端口。
    """
    import socket
    for _ in range(50):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        finally:
            s.close()
        if port != PROD_PORT:
            return port
    raise RuntimeError("找不到一个能用的测试端口")


def port_holder_pid(port):
    """谁占着这个端口(没有 = 空)。"""
    try:
        p = subprocess.run(["lsof", "-t", "-iTCP:%d" % port, "-sTCP:LISTEN"],
                           capture_output=True, text=True, timeout=20)
        return (p.stdout or "").strip()
    except Exception:
        return ""


# ══════════════════════════════════════════════════════════
section("【1】工具目录优先 —— U 盘上的 fio 得比系统 PATH 先被找到")
# ══════════════════════════════════════════════════════════
def t1_tool_dir():
    tmp = mkdtemp(prefix="aging-tooldir-")
    bindir = os.path.join(tmp, "tools", "bin")
    os.makedirs(bindir)
    fake = os.path.join(bindir, "fio")
    fake_bin(fake, "#!/bin/sh\necho fake-fio\n")

    old = os.environ.get(T.TOOL_DIR_ENV)
    try:
        os.environ[T.TOOL_DIR_ENV] = os.path.join(tmp, "tools")
        got = T.find_tool("fio")
        check(got == fake, "工具目录里的 fio 被找到了", got)

        got2 = T.find_tool("sh")
        check(got2 is not None and got2 != os.path.join(bindir, "sh"),
              "工具目录里没有的 ⇒ 照旧去系统里找", got2)

        os.environ.pop(T.TOOL_DIR_ENV, None)
        got3 = T.find_tool("fio")
        check(got3 != fake, "没设环境变量时不会去工具目录里找", got3)
    finally:
        if old is None:
            os.environ.pop(T.TOOL_DIR_ENV, None)
        else:
            os.environ[T.TOOL_DIR_ENV] = old


# ══════════════════════════════════════════════════════════
section("【2】start.sh 的 U 盘模式自举 —— 数据根 / 工具目录 / 配置文件")
# ══════════════════════════════════════════════════════════
def t2_start_sh_usb_mode():
    tmp = mkdtemp(prefix="aging-usbmode-")
    shutil.copy2(os.path.join(REPO, "start.sh"), os.path.join(tmp, "start.sh"))
    with open(os.path.join(tmp, "config.yaml"), "w") as f:
        f.write("sn: SN-TEST\n")
    os.makedirs(os.path.join(tmp, "tools", "bin"))
    os.makedirs(os.path.join(tmp, "venv", "bin"))

    # 假的 **venv/bin/python3**: 把自己看到的三个环境变量写下来, 再起一个最小的
    # /api/health 服务 (start.sh 的就绪判定靠它)。
    # ⚠️ **必须是 python3 而不是 venv/bin/uvicorn** —— start.sh 现在用的是
    #   `python3 -m uvicorn`(可搬迁形式)。console script(`venv/bin/uvicorn`)
    #   的第一行写死了**构建时的绝对路径** ⇒ 盘换台设备就坏
    #   (详见 docs/部署方式对比.md §13.4 的前提③)。
    #   ⇒ 假的也得按 `-m uvicorn` 的调用形态接, 顺便把调用形态记下来。
    out_file = os.path.join(tmp, "seen.env")
    fake_python3 = os.path.join(tmp, "venv", "bin", "python3")
    fake_bin(fake_python3,
             "#!/usr/bin/env python3\n"
             "import json, os, sys\n"
             "from http.server import BaseHTTPRequestHandler, HTTPServer\n"
             "argv = sys.argv[1:]\n"
             "# ⚠️ **探针会把程序从 stdin 喂进来**(argv[0] == '-') —— 那就\n"
             "#   **exec 真的解释器**(读同一份 stdin): 假桩冒充不了解释器,\n"
             "#   而这一条路径(start.sh 的就绪判定)必须跟真机一模一样。\n"
             "if argv and argv[0] == '-':\n"
             "    os.execv(sys.executable, [sys.executable, '-'] + argv[1:])\n"
             "# ⚠️ **只认 `-m uvicorn` 这一种调用** —— 别的调用(比如就绪\n"
             "#   探针)不能让它改写 seen.env, 更不能让它起一个永远不退出的\n"
             "#   服务器(桩的端口是 0 = 随机)。\n"
             "if '-m' not in argv:\n"
             "    sys.exit(0)\n"
             "port = 0\n"
             "for i, a in enumerate(argv):\n"
             "    if a == '--port' and i + 1 < len(argv):\n"
             "        port = int(argv[i + 1])\n"
             "with open(os.environ['FAKE_UVI_OUT'], 'w') as fh:\n"
             "    for k in ('AGING_DATA_DIR', 'AGING_TOOL_DIR', 'AGING_CONFIG'):\n"
             "        fh.write('%s=%s\\n' % (k, os.environ.get(k, '')))\n"
             "    fh.write('ARGV=%s\\n' % ' '.join(argv))\n"
             "class H(BaseHTTPRequestHandler):\n"
             "    def do_GET(self):\n"
             "        self.send_response(200)\n"
             "        self.send_header('Content-Type', 'application/json')\n"
             "        self.end_headers()\n"
             "        self.wfile.write(json.dumps({'ok': True}).encode())\n"
             "    def log_message(self, *a):\n"
             "        pass\n"
             "HTTPServer(('127.0.0.1', port), H).serve_forever()\n")
    # venv/bin/python 也得有 (start.sh 用它判「镜像里带着建好的 venv」)
    os.symlink(sys.executable, os.path.join(tmp, "venv", "bin", "python"))

    # 假 xdg-open: 别让测试真的去开浏览器
    fakebin = os.path.join(tmp, "fakebin")
    os.makedirs(fakebin)
    fake_bin(os.path.join(fakebin, "xdg-open"), "#!/bin/sh\nexit 0\n")

    env = {"FAKE_UVI_OUT": out_file,
           "PATH": fakebin + os.pathsep + os.environ.get("PATH", "")}

    # ⚠️ **必须显式指定端口** —— 不指定就是 8000(默认值)⇒ `stop_service()`
    #    会去 `kill_port_holder 8000` ⇒ **回归把产线服务杀了**
    #    (2026-11-11 真的发生过一次, 产线当场断服务)。
    prod_holder_before = port_holder_pid(PROD_PORT)
    test_port = free_port()
    env["PORT"] = str(test_port)
    check(test_port != PROD_PORT,
          "测试用的是**自己的端口** %d, 不是产线的 %d" % (test_port, PROD_PORT),
          test_port)

    # **设备版(deb 装)的数据根在 /var/lib/aging-test**, 不在代码根 ——
    # 这里拿临时目录里一个同角色的目录来冒充它(别真往 /var/lib 里写)。
    device_data = os.path.join(tmp, "vardata")
    os.makedirs(device_data)
    env["AGING_DATA_DIR"] = device_data

    def seen_env():
        got = {}
        if os.path.exists(out_file):
            for line in open(out_file):
                if "=" in line:
                    k, v = line.strip().split("=", 1)
                    got[k] = v
        return got

    # ① **没有**标记 ⇒ 设备版行为: 数据根 = /var/lib/aging-test, 不指工具目录
    run(["bash", "start.sh", "start"], cwd=tmp, env=env, timeout=120)
    seen = seen_env()
    check(seen.get("AGING_DATA_DIR") == device_data,
          "设备版: 数据根 = /var/lib/aging-test(没被 U 盘模式改掉)",
          seen.get("AGING_DATA_DIR"))
    check(seen.get("AGING_TOOL_DIR") in (None, ""),
          "设备版: 不指工具目录", seen.get("AGING_TOOL_DIR"))
    run(["bash", "start.sh", "stop"], cwd=tmp, env=env, timeout=60)

    # ② **有**标记 ⇒ U 盘模式: 数据根被改到 U 盘上
    with open(os.path.join(tmp, "AGING_USB_MODE"), "w") as f:
        f.write("U 盘版应用\n")
    if os.path.exists(out_file):
        os.remove(out_file)
    code, out = run(["bash", "start.sh", "start"], cwd=tmp, env=env, timeout=120)
    seen = seen_env()
    check(seen.get("AGING_DATA_DIR") == tmp,
          "U 盘模式: 数据根被改到 U 盘上(不是 /var/lib/aging-test)",
          seen.get("AGING_DATA_DIR"))
    check(seen.get("AGING_TOOL_DIR") == os.path.join(tmp, "tools"),
          "U 盘模式: 工具目录指到 U 盘上的 tools/", seen.get("AGING_TOOL_DIR"))
    check(seen.get("AGING_CONFIG") == os.path.join(tmp, "config.yaml"),
          "U 盘模式: 用盘上那份 config.yaml", seen.get("AGING_CONFIG"))
    # ⚠️ **可搬迁形式**: 服务必须是用 `python3 -m uvicorn` 起的 ——
    #   console script(`venv/bin/uvicorn`)的第一行写死了构建时的绝对路径
    #   ⇒ 盘插到别的设备上就 bad interpreter ⇒ 双击「没反应」。
    #   (回归 tests/test_usb_relocatable.py 是这条的另一道绊线。)
    check(seen.get("ARGV", "").startswith("-m uvicorn"),
          "U 盘模式: 服务是 `python3 -m uvicorn` 起的(可搬迁形式)",
          seen.get("ARGV"))
    check("U 盘模式" in out, "启动时打了「U 盘模式」的横幅", out[-300:])
    run(["bash", "start.sh", "stop"], cwd=tmp, env=env, timeout=60)

    # ②' **产线端口必须原封不动** —— 这一条是本测试的**人命线**:
    #    回归跑在产线板上, 一旦把 8000 上的服务杀了, 工人当场断服务,
    #    而日志里**查不到是谁干的**(干活的正是这个测试自己)。
    prod_holder_after = port_holder_pid(PROD_PORT)
    check(prod_holder_after == prod_holder_before,
          "**产线端口 %d 上的服务没被动过** (跑之前 %s / 跑之后 %s)"
          % (PROD_PORT, prod_holder_before or "(空)", prod_holder_after or "(空)"),
          "跑之前=%s 跑之后=%s" % (prod_holder_before, prod_holder_after))

    # ③ 语法检查
    code, out = run(["bash", "-n", os.path.join(REPO, "start.sh")])
    check(code == 0, "start.sh 语法没问题", out)


# ══════════════════════════════════════════════════════════
section("【3】报告跟着应用自己那根盘走 (插两根盘时不能拷错)")
# ══════════════════════════════════════════════════════════
def t3_report_follows_own_stick():
    from server import storage
    from tests._fake_sysfs import make_mounts, make_sysfs  # 复用假 sysfs

    tmp = mkdtemp(prefix="aging-stickpref-")
    sysfs = make_sysfs(tmp, usb_disks=("sda", "sdb"), plain_disks=("mmcblk0",),
                       partitions={"sda": ["sda1"], "sdb": ["sdb1"],
                                   "mmcblk0": ["mmcblk0p1"]})
    stick_a = os.path.join(tmp, "stickA")
    stick_b = os.path.join(tmp, "stickB")
    os.makedirs(stick_a)
    os.makedirs(stick_b)
    mp = make_mounts(tmp, [
        f"/dev/sda1 {stick_a} vfat rw,relatime 0 0",
        f"/dev/sdb1 {stick_b} vfat rw,relatime 0 0",
    ])
    mounts = storage.read_mounts(mp)

    got = U.find_stick_for_path(stick_a + "/reports", mounts, sysfs)
    check(got is not None and got.mountpoint == stick_a,
          "认得出「路径所在的那根 U 盘」", got)
    got2 = U.find_stick_for_path("/var/lib/aging-test", mounts, sysfs)
    check(got2 is None, "路径在 eMMC 上 ⇒ 不算 U 盘", got2)

    pdf = os.path.join(tmp, "x.pdf")
    with open(pdf, "wb") as f:
        f.write(b"%PDF-1.4\n")
    orig_pref = U._preferred_paths
    try:
        # **应用自己跑在第二根盘上**(sdb) —— 故意**不是**「枚举到的第一根」。
        # 要是「报告跟着应用走」这条没实现, 它会落到 sda 上去 ⇒ 断言咬住。
        U._preferred_paths = lambda: [stick_b + "/reports"]
        res = asyncio.new_event_loop().run_until_complete(
            U.export_report_to_usb(pdf, {"sn": "SN1", "status": "passed",
                                         "started_at": 1000, "finished_at": 2000,
                                         "summary": {"total": 1, "passed": 1,
                                                     "planned": 1}},
                                   mounts=mounts, sys_block=sysfs))
        check(res.ok, "导出成功", res.message)
        check(res.directory == os.path.join(stick_b, U.USB_REPORT_DIRNAME),
              "报告落在**应用自己那根盘**上(不是枚举到的第一根)", res.directory)
        check(os.path.isfile(os.path.join(res.directory, "x.pdf")),
              "PDF 真的拷过去了")
        other = os.path.join(stick_a, U.USB_REPORT_DIRNAME)
        check(not os.path.exists(other), "另一根盘上没留东西", other)
    finally:
        U._preferred_paths = orig_pref


# ══════════════════════════════════════════════════════════
section("【4】sudo「不许执行这条命令」—— 别当成「板子坏了」")
# ══════════════════════════════════════════════════════════
def t4_sudo_not_allowed():
    msg = ("sudo: user aging is not allowed to execute "
           "'/mnt/aging/tools/bin/fio'")
    check(T.is_sudo_not_allowed_error(msg), "认得出「not allowed to execute」")
    check(not T.is_sudo_password_error(msg),
          "它跟「要密码」是**两回事**, 不能混")
    check(not T.is_sudo_not_allowed_error("温度 91°C 超过阈值 85°C"),
          "不相干的报错不误认")
    h = T.sudo_not_allowed_hint(msg)
    check("产线环境配置问题" in h, "文案写明是产线环境配置问题", h)
    check("not allowed to execute" in h, "带上了原始报错", h)
    check("sudoers" in h, "文案点名 sudoers", h)


def t4b_usb_mode_sudo_authorizes_system_path():
    """**U 盘模式下, sudo 被要求执行的命令永远是系统路径** ——
    盘上的 fio / glmark2 只是 `timeout` 的**参数**, 由 root 身份的 timeout 去 exec。

        sudo -n /usr/bin/timeout -k 5 60 /mnt/aging/tools/bin/fio --name=...
                       ^^^^^^^^^^^^^^^ sudo 授权的是这个; 盘上的 fio 只是参数

    ⇒ **产线的 sudoers 白名单一行都不用加**: 设备版和 U 盘版**被授权的
      那条命令完全一样** ⇒ 现有的白名单继续有效。
      (2026-11-05 实测: 两种模式下逐项比对, 被授权的命令一模一样。)

    ⚠️ 这是上面那个结论的**绊线**: 哪天有人把 `with_sudo()` 改成不包
      timeout(直接 `sudo -n <工具>`), 「sudoers 要覆盖盘上工具」这个前提
      就会回来 —— 这个测试会先红。
    """
    if T.is_root():
        print("    (以 root 跑 —— 免得开 sudo 就没白名单问题了, 跳过)")
        return

    def authorized(cmd):
        """sudo 真正被要求执行的那条命令(它后面的都只是参数)。"""
        c = list(cmd)
        if c and c[0] == "sudo":
            c = c[1:]
            if c and c[0] == "-n":
                c = c[1:]
            if c and c[0] == "env":        # 'env VAR=1 ... cmd' ⇒ 授权的是 cmd
                c = c[1:]
                while c and "=" in c[0]:
                    c = c[1:]
        return c[0] if c else ""

    tmp = mkdtemp(prefix="aging-sudopath-")
    bindir = os.path.join(tmp, "tools", "bin")
    os.makedirs(bindir)
    for name in ("fio", "glmark2-es2"):
        fake_bin(os.path.join(bindir, name), "#!/bin/sh\nexit 0\n")
    tool_dir = os.path.join(tmp, "tools")

    def build(dir_or_none):
        """按这个工具目录算出各测试项**实际**的 sudo 命令。"""
        old = os.environ.get(T.TOOL_DIR_ENV)
        try:
            if dir_or_none:
                os.environ[T.TOOL_DIR_ENV] = dir_or_none
            else:
                os.environ.pop(T.TOOL_DIR_ENV, None)
            fio = T.find_tool("fio")
            gm = T.find_tool("glmark2-es2")
            return {
                "eMMC/TF": T.with_sudo([fio, "--name=randwrite"], timeout_sec=60),
                "GPU": T.with_sudo([gm, "--fullscreen"],
                                   env={"DISPLAY": ":0"}, timeout_sec=615),
                "内存": T.with_sudo([T.find_tool("memtester") or "memtester",
                                     "64M", "1"], timeout_sec=60),
            }
        finally:
            if old is None:
                os.environ.pop(T.TOOL_DIR_ENV, None)
            else:
                os.environ[T.TOOL_DIR_ENV] = old

    dev = build(None)
    usb = build(tool_dir)

    # 前提先成立: U 盘模式下工具路径确实变了
    check(os.path.join(bindir, "fio") in usb["eMMC/TF"],
          "U 盘模式下 fio 确实从盘上取(前提成立)")

    for name in dev:
        check(authorized(dev[name]) == authorized(usb[name]),
              f"{name}: 两种模式**被授权的命令完全一样**",
              f"{authorized(dev[name])} vs {authorized(usb[name])}")

    check(authorized(usb["eMMC/TF"]).endswith("timeout"),
          "eMMC/TF: 被授权的是 timeout, 不是盘上的 fio",
          authorized(usb["eMMC/TF"]))
    check(authorized(usb["内存"]).endswith("timeout"),
          "内存: 被授权的是 timeout", authorized(usb["内存"]))
    check(authorized(usb["GPU"]).endswith("env") or
          authorized(usb["GPU"]).endswith("timeout"),
          "GPU: 被授权的是 env/timeout, 不是盘上的 glmark2",
          authorized(usb["GPU"]))
    for name in usb:
        check(not authorized(usb[name]).startswith(tool_dir),
              f"{name}: 被授权的命令**不在 U 盘上**", authorized(usb[name]))


# ══════════════════════════════════════════════════════════
section("【5】镜像打包的关键锚点 —— 单独的测试文件管")
# ══════════════════════════════════════════════════════════
#
# 静态锚点检查放在 tests/test_usb_image_anchors.py —— 那边要读
# scripts/build_usb_image.sh, 而本测试的**变异副本**里不需要那个文件
# (副本里少一个文件, 里面的测试就会假红 —— 见 tests/_mutate_usb_app_mode.py
# 的说明)。两边分开, 各自干净。
print("    (见 tests/test_usb_image_anchors.py)")


for _t in (t1_tool_dir, t2_start_sh_usb_mode, t3_report_follows_own_stick,
           t4_sudo_not_allowed, t4b_usb_mode_sudo_authorizes_system_path):
    _t()


# ══════════════════════════════════════════════════════════
print("\n" + "─" * 60)
if FAILS:
    print(f"✗ {len(FAILS)} 项失败 / 共 {PASSES + len(FAILS)} 项")
    for f in FAILS:
        print("   · " + f)
    sys.exit(1)
print(f"✓ 全部通过 ({PASSES} 项)")
sys.exit(0)
