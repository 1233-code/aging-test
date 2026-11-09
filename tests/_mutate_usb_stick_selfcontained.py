# 变异测试: 故意把「U 盘上的工具换台板子还起不起来」这件事改坏, 看
# tests/test_usb_stick_selfcontained.py 会不会红。
#
# 为什么要有这么个东西: 「测试全绿」本身说明不了什么 —— 断言写错了、
# 写松了、或者压根没跑到, 都是绿的。唯一能证明这测试**咬得动**的办法,
# 就是亲手把代码改坏, 看它是不是真的红。
#
# 这一份守的是 2026-11-06 那批坏镜像出厂的病根(SN A10010000000058):
#   ① wrapper 的库搜索路径只列了**顶层** ⇒ `libceph-common.so.2`
#      (住在 `usr/lib/aarch64-linux-gnu/ceph/` 里)找不到 ⇒ fio rc=127
#      「起不来」⇒ eMMC 项 error ⇒ **好板子被当成坏板子返修**;
#   ② 构建时的检查**只警告不拦** ⇒ 坏镜像照样出厂;
#   ③ 检查脚本用的路径跟 wrapper 用的**不一致** ⇒ 验的是另一条路径, 白验。
#
# 做法: **每个变异单开一个全新目录**, 把 scripts/ + tests/ 复制进去,
# 在**副本**上做一处改动, 在副本目录里跑测试。仓库里 scripts/ 是产线在跑
# 的那份 —— 只在副本上改, 副本也不删(留在 /tmp 里可查)。
#
# 跑法: .venv/bin/python tests/_mutate_usb_stick_selfcontained.py
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests._tmp import mkdtemp                       # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(REPO, ".venv", "bin", "python")
TEST = "tests.test_usb_stick_selfcontained"

#: 副本里要带的东西 —— **少一个测试就测不到**
COPY_TREE = ("scripts",)
COPY_TESTS = ("test_usb_stick_selfcontained.py", "_tmp.py")

# 每个变异: (文件, from, to, 说明)。from 必须**找得到且唯一**。
MUTATIONS = [
    (
        "scripts/usb_wrapper.sh.tpl",
        '         "$_sysroot"/usr/lib/*/*/ "$_sysroot"/lib/*/*/; do',
        "         ; do",
        "wrapper 的搜索路径又退回只有顶层 ⇒ 库住在子目录里的就断了"
        "(2026-11-06 出厂那批坏镜像的病根)",
    ),
    (
        "scripts/build_usb_image.sh",
        'WRAPPER_TPL="$PROJECT_ROOT/scripts/usb_wrapper.sh.tpl"',
        'WRAPPER_TPL="$PROJECT_ROOT/scripts/wrapper.sh.tpl"',
        "构建脚本不再用那份唯一模板 ⇒ 构建出来的盘和打补丁的盘迟早对不上",
    ),
    (
        "scripts/build_usb_image.sh",
        '        die "这根盘在**别的板子**上起不来',
        '        warn "这根盘在**别的板子**上起不来',
        "检查不通过只警告 ⇒ **坏镜像照样出厂**(2026-11-06 就是这么漏出去的)",
    ),
    (
        "scripts/check_usb_selfcontained.py",
        "    for rp in runpath_dirs:\n"
        "        if os.path.exists(os.path.join(rp, name)):\n"
        '            return "runpath", rp\n',
        "    for rp in runpath_dirs:\n"
        "        if os.path.exists(os.path.join(rp, name)):\n"
        "            pass\n",
        "检查脚本不再报「只能靠写死路径找到」这一类(最危险的那类!)",
    ),
    (
        "scripts/check_usb_selfcontained.py",
        'LIB_GLOBS = (\n'
        '    "usr/lib/*/", "usr/lib/", "lib/*/", "lib/",\n'
        '    "usr/lib/*/*/", "lib/*/*/",\n'
        ")",
        'LIB_GLOBS = (\n'
        '    "usr/lib/*/", "usr/lib/", "lib/*/", "lib/",\n'
        ")",
        "检查脚本用的路径跟 wrapper 不一致 ⇒ 验的是另一条路径(白验)",
    ),
    (
        "scripts/check_usb_selfcontained.py",
        'ALWAYS = {\n'
        '    "linux-vdso.so.1", "ld-linux-aarch64.so.1", "ld-linux.so.2",\n'
        "}",
        'ALWAYS = {\n'
        '    "linux-vdso.so.1", "ld-linux-aarch64.so.1", "ld-linux.so.2",\n'
        '    "libceph-common.so.2",\n'
        "}",
        "把 libceph-common.so.2 当成「到处都有」⇒ 它断不断都不报",
    ),
]


def build_copy(dst: str, rel: str, new_text: str):
    for tree in COPY_TREE:
        shutil.copytree(os.path.join(REPO, tree), os.path.join(dst, tree),
                        dirs_exist_ok=True)
    os.makedirs(os.path.join(dst, "tests"), exist_ok=True)
    for f in COPY_TESTS:
        shutil.copy2(os.path.join(REPO, "tests", f),
                     os.path.join(dst, "tests", f))

    outp = os.path.join(dst, rel)
    os.makedirs(os.path.dirname(outp), exist_ok=True)
    with open(outp, "w", encoding="utf-8") as f:
        f.write(new_text)


def run_one(work: str):
    env = dict(os.environ)
    env["PYTHONPATH"] = work
    proc = subprocess.run(
        [PY, "-m", TEST], cwd=work, env=env,
        capture_output=True, text=True, timeout=300,
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode != 0, out.count("✗"), out


def main():
    root = mkdtemp(prefix="mut_stickself_")
    print(f"副本都在 {root} 下面(进程退出时自动删掉)\n")

    bad = 0
    for i, (rel, frm, to, desc) in enumerate(MUTATIONS):
        srcp = os.path.join(REPO, rel)
        with open(srcp, encoding="utf-8") as f:
            orig = f.read()
        if frm not in orig:
            print(f"\n✗ {desc}\n   → 变异点没找到(源文件变了?): {frm[:70]!r}")
            bad += 1
            continue
        if orig.count(frm) > 1:
            print(f"\n✗ {desc}\n   → 锚点不唯一(出现 {orig.count(frm)} 次) ⇒ "
                  f"`replace` 会把所有出现处一起改 ⇒ 证据不成立")
            bad += 1
            continue

        work = os.path.join(root, f"m{i:02d}")
        os.makedirs(work, exist_ok=True)
        build_copy(work, rel, orig.replace(frm, to))

        red, fails, out = run_one(work)
        if red and fails == 0:
            print(f"\n✗ {desc}\n   → 红了, 但一个 ✗ 都没有 —— "
                  f"这是**崩了**, 不是被断言咬住的, 不算数:\n{out[-800:]}")
            bad += 1
        elif red:
            print(f"\n✓ {desc}\n   → 测试变红 ({fails} 项失败) —— 咬得动")
        else:
            print(f"\n✗ {desc}\n   → 测试还是绿的! 这个变异没被守住")
            bad += 1

    print("\n" + ("全部变异都被抓住了 ✓" if bad == 0
                  else f"有 {bad} 个变异没被抓住 ✗"))
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
