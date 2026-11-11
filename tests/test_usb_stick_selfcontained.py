"""回归测试: U 盘上的工具在**别的板子**上还起不起来。

起因 (2026-11-06 实测, 报告 SN A10010000000058):
    那轮 eMMC 项 error: `fio write 失败: rc=127 · .../fio.bin: error while
    loading shared libraries: libceph-common.so.2`。
    库**就在盘上**(`tools/root/usr/lib/aarch64-linux-gnu/ceph/libceph-common.so.2`),
    可链接器找不到它:
      · wrapper 给的 LD_LIBRARY_PATH 只列了**上一层**目录 ⇒ 没列 `ceph/`;
      · 盘上那份 `librados.so.2` 的 RUNPATH 写的是**板子上的绝对路径**
        `/usr/lib/aarch64-linux-gnu/ceph` ⇒ 开发板上有这个目录(所以那儿能跑),
        **0058 那台没有** ⇒ rc=127「起不来」⇒ 存储项 error ⇒
        **一台好板子被当成坏板子返修**。

    为什么构建时没拦住: 那个自检是「在**构建机上**跑一下 `fio --version`」
    ⇒ 构建机碰巧有那个目录 ⇒ 绿灯 ⇒ **坏镜像出厂**。

这个测试守三件事:
    ① wrapper 的库搜索路径**必须带上子目录**(少了就断);
    ② wrapper 的路径清单 跟 检查脚本(`scripts/check_usb_selfcontained.py`)
       用的**必须一致** —— 不一致的话, 检查脚本验的是另一条路径, 白验;
    ③ 构建时那个检查**不通过就得停手**(只警告 = 坏镜像照样出厂)。

全是**静态**检查 —— 不执行盘里的任何二进制, 不碰真 U 盘。
跑法: .venv/bin/python -m tests.test_usb_stick_selfcontained
"""
import importlib.util
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAILS = []
PASSES = 0


def check(cond, msg, extra=""):
    global PASSES
    if cond:
        PASSES += 1
        print(f"  ✓ {msg}")
    else:
        FAILS.append(msg)
        print(f"  ✗ {msg}" + (f"  [{extra}]" if extra else ""))


def load_checker():
    """把 scripts/check_usb_selfcontained.py 装进来(它不是包)。"""
    path = os.path.join(REPO, "scripts", "check_usb_selfcontained.py")
    spec = importlib.util.spec_from_file_location("_usb_selfcontained", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def wrapper_globs(src):
    """从 wrapper 模板里抠出那串 glob(去掉 $_sysroot 前缀)。

    模板里长这样(注意引号**在** $_sysroot 后面, 后面跟的是路径):
        for _d in "$_sysroot"/usr/lib/*/ "$_sysroot"/usr/lib/ ...
    """
    out = []
    for m in re.finditer(r'"\\?\$_sysroot"(/[^\s"\\;]+)', src):
        out.append(m.group(1).lstrip("/"))
    return out


def main():
    build = open(os.path.join(REPO, "scripts", "build_usb_image.sh"),
                 encoding="utf-8").read()
    tpl_path = os.path.join(REPO, "scripts", "usb_wrapper.sh.tpl")
    check(os.path.isfile(tpl_path), "wrapper 模板 scripts/usb_wrapper.sh.tpl 在")
    tpl = open(tpl_path, encoding="utf-8").read() if os.path.isfile(tpl_path) else ""
    C = load_checker()

    # ─────────────────────────────────────────────────────────────
    print("\n【1】wrapper 的库搜索路径必须带上**子目录**")
    # ─────────────────────────────────────────────────────────────
    globs = wrapper_globs(tpl)
    check("usr/lib/*/*/" in globs,
          "wrapper 的路径里有 usr/lib/*/*/ (库住在子目录里)",
          f"globs={globs}")
    check("lib/*/*/" in globs,
          "wrapper 的路径里有 lib/*/*/", f"globs={globs}")

    # ─────────────────────────────────────────────────────────────
    print("\n【1b】wrapper 只有**一份**来源(构建和打补丁共用)")
    #    两边各写一份 ⇒ 迟早漂 ⇒ 盘上的 wrapper 悄悄少一层 ⇒
    #    同样的毛病再犯一次(2026-11-06 的教训)。
    # ─────────────────────────────────────────────────────────────
    # ⚠️ 别用 `"scripts/usb_wrapper.sh.tpl" in build` 这种糊法 ——
    #    注释里也写着这个路径 ⇒ 路径改了测试还是绿的(变异脚本抓到了这个)。
    #    要看的是**赋值**指向哪个文件。
    m = re.search(r'WRAPPER_TPL="([^"]+)"', build)
    tpl_rel = m.group(1) if m else ""
    check(tpl_rel.endswith("scripts/usb_wrapper.sh.tpl"),
          "构建脚本用的模板就是那份唯一来源", tpl_rel or "(没找到 WRAPPER_TPL 赋值)")
    check(os.path.isfile(os.path.join(REPO, tpl_rel.replace("$PROJECT_ROOT/", "")))
          if tpl_rel else False,
          "模板文件真的在那个位置", tpl_rel)
    check('sed -e "s|@REAL@|$real|g" "$WRAPPER_TPL"' in build,
          "构建脚本是从模板 sed 出来的(不是自己再写一份)")
    check("_sysroot" not in build,
          "构建脚本里没有第二份 inline 路径清单(会漂)",
          "构建脚本里出现了 _sysroot")

    # ─────────────────────────────────────────────────────────────
    print("\n【2】wrapper 的路径 跟 检查脚本的必须一致")
    # ─────────────────────────────────────────────────────────────
    #    不一致 ⇒ 检查脚本验的是另一条路径 ⇒ 白验(比没有检查还坏:
    #    它会让人以为验过了)。
    check(list(C.LIB_GLOBS) == globs,
          "检查脚本的 LIB_GLOBS == wrapper 的路径清单",
          f"checker={list(C.LIB_GLOBS)} wrapper={globs}")

    # ─────────────────────────────────────────────────────────────
    print("\n【3】构建时那个检查不通过就得**停手**")
    # ─────────────────────────────────────────────────────────────
    check("check_usb_selfcontained.py" in build,
          "构建脚本调了检查脚本")
    check('die "这根盘在**别的板子**上起不来' in build,
          "检查不通过是 die(停手), 不是 warn(只警告 = 坏镜像照样出厂)")
    check("⑩b" in build, "检查有自己的编号(能在构建日志里找得到)")

    # ─────────────────────────────────────────────────────────────
    print("\n【4】检查脚本的判定逻辑")
    # ─────────────────────────────────────────────────────────────
    stick = {"libfoo.so.1": "/stick/lib/libfoo.so.1",
             "libbar.so.2.0.0": "/stick/lib/libbar.so.2.0.0"}
    board = {"libX11.so.6": "/usr/lib/aarch64-linux-gnu/libX11.so.6"}

    v, w = C.classify_needed("libfoo.so.1", stick, board, [])
    check(v == "stick", "盘上有的库 → stick", f"{v}/{w}")
    v, w = C.classify_needed("libbar.so.1", stick, board, [])
    check(v == "stick", "真实文件名带版本号也能认(soname → 真实文件)",
          f"{v}/{w}")
    v, w = C.classify_needed("libX11.so.6", stick, board, [],
                             image_owner={"libX11.so.6": "libx11-6"},
                             baseline={"libx11-6"})
    check(v == "board", "出厂镜像标准目录里有的 → board", f"{v}/{w}")
    v, w = C.classify_needed("libnope.so.1", stick, board, [])
    check(v == "missing", "哪儿都找不到 → missing", f"{v}/{w}")
    # ⚠️ 2026-11-11: 「board」这一档现在**要判据**(出货镜像基线 + 归属包)
    #    —— 判据不全不许说「出厂镜像提供」(那是假绿)⇒ 归到 unknown。
    v, w = C.classify_needed("libX11.so.6", stick, board, [])
    check(v == "unknown",
          "判据不全(没给基线)⇒ 归到「下不了结论」, **不**说「出厂镜像提供」",
          f"{v}/{w}")

    # 「只能靠库里写死的绝对路径找到」—— 2026-11-06 那个坑
    os.makedirs("/tmp", exist_ok=True)
    fake = "/tmp/aging-fake-runpath-dir"
    os.makedirs(fake, exist_ok=True)
    with open(os.path.join(fake, "libceph-common.so.2"), "w") as f:
        f.write("x")
    v, w = C.classify_needed("libceph-common.so.2", {}, board, [fake])
    check(v == "runpath",
          "只能靠 RUNPATH 里写死的绝对路径找到 → runpath(**危险**)", f"{v}/{w}")
    stick2 = dict(stick)
    stick2["libceph-common.so.2"] = "/stick/lib/ceph/libceph-common.so.2"
    v, w = C.classify_needed("libceph-common.so.2", stick2, board, [fake])
    check(v == "stick",
          "同一库, 盘上也有 ⇒ stick(修好之后就是这样)", f"{v}/{w}")

    # ─────────────────────────────────────────────────────────────
    print("\n【5】核心库不算缺")
    # ─────────────────────────────────────────────────────────────
    for n in ("linux-vdso.so.1", "ld-linux-aarch64.so.1"):
        check(n in C.ALWAYS, f"{n} 在「到处都有」的名单里")
    check("libceph-common.so.2" not in C.ALWAYS,
          "libceph-common.so.2 **不在**那个名单里(它不该被当成「到处都有」)")
    # libc 在板子的标准目录里 ⇒ 判成 board(不是 missing)
    # ⚠️ 2026-11-11: 「board」要**判据**(基线 + 归属包)⇒ 这儿用真的那份
    real_board = C.build_index([d for d in C.STD_DIRS if os.path.isdir(d)])
    owner = C.load_image_owner() or {}
    baseline = C.load_baseline() or set()
    v, _w = C.classify_needed("libc.so.6", {}, real_board, [],
                               image_owner=owner, baseline=baseline)
    check(v == "board", "libc.so.6 在出厂镜像里 ⇒ board(不是 missing)", v)
    v, _w = C.classify_needed("libX11.so.6", {}, real_board, [],
                               image_owner=owner, baseline=baseline)
    check(v == "board", "libX11.so.6 在出厂镜像里 ⇒ board", v)

    print("\n" + "=" * 60)
    if FAILS:
        print(f"✗ {len(FAILS)} 项失败 / 共 {PASSES + len(FAILS)} 项")
        for f in FAILS:
            print(f"   - {f}")
        return 1
    print(f"✓ 全部 {PASSES} 项通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
