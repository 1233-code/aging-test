"""回归测试: 解包**不许**踩坏 usrmerge 的软链 (2026-10-29 新机装机事故)。

**事故经过**
    装机脚本用 `dpkg-deb -x <deb> /` 把随包工具解到根目录。
    `dpkg-deb -x` 底下就是**裸 tar** —— 它遇到 tar 包里的 `./lib/`
    这种**目录成员**时, 会**把 `/lib` 这个软链删掉、换成一个真目录**!

    于是:
      · `/lib/ld-linux-aarch64.so.1`(动态链接器)当场消失;
      · **整台板子上所有动态链接的程序全部 execve 报 ENOENT**
        (bash 打印的是绝对路径: "/usr/bin/grep: No such file or directory");
      · 只有**已经在内存里**的 dpkg / bash 还能接着跑 —— 于是后面每个包
        都静默跳过 (`dpkg-deb -f` 读不到包名 ⇒ continue), 最后 postinst
        死在 `python3 -m venv` 上, dpkg 报:
          "post-installation script subprocess returned error exit status 1"

    这就是新设备上 `dpkg -i aging-test_*.deb` 失败的真正原因。

**这个测试守的**
  ① 老解法 (`tar -x`) **确实**会把软链踩坏 —— 先证明这道闸抓得住,
     免得测试自己变成空闸;
  ② 新解法 (`tar -x --keep-directory-symlink`) **保得住**软链,
     而且内容**落到软链指向的地方** (./lib/... ⇒ /usr/lib/...);
  ③ `deb/postinst` 的**代码**里用的是新解法, 不是 `dpkg-deb -x ... /`;
  ④ `deb/postinst` 的**代码**里还留着「每解一个包就只读核一遍软链」
     的安全网 —— 坏了要**停手**, 不能闷头把板子拆完。

**它不碰任何外部包**: tar 夹具是本测试**自己造的**。
跑法: .venv/bin/python -m tests.test_extract_safety
"""
import os
import re
import subprocess
import sys
import tarfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests._tmp import mkdtemp  # noqa: E402

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


def section(title):
    print("\n" + title)


# ── 造一个假的 usrmerge 根目录 ──────────────────────────────────────────
def make_fake_root(base):
    """长得跟 Ubuntu 20.04 (usrmerge) 一样的根目录。

    /lib → usr/lib、/bin → usr/bin、/sbin → usr/sbin、/lib64 → usr/lib64,
    动态链接器挂在 /usr/lib 底下 (顺着 /lib 才解析得到)。
    """
    root = os.path.join(base, "root")
    os.makedirs(root)
    for real in ("usr/bin", "usr/sbin", "usr/lib", "usr/lib64"):
        os.makedirs(os.path.join(root, real), exist_ok=True)
    loader = os.path.join(root, "usr/lib/ld-linux-aarch64.so.1")
    with open(loader, "w") as f:
        f.write("fake loader\n")
    for link, target in (("bin", "usr/bin"), ("sbin", "usr/sbin"),
                         ("lib", "usr/lib"), ("lib64", "usr/lib64")):
        os.symlink(target, os.path.join(root, link))
    return root


def make_fixture_tar(path):
    """造一个 tar: 里面**有** `./lib/` 这个目录成员 (就是踩雷的那个)。"""
    def add(tar, name, data=b"x"):
        info = tarfile.TarInfo(name)
        info.size = len(data)
        info.mtime = 0
        tar.addfile(info, __import__("io").BytesIO(data))

    with tarfile.open(path, "w") as tar:
        d = tarfile.TarInfo("./lib")
        d.type = tarfile.DIRTYPE
        d.mode = 0o755
        d.mtime = 0
        tar.addfile(d)
        add(tar, "./lib/systemd/system/thing.service", b"[Unit]\n")
        add(tar, "./usr/bin/thing", b"#!/bin/sh\n")
    return path


def extract(root, tar_path, keep_links):
    """按 postinst 的解法解包 (keep_links=True) 或老办法 (False)。"""
    cmd = ["tar", "-x"]
    if keep_links:
        cmd.append("--keep-directory-symlink")
    cmd += ["-C", root, "-f", tar_path]
    return subprocess.run(cmd, stdout=subprocess.DEVNULL,
                          stderr=subprocess.PIPE).returncode == 0


# ══════════════════════════════════════════════════════════════════════
section("【1】老解法 (裸 tar -x) —— 确实会把 /lib 软链踩坏 (证明闸抓得住)")

base = mkdtemp(prefix="aging-extract-old-")
root = make_fake_root(base)
tar_path = make_fixture_tar(os.path.join(base, "fixture.tar"))
ok = extract(root, tar_path, keep_links=False)
check(ok, "老解法把包解开了", "")
check(not os.path.islink(os.path.join(root, "lib")),
      "老解法把 /lib 软链**换成了真目录** —— 这就是事故的根因",
      os.path.realpath(os.path.join(root, "lib")))
check(not os.path.exists(os.path.join(root, "lib/ld-linux-aarch64.so.1")),
      "动态链接器 (/lib/ld-linux-aarch64.so.1) 解析不到了 —— "
      "板子上所有动态链接的程序都会死")

section("【2】新解法 (tar -x --keep-directory-symlink) —— 软链保得住")

base = mkdtemp(prefix="aging-extract-new-")
root = make_fake_root(base)
tar_path = make_fixture_tar(os.path.join(base, "fixture.tar"))
ok = extract(root, tar_path, keep_links=True)
check(ok, "新解法把包解开了", "")
for link in ("lib", "bin", "sbin", "lib64"):
    check(os.path.islink(os.path.join(root, link)),
          "/%s 还是软链 (指向 %s)" % (link, os.readlink(
              os.path.join(root, link)) if os.path.islink(
                  os.path.join(root, link)) else "?"))
check(os.path.exists(os.path.join(root, "lib/ld-linux-aarch64.so.1")),
      "动态链接器还解析得到 (/lib → usr/lib)")
check(os.path.exists(os.path.join(root, "usr/lib/systemd/system/thing.service")),
      "内容落到了软链指向的地方 (/usr/lib/systemd/system/thing.service), "
      "而不是把软链拆了")
check(os.path.exists(os.path.join(root, "usr/bin/thing")),
      "普通文件也照常落在 /usr/bin")

section("【3】postinst 的代码 —— 用的是新解法, 不是 dpkg-deb -x")

postinst = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "deb", "postinst")
with open(postinst, encoding="utf-8") as f:
    src = f.read()
code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))

check("--keep-directory-symlink" in code,
      "postinst 的代码里有 --keep-directory-symlink")
# 只认**调用形式** (dpkg-deb -x "$deb" /) —— 注释和报错文案里提到
# 这个名字不算数 (那是在解释「为什么不能用它」)
call_re = re.compile(r'dpkg-deb\s+-x\s+["\']?\$\{?deb')
bad_calls = [l.strip() for l in code.splitlines() if call_re.search(l)]
check(not bad_calls,
      "postinst 的代码里**没有** dpkg-deb -x 调用 (那会把软链踩坏)",
      bad_calls)
check("dpkg-deb --fsys-tarfile" in code,
      "postinst 用的是 dpkg-deb --fsys-tarfile | tar -x")
check("--fsys-tarfile" in code and "-C /" in code,
      "解包的落点是 / (解到根目录)")

section("【4】安全网 —— 每解一个包就只读核一遍软链, 坏了要停手")

check("check_usrmerge_links" in code,
      "postinst 里有 check_usrmerge_links (装完核一遍软链)")
check("USRMERGE_LINKS" in code,
      "postinst 记得住装之前哪些是软链")
check("die" in code,
      "发现软链坏了会**停手**(die), 而不是闷头把板子拆完")
# 安全网是**只读**的 —— 不许自己去删改 /lib
check("rm -rf" not in code,
      "postinst 的代码里**没有** rm -rf —— 脚本不擅自删改 /lib /bin")

# 探针: 跑一个**动态链接**的系统命令 —— 加载器没了它就 execve 报 ENOENT。
# ⚠️ 这条独立于「软链那圈」: 板子若**早就**被弄坏过 (/lib 已经是真目录),
#    那时 USRMERGE_LINKS 记下来是空的 —— 只查软链查不出来。
fn = code.split("check_usrmerge_links() {")
check(len(fn) > 1 and "ls /" in fn[1].split("}")[0],
      "check_usrmerge_links 里有个**动态链接探针** (跑一个系统命令)")
probe = "ls / >/dev/null 2>&1"
check(probe in code,
      "postinst 里用的是 `ls / >/dev/null 2>&1` 当探针")
# 装机**之前**也要探一次 —— 板子本来就废的话, 一件事都别做
check(code.index(probe) < code.index('extract_keep_links "$deb"'),
      "探针在**第一次解包之前**就跑了 —— 板子本来就废的话一件事都不做")

section("【5】数据目录 —— 清单文件写之前目录必须已经建好")

check(src.index("建数据目录") < src.index("装随包带的系统工具"),
      "建数据目录**排在**装工具**前面** —— 否则清单 (.tmp) 写不出来, "
      "purge 时永远收不干净")
check("install -d -m 0755 \"$DATA_DIR\"" in code,
      "数据目录是用 install -d 建的")


print("\n" + ("全部通过" if not FAILS else f"有失败项 ({len(FAILS)})"))
for f in FAILS:
    print("  ✗ " + f)
sys.exit(0 if not FAILS else 1)
