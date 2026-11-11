# 变异测试: 故意把 `scripts/check_eth.sh` 的 ping 成功判据改回坏样子, 看
# tests/test_check_eth_ping.py 会不会红。
#
# 为什么要有这么个东西: 「测试全绿」本身说明不了什么 —— 断言写错了、
# 写松了、或者压根没跑到, 都是绿的。唯一能证明这测试**咬得动**的办法,
# 就是亲手把代码改坏, 看它是不是真的红。
#
# 这一份守的是 2026-11-11 查出来的坑:
#   `grep -qP '^\d+ packets transmitted, \1 received'` —— `\1` 是反向引用,
#   而模式里**没有捕获组** ⇒ GNU grep 编译阶段就报错并 exit 2 ⇒ 那个 `if`
#   **恒为假** ⇒ `_try_ping` 永远走「未通」分支 ⇒ 网络好好的, 诊断脚本
#   却恒定报「三级全败」⇒ 工人照它排障 ⇒ 去追一个不存在的故障。
#
# 做法: **每个变异单开一个全新目录**, 把脚本复制进去, 在副本上改一处,
# 用 AGING_CHECK_ETH_SCRIPT 指过去跑测试。
# 被改的那个文件从头到尾**只写一次** —— 全程不覆写任何已存在的文件。
#
# 跑法: .venv/bin/python tests/_mutate_check_eth_ping.py
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests._tmp import mkdtemp                       # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(REPO, ".venv", "bin", "python")
TEST = "tests.test_check_eth_ping"
SCRIPT_REL = "scripts/check_eth.sh"
# ⚠️ MUTATIONS 里那几处**必须写字面量** —— tests/_sweep_anchors.py 要静态查
#    锚点, 写成变量它就 literal_eval 不了 ⇒ 那条变异从来没被静态验过。

# 每个变异: (文件, from, to, 说明)。from 必须在源文件里**找得到且唯一**。
MUTATIONS = [
    (
        "scripts/check_eth.sh",
        "grep -qP '^\\d+ packets transmitted, [1-9]\\d* received'",
        "grep -qP '^\\d+ packets transmitted, \\1 received'",
        "ETH: 判据回到**没有捕获组却用 \\1 反向引用**的老写法 ⇒ grep 编译就报"
        "错 ⇒ 那个 if 恒为假 ⇒ 永远判「未通」",
    ),
    (
        "scripts/check_eth.sh",
        "grep -qP '^\\d+ packets transmitted, [1-9]\\d* received'",
        "grep -qP '^\\d+ packets transmitted, [0-9]\\d* received'",
        "ETH: 判据允许「收到 0 个」也算通 ⇒ 不通也报通",
    ),
    (
        "scripts/check_eth.sh",
        "grep -qP '^\\d+ packets transmitted, [1-9]\\d* received'",
        "grep -q 'packets transmitted'",
        "ETH: 判据退化成「只要跑过 ping 就算通」⇒ 没通也报通",
    ),
]


def main():
    root = mkdtemp(prefix="mut_ethping_")
    print(f"副本都在 {root} 下面\n")

    bad = 0
    for i, (rel, frm, to, desc) in enumerate(MUTATIONS):
        srcp = os.path.join(REPO, rel)
        with open(srcp, encoding="utf-8") as f:
            orig = f.read()
        if orig.count(frm) != 1:
            print(f"\n✗ {desc}\n   → 锚点出现 {orig.count(frm)} 次(要恰好 1 次): "
                  f"{frm[:70]!r}")
            bad += 1
            continue

        work = os.path.join(root, "m%02d" % i)
        os.makedirs(os.path.join(work, "scripts"), exist_ok=True)
        # 被改的那份**只写一次** —— 全程不覆写任何已存在的文件。
        outp = os.path.join(work, rel)
        with open(outp, "w", encoding="utf-8") as f:
            f.write(orig.replace(frm, to))
        shutil.copy2(os.path.join(REPO, "config.yaml"),
                     os.path.join(work, "config.yaml"))

        env = dict(os.environ)
        env["AGING_CHECK_ETH_SCRIPT"] = outp
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
