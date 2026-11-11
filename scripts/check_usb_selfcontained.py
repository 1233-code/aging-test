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
    ⇒ 退出码 0 = 没问题; 1 = 有库在别的板子上会找不到(真问题);
      3 = **下不了结论**(判据不全 —— 读不到出货镜像基线 / 认不出归属包)
      —— 3 **不是**「板子有问题」, 只表示这次判不了

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


def classify_needed(name, stick_index, board_index, runpath_dirs,
                    image_owner=None, baseline=None):
    """一个依赖库到别的板子上还找不找得到?

    返回 (判定, 详情):
        "stick"   —— 盘上自带 ✓
        "board"   —— **出厂镜像**里有 ✓(判据 = 出货镜像基线, 见 image_owner)
        "notimage"—— ⚠ 这台**打包机**上有, 但**出厂镜像里没有** ⇒ 换台板子
                     可能就找不到 ⇒ 算问题
        "unknown" —— ⚠ **下不了结论**(拿不到基线 / 认不出归属包)⇒ 既不说
                     「有」也不说「没有」⇒ **不算问题, 但也别当它通过了**
        "runpath" —— ⚠ 只能靠库里写死的绝对路径找到(换台板子就断)
        "missing" —— ✗ 哪儿都找不到

    ⚠️ **「出厂镜像提供」不能只看「这台机器的 /lib 里有没有」** ——
       打包机上开发期 `apt install fio` 拖进来的那一族库(librados2 /
       librbd1 / libtirpc3 / libgfapi0 / librdmacm1 / libceph-common …)
       在打包机的 /lib 里躺着, 但**出厂镜像里根本没有**(不在基线里)
       ⇒ 拿 /lib 当判据 = **假绿** ⇒ 换台板子就 rc=127 ⇒ **好板子被返修**
       ⇒ 2026-11-06 那次 fio rc=127 就是这么漏出去的。
       ⇒ 判据改成 **出货镜像基线**(`deb/image-baseline.txt`): 提供这个库的
       **包**在基线里才算「出厂镜像提供」。

    ⚠️ **判据不全 ≠ 「有问题」** —— 这两件事必须分开:
       · 判据齐全 ⇒ 确知「镜像里没有」⇒ **notimage** ⇒ 算问题(退出码 1)
       · 判据不全(盘上没带基线文件 / 这台机器认不出归属包)⇒ **unknown**
         ⇒ 退出码 **3**(= 下不了结论)⇒ **不是**「板子有问题」。
       (在产线板子上跑盘上这份脚本就会落到后者: 盘上没带 deb/image-baseline.txt
        ⇒ 判据不全 ⇒ 退出码 3。**那道真正的闸在打包机上跑**
        —— `build_usb_image.sh` 出镜像时会跑一遍。)
    """
    p = stick_index.get(name)
    if p:
        return "stick", os.path.dirname(p)
    base = name.split(".so")[0] + ".so"
    for fn, full in stick_index.items():
        if fn.startswith(base) and ".so" in fn:
            return "stick", os.path.dirname(full)
    if name in board_index:
        if image_owner is None or baseline is None:
            # 判据不全(拿不到基线 / 认不出归属包) ⇒ **不许**说「出厂镜像提供」
            # —— 那是**假绿**, 而假绿的代价是换台板子就断(好板子被返修);
            # 但也**不许**说「有问题」—— 那是在没判据的情况下乱扣帽子
            # ⇒ 归到 unknown(退出码 3 = 下不了结论)。
            return "unknown", "判据不全(拿不到基线或归属包)⇒ 下不了结论"
        pkg = image_owner.get(name)
        if pkg is None:
            return "unknown", "归属包认不出(⚠ 这台机器上没有 dpkg 信息)⇒ 下不了结论"
        if pkg in baseline:
            return "board", "出厂镜像(%s)" % pkg
        return "notimage", "%s(不在出厂镜像基线里)" % pkg
    for rp in runpath_dirs:
        if os.path.exists(os.path.join(rp, name)):
            return "runpath", rp
    return "missing", ""


#: 出货镜像基线 —— 「目标板上本来就有的包」= 出厂镜像契约。
BASELINE_REL = os.path.join("deb", "image-baseline.txt")


def load_baseline():
    """读出货镜像基线(包名集合)。读不到就返回 None(判据退化成老口径)。"""
    path = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), BASELINE_REL)
    try:
        with open(path, encoding="utf-8") as f:
            return {ln.strip() for ln in f
                    if ln.strip() and not ln.startswith("#")}
    except OSError:
        return None


def load_image_owner():
    """soname → 提供它的包名(扫 dpkg 的 files-list)。

    ⚠️ 扫**所有**已装的包, 不是只扫基线里的 —— 因为要回答的是
       「这个库是不是**基线里的包**提供的」。
    """
    owner = {}
    for lst in glob.glob("/var/lib/dpkg/info/*.list"):
        pkg = os.path.basename(lst)[:-len(".list")]
        try:
            with open(lst, encoding="utf-8", errors="replace") as f:
                for line in f:
                    p = line.strip()
                    if "/lib" in p and ".so" in os.path.basename(p):
                        owner.setdefault(os.path.basename(p), pkg)
        except OSError:
            continue
    return owner or None


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
    # 「出厂镜像提供」的判据 = **出货镜像基线**, 不是「打包机的 /lib 里有没有」
    baseline = load_baseline()
    image_owner = load_image_owner()
    if baseline is None:
        print("   ⚠ 读不到出货镜像基线(%s)⇒ 判据退化成「这台机器上有就算」"
              " ⇒ **偏松**, 结论别全信" % BASELINE_REL)
    elif image_owner is None:
        print("   ⚠ 这台机器上读不到 dpkg 的 files-list ⇒ 认不出每个库的归属包"
              " ⇒ 判据退化成「这台机器上有就算」⇒ **偏松**, 结论别全信")

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
                n, stick_index, board_index, info["runpath"],
                image_owner=image_owner, baseline=baseline)
            resolved[n] = (verdict, where, {who})
            if verdict == "stick":
                path, _d = lookup(n)
                if path:
                    queue.append(path)

    bad_runpath = {n: v for n, v in resolved.items() if v[0] == "runpath"}
    missing = {n: v for n, v in resolved.items() if v[0] == "missing"}
    notimage = {n: v for n, v in resolved.items() if v[0] == "notimage"}
    unknown = {n: v for n, v in resolved.items() if v[0] == "unknown"}
    n_stick = sum(1 for v in resolved.values() if v[0] == "stick")
    n_board = sum(1 for v in resolved.values() if v[0] == "board")

    print(f"   (搜索路径: {'带子目录' if deep else '**只有顶层**(旧布局)'}, "
          f"{len(stick_dirs)} 个目录)")
    print(f"   依赖库: 盘上自带 {n_stick} 个, 出厂镜像提供 {n_board} 个, "
          f"⚠ 靠写死路径 {len(bad_runpath)} 个, "
          f"⚠ **镜像里没有** {len(notimage)} 个, 缺 {len(missing)} 个"
          + (f", ⚠ 下不了结论 {len(unknown)} 个" if unknown else ""))

    if notimage:
        print()
        print("   ⚠⚠ **这些库在这台打包机上有, 但**出厂镜像里没有** ⇒ 换台板子"
              "就断:")
        for n, (_k, where, who) in sorted(notimage.items()):
            print(f"      {n}  ← {where}  ← 谁要它: {', '.join(sorted(who))}")
        print("      ⇒ 提供它们的包**不在出货镜像基线**里 ⇒ 那只是**开发期"
              "在这台机器上 apt 装出来的**")
        print("      ⇒ 2026-11-06 实测: fio 硬链 librbd.so.1, 而 librbd1/"
              "librados2/... 都不在镜像里 ⇒ 换台没装 ceph 的板子 fio rc=127")
        print("      ⇒ **好板子被返修**。修法: 把提供这些库的 .deb 也解进"
              "tools/root(打进盘里)")

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

    if unknown:
        print()
        print("   ⚠ **下不了结论**(不是「有问题」):")
        for n, (_k, where, who) in sorted(unknown.items()):
            print(f"      {n}  ← {where}  ← 谁要它: {', '.join(sorted(who))}")
        print("      ⇒ 原因: **判据不全** —— 这台机器上读不到出货镜像基线"
              "(deb/image-baseline.txt)或认不出归属包")
        print("      ⇒ 在**产线板子**上跑盘上这份脚本就是这样(盘上没带基线文件)")
        print("      ⇒ **真正的闸在打包机上**: build_usb_image.sh 出镜像时会跑一遍"
              "(那时基线在仓库里)⇒ 这里是提醒, 不是判废")

    if bad_runpath or missing or notimage:
        return 1
    if unknown:
        return 3
    print("   ✓ 每个库在别的板子上都找得到")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
