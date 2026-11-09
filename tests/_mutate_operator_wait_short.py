# 变异测试: 故意把「TF 卡没插 / 网线没插的提示只等 30 秒」改坏, 看
# tests/test_operator_wait_short.py 会不会红。
#
# 为什么要有这么个东西: 「测试全绿」本身说明不了什么 —— 断言写错了、
# 写松了、或者压根没跑到, 都是绿的。唯一能证明这测试**咬得动**的办法,
# 就是亲手把代码改坏, 看它是不是真的红。
#
# 这一份守的是 2026-11-09 工人要求「tf卡和网线没插的情况下提示只等 30s,
# 30s 内没操作直接跳过」换来的规矩:
#   ① 两个「料没到位」的提示必须**自己声明** 30 秒(wait_sec);
#   ② runner 必须**认**这个声明 —— 30 秒内没人操作就**直接跳过**,
#      下一项接着跑, 这一项仍然是 skipped(没测 ⇒ 不计入通过率);
#   ③ 提示自己声明的值**压过**全局 flow.operator_wait_sec(不是取较小值);
#   ④ 不该等的情况(TF 挂载失败 / 工位没 DHCP / 上游网络坏了)一个都不许弹框;
#   ⑤ wait_sec 写坏(0 / 负数 / 乱写)时退回全局, **绝不能变成「不等」**。
#
# ⚠️ **这些改动全部只发生在临时副本里** —— 仓库里 server/ 下的是产线在跑
#   的那几份, 一个字都不动。副本跑完就扔, 不用还原。
#
# 跑法: .venv/bin/python tests/_mutate_operator_wait_short.py
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests._tmp import mkdtemp                       # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(REPO, ".venv", "bin", "python")
TEST = "tests.test_operator_wait_short"

# 每个变异: (文件, from, to, 说明)。from 必须在源文件里**找得到**, 否则算失败。
MUTATIONS = [
    (
        "server/runner.py",
        '        own = prompt.get("wait_sec")\n',
        '        own = None\n',
        "runner: 无视提示自己声明的等待上限(一律用全局 300s)"
        "(⇒ 卡槽空 / 网线没插时整轮白等 5 分钟, 工人要求的是 30 秒)",
    ),
    (
        "server/runner.py",
        "            timeout = own_f if own_f > 0 else float(\n"
        '                flow_cfg.get("operator_wait_sec", OPERATOR_WAIT_DEFAULT_SEC)\n'
        "            )\n",
        "            timeout = min(own_f, float(\n"
        '                flow_cfg.get("operator_wait_sec", OPERATOR_WAIT_DEFAULT_SEC)\n'
        "            )) if own_f > 0 else float(\n"
        '                flow_cfg.get("operator_wait_sec", OPERATOR_WAIT_DEFAULT_SEC)\n'
        "            )\n",
        "runner: 提示的值与全局**取较小值**"
        "(⇒ 全局配小了就把 30 秒压下去, 提示说了也不算)",
    ),
    (
        "server/runner.py",
        "                own_f = float(own)\n",
        "                own_f = int(own)\n",
        "runner: 等待值被 int 截断"
        "(⇒ 历史坑: int(0.5) 截成 0, 「等操作员」直接变成「不等」)",
    ),
    (
        "server/runner.py",
        "            timeout = own_f if own_f > 0 else float(\n",
        "            timeout = own_f if own_f >= 0 else float(\n",
        "runner: wait_sec=0 也算数"
        "(⇒ 一个 0 就让「等操作员」变成「不等」, 弹框秒消失)",
    ),
    (
        "server/tests/tfcard.py",
        '                "wait_sec": OPERATOR_WAIT_SHORT_SEC,\n',
        '                "wait_sec": None,\n',
        "TF 卡: 提示里不声明 30 秒(退回全局 300s)",
    ),
    (
        "server/tests/ethernet.py",
        '                "wait_sec": OPERATOR_WAIT_SHORT_SEC,\n',
        '                "wait_sec": None,\n',
        "网线: 提示里不声明 30 秒(退回全局 300s)",
    ),
    (
        "server/tests/base.py",
        "OPERATOR_WAIT_SHORT_SEC: float = 30.0\n",
        "OPERATOR_WAIT_SHORT_SEC: float = 300.0\n",
        "常量: 「等 30 秒」被写成 300 秒",
    ),
    (
        "server/tests/base.py",
        "OPERATOR_WAIT_SHORT_SEC: float = 30.0\n",
        "OPERATOR_WAIT_SHORT_SEC: float = 0.0\n",
        "常量: 「等 30 秒」被写成 0(= 不等)",
    ),
    (
        "server/tests/ethernet.py",
        '        if result.status == "skipped" and m.get("skipped_reason") == "unplugged":\n',
        '        if result.status == "skipped":\n',
        "网线: 所有 skipped 都弹框"
        "(⇒ 工位没 DHCP / 上游网络坏了这种等一下也没用的, 也占着整轮等)",
    ),
    (
        "server/tests/tfcard.py",
        '        if result.status == "skipped" and (result.metrics or {}).get("no_card"):\n',
        '        if result.status != "error":\n',
        "TF 卡: 不是「卡槽空」也弹框"
        "(⇒ 这一项正常跑完了还要工人来点一下)",
    ),
]


def build_copy(dst, rel, new_text):
    """把 server/ + config.yaml + 那个测试文件复制到 dst, 并把 rel 写成 new_text。

    每个文件**只写一次** —— 被变异的那个根本不复制进来, 直接写变异后的
    内容。全程不覆写任何已存在的文件, 所以跑完不需要还原。
    """
    src = os.path.join(REPO, "server")
    for root, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        relroot = os.path.relpath(root, src)
        outroot = (os.path.join(dst, "server") if relroot == "."
                   else os.path.join(dst, "server", relroot))
        os.makedirs(outroot, exist_ok=True)
        for fn in files:
            relfile = os.path.normpath(os.path.join(relroot, fn))
            if relfile == rel:
                continue          # 这个下面单独写
            shutil.copy2(os.path.join(root, fn), os.path.join(outroot, fn))

    shutil.copy2(os.path.join(REPO, "config.yaml"),
                 os.path.join(dst, "config.yaml"))
    os.makedirs(os.path.join(dst, "tests"), exist_ok=True)
    shutil.copy2(os.path.join(REPO, "tests", "test_operator_wait_short.py"),
                 os.path.join(dst, "tests", "test_operator_wait_short.py"))

    outp = os.path.join(dst, rel)
    os.makedirs(os.path.dirname(outp), exist_ok=True)
    with open(outp, "w", encoding="utf-8") as f:
        f.write(new_text)


def run_one(work):
    env = dict(os.environ)
    env["PYTHONPATH"] = work
    proc = subprocess.run(
        [PY, "-m", TEST],
        cwd=work, env=env, capture_output=True, text=True, timeout=300,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def main():
    root = mkdtemp(prefix="mut_operator_wait_")
    print(f"副本都在 {root} 下面\n")

    bad = 0
    for i, (rel, frm, to, desc) in enumerate(MUTATIONS):
        srcp = os.path.join(REPO, rel)
        with open(srcp, encoding="utf-8") as f:
            orig = f.read()
        if frm not in orig:
            print(f"\n✗ {desc}\n   → 变异点没找到(源文件变了?): {frm[:60]!r}")
            bad += 1
            continue
        if orig.count(frm) != 1:
            print(f"\n✗ {desc}\n   → 变异点**不唯一**({orig.count(frm)} 处) "
                  f"⇒ 证据不成立: {frm[:60]!r}")
            bad += 1
            continue

        work = os.path.join(root, f"m{i:02d}")
        os.makedirs(work, exist_ok=True)
        build_copy(work, rel, orig.replace(frm, to))

        code, out = run_one(work)
        fails = out.count("✗")
        if code != 0 and fails == 0:
            # ⚠️ **红了但一个 ✗ 都没有** —— 那多半不是断言把它咬住的, 而是
            #   测试自己崩了(语法错/导入错/异常)。这不算「咬得动」。
            print(f"\n✗ {desc}\n   → 红了, 但一个 ✗ 都没有 (exit={code}) —— "
                  f"这是**崩了**, 不是被断言咬住的, 不算数:\n{out[-800:]}")
            bad += 1
        elif code != 0:
            print(f"\n✓ {desc}\n   → 测试变红 (exit={code}, {fails} 项失败) —— 咬得动")
        else:
            print(f"\n✗ {desc}\n   → 测试还是绿的! 这个变异没被守住")
            bad += 1

    print("\n" + ("全部变异都被抓住了 ✓" if bad == 0
                  else f"有 {bad} 个变异没被抓住 ✗"))
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
