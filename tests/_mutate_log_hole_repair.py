# 变异测试: 故意把「修日志空洞」这道工序改坏, 看
# tests/test_log_hole_repair.py 会不会红。
#
# 为什么要有这么个东西: 「测试全绿」本身说明不了什么 —— 断言写错了、
# 写松了、或者压根没跑到, 都是绿的。唯一能证明这测试**咬得动**的办法,
# 就是亲手把代码改坏, 看它是不是真的红。
#
# 这一份守的是 2026-11-11 在**产线盘**上实测到的坑:
#   `logs/server.log` 里有一段 956 个 NUL 字节的空洞 ⇒ **grep 把整个文件
#   当二进制** ⇒ `grep 什么 logs/server.log` 只打一行「Binary file … matches」,
#   **把真正的行全吞掉**
#   ⇒ 而手册教工人去 grep 这个日志 ⇒ 工人拿到静默的空
#   ⇒ 去追一个**不存在的故障**(84 条 WebSocket 记录就在那儿)。
#
# 做法: **每个变异单开一个全新目录**, 把 start.sh 复制进去, 在副本上改一处,
# 用 AGING_START_SH 指过去跑测试。
# 被改的那份**只写一次** —— 全程不覆写任何已存在的文件。
#
# 跑法: .venv/bin/python tests/_mutate_log_hole_repair.py
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests._tmp import mkdtemp                       # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(REPO, ".venv", "bin", "python")
TEST = "tests.test_log_hole_repair"
# ⚠️ MUTATIONS 里那几处**必须写字面量** —— tests/_sweep_anchors.py 要静态查
#    锚点, 写成变量它就 literal_eval 不了 ⇒ 那条变异从来没被静态验过。

# 每个变异: (文件, from, to, 说明)。from 必须在源文件里**找得到且唯一**。
MUTATIONS = [
    (
        "start.sh",
        "    if tr '\\000' '\\n' < \"$LOG_FILE\" > \"$LOG_FILE.fix\" 2>/dev/null; then",
        "    if tr '\\000' '' < \"$LOG_FILE\" > \"$LOG_FILE.fix\" 2>/dev/null; then",
        "LOG: 把 NUL **删掉**而不是换成换行 ⇒ **内容少了** ⇒ 日志被悄悄改短",
    ),
    (
        "start.sh",
        "    clean=$(tr -d '\\000' < \"$LOG_FILE\" 2>/dev/null | wc -c)",
        "    clean=$(tr -d '\\001' < \"$LOG_FILE\" 2>/dev/null | wc -c)",
        "LOG: 探测空洞时找错了字节(NUL→SOH) ⇒ **认不出有洞** ⇒ 压根不修",
    ),
    (
        "start.sh",
        "    cp \"$LOG_FILE\" \"$keep\" 2>/dev/null || keep=\"\"",
        "    keep=\"\"",
        "LOG: **不留原件** ⇒ 日志被就地改写 ⇒ 出事回不去头",
    ),
    (
        "start.sh",
        "    if [ \"$raw\" = \"$clean\" ]; then\n        return 0                      # 没有空洞 ⇒ 什么都不用做\n    fi",
        "    if false; then\n        return 0                      # 没有空洞 ⇒ 什么都不用做\n    fi",
        "LOG: 去掉「没洞就别动」的判断 ⇒ **每次启动都重写一遍日志**"
        "⇒ 干净日志也被碰(还多留一份副本)",
    ),
    (
        "start.sh",
        "        repair_log_holes\n        setsid nohup",
        "        setsid nohup",
        "LOG: **把调用删了** ⇒ 函数写得再好也没人用 ⇒ 洞永远补不上",
    ),
]


def main():
    root = mkdtemp(prefix="mut_loghole_")
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
        os.makedirs(work, exist_ok=True)
        outp = os.path.join(work, rel)
        with open(outp, "w", encoding="utf-8") as f:
            f.write(orig.replace(frm, to))
        shutil.copy2(os.path.join(REPO, "start.sh"),
                     os.path.join(work, "start.sh.orig"))

        env = dict(os.environ)
        env["AGING_START_SH"] = outp
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
