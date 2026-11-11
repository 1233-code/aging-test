# 变异测试: 故意把「check_deps.sh 认盘上的工具」改坏, 看
# tests/test_check_deps_stick_tools.py 会不会红。
#
# 为什么要有这么个东西: 「测试全绿」本身说明不了什么 —— 断言写错了、
# 写松了、或者压根没跑到, 都是绿的。唯一能证明这测试**咬得动**的办法,
# 就是亲手把代码改坏, 看它是不是真的红。
#
# 这一份守的是 2026-11-11 挖出来的坑:
#   U 盘版里 fio / glmark2-es2 是**盘上**的(`<盘>/tools/bin/`, 由
#   `scripts/build_usb_image.sh` 生成的 wrapper)⇒ **系统 PATH 里压根没有**
#   ⇒ 而 `check_deps.sh` 找工具**只看系统 PATH** ⇒ 一律报「缺关键工具」
#   ⇒ 汇总成「**拒绝出货**」—— 而工具好好地在盘上、测试项也真调得到它
#   (Python 侧的 `find_tool` 认 AGING_TOOL_DIR)⇒ 同一台机器两个说法。
#
# 做法: **每个变异单开一个全新目录**, 把 `scripts/check_deps.sh` **改坏**
# 的副本放进去, 用环境变量 `AGING_CHECK_DEPS_SCRIPT` 指给测试 ⇒ 测试
# 跑的是**改坏的那份**。仓库里那份一个字都不动。
#
# 跑法: .venv/bin/python tests/_mutate_check_deps_stick_tools.py
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests._tmp import mkdtemp                       # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(REPO, ".venv", "bin", "python")
TEST = "tests.test_check_deps_stick_tools"
SCRIPT_REL = "scripts/check_deps.sh"

# 每个变异: (文件, from, to, 说明)。from 必须在源文件里**找得到且唯一**。
#: ⚠️ 用**四元组**的标准形态 —— `tests/_sweep_anchors.py` 才扫得到
#:    (三元的它扫不了 ⇒ 锚点烂了没人知道)。
MUTATIONS = [
    (
        "scripts/check_deps.sh",
        '    if [ -n "$TOOL_DIR" ] && [ -x "$TOOL_DIR/bin/$name" ]; then',
        '    if false; then',
        "DEPS: find_tool **不认随包工具目录**了 ⇒ 盘上的 fio 又被说成「缺失」"
        "⇒ 拒绝出货(工具好好地在盘上)",
    ),
    (
        "scripts/check_deps.sh",
        '    TOOL_DIR="$PROJECT_ROOT/tools"',
        '    TOOL_DIR=""',
        "DEPS: 找不到「盘根的 tools/bin」⇒ 只靠 AGING_TOOL_DIR ⇒ "
        "工人直接跑脚本(没经 start.sh)时盘上的工具又看不见了",
    ),
    (
        "scripts/check_deps.sh",
        '    if _p="$(find_tool "$cmd")"; then\n'
        '        printf "    ✓ %-14s %s\\n" "$cmd" "${CRITICAL[$cmd]}"',
        '    if command -v "$cmd" > /dev/null 2>&1; then\n'
        '        printf "    ✓ %-14s %s\\n" "$cmd" "${CRITICAL[$cmd]}"',
        "DEPS: 关键工具那一栏又只看系统 PATH(老口径)⇒ U 盘版一律报缺",
    ),
]


def main():
    root = mkdtemp(prefix="mut_checkdeps_")
    print(f"副本都在 {root} 下面\n")

    src = os.path.join(REPO, SCRIPT_REL)
    with open(src, encoding="utf-8") as f:
        orig = f.read()

    bad = 0
    for i, (rel, frm, to, desc) in enumerate(MUTATIONS):
        if rel != SCRIPT_REL:
            print(f"\n✗ {desc}\n   → 还没支持改 {rel}")
            bad += 1
            continue
        if orig.count(frm) != 1:
            print(f"\n✗ {desc}\n   → 锚点出现 {orig.count(frm)} 次(要恰好 1 次): "
                  f"{frm[:70]!r}")
            bad += 1
            continue

        work = os.path.join(root, f"m{i:02d}")
        os.makedirs(os.path.join(work, "scripts"), exist_ok=True)
        # 被改的那份**只写一次** —— 全程不覆写任何已存在的文件。
        with open(os.path.join(work, SCRIPT_REL), "w", encoding="utf-8") as f:
            f.write(orig.replace(frm, to))
        shutil.copy2(os.path.join(REPO, "config.yaml"),
                     os.path.join(work, "config.yaml"))

        env = dict(os.environ)
        env["PYTHONPATH"] = REPO
        env["AGING_CHECK_DEPS_SCRIPT"] = os.path.join(work, SCRIPT_REL)
        proc = subprocess.run([PY, "-m", TEST], cwd=REPO, env=env,
                              capture_output=True, text=True, timeout=300)
        out = (proc.stdout or "") + (proc.stderr or "")
        fails = out.count("✗")
        if proc.returncode != 0 and fails == 0:
            print(f"\n✗ {desc}\n   → 红了, 但一个 ✗ 都没有 (exit="
                  f"{proc.returncode}) —— 这是**崩了**, 不算数:\n{out[-600:]}")
            bad += 1
        elif proc.returncode != 0:
            print(f"\n✓ {desc}\n   → 测试变红 (exit={proc.returncode}, "
                  f"{fails} 项失败) —— 咬得动")
        else:
            print(f"\n✗ {desc}\n   → 测试还是绿的! 这个变异没被守住")
            bad += 1

    print("\n" + ("全部变异都被抓住了 ✓" if bad == 0
                  else f"有 {bad} 个变异没被抓住 ✗"))
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
