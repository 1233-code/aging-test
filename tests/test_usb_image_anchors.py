"""静态锚点检查: 「U 盘版应用」的镜像构建脚本里, 那些**不能丢的关键行**。

镜像构建脚本 (scripts/build_usb_image.sh) 里有几处是**踩过坑才写出来的**,
删掉一处就出事, 而且出事得很晚(在板子上才炸):

  ① wrapper 在 exec 真二进制**之前**把 LD_LIBRARY_PATH 指好
     —— 那 10 个运行时共享库在 U 盘上, 不在系统路径里; 不指就是
        「error while loading shared libraries」(fio 就是这么翻车的)。
  ② glmark2 显式 `--data-path` —— 编译进去的默认路径是 /usr/share/glmark2,
     设备上根本没那个目录; 不指 GPU 项起不来。
  ③ 调用方自己给了 `--data-path` 就不许再加一遍(加了 glmark2 会报错)。
  ④ 核心库守卫 —— LD_LIBRARY_PATH 排在系统路径**前面**, 盘上那份会盖住
     系统那份; 核心库( libc / libstdc++ / ld-linux )搬上来 = 拿整台机器赌。
  ⑤ 自检可以关掉(`--no-self-check`) —— 构建时**不该**执行刚解出来的二进制。

⚠️ **锚点必须唯一** —— 不唯一的话, 靠 replace 做的变异会一次改掉好几处,
   证据就不成立(这个坑踩过)。所以这里查的是「出现且**只**出现一次」。

⚠️ 跟镜像打包有关的**动态**验证在构建脚本自己的 §⑩ 自检里 —— 在板子上
   构建时真跑一遍 fio/glmark2。本文件只做静态检查, 不执行任何东西。

跑法: .venv/bin/python -m tests.test_usb_image_anchors
"""
import os
import shlex
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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


#: (文件, 说明, 锚点原文)
ANCHORS = [
    ("scripts/build_usb_image.sh", "wrapper 从**唯一模板**生成",
     'WRAPPER_TPL="$PROJECT_ROOT/scripts/usb_wrapper.sh.tpl"'),
    ("scripts/build_usb_image.sh", "模板里没替换掉的占位符 = 停手",
     'die "wrapper 里有没替换掉的占位符: $TOOLS/bin/$name"'),
    ("scripts/usb_wrapper.sh.tpl", "wrapper 在 exec 前设 LD_LIBRARY_PATH",
     'LD_LIBRARY_PATH="$_libs${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"'),
    ("scripts/usb_wrapper.sh.tpl",
     "wrapper 把 sysroot 的库目录加进 LD_LIBRARY_PATH",
     'for _d in "$_sysroot"/usr/lib/*/ "$_sysroot"/usr/lib/ '
     '"$_sysroot"/lib/*/ "$_sysroot"/lib/ \\'),
    ("scripts/usb_wrapper.sh.tpl",
     "wrapper 的搜索路径要带上**子目录**(2026-11-06: libceph-common.so.2 "
     "住在 usr/lib/aarch64-linux-gnu/ceph/ 里, 少这一层 ⇒ fio 在别的板子"
     "上 rc=127「起不来」⇒ 存储项 error ⇒ 好板子被返修)",
     '"$_sysroot"/usr/lib/*/*/ "$_sysroot"/lib/*/*/; do'),
    ("scripts/usb_wrapper.sh.tpl", "glmark2 显式指 --data-path",
     'set -- --data-path "$_root/share/glmark2" "$@"'),
    ("scripts/usb_wrapper.sh.tpl", "调用方自己给了 --data-path 就不重复加",
     '--data-path|-d) _has_data=1; break ;;'),
    ("scripts/build_usb_image.sh", "核心库守卫: libc + libstdc++ 一起查",
     'libc.so.6 "$SYSROOT"/usr/lib/*/libstdc++.so.6'),
    ("scripts/build_usb_image.sh", "核心库守卫: ld-linux",
     '"$SYSROOT"/usr/lib/*/ld-linux-aarch64.so.1'),
    ("scripts/build_usb_image.sh",
     "核心库守卫也要扫**子目录**(有的包把库放子目录里, 只扫顶层会漏)",
     '"$SYSROOT"/usr/lib/*/*/libc.so.6'),
    ("scripts/build_usb_image.sh",
     "构建时要跑「换台板子还能用吗」的静态检查(2026-11-06 加)",
     'check_usb_selfcontained.py" "$TOOLS"'),
    ("scripts/build_usb_image.sh", "核心库守卫不许混进镜像",
     "⇒ LD_LIBRARY_PATH 会拿盘上那份盖住系统的 ⇒ 不能出这个镜像。"),
    ("scripts/build_usb_image.sh", "写 U 盘模式标记",
     'cat > "$OUT_DIR/AGING_USB_MODE" <<EOF'),
    ("scripts/build_usb_image.sh", "自检可以关掉(不执行解出来的二进制)",
     'if [ "$SELF_CHECK" = "0" ]; then'),
    ("scripts/build_usb_image.sh", "镜像必须在 aarch64 上打(wheels 是 cp38)",
     'if [ "$ARCH" != "aarch64" ]; then'),
    ("scripts/build_usb_image.sh", "核心库守卫找不到就停手",
     'die "sysroot 里有核心库:$core_bad'),
    ("scripts/build_usb_image.sh", "输出目录不许非空(不 rm -rf)",
     'die "输出目录不是空的: $OUT_DIR'),
    ("start.sh", "U 盘模式把数据根指到 U 盘",
     'export AGING_DATA_DIR="$SCRIPT_DIR"'),
    ("start.sh", "U 盘模式把工具目录指到 U 盘",
     'export AGING_TOOL_DIR="$SCRIPT_DIR/tools"'),
    ("start.sh", "U 盘模式用盘上那份 config.yaml",
     'export AGING_CONFIG="$SCRIPT_DIR/config.yaml"'),
    ("start.sh", "U 盘模式的横幅",
     'log "▶ U 盘模式: 程序 / 数据 / 工具全在 U 盘上"'),
    ("start.sh", "U 盘模式由标记文件触发",
     'if [ -f "$SCRIPT_DIR/AGING_USB_MODE" ]; then'),
]


# ── 「打包/部署类脚本不上盘」(2026-11-10) ─────────────────────────────
#
#   以前构建脚本是 `for d in server static docs scripts` ⇒ 整个 scripts/
#   原样上盘 ⇒ 产线盘上清掉的 8 个「打包/部署类」脚本下次打包又都回来
#   (其中 install_deps.sh 特别不能带 —— 缺工具时页面/报错按形态给办法,
#    U 盘版那句话说「盘上没带齐 ⇒ 改用设备版」; 要是盘上偏偏躺着那个脚本,
#    工人照着跑只会白跑: 产线取不到公网, 装上了也是装在板子上、拔盘带走)。
#
#   这一节查两件事:
#     ① 静态: 整目录拷贝那一行没了, 排除清单在;
#     ② 行为: 把脚本里那个拷贝函数抠出来真跑一遍, 看排除是不是真生效。
#
#   ⚠️ 只在临时目录里跑, 碰不到仓库, 也不执行盘里的任何东西。

#: 不该上盘的脚本(理由见上面那段)
NOT_ON_STICK = [
    "install_deps.sh",          # 缺工具时的「死办法」—— U 盘版不该带
    "build_deb.sh",
    "build_usb_image.sh",
    "verify_deb.sh",
    "deploy.sh",
    "install_desktop_icon.sh",
    "prepare_for_firmware.sh",
    "find_pi5_label.py",
    "probe_rp_gpio_write.py",   # 开发期探针(跟 deb 版一致)
]

#: 必须上盘的(否则产线上没人能自检)
MUST_ON_STICK = ["check_deps.sh", "check_eth.sh"]


def _exclusion_list(src):
    """把构建脚本里那行排除清单**真抠出来**(按名字, 不是按「文件里出现过没有」)。

    ⚠️ 抠的是**调用那一处**的实参 —— 只看「文件里有没有这个名字」是不够的:
       名字出现在**别处**(比如注释里)不等于它在排除清单里。
    """
    i = src.find('copy_tree_except "$PROJECT_ROOT/scripts"')
    if i < 0:
        return []          # 调用那处没了 ⇒ 清单为空(下面的检查会一条条报出来)
    j = src.index("\n", i)
    while src[j - 1] == "\\":          # 续行 ⇒ 接着收
        j = src.index("\n", j + 1)
    call = src[i:j]
    args = call.split()
    # 前两个是源目录/目标目录 ⇒ 后面全是排除名
    return [a for a in args[2:] if "." in a]


def _build_script():
    """构建脚本的全文(手册那节要拿它对账 ⇒ 单独一个函数)。"""
    with open(os.path.join(REPO, "scripts", "build_usb_image.sh"),
              encoding="utf-8") as f:
        return f.read()


def scripts_section():
    print("\n① scripts/ 不整目录上盘")
    path = os.path.join(REPO, "scripts", "build_usb_image.sh")
    with open(path, encoding="utf-8") as f:
        src = f.read()
    check("for d in server static docs scripts;" not in src,
          "构建脚本不再整目录拷 scripts/(改成排除法)")
    check("copy_tree_except" in src,
          "构建脚本用 copy_tree_except 拷 scripts/")
    check('copy_tree_except "$PROJECT_ROOT/scripts" "$OUT_DIR/scripts"' in src,
          "scripts/ 那一处**确实**调的是 copy_tree_except(不是整目录的 copy_tree)")
    excl = _exclusion_list(src)
    for name in NOT_ON_STICK:
        check(name in excl, "排除清单里有: %s" % name)
    for name in MUST_ON_STICK:
        check(name not in excl,
              "该上盘的**没**被排除: %s" % name)

    print("② 行为: 排除真的生效(把那个函数抠出来真跑一遍)")
    i = src.index("copy_tree_except() {")
    j = src.index("\n}\n", i) + len("\n}\n")
    func = src[i:j]
    with tempfile.TemporaryDirectory() as td:
        sdir = os.path.join(td, "src")
        ddir = os.path.join(td, "dst")
        os.makedirs(sdir)
        os.makedirs(ddir)
        for name in ("keep.sh", "drop.sh", "also_drop.py"):
            with open(os.path.join(sdir, name), "w", encoding="utf-8") as f:
                f.write("# %s\n" % name)
        harness = ("set -eu\n%s\ncopy_tree_except %s %s drop.sh also_drop.py\n"
                   % (func, shlex.quote(sdir), shlex.quote(ddir)))
        p = subprocess.run(["bash", "-c", harness], capture_output=True, text=True)
        check(p.returncode == 0, "那个拷贝函数跑得起来", p.stderr[-300:])
        got = sorted(os.listdir(ddir)) if os.path.isdir(ddir) else []
        check(got == ["keep.sh"],
              "排除清单里的文件**没有**被拷过去(其余照拷)", got)


# ── 作业手册与脚本对得上(2026-11-10 加) ────────────────────────────────
#
#   手册在 docs/U盘版部署作业手册.md —— 产线照着它烧盘、照着它验收。
#   手册跟脚本各说各话 = 工人照着一份过期的手册干活 ⇒ 这里查两件事:
#     ① 手册里那些**关键命令/关键值**跟脚本一致;
#     ② 手册没有把「红线」写成正面建议(比如把格式写成 FAT)。
def _doc_excluded_list(doc):
    """把手册里那份「不上盘」的名单抠出来(一个代码块, 一行一个名字)。

    ⚠️ 抠的是**那一块**, 不是「全文里出现过没有」—— 名字出现在别处
       (比如 §7 红线里也提到 install_deps.sh)不等于它在名单里。
    """
    i = doc.find("下面这九个")
    if i < 0:
        return []
    j = doc.find("```", i)
    if j < 0:
        return []
    k = doc.find("```", j + 3)
    if k < 0:
        return []
    return [ln.strip() for ln in doc[j + 3:k].splitlines() if ln.strip()]


def _doc_row(doc, prefix):
    """手册 §4.3 验收清单里**以 prefix 开头的那一行**(表格行)。"""
    for ln in doc.splitlines():
        if ln.startswith(prefix):
            return ln
    return ""


def manual_section():
    print("\n③ 作业手册(docs/U盘版部署作业手册.md)跟脚本对得上")
    path = os.path.join(REPO, "docs", "U盘版部署作业手册.md")
    if not os.path.isfile(path):
        check(False, "手册在: docs/U盘版部署作业手册.md")
        return
    with open(path, encoding="utf-8") as f:
        doc = f.read()

    for needle, why in (
            ("mkfs.ext4 -L AGING", "烧盘: ext4 + 标签 AGING"),
            ("wipefs -a", "烧盘: 先清掉旧的文件系统标记"),
            ("cp -a", "烧盘: 拷镜像"),
            ("chmod a+rwx", "烧盘: 挂载点本身也要可写"),
            ("umount", "烧盘: 卸载之后断电再拔")):
        check(needle in doc, "手册里有这一步: %s" % why)

    check("mkfs.vfat" not in doc and "mkfs.exfat" not in doc,
          "手册里没有把 FAT/exFAT 当格式的建议(FAT 建不起 venv)")

    for name in NOT_ON_STICK:
        check(name in doc, "手册里也写了(不上盘): %s" % name)

    # 「不上盘」的名单: 手册那份**跟脚本那份逐个对得上**
    script_excl = set(_exclusion_list(_build_script()))
    doc_excl = set(_doc_excluded_list(doc))
    check(doc_excl == script_excl,
          "手册里那份「不上盘」的名单跟构建脚本的排除清单**完全一致**",
          ("手册:", sorted(doc_excl), "脚本:", sorted(script_excl)))

    # §4.3 验收清单: **逐行**查期望值(不是全文里找那个字符串)
    row_threads = _doc_row(doc, "| 4 | 出厂默认核数")
    check("`threads: 4`" in row_threads,
          "验收清单: 出厂默认核数那行的期望值是 threads: 4", row_threads)
    row_min = _doc_row(doc, "| 5 | 旋钮下界")
    check('`min="1"`' in row_min,
          "验收清单: 旋钮下界那行的期望值是 min=\"1\"", row_min)
    row_mode = _doc_row(doc, "| 2 | **U 盘模式生效**")
    check('"mode": "usb"' in row_mode,
          "验收清单: U 盘模式那行的期望值是 mode=usb", row_mode)
    row_fix = _doc_row(doc, "| 3 | **没有那条死命令**")
    check('"fix_command": ""' in row_fix,
          "验收清单: fix_command 那行的期望值是空", row_fix)

    for needle, why in (
            ("AGING_USB_MODE", "U 盘模式标记"),
            ("UUID", "台账: 盘的身份 = 文件系统 UUID"),
            ("启动老化测试", "工人入口: 原生启动器")):
        check(needle in doc, "手册里有: %s" % why)


def main():
    for rel, name, needle in ANCHORS:
        path = os.path.join(REPO, rel)
        if not os.path.isfile(path):
            check(False, f"找不到 {rel}")
            continue
        with open(path, encoding="utf-8") as f:
            src = f.read()
        n = src.count(needle)
        check(n == 1, f"锚点在且只在一处: {name}", n)

    scripts_section()
    manual_section()

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
