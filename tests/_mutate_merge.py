# 变异测试(后端): 故意把「合并重测」改坏, 看 tests/test_history_retest_merge.py
# 会不会红。
#
# 为什么要有这么个东西: 「测试全绿」本身说明不了什么 —— 断言写错了、
# 写松了、或者压根没跑到, 都是绿的。唯一能证明这测试**咬得动**的办法,
# 就是亲手把代码改坏, 看它是不是真的红。
#
# 做法: 把 server/ + config.yaml + 那个测试文件复制一份到临时目录, 在**副本**
# 上做一处改动, 在副本目录里跑测试。每个变异跑完把副本还原。
#
# ⚠️ 只在**副本**上改 —— 仓库里 server/ 是产线在跑的那份, 改坏了没人知道。
#
# 跑法: .venv/bin/python tests/_mutate_merge.py
import os
import shutil
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(REPO, ".venv", "bin", "python")
TEST = "tests.test_history_retest_merge"

# 每个变异: (文件, from, to, 说明)。from 必须在副本里**找得到**, 否则算失败。
MUTATIONS = [
    (
        "server/db.py",
        '            elif total == 0:\n                status = "skipped"',
        '            elif total == 0:\n                status = "passed"',
        "DB: 一项都没实际执行 → 判 passed (§9.2 明令不许)",
    ),
    (
        "server/db.py",
        '            total = counts["passed"] + counts["failed"] + counts["error"]',
        '            total = counts["passed"] + counts["failed"]',
        "DB: total 不再把 error 算进去 (计数口径跟 §9.2 不一致)",
    ),
    (
        "server/db.py",
        "retest_count=COALESCE(retest_count,0)+1 WHERE id=?",
        "retest_count=COALESCE(retest_count,0) WHERE id=?",
        "DB: 合并后不记重测次数 (「第 N 次重测通过」永远是 0)",
    ),
    (
        "server/db.py",
        '                "WHERE run_id=? AND status=\'running\'",',
        '                "WHERE run_id=? AND status=\'__never__\'",',
        "DB: 不清残留的 running 行 (父轮计数会凭空少一项)",
    ),
    (
        "server/db.py",
        "ORDER BY COALESCE(updated_at, started_at) DESC",
        "ORDER BY started_at DESC",
        "DB: 历史列表不按更新时间排 (合并过的记录沉底, 找不到)",
    ),
    (
        "server/runner.py",
        "        base = self._parent_attempt.get(name, 1) if self.parent_run_id else 0\n"
        "        return base + attempt",
        "        base = self._parent_attempt.get(name, 1) if self.parent_run_id else 0\n"
        "        return base",
        "RUNNER: 尝试次数不再累加 (「第 N 次测试通过」永远停在 1)",
    ),
    (
        "server/runner.py",
        "                pdf_path = await report.export_pdf(run_for_pdf, parent_results,\n"
        "                                                  retest_runs=retest_runs)",
        "                pdf_path = None",
        "RUNNER: 不重导父轮的 PDF (报告还是失败那版, 没合并)",
    ),
    (
        "server/runner.py",
        '            "type": "parent_merged",',
        '            "type": "parent_merged_disabled",',
        "RUNNER: 不广播 parent_merged (前端不知道要刷新列表/报告)",
    ),
    (
        "server/runner.py",
        "        if parent_run:\n            self.run_id = parent_id\n            self.status = status",
        "        if False:\n            self.run_id = parent_id\n            self.status = status",
        "RUNNER: 合并后不把「当前这一轮」切到父轮 (总结页显示的还是重测那几项)",
    ),
    (
        "server/runner.py",
        "        except Exception as e:\n"
        "            # §8.2 硬规则: 报告导出失败**不阻断**结果",
        "        except ZeroDivisionError as e:\n"
        "            # §8.2 硬规则: 报告导出失败**不阻断**结果",
        "RUNNER: 报告导不出来时把整个合并掀翻 (§8.2 硬规则)",
    ),
    (
        "server/templates/report.html.j2",
        "{% if summary.retest_count %}",
        "{% if false %}",
        "TEMPLATE: 报告不写明「本报告含 N 轮重测」(读者不知道结论变了)",
    ),
    (
        "server/templates/report.html.j2",
        "{% if c.attempt and c.attempt > 1 %}",
        "{% if false %}",
        "TEMPLATE: 逐项不印「第 N 次测试通过」(返修过的项看不出来)",
    ),
    (
        "server/runner.py",
        "                parent_fields = {k: v for k, v in common_fields.items()\n"
        "                                 if k != \"log\"}",
        "                parent_fields = dict(common_fields)",
        "RUNNER: 父轮的原始日志被**冲掉** (首测失败的原因当场蒸发)",
    ),
    (
        "server/templates/report.html.j2",
        "{% if has_retest_log %}",
        "{% if false %}",
        "TEMPLATE: 报告里不印「重测日志附录」那一节 (重测的日志没人看得到)",
    ),
    (
        "server/report.py",
        '    retest_log = "\\n\\n".join(retest_sections)',
        '    full_log = full_log + "\\n\\n" + "\\n\\n".join(retest_sections)\n'
        '    retest_log = ""',
        "REPORT: 重测日志被**混进**「日志附录」 (两轮的分界没了)",
    ),
    (
        "server/templates/report.html.j2",
        '<div class="log-appendix">',
        '{% if has_retest_log %}\n'
        '<div class="retest-appendix">\n'
        '    <h2>重测日志附录</h2>\n'
        '    <p class="snapshot-note">乱序版(变异)</p>\n'
        '    <pre class="log">{{ retest_log }}</pre>\n'
        '</div>\n'
        '{% endif %}\n'
        '<div class="log-appendix">',
        "TEMPLATE: 「重测日志附录」排在「日志附录」**前面** (顺序反了)",
    ),
    (
        "server/templates/report.html.j2",
        "{% if c.retested %}本项另有重测轮, 那轮的日志见其后的「重测日志附录」。{% endif %}",
        "",
        "TEMPLATE: 逐项段不给指路 (读者以为只有首测那一份日志)",
    ),
    (
        "server/db.py",
        'where = "" if include_mini else " WHERE mini_of IS NULL "',
        'where = "" if include_mini else " "',
        "DB: 历史列表把重测轮也列出来了 (重测两次 → 四条带「重测」的行)",
    ),
    (
        "server/db.py",
        '        run["retest_runs"] = await Database.get_retest_runs(run_id)',
        '        run["retest_runs"] = []',
        "DB: 详情里不带重测轮 (重测过程在界面上彻底看不见了)",
    ),
]


def run_one(tmp):
    env = dict(os.environ)
    env["PYTHONPATH"] = tmp
    proc = subprocess.run(
        [PY, "-m", TEST],
        cwd=tmp, env=env, capture_output=True, text=True, timeout=300,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def main():
    tmp = tempfile.mkdtemp(prefix="mut_merge_")
    # 复制一份: server/ + config.yaml + 那个测试文件
    shutil.copytree(os.path.join(REPO, "server"), os.path.join(tmp, "server"),
                    ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy2(os.path.join(REPO, "config.yaml"),
                 os.path.join(tmp, "config.yaml"))
    os.makedirs(os.path.join(tmp, "tests"), exist_ok=True)
    shutil.copy2(os.path.join(REPO, "tests", "test_history_retest_merge.py"),
                 os.path.join(tmp, "tests", "test_history_retest_merge.py"))
    print(f"副本在 {tmp}\n")

    bad = 0
    for rel, frm, to, desc in MUTATIONS:
        p = os.path.join(tmp, rel)
        with open(p, encoding="utf-8") as f:
            orig = f.read()
        if frm not in orig:
            print(f"\n✗ {desc}\n   → 变异点没找到(源文件变了?): {frm[:60]!r}")
            bad += 1
            continue
        with open(p, "w", encoding="utf-8") as f:
            # 替换**全部**出现处 —— 模板里同一个 {% if %} 出现两次(逐项段和
            # 汇总段各一处), 只改一处的话断言照样绿, 变异就没生效。
            f.write(orig.replace(frm, to))
        code, out = run_one(tmp)
        fails = out.count("✗")
        if code != 0:
            print(f"\n✓ {desc}\n   → 测试变红 (exit={code}, {fails} 项失败) —— 咬得动")
        else:
            print(f"\n✗ {desc}\n   → 测试还是绿的! 这个变异没被守住")
            bad += 1
        with open(p, "w", encoding="utf-8") as f:
            f.write(orig)      # 还原

    print("\n" + ("全部变异都被抓住了 ✓" if bad == 0
                  else f"有 {bad} 个变异没被抓住 ✗"))
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
