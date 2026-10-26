"""回归测试: 报告里的「本次配置快照」只列**本轮勾选了的**项目。

起因: 产线只勾了 USB 跑了一轮, 打开报告发现「本次配置快照」里并排摆着
CPU / GPU / WiFi / 网口 / HDMI / 风扇 / eMMC / TF 卡 / 内存 —— **十项**。
纸面上读起来像"这轮测了十项", 实际只测了一项。

为什么这是个真问题而不是排版洁癖: 报告是**纸面归档**用的, 归档后没人再去
对数据库。快照表里摆着没勾的项目, 事后翻报告的人(质量/返修)分不清
"这一轮测了这些" 和 "机器支持这些" —— 两种解读导出完全相反的结论。

这个测试直接调 `report.render_html()`, 不导出 PDF、不碰真库、不碰真硬件。
跑法: .venv/bin/python -m tests.test_report_config_snapshot
"""
import os
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


def snapshot_section(html: str) -> str:
    """只截「本次配置快照」那张表 —— 别的段落里也可能出现测试项的名字
    (比如「逐项结果」表), 整页断言会互相干扰。"""
    i = html.find("<h2>本次配置快照</h2>")
    if i < 0:
        return ""
    j = html.find("</table>", i)
    return html[i:j if j > 0 else len(html)]


def mk_run(tests_cfg, results=None, run_id=1):
    return {
        "id": run_id, "started_at": 0, "finished_at": 1, "status": "passed",
        "sn": "SN-X", "operator": "测试员", "batch": None,
        "mini_of": None, "error": None,
        "summary": {"total": 1, "passed": 1, "failed": 0, "error": 0,
                    "skipped": 0},
        "config": {"tests": tests_cfg},
    }, results or []


ALL_TESTS = {
    "cpu": {"enabled": False, "times": 1, "duration_sec": 60},
    "gpu": {"enabled": False, "rounds": 1, "timeout_sec": 600},
    "wifi": {"enabled": False, "times": 1, "iface": "wlan0"},
    "usb": {"enabled": True, "times": 3, "duration_sec": 18, "size_mb": 32},
    "ethernet": {"enabled": False, "times": 1},
    "hdmi": {"enabled": False, "target_resolution": "1920x1080@60"},
    "fan": {"enabled": False, "cycles": 2},
    "emmc": {"enabled": False, "times": 1, "duration_sec": 30},
    "tfcard": {"enabled": False, "inserted": False},
    "memory": {"enabled": False, "times": 1, "timeout_sec": 60},
}


def main():
    print("【1】只勾了 USB: 配置快照里就只有 USB 一项")
    run, results = mk_run(ALL_TESTS, [
        {"test_name": "usb", "status": "passed", "duration_sec": 0.2,
         "metrics": {}, "log": "", "error": None},
    ])
    html = report_mod.render_html(run, results, "t.pdf")
    sec = snapshot_section(html)
    check("USB 接口" in sec, "USB 接口 在快照里")
    for name in ("CPU 压力", "GPU 渲染", "WiFi 验证", "网口", "HDMI 输出",
                 "风扇 GPIO", "eMMC 存储", "TF 卡", "内存"):
        check(name not in sec, f"没勾的「{name}」不在快照里")
    # 逐项结果表**不受影响** —— 跑过的东西还得在结果表里
    check("USB 接口" in html, "逐项结果表里 USB 还在")

    print("\n【2】勾了但被跳过 (TF 卡没插): 照样要列 —— 勾了就是勾了")
    cfg = dict(ALL_TESTS)
    cfg["tfcard"] = {"enabled": True, "inserted": False, "duration_sec": 30}
    cfg["usb"] = {"enabled": True, "times": 3}
    run, results = mk_run(cfg, [
        {"test_name": "usb", "status": "passed", "duration_sec": 0.2,
         "metrics": {}, "log": "", "error": None},
        {"test_name": "tfcard", "status": "skipped", "duration_sec": 0.0,
         "metrics": {}, "log": "", "error": "未检测到 TF 卡挂载点"},
    ])
    sec = snapshot_section(report_mod.render_html(run, results, "t.pdf"))
    check("TF 卡" in sec, "勾了但跳过的 TF 卡 也在快照里")
    check("CPU 压力" not in sec, "没勾的 CPU 仍然不在")

    print("\n【3】老库的配置里没有 enabled 键: 靠「实际跑过」兜底")
    # 这种 run 的 config 是老格式, 没有 enabled 字段。按 _build_test_list 的
    # `cfg.get("enabled", False)` 判定, 没键 = 没勾 —— 但它**确实跑过**,
    # 结果表里在, 快照里就不该消失 (否则老报告的配置快照整个空掉)。
    old_cfg = {"usb": {"times": 3, "duration_sec": 18}}
    run, results = mk_run(old_cfg, [
        {"test_name": "usb", "status": "passed", "duration_sec": 0.2,
         "metrics": {}, "log": "", "error": None},
    ])
    sec = snapshot_section(report_mod.render_html(run, results, "t.pdf"))
    check("USB 接口" in sec, "跑过的项没被过滤掉", sec[-200:])

    print("\n【4】一个都没勾: 占位符要说「没勾」, 不是「没记录快照」")
    cfg = {k: dict(v, enabled=False) for k, v in ALL_TESTS.items()}
    run, results = mk_run(cfg, [])
    sec = snapshot_section(report_mod.render_html(run, results, "t.pdf"))
    check("(本次未勾选任何测试项)" in sec,
          "显示「本次未勾选任何测试项」", sec[-200:])
    check("(未记录配置快照)" not in sec, "没有误报成「未记录配置快照」")

    print("\n【5】压根没有 tests 段 (老 run): 「未记录配置快照」")
    run, results = mk_run({}, [])
    run["config"] = {}
    sec = snapshot_section(report_mod.render_html(run, results, "t.pdf"))
    check("(未记录配置快照)" in sec, "显示「未记录配置快照」", sec[-200:])

    print("\n【6】enabled=True 不再出现在参数里 (出现在表里本身就是证据)")
    run, results = mk_run(ALL_TESTS, [
        {"test_name": "usb", "status": "passed", "duration_sec": 0.2,
         "metrics": {}, "log": "", "error": None},
    ])
    sec = snapshot_section(report_mod.render_html(run, results, "t.pdf"))
    check("enabled=True" not in sec, "参数列表里没有 enabled=True")
    check("times=3" in sec, "真正的参数还在", sec[-200:])

    print("\n【7】mini-run: 只列重测的那几项")
    # mini-run 的 config["tests"] 就是重测项(见 runner.start_mini), 父轮那
    # 些没重测的项目压根不在里面 —— 快照自然就只有重测的那几项。
    mini_cfg = {"emmc": {"enabled": True, "times": 1, "duration_sec": 30},
                "cpu": {"enabled": True, "times": 1, "duration_sec": 60}}
    run, results = mk_run(mini_cfg, [
        {"test_name": "emmc", "status": "passed", "duration_sec": 30.0,
         "metrics": {}, "log": "", "error": None},
        {"test_name": "cpu", "status": "failed", "duration_sec": 60.0,
         "metrics": {}, "log": "", "error": "温度超限"},
    ])
    run["mini_of"] = 120
    sec = snapshot_section(report_mod.render_html(run, results, "t.pdf"))
    check("eMMC 存储" in sec and "CPU 压力" in sec, "重测的两项都在")
    check("USB 接口" not in sec, "没重测的不在")

    print("\n【8】快照表上方要有一句说明 —— 别让人以为报告漏了内容")
    run, results = mk_run(ALL_TESTS, [
        {"test_name": "usb", "status": "passed", "duration_sec": 0.2,
         "metrics": {}, "log": "", "error": None},
    ])
    html = report_mod.render_html(run, results, "t.pdf")
    check("仅列本轮勾选" in html, "有「仅列本轮勾选并执行的项目」的说明")

    print("\n" + ("全部通过" if not FAILS else f"有失败项 ({len(FAILS)})"))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
