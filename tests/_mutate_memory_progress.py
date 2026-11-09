# 变异测试: 故意把「内存这一项的进度条要按实际百分比 + 显示时间」改坏, 看
# tests/test_memory_progress.py 会不会红。
#
# 为什么要有这么个东西: 「测试全绿」本身说明不了什么 —— 断言写错了、
# 写松了、或者压根没跑到, 都是绿的。唯一能证明这测试**咬得动**的办法,
# 就是亲手把代码改坏, 看它是不是真的红。
#
# 这一份守的是 2026-11-06 产线反馈「内存测试的进度条不对, 没按照实际的
# 进度百分比和显示时间」换来的规矩:
#   ① 进度**文案里必须带时间** —— 别的项(eMMC / CPU / GPU / USB / 风扇 /
#      HDMI)文案里都带「已用 Ns / 预计 Ns」, 只有内存这项原来只有一句
#      「第 1/1 轮」⇒ 工人手里光秃秃一个百分比, 没有第二个数可以对;
#   ② 时间必须是**整项**的尺度(不是本轮的) —— 本轮走完是 100%, 而条才
#      走了 1/times ⇒ 两个数对不上, 工人照样会怀疑条坏了;
#   ③ 文案里的两个数换算出来的百分比, 必须**等于**条上的百分比 ——
#      「已用 30s / 预计 60s」⇒ 条就该在 50%, 工人一比就知道条没骗人;
#   ④ 开场就报整项的预计、收尾报这一项一共花了多久 —— 前后都有账;
#   ⑤ 本轮真跑完之前不许显示 100%(提前显示「完成」是骗人)。
#
# ⚠️ **这些改动全部只发生在临时副本里** —— 仓库里 server/tests/memory.py
#   是产线在跑的那份, 一个字都不动。副本跑完就扔, 不用还原。
#
# 跑法: .venv/bin/python tests/_mutate_memory_progress.py
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests._tmp import mkdtemp                       # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(REPO, ".venv", "bin", "python")
TEST = "tests.test_memory_progress"

# 每个变异: (文件, from, to, 说明)。from 必须在源文件里**找得到**, 否则算失败。
MUTATIONS = [
    (
        "server/tests/memory.py",
        '                msg = f"{msg} · 已用 {elapsed:.0f}s / 预计 {total:.0f}s"\n',
        "",
        "内存: 进度文案里不带时间"
        "(⇒ 工人手里只有光秃秃一个百分比, 没有第二个数能跟条对)",
    ),
    (
        "server/tests/memory.py",
        "                total = times * round_sec_est\n",
        "                total = round_sec_est\n",
        "内存: 「预计」用一轮的时长, 不是整项的"
        "(⇒ times>1 时文案里的比例跟条上的百分比对不上)",
    ),
    (
        "server/tests/memory.py",
        "                elapsed = time.time() - item_t0\n",
        "                elapsed = time.time() - item_t0 + 30\n",
        "内存: 「已用」不是真实经过的时间"
        "(⇒ 文案里的时间对不上真实进度, 工人一比就发现条跟时间脱节)",
    ),
    (
        "server/tests/memory.py",
        '        await ctx.on_progress(0, f"准备开始 · 预计 {times * timeout_sec:.0f}s")\n',
        '        await ctx.on_progress(0, "准备开始")\n',
        "内存: 开场不报整项的预计时长",
    ),
    (
        "server/tests/memory.py",
        '        await ctx.on_progress(100, f"完成 · 共 {time.time() - item_t0:.0f}s")\n',
        '        await ctx.on_progress(100, "完成")\n',
        "内存: 收尾不报这一项一共花了多久",
    ),
    (
        "server/tests/memory.py",
        "    frac = min(max(frac, 0.0), 0.99)\n",
        "    frac = min(max(frac, 0.0), 1.0)\n",
        "内存: 轮内刻度不封顶(本轮还没真跑完就显示 100% ⇒ 骗人)",
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
    shutil.copy2(os.path.join(REPO, "tests", "test_memory_progress.py"),
                 os.path.join(dst, "tests", "test_memory_progress.py"))

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
    root = mkdtemp(prefix="mut_memory_progress_")
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
