#!/usr/bin/env python3
"""检查「U 盘上的工具在**别的板子**上还能不能起来」—— 静态解析 ELF, **不执行**盘里的二进制。

起因 (2026-11-06 实测, 报告 SN A10010000000058):
    那轮 eMMC 项 error: `fio write 失败: rc=127 · .../fio.bin: error while
    loading shared libraries: libceph-common.so.2`。
    库**就在盘上**(tools/root/usr/lib/aarch64-linux-gnu/ceph/libceph-common.so.2),
    可链接器找不到它 —— 因为:
      · wrapper 给的 LD_LIBRARY_PATH 只列了**上一层**目录, 没列 `ceph/` 子目录;
      · 盘上那份 `librados.so.2` 的 RUNPATH 写的是**板子上的绝对路径**
        `/usr/lib/aarch64-linux-gnu/ceph` —— 开发板上有这个目录(所以那儿
        能跑), **0058 那台没有** ⇒ rc=127 ⇒ 存储项 error ⇒
        **好板子被当成坏板子返修**。

    为什么构建时的自检没拦住: 那个自检是「在**构建机上**跑一下
    `fio --version`」⇒ 构建机碰巧有那个目录 ⇒ 绿灯 ⇒ 坏镜像出厂。

这个检查问的是: **每个依赖库, 到了别的板子上还找得到吗?**
    链接器的找法(依次): RPATH → LD_LIBRARY_PATH(wrapper 给的) → RUNPATH
    → ld.so.cache → 标准目录(/lib, /usr/lib, …)
    ⇒ 分三档:
      ① 盘上找得到          —— 最好(这根盘自带)
      ② 标准目录里找得到    —— 出厂镜像提供(可以接受)
      ③ **只能靠库里写死的绝对路径(RUNPATH)找到** —— ⚠️ **危险**:
         那个路径是**目标板子**的, 换个没这目录的板子就 rc=127 ⇒
         2026-11-06 就是这么坏的 ⇒ 报出来
      ④ 哪儿都找不到        —— 缺 ⇒ 报出来

用法:
    scripts/check_usb_selfcontained.py <tools/ 目录>
    ⇒ 退出码 0 = 没问题; 1 = 有库在别的板子上会找不到

    --no-deep : 按**旧**的搜索路径(只有顶层, 不带子目录)来查
                —— 用来验证「2026-11-06 那个坏布局」确实会被报出来
"""
import glob
import os
import struct
import sys

#: 这些库是**所有** Linux 都有的(核心库/通用库)⇒ 不管在哪儿都找得到
ALWAYS = {
    "linux-vdso.so.1", "ld-linux-aarch64.so.1", "ld-linux.so.2",
}

#: 链接器的**标准目录** —— 出厂镜像的库都在这儿(ld.so.cache 也是指到这儿)
STD_DIRS = (
    "/lib", "/usr/lib",
    "/lib/aarch64-linux-gnu", "/usr/lib/aarch64-linux-gnu",
    "/lib64", "/usr/lib64",
)

#: wrapper 用的搜索路径 —— **必须跟 build_usb_image.sh 里那份一模一样**
#: (那边是 glob, 这边是同样的 glob; 少一层 ⇒ fio 在别的板子上起不来)
LIB_GLOBS = (
    "usr/lib/*/", "usr/lib/", "lib/*/", "lib/",
    "usr/lib/*/*/", "lib/*/*/",
)

SHT_DYNAMIC = 6
SHT_STRTAB = 3
DT_NEEDED = 1
DT_SONAME = 14
DT_RPATH = 15
DT_RUNPATH = 29


def elf_info(path):
    """返回 dict(soname, needed=[...], runpath=[...])；不是 ELF 返回 None。"""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return None
    if len(data) < 64 or data[:4] != b"\x7fELF":
        return None
    if data[4] != 2:                      # 只处理 64 位(板子是 aarch64)
        return None
    try:
        (e_shoff,) = struct.unpack_from("<Q", data, 0x28)
        (e_shentsize, e_shnum, _shstrndx) = struct.unpack_from("<HHH", data, 0x3A)
        sections = []
        for i in range(e_shnum):
            off = e_shoff + i * e_shentsize
            (sh_name, sh_type, _flags, _addr, sh_offset, sh_size,
             sh_link, _info, _align, sh_entsize) = struct.unpack_from(
                "<IIQQQQIIQQ", data, off)
            sections.append((sh_type, sh_offset, sh_size))
        dyn = next(((o, s) for t, o, s in sections if t == SHT_DYNAMIC), None)
        dynstr = next(((o, s) for t, o, s in sections if t == SHT_STRTAB and s), None)
        if not dyn or not dynstr:
            return None
        dyn_off, dyn_size = dyn
        str_off, _str_size = dynstr
        needed, runpath, soname = [], [], None
        for i in range(dyn_size // 16):
            tag, val = struct.unpack_from("<qQ", data, dyn_off + i * 16)
            if tag == 0:
                break
            if tag == DT_NEEDED:
                needed.append(val)
            elif tag == DT_SONAME:
                soname = val
            elif tag in (DT_RPATH, DT_RUNPATH):
                runpath.append(val)

        def cstr(off):
            end = data.index(b"\0", str_off + off)
            return data[str_off + off:end].decode("utf-8", "replace")

        return {
            "soname": cstr(soname) if soname is not None else None,
            "needed": [cstr(o) for o in needed],
            "runpath": [cstr(o) for o in runpath],
        }
    except Exception:
        return None


def dirs_from_globs(sysroot, deep=True):
    out = []
    globs = LIB_GLOBS if deep else LIB_GLOBS[:4]
    for g in globs:
        for d in sorted(glob.glob(os.path.join(sysroot, g))):
            if os.path.isdir(d) and d not in out:
                out.append(d)
    return out


def build_index(dirs):
    """目录 → {文件名: 完整路径}；同时按「soname 前缀」登记真实文件。"""
    index = {}
    for d in dirs:
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for n in names:
            index.setdefault(n, os.path.join(d, n))
    return index


def classify_needed(name, stick_index, board_index, runpath_dirs):
    """一个依赖库到别的板子上还找不找得到?

    返回 (判定, 详情):
        "stick"   —— 盘上自带 ✓
        "board"   —— 出厂镜像的标准目录里有 ✓
        "runpath" —— ⚠ 只能靠库里写死的绝对路径找到(换台板子就断)
        "missing" —— ✗ 哪儿都找不到
    """
    p = stick_index.get(name)
    if p:
        return "stick", os.path.dirname(p)
    base = name.split(".so")[0] + ".so"
    for fn, full in stick_index.items():
        if fn.startswith(base) and ".so" in fn:
            return "stick", os.path.dirname(full)
    if name in board_index:
        return "board", "出厂镜像"
    for rp in runpath_dirs:
        if os.path.exists(os.path.join(rp, name)):
            return "runpath", rp
    return "missing", ""


def main(argv):
    args = [a for a in argv[1:] if not a.startswith("--")]
    deep = "--no-deep" not in argv
    if len(args) != 1:
        print(__doc__)
        return 2
    tools = os.path.abspath(args[0])
    sysroot = os.path.join(tools, "root")
    libexec = os.path.join(tools, "libexec")

    if not os.path.isdir(libexec):
        print(f"✗ {libexec} 不像是个镜像的 tools 目录")
        return 2

    stick_dirs = dirs_from_globs(sysroot, deep=deep)
    stick_index = build_index(stick_dirs)
    board_index = build_index([d for d in STD_DIRS if os.path.isdir(d)])

    entries = [os.path.join(libexec, f) for f in sorted(os.listdir(libexec))
               if f.endswith(".bin")]

    # soname -> 判定结果
    resolved = {}      # soname -> ("stick"|"board"|"runpath"|"missing", 详情, 谁要它)
    queue = list(entries)
    seen_files = set()

    def lookup(name):
        """在盘里找 —— 返回 (路径, 所在目录) 或 (None, None)。"""
        p = stick_index.get(name)
        if p:
            return p, os.path.dirname(p)
        base = name.split(".so")[0] + ".so"
        for fn, full in stick_index.items():
            if fn.startswith(base) and ".so" in fn:
                return full, os.path.dirname(full)
        return None, None

    while queue:
        cur = queue.pop(0)
        if cur in seen_files:
            continue
        seen_files.add(cur)
        info = elf_info(cur)
        if not info:
            continue
        who = os.path.basename(cur)
        for n in info["needed"]:
            if n in ALWAYS:
                continue
            if n in resolved:
                resolved[n][2].add(who)
                continue
            verdict, where = classify_needed(
                n, stick_index, board_index, info["runpath"])
            resolved[n] = (verdict, where, {who})
            if verdict == "stick":
                path, _d = lookup(n)
                if path:
                    queue.append(path)

    bad_runpath = {n: v for n, v in resolved.items() if v[0] == "runpath"}
    missing = {n: v for n, v in resolved.items() if v[0] == "missing"}
    n_stick = sum(1 for v in resolved.values() if v[0] == "stick")
    n_board = sum(1 for v in resolved.values() if v[0] == "board")

    print(f"   (搜索路径: {'带子目录' if deep else '**只有顶层**(旧布局)'}, "
          f"{len(stick_dirs)} 个目录)")
    print(f"   依赖库: 盘上自带 {n_stick} 个, 出厂镜像提供 {n_board} 个, "
          f"⚠ 靠写死路径 {len(bad_runpath)} 个, 缺 {len(missing)} 个")

    if bad_runpath:
        print()
        print("   ⚠⚠ **这些库只能靠「库里写死的绝对路径」找到** ⇒ 换台板子就断:")
        for n, (_k, where, who) in sorted(bad_runpath.items()):
            print(f"      {n}  ← 路径 {where}  ← 谁要它: {', '.join(sorted(who))}")
        print("      ⇒ 2026-11-06 实测: libceph-common.so.2 就是这么断的")
        print("      ⇒ 开发板上有那个目录(所以那儿能跑), 别的板子没有 ⇒")
        print("         fio rc=127「起不来」⇒ 存储项 error ⇒ **好板子被返修**")
        print("      ⇒ 修法: ① wrapper 的搜索路径带上那个子目录")

    if missing:
        print()
        print("   ✗ **这些库哪儿都找不到** ⇒ 工具在板子上根本起不来:")
        for n, (_k, _w, who) in sorted(missing.items()):
            print(f"      {n}  ← 谁要它: {', '.join(sorted(who))}")
        print("      ⇒ 修法: 把提供这些库的 .deb 也解进 tools/root")

    if bad_runpath or missing:
        return 1
    print("   ✓ 每个库在别的板子上都找得到")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
