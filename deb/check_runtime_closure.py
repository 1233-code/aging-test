"""运行时共享库闭包闸 —— 出包前必须过。

**它查什么**
    随包的工具是被 `dpkg-deb -x` 直接解到目标机上的, **不走 dpkg 依赖解析**。
    所以「这个工具能不能跑起来」完全取决于: 它 NEEDED 的那些 .so,
    在目标机上存不存在。

    存在 = 两种来源:
      ① 目标机**本来就有** —— 出货镜像自带 (见 deb/image-baseline.txt);
      ② **随包带走了** —— 就在 deps/ 里另一个 .deb 中。

    两种都占不上 ⇒ 这个工具装上了也跑不起来 (fio 就是这么翻车的:
    它 NEEDED librbd.so.1, 而 librbd1/librados2/libtirpc3/... 全是开发期
    apt 装 fio 时才拖进来的, 出货镜像里根本没有)。

    这道闸就是拿 ELF 的 NEEDED 表逐条比对, 缺一个就不许出包。

**它不做什么**
    · 不执行被检查的二进制 (只用 readelf 读 ELF 头, 不跑 dlopen);
    · 不联网, 不改系统;
    · 不校验版本 —— 镜像里那个版本的 .so 能不能满足 NEEDED, 属于镜像的事,
      镜像基线文件就是那份承诺。

跑法:  python3 deb/check_runtime_closure.py build/deps [--baseline deb/image-baseline.txt]
退出码: 0 = 闭包完整; 1 = 有缺口 (不许出包)
"""
import glob
import os
import shutil
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def fail(msg):
    sys.stderr.write("[closure] ✗ %s\n" % msg)
    sys.exit(1)


def read_baseline(path):
    """读镜像基线 -> 集合 (跳过注释行)。"""
    names = set()
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#"):
                    names.add(line)
    except OSError:
        fail("读不到镜像基线 %s —— 没有它就没法判断「目标机上本来有啥」" % path)
    return names


def pkg_list_file(pkg):
    """找一个包的 files-list 路径。

    ⚠️ 不能用 `glob(base + "*.list")[0]` —— 「-」的 ASCII 码比「:」小, 所以
    `libc6-dev:arm64.list` 会排在 `libc6:arm64.list` 前面, 于是「libc6 的
    共享库」会被当成「libc6-dev 的头文件」→ 误判成镜像里没有, 一片假警报。
    这里按包名**精确**匹配, 先 <pkg>.list 再 <pkg>:<arch>.list。
    """
    exact = "/var/lib/dpkg/info/%s.list" % pkg
    if os.path.isfile(exact):
        return exact
    for cand in sorted(glob.glob("/var/lib/dpkg/info/%s:*.list" % pkg)):
        return cand
    return None


def image_provides(baseline):
    """镜像自带的包 -> 它们提供的 soname 集合。

    注意 usrmerge: dpkg 的 files-list 记的是 /usr/lib/..., 这里统一取**basename**,
    所以 /lib 与 /usr/lib 两种写法都能对上。
    """
    provides = {}
    missing = []
    for pkg in sorted(baseline):
        lst = pkg_list_file(pkg)
        if not lst:
            missing.append(pkg)
            continue
        with open(lst, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                p = line.strip()
                if "/lib" in p and ".so" in os.path.basename(p):
                    provides.setdefault(os.path.basename(p), pkg)
    return provides, missing


def is_elf(path):
    try:
        with open(path, "rb") as fh:
            return fh.read(4) == b"\x7fELF"
    except OSError:
        return False


def needed_libs(path):
    """readelf 读 NEEDED 表 —— 只读 ELF 头, 不执行二进制。"""
    out = []
    try:
        r = subprocess.run(["readelf", "-d", path], capture_output=True, text=True, timeout=30)
    except Exception:
        return out
    for line in r.stdout.splitlines():
        if "(NEEDED)" in line and "[" in line:
            soname = line.split("[", 1)[1].split("]", 1)[0].strip()
            if soname:
                out.append(soname)
    return out


def main():
    if len(sys.argv) < 2:
        fail("用法: check_runtime_closure.py <deps_dir> [--baseline <file>]")
    deps_dir = sys.argv[1]
    baseline_path = os.path.join(REPO, "deb", "image-baseline.txt")
    if "--baseline" in sys.argv:
        baseline_path = sys.argv[sys.argv.index("--baseline") + 1]

    debs = sorted(glob.glob(os.path.join(deps_dir, "*.deb")))
    if not debs:
        fail("%s 里一个 .deb 都没有 —— 离线仓是空的" % deps_dir)

    print("[closure] 镜像基线: %s" % baseline_path)
    baseline = read_baseline(baseline_path)
    img_provides, not_installed = image_provides(baseline)
    print("[closure] 镜像自带 %d 个包, 提供 %d 个 soname"
          % (len(baseline) - len(not_installed), len(img_provides)))
    if not_installed:
        print("[closure] ⚠ 基线里有 %d 个包在这台打包机上没装 (它们的 .so 算不进「镜像自带」)"
              % len(not_installed))

    # 把随包的 .deb 解开, 收集「它们提供什么」+「它们需要什么」
    work = tempfile.mkdtemp(prefix="aging-closure-")
    bundled_provides = {}
    demands = {}          # soname -> [提出需求的 .deb]
    provided_by = {}      # soname -> 提供它的 .deb
    try:
        for i, deb in enumerate(debs):
            pkg = subprocess.run(["dpkg-deb", "-f", deb, "Package"],
                                 capture_output=True, text=True).stdout.strip() or os.path.basename(deb)
            dest = os.path.join(work, "%02d-%s" % (i, pkg))
            os.makedirs(dest, exist_ok=True)
            r = subprocess.run(["dpkg-deb", "-x", deb, dest],
                               capture_output=True, text=True)
            if r.returncode != 0:
                fail("解不开 %s: %s" % (deb, r.stderr.strip()))
            for root, _dirs, files in os.walk(dest):
                for fn in files:
                    full = os.path.join(root, fn)
                    rel = os.path.relpath(full, dest)
                    if ".so" in fn:
                        bundled_provides.setdefault(fn, "%s (%s)" % (pkg, rel))
                    if is_elf(full):
                        for soname in needed_libs(full):
                            demands.setdefault(soname, []).append("%s: %s" % (pkg, rel))
                            provided_by.setdefault(soname, "%s (%s)" % (pkg, rel))

        print("[closure] 随包 %d 个 .deb, 提供 %d 个 soname; 需求 %d 个 soname"
              % (len(debs), len(bundled_provides), len(demands)))

        # ── 缺口: 既不在镜像里, 也不在随包里 ──
        gaps = []
        for soname, askers in sorted(demands.items()):
            if soname in bundled_provides or soname in img_provides:
                continue
            gaps.append((soname, askers))

        if gaps:
            print("")
            print("[closure] ✗ 有 %d 个共享库既不在出货镜像里、也没随包带走 ——"
                  " 这些工具装上去就跑不起来:" % len(gaps))
            for soname, askers in gaps:
                print("      %-34s 需要它的是: %s" % (soname, ", ".join(askers)))
            print("")
            print("[closure]   修法: 把上面这些 soname 所属的包加进 build_deb.sh 的")
            print("[closure]   TOOL_PKGS, 重新打一次包。")
            return 1

        # ── 报告: 谁提供了什么 ──
        from_image = [s for s in demands if s in img_provides and s not in bundled_provides]
        from_bundle = [s for s in demands if s in bundled_provides]
        print("[closure] ✓ 闭包完整")
        print("[closure]   随包解决 %d 个: %s"
              % (len(from_bundle), ", ".join(sorted(from_bundle))))
        print("[closure]   镜像自带 %d 个: %s"
              % (len(from_image), ", ".join(sorted(from_image))))
        return 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
