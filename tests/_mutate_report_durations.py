# 变异测试(后端): 故意把「报告里的时间」改坏, 看 tests/test_report_durations.py
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
# 跑法: .venv/bin/python tests/_mutate_report_durations.py
import os
import shutil
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(REPO, ".venv", "bin", "python")
TEST = "tests.test_report_durations"

# 每个变异: (文件, from, to, 说明)。from 必须在副本里**找得到**, 否则算失败。
MUTATIONS = [
    (
        "server/templates/report.html.j2",
        '    <tr><td>总耗时</td><td>{{ total_duration }}{% for rt in retest_times %}<br><span class="rt-line">重测第 {{ rt.round }} 轮: {{ rt.duration }}</span>{% endfor %}</td></tr>\n',
        "",
        "TEMPLATE: 报告头表里没有「总耗时」这一行了",
    ),
    (
        "server/templates/report.html.j2",
        "    总耗时 <b>{{ total_duration }}</b>\n",
        "",
        "TEMPLATE: 汇总段里没有「总耗时」(读者第一眼看的那块看不到总时间)",
    ),
    (
        "server/templates/report.html.j2",
        '<span class="case-meta">耗时 {{ c.duration }}</span>',
        '<span class="case-meta">{{ c.duration }}</span>',
        "TEMPLATE: 逐项段里只有个孤零零的数, 没标「耗时」(读者不知道那是什么) "
        "[故意多改: 同一段模板有**两个布局分支**(有指标的走表格头, 没指标的"
        "走独立块), 两处都得改 —— 少改一处, 报告会随机地一半标一半不标]",
    ),
    (
        "server/templates/report.html.j2",
        "<th>耗时</th>",
        "<th></th>",
        "TEMPLATE: 未测项明细表里没有「耗时」这一列",
    ),
    (
        "server/templates/report.html.j2",
        "            <td>{{ c.duration }}</td>\n",
        "            <td>—</td>\n",
        "TEMPLATE: 未测项明细表里耗时那一列不填数 (全是占位符)",
    ),
    (
        "server/report.py",
        "    if s < 60:\n        return f\"{_round_half_up(s)}s\"",
        "    if s < 60:\n        return f\"{s:.1f}s\"",
        "REPORT: 四舍五入退回「五成双」(58.25 → 58.2, 跟屏幕上的 58.3 对不上)",
    ),
    (
        "server/report.py",
        "    return f\"{int(math.floor(s / 60))}m{int(math.floor(s % 60)):02d}s\"",
        "    return f\"{int(math.floor(s / 60))}m{int(math.ceil(s % 60)):02d}s\"",
        "REPORT: 秒数改用 ceil 拆 (123.45 秒 → 「2m04s」, 跟屏幕上的对不上)",
    ),
    (
        "server/report.py",
        "    return f\"{int(math.floor(s / 60))}m{int(math.floor(s % 60)):02d}s\"",
        "    return f\"{int(round(s / 60))}m{int(math.floor(s % 60)):02d}s\"",
        "REPORT: 分钟数改用 round 拆 (119.999 秒 → 「2m59s」, 跟屏幕上的对不上)",
    ),
    (
        "server/report.py",
        "    if not math.isfinite(s) or s < 0:\n        return \"—\"",
        "    if not math.isfinite(s):\n        return \"—\"",
        "REPORT: 负数不挡 (纸上写个 -1.0s, 屏幕上是「—」)",
    ),
    (
        "server/report.py",
        "        total_dur = (float(finished) - float(started)\n"
        "                     if started is not None and finished is not None else None)",
        "        total_dur = (float(finished) - float(started)\n"
        "                     if started and finished else None)",
        "REPORT: 判空退回「是不是真的」(started_at 正好是 0.0 时总耗时整行变「—」)",
    ),
    (
        "server/report.py",
        '            "duration": _fmt_dur(r.get("duration_sec")),',
        '            "duration": "—",',
        "REPORT: 每一项的耗时都不填了",
    ),
    # ── §7.8.6 重测轮的时间: 原值保留, 重测的接在后面 ──
    (
        "server/templates/report.html.j2",
        '    <tr><td>开始时间</td><td>{{ started_at }}{% for rt in retest_times %}<br><span class="rt-line">重测第 {{ rt.round }} 轮: {{ rt.started_at }}</span>{% endfor %}</td></tr>\n',
        '    <tr><td>开始时间</td><td>{{ started_at }}</td></tr>\n',
        "TEMPLATE: 开始时间那一行里没有重测轮的时间",
    ),
    (
        "server/templates/report.html.j2",
        '    <tr><td>结束时间</td><td>{{ finished_at }}{% for rt in retest_times %}<br><span class="rt-line">重测第 {{ rt.round }} 轮: {{ rt.finished_at }}</span>{% endfor %}</td></tr>\n',
        '    <tr><td>结束时间</td><td>{{ finished_at }}</td></tr>\n',
        "TEMPLATE: 结束时间那一行里没有重测轮的时间",
    ),
    (
        "server/templates/report.html.j2",
        '    <tr><td>总耗时</td><td>{{ total_duration }}{% for rt in retest_times %}<br><span class="rt-line">重测第 {{ rt.round }} 轮: {{ rt.duration }}</span>{% endfor %}</td></tr>\n',
        '    <tr><td>总耗时</td><td>{{ total_duration }}</td></tr>\n',
        "TEMPLATE: 总耗时那一行里没有重测轮的时间",
    ),
    (
        "server/templates/report.html.j2",
        '    {% for rt in retest_times %}<br><span class="rate-note">重测第 {{ rt.round }} 轮: {{ rt.duration }}</span>{% endfor %}\n',
        "",
        "TEMPLATE: 汇总段里没有跟着说重测轮的耗时 (读者第一眼看的那块看不到)",
    ),
    (
        "server/templates/report.html.j2",
        '    <tr><td>开始时间</td><td>{{ started_at }}{% for rt in retest_times %}<br><span class="rt-line">重测第 {{ rt.round }} 轮: {{ rt.started_at }}</span>{% endfor %}</td></tr>\n',
        '    <tr><td>开始时间</td><td>{% for rt in retest_times %}<br><span class="rt-line">重测第 {{ rt.round }} 轮: {{ rt.started_at }}</span>{% endfor %}{{ started_at }}</td></tr>\n',
        "TEMPLATE: 重测的时间排在**原值前面**(要求是「在原时间后面」)",
    ),
    (
        "server/templates/report.html.j2",
        '<span class="rt-line">重测第 {{ rt.round }} 轮: {{ rt.duration }}</span>',
        '<span class="rt-line">重测轮: {{ rt.duration }}</span>',
        "TEMPLATE: 重测那几行不带轮次号 (读者分不清是哪一轮)",
    ),
    (
        "server/templates/report.html.j2",
        '<span class="rt-line">重测第 {{ rt.round }} 轮: {{ rt.started_at }}</span>',
        '<span>重测第 {{ rt.round }} 轮: {{ rt.started_at }}</span>',
        "TEMPLATE: 开始时间那一行里重测的几行没有自己的样式 (印得跟主数一样大)",
    ),
    (
        "server/templates/report.html.j2",
        ".rt-line { font-size: 8.5pt; color: #546e7a; }",
        "",
        "TEMPLATE: CSS 里没有 .rt-line (重测那几行跟主数一样大)",
    ),
    (
        "server/report.py",
        "    for idx, mr in enumerate(retest_runs or [], 1):",
        "    for idx, mr in enumerate([], 1):",
        "REPORT: 重测轮的时间压根不收 (报告里查不到重测跑了多久)",
    ),
    (
        "server/report.py",
        '            "started_at": _fmt_time(ms),\n            "finished_at": _fmt_time(mf),',
        '            "started_at": _fmt_time(mf),\n            "finished_at": _fmt_time(ms),',
        "REPORT: 重测轮的起点和终点**写反了**",
    ),
    (
        "server/report.py",
        "            mdur = (float(mf) - float(ms)\n"
        "                    if ms is not None and mf is not None else None)",
        "            mdur = (float(mf) - float(ms)\n"
        "                    if ms is not None and mf is not None else total_dur)",
        "REPORT: 重测轮的耗时写成了整轮的耗时",
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
    tmp = tempfile.mkdtemp(prefix="mut_durations_")
    # 复制一份: server/ + config.yaml + 那个测试文件
    shutil.copytree(os.path.join(REPO, "server"), os.path.join(tmp, "server"),
                    ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy2(os.path.join(REPO, "config.yaml"),
                 os.path.join(tmp, "config.yaml"))
    os.makedirs(os.path.join(tmp, "tests"), exist_ok=True)
    shutil.copy2(os.path.join(REPO, "tests", "test_report_durations.py"),
                 os.path.join(tmp, "tests", "test_report_durations.py"))
    # ⚠️ **_pdftext.py 也要复制** —— 那个测试的第 8 个场景要**真导一份 PDF**
    #   并从 PDF 字节里把字抠出来。漏了它的话, 测试一 import 就崩,
    #   崩了也算「红」—— 那种红是**假的**(这个脚本自己会把它判成
    #   「崩了, 不算数」, 但那样一个变异都测不了)。
    shutil.copy2(os.path.join(REPO, "tests", "_pdftext.py"),
                 os.path.join(tmp, "tests", "_pdftext.py"))
    # ⚠️ **fixtures/ 也要复制** —— 那个测试要读 tests/fixtures/duration_formats.json
    #   (前端和报告共用的那张对照表)。漏了的话, 测试跑到 case5 就崩,
    #   崩了也算「红」—— 那种红是**假的**, 跟断言咬不咬得住没关系。
    shutil.copytree(os.path.join(REPO, "tests", "fixtures"),
                    os.path.join(tmp, "tests", "fixtures"))
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
            f.write(orig.replace(frm, to))
        code, out = run_one(tmp)
        fails = out.count("✗")
        if code != 0 and fails == 0:
            # ⚠️ **红了但一个 ✗ 都没有** —— 那多半不是断言把它咬住的, 而是
            #   测试自己崩了(语法错/导入错/异常)。这不算「咬得动」——
            #   崩了只能证明「跑不起来」, 证明不了「断言抓得住这个缺陷」。
            #   同样的道理, 变异把代码改到**语法都不对**时, 那个变异也是
            #   白做的(它压根没测到要测的东西)。
            print(f"\n✗ {desc}\n   → 红了, 但一个 ✗ 都没有 (exit={code}) —— "
                  f"这是**崩了**, 不是被断言咬住的, 不算数:\n"
                  f"{out[-800:]}")
            bad += 1
        elif code != 0:
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
