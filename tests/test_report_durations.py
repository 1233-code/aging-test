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
    ③ **屏幕上的数跟报告里的数必须是同一个数、同一��写法** —— 前端的
       `fmtDur` 和这里的 `_fmt_dur` 拿的是**同一张对照表**
       (tests/fixtures/duration_formats.json), 谁改歪了谁红。

这个测试直接调 `report.render_html()`, 不导出 PDF、不碰真库、不碰真硬件。
跑法: .venv/bin/python -m tests.test_report_durations
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import report as report_mod

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


def render(run, results):
    return report_mod.render_html(run, results, "SN-TEST-0007_20261028_100000.pdf")


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


def main():
    case1()
    case2()
    case3()
    case4()
    case5()
    case6()
    print("\n" + ("全部通过" if not FAILS else f"有失败项 ({len(FAILS)})"))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
