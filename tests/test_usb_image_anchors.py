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
import sys

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
