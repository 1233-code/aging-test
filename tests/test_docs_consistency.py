"""回归测试: README / postinst 里写的**事实**得跟代码对得上(2026-11-11 查出来的)。

起因(审查查出来的三处文档与代码打架):
  ① README 说 USB 项是 `fio` 「**写30s + 读30s**」, 而 `server/tests/usb.py`
     里就是 `--rw=write`(§6.5 也写的是「**只写不读****)⇒ 文档把没做的事
     说成做了 ⇒ 看文档的人以为有读测试。
  ② README 把 `chromium-browser` 列成「关键工具」, 而它恰恰是**不能装**的那个
     (focal 上是 snap 空壳, 装了会换掉镜像自带的 chromium 112 ⇒
     PDF 报告静默变空白)⇒ 照着 README 装 = 自己把浏览器换没了。
  ③ `deb/postinst` 写「16 个工具 .deb」, 而 `TOOL_PKGS` 已经是 **17** 个
     ⇒ 写死的数字一漂就成假话。

这个测试守四件事:
  ① README 的 USB 项说「只写」;
  ② README 不许把 chromium-browser 当成「要装的工具」, 且**必须**说清不许装;
  ③ README 的 `scripts/` 清单**盖得住**仓库里真实存在的脚本
     (漏写的脚本 = 工人根本不知道它存在);
  ④ 文档里写死的**包个数**跟 `build_deb.sh` 的清单一致。

跑法: .venv/bin/python -m tests.test_docs_consistency
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 变异脚本会把**改坏的那一份**指过来(AGING_README / AGING_POSTINST) ——
# 变异脚本**不许**原地改仓库里的 README ⇒ 只能靠副本 + 环境变量指过去。
README = os.environ.get("AGING_README", os.path.join(REPO, "README.md"))
POSTINST = os.environ.get("AGING_POSTINST", os.path.join(REPO, "deb", "postinst"))
BUILD = os.path.join(REPO, "scripts", "build_deb.sh")
USB_TEST = os.path.join(REPO, "server", "tests", "usb.py")

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


def scripts_block(readme):
    """README 里 `├── scripts/` 那一段树(到下一个顶层 `├── ` 为止)。"""
    lines = readme.splitlines()
    out, on = [], False
    for ln in lines:
        if not on:
            if re.match(r"^├── scripts/", ln):
                on = True
            continue
        if re.match(r"^├── |^└── ", ln):
            break
        out.append(ln)
    return "\n".join(out)


def pkg_list(src, name):
    """读 build_deb.sh 里那个数组的内容(行尾注释也要剥掉)。"""
    m = re.search(name + r"=\((.*?)\n\)", src, re.S)
    if not m:
        return None
    names = []
    for ln in m.group(1).splitlines():
        s = ln.split("#", 1)[0].strip()
        if not s:
            continue
        s = s.strip('"')
        if s:
            names.append(s)
    return names


def main():
    readme = read(README)
    build = read(BUILD)

    print("① README 的 USB 项得说「只写」")
    usb_row = [ln for ln in readme.splitlines()
               if ln.startswith("| 5 |")]
    check(len(usb_row) == 1, "找得到 README 表格里的第 5 行(USB)", usb_row)
    row = usb_row[0] if usb_row else ""
    check("读30s" not in row and "读写都测" not in row,
          "README 不再说 USB 有「读30s」", row)
    check("只写" in row, "README 说清楚了是「只写」", row)
    # 代码那边确实是只写
    usrc = read(USB_TEST)
    check("--rw=write" in usrc, "代码里 USB 确实是 --rw=write(自检)")
    check("--rw=read" not in usrc and "--rw=randread" not in usrc,
          "代码里 USB 没有读模式(自检)")

    print("② README 不许把 chromium-browser 当成要装的工具")
    # 只看「关键工具」那一行(README 里唯一列工具清单的地方)
    tool_lines = [ln for ln in readme.splitlines() if ln.startswith("关键工具:")]
    check(len(tool_lines) == 1, "找得到 README 的「关键工具」那行", tool_lines)
    tline = tool_lines[0] if tool_lines else ""
    check("chromium-browser" not in tline,
          "「关键工具」清单里没有 chromium-browser", tline[:200])
    check("不许 apt 装" in readme or "不要 apt 装" in readme
          or "不许装" in readme,
          "README 里明说了 chromium 不许 apt 装")
    check("snap" in readme and "空白" in readme,
          "README 说了原因(snap 空壳) 和后果(PDF 报告静默变空白)")

    print("③ README 的 scripts/ 清单要盖得住真实文件")
    block = scripts_block(readme)
    check(bool(block), "找得到 README 的 scripts/ 段")
    listed = set(re.findall(r"([A-Za-z0-9_.\-]+\.(?:sh|py|c|tpl))", block))
    real = set()
    for fn in sorted(os.listdir(os.path.join(REPO, "scripts"))):
        if fn == "__pycache__" or fn.startswith("."):
            continue
        if fn.endswith((".sh", ".py", ".c", ".tpl")):
            real.add(fn)
    missing = sorted(real - listed)
    check(not missing,
          "仓库里的每个脚本都在 README 的清单里(漏写 = 工人不知道它存在)",
          missing)

    print("④ 写死的包个数要跟清单一致")
    tools = pkg_list(build, "TOOL_PKGS")
    runtimes = pkg_list(build, "RUNTIME_PKGS")
    check(tools is not None and runtimes is not None,
          "读得到 build_deb.sh 的两个清单")
    n_tools = len(tools or [])
    n_rt = len(runtimes or [])
    m = re.search(r"\*\*(\d+) 个 \.deb\*\*\((\d+) 个工具 \+ (\d+) 个运行时共享库\)",
                  readme)
    check(m is not None, "README 里那行「N 个 .deb(M 个工具 + K 个运行时)」在",
          "找不到这行")
    if m:
        check(int(m.group(2)) == n_tools,
              "README 写的工具个数 == TOOL_PKGS 的长度",
              "README %s vs 清单 %d" % (m.group(2), n_tools))
        check(int(m.group(3)) == n_rt,
              "README 写的运行时库个数 == RUNTIME_PKGS 的长度",
              "README %s vs 清单 %d" % (m.group(3), n_rt))
        check(int(m.group(1)) == n_tools + n_rt,
              "总数 = 工具 + 运行时",
              "%s vs %d" % (m.group(1), n_tools + n_rt))
    # postinst 里不许再写死个数
    # ⚠️ 判据只认「数字 + 个 + (工具|包|运行时库|.deb)」这种**写法**;
    #    注释里「授权当时是 16 个, 后来加了 …⇒ 现在 17 个」这种**说明**
    #    不算写死(它后面跟的是逗号, 不是「个包」)。
    pinst = read(POSTINST)
    hard = re.findall(r"(\d+) 个(?:工具|包|运行时共享库|运行时库|\.deb)", pinst)
    check(not hard,
          "postinst 里没有写死「N 个工具/包」(写死就会跟清单漂)",
          hard)

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
