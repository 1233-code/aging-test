# 变异测试: 故意把「服务中途夭折的轮次怎么收尾」改坏, 看
# tests/test_interrupted_run_reconcile.py 会不会红。
#
# 为什么要有这么个东西: 「测试全绿」本身说明不了什么 —— 断言写错了、
# 写松了、或者压根没跑到, 都是绿的。唯一能证明这测试**咬得动**的办法,
# 就是亲手把代码改坏, 看它是不是真的红。
#
# 这一份守的是 2026-10-28 事故的尾巴: 服务在跑测试的途中冻死了, 那一轮
# 在库里永远停在「运行中」—— 界面上一整天都显示有测试在跑, 明明什么都没
# 在跑。收尾逻辑最容易出的错是**假通过**(没跑完的项被判成 passed), 那比
# 假失败更坑: 好板子被当成合格品放行。
#
# 做法: **每个变异单开一个全新目录**, 把本仓库自己的 server/ + config.yaml
# + 那个测试文件复制进去(源就是**这个仓库**, 不是任何外部下载的东西), 在
# **副本**上做一处改动, 在副本目录里跑测试。
# 被改的那个文件是从头到尾**只写一次**(直接写变异后的内容), 别的文件原样
# 复制 —— 所以整个过程不覆写任何已存在的文件, 跑完也不用还原。
#
# ⚠️ 只在**副本**上改 —— 仓库里 server/ 是产线在跑的那份, 改坏了没人知道。
#
# 跑法: .venv/bin/python tests/_mutate_interrupted_reconcile.py
import os
import shutil
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(REPO, ".venv", "bin", "python")
TEST = "tests.test_interrupted_run_reconcile"

# 每个变异: (文件, from, to, 说明)。from 必须在源文件里**找得到**, 否则算失败。
MUTATIONS = [
    (
        "server/db.py",
        "            for rid, is_parent in ((run_id, False), (mini_of, True)):",
        "            for rid, is_parent in ((run_id, False),):",
        "DB: 重测轮被中断时**不收父轮** (父轮里那一项永远卡在 running)",
    ),
    (
        "server/db.py",
        '                    "UPDATE results SET status=\'stopped\', "\n'
        '                    "error=COALESCE(error, ?) "\n'
        '                    "WHERE run_id=? AND status=\'running\'",',
        '                    "UPDATE results SET status=\'running\', "\n'
        '                    "error=COALESCE(error, ?) "\n'
        '                    "WHERE run_id=? AND status=\'running\'",',
        "DB: 逐项不改成 stopped (还在跑的那一项永远停在 running)",
    ),
    (
        "server/db.py",
        "            if hard_reset and not is_parent:",
        "            if False and not is_parent:",
        "DB: 硬复位的那个分支**整个不走**(板子被硬拉死也还是判 stopped "
        "= **放行扛不住加压的板子**)",
    ),
    (
        "server/db.py",
        "            if hard_reset and not is_parent:",
        "            if hard_reset:",
        "DB: 硬复位时**父轮**那一行也判 failed(硬复位打死的不是父轮 "
        "= 张冠李戴, 父轮自己的结论被改掉)",
    ),
    (
        "server/db.py",
        "                    (boot_evidence.HARD_RESET_ITEM_ERROR, run_id)",
        "                    (boot_evidence.SOFT_ITEM_ERROR, run_id)",
        "DB: 硬复位的逐项说法换成了软重启那句(还是写着「服务重启, "
        "本项没跑完」⇒ 方向还是反的)",
    ),
    (
        "server/db.py",
        "                 boot_evidence.HARD_RESET_RUN_ERROR if hard_reset\n"
        "                 else boot_evidence.SOFT_RUN_ERROR,",
        "                 boot_evidence.SOFT_RUN_ERROR,",
        "DB: 硬复位的轮级说法换成了「不代表被测设备不合格」",
    ),
    (
        "server/db.py",
        "                    rid, is_parent=is_parent, hard_reset=hard_reset)",
        "                    rid, is_parent=is_parent)",
        "DB: hard_reset 压根没传下去(收尾永远走软重启那条路)",
    ),
    (
        "server/db.py",
        '    if failed or error:\n        return "failed"',
        '    if False:\n        return "failed"',
        "DB: 轮级判定里「有 failed/error 的项 → failed」被删掉 "
        "(板子真不合格的结论被抹掉 = **放行坏板子**)",
    ),
    (
        "server/db.py",
        '    if not_run > 0 or aborted > 0:\n        return "stopped"',
        '    if not_run > 0 or aborted > 0:\n        return "passed"',
        "DB: 「没跑完 ⇒ 没结论」被改成 passed (**假通过** —— 好板子被当合格品放行)",
    ),
    (
        "server/db.py",
        '    if not_run > 0 or aborted > 0:\n        return "stopped"\n',
        '',
        "DB: 「有没跑到的项 ⇒ stopped」那条整个删掉 "
        "(total==0 时被判成 skipped, 像「用户主动跳过的」)",
    ),
    (
        "server/db.py",
        "                r = await Database.finalize_interrupted_run(\n"
        "                    rid, is_parent=is_parent, hard_reset=hard_reset)",
        "                r = await Database.finalize_interrupted_run(\n"
        "                    rid)",
        "DB: 父轮也按「整轮收尾」处理 (父轮的轮级 status/error 被盖成"
        "「服务重启, 这一轮中途夭折」= **张冠李戴**, 父轮自己那条 error 蒸发)",
    ),
    (
        "server/db.py",
        "                r = await Database.finalize_interrupted_run(\n"
        "                    rid, is_parent=is_parent, hard_reset=hard_reset)",
        "                r = None",
        "DB: 收尾流程压根不跑 (残留在 running 上的轮次一个都不动)",
    ),
    (
        "server/db.py",
        '                "SELECT id, sn, status, started_at, mini_of FROM runs "\n'
        '                "WHERE status=\'running\' ORDER BY id"',
        '                "SELECT id, sn, status, started_at, mini_of FROM runs "\n'
        '                "WHERE status!=\'passed\' ORDER BY id"',
        "DB: 列残留轮次的条件放宽 (收完之后还列得出来, 操作员点一次没反应)",
    ),
    # ── 2026-11-04 补的**第二条通道**: journal 尾巴认不出我们的测试时,
    #    改看「加压现场记录」(逐行 fsync 落盘, 硬复位带不走) ──
    (
        "server/main.py",
        'if boot_evidence.cpu_evidence_cut_short(r.get("id")):',
        'if False:',
        "MAIN: 第二条通道**压根不走**(journal 尾巴认不出我们时永远不查现场"
        "记录 ⇒ 死在加压途中的板子判 stopped ⇒ **不计入通过率分母 ⇒ 放行**)",
    ),
    (
        "server/main.py",
        "                        if cut is not None:\n"
        "                            hard_reset = True",
        "                        if cut is not None:\n"
        "                            hard_reset = False",
        "MAIN: 查到「加压起手了、没跑完」也**不改判**(还是 stopped ⇒ "
        "一块扛不住加压的板子照样被放行)",
    ),
    (
        "server/boot_evidence.py",
        '_EVID_LOAD_START = "起跑 "',
        '_EVID_LOAD_START = "起跑"',
        "EVID: 认「起跑」时**不认那个空格**(文件头那行「# 起跑时板子背景负载」"
        "会被当成加压起跑 ⇒ 压根没加压也判死在加压途中 ⇒ 冤枉好板子)",
    ),
    (
        "server/boot_evidence.py",
        '_EVID_LOAD_DONE = "加压结束"',
        '_EVID_LOAD_DONE = "加压结束 "',
        "EVID: 「加压结束」**永远认不出来**(跑完了也当成没跑完 ⇒ 冤枉好板子)",
    ),
    (
        "server/boot_evidence.py",
        '    return last == "start"',
        "    return last is not None",
        "EVID: 只要有标记就当「死在加压途中」(加压**跑完了**也算 ⇒ 冤枉好板子)",
    ),
    (
        "server/boot_evidence.py",
        "    last = None\n    for line in text.splitlines():",
        "    last = None\n    for line in text.splitlines()[:1]:",
        "EVID: 只看文件**第一行**(标记在后面几行 ⇒ 认不出来 ⇒ 死在加压途中的"
        "板子判 stopped ⇒ 被放行)",
    ),
    (
        "server/boot_evidence.py",
        "    text = read_cpu_evidence(run_id)\n    if not text:\n        return False",
        "    text = \"\"\n    if not text:\n        return False",
        "EVID: 压根**不读**现场记录(第二条通道整个是空的)",
    ),
]


def build_copy(dst, rel, new_text):
    """把本仓库的 server/ + config.yaml + 那个测试文件复制到 dst。

    被变异的那个文件**只写一次** —— 根本不复制进来, 直接写变异后的内容。
    全程不覆写任何已存在的文件, 所以跑完不需要还原。
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
    # ⚠️ **static/ 也要复制** —— 那个测试的第 8 个场景要走**真正的启动流程**
    #   (`server.main`), 而 main.py 在 import 时就 `app.mount("/static", ...)`,
    #   目录不存在会直接 RuntimeError ⇒ 测试**崩**了。那种红是假的(崩了只
    #   证明「跑不起来」, 证明不了「断言抓得住这个缺陷」)。
    shutil.copytree(os.path.join(REPO, "static"), os.path.join(dst, "static"),
                    ignore=shutil.ignore_patterns("__pycache__"))
    # ⚠️ **目标目录是空的** —— dst 是主流程刚 `mkdtemp` 出来的新目录, 里面
    #   什么都没有; 这几行只是把源文件**复制**进去(源在仓库里, 只读),
    #   仓库里的文件一个字都不动。
    # ⚠️ `_tmp.py` 也要复制 —— 那个测试用它建临时目录。不复制的话, 副本里
    #   `from tests._tmp import mkdtemp` 直接 ImportError ⇒ 测试**崩**了,
    #   那种红是假的(崩了只证明「跑不起来」, 证明不了「断言抓得住这个缺陷」)。
    os.makedirs(os.path.join(dst, "tests"), exist_ok=True)
    for extra in ("test_interrupted_run_reconcile.py", "_tmp.py"):
        shutil.copy2(os.path.join(REPO, "tests", extra),
                     os.path.join(dst, "tests", extra))

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
    root = tempfile.mkdtemp(prefix="mut_reconcile_")
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

        work = os.path.join(root, f"m{i:02d}")
        os.makedirs(work, exist_ok=True)
        build_copy(work, rel, orig.replace(frm, to))

        code, out = run_one(work)
        fails = out.count("✗")
        if code != 0 and fails == 0:
            # ⚠️ **红了但一个 ✗ 都没有** —— 那多半不是断言把它咬住的, 而是
            #   测试自己崩了(语法错/导入错/异常)。这不算「咬得动」——
            #   崩了只能证明「跑不起来」, 证明不了「断言抓得住这个缺陷」。
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
