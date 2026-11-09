"""解包安全性闸 —— 出包前必须过 (usrmerge 软链不许被踩坏)。

**随包 .deb 的来源 (白名单)**
    全部来自 **Ubuntu 20.04.6 LTS (focal) 官方 archive**
    (`http://archive.ubuntu.com/ubuntu/pool/...`), 架构 arm64 / all。
    由 `scripts/build_deb.sh` 用 `apt-get download` 取得, 清单见
    `deb/image-baseline.txt`。

    这道闸**只把它们当数据文件读** (纯 Python 解 ar + tar 头),
    **不解包、不执行、不安装** —— 包里的一个字节都不会被运行。

**它查什么**
    随包的工具是被**直接解到 `/`** 的 (不走 dpkg 的依赖解析),
    所以「解包这一步会不会砸了板子」必须单独验。

    Ubuntu 20.04 是 usrmerge: `/lib` → `usr/lib`、`/bin` → `usr/bin`、
    `/sbin` → `usr/sbin`、`/lib64` → `usr/lib64` —— **四个都是软链**,
    而动态链接器 (`/lib/ld-linux-aarch64.so.1`) 就挂在 `/lib` 底下。

    解包用的 `dpkg-deb -x` 底下就是**裸 tar**: 它遇到 tar 包里的
    `./lib/` 这种**目录成员**时, 会**把软链删掉、换成一个真目录**!
    ⇒ 动态链接器当场消失 ⇒ **整台板子上所有动态链接的程序全部
      execve 报 ENOENT** (bash 打印的是绝对路径:
      "/usr/bin/grep: No such file or directory"),
      只有**已经在内存里**的 dpkg / bash 还能接着跑 ⇒ 后面每个包都
      静默跳过 ⇒ 最后 postinst 死在 `python3 -m venv` 上, dpkg 报:
        "post-installation script subprocess returned error exit status 1"

    ⇒ 闸查两件事:
      ① **哪些包带了这种「目录成员」** (带了就必须在解包时保住软链);
      ② **postinst 是不是真的在用保软链的解法**
         (`tar --keep-directory-symlink`) —— 不在, 就不许出包。

**它不做什么**
    · 不解包、不执行包里的任何东西 (纯 Python 读归档头);
    · 不联网、不改系统。

跑法:  python3 deb/check_extract_safety.py build/deps
退出码: 0 = 安全; 1 = 不许出包
"""
import glob
import io
import os
import sys
import tarfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: usrmerge 的软链 —— 顶层目录名 → 它指向的 usr/ 目录
USRMERGE = ("bin", "sbin", "lib", "lib64")

#: postinst 里必须出现的那个解法 (保住软链)
KEEP_LINK_FLAG = "--keep-directory-symlink"


def fail(msg):
    sys.stderr.write("[extract] ✗ %s\n" % msg)
    sys.exit(1)


def ar_members(path):
    """读 .deb (就是 ar 归档) 的成员: (名字, 数据)。纯 Python, 不调外部命令。"""
    with open(path, "rb") as f:
        if f.read(8) != b"!<arch>\n":
            raise ValueError("%s 不是 ar 归档" % path)
        while True:
            hdr = f.read(60)
            if len(hdr) < 60:
                break
            name = hdr[0:16].decode("ascii", "replace").strip().rstrip("/")
            try:
                size = int(hdr[48:58].decode("ascii", "replace").strip())
            except ValueError:
                break
            data = f.read(size)
            if size % 2:
                f.read(1)          # ar 成员按偶数字节对齐
            yield name, data


def data_tar(deb_path):
    """取出 .deb 里的 data.tar.* (真正的文件内容)。"""
    for name, data in ar_members(deb_path):
        if name.startswith("data.tar"):
            return data
    raise ValueError("%s 里没有 data.tar" % deb_path)


def top_dirs(deb_path):
    """这个 deb 的顶层目录成员 —— 也就是「会把 usrmerge 软链踩坏」的那些。

    只看**目录**成员: 普通文件成员落进软链底下是安全的 (内核会顺着
    软链写), 只有「目录成员」才会逼着 tar 把软链换成真目录。
    """
    blob = data_tar(deb_path)
    found = set()
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:*") as tf:
        for m in tf:
            if not m.isdir():
                continue
            name = m.name[2:] if m.name.startswith("./") else m.name
            head = name.split("/")[0]
            if head in USRMERGE:
                found.add(head)
    return found


def pkg_name(deb_path):
    """从 control 成员里读包名。"""
    for name, data in ar_members(deb_path):
        if name.startswith("control.tar"):
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tf:
                for m in tf:
                    if m.name.lstrip("./") == "control":
                        text = tf.extractfile(m).read().decode("utf-8", "replace")
                        for line in text.splitlines():
                            if line.startswith("Package:"):
                                return line.split(":", 1)[1].strip()
    return os.path.basename(deb_path)


def postinst_keeps_links():
    """postinst 是不是真的用了保软链的解法? (读源码, 不执行)"""
    path = os.path.join(REPO, "deb", "postinst")
    try:
        with open(path, encoding="utf-8") as f:
            src = f.read()
    except OSError:
        return False, "读不到 deb/postinst"
    # 排除注释行再找 —— 注释里提到这个 flag 不算数
    code = "\n".join(l for l in src.splitlines()
                     if not l.lstrip().startswith("#"))
    if KEEP_LINK_FLAG in code:
        return True, ""
    return False, ("postinst 的**代码**里没有 %s —— "
                   "解包会用 dpkg-deb -x, 把 usrmerge 软链踩坏"
                   % KEEP_LINK_FLAG)


def main(argv):
    deps_dir = argv[1] if len(argv) > 1 else "build/deps"

    debs = sorted(glob.glob(os.path.join(deps_dir, "*.deb")))
    if not debs:
        fail("在 %s 里一个 .deb 都没有 —— 打包脚本的缺陷 (§11.7)" % deps_dir)

    print("[extract] 检查 %d 个随包 .deb …" % len(debs))

    risky = []
    for deb in debs:
        pkg = pkg_name(deb)
        try:
            tops = top_dirs(deb)
        except Exception as e:                      # noqa: BLE001
            risky.append((pkg, []))
            print("    ✗ %-22s 读不了归档: %s" % (pkg, e))
            continue
        if tops:
            risky.append((pkg, sorted(tops)))
            print("    ⚠ %-22s 带 %s 目录成员 —— 解包**必须**保住软链"
                  % (pkg, "/".join(sorted(tops))))
        else:
            print("    ✓ %-22s 无 usrmerge 目录成员" % pkg)

    ok, why = postinst_keeps_links()
    if not ok:
        sys.stderr.write("\n[extract] ✗ %s\n" % why)
        sys.stderr.write(
            "[extract]   ⇒ %d 个包会踩坏软链: %s\n"
            % (len(risky), ", ".join(p for p, _ in risky)))
        sys.stderr.write(
            "[extract]   ⇒ 不许出包。解包必须用 "
            "`dpkg-deb --fsys-tarfile <deb> | tar -x --keep-directory-symlink -C /`\n")
        return 1

    if risky:
        print("[extract] ✓ postinst 用的是保软链的解法 (%s)" % KEEP_LINK_FLAG)
        print("[extract]   带目录成员的包 (%d 个): %s"
              % (len(risky), ", ".join(p for p, _ in risky)))
        print("[extract]   —— 这些包解到 /usr/ 底下, 软链不动 ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
