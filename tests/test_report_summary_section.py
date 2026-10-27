"""回归测试: PDF 报告的**汇总段**要把「测了什么 / 没测什么」说清楚。

起因: §9.2 的 total 口径改了 (total = 实际执行的项数, skipped / stopped
不计入), 但报告的汇总段还在印老口径:

    通过 passed | 失败 failed | 错误 error | 未测 skipped | 合计 total

    · 「合计」印的是 total(= 实际执行数), 却跟「勾选数」混为一谈 ——
      一轮勾了 9 项只测了 6 项, 报告上写「合计 6」, 纸面读者会以为
      这轮就测了 6 项, 剩下 3 项不知去向。
    · 「未测项明细」只列 skipped, **漏掉 stopped**(中途停止时没跑到的那些)——
      看上去像"这轮全测了", 实际有一半的项压根没跑。
    · total == 0 时那句警告写的是「勾选数为 0 或全部未跑到」, 跟新的
      口径对不上, 而且没告诉读者**勾选了几项**。

这轮把汇总段改成三个数分清楚 (已测 / 未测 / 未跑到 / 共勾选) + 一行通过率
(带分母说明) + 中文整体结论。

这个测试直接调 `report.render_html()`, 不导出 PDF、不碰真库、不碰真硬件。
跑法: .venv/bin/python -m tests.test_report_summary_section
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import report as report_mod

FAILS = []
PASSES = 0


def check(cond, msg, extra=""):
    global PASSES
    if cond:
        PASSES += 1
        print("    ✓ " + msg)
    else:
        FAILS.append(msg)
        print("    ✗ " + msg + ("  → " + str(extra) if extra else ""))


def make_run(status="passed", summary=None, **over):
    run = {
        "id": 121,
        "sn": "SN-TEST-0001",
        "operator": "产线甲",
        "status": status,
        "started_at": 1000,
        "finished_at": 2000,
        "summary": summary if summary is not None else {
            "total": 2, "passed": 1, "failed": 1, "error": 0,
            "skipped": 0, "not_run": 0, "planned": 2,
        },
        "config": {"tests": {}},
    }
    run.update(over)
    return run


def render(run, results):
    return report_mod.render_html(run, results, "SN-TEST-0001_20261027_100000.pdf")


def summary_section(html: str) -> str:
    """只截「汇总」那一段 —— 逐项段里也有测试项名字, 整页断言会串。"""
    i = html.find("<h2>汇总</h2>")
    if i < 0:
        return ""
    j = html.find("<h2>逐项结果</h2>", i)
    return html[i:j if j > 0 else len(html)]


def case1():
    print("【1】汇总表要把 已测 / 未测 / 未跑到 / 共勾选 分清楚")
    run = make_run(summary={"total": 2, "passed": 1, "failed": 1, "error": 0,
                            "skipped": 1, "not_run": 2, "planned": 5})
    results = [
        {"test_name": "cpu", "status": "passed", "duration_sec": 1.0,
         "metrics": {}, "error": None, "log": "x"},
        {"test_name": "gpu", "status": "failed", "duration_sec": 1.0,
         "metrics": {}, "error": "温度超了", "log": "x"},
        {"test_name": "usb", "status": "skipped", "duration_sec": 0.0,
         "metrics": {}, "error": "未检测到任何 USB 设备", "log": ""},
        {"test_name": "wifi", "status": "stopped", "duration_sec": 0.0,
         "metrics": {}, "error": "用户中途停止, 本项未执行", "log": ""},
        {"test_name": "fan", "status": "stopped", "duration_sec": 0.0,
         "metrics": {}, "error": "用户中途停止, 本项未执行", "log": ""},
    ]
    html = render(run, results)
    sec = summary_section(html)
    check("共勾选 planned" in sec, "有「共勾选」这一列", sec[:200])
    check("已测 total" in sec, "有「已测」这一列", sec[:200])
    check("未跑到 stopped" in sec, "有「未跑到」这一列", sec[:200])
    check("未测 skipped" in sec, "有「未测」这一列", sec[:200])
    # 五个数都得在
    for label, val in (("通过", 1), ("失败", 1), ("错误", 0),
                       ("未测", 1), ("未跑到", 2), ("已测", 2), ("共勾选", 5)):
        check(re.search(rf"<td[^>]*>\s*{val}\s*</td>", sec) is not None,
              f"「{label}」的数是 {val}", sec[sec.find("<tr>", 40):][:400])


def case2():
    print("\n【2】通过率要带分母说明 (已测 M / 共勾选 K)")
    run = make_run(summary={"total": 2, "passed": 2, "failed": 0, "error": 0,
                            "skipped": 1, "not_run": 2, "planned": 5})
    results = [
        {"test_name": "cpu", "status": "passed", "duration_sec": 1.0,
         "metrics": {}, "error": None, "log": "x"},
        {"test_name": "gpu", "status": "passed", "duration_sec": 1.0,
         "metrics": {}, "error": None, "log": "x"},
    ]
    html = render(run, results)
    sec = summary_section(html)
    check("通过率" in sec, "有「通过率」", sec[:300])
    check("2 / 2" in sec, "通过率写成 2 / 2", sec[:400])
    check("已测 2 项" in sec and "共勾选 5 项" in sec,
          "分母说清了 (已测 2 / 共勾选 5)", sec[:500])


def case3():
    print("\n【3】未测项明细: skipped **和** stopped 都要列出来")
    run = make_run(status="stopped",
                   summary={"total": 1, "passed": 1, "failed": 0, "error": 0,
                            "skipped": 1, "not_run": 2, "planned": 4})
    results = [
        {"test_name": "cpu", "status": "passed", "duration_sec": 1.0,
         "metrics": {}, "error": None, "log": "x"},
        {"test_name": "usb", "status": "skipped", "duration_sec": 0.0,
         "metrics": {}, "error": "未检测到任何 USB 设备", "log": ""},
        {"test_name": "wifi", "status": "stopped", "duration_sec": 0.0,
         "metrics": {}, "error": "用户中途停止, 本项未执行", "log": ""},
        {"test_name": "fan", "status": "stopped", "duration_sec": 0.0,
         "metrics": {}, "error": "用户中途停止, 本项未执行", "log": ""},
    ]
    html = render(run, results)
    sec = summary_section(html)
    check("未测项明细" in sec, "有「未测项明细」", sec[:300])
    # 三项都得在明细表里
    for name in ("USB 接口", "WiFi", "风扇"):
        check(name in sec, f"明细里有「{name}」", sec[:900])
    check("st-skipped" in sec, "标了 skipped 的状态色块", sec[:900])
    check("st-stopped" in sec, "标了 stopped 的状态色块", sec[:900])
    check("用户中途停止" in sec, "写了没跑到的原因", sec[:900])


def case4():
    print("\n【4】一项都没执行 → 黄条要说清「勾选了几项, 一项都没跑」")
    run = make_run(status="skipped",
                   summary={"total": 0, "passed": 0, "failed": 0, "error": 0,
                            "skipped": 2, "not_run": 1, "planned": 3})
    results = [
        {"test_name": "usb", "status": "skipped", "duration_sec": 0.0,
         "metrics": {}, "error": "未检测到任何 USB 设备", "log": ""},
        {"test_name": "wifi", "status": "skipped", "duration_sec": 0.0,
         "metrics": {}, "error": "没插网卡", "log": ""},
        {"test_name": "fan", "status": "stopped", "duration_sec": 0.0,
         "metrics": {}, "error": "用户中途停止, 本项未执行", "log": ""},
    ]
    html = render(run, results)
    sec = summary_section(html)
    check("本次未实际执行任何测试项" in sec, "黄条还在", sec[:400])
    check("共勾选了 3 项" in sec, "写清了勾选了几项", sec[:400])
    check("不构成合格判定" in sec, "写清了不构成合格判定", sec[:400])
    # 整体结论也要说人话
    check("不构成合格判定" in html, "整体结论也说了不作合格判定", html[:2000])


def case5():
    print("\n【5】整体结论要给中文说法 (不能只印一个英文状态词)")
    for status, expect in (("passed", "已测项目全部通过"),
                            ("failed", "不合格"),
                            ("skipped", "不构成合格判定"),
                            ("stopped", "未跑的不算数")):
        html = render(make_run(status=status), [])
        check(expect in html, f"status={status} → 结论里有「{expect}」",
              html[:2000])


def case6():
    print("\n【6】老库里的旧 run (summary 里没有 planned/not_run) 也不能印出「共勾选 0」")
    run = make_run(summary={"total": 3, "passed": 3, "failed": 0, "error": 0,
                            "skipped": 0})
    html = render(run, [])
    sec = summary_section(html)
    check("已测 3 项" in sec, "按可推的口径补出了勾选数", sec[:500])


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
