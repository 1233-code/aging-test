# 变异测试: 故意把历史库体检工具的「结论 / 退出码」改坏, 看
# tests/test_history_db_check_verdict.py 会不会红。
#
# 为什么要有这么个东西: 「测试全绿」本身说明不了什么 —— 断言写错了、
# 写松了、或者压根没跑到, 都是绿的。唯一能证明这测试**咬得动**的办法,
# 就是亲手把代码改坏, 看它是不是真的红。
#
# 这一份守的是 2026-11-11 审查查出来的两个洞:
#   ① 「文案说这个库是坏的」但**退出码是 0**(= 库是好的)⇒ 按退出码判断的
#      流程会把**坏库当成好库**;
#   ② 旁边有**热日志**时, **只读**打开回滚不了它 ⇒ 那些「读不出来」是只读
#      造成的, 不是库坏的证据 ⇒ 却报「坏」⇒ 工人照着动手 ⇒ 好端端的历史
#      数据被工人自己弄丢。
#
# 做法: **每个变异单开一个全新目录**, 把体检脚本复制进去, 在副本上改一处,
# 用 AGING_HISTORY_DB_CHECK_TOOL 指过去跑测试。
# 被改的那个文件从头到尾**只写一次** —— 全程不覆写任何已存在的文件。
#
# 跑法: .venv/bin/python tests/_mutate_history_db_check_verdict.py
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests._tmp import mkdtemp                       # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(REPO, ".venv", "bin", "python")
TEST = "tests.test_history_db_check_verdict"
TOOL_REL = os.path.join("scripts", "check_history_db.py")
# ⚠️ MUTATIONS 里那几处**必须写字面量** —— tests/_sweep_anchors.py 要静态查
#    锚点, 写成变量它就 literal_eval 不了 ⇒ 那条变异从来没被静态验过。

# 每个变异: (文件, from, to, 说明)。from 必须在源文件里**找得到且唯一**。
MUTATIONS = [
    (
        "scripts/check_history_db.py",
        '               and (out.get("integrity_check") or {}).get("ok")',
        "               # (变异: 去掉 integrity_check 这一项)",
        "DB: 退出码里**不看 integrity_check** ⇒ 只有索引坏的库 ⇒ 文案说坏、"
        "退出码说 0(= 库是好的)",
    ),
    (
        "scripts/check_history_db.py",
        '    if out.get("indeterminate"):\n'
        '        # 「热日志还在, 只读回滚不了」',
        '    if False:\n'
        '        # 「热日志还在, 只读回滚不了」',
        "DB: **不再认「下不了结论」** ⇒ 热日志下也报「库坏了」(退出码 1)",
    ),
    (
        "scripts/check_history_db.py",
        '        out["journal_looks_hot"] = _journal_looks_hot(jpath)',
        '        out["journal_looks_hot"] = False',
        "DB: 认不出哪个是**热日志** ⇒ 热日志下照样报「库坏了」",
    ),
    (
        "scripts/check_history_db.py",
        "    return head == JOURNAL_MAGIC",
        "    return True",
        "DB: 把**任何**残留 -journal 都当成热日志 ⇒ 结论永远「下不了结论」",
    ),
]


def main():
    root = mkdtemp(prefix="mut_dbverdict_")
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

        env = dict(os.environ)
        env["AGING_HISTORY_DB_CHECK_TOOL"] = outp
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
