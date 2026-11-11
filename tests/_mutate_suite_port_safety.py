# 变异测试: 故意把「回归套件不许杀产线服务」这道防线改坏, 看
# tests/test_suite_port_safety.py 会不会红。
#
# 为什么要有这么个东西: 「测试全绿」本身说明不了什么 —— 断言写错了、
# 写松了、或者压根没跑到, 都是绿的。唯一能证明这测试**咬得动**的办法,
# 就是亲手把代码改坏, 看它是不是真的红。
#
# 这一份守的是 2026-11-11 的**真实停产事故**:
#   回归里有个测试拿临时目录里的 start.sh 跑 `stop`, 没改 PORT
#   ⇒ 默认 8000 ⇒ `kill_port_holder 8000` ⇒ **产线服务被 SIGTERM 掉**
#   ⇒ 产线当场断服务, 而日志里查不到凶手(凶手就是回归自己)。
#
# 做法: **每个变异单开一个全新目录**, 把被改的文件复制进去, 在副本上改一处,
# 用 AGING_START_SH / AGING_TESTS_DIR 指过去跑测试。
# 被改的那个文件从头到尾**只写一次** —— 全程不覆写任何已存在的文件。
#
# 跑法: .venv/bin/python tests/_mutate_suite_port_safety.py
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests._tmp import mkdtemp                       # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(REPO, ".venv", "bin", "python")
TEST = "tests.test_suite_port_safety"
# ⚠️ MUTATIONS 里那几处**必须写字面量** —— tests/_sweep_anchors.py 要静态查
#    锚点, 写成变量它就 literal_eval 不了 ⇒ 那条变异从来没被静态验过。

# 每个变异: (改哪个, from, to, 说明)。
#   "start" ⇒ 改 start.sh        "tests" ⇒ 改 tests/ 目录里的文件
# from 必须在源文件里**找得到且唯一**。
MUTATIONS = [
    (
        "start.sh",
        'if ! is_our_server "$holder"; then',
        'if false; then',
        "PORT: 去掉「只杀自己人」这道闸 ⇒ 端口上**是谁就杀谁** ⇒ "
        "回归又会把产线服务杀了(事故原样)",
        "start",
    ),
    (
        "start.sh",
        "*uvicorn*|*server.main*) return 0 ;;",
        "*nothing-matches-this*) return 0 ;;",
        "PORT: 「自己人」的认法认不出来了 ⇒ 自己的旧进程也不清 ⇒ "
        "**自愈路径坏掉**(盘换了/PID 文件丢了就起不来)",
        "start",
    ),
    (
        "tests/test_usb_app_mode.py",
        'env["PORT"] = str(test_port)',
        "",
        "PORT: 把「显式指定端口」那行删了 ⇒ 又回到默认值 8000 ⇒ "
        "**事故原样**",
        "tests",
    ),
    (
        "tests/test_usb_app_mode.py",
        "PROD_PORT = 8000",
        "PROD_PORT = 8001",
        "PORT: 把「产线端口是 8000」这个事实改错 ⇒ 盯着别的端口看",
        "tests",
    ),
    (
        "tests/run_all.py",
        "if holder_before != holder_after:",
        "if false:",
        "PORT: 把 run_all.py 的绊线短路 ⇒ 回归杀了产线服务**也不报**",
        "tests",
    ),
]


def main():
    root = mkdtemp(prefix="mut_portsafety_")
    print(f"副本都在 {root} 下面\n")

    bad = 0
    for i, (rel, frm, to, desc, kind) in enumerate(MUTATIONS):
        work = os.path.join(root, "m%02d" % i)
        os.makedirs(work, exist_ok=True)
        env = dict(os.environ)

        if kind == "start":
            srcp = os.path.join(REPO, rel)
            with open(srcp, encoding="utf-8") as f:
                orig = f.read()
            if orig.count(frm) != 1:
                print(f"\n✗ {desc}\n   → 锚点出现 {orig.count(frm)} 次(要恰好 1 次): "
                      f"{frm[:70]!r}")
                bad += 1
                continue
            outp = os.path.join(work, "start.sh")
            with open(outp, "w", encoding="utf-8") as f:
                f.write(orig.replace(frm, to))
            env["AGING_START_SH"] = outp
        else:
            # 把整个 tests/ 复制一份, 在副本上改那一个文件
            tdir = os.path.join(work, "tests")
            shutil.copytree(os.path.join(REPO, "tests"), tdir,
                            ignore=shutil.ignore_patterns("__pycache__"))
            outp = os.path.join(tdir, os.path.basename(rel))
            with open(outp, encoding="utf-8") as f:
                orig = f.read()
            if orig.count(frm) != 1:
                print(f"\n✗ {desc}\n   → 锚点出现 {orig.count(frm)} 次(要恰好 1 次): "
                      f"{frm[:70]!r}")
                bad += 1
                continue
            with open(outp, "w", encoding="utf-8") as f:
                f.write(orig.replace(frm, to))
            env["AGING_TESTS_DIR"] = tdir

        proc = subprocess.run([PY, "-m", TEST], cwd=REPO, env=env,
                              capture_output=True, text=True, timeout=600)
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
