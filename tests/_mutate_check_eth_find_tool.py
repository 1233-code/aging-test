# 变异测试: 故意把 `scripts/check_eth.sh` 找工具的口径改坏, 看
# tests/test_check_eth_find_tool.py 会不会红。
#
# 为什么要有这么个东西: 「测试全绿」本身说明不了什么 —— 断言写错了、
# 写松了、或者压根没跑到, 都是绿的。唯一能证明这测试**咬得动**的办法,
# 就是亲手把代码改坏, 看它是不是真的红。
#
# 这一份守的是 2026-11-11 **上板实测**的坑:
#   脚本用 `command -v ethtool` ⇒ `/usr/sbin` 不在非登录 shell 的 PATH 里
#   ⇒ 脚本报「未装 ethtool」, 而同一台机器 `/api/deps/check` 报「装好了」
#   ⇒ 两个口径互相打脸 ⇒ 工人照它排障 ⇒ 去追一个不存在的故障。
#
# 做法: **每个变异单开一个全新目录**, 把脚本复制进去, 在副本上改一处,
# 用 AGING_CHECK_ETH_SCRIPT 指过去跑测试。
# 被改的那个文件从头到尾**只写一次** —— 全程不覆写任何已存在的文件。
#
# 跑法: .venv/bin/python tests/_mutate_check_eth_find_tool.py
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests._tmp import mkdtemp                       # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(REPO, ".venv", "bin", "python")
TEST = "tests.test_check_eth_find_tool"
# ⚠️ MUTATIONS 里那几处**必须写字面量** —— tests/_sweep_anchors.py 要静态查
#    锚点, 写成变量它就 literal_eval 不了 ⇒ 那条变异从来没被静态验过。

# 每个变异: (文件, from, to, 说明)。from 必须在源文件里**找得到且唯一**。
MUTATIONS = [
    (
        "scripts/check_eth.sh",
        'ETHTOOL="$(find_tool ethtool || true)"',
        'ETHTOOL="$(command -v ethtool || true)"',
        "ETH: 退回裸 `command -v` ⇒ PATH 里没有 /usr/sbin ⇒ 误报「未装 "
        "ethtool」⇒ **同一台机器两个口径**",
    ),
    (
        "scripts/check_eth.sh",
        "    for d in /usr/local/sbin /usr/sbin /sbin /usr/local/bin; do",
        "    for d in /usr/local/bin; do",
        "ETH: 把 /usr/sbin 等目录从查找范围里去掉 ⇒ ethtool/udhcpc/ip 全找不到",
    ),
    (
        "scripts/check_eth.sh",
        "    if [ -n \"$TOOL_DIR\" ] && [ -x \"$TOOL_DIR/bin/$name\" ]; then\n"
        "        printf \"%s\" \"$TOOL_DIR/bin/$name\"\n"
        "        return 0\n"
        "    fi",
        "    if [ -n \"$TOOL_DIR\" ] && [ -x \"$TOOL_DIR/bin/$name\" ]; then\n"
        "        printf \"%s\" \"$TOOL_DIR/bin/$name\"\n"
        "    fi",
        "ETH: 随包目录命中时**不 return 0** ⇒ 找不到的工具被判成「有」"
        "⇒ 后面跑起来才炸",
    ),
    (
        "scripts/check_eth.sh",
        'IP="$(find_tool ip || true)"',
        'IP="$(command -v ip || true)"',
        "ETH: `ip` 退回裸 command -v ⇒ 取不到 IP ⇒ 诊断结论跟实际不符",
    ),
    (
        "scripts/check_eth.sh",
        'PING="$(find_tool ping || true)"',
        'PING="$(command -v ping || true)"',
        "ETH: `ping` 退回裸 command -v ⇒ 出网那一项判不了",
    ),
]


def main():
    root = mkdtemp(prefix="mut_ethfindtool_")
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
