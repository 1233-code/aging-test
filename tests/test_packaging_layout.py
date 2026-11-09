"""回归测试: 打包成 deb 的那些**布局不变式** (2026-10-29)。

产线要的是「这个应用连同测试工具打一个 deb, 拿到另一台板子上解包就能用」。
这句话拆开就是一堆硬性约束, 每一条都对应一个真实的翻车方式:

    ① **代码要在, 而且要全** —— 11 个测试项一个都不能少, 少了就是
       「装上了但那一项点开是空的」;
    ② **测试数据不能在** —— history.db / 报告 PDF / 日志是这台开发机的产线
       记录, 混进出货包 = 别的板子装上看到别人的历史;
    ③ **工具要随包带走** —— 装机时不能联网 (§11.7), 所以 stress-ng / fio /
       memtester / ... 17 个 .deb 必须躺在包里;
    ④ **chromium 不能碰** —— 出货镜像自带; Ubuntu 20.04 仓库里那个
       chromium-browser 是 48KB 的 snap 过渡包, 装上 PDF 报告会静默变空白
       (§8.2.1), 比没有还糟;
    ⑤ **数据和代码要分家** —— 代码装在 /usr/lib/aging-test (root 所有),
       跑测试的是普通用户, 写不进去。history.db / 报告 PDF / 日志必须落
       /var/lib/aging-test;
    ⑥ **校验闸要真的在** —— verify_deb.sh 得真的查那些禁入项, 不然它就
       是一张摆设。

跑法: .venv/bin/python -m tests.test_packaging_layout
"""
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

FAIL = []
CHECKS = 0


def check(cond, msg, detail=""):
    """记一条检查结果。"""
    global CHECKS
    CHECKS += 1
    if cond:
        print(f"  ✓ {msg}")
    else:
        print(f"  ✗ {msg}" + (f"\n      {detail}" if detail else ""))
        FAIL.append(msg)


def section(title):
    print(f"\n== {title} ==")


# ────────────────────────── 读打包脚本 ──────────────────────────

def read(rel):
    p = REPO / rel
    return p.read_text(encoding="utf-8") if p.is_file() else ""


BUILD = read("scripts/build_deb.sh")
VERIFY = read("scripts/verify_deb.sh")
CONTROL = read("deb/control")
CONFFILES = read("deb/conffiles")
POSTINST = read("deb/postinst")
PRERM = read("deb/prerm")
POSTRM = read("deb/postrm")


def bash_list(text, varname):
    """从 shell 脚本里抠出一个数组的内容 (TOOL_PKGS=( ... ))。

    容忍行尾注释 —— 清单里带注释是常态 (「这个包是给谁用的」), 抠的时候
    不能因为后面跟了 `# ...` 就把这一行漏掉。
    """
    m = re.search(rf"{varname}=\((.*?)\n\)", text, re.S)
    if not m:
        return []
    return re.findall(r"^\s*([A-Za-z0-9][A-Za-z0-9._+-]*)\s*(?:#.*)?$", m.group(1), re.M)


# ══════════════════════ ① 代码白名单 ══════════════════════
section("① 代码白名单 —— 该装的都得装上")

TEST_ITEMS = ["cpu", "gpu", "wifi", "bluetooth", "usb", "ethernet", "hdmi",
              "fan", "tfcard", "memory", "emmc"]

# 11 个测试项的实现文件必须都在 server/tests/ 下, 且打包时整个 server/ 目录
# 都被搬进包 —— 少一个文件, 那一项在产线上点开就是空的
for item in TEST_ITEMS:
    f = REPO / "server" / "tests" / f"{item}.py"
    check(f.is_file(), f"测试项实现存在: server/tests/{item}.py")

check("copy_tree" in BUILD and '"$PROJECT_ROOT/server"' in BUILD,
      "打包脚本搬了整个 server/ 目录 (含 server/tests/ 的 11 个测试项)")
check('"$PROJECT_ROOT/static"' in BUILD,
      "打包脚本搬了整个 static/ 目录 (UI 三件套)")

# ⚠️ **每加一个新模块, 出包闸的「必入清单」就得跟着加** —— 少了它, 闸会放一个
#    「装完起不来 / 点开是空的」的包出去(§11.7)。蓝牙是第 11 项(2026-11-04),
#    之前清单里只有 10 个 ⇒ 闸根本不看 bluetooth.py 有没有进包。
#    这条检查对着**源码**查(不依赖 dist/ 里有没有现成的包), 所以重打包之前
#    也能守住。
_must_have = re.search(r'for t in ([^;]+); do\s*\n\s*need "usr/lib/aging-test/server/tests/\$t\.py"',
                        VERIFY, re.S)
check(_must_have is not None, "出包闸的必入清单是一个逐项点名的循环")
if _must_have:
    _listed = re.findall(r"[A-Za-z0-9]+", _must_have.group(1))
    _missing = [t for t in TEST_ITEMS if t not in _listed]
    check(not _missing, "出包闸的必入清单点名了全部 11 个测试项", _missing)
    check(len(_listed) == len(TEST_ITEMS),
          "必入清单里没有多出来的项(不多不少 11 个)", _listed)

# 基础设施文件: 少一个就起不来
for rel in ["server/main.py", "server/config.py", "server/db.py",
            "server/report.py", "server/runner.py", "server/identity.py",
            "server/templates/report.html.j2",
            "static/index.html", "static/app.js", "static/style.css",
            "start.sh", "config.yaml", "requirements.txt",
            "scripts/check_deps.sh"]:
    check((REPO / rel).is_file(), f"运行期需要的文件在仓里: {rel}")

# 这些文件必须真的被 copy 进包 (逐个在打包脚本里找对应的复制动作)
for needle, desc in [
    ('"$PROJECT_ROOT/config.yaml"', "config.yaml 被复制"),
    ('"$PROJECT_ROOT/requirements.txt"', "requirements.txt 被复制"),
    ('"$PROJECT_ROOT/start.sh"', "start.sh 被复制"),
    ("check_deps.sh", "check_deps.sh 被复制 (postinst 要调它自检)"),
]:
    check(needle in BUILD, desc)

# ══════════════════════ ② 禁入清单 ══════════════════════
section("② 禁入清单 —— 不该装的绝不能装")

# 打包脚本只搬 server/ static/ + 三个脚本 + 四个单文件, 源码树根下的
# reports/ logs/ docs/ tests/ .venv/ 一个都没被搬
for junk in ["reports", "logs", "docs"]:
    check(f'copy_tree "$PROJECT_ROOT/{junk}"' not in BUILD,
          f"打包脚本没有搬 {junk}/")
check('"$PROJECT_ROOT/tests"' not in BUILD and '"$PROJECT_ROOT/.venv"' not in BUILD,
      "打包脚本没有搬根目录的 tests/ 与 .venv/")

# 仓库里真的存在这些"垃圾"目录 —— 也就是说"不搬"这件事是真有意义的,
# 而不是"反正不存在"
for junk in ["reports", "logs", "docs", "tests", ".venv"]:
    check((REPO / junk).exists(), f"仓库里确实有 {junk}/ (所以禁入是真问题)")

# verify_deb.sh 必须真的查这些
for guard in ["history.db", "reports/pdf/", "*.pdf", "__pycache__",
              r"\.pyc$", "imageio_ffmpeg", "chromium",
              "probe_rp_gpio_write"]:
    check(guard in VERIFY, f"校验脚本盯着: {guard}")

# ══════════════════════ ③ 工具离线仓 ══════════════════════
section("③ 工具离线仓 —— 装机时不许联网")

tools = bash_list(BUILD, "TOOL_PKGS")
EXPECT = ["stress-ng", "fio", "memtester", "ethtool", "iw", "rfkill",
          "busybox", "util-linux", "iproute2", "usbutils", "iputils-ping",
          "x11-xserver-utils", "xdg-utils", "fonts-wqy-zenhei",
          "glmark2-es2", "glmark2-es2-wayland", "glmark2-data"]
check(len(tools) == len(EXPECT), f"工具清单条数对 ({len(EXPECT)} 个)",
      f"实际: {tools}")
for t in EXPECT:
    check(t in tools, f"工具清单里有: {t}")
check(not any("chromium" in t for t in tools),
      "工具清单里没有 chromium (出货镜像自带, 仓库里那个是 snap 空壳)")

# ── 运行时共享库 ──
# 工具是 dpkg-deb -x 解过去的, **不走 dpkg 依赖解析** ⇒「工具 NEEDED 的那些
# .so 在目标机上有没有」没人替我们保证。fio 就是这么翻车的: 它 ELF 里 NEEDED
# librbd.so.1, 而 librbd1/librados2/libtirpc3/... 全是开发期 apt 装 fio 时
# 才拖进来的, 出货镜像里没有 → 干净板子上 fio 跑不起来, eMMC/TF卡/USB 全废。
runtime = bash_list(BUILD, "RUNTIME_PKGS")
EXPECT_RUNTIME = ["librbd1", "librados2", "libtirpc3",
                  "libboost-iostreams1.71.0", "libgfapi0", "libgfrpc0",
                  "libgfxdr0", "libglusterfs0", "librdmacm1", "libatm1"]
check(len(runtime) == len(EXPECT_RUNTIME),
      f"运行库清单条数对 ({len(EXPECT_RUNTIME)} 个)", f"实际: {runtime}")
for t in EXPECT_RUNTIME:
    check(t in runtime, f"运行库清单里有: {t}")

# 核心库绝对不能随包带 —— 解过去会覆盖目标机的 libc/stdc++
for core in ["libc6", "libstdc++6", "libgcc-s1", "zlib1g", "libcrypt1"]:
    check(core not in runtime, f"运行库清单里没有核心库 {core}")

# python2.7 那一串是 fio 的**包级** Depends (给 librbd1 的 python 绑定用),
# 运行时不需要 —— 别把第二个 Python 塞进产线板子
check(not any("python2" in t for t in runtime),
      "运行库清单里没有 python2.7 那一串 (运行时不需要)")
# libtirpc-common 只带一个 /etc/netconfig —— 解过去会覆盖目标机配置
check("libtirpc-common" not in runtime,
      "运行库清单里没有 libtirpc-common (只带 /etc/netconfig, 会覆盖目标机配置)")

# 闭包闸: 打包时必须真的跑, 而不是摆在那儿
check("check_runtime_closure.py" in BUILD,
      "打包脚本真的调了运行时闭包闸")
check("check_runtime_closure.py" in read("deb/check_runtime_closure.py")
      or (REPO / "deb" / "check_runtime_closure.py").is_file(),
      "deb/check_runtime_closure.py 在仓里")
check((REPO / "deb" / "image-baseline.txt").is_file(),
      "出货镜像基线 (deb/image-baseline.txt) 在仓里 —— 闭包闸靠它判断「目标机上本来有啥」")
BASELINE = read("deb/image-baseline.txt")
base_pkgs = [ln.strip() for ln in BASELINE.splitlines()
             if ln.strip() and not ln.strip().startswith("#")]
base_norm = {p.split(":")[0] for p in base_pkgs}
for must in ["libc6", "busybox", "iw", "x11-xserver-utils", "iproute2"]:
    check(must in base_norm, f"镜像基线里有: {must}")
# fio / glmark2 那一串是开发期才装的 ⇒ **不该**在镜像基线里, 必须随包带走
for dev_only in ["fio", "glmark2-es2", "glmark2-es2-wayland", "glmark2-data",
                 "librbd1", "librados2", "libtirpc3", "librdmacm1",
                 "libgfapi0", "libgfrpc0", "libgfxdr0", "libglusterfs0",
                 "libatm1", "libboost-iostreams1.71.0"]:
    check(dev_only not in base_norm,
          f"镜像基线里没有 {dev_only} (它是开发期才装的 ⇒ 必须随包带走)")

# 每个工具都得在 check_deps.sh 的关键清单里被认到 (不然装完还是判缺件)
CHECK_DEPS = read("scripts/check_deps.sh")
for cmd, pkg in [("stress-ng", "stress-ng"), ("fio", "fio"), ("memtester", "memtester"),
                 ("ethtool", "ethtool"), ("iw", "iw"), ("rfkill", "rfkill"),
                 ("udhcpc", "busybox"), ("ip", "iproute2"), ("ping", "iputils-ping"),
                 ("lsusb", "usbutils"), ("lsblk", "util-linux"), ("xrandr", "x11-xserver-utils")]:
    check(f'["{cmd}"]' in CHECK_DEPS, f"check_deps 认得 {cmd} (来自 {pkg})")

# ── 剥掉注释行, 免得「注释里提到」被当成「代码里真用了」 ──
POSTINST_CODE = "\n".join(ln for ln in POSTINST.splitlines()
                          if not ln.strip().startswith("#"))
POSTRM_CODE = "\n".join(ln for ln in POSTRM.splitlines()
                         if not ln.strip().startswith("#"))

# postinst 必须真的按"补缺不降级"装工具
check("dpkg-deb --fsys-tarfile" in POSTINST,
      "postinst 用 dpkg-deb --fsys-tarfile 解包 (不嵌套 dpkg, 不撞库锁)")
check("compare-versions" in POSTINST, "postinst 比版本 (补缺不降级)")
check("no-index" in POSTINST, "postinst 用 pip --no-index 离线装 (不联网)")

# ── 解包不许踩坏 usrmerge 的软链 (2026-10-29 新机装机失败) ──
# `dpkg-deb -x` 是裸 tar: 遇到包里的 ./lib/ 这种**目录成员**, 会把
# /lib 这个软链换成真目录 ⇒ 动态链接器消失 ⇒ 板子上所有动态链接的程序全死。
# 必须用 `tar --keep-directory-symlink`, 并且装完只读核一遍软链。
check("--keep-directory-symlink" in POSTINST,
      "postinst 解包用 tar --keep-directory-symlink (保住 usrmerge 的 /lib 软链)")
check(not re.search(r'dpkg-deb\s+-x\s+["\']?\$\{?deb', POSTINST_CODE),
      "postinst 代码里**没有** dpkg-deb -x 调用 (那会把软链踩坏)")
check("check_usrmerge_links" in POSTINST,
      "postinst 装完会只读核一遍 usrmerge 软链 (坏了就停手)")
check("rm -rf" not in POSTINST_CODE,
      "postinst 代码里没有 rm -rf —— 脚本不擅自删改 /lib /bin")
check((REPO / "deb" / "check_extract_safety.py").is_file(),
      "解包安全性闸在仓里: deb/check_extract_safety.py")
check("check_extract_safety.py" in BUILD,
      "build_deb.sh 会跑解包安全性闸 (别让会踩坏软链的包出包)")
# 清单里必须记**真实落点** —— usrmerge 板子上 ./lib/xxx ⇒ /usr/lib/xxx,
# 不然 postrm 的白名单一个都匹配不上 (那些文件就永远收不回来)
check("real_path" in POSTINST,
      "postinst 记清单时把路径翻成真实落点 (usrmerge 板子上 /lib ⇒ /usr/lib)")

# ── purge 时的归属判据 (收错了 = 把产线板子拆了) ──
# postrm 要把自己铺出去的文件收回去。判据必须能**证明**「这份是我们铺的」,
# 不能靠"看着像"。
check("fingerprint" in POSTINST and "sha256sum" in POSTINST,
      "postinst 装机时记文件指纹 (留证据)")
check("aging-test-self" in POSTINST,
      "postinst 把自己造的软链单独标记 (不属于任何包)")
check("dpkg-query -W" in POSTRM,
      "postrm 用 dpkg-query -W 查状态库 (可靠的那个判据)")
check("dpkg -S" not in POSTRM_CODE,
      "postrm 代码里**没用 dpkg -S** 查归属 (它在 usrmerge 板子上是半废的)")
check("sha256sum" in POSTRM and "fingerprint" in POSTRM,
      "postrm 卸载时比对指纹 (证明文件没被换过)")
check("ok installed" in POSTRM,
      "postrm 认「包是正式装着的」⇒ 不删")
check("DESTDIR" in POSTRM,
      "postrm 落点带 DESTDIR 前缀 —— 能在沙箱里真跑一遍 (见 test_postrm_purge.py)")
check("/etc/*" not in POSTRM_CODE,
      "postrm 的白名单里没有 /etc (配置文件不归我们管)")

# ══════════════════════ ④ Debian 元数据 ══════════════════════
section("④ Debian 元数据")

check(re.search(r"^Package:\s*aging-test", CONTROL, re.M), "control 里有包名")
check(re.search(r"^Architecture:\s*arm64", CONTROL, re.M), "control 里有架构")
deps = re.search(r"^Depends:\s*(.+)$", CONTROL, re.M)
deps = deps.group(1) if deps else ""
for d in ["python3", "python3-venv", "python3-pip"]:
    check(d in deps, f"Depends 里有 {d}")
check("chromium" not in deps, "Depends 里没有 chromium")
# 工具**故意不在** Depends 里 —— 依赖解析要联网, 且会顺手降级目标机已有的包
check("stress-ng" not in deps and "fio" not in deps,
      "Depends 里没有工具包 (走随包离线仓, 不走 apt 依赖解析)")

check("/etc/aging-test/config.yaml" in CONFFILES, "conffiles 里有配置文件")
check("/etc/default/aging-test" in CONFFILES, "conffiles 里有默认值文件")

for name, body in [("postinst", POSTINST), ("prerm", PRERM), ("postrm", POSTRM)]:
    check(body.strip() != "", f"deb/{name} 不是空文件")
    check(body.startswith("#!"), f"deb/{name} 有 shebang")

# ══════════════════════ ⑤ 数据与代码分家 ══════════════════════
section("⑤ 数据与代码分家 (写路径不许落在代码根)")

# 真正跑一遍: 把数据根指到临时目录, 看落点对不对
# ⚠️ 必须指到临时目录 —— 生产库的 history.db 和 reports/pdf/ 是产线在用的
#    真实数据, 测试绝不能碰 (见 test_skip_next.py 里同一套做法)。
with tempfile.TemporaryDirectory(prefix="aging-pkg-") as tmp:
    os.environ["AGING_DATA_DIR"] = tmp
    for mod in [m for m in list(sys.modules) if m.startswith("server")]:
        del sys.modules[mod]

    from server import db as DB
    from server import report as REPORT
    from server.config import Config

    data_root = Config.data_root()
    check(str(data_root) == tmp, "data_root() 跟着 AGING_DATA_DIR 走",
          f"实际: {data_root}")
    check(str(DB.DB_PATH).startswith(tmp),
          "history.db 落在数据根下", f"实际: {DB.DB_PATH}")
    out = REPORT.output_dir()
    check(str(out).startswith(tmp),
          "报告输出目录挂在数据根下 (不是代码根)", f"实际: {out}")

    # 代码根写不动时必须落到 /var/lib/aging-test
    os.environ.pop("AGING_DATA_DIR", None)
    import server.config as C
    orig = C.PROJECT_ROOT
    try:
        C.PROJECT_ROOT = Path("/nonexistent-readonly-root")
        C.SYSTEM_DATA_PATH = Path(tmp) / "fallback"
        check(str(C.Config.data_root()).startswith(str(tmp)),
              "代码根写不动时回落到系统数据目录",
              f"实际: {C.Config.data_root()}")
    finally:
        C.PROJECT_ROOT = orig

# 代码里不许有硬写的 /var/lib 之外的落点
for f in ["server/db.py", "server/report.py", "server/main.py"]:
    text = read(f)
    check("data_root()" in text or "Config.data_root" in text,
          f"{f} 的写路径挂在 data_root() 上")

# ══════════════════════ ⑥ 自检的两种模式 ══════════════════════
section("⑥ check_deps.sh 的两种模式")

check("--packages-only" in CHECK_DEPS, "check_deps.sh 支持 --packages-only")
r = subprocess.run(["bash", str(REPO / "scripts" / "check_deps.sh"), "--packages-only"],
                   capture_output=True, text=True, timeout=120)
check(r.returncode in (0, 1), "--packages-only 模式能跑完 (不崩)")
check("非包类逐台自检 —— 跳过" in r.stdout,
      "--packages-only 确实跳过了逐台自检 (装机时可能连 X 都没有)")
check("① 包类·关键工具" in r.stdout, "--packages-only 仍然查关键工具")

# ══════════════════════ ⑦ 已打出的包 ══════════════════════
section("⑦ 已打出的包 (有就校一遍)")

debs = sorted((REPO / "dist").glob("aging-test_*.deb"),
              key=lambda p: p.stat().st_mtime) if (REPO / "dist").is_dir() else []
if not debs:
    print("  – 没有 dist/*.deb, 跳过 (跑 scripts/build_deb.sh 之后再回来看)")
else:
    # ⚠️ 按**文件时间**取最新的那个 —— 按名字排序会取到 `-9`(字符串排序:
    #    "-10" < "-1_" < "-2" … "-9")⇒ 拿到的根本不是最新的包。
    deb = debs[-1]

    # 这个包是不是比源码旧? —— 旧的话内容闸验的是**旧代码**, 结论对不上
    # 现在的代码 ⇒ 内容闸跳过(下面「包里没有产线数据」照查, 那说的是包本身)。
    _packaged = ["server", "static", "scripts/check_deb.sh", "config.yaml",
                 "start.sh", "requirements.txt"]
    _src_ts = []
    for rel in _packaged:
        p = REPO / rel
        if p.is_file():
            _src_ts.append(p.stat().st_mtime)
        elif p.is_dir():
            _src_ts += [f.stat().st_mtime for f in p.rglob("*") if f.is_file()]
    _src_newest = max(_src_ts) if _src_ts else 0
    stale = deb.stat().st_mtime < _src_newest

    if stale:
        print("  – ⚠ dist 里最新的包(%s, %s)比源码还旧 ⇒ **内容闸跳过**: "
              "它验的是旧代码, 不是现在这份" % (
                  deb.name,
                  time.strftime("%Y-%m-%d %H:%M", time.localtime(deb.stat().st_mtime))))
        print("    (重新打一个: bash scripts/build_deb.sh --offline —— 打完这条才会真的校)")
    else:
        r = subprocess.run(["bash", str(REPO / "scripts" / "verify_deb.sh"), str(deb)],
                           capture_output=True, text=True, timeout=300)
        check(r.returncode == 0, f"包过得了 verify_deb.sh: {deb.name}",
              r.stdout[-1500:])
    # 包里绝不能有产线数据
    listing = subprocess.run(["dpkg-deb", "-c", str(deb)],
                             capture_output=True, text=True, timeout=120).stdout
    for bad in ["history.db", ".pdf", "__pycache__", "probe_rp_gpio_write"]:
        check(bad not in listing, f"包里没有 {bad}")

# ══════════════════════ 汇总 ══════════════════════
print(f"\n{'=' * 60}")
if FAIL:
    print(f"✗ {len(FAIL)}/{CHECKS} 项没过:")
    for f in FAIL:
        print(f"    - {f}")
    sys.exit(1)
print(f"✓ 全部通过 ({CHECKS} 项)")
