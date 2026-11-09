"""回归测试: 报告里的**时间** —— 总耗时 + 每一项的单项耗时。

起因 (2026-10-28, 产线原话):「测试的时候加一个时间读秒 …… 然后最后这个
时间在测试界面和报告中都要体现, 报告中既要有测试总时间, 也要每一项上
展示单项测试的时间」。

要盯住的:
    ① **总耗时**在报告上有两处: 报告头那张表里有一行「总耗时」, 汇总段里
       也要再说一遍 —— 汇总段是**读者第一眼看的地方**, 只在页眉的小表里
       写一个数, 多数人翻过去根本看不见。
    ② **每一项自己的耗时**要在「逐项结果」段里印出来 (case-meta), 未测项
       也要在「未测项明细」表里有一列 —— 那些项虽然没跑, 但质检要对着
       「它到底花了多久 / 是不是压根没花时间」来判断能不能放行。
    ③ **屏幕上的数跟报告里的数必须是同一个数、同一套写法** —— 前端的
       `fmtDur` 和这里的 `_fmt_dur` 拿的是**同一张对照表**
       (tests/fixtures/duration_formats.json), 谁改歪了谁红。

这个测试直接调 `report.render_html()`, 不导出 PDF、不碰真库、不碰真硬件。
跑法: .venv/bin/python -m tests.test_report_durations
"""
import asyncio
import json
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from server import report as report_mod
from _pdftext import page_texts

FAILS = []
PASSES = 0

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def check(cond, msg, extra=""):
    global PASSES
    if cond:
        PASSES += 1
        print("    ✓ " + msg)
    else:
        FAILS.append(msg)
        print("    ✗ " + msg + ("  → " + str(extra) if extra else ""))


def make_run(started=1000.0, finished=1600.0, **over):
    run = {
        "id": 7,
        "sn": "SN-TEST-0007",
        "operator": "产线甲",
        "status": "passed",
        "started_at": started,
        "finished_at": finished,
        "summary": {"total": 3, "passed": 2, "failed": 0, "error": 0,
                    "skipped": 1, "not_run": 0, "planned": 4},
        "config": {"tests": {}},
    }
    run.update(over)
    return run


def render(run, results, retest_runs=None):
    return report_mod.render_html(run, results, "SN-TEST-0007_20261028_100000.pdf",
                                   retest_runs=retest_runs)


def section_of(html, title, next_title):
    """截出某一节 (标题到下一节标题之间) —— 整页断言会串。

    ⚠️ 下一节的查找**必须从本节标题之后开始** —— 否则 `"<h2>逐项结果</h2>"`
       这种标题自己就以 `"<h2>"` 开头, find 会**找到它自己**, 截出空串,
       断言会莫名其妙地全红(踩过)。
    """
    i = html.find(title)
    if i < 0:
        return ""
    j = html.find(next_title, i + len(title))
    return html[i:j if j > 0 else len(html)]


def kv_cell(html, label):
    """报告头那张表里, 某一行的第二个格子的**原始 HTML**。

    ⚠️ 用 `(.*?)` 而不是 `([^<]*)` —— §7.8.6 之后这一格可能不止一个值
       (原值后面还跟着重测轮的行), 只取「第一个 `<` 之前」那段会把重测
       的时间整个漏掉, 断言就成了摆设。
    """
    m = re.search(r"<tr>\s*<td>" + re.escape(label) + r"</td>\s*<td>(.*?)</td>",
                  html, re.S)
    return m.group(1) if m else None


def cell_lines(cell):
    """把一个格子的 HTML 拆成**一行一个值**(去掉 <br> 和 <span> 标签)。"""
    if cell is None:
        return []
    txt = re.sub(r"<br\s*/?>", "\n", cell)
    txt = re.sub(r"<[^>]+>", "", txt)
    return [ln.strip() for ln in txt.split("\n") if ln.strip()]


def make_retest(rid, started, finished, tests=("cpu",)):
    return {
        "id": rid,
        "started_at": started,
        "finished_at": finished,
        "status": "passed",
        "results": [{"test_name": t, "status": "passed", "attempt": 2,
                      "log": "x"} for t in tests],
    }


def case1():
    print("【1】总耗时: 报告头表里有, **汇总段里也要再写一遍**")
    run = make_run(started=1000.0, finished=1600.0)
    results = [
        {"test_name": "cpu", "status": "passed", "duration_sec": 12.5,
         "metrics": {}, "error": None, "log": "x"},
        {"test_name": "gpu", "status": "passed", "duration_sec": 30.25,
         "metrics": {}, "error": None, "log": "x"},
        {"test_name": "usb", "status": "skipped", "duration_sec": 0.0,
         "metrics": {}, "error": "未检测到任何 USB 设备", "log": ""},
    ]
    html = render(run, results)
    want = report_mod._fmt_dur(600.0)          # 600 秒 = 10 分钟
    check(want == "10m00s", "600 秒写成 10m00s", want)

    # 报告头那张表
    m = re.search(r"<tr>\s*<td>总耗时</td>\s*<td>([^<]*)</td>\s*</tr>", html)
    check(m is not None, "报告头表里有「总耗时」这一行")
    if m:
        check(m.group(1).strip() == want,
              f"报告头表里的总耗时是 {want}", m.group(1).strip())

    # 汇总段
    sec = section_of(html, "<h2>汇总</h2>", "<h2>逐项结果</h2>")
    check("总耗时" in sec, "汇总段里也有「总耗时」", sec[:600])
    check(want in sec, f"汇总段里的总耗时是 {want}", sec[:600])


def case2():
    print("\n【2】每一项**自己的**耗时要在「逐项结果」段里印出来")
    run = make_run()
    results = [
        {"test_name": "cpu", "status": "passed", "duration_sec": 12.5,
         "metrics": {}, "error": None, "log": "x"},
        {"test_name": "gpu", "status": "passed", "duration_sec": 30.25,
         "metrics": {}, "error": None, "log": "x"},
        {"test_name": "usb", "status": "skipped", "duration_sec": 0.0,
         "metrics": {}, "error": "未检测到任何 USB 设备", "log": ""},
    ]
    html = render(run, results)
    sec = section_of(html, "<h2>逐项结果</h2>", "<h2>")
    check("耗时" in sec, "逐项段里印了「耗时」", sec[:800])
    for sec_val, label in ((12.5, "cpu"), (30.25, "gpu")):
        want = report_mod._fmt_dur(sec_val)
        check(f"耗时 {want}" in sec,
              f"{label} 这一项印了「耗时 {want}」", sec[:1200])
    # ⚠️ **数字前面要有「耗时」两个字** —— 光一个孤零零的 "12.5s" 摆在
    #    一堆指标里, 读者不知道那是耗时还是某个指标的值。
    check(re.search(r"耗时\s*12\.5s", sec) is not None,
          "耗时前面带「耗时」两个字", sec[:1200])


def case3():
    print("\n【3】未测项明细表里也要有一列**耗时**")
    run = make_run(status="stopped")
    results = [
        {"test_name": "cpu", "status": "passed", "duration_sec": 5.0,
         "metrics": {}, "error": None, "log": "x"},
        {"test_name": "usb", "status": "skipped", "duration_sec": 0.0,
         "metrics": {}, "error": "未检测到任何 USB 设备", "log": ""},
        {"test_name": "wifi", "status": "stopped", "duration_sec": 0.0,
         "metrics": {}, "error": "用户中途停止, 本项未执行", "log": ""},
    ]
    html = render(run, results)
    sec = section_of(html, "<h2>汇总</h2>", "<h2>逐项结果</h2>")
    check("未测项明细" in sec, "有「未测项明细」", sec[:400])
    check(re.search(r"<th[^>]*>\s*耗时\s*</th>", sec) is not None,
          "明细表里有一列表头写着「耗时」", sec[:900])
    # 每一行都要印出这一项自己的耗时 (这里是 0.0s —— 没跑, 就是没花时间)
    check("0.0s" in sec, "明细行里印了耗时 (0.0s)", sec[:1200])


def case4():
    print("\n【4】没跑完 / 没记录时间的 run, 总耗时不能瞎编")
    # started 有、finished 没有 (中途停了 / 服务挂了) → 算不出总耗时
    html = render(make_run(started=1000.0, finished=None), [])
    check("总耗时" in html, "还是有一行「总耗时」", html[:2000])
    check("—" in html, "算不出来的写「—」(不是 0.0s, 0.0s 会被当成'瞬间跑完')",
          html[:2000])

    # 两个都没有
    html = render(make_run(started=None, finished=None), [])
    check("总耗时" in html, "还是没有 started/finished 时也有「总耗时」这一行",
          html[:2000])


def case5():
    print("\n【5】前端和报告的写法拿**同一张对照表**钉死")
    """`_fmt_dur` (报告) 必须和前端的 `fmtDur` (屏幕) 一个字不差。

    ⚠️ 这张表 tests/fixtures/duration_formats.json 是**两边共用的** ——
    前端那边 (tests/test_frontend_timer.js) 也拿它钉一遍。
    表里的 58.25 / 58.35 是**故意挑的「正好卡在中间」的值**:
    Python 的 f"{x:.1f}" 是「五成双」(58.25 → 58.2), JS 的 toFixed(1) 是
    「五入」(58.25 → 58.3)。两边原本会对不上 —— 工人盯着屏幕记个数,
    又翻报告对一眼, 差 0.1 秒他也会觉得不对劲。所以报告这边也改成五入。
    """
    with open(os.path.join(FIXTURES, "duration_formats.json"), encoding="utf-8") as f:
        table = json.load(f)
    check(len(table) > 0, "共用的对照表读到了")
    for sec, want in table:
        got = report_mod._fmt_dur(sec)
        check(got == want, f"{sec} 秒 → \"{want}\"", got)

    # 顺带钉一下「五入」这件事本身 —— 有人把 _fmt_dur 改回 f"{x:.1f}"
    # (五成双) 的话, 58.25 就变 58.2s, 跟屏幕对不上。
    check(report_mod._fmt_dur(58.25) == "58.3s",
          "卡在中间的数是「五入」(58.25 → 58.3s), 不是「五成双」",
          report_mod._fmt_dur(58.25))
    check(report_mod._fmt_dur(-1) == "—", "负数 → —", report_mod._fmt_dur(-1))


def case6():
    print("\n【6】总耗时是**整轮**的时间, 不是最长那一项")
    run = make_run(started=0.0, finished=600.0)
    results = [
        {"test_name": "cpu", "status": "passed", "duration_sec": 1.0,
         "metrics": {}, "error": None, "log": "x"},
        {"test_name": "gpu", "status": "passed", "duration_sec": 1.0,
         "metrics": {}, "error": None, "log": "x"},
    ]
    html = render(run, results)
    m = re.search(r"<tr>\s*<td>总耗时</td>\s*<td>([^<]*)</td>\s*</tr>", html)
    check(m is not None and m.group(1).strip() == "10m00s",
          "总耗时是 10m00s (不是各项之和, 也不是最长的那项)",
          m.group(1).strip() if m else None)


def case7():
    print("\n【7】有重测时: 原时间**保留**, 重测的那三项**接在后面**")
    """2026-10-28 工人原话:「如果有重测的情况, 可以在报告的开始时间结束时间
    和总耗时在**保留原先的数据的前提下**把重测的这三项时间填进去, 在原时间
    后面」。

    盯三件事:
      ① **原值不许被替换** —— 首测那一次是什么时候跑的, 那是**事实**;
         替换掉就等于篡改记录。
      ② 重测的「开始 / 结束 / 总耗时」**接在原值后面**, 各带「重测第 N 轮」
         的标签 —— 读者一眼分得清哪个是哪轮的。
      ③ **不累加** —— 12m00s 那种数**不许**出现: 把两轮加起来, 那个数
         既不是首测的、也不是重测的, 谁都对不上(跟「每项之和 ≠ 总耗时」
         同一个道理)。
    """
    run = make_run(started=1000.0, finished=1600.0, retest_count=2)
    results = [
        {"test_name": "cpu", "status": "passed", "duration_sec": 12.5,
         "metrics": {}, "error": None, "log": "x"},
        {"test_name": "gpu", "status": "passed", "duration_sec": 30.25,
         "metrics": {}, "error": None, "log": "x"},
    ]
    rs = [make_retest(8, 2000.0, 2120.0, ("cpu",)),
          make_retest(9, 3000.0, 3060.0, ("gpu",))]
    html = render(run, results, retest_runs=rs)

    r0_started = report_mod._fmt_time(1000.0)
    r0_finished = report_mod._fmt_time(1600.0)
    r0_dur = report_mod._fmt_dur(600.0)          # 10m00s
    r1_started = report_mod._fmt_time(2000.0)
    r1_finished = report_mod._fmt_time(2120.0)
    r1_dur = report_mod._fmt_dur(120.0)          # 2m00s
    r2_started = report_mod._fmt_time(3000.0)
    r2_finished = report_mod._fmt_time(3060.0)
    r2_dur = report_mod._fmt_dur(60.0)           # 1m00s

    # ── 开始时间 ──
    lines = cell_lines(kv_cell(html, "开始时间"))
    check(len(lines) >= 3, "开始时间那一格里有 3 行 (原值 + 两轮重测)", lines)
    check(lines and lines[0] == r0_started,
          f"第 1 行是**原值** {r0_started}", lines)
    check(f"重测第 1 轮: {r1_started}" in lines,
          f"接着是「重测第 1 轮: {r1_started}」", lines)
    check(f"重测第 2 轮: {r2_started}" in lines,
          f"再接着是「重测第 2 轮: {r2_started}」", lines)

    # ── 结束时间 ──
    lines = cell_lines(kv_cell(html, "结束时间"))
    check(lines and lines[0] == r0_finished,
          f"结束时间第 1 行是**原值** {r0_finished}", lines)
    check(f"重测第 1 轮: {r1_finished}" in lines,
          f"「重测第 1 轮: {r1_finished}」", lines)
    check(f"重测第 2 轮: {r2_finished}" in lines,
          f"「重测第 2 轮: {r2_finished}」", lines)

    # ── 总耗时 ──
    lines = cell_lines(kv_cell(html, "总耗时"))
    check(lines and lines[0] == r0_dur,
          f"总耗时第 1 行是**原值** {r0_dur}", lines)
    check(f"重测第 1 轮: {r1_dur}" in lines,
          f"「重测第 1 轮: {r1_dur}」", lines)
    check(f"重测第 2 轮: {r2_dur}" in lines,
          f"「重测第 2 轮: {r2_dur}」", lines)

    # ⚠️ **不许累加** —— 10m00s + 2m00s = 12m00s 那个数**不许**出现。
    check("12m00s" not in html, "两轮的时间**没有**被累加成 12m00s",
          "12m00s" in html)
    check("13m00s" not in html, "连三轮一起加的 13m00s 也没有", "13m00s" in html)

    # ── 汇总段也要跟一句 (汇总段才是读者真正会盯的那块) ──
    sec = section_of(html, "<h2>汇总</h2>", "<h2>逐项结果</h2>")
    check(f"重测第 1 轮: {r1_dur}" in sec,
          f"汇总段里也有「重测第 1 轮: {r1_dur}」", sec[:900])
    check(f"重测第 2 轮: {r2_dur}" in sec,
          f"汇总段里也有「重测第 2 轮: {r2_dur}」", sec[:900])

    # ── **没有重测**的记录: 一行重测的字都不许有 ──
    html2 = render(run, results)
    for label in ("开始时间", "结束时间", "总耗时"):
        lines2 = cell_lines(kv_cell(html2, label))
        check(len(lines2) == 1, f"没重测时「{label}」只有一行", lines2)
    check("重测第" not in html2, "没重测的报告里没有「重测第 N 轮」的字样")

    # ── 重测轮自己缺时间 → 写「—」, 不许编 ──
    rs_bad = [make_retest(8, None, None, ("cpu",))]
    html3 = render(run, results, retest_runs=rs_bad)
    lines3 = cell_lines(kv_cell(html3, "总耗时"))
    check(len(lines3) == 2 and lines3[1].endswith("—"),
          "重测轮没记时间时写「—」而不是 0.0s", lines3)

    # ── 重测的那几行要有**自己的样式** ──
    # 原值是**主数**, 重测的那些是补充说明 —— 两个印得一样大, 读者会以为是
    # 两行平级的「总耗时」, 反而不知道这一轮真正的耗时是哪个了。
    # ⚠️ 断言要盯**开始时间那一格** —— 别的格子也带 rt-line, 只看整页的话,
    #    把这一格的样式去掉, 页面上还有别处带着, 断言照样绿。
    check('class="rt-line"' in (kv_cell(html, "开始时间") or ""),
          "重测的那几行带自己的样式类 (rt-line)")
    tpl_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "server", "templates", "report.html.j2")
    with open(tpl_path, encoding="utf-8") as f:
        tpl = f.read()
    m_css = re.search(r"\.rt-line\s*\{([^}]*)\}", tpl)
    check(m_css is not None, "模板的 CSS 里定义了 .rt-line")
    if m_css:
        m_fs = re.search(r"font-size:\s*([\d.]+)pt", m_css.group(1))
        check(m_fs is not None and float(m_fs.group(1)) < 9.5,
              "重测那几行的字号比正文小 (原值才是主数)",
              m_fs.group(1) if m_fs else None)


def case8():
    print("\n【8】真导一份 PDF: 重测的那三项**真的印在纸上**")
    """HTML 里有 ≠ 纸上就有 —— 字体、分页、中文渲染都可能把它吃掉。

    跟 §8.2 日志那一轮同一条规矩(「不只信自己的检查器」): 自己渲染出来的
    HTML 只能证明**模板**写对了, 证明不了 chromium 印出来的那张纸上真的有
    那些字。所以这里真跑一遍打印, 把字从 PDF 里抠出来对一遍。

    ⚠️ PDF 落在**临时目录**里 —— 不碰生产的 reports/pdf/。
    """
    chromium = report_mod.find_chromium()
    if not chromium:
        print("    ⚠ 跳过: 没装 chromium, 纸上有没有字渲不出来"
              "(§11.7 要求必装 —— 产线机上应该有)")
        return

    run = make_run(started=1000.0, finished=1600.0, retest_count=1)
    results = [
        {"test_name": "cpu", "status": "passed", "duration_sec": 12.5,
         "metrics": {}, "error": None, "log": "CPU 日志"},
    ]
    rs = [make_retest(8, 2000.0, 2120.0, ("cpu",))]
    html = report_mod.render_html(run, results, "t.pdf", retest_runs=rs)

    # ⚠️ 临时目录用 `TemporaryDirectory` 而不是 `mkdtemp` —— 前者**跑完自动删**。
    #    `mkdtemp` 留下的残骸是**每次回归攒一个**(`/tmp/aging-durations-*`),
    #    跑几十遍就是几十个目录 —— 自己给自己制造垃圾。
    with tempfile.TemporaryDirectory(prefix="aging-durations-") as tmp:
        html_path = os.path.join(tmp, "r.html")
        pdf_path = os.path.join(tmp, "r.pdf")
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html)
        asyncio.run(report_mod._print_pdf(chromium, html_path, pdf_path))

        try:
            report_mod.verify_cjk(pdf_path)
            check(True, "导出的 PDF 里有中文字形 (verify_cjk 通过)")
        except Exception as e:
            check(False, "导出的 PDF 里有中文字形", e)
            return

        pages = page_texts(pdf_path)
        text = "\n".join(pages)
        check(any(p.strip() for p in pages),
              "逐页取字能取出内容 (取字失败的话下面的断言全是空转)",
              [len(p) for p in pages])

        r_dur = report_mod._fmt_dur(120.0)          # 2m00s
        check(f"重测第 1 轮: {r_dur}" in text,
              f"纸上印着「重测第 1 轮: {r_dur}」", text[:800])
        check(report_mod._fmt_dur(600.0) in text,
              "纸上也有原值那个总耗时 (10m00s)", text[:800])
        check(report_mod._fmt_time(2000.0) in text,
              "纸上印着重测那一轮的**开始时间**", text[:800])
        check(report_mod._fmt_time(2120.0) in text,
              "纸上印着重测那一轮的**结束时间**", text[:800])


def main():
    case1()
    case2()
    case3()
    case4()
    case5()
    case6()
    case7()
    case8()
    print("\n" + ("全部通过" if not FAILS else f"有失败项 ({len(FAILS)})"))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
