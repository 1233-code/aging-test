"""回归测试: §8.2 报告排版 —— 长表项**接着上一项排**, 续页**重复项名**。

起因 (2026-10-26): 产线反馈「一项测试表格很长就直接到下一页展示」。查下来
是模板里 `.case { break-inside: avoid; }` —— 那一项(放不下当前页剩余空间)
整块被推到下一页, 当前页底部空掉一大截, 整份报告还多一页。实测真实 GPU
那一轮: 改之前 5 页, 改之后 4 页; 第 1 页原先整页空白(只剩报告头), 改后
GPU 项直接接在后面开始。

那条规则本来的用意是「撕页时项和状态会走散」—— 用意没错, 但解法选错了:
真正该做的是**让项名跟着表一起重复**, 而不是禁止表跨页。
`<thead>` 是分页媒体里唯一能被浏览器逐页重画的内容, 所以项名+状态放进去,
表拆到哪一页, 那一页顶上就有一份。

本测试**真跑 chromium 打印 PDF**, 再逐页取字回答两个问题:
  ① 长表项有没有整块跳到下一页? (项名所在的页 == 上一项所在的页)
  ② 续页顶上有没有重复项名和列头?

⚠️ 不碰真库、不碰 reports/pdf/ —— PDF 落在临时目录里。
跑法: .venv/bin/python -m tests.test_report_pagination
"""
import asyncio
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from server import report as report_mod
from _pdftext import page_texts

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


# 一段"巨大"的**原始输出** —— 模仿真实 GPU 那一轮 glmark2 的完整输出
# (35 行 / 2600+ 字符)。⚠️ 它**不该**进指标表: 指标表只放量化结论, 完整
# 输出归日志附录(§8.2「日志只有一处, 残缺副本比没有更糟」)。
# 这里保留它, 是为了盯住「原始输出不许再被 repr 成一个巨大的单元格」。
RAW_OUTPUT = "\n".join(
    "[scene-%02d] duration=3: FPS: %d FrameTime: %.3f ms" % (i, 100 + i, 8.0 + i / 100)
    for i in range(40)
)


def mk_round(idx=1, score=147.0, scenes=33, status="passed", error=None):
    """一轮的**量化**字段 —— 真实形状(见 server/tests/gpu.py 的 rounds_detail)。

    ⚠️ 这里**没有** tail: 指标里不存原始输出(完整的那份在日志附录)。
    老库里存的旧形状(**带** tail)由 report._metric_views 兜住 ——
    见 tests/test_report_metric_subtable.py。
    """
    return {"round": idx, "score": score, "scenes": scenes, "scenes_expected": 33,
            "elapsed_sec": 105.3, "exit_code": 0, "status": status, "error": error}


def case_tables(html):
    """取出每个 `<table class="case-metrics">…</table>` 块。

    ⚠️ **不能用非贪婪正则** `<table class="case-metrics">(.*?)</table>` ——
    指标表现在**可能内嵌子表**(rounds_detail 的每轮明细), 非贪婪匹配会停在
    **子表**的 `</table>` 上, 把外层表的后半截(失败原因那一行!)切掉 ——
    断言于是「表里没有失败行」, 看着像模板错了, 其实是取块取错了。
    (跟 tests/_pdftext.py 里那个"非贪婪截在嵌套字典上"是同一类坑。)
    """
    out, pos = [], 0
    tag_open = '<table class="case-metrics">'
    while True:
        j = html.find(tag_open, pos)
        if j < 0:
            return out
        depth, k, end = 0, j, -1
        while True:
            nt = html.find('<table', k + 1)
            et = html.find('</table>', k + 1)
            if et < 0:
                break
            if 0 <= nt < et:
                depth += 1
                k = nt
            else:
                if depth == 0:
                    end = et
                    break
                depth -= 1
                k = et
        if end < 0:
            return out
        out.append(html[j:end + len('</table>')])
        pos = end + len('</table>')


def mk_run(results):
    # 汇总/整体结论**按结果算**, 别写死 —— 否则「一项失败」的样本配着
    # 「全过」的汇总, 报告自己跟自己打架, 看着像 bug。
    cnt = {s: sum(1 for r in results if r["status"] == s)
           for s in ("passed", "failed", "error", "skipped")}
    return {
        "id": 1, "started_at": 1750000000.0, "finished_at": 1750000100.0,
        "status": "failed" if cnt["failed"] or cnt["error"] else "passed",
        "sn": "SN-PAGINATE", "operator": "测试员",
        "batch": None, "mini_of": None, "error": None,
        "summary": {"total": len(results), **cnt},
        "config": {"tests": {"gpu": {"enabled": True, "rounds": 1}}},
    }


def mk_results():
    """一份样本: 短项 / GPU(带子表) / **长表项** / 无指标项。

    ⚠️ **长表项现在不是 GPU 了** —— GPU 的 rounds_detail 改成「每轮一行的
    子表」之后, 它的指标表变短了(§8.2.2)。所以这里另用 CPU 当长表项:
    三十几行**普通**指标行 —— 这才是「长表项不许整块跳页」真正要治的形状
    (一行不高, 表自然跨页, 续页重复抬头)。
    """
    long_metrics = {"duration_sec": 60, "threads": 8}
    for i in range(30):
        long_metrics["round_%02d_write_mbps" % i] = 10.8 + i / 100
    return [
        {"test_name": "memory", "status": "passed", "duration_sec": 60.4,
         "metrics": {"times": 1, "timeout_sec": 60}, "log": "内存日志", "error": None},
        {"test_name": "gpu", "status": "passed", "duration_sec": 105.2,
         "metrics": {"rounds": 1, "score": 147, "scenes_completed": 33,
                     "rounds_scores": "147",
                     "rounds_detail": [mk_round()]},
         "log": RAW_OUTPUT, "error": None},
        {"test_name": "cpu", "status": "passed", "duration_sec": 60.5,
         "metrics": long_metrics, "log": "CPU 日志", "error": None},
        # 没有指标的项 —— 标题只能是独立块(没有表就没有表头组)
        {"test_name": "hdmi", "status": "passed", "duration_sec": 0.1,
         "metrics": {}, "log": "HDMI 日志", "error": None},
    ]


def mk_failed_results(nrows=29):
    """一份**带失败项**的样本 —— 用来盯「失败原因不许落成一个没有项名的页」。

    ⚠️ nrows 是**页边界旋钮**: 给 GPU 项加 nrows 行填充指标, 指标表就多占
    nrows 行版面。实测(把原因框改回「表外」那份版本): **nrows=1 和 nrows=29
    两档**会让原因框孤立在某一页(顶上既没项名也没状态); 改版之后 0~39 档
    全部不孤立。所以测试盯住那两档 —— 押在会出事的档位上, 而不是随便一档。
    """
    metrics = {"score": 12, "rounds_scores": "12",
               "rounds_detail": [mk_round(1, 12, 12, "failed", "退出码 1")]}
    for i in range(nrows):
        metrics["filler_%02d" % i] = i
    return [
        {"test_name": "memory", "status": "passed", "duration_sec": 60.4,
         "metrics": {"times": 1, "timeout_sec": 60}, "log": "内存日志", "error": None},
        {"test_name": "gpu", "status": "failed", "duration_sec": 105.2,
         "metrics": metrics, "log": "GPU 日志",
         "error": "GPU 项失败: glmark2 退出码 1, 场景数 12/33 不达标。"},
        {"test_name": "cpu", "status": "passed", "duration_sec": 60.5,
         "metrics": {"duration_sec": 60, "threads": 8}, "log": "CPU 日志", "error": None},
    ]


def main():
    chromium = report_mod.find_chromium()
    if not chromium:
        print("    ⚠ 跳过: 没装 chromium, 排版结果渲不出来 "
              "(§11.7 要求必装 —— 产线机上应该有)")
        return 0

    results = mk_results()
    html = report_mod.render_html(mk_run(results), results, "排版试验.pdf")

    tmp = tempfile.mkdtemp(prefix="aging-paginate-")
    try:
        html_path = os.path.join(tmp, "r.html")
        pdf_path = os.path.join(tmp, "r.pdf")
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html)
        asyncio.run(report_mod._print_pdf(chromium, html_path, pdf_path))

        try:
            report_mod.verify_cjk(pdf_path)
        except Exception as e:
            check(False, "导出的 PDF 里有中文字形", e)
            return 1
        check(True, "导出的 PDF 里有中文字形 (verify_cjk 通过)")

        pages = page_texts(pdf_path)
        check(any(p.strip() for p in pages),
              "逐页取字能取出内容 (取字失败的话下面的断言全是空转)",
              [len(p) for p in pages])

        def page_of(marker):
            for i, t in enumerate(pages):
                if marker in t:
                    return i
            return -1

        # ⚠️ 找项名必须带**序号前缀**(「2. GPU 渲染」) —— 光找「GPU 渲染」
        # 会撞上「本次配置快照」那一行(那里也列了项名), 页码就指错了。
        from server.tests.registry import get_all_tests
        disp = {t["name"]: t["display_name"] for t in get_all_tests()}
        titles = {}
        for idx, r in enumerate(results, 1):
            titles[r["test_name"]] = "%d. %s" % (idx, disp.get(r["test_name"], "?"))

        def pages_of(marker):
            return [i for i, t in enumerate(pages) if marker in t]

        p_mem = page_of(titles["memory"])
        p_gpu = page_of(titles["gpu"])
        p_cpu = page_of(titles["cpu"])
        p_hdmi = page_of(titles["hdmi"])
        check(p_mem >= 0 and p_gpu >= 0,
              "前两项的名字都能在 PDF 里找到", (p_mem, p_gpu))

        # ① 长表项不许整块跳页
        #    判据: 长项的名字所在的页 **不晚于** 上一项的**最后一页** ——
        #    如果它被整块推到下一页, 名字就会晚于上一项结束的那页。
        #    (不能写成「同名页」: 上一项自己也可能跨页。)
        cpu_pages = pages_of(titles["cpu"])
        gpu_pages = pages_of(titles["gpu"])
        check(cpu_pages and gpu_pages and min(cpu_pages) <= max(gpu_pages),
              "长表项(CPU)接在上一项(GPU)后面, 没有整块跳到下一页",
              "GPU 到第%d页, CPU 从第%d页开始"
              % (max(gpu_pages) + 1, (min(cpu_pages) if cpu_pages else -1) + 1))

        # ② 续页必须重复项名 —— 长项跨页时, 每一页顶上都有它的项名
        check(len(cpu_pages) >= 2,
              "长表项(CPU)跨页时, 每一页顶上都有它的项名 (重复抬头)",
              "出现在第%s页" % [i + 1 for i in cpu_pages])

        # ③ 续页上列头也跟着重复
        for i in cpu_pages[1:]:
            check("指标" in pages[i],
                  "第%d页(续页)上重复了列头「指标」" % (i + 1))

        # ④ 没有指标的项, 标题仍然是独立块(不能因为改了排版就丢掉)
        check(p_hdmi >= 0, "没有指标的项(HDMI)标题也还在", p_hdmi)
        if p_hdmi >= 0:
            check("passed" in pages[p_hdmi], "HDMI 的状态色块文字也在")

        # ⑤ 后面的项没被前面的长表挤掉
        check(p_hdmi >= 0 and p_cpu >= 0 and p_hdmi >= p_cpu,
              "长表之后的项(HDMI)排在它后面, 没被挤没", (p_cpu, p_hdmi))

        # ── ⑥ GPU 的 rounds_detail: 原始输出不许再进指标表 ──
        # 起因: rounds_detail 以前是「整段 glmark2 输出」, 被 repr 成**一个
        # 巨大的单元格**(一行几千字符, 比一页还高) → 浏览器只能把这行推到
        # 下一页, 指标表跟前面的行**断开**; 而且它是**截断副本**(每轮只留
        # 末尾 20 行), 完整的那份本来就在日志附录里。
        # 改法: 指标里只留量化字段, 报告里渲染成**每轮一行的子表**。
        import re
        check("metric-sub" in html,
              "rounds_detail 渲染成子表(每轮一行), 不是一整块原始 dump")
        # 原始输出**不许**出现在指标表所在的页 —— 它只该在日志附录里
        raw_pages = [i + 1 for i, t in enumerate(pages) if "[scene-00]" in t]
        item_pages = [i + 1 for i in range(len(pages))
                      if titles["gpu"] in pages[i] or titles["cpu"] in pages[i]
                      or titles["memory"] in pages[i]]
        check(not (set(raw_pages) & set(item_pages)),
              "原始输出没混进指标表所在的页",
              "原始输出在第%s页, 指标表在第%s页" % (raw_pages, item_pages))
        check(raw_pages, "原始输出在日志附录里(还在, 一行不少)")
        # 子表所在的那一页必须带着项名 —— 不许落单
        sub_pages = [i for i, t in enumerate(pages) if "轮次" in t and "分数" in t]
        check(sub_pages and all(titles["gpu"] in pages[i] for i in sub_pages),
              "每轮明细子表所在的那一页带着 GPU 项的项名(没跟前面的断开)",
              "子表在第%s页" % [i + 1 for i in sub_pages])

        # ── ⑦ 失败原因不许落成一个「没有项名」的页 ──
        # 起因: 失败原因框原本在指标表**外面**。表刚好在页尾排完、框放不下
        # 时, 框被推到下一页, 而那页顶上既没项名也没状态 —— 读者认不出是
        # 哪一项失败。修法: 原因进表(最后一行), 跟着表头组一起重复。
        fdisp = {t["name"]: t["display_name"] for t in get_all_tests()}

        # ① 结构上: 原因必须在 <table class="case-metrics"> 里面
        fres = mk_failed_results()
        fhtml = report_mod.render_html(mk_run(fres), fres, "失败样本.pdf")
        blocks = case_tables(fhtml)
        check(blocks and any("case-fail" in b for b in blocks),
              "失败原因是指标表的一部分(在表里, 不在表外)",
              "找到 %d 张指标表, 里面有失败行: %s"
              % (len(blocks), [("case-fail" in b) for b in blocks]))

        # ② 渲染上: 印「失败原因」的那一页, 必须同时有该项的项名
        #    (nrows 是页边界旋钮 —— 见 mk_failed_results 的说明: 旧版在
        #     nrows=1 和 29 上会孤立, 所以这两档必须盯住)
        for nrows in (1, 29):
            fres = mk_failed_results(nrows)
            fhtml = report_mod.render_html(mk_run(fres), fres, "失败样本.pdf")
            fhtml_path = os.path.join(tmp, "fail.html")
            fpath = os.path.join(tmp, "fail.pdf")
            with open(fhtml_path, "w", encoding="utf-8") as f:
                f.write(fhtml)
            asyncio.run(report_mod._print_pdf(chromium, fhtml_path, fpath))
            fpages = page_texts(fpath)
            ftitles = {r["test_name"]: "%d. %s" % (i, fdisp.get(r["test_name"], "?"))
                       for i, r in enumerate(fres, 1)}
            bad = [i + 1 for i, t in enumerate(fpages)
                   if "失败原因" in t and ftitles["gpu"] not in t]
            check(not bad,
                  "nrows=%d: 印着「失败原因」的那一页, 顶上带着该项的项名(不孤立)" % nrows,
                  "孤立在第%s页" % bad)
            check(any("失败原因" in t for t in fpages),
                  "nrows=%d: 失败原因确实印出来了(别把上一条断言变成空转)" % nrows)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + ("全部通过" if not FAILS else "有失败项 (%d)" % len(FAILS)))
    for f in FAILS:
        print("  ✗ " + f)
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
