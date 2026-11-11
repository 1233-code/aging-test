"""回归测试: `scripts/check_eth.sh` 的 ping 成功判据(2026-11-11 查出来的)。

起因:
    脚本里判断「ping 通了」的那一行原来写的是:
        grep -qP '^\\d+ packets transmitted, \\1 received'
    —— `\\1` 是**反向引用**, 而整个模式里**一个捕获组都没有** ⇒ GNU grep 在
    **编译阶段**就报 `reference to non-existent subpattern` 并 `exit 2`
    ⇒ 这个 `if` **恒为假** ⇒ `_try_ping` **永远走「未通」分支**
    ⇒ 哪怕网络完全正常, 这个诊断脚本也恒定报「三级全败」
    ⇒ 工人照它排障会去追一个**不存在的网络故障** ⇒ 可能把好板子误判成坏板子。

这个测试直接拿**系统真 grep**(`/usr/bin/grep`, 产线板同版 GNU grep 3.4)
去验脚本里那个模式:
  ① 「收到了包」⇒ 必须**匹配**;
  ② 「一个包都没收到」⇒ 必须**不匹配**;
  ③ 模式本身**不许编译报错**(grep exit 2 = 模式非法)—— 这一条就是当年
     那个 bug 的判别器。

⚠️ 必须用 `/usr/bin/grep` 绝对路径: Claude Code 的 Bash 工具里 `grep`
   是个包了 ugrep 的函数, 行为不一样。

跑法: .venv/bin/python -m tests.test_check_eth_ping
"""
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 变异脚本会把**改坏的那一份**指过来(AGING_CHECK_ETH_SCRIPT)。
SCRIPT = os.environ.get("AGING_CHECK_ETH_SCRIPT",
                         os.path.join(REPO, "scripts", "check_eth.sh"))
GREP = "/usr/bin/grep"

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


def find_pattern():
    """从脚本里把 _try_ping 用的那个 `grep -qP '...'` 模式抠出来。"""
    src = open(SCRIPT, encoding="utf-8").read()
    m = re.search(r"grep\s+-qP\s+'([^']*)'", src)
    return m.group(1) if m else None


def grep_exit(pattern, text):
    """拿系统 grep 跑一次, 返回退出码(0=匹配, 1=不匹配, 2=模式非法)。"""
    p = subprocess.run([GREP, "-qP", pattern], input=text,
                       capture_output=True, text=True)
    return p.returncode, (p.stderr or "").strip()


def main():
    section("【1】脚本里那个模式找得到、且编译得过去")
    pat = find_pattern()
    check(pat is not None, "脚本里有 `grep -qP '...'` 的模式", pat)
    if pat is None:
        return finish()

    code, err = grep_exit(pat, "3 packets transmitted, 3 received, 0% packet loss\n")
    check(code != 2, "模式**编译得过去**(grep 没报 'non-existent subpattern')",
          "exit=%d %s" % (code, err))

    section("【2】ping 通了 ⇒ 必须判「通」")
    code, err = grep_exit(pat, "3 packets transmitted, 3 received, 0% packet loss\n")
    check(code == 0, "收到包 ⇒ 匹配(判「通」)", "exit=%d %s" % (code, err))

    section("【3】一个包都没收到 ⇒ 必须判「不通」")
    code, err = grep_exit(pat, "3 packets transmitted, 0 received, 100% packet loss\n")
    check(code == 1, "0 received ⇒ **不**匹配(判「不通」)",
          "exit=%d %s" % (code, err))

    section("【4】单包收到也算通(1 个包)")
    code, err = grep_exit(pat, "1 packets transmitted, 1 received, 0% packet loss\n")
    check(code == 0, "1 received ⇒ 匹配", "exit=%d %s" % (code, err))

    section("【5】脚本里不再有「没有捕获组却用 \\1」的反向引用")
    src = open(SCRIPT, encoding="utf-8").read()
    check("\\1" not in pat, "模式里没有 \\1 反向引用", pat)

    return finish()


def finish():
    print("\n" + "─" * 60)
    if FAILS:
        print("✗ %d 项失败 / 共 %d 项" % (len(FAILS), PASSES + len(FAILS)))
        for f in FAILS:
            print("   · " + f)
        sys.exit(1)
    print("✓ 全部通过 (%d 项)" % PASSES)


if __name__ == "__main__":
    main()
