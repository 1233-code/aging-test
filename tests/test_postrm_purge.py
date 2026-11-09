"""回归测试: purge 时**不许误删目标板子上的文件** (2026-10-29)。

`deb/postrm` 干的是一件危险的事: 它把当初 `dpkg-deb -x` 铺到 `/` 下的工具
文件**删回去**。而那台板子上的文件, 有的是**我们铺的**, 有的是**人家 apt
正式装的** —— 删错了就是把产线板子拆了。

判据必须能证明「这份是我们铺的那份」, 光靠"看着像"不行。这里把当时的坑
逐个钉成用例:

  ① 目标机上**正式装了这个包** ⇒ 文件是人家的 ⇒ 不删
     (最要命的一种: 目标机装的是**旧版本**, 我们照样解包盖了上去, 删了
      就等于把人家的包装拆了, dpkg 库里还写着它拥有这个文件)
  ② 文件**内容被人换过** (后来 apt 装了新版本) ⇒ 指纹对不上 ⇒ 不删
  ③ 清单里**没有指纹** (老版本装的) ⇒ 证据不足 ⇒ 不删
  ④ **dpkg 状态库整个读不出来** ⇒ 判据不可用 ⇒ 一个都不删
  ⑤ 路径不在系统白名单里 ⇒ 根本不碰

而反过来, 真正属于我们、也没人动过的文件, **必须删掉** —— 不然 purge
完板子上留一地残骸。

跑法: .venv/bin/python -m tests.test_postrm_purge
"""
import hashlib
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
POSTRM = REPO / "deb" / "postrm"

FAIL = []
CHECKS = 0


def check(cond, msg, detail=""):
    global CHECKS
    CHECKS += 1
    if cond:
        print(f"  ✓ {msg}")
    else:
        print(f"  ✗ {msg}" + (f"\n      {detail}" if detail else ""))
        FAIL.append(msg)


def section(title):
    print(f"\n== {title} ==")


def sha256_of(path: Path) -> str:
    """跟 postrm 里的 fingerprint() 同一套算法: <工具>:<值>。"""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return f"sha256sum:{h.hexdigest()}"


# ─────────────────── 伪造的 dpkg-query (状态库) ───────────────────

FAKE_DPKG_QUERY = r"""#!/bin/bash
# 假的 dpkg-query —— 只答最后那个包名的状态, 供 postrm 判归属用。
pkg=""
for a in "$@"; do pkg="$a"; done
case "$pkg" in
    dpkg|coreutils|libc6) echo "install ok installed"; exit 0 ;;
esac
if [ -f "$TABLE" ]; then
    st="$(grep -E "^${pkg} " "$TABLE" 2>/dev/null | head -1 | cut -d' ' -f2-)"
    if [ -n "$st" ]; then echo "$st"; exit 0; fi
fi
echo "dpkg-query: no packages found matching $pkg" >&2
exit 1
"""


def make_env(tmp: Path, statuses: dict) -> dict:
    """伪造一套 dpkg 状态库 —— statuses: {包名: 状态字符串}。"""
    bindir = tmp / "bin"
    bindir.mkdir(exist_ok=True)
    table = tmp / "status-table"
    table.write_text("".join(f"{k} {v}\n" for k, v in statuses.items()),
                     encoding="utf-8")
    q = bindir / "dpkg-query"
    q.write_text(FAKE_DPKG_QUERY, encoding="utf-8")
    q.chmod(0o755)
    return {
        "PATH": f"{bindir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
        "TABLE": str(table),
    }


def run_postrm(tmp: Path, env_extra: dict, files_file: Path) -> str:
    env = dict(os.environ)
    env.update(env_extra)
    env["DESTDIR"] = str(tmp / "root")
    env["AGING_TEST_FILES_FILE"] = str(files_file)
    env["AGING_TEST_DATA_DIR"] = str(tmp / "root" / "var" / "lib" / "aging-test")
    r = subprocess.run(["bash", str(POSTRM), "purge"],
                       capture_output=True, text=True, timeout=120, env=env)
    if r.returncode != 0:
        print(f"      [postrm 退出码 {r.returncode}]\n{r.stdout}\n{r.stderr}")
    return r.stdout + r.stderr


# ══════════════════════ 造场景 ══════════════════════

def build_scenario(tmp: Path):
    """在假根里摆出五种文件, 并生成对应的清单。

    返回 (清单内容, 各文件的绝对路径)
    """
    root = tmp / "root"
    (root / "usr" / "bin").mkdir(parents=True, exist_ok=True)
    (root / "usr" / "lib" / "x").mkdir(parents=True, exist_ok=True)
    (root / "etc").mkdir(parents=True, exist_ok=True)

    # ① 包已正式安装 + 内容没变 → 必须保留
    a = root / "usr" / "bin" / "tool-installed"
    a.write_text("installed-pkg-bytes\n", encoding="utf-8")

    # ② 包没装 + 内容没变 → 必须删掉 (我们铺的, 没人动过)
    b = root / "usr" / "bin" / "tool-ours"
    b.write_text("our-bytes\n", encoding="utf-8")

    # ③ 包没装, 但内容被人换过 → 指纹对不上 → 必须保留
    c = root / "usr" / "bin" / "tool-replaced"
    c.write_text("original-bytes\n", encoding="utf-8")

    # ④ 包没装, 是个软链 → 指纹记的是链接目标
    d = root / "usr" / "bin" / "tool-link"
    if d.exists() or d.is_symlink():
        d.unlink()
    os.symlink("/bin/busybox", d)

    # ④b 软链被人换过 (指向变了) → 指纹对不上 → 必须保留
    d2 = root / "usr" / "bin" / "tool-link-changed"
    if d2.exists() or d2.is_symlink():
        d2.unlink()
    os.symlink("/bin/sh", d2)

    # ⑤ 不在白名单里的路径 (比如 /etc 下的东西) → 根本不碰
    e = root / "etc" / "netconfig"
    e.write_text("not-ours\n", encoding="utf-8")

    # 清单:
    #   tool-installed → pkg-installed (状态 ok installed)
    #   tool-ours      → pkg-ours      (状态 not-installed)
    #   tool-replaced  → pkg-ours      (指纹是**换之前**的 ⇒ 对不上)
    #   tool-link      → pkg-ours      (link:/bin/busybox)
    #   netconfig      → pkg-ours      (不在白名单)
    lines = [
        f"pkg-installed\t{sha256_of(a)}\t/usr/bin/tool-installed",
        f"pkg-ours\t{sha256_of(b)}\t/usr/bin/tool-ours",
        f"pkg-ours\t{sha256_of(c).replace('original', 'original')}\t/usr/bin/tool-replaced",
        f"pkg-ours\tlink:/bin/busybox\t/usr/bin/tool-link",
        f"pkg-ours\tlink:/bin/busybox\t/usr/bin/tool-link-changed",
        f"pkg-ours\t{sha256_of(e)}\t/etc/netconfig",
    ]
    # tool-replaced 的指纹要故意写成"换过的内容"的哈希 ⇒ 模拟事后被人替换
    lines[2] = f"pkg-ours\tsha256sum:{'0' * 64}\t/usr/bin/tool-replaced"

    files_file = tmp / "files.txt"
    files_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return files_file, {"installed": a, "ours": b, "replaced": c,
                        "link": d, "link_changed": d2, "etc": e}


# ══════════════════════ 跑 ══════════════════════

section("① 正常 purge —— 该删的删, 不该删的一个不许动")

with tempfile.TemporaryDirectory(prefix="aging-postrm-") as td:
    tmp = Path(td)
    env = make_env(tmp, {
        "pkg-installed": "install ok installed",
        "pkg-ours": "deinstall ok config-files",
    })
    files_file, f = build_scenario(tmp)

    out = run_postrm(tmp, env, files_file)

    check(not f["ours"].exists(),
          "我们铺的、没人动过的文件被删掉了 (pkg 未安装 + 指纹对得上)")
    check(f["installed"].exists(),
          "目标机上正式装着的包的文件**保留**了 (状态 ok installed)")
    check(f["replaced"].exists(),
          "内容被人换过的文件**保留**了 (指纹对不上)")
    check(not (f["link"].exists() or f["link"].is_symlink()),
          "软链: 指纹记的是链接目标, 对得上且包没装 ⇒ 删掉")
    check(f["link_changed"].exists() or f["link_changed"].is_symlink(),
          "软链被人换过指向 ⇒ 指纹对不上 ⇒ 保留")
    check(f["etc"].exists(),
          "不在系统白名单里的路径 (/etc/...) 根本没碰")
    check("删了 1 个" in out or "删了" in out,
          "日志里说了删了几个", out[:400])

section("② 清单里没有指纹 (老版本装的) —— 证据不足, 一个都不删")

with tempfile.TemporaryDirectory(prefix="aging-postrm-") as td:
    tmp = Path(td)
    env = make_env(tmp, {"pkg-ours": "deinstall ok config-files"})
    root = tmp / "root"
    (root / "usr" / "bin").mkdir(parents=True, exist_ok=True)
    legacy = root / "usr" / "bin" / "tool-legacy"
    legacy.write_text("legacy\n", encoding="utf-8")
    files_file = tmp / "files.txt"
    files_file.write_text(f"/usr/bin/tool-legacy\n", encoding="utf-8")

    run_postrm(tmp, env, files_file)
    check(legacy.exists(),
          "没有指纹的老清单条目**保留**了 (不能证明是我们铺的)")

section("③ dpkg 状态库读不出来 —— 判据不可用, 一个都不删")

with tempfile.TemporaryDirectory(prefix="aging-postrm-") as td:
    tmp = Path(td)
    # 伪造的 dpkg-query 对所有包都答不出来 ⇒ DB_OK=0
    env = make_env(tmp, {})
    bindir = tmp / "bin"
    (bindir / "dpkg-query").write_text(
        "#!/bin/bash\nexit 1\n", encoding="utf-8")
    (bindir / "dpkg-query").chmod(0o755)
    env["TABLE"] = str(tmp / "status-table")

    root = tmp / "root"
    (root / "usr" / "bin").mkdir(parents=True, exist_ok=True)
    keepme = root / "usr" / "bin" / "tool-keep"
    keepme.write_text("keep\n", encoding="utf-8")
    files_file = tmp / "files.txt"
    files_file.write_text(
        f"pkg-ours\t{sha256_of(keepme)}\t/usr/bin/tool-keep\n", encoding="utf-8")

    out = run_postrm(tmp, env, files_file)
    check(keepme.exists(),
          "状态库坏掉时文件**一个都没删** (宁可留残留, 也不误删)")
    check("一个文件都不删" in out or "删了 0 个" in out,
          "日志里明说了没删", out[:400])

section("④ 白名单之外的路徑 —— 永远不碰")

check("/etc/*" not in POSTRM.read_text(encoding="utf-8"),
      "postrm 的白名单里没有 /etc (配置类文件不归我们管)")
for must in ["/usr/bin/*", "/usr/lib/*", "/usr/sbin/*", "/bin/*"]:
    check(must in POSTRM.read_text(encoding="utf-8"),
          f"白名单里有 {must}")

section("⑤ postrm 用的是可靠的那个判据")

text = POSTRM.read_text(encoding="utf-8")
check("dpkg-query -W" in text,
      "用 dpkg-query -W 查状态库 (不是 dpkg -S 那个半废的文件索引)")
# 注释里当然会提到 dpkg -S (解释为什么不用它) —— 只查**代码**
code = "\n".join(ln for ln in text.splitlines()
                 if not ln.strip().startswith("#"))
check("dpkg -S" not in code,
      "postrm 的**代码里一次都没用 dpkg -S** 查归属",
      f"代码中出现 {code.count('dpkg -S')} 次")
check("sha256sum" in text and "md5sum" in text,
      "指纹用的是内容哈希 (sha256sum, 退化时 md5sum)")
check("DESTDIR" in text,
      "落点带 DESTDIR 前缀 —— 能在沙箱里真跑一遍, 不用碰真板子")

# ══════════════════════ 汇总 ══════════════════════
print(f"\n{'=' * 60}")
if FAIL:
    print(f"✗ {len(FAIL)}/{CHECKS} 项没过:")
    for x in FAIL:
        print(f"    - {x}")
    sys.exit(1)
print(f"✓ 全部通过 ({CHECKS} 项)")
