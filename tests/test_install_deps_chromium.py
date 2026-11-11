"""回归测试: `scripts/install_deps.sh` **不许 apt 装 chromium**(2026-11-11 查出来的)。

起因:
    出厂镜像**自带** chromium 112(focal 的 `chromium-browser` 只是个
    **snap 过渡空壳**, 48KB 的壳)。但 `install_deps.sh` 以前把
    `"chromium-browser"` 列在 `CRITICAL_PKGS` 里 ⇒ 工人照手册跑这个脚本时
    会执行 `apt-get install -y chromium-browser`
    ⇒ apt 把镜像里那份**能用的** chromium 112 换成 snap 过渡包(1:85)
    ⇒ **PDF 报告静默变成空白** —— 不报错, 只是没内容 ⇒
    产线以为「报告导出坏了」, 实际是自己把浏览器换没了。

    同一个坑 `scripts/build_deb.sh` 早就设了闸门(清单里出现 chromium 就
    `die`)⇒ 这边是**同一类坑的另一扇门**。

这个测试守四件事(全是**静态**检查, 不跑 apt、不碰网络):
  ① `CRITICAL_PKGS` 里**没有** chromium / chromium-browser;
  ② 整个脚本里**没有**「apt 装 chromium」的动作(任何形式);
  ③ 脚本里**有**一段「只查不装」的自检, 且**说明**了为什么不能装
     —— 光删掉不解释, 下一个人还会把它加回来;
  ④ `build_deb.sh` 那道闸门**还在**(别把已有的防护改没了)。

跑法: .venv/bin/python -m tests.test_install_deps_chromium
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTALL_REL = os.path.join("scripts", "install_deps.sh")
BUILD_REL = os.path.join("scripts", "build_deb.sh")
# 变异脚本会把**改坏的那一份**指过来(AGING_INSTALL_DEPS_SH)。
INSTALL_PATH = os.environ.get("AGING_INSTALL_DEPS_SH",
                               os.path.join(REPO, INSTALL_REL))

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


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def critical_pkgs(src):
    """从 CRITICAL_PKGS=( ... ) 数组里把包名抠出来。

    ⚠️ **注释行要先剥掉**: 数组里那段解释「为什么不能装 chromium」的注释
       本身就写着 `"chromium-browser"` ⇒ 不剥的话会把**注释**当成包名,
       报一个假的「清单里有 chromium」。
    """
    m = re.search(r"CRITICAL_PKGS=\((.*?)\n\)", src, re.S)
    if not m:
        return None
    body = "\n".join(ln for ln in m.group(1).splitlines()
                     if not ln.strip().startswith("#"))
    return re.findall(r'"([^"]+)"', body)


def main():
    src = read(INSTALL_PATH)

    print("① CRITICAL_PKGS 里不许有 chromium")
    pkgs = critical_pkgs(src)
    check(pkgs is not None, "找得到 CRITICAL_PKGS 数组", src[:200])
    if pkgs is None:
        pkgs = []
    bad = [p for p in pkgs if "chromium" in p or "chrome" in p]
    check(not bad,
          "关键包清单里没有 chromium 系的包",
          "装了会换掉镜像自带的 chromium 112 ⇒ PDF 报告静默变空白: %s" % bad)
    check("fonts-wqy-zenhei" in pkgs,
          "但**字体**还在(它是 PDF 里中文显示的前提, 缺了也是空白报告)",
          pkgs)

    print("② 整个脚本不许出现「apt 装 chromium」")
    # 把注释行先剥掉 —— 注释里可以解释, 但**代码**里不许装
    code_lines = []
    for ln in src.splitlines():
        s = ln.strip()
        if s.startswith("#"):
            continue
        code_lines.append(ln)
    code = "\n".join(code_lines)
    apt_lines = [ln for ln in code_lines
                 if re.search(r"apt(-get)?\s+(install|.*install)", ln)
                 and "chrom" in ln]
    check(not apt_lines,
          "没有任何一行会 apt 装 chromium",
          "\n".join(apt_lines))

    print("③ 有一段「只查不装」的自检, 而且说明了原因")
    check("chrom" in src, "脚本里提到了 chromium")
    check("command -v" in src and re.search(r"for c in .*chrom", src),
          "用 command -v 找 chromium(而不是 dpkg-query —— 包名和命令名对不上)")
    check("(只查, 不装)" in src,
          "标题里明说了「只查, 不装」(否则下一个人会顺手加回去)")
    check("别再 apt 装 chromium-browser" in src,
          "找到了 chromium 时**特意提醒**别再 apt 装(解释得具体才拦得住)")
    check("snap" in src,
          "说明了 focal 的 chromium-browser 是 snap 空壳(为什么不能装)")
    check("空白" in src,
          "说明了后果是「PDF 报告静默变空白」(工人看得懂为什么要小心)")

    print("④ build_deb.sh 那道闸门还在")
    try:
        bsrc = read(os.path.join(REPO, BUILD_REL))
    except OSError as e:
        bsrc = ""
        check(False, "读得到 build_deb.sh", e)
    check(bool(bsrc) and re.search(r"chromium\*?\)?", bsrc) is not None
          and "die" in bsrc,
          "build_deb.sh 里「清单出现 chromium 就 die」的闸门还在")

    print("\n" + "─" * 60)
    if FAILS:
        print("✗ %d 项失败 / 共 %d 项" % (len(FAILS), PASSES + len(FAILS)))
        for f in FAILS:
            print("   · " + f)
        return 1
    print("✓ 全部通过 (%d 项)" % PASSES)
    return 0


if __name__ == "__main__":
    sys.exit(main())
