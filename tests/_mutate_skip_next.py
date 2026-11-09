# 变异测试: 故意把「停止 / 下一项」这套逻辑改坏, 看
# tests/test_skip_next.py + tests/test_summary_total.py 会不会红。
#
# 为什么要有这么个东西: 「测试全绿」本身说明不了什么 —— 断言写错了、
# 写松了、或者压根没跑到, 都是绿的。唯一能证明这测试**咬得动**的办法,
# 就是亲手把代码改坏, 看它是不是真的红。
#
# 这一份守的是 2026-10-29 的两个操作键:
#   · 「停止」  = 中止整轮 —— 当前项 + 后面所有项都记 skipped, 跳结果页;
#   · 「下一项」= 只跳当前项 —— 当前项记 skipped, 后面照常跑;
#   · 整轮判定多了一个「aborted」口径 —— 工人中止过就判 stopped。
#
# 这些改坏的后果分两类:
#   ① **假通过**(最坏): aborted 那条口径一没, 「3 项测过都过了 + 工人按了
#      停止剩下 7 项」就判成 passed —— 一台只测了三项的机器拿着「整体通过」
#      出货, 连申诉的由头都没有(§9.3: 静默的「通过」比明确的「失败」有害得多);
#   ② **界面与后台打架**: 前端按「下一项」, 后端收到的是「停止」——
#      工人以为跳了一项, 实际整轮没了。
#
# 做法: **每个变异单开一个全新目录**, 把本仓库自己的 server/ + config.yaml
# + 那两个测试文件复制进去(源就是**这个仓库**, 不是任何外部下载的东西), 在
# **副本**上做一处改动, 在副本目录里跑测试。
# 被改的那个文件是从头到尾**只写一次**(直接写变异后的内容), 别的文件原样
# 复制 —— 所以整个过程不覆写任何已存在的文件, 跑完也不用还原。
#
# ⚠️ 只在**副本**上改 —— 仓库里 server/ 是产线在跑的那份, 改坏了没人知道。
#
# 跑法: .venv/bin/python tests/_mutate_skip_next.py
import os
import shutil
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(REPO, ".venv", "bin", "python")

TESTS = ("tests.test_skip_next", "tests.test_summary_total")

# 每个变异: (文件, from, to, 说明)。
# from 必须在源文件里**找得到**, 否则算失败。
MUTATIONS = [
    # ── ① 当前项那一行: 用户按了按钮 → 记 skipped ──
    (
        "server/runner.py",
        "        if self.cancelled or self._skip_current:\n"
        "            if result.status not in (\"passed\", \"failed\"):\n"
        "                by_stop = self.cancelled\n"
        "                result.status = \"skipped\"\n",
        "        if False:\n"
        "            if result.status not in (\"passed\", \"failed\"):\n"
        "                by_stop = self.cancelled\n"
        "                result.status = \"skipped\"\n",
        "当前项: 用户按了按钮也不改标签 (还是 stopped —— 「停止」和「服务死了」"
        "分不清, 报告上写不出是工人自己按的)",
    ),
    (
        "server/runner.py",
        '                    "aborted_by_user" if by_stop else "skipped_by_user")',
        '                    "aborted_by_user")',
        "当前项: 不分「中止整轮」和「跳过此项」(跳一项也被说成整轮中止 → "
        "整轮判 stopped, 明明后面各项都测了)",
    ),
    (
        "server/runner.py",
        "        self._skip_current = False\n"
        "        # 实例先建出来:",
        "        # 实例先建出来:",
        "当前项: 标志**不按项清零**(「下一项」的请求漏到下一项 → 新的一项"
        "刚开跑就被判跳过)",
    ),
    # ── ② 后面那些没跑到的项: 补 skipped 行 ──
    (
        "server/runner.py",
        '            r = TestResult(status="skipped", error="用户中止整轮测试, 本项未执行")',
        '            r = TestResult(status="stopped", error="用户中止整轮测试, 本项未执行")',
        "剩余项: 补的行又记回 stopped (2026-10-29 新口径: 用户主动叫停 → 跳过)",
    ),
    (
        "server/runner.py",
        '                    status="skipped",\n'
        '                    started_at=clock.now(),\n'
        '                    finished_at=clock.now(),\n'
        '                    duration_sec=0.0,\n'
        '                    metrics={"skipped_reason": "aborted_by_user"},\n',
        '                    status="stopped",\n'
        '                    started_at=clock.now(),\n'
        '                    finished_at=clock.now(),\n'
        '                    duration_sec=0.0,\n'
        '                    metrics={"skipped_reason": "aborted_by_user"},\n',
        "剩余项: 补进库的行是 stopped (界面上和报告里看到的是「没跑完」, "
        "而不是「用户主动跳过」)",
    ),
    (
        "server/runner.py",
        '                    metrics={"skipped_reason": "aborted_by_user"},\n',
        '                    metrics={},\n',
        "剩余项: 补的行**不写**「中止整轮」的标记 (整轮判 stopped 就靠它 —— "
        "没了它, 工人中止过也判成 passed = **假通过**)",
    ),
    (
        "server/runner.py",
        "            await self._mark_remaining_skipped(enabled_tests)",
        "            pass  # ← 变异: 不补剩余的项",
        "剩余项: 压根不补行 (报告逐项段里没有它们, 界面显示成「⏳ 等待」, "
        "工人以为还在排队; 数字也对不上账)",
    ),
    # ── ③ 整轮判定: aborted 口径 (假通过就在这一条上) ──
    (
        "server/runner.py",
        "        aborted = sum(\n"
        "            1 for r in self.results.values()\n"
        '            if r.status == "skipped"\n'
        '            and (r.metrics or {}).get("skipped_reason") == "aborted_by_user"\n'
        "        )\n",
        "        aborted = 0\n",
        "整轮: aborted 恒为 0 (工人中止过也看不出来 → 「3 项测过都过了 + "
        "按了停止」判 **passed** = **假通过**)",
    ),
    (
        "server/db.py",
        '    if not_run > 0 or aborted > 0:\n        return "stopped"',
        '    if not_run > 0:\n        return "stopped"',
        "整轮判定: 去掉 aborted 那条 (同上, 假通过)",
    ),
    (
        "server/db.py",
        '                    if _metrics_skipped_reason(metrics_json) == "aborted_by_user":\n'
        '                        counts["aborted"] += 1\n',
        "                    pass\n",
        "重算: 不数 aborted_by_user 那几行 (父轮被中止过、重测只补了一部分 → "
        "整轮被重算成 passed = 假通过)",
    ),
    # ── ④ 「下一项」不许把整轮停掉 ──
    (
        "server/runner.py",
        "        self._skip_current = True\n"
        "        try:\n"
        "            await inst.cancel()\n"
        "        except Exception:\n"
        "            pass\n"
        '        return {"ok": True, "mode": "skip_current"}\n',
        "        self.cancelled = True\n"
        "        try:\n"
        "            await inst.cancel()\n"
        "        except Exception:\n"
        "            pass\n"
        '        return {"ok": True, "mode": "skip_current"}\n',
        "「下一项」: 把整轮也停了 (工人只想跳一项, 结果整轮没了)",
    ),
    (
        "server/runner.py",
        "            if self._skip_current:\n"
        "                return True       # 「下一项」→ 不用等了\n",
        "",
        "静默期: 「下一项」按了也照等 3 秒 (工人按了键看着像没反应)",
    ),
    # ── ⑤ 接线: 「下一项」不许打到「停止」 ──
    (
        "server/main.py",
        "    return await runner.skip_current()",
        "    return await runner.stop()",
        "接线: /api/test/next 打到 stop() (前端按「下一项」, 后端当成「停止」)",
    ),
]


def build_copy(dst, rel, new_text):
    """把本仓库的 server/ + config.yaml + 那两个测试文件复制到 dst。

    被变异的那个文件**只写一次** —— 根本不复制进来, 直接写变异后的内容。
    目标目录 `dst` 是**这个变异专属的新目录**, 里面每个文件都只写一次 ——
    全程不覆写任何已存在的文件, 跑完不用还原。
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
    # static/ 也要: server/main.py 在 import 时就把 static/ 挂上去,
    # 目录不存在它直接抛 RuntimeError —— 那样测的是「崩了」, 不是断言。
    shutil.copytree(os.path.join(REPO, "static"),
                    os.path.join(dst, "static"),
                    ignore=shutil.ignore_patterns("__pycache__"))
    os.makedirs(os.path.join(dst, "tests"), exist_ok=True)
    # ⚠️ **`_tmp.py` 也要复制** —— `test_skip_next.py` 里写着
    #   `from tests._tmp import mkdtemp`。不复制的话, 副本里那个 import
    #   直接 ModuleNotFoundError ⇒ 那个测试**整个跑不起来** ⇒ 崩了只证明
    #   「跑不起来」, 证明不了「断言抓得住这个缺陷」⇒ **变异一个都验不到**
    #   (2026-11-04 实测踩到: 6 个变异都是「红了但一个 ✗ 都没有」)。
    for t in ("test_skip_next.py", "test_summary_total.py", "_tmp.py"):
        shutil.copy2(os.path.join(REPO, "tests", t),
                     os.path.join(dst, "tests", t))

    outp = os.path.join(dst, rel)
    os.makedirs(os.path.dirname(outp), exist_ok=True)
    with open(outp, "w", encoding="utf-8") as f:
        f.write(new_text)


def run_one(work, test_mod):
    env = dict(os.environ)
    env["PYTHONPATH"] = work
    proc = subprocess.run(
        [PY, "-m", test_mod],
        cwd=work, env=env, capture_output=True, text=True, timeout=300,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def main():
    root = tempfile.mkdtemp(prefix="mut_skip_next_")
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
            print(f"\n✗ {desc}\n   → 变异点在源文件里出现 {orig.count(frm)} 次, "
                  f"不唯一 —— 这个变异脚本自己写错了")
            bad += 1
            continue

        work = os.path.join(root, f"m{i:02d}")
        os.makedirs(work, exist_ok=True)
        build_copy(work, rel, orig.replace(frm, to))

        code, out = 0, ""
        for t in TESTS:
            c, o = run_one(work, t)
            code = code or c
            out += o
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
