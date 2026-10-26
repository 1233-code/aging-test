"""回归测试: 报告里的日志必须是**完整的**, 不是节选。

起因: 产线反馈「报告中的日志要不就显示全, 要不然就不要显示, 我推荐是显示全」。
查下来报告里的日志确实是"半成品", 而且是**三种半成品混在一起**:

    测试项            实时日志      落进报告的日志
    ─────────────────────────────────────────────
    CPU               ✓            末尾 400 行 (源头就被截了)
    GPU               ✓            末尾 20 行
    内存               ✓            末尾 200 行
    WiFi/USB/网口/    ✓            **空** —— 报告显示「(无日志)」
    HDMI/风扇/TF/eMMC

外加逐项段里还印着「stderr 末若干行」—— 同一份日志的**残缺副本**。

为什么"半截"比"没有"更糟: 报告是**纸面归档**, 事后拿它复盘/申诉。残缺的
日志看着像日志, 纸面读者会以为那就是全部, 不会想到要去别处找; 而"没有
日志"他至少知道没记录。**缺了头的日志比没有日志更危险**。

这个测试直接调 `report.render_html()`, 不导出 PDF、不碰真库、不碰真硬件。
跑法: .venv/bin/python -m tests.test_report_full_log
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


def make_run(**over):
    run = {
        "id": 121,
        "sn": "SN-TEST-0001",
        "operator": "产线甲",
        "status": "passed",
        "started_at": 1000,
        "finished_at": 2000,
        "summary": {"total": 3, "passed": 2, "failed": 1, "error": 0, "skipped": 0},
        "config": {"tests": {}},
    }
    run.update(over)
    return run


def render(run, results):
    return report_mod.render_html(run, results, "SN-TEST-0001_20261026_100000.pdf")


def log_appendix(html: str) -> str:
    """只截「日志附录」那一段 —— 逐项段里也有测试项名字, 整页断言会串。"""
    i = html.find("<h2>日志附录</h2>")
    if i < 0:
        return ""
    j = html.find("</body>", i)
    return html[i:j if j > 0 else len(html)]


def case1():
    print("【1】每一项的日志必须**全**进附录 —— 哪怕有一百行")
    long_log = "\n".join(f"第 {i} 行日志" for i in range(1, 101))
    results = [
        {"test_name": "usb", "status": "passed", "duration_sec": 1.0,
         "metrics": {}, "error": None, "log": long_log},
    ]
    html = render(make_run(), results)
    ap = log_appendix(html)
    missing = [i for i in range(1, 101) if f"第 {i} 行日志" not in ap]
    check(not missing, "100 行日志一行不少", f"少了 {len(missing)} 行: {missing[:5]}")
    check("第 1 行日志" in ap, "第一行也在 (不是只留尾巴)")
    check("第 100 行日志" in ap, "最后一行也在")


def case2():
    print("\n【2】十项都跑了 → 十项的日志都得在附录里 (以前七项是空的)")
    results = []
    for name in ("cpu", "gpu", "memory", "wifi", "usb", "ethernet",
                 "hdmi", "fan", "tfcard", "emmc"):
        results.append({
            "test_name": name, "status": "passed", "duration_sec": 1.0,
            "metrics": {}, "error": None,
            "log": f"[{name}] 这一项的日志内容",
        })
    html = render(make_run(), results)
    ap = log_appendix(html)
    missing = [n for n in ("cpu", "gpu", "memory", "wifi", "usb", "ethernet",
                           "hdmi", "fan", "tfcard", "emmc")
               if f"[{n}] 这一项的日志内容" not in ap]
    check(not missing, "十项的日志全在附录里", f"缺: {missing}")
    check("(无日志)" not in ap, "附录里不再有「(无日志)」这种含糊占位")


def case3():
    print("\n【3】一项都没产生日志 → 说清楚是「没跑」还是「跑完没记录」")
    results = [
        {"test_name": "usb", "status": "passed", "duration_sec": 1.0,
         "metrics": {}, "error": None, "log": ""},
        {"test_name": "tfcard", "status": "skipped", "duration_sec": 0,
         "metrics": {}, "error": None, "log": ""},
    ]
    html = render(make_run(), results)
    ap = log_appendix(html)
    check("本项没有产生任何日志" in ap,
          "跑过但没日志 → 明说「没有产生任何日志」")
    check("本项被跳过, 未执行" in ap,
          "被跳过的项 → 明说「被跳过, 未执行」")
    check("(无日志)" not in ap, "不再有「(无日志)」")


def case4():
    print("\n【4】逐项段里**不再**有半截日志的副本")
    results = [
        {"test_name": "cpu", "status": "failed", "duration_sec": 1.0,
         "metrics": {}, "error": "温度超限",
         "log": "line-1\nline-2\nline-3"},
    ]
    html = render(make_run(), results)
    check("stderr 末若干行" not in html,
          "逐项段里没有「stderr 末若干行」这个残缺副本了")
    check("本项完整日志见文末" in html,
          "留了一句指路, 免得读者以为这一项没日志")
    # 指路那句要出现在**附录之前** —— 否则读者先看到附录再看到指路, 绕
    i_ptr = html.find("本项完整日志见文末")
    i_apx = html.find("<h2>日志附录</h2>")
    check(0 <= i_ptr < i_apx, "指路在附录之前", f"ptr={i_ptr} apx={i_apx}")


def case5():
    print("\n【5】日志被上限截断过 → 报告必须**说出来**")
    results = [
        {"test_name": "cpu", "status": "passed", "duration_sec": 1.0,
         "metrics": {"log_truncated": "是", "log_kept_lines": 10000,
                     "log_dropped_lines": 37},
         "error": None, "log": "剩下的日志"},
    ]
    html = render(make_run(), results)
    ap = log_appendix(html)
    check("日志过长" in ap, "附录里写明了日志被截断")
    check("37" in ap, "写明了丢了多少行")
    check("只保留了末尾 10000 行" in ap, "写明了保留了多少行")


def case6():
    print("\n【6】附录开头要写清楚「这是完整日志」")
    results = [{"test_name": "usb", "status": "passed", "duration_sec": 1.0,
                "metrics": {}, "error": None, "log": "x"}]
    html = render(make_run(), results)
    ap = log_appendix(html)
    check("完整日志" in ap, "附录上方有一句说明")
    check("未截断、未删节" in ap, "明写未截断")


def case7():
    print("\n【7】失败项的**完整**原因仍然在 (不能因为去日志摘录就丢了原因)")
    results = [
        {"test_name": "cpu", "status": "failed", "duration_sec": 1.0,
         "metrics": {}, "error": "峰值温度 95°C 超过 85°C",
         "log": "a\nb\nc"},
    ]
    html = render(make_run(), results)
    check("峰值温度 95°C 超过 85°C" in html, "失败原因完整保留")
    check("失败原因" in html, "失败原因那一栏还在")


def case8():
    print("\n【8】CSS 里不许再剪日志 (max-height + overflow:hidden 是静默截断)")
    # 这条是**静默**的: 模板上写着 `max-height: 235mm; overflow: hidden`,
    # 日志超过一页, 超出部分被剪掉, 而报告上一点看不出来 —— 被剪掉的恰恰
    # 是尾巴 (报错、失败原因都在尾巴上)。剪掉的日志比半截日志更坏: 半截
    # 至少还看得出是半截, 剪掉的看起来是完整的。
    tpl_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "server", "templates", "report.html.j2")
    tpl = open(tpl_path, encoding="utf-8").read()
    # 先把 CSS 注释剥掉 —— 注释里会**提到** "max-height" / "overflow: hidden"
    # (就是在讲为什么不能有它们), 不剥的话这个断言会拿注释当成声明。
    tpl_nc = re.sub(r"/\*.*?\*/", "", tpl, flags=re.S)
    i = tpl_nc.find("pre.log {")
    block = tpl_nc[i:tpl_nc.find("}", i)] if i >= 0 else ""
    check(bool(block), "找得到 pre.log 的样式块")
    check("max-height" not in block,
          "pre.log 没有 max-height (超出部分不会被剪掉)", block.replace("\n", " "))
    check("overflow: hidden" not in block and "overflow:hidden" not in block,
          "pre.log 没有 overflow:hidden (同上)")
    # 整个模板里也不该有别的地方剪日志
    check("overflow: hidden" not in tpl_nc and "overflow:hidden" not in tpl_nc,
          "整个模板里没有 overflow:hidden")


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
