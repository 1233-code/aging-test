"""§8.2 报告导出 —— Jinja2 渲染 HTML + chromium headless 打印成 PDF

**为什么走浏览器而不是 reportlab** (2026-10-23 定):
chromium 已经在出货镜像里(§11.7 已从"任一浏览器"收紧成必装), 走它
**一个 Python 包都不用新增**; 而 reportlab 不认本机唯一的中文字体
`wqy-zenhei.ttc` —— 那是个 TTC 集合(ttcf magic, 3 个子字体), 得先抽成
单字体 TTF, 反而多一道依赖。

**但走浏览器有个必须防的坑** —— 实测本机 (aarch64 / Ubuntu 20.04):

    `fc-match sans-serif`         → DejaVu Sans
    `fc-match sans-serif:lang=zh` → DejaVu Sans      ← 加 :lang=zh 也救不了

DejaVu Sans **一个 CJK 字形都没有**(`fc-list ':charset=5de5'` 查不到)。
浏览器遇到缺字形**既不报错也不换字体**, 直接画成空白, **然后照样 rc=0
输出一份格式完全合法的 PDF** —— 产线会把一份全空白的中文报告当合格品归档。

所以本模块**每导出一份都会回头验**: `verify_cjk()` 解 PDF 的 ToUnicode CMap,
数里面有没有 CJK 码位。为 0 → **判定导出失败**。实测坏样本 CJK 码位 = 0、
好样本 = 11, 判据干净且**与字体名无关**(子集化后名字带 `BAAAAA+` 前缀,
认名字太脆)。

**失败一律抛 `ReportError`**, 由调用方 (runner) 捕获降级(§8.2: 结果已入库,
PDF 失败不阻断) —— 本模块不吞异常, 也不用"空字符串代表失败"那种
要靠约定才能懂的返回值。

**本模块不删任何文件。** 半成品 PDF 一律写到 `reports/pdf/.staging-*` 这种
点开头的临时名, 只有验过 CJK 之后才 `os.replace` 成正式名。宁可让一次失败
留下一个隐藏的 `.staging-*.pdf` 垃圾, 也不要出现"删掉了归档目录里某个
看起来像正式报告的 PDF"这种代码 —— 后者出了事没人查得清。
"""
import asyncio
import decimal
import math
import os
import re
import tempfile
import time
import zlib
from pathlib import Path
from typing import Optional

import jinja2

from .config import Config
from .tests._tools import find_tool
from . import boot_evidence as boot_evidence_mod
from . import clock

PROJECT_ROOT = Config.project_root()
TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"

#: chromium 的可执行名。各发行版/包叫法不一样, 挨个试。
_CHROMIUM_NAMES = ("chromium-browser", "chromium", "chromium-browser-chromium",
                   "google-chrome", "google-chrome-stable")

#: 单轮导出超时。chromium 冷启动约 1~2s, 给 90s 足够, 又不至于卡死收尾流程。
EXPORT_TIMEOUT_SEC = 90

#: 轮级状态 → **中文结论**。
#:
#: 为什么在 Python 侧而不是写在模板里: 同一张表要出现在**两个地方** ——
#: 报告 PDF 的「整体结论」那一行, 和 U 盘上 `INDEX.csv` 的「整体状态」那一列
#: (§8.2.3)。两处各写一份, 改了一处忘了另一处 ⇒ 纸上写的和索引里写的
#: 对不上, 工人对着看会以为有一份是错的。
#:
#: 光印一个英文状态词没用 —— 质检/返修的人未必知道 "skipped" 是什么意思,
#: 尤其 "skipped" 跟 "failed" 在他们眼里都像"没通过"。所以给一句中文结论,
#: 明确这轮能不能放行。
STATUS_CONCLUSION = {
    "passed": "✅ 已测项目全部通过",
    "failed": "❌ 存在失败/错误项 —— 不合格, 需返修",
    "error": "⚠ 本轮异常终止 —— 结果不作数, 需重跑",
    "skipped": "⏭ 本次未实际执行任何测试项 —— 不构成合格判定",
    "stopped": "🛑 用户中途停止 —— 已完成的项按自身判定, 未跑的不算数",
}


def status_conclusion(status: str) -> str:
    """轮级状态的中文结论(认不出来就把原状态词原样返回)。"""
    return STATUS_CONCLUSION.get(status, status or "")


class ReportError(RuntimeError):
    """导出失败。调用方负责降级(§8.2: 不阻断测试结果)。"""


# ==================== chromium ====================
def find_chromium() -> Optional[str]:
    """找 chromium 绝对路径, 找不到返回 None。"""
    for name in _CHROMIUM_NAMES:
        p = find_tool(name)
        if p:
            return p
    return None


# ==================== CJK 校验 (见模块 docstring) ====================
def _iter_streams(data: bytes):
    """把 PDF 里的 stream 逐个解压出来。"""
    for m in re.finditer(rb"stream\r?\n(.*?)\r?\nendstream", data, re.S):
        raw = m.group(1)
        try:
            yield zlib.decompress(raw)
        except zlib.error:
            yield raw          # 未压缩的(如字体程序本身), 原样给出去


def count_cjk_codepoints(pdf_path) -> int:
    """数 ToUnicode CMap 里映射到的 CJK 码位数。

    判据是**字体无关**的 —— 不认字体名, 只问「这份 PDF 里的字, 有没有落在
    CJK 基本区」。注意不能拿"ToUnicode 非空"当判据: 那份坏样本里也有几百个
    拉丁码位, 非空得很。
    """
    try:
        data = Path(pdf_path).read_bytes()
    except OSError:
        return 0
    found = set()
    for s in _iter_streams(data):
        if b"beginbfchar" not in s and b"beginbfrange" not in s:
            continue
        for blk in re.findall(rb"beginbfchar(.*?)endbfchar", s, re.S):
            for _src, dst in re.findall(
                    rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", blk):
                found.add(int(dst[:4], 16))
        for blk in re.findall(rb"beginbfrange(.*?)endbfrange", s, re.S):
            for lo, hi, dst in re.findall(
                    rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", blk):
                lo_i, hi_i, d = int(lo, 16), int(hi, 16), int(dst[:4], 16)
                # 单个 range 可能很大, 封顶 512 —— 报告用不到更大的区间,
                # 而真被撑爆的 range 会让这个函数在坏文件上跑很久。
                for i in range(min(hi_i - lo_i + 1, 512)):
                    found.add(d + i)
    return sum(1 for c in found if 0x4E00 <= c <= 0x9FFF)


def verify_cjk(pdf_path) -> None:
    """PDF 里没有 CJK 码位 → 抛 ReportError。

    失败的 PDF **不是废文件** —— 格式合法、chromium 也 rc=0, 只是中文全是空白。
    不校验就等于把废纸交给归档。
    """
    if count_cjk_codepoints(pdf_path) == 0:
        raise ReportError(
            "导出的 PDF 里没有任何中文字形 —— 报告会是全空白。"
            "本板 `sans-serif` 默认解析到 DejaVu Sans(零 CJK 字形), 且浏览器"
            "缺字形时不报错仍返回 rc=0。请确认装了含 CJK 字形的字体"
            "(apt: fonts-wqy-zenhei), 且模板 CSS 的 font-family 显式写了中文字体名。"
        )


# ==================== HTML 渲染 ====================
def _fmt_time(ts) -> str:
    if not ts:
        return "—"
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(ts)))
    except (TypeError, ValueError):
        return "—"


def fmt_time(ts) -> str:
    """时间戳 → 'YYYY-mm-dd HH:MM:SS'。

    公开出口 —— 报告之外的地方也要印同一个时间格式(比如 U 盘上那份
    `INDEX.csv` 的「起止时间」列), 各写各的迟早对不上。
    """
    return _fmt_time(ts)


def _round_half_up(x: float, nd: int = 1) -> float:
    """四舍五入到一半**往上进**(跟浏览器里的 toFixed() 一个规矩)。

    ⚠️ **不能用 f"{x:.1f}"** —— Python 那个是「四舍六入五成双」:
    58.25 → "58.2"; 而 JS 的 (58.25).toFixed(1) → "58.3"。
    报告印在纸上、读秒印在屏幕上, 工人正把这两个数对着看 ——
    算法不一样, 他会以为其中一个是错的。
    """
    try:
        d = decimal.Decimal(x)
    except (TypeError, ValueError, ArithmeticError):
        return float("nan")
    q = decimal.Decimal(1).scaleb(-nd)
    return float(d.quantize(q, rounding=decimal.ROUND_HALF_UP))


def _fmt_dur(sec) -> str:
    try:
        s = float(sec)
    except (TypeError, ValueError):
        return "—"
    # 负数 / 非有限值 → 「—」。前端 (static/app.js 的 fmtDur) 也是这么写的,
    # 两边得一致 —— 屏幕上一个 "—", 纸上写个 "-1.0s", 工人会以为工具坏了。
    if not math.isfinite(s) or s < 0:
        return "—"
    if s < 60:
        return f"{_round_half_up(s)}s"
    # ⚠️ 分钟/秒的拆法跟前端**一个字不差**: 前端是 Math.floor(s / 60) 和
    #   Math.floor(s % 60), 这里就写 math.floor(s / 60) 和 math.floor(s % 60)。
    #   (Python 的 s // 60 实际上也是一个样, 但写成跟前端一模一样的运算,
    #   以后谁改动都看得出「这两边得一起改」。)
    return f"{int(math.floor(s / 60))}m{int(math.floor(s % 60)):02d}s"


def _report_cfg() -> dict:
    # 用 `or {}`: config.yaml 里写 `report:`(下面全注释掉)时 yaml 解析出的是
    # **None** 而不是 {}。`Config.get("report", {})` 的默认值只在 key 不存在时
    # 才生效, 那种情况下它会返回 None, 后面 .get() 直接炸。
    return Config.get("report") or {}


# ── 指标值 → 报告能渲染的形态 ──
#
# 子表列名(只认这几个, 认不出的照原样印 —— 别把字段名猜错)
_SUB_COL_LABELS = {
    "round": "轮次", "score": "分数", "scenes": "场景数",
    "scenes_expected": "应跑场景", "elapsed_sec": "耗时(s)",
    "exit_code": "退出码", "status": "状态", "error": "说明",
}
# 一整列都是**这么长的字符串** → 它是原始输出, 不是量化结论 → 归日志附录
_RAW_TEXT_MIN_LEN = 80


def _metric_views(metrics: dict) -> list:
    """把指标字典变成报告能渲染的形态(每项: 键 / 值 / 可选的子表)。

    **为什么要有这一步**: 指标值里混着两种东西 ——
      ① **量化结论**(分数、场景数、耗时、退出码) —— 该进指标表;
      ② **原始输出**(一整段 glmark2 输出) —— 那是**日志**, 归日志附录。

    以前 ② 被 repr 成**一个巨大的单元格**(一行几千字符, 比一页还高): 浏览器
    只能把这行推到下一页, 于是指标表跟前面的行**断开**(§8.2.2); 而且它还是
    **截断副本**(每轮只留末尾 20 行)—— 完整的那份本来就在日志附录里。
    文档里早有规矩: 「报告里日志只有一处, 残缺副本比没有更糟」(§8.2)。

    **改法**: 值是「一串字典」时, 渲染成**每条一行的子表**; 整列都是长文本的
    (= 原始输出)那一列**不印** —— 读者要原始输出, 去日志附录, 那里一字不少。
    """
    views = []
    for k, v in (metrics or {}).items():
        view = {"key": k, "value": v, "sub_cols": None, "sub_rows": None}
        if isinstance(v, list) and v and all(isinstance(x, dict) for x in v):
            cols = []
            for d in v:
                for ck in d:
                    if ck not in cols:
                        cols.append(ck)
            raw = {c for c in cols
                   if all(isinstance(d.get(c), str) and len(d.get(c)) > _RAW_TEXT_MIN_LEN
                          for d in v)}
            # 整列都是 None 的 → 那一列**没有任何信息**, 印出来是空列
            empty = {c for c in cols if all(d.get(c) is None for d in v)}
            cols = [c for c in cols if c not in raw and c not in empty]
            if cols:
                view["sub_cols"] = [_SUB_COL_LABELS.get(c, c) for c in cols]
                view["sub_rows"] = [[d.get(c) for c in cols] for d in v]
                view["value"] = "(见下表)"
            else:
                # 整张表**每一列**都是原始输出/空值 → 什么都不值得印,
                # 只留一句指路(去日志附录)。别退回 repr —— 那正是把
                # 「几千字符一个单元格」请回来的路。
                view["value"] = "(原始输出见日志附录)"
        views.append(view)
    return views


def render_html(run: dict, results: list, filename: str = "",
                retest_runs: list = None, boot_evidence: str = "",
                cpu_evidence: str = "", clock_skew: str = None) -> str:
    """把一条 run + 它的逐项结果渲染成完整 HTML。

    `retest_runs` 是**重测轮**列表(§7.7), 由 `Database.get_retest_runs()` 给 ——
    里面的日志渲染成「重测日志附录」, 单独一节, 排在「日志附录」**后面**;
    前面的「日志附录」是首测原始日志, 一个字都不动。

    `boot_evidence` 是**上一轮的内核日志尾巴**(板子为什么死), 由
    `boot_evidence.read_evidence(run_id)` 给 —— 空串就不渲染那一节。
    **捞不到就不画**: 编一段「疑似原因」比没有更糟(会把排查方向带偏)。

    `cpu_evidence` 是**加压过程中的逐秒现场记录**(温度/负载/线程数), 由
    `boot_evidence.read_cpu_evidence(run_id)` 给 —— 空串就不渲染那一节。
    它跟 `boot_evidence` 是两回事: 前者是**内核视角**(板子死时内核说了什么),
    后者是**我们自己的视角**(加压到哪一秒、几线程、多少度、负载多少)。

    `clock_skew` 是 §9.3.4 的**时钟旁挂文件** —— 这一轮的时间戳是不是
    「推算值」(板子硬复位过, 系统时钟被退回了上次干净关机那一刻)。
    默认 None = 自己按 run_id 去读; 空串 = 明确表示「这一轮没有」(测试用)。
    """
    if clock_skew is None:
        clock_skew = clock.read_run_skew(run.get("id"))
    from .tests.registry import get_all_tests
    names = {t["name"]: t["display_name"] for t in get_all_tests()}

    summary = dict(run.get("summary") or {})
    for k in ("total", "passed", "failed", "error", "skipped", "not_run", "planned"):
        summary.setdefault(k, 0)
    # §7.7 合并重测: 这一轮被重测过几轮。报告要写明「这是合并后的最终结果」
    # —— 不写的话, 读者会拿这一版跟当初那版对不上, 以为结果凭空变了。
    summary["retest_count"] = run.get("retest_count") or 0
    # 老 run 的 summary_json 里没有 planned/not_run(那是这轮才加的两个口径)。
    # 直接印「共勾选 0」会跟旁边的「已测 6」打架, 所以用可推的口径补:
    # 勾选数 = 已测 + 未测 + 未跑到。对**新** run 这个等式本来就成立
    # (每个勾选项最终必落在 passed/failed/error/skipped/stopped 之一),
    # 所以这个回退不会把新 run 的数字算歪, 只会把老 run 的空缺补上。
    if not summary["planned"]:
        summary["planned"] = (summary["total"] + summary["skipped"]
                              + summary["not_run"])

    # 哪些项被重测过 —— 逐项段要给「这一项另有重测轮的日志」的指路,
    # 否则读者看完「日志附录」就以为只有首测那一份日志。
    retested_names = set()
    for mr in (retest_runs or []):
        for r in (mr.get("results") or []):
            if r.get("test_name"):
                retested_names.add(r.get("test_name"))

    cases = []
    for r in results:
        cases.append({
            "display_name": names.get(r.get("test_name"), r.get("test_name") or "?"),
            "name": r.get("test_name") or "",
            "status": r.get("status") or "pending",
            "duration": _fmt_dur(r.get("duration_sec")),
            "metrics": _metric_views(r.get("metrics") or {}),
            "error": r.get("error"),
            # §7.7「第 N 次测试通过」: 这一项试了几次(1 = 首测)。
            "attempt": r.get("attempt") or 1,
            # 这一项另有一轮重测的日志(在「重测日志附录」那一节里)。
            "retested": r.get("test_name") in retested_names,
        })
    # ── §9.3「板子死在哪一项」 ──
    #
    # 服务(板子)重启时, **正在跑的那一项**在 results 表里留下的是
    # `stopped` + 「服务重启, 本项没跑完」(db.finalize_interrupted_run
    # 写的)。从这一行就能知道板子死在哪一项 —— **只读**, 不改任何历史。
    #
    # 为什么值得单独点出来: 2026-10-30 新板连着两次死在**同一个地方**,
    # 但报告上从头到尾只写着「服务重启, 本项没跑完」 —— 读者要自己翻
    # 逐项明细才能对上是 CPU 那一项。**写在死因那一节旁边**, 才看得出
    # 「死在同一项上」。
    died_in_item = ""
    died_in_item_key = ""
    died_hard_reset = False
    for r in results:
        err = r.get("error") or ""
        if err.startswith(boot_evidence_mod.SOFT_ITEM_ERROR) or \
                err.startswith(boot_evidence_mod.HARD_RESET_ITEM_ERROR):
            died_in_item = names.get(r.get("test_name"),
                                     r.get("test_name") or "?")
            died_in_item_key = r.get("test_name") or ""
            died_hard_reset = err.startswith(
                boot_evidence_mod.HARD_RESET_ITEM_ERROR)
            break

    # ── 「板子死在哪一级加压」: 一句结论 ──
    #
    # 现场记录是**一屏原始数据**(200ms 一行) —— 读者得自己翻到最后一行、
    # 看线程数、看温度、再回头找「起跑 第 N/M 片」那几行才能拼出结论。
    # 这里直接给结论: 死在第几级、起手后几秒、当时多热/多少频率。
    #
    # ⚠️ **只在「板子确实死在 CPU 这一项上」时才给** —— 跑完了也有这个文件
    # (那时最后一行是「跑完了」而不是「死了」), 硬套一句「板子死在第 N 片」
    # 就是**编造**。
    #
    # ⚠️ **现场记录可能整个读不出来**(它是板子死的那一刻写的, 尾巴随时可能
    # 只写了一半; 2026-11-02 实测: 新板死在 2 线程那片, 那一节整个不见了)
    # ⇒ **兜底**: 从**上一次启动的日志**里读 `stress-ng --cpu N` 的命令行 ——
    # 那是 sudo 记的, 板子死后还在 ⇒ 至少能说出「死在几线程那一片」。
    cpu_death_summary = ""
    if died_in_item_key == "cpu" and died_hard_reset:
        cpu_death_summary = boot_evidence_mod.summarize_cpu_death(cpu_evidence)
        if not cpu_death_summary:
            cpu_death_summary = (
                boot_evidence_mod.summarize_cpu_death_from_journal(
                    boot_evidence or ""))

    # ── 「现场记录文件在, 却是空的」: **说清楚**, 别静悄悄地不画 ──
    #
    # ⚠️ 2026-11-02 13:50 实测踩过的坑: 板子上 `cpu_evidence_run3.txt` 是
    #    **0 字节**(文件建出来了, 一行数据都没落盘)⇒ 读出来是空串 ⇒ 报告
    #    **分不清**「压根没有这份记录」和「有, 但被硬复位带走了」⇒ 什么
    #    都不说 ⇒ 而这恰恰是最该有证据的一次。
    #
    # ⇒ 报告里**明写**这一节为什么是空的, 并指向「板子死在哪一级加压」
    #   那节(它能从上一次启动的日志里读出结论)。
    cpu_evidence_note = ""
    if died_in_item_key == "cpu" and died_hard_reset:
        state = boot_evidence_mod.cpu_evidence_state(run.get("id"))
        if state == boot_evidence_mod.CPU_EVIDENCE_EMPTY:
            cpu_evidence_note = (
                "「加压现场记录」那个文件**存在, 但一个字节都没有** —— "
                "板子被硬复位的时候, 还没写进存储的那几行**跟着内存一起没了**"
                "(这一版之前只把数据交给内核缓存, 没强制刷进存储)。"
                "⇒ **这一节没有现场数据**; "
            )
            if cpu_death_summary:
                cpu_evidence_note += (
                    "但板子死在第几片, 上一次启动的日志里还读得出来 "
                    "⇒ 见上面「板子死在哪一级加压」。"
                )
            else:
                cpu_evidence_note += (
                    "而板子死在第几片**这次也没读出来**(上一次启动的日志里连 "
                    "`stress-ng --cpu N` 都没有)⇒ 这一轮只能确定"
                    "「板子死在 CPU 加压这一项上」, 死在几线程**不知道**。"
                )

    # 「未测项」= skipped(用户主动跳/环境原因) + stopped(中途停止没跑到)。
    # §9.3 这两种都**没有实际执行**, 都不计入通过率, 都必须逐一列出让质检
    # 人工确认能不能放行 —— 只列 skipped 会漏掉"中途停止"那批, 纸面上
    # 看上去就像"这轮全测了", 而实际上有一半的项压根没跑。
    not_tested_cases = [c for c in cases if c["status"] in ("skipped", "stopped")]

    # ── 日志附录: **每一项的完整日志** (§8.2「全量测试日志」) ──
    #
    # 为什么是"完整"而不是"摘一段": 报告是**纸面归档**, 事后拿它去复盘 /
    # 申诉。摘一段的日志看着像日志, 读的人会以为那就是全部 —— 而缺了头的
    # 日志比没有日志更坏(没有日志他知道没记录, 缺了头的日志他不知道)。
    #
    # 所以这里只认两种情况: 有日志就**全给**; 一行都没有就把原因写清楚
    # ("本项被跳过, 未执行" / "本项没有产生任何日志") —— 绝不写"(无日志)"
    # 这种分不清"没记"还是"没跑"的含糊话。
    log_sections = []
    for c, r in zip(cases, results):
        log = (r.get("log") or "").strip()
        if log:
            body = log
        elif c["status"] == "skipped":
            body = "(本项被跳过, 未执行, 所以没有日志)"
        elif c["status"] == "stopped":
            body = "(本项未跑到 —— 中途停止, 未执行, 所以没有日志)"
        elif died_hard_reset and c.get("name") == died_in_item_key:
            # 板子在这一项上**硬复位** ⇒ 这一项的日志还**留在内存里**
            # (日志是这一项跑完才落库的) ⇒ 跟着进程一起没了。
            # 这不是「异常」, 是**板子把这一项打断了** —— 说清楚, 别让
            # 读报告的人以为工具出了 bug。
            body = ("(本项被板子硬复位打断 —— 这一项的日志还没来得及落库, "
                    "跟着进程一起没了。死因看上面「板子为什么死」那两节: "
                    "上一次启动的日志尾巴 + 加压现场记录。)")
        else:
            body = "(本项没有产生任何日志 —— 属异常: 跑过的项都该有日志)"
        m = r.get("metrics") or {}
        if m.get("log_truncated"):
            body += (f"\n⚠ 本项日志过长, 只保留了末尾 {m.get('log_kept_lines')} 行"
                     f"(前面 {m.get('log_dropped_lines')} 行已丢弃)")
        log_sections.append(f"===== {c['display_name']} [{c['status']}] =====\n{body}")
    full_log = "\n\n".join(log_sections) or "(本次运行无逐项日志)"

    # ── §7.7 重测日志: **单独一节**, 排在「日志附录」后面 ──
    #
    # 为什么要单独一节, 而不是把重测日志接在首测日志后面:
    #   · 父轮那一行的 log 是**首测的原始日志**, 是返修时最要看的证据
    #     (「当初为什么失败」) —— 报告里那一节**一个字都不许动**。
    #   · 重测的日志是**另一轮**的证据, 混进首测那一节里, 读者分不清
    #     哪段是当初的、哪段是后来的 —— 而这正是事后申诉最要紧的分界。
    # 所以: 前面的「日志附录」原样不动, 后面另起一节说明重测那一轮。
    retest_sections = []
    for mr in (retest_runs or []):
        for r in (mr.get("results") or []):
            log = (r.get("log") or "").strip()
            disp = names.get(r.get("test_name"), r.get("test_name") or "?")
            head = (f"===== {disp} [{r.get('status') or '?'}] "
                    f"· 第 {r.get('attempt') or 1} 次 =====")
            if log:
                body = log
            else:
                # 跟「日志附录」一个口径: 一行都没有就把原因写清楚, 不写
                # "(无日志)" 那种分不清"没记"还是"没跑"的含糊话。
                # 也不**跳过**这一项 —— 跳过了读者会以为这一项没被重测过。
                body = "(本项重测没有产生任何日志 —— 属异常: 跑过的项都该有日志)"
            retest_sections.append(f"{head}\n{body}")
    retest_log = "\n\n".join(retest_sections)

    cfg = run.get("config") or {}
    test_cfgs = (cfg.get("tests") if isinstance(cfg, dict) else None) or {}
    # 「本次配置快照」只列**本轮勾选了的**项目。
    #
    # 为什么: config["tests"] 装的是**整份配置**(勾选 + 没勾选都在里面), 直接
    # 整个列出来的话, 一轮只跑了 USB 的报告上会并排摆着 cpu / gpu / wifi /
    # emmc …… 十项配置 —— 纸面上读起来像"这轮测了十项", 而实际只测了一项。
    # 报告是给产线**归档**用的, 它得能回答"这一轮到底测了什么、用的什么参数",
    # 没勾的项目摆在那里只会让人误读。
    #
    # 判据: `enabled` 为真(= 本轮勾了)。**外加一条兜底**: 实际跑出结果的项
    # 也要列出来 —— 老库的 run 配置里可能压根没有 `enabled` 这个键(那时按
    # `_build_test_list` 的 `cfg.get("enabled", False)` 判定, 没键 = 没勾),
    # 只按 enabled 过滤会让老报告的配置快照整个空掉。
    ran_names = {r.get("test_name") for r in results if r.get("test_name")}
    config_items = []
    for tname, tv in test_cfgs.items():
        if not isinstance(tv, dict):
            continue
        if not (tv.get("enabled") or tname in ran_names):
            continue
        # `enabled=True` 不用再列 —— 出现在这张表里本身就是"勾了"的证据。
        shown = {k: v for k, v in tv.items() if k != "enabled"}
        config_items.append((names.get(tname, tname),
                             ", ".join(f"{k}={v}" for k, v in shown.items())))
    if not config_items:
        # ⚠️ 两种"空"要分清楚: 压根没记配置 vs 记了但**一项都没勾** ——
        # 后者是"没选任何测试", 不是"快照丢了", 写同一个占位符会误导人。
        config_items = [
            ("(本次未勾选任何测试项)", "") if test_cfgs
            else ("(未记录配置快照)", "")
        ]

    # ── 「勾了但连行都没有」的项, 也要进「未测项明细」 (§9.3) ──
    #
    # 为什么: 这类项在 results 表里**一行都没有** —— 服务(板子)重启前它
    # 还没开始, 连 `running` 那行都没写下来。db 那边算 not_run 时是
    # **算术补上去的**(`planned - 实际跑过的`), 所以汇总段会印「未跑到 1」;
    # 但明细表里**没有这一行**, 读者只看到一个数字, 不知道**是哪一项**没跑。
    #
    # 2026-10-29 实测(新板子那轮): 板子跑崩重启, 报告上「未跑到 1」,
    # 明细表却是**空的** —— CPU 压力压根没跑, 纸面上却查不到它。
    #
    # 补上之后: 明细表的行数 == 汇总段「未测 skipped + 未跑到 stopped」
    # 两列之和(跳过 / 没跑到的项逐个点名, 谁没跑一眼看到)。
    for tname, tv in test_cfgs.items():
        if not isinstance(tv, dict) or not tv.get("enabled"):
            continue
        if tname in ran_names:
            continue
        not_tested_cases.append({
            "display_name": names.get(tname, tname),
            "status": "stopped",
            "duration": _fmt_dur(None),
            "metrics": {},
            "error": "服务重启前这一项还没开始 —— 连记录都没留下",
            "attempt": 1,
            "retested": False,
        })

    started, finished = run.get("started_at"), run.get("finished_at")
    # ⚠️ **判空要用 `is not None`, 不能用「是不是真的」** —— 原来写的是
    #    `if started and finished`, 结果 started_at 正好是 0.0 的时候
    #    (0.0 是假值) 会被当成「没记录时间」, 总耗时整行变成「—」。
    try:
        total_dur = (float(finished) - float(started)
                     if started is not None and finished is not None else None)
    except (TypeError, ValueError):
        total_dur = None

    # ── §7.8.6 重测轮的时间: 原时间**保留**, 重测的那三项**接在后面** ──
    #
    # 口径 (2026-10-28, 工人原话:「如果有重测的情况, 可以在报告的开始时间
    # 结束时间和总耗时在**保留原先的数据的前提下**把重测的这三项时间填进去,
    # 在原时间后面」):
    #   · **父轮(首测)的时间一个字都不改** —— 那是「当初那一轮是什么时候
    #     跑的」这个**事实**, 改了就等于篡改记录。
    #   · 重测轮的「开始 / 结束 / 总耗时」**另起一行接在原值后面**, 每行
    #     带「重测第 N 轮」的标签 —— 读者一眼分得清哪个是首测、哪个是返修。
    #   · **不累加**。重测的时间不是"首测的补充", 是**另一轮**的独立时间;
    #     把它加到首测上, 那个数就既不是首测的、也不是重测的, 谁都对不上
    #     (跟 §7.8「每项之和 ≠ 总耗时」同一个道理)。
    retest_times = []
    for idx, mr in enumerate(retest_runs or [], 1):
        ms, mf = mr.get("started_at"), mr.get("finished_at")
        # 判空跟上面一样用 `is not None` —— 0.0 也是合法的起点。
        try:
            mdur = (float(mf) - float(ms)
                    if ms is not None and mf is not None else None)
        except (TypeError, ValueError):
            mdur = None
        retest_times.append({
            "round": idx,
            "started_at": _fmt_time(ms),
            "finished_at": _fmt_time(mf),
            "duration": _fmt_dur(mdur),
        })

    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=True,      # run 数据(测试名/错误/指标)会进 HTML, 必须转义
        trim_blocks=True,
        lstrip_blocks=True,
    )
    return env.get_template("report.html.j2").render(
        title=_report_cfg().get("title", "工厂老化测试报告"),
        run=run,
        summary=summary,
        # 轮级状态的中文结论 —— 唯一出处(报告 PDF 和 U 盘 INDEX.csv 共用)
        status_conclusion=STATUS_CONCLUSION,
        cases=cases,
        not_tested_cases=not_tested_cases,
        config_items=config_items,
        full_log=full_log,
        retest_log=retest_log,
        has_retest_log=bool(retest_log),
        boot_evidence=boot_evidence,
        has_boot_evidence=bool(boot_evidence),
        cpu_evidence=cpu_evidence,
        has_cpu_evidence=bool(cpu_evidence),
        cpu_evidence_note=cpu_evidence_note,
        has_cpu_evidence_note=bool(cpu_evidence_note),
        # §9.3.4 时钟推算值横幅 —— 纸面归档的人得知道这批时间戳不能当准点用
        clock_skew=clock_skew,
        has_clock_skew=bool(clock_skew),
        died_in_item=died_in_item,
        died_hard_reset=died_hard_reset,
        cpu_death_summary=cpu_death_summary,
        has_cpu_death_summary=bool(cpu_death_summary),
        started_at=_fmt_time(started),
        finished_at=_fmt_time(finished),
        total_duration=_fmt_dur(total_dur),
        # §7.8.6 重测轮的那三项时间 —— 模板里接在原值**后面**。
        retest_times=retest_times,
        generated_at=_fmt_time(clock.now()),
        filename=filename or "(未导出)",
    )


# ==================== PDF 导出 ====================
def output_dir() -> Path:
    d = _report_cfg().get("output_dir") or "reports"
    p = Path(d)
    if not p.is_absolute():
        # 相对路径挂在**数据根**下 —— 打包后代码归 root, 报告写到代码根里
        # 工人根本写不进去 (PDF 是产线天天要出的东西)。
        p = Config.data_root() / p
    return p / "pdf"


def resolve_pdf_path(stored: str) -> Optional[Path]:
    """把库里存的路径还原成一个**现在真的存在**的报告文件。

    ⚠️ **为什么要有这么个函数**: 库里存过三种路径, 都能碰上 ——
      · **相对数据根**(`reports/pdf/x.pdf`)—— 现在新写进去的;
      · **绝对路径**, 还在(挂载点没漂)—— 老记录;
      · **绝对路径**, **已经失效** —— 盘昨天挂在 `/media/User/AGING`,
        今天挂在 `/media/User/AGING1` ⇒ 库里那条 `/media/User/AGING/reports/
        pdf/x.pdf` 指向一个**不存在**的地方 ⇒ 工人点「打开 PDF 报告」打不开
        (2026-11-11 实况: Nov 10 15:14 那轮就是这样)。

    ⇒ 第三种**按文件名**在当前报告目录里找回同一个文件
      (文件名 = SN + 收尾时间戳, 盘上唯一) ⇒ **老记录不用改库也能打开**。

    找不到就返回 None —— 由调用方决定报 404 还是别的。
    """
    if not stored:
        return None
    p = Path(stored)
    if not p.is_absolute():
        # 相对路径一律相对**数据根** —— 报告就落在数据根下的 reports/pdf/。
        p = Config.data_root() / p
    if p.exists():
        return p
    # 绝对路径失效了(挂载点漂了) ⇒ 按文件名在当前报告目录里找同一个
    try:
        cand = output_dir() / p.name
    except Exception:
        return None
    if cand.exists():
        return cand
    return None


def _run_finished_ts(run: dict) -> float:
    """文件名时间戳优先用**结束**时间 —— 名字要对应报告真正成形的那一刻。"""
    try:
        return float(run.get("finished_at") or clock.now())
    except (TypeError, ValueError):
        return clock.now()


async def _print_pdf(chromium: str, html_path: Path, pdf_path: Path) -> None:
    """调 chromium headless 打印。失败抛 ReportError。"""
    cmd = [
        chromium,
        "--headless",
        "--disable-gpu",
        # 关掉 chromium 默认的页眉页脚(否则每页顶上会有日期/标题/文件 URL)
        "--no-pdf-header-footer",
        f"--print-to-pdf={pdf_path}",
        f"file://{html_path}",
    ]
    # 刻意**不加** --no-sandbox: 实测本板不带 sandbox 也能起。带上它等于关掉
    # chromium 的沙箱, 换一个我们并不需要的"兼容旧内核"的借口。
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (FileNotFoundError, PermissionError) as e:
        raise ReportError(f"起不来 chromium: {e}") from e

    try:
        _out, err = await asyncio.wait_for(
            proc.communicate(), timeout=EXPORT_TIMEOUT_SEC
        )
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()
        raise ReportError(
            f"chromium 打印超时({EXPORT_TIMEOUT_SEC}s), 已终止。"
            "X server / 字体缓存异常时会出现这种情况"
        )
    if proc.returncode != 0:
        raise ReportError(
            f"chromium 退出码 {proc.returncode}: "
            f"{err.decode(errors='replace').strip()[:300]}"
        )
    # ⚠️ 故意**不**在 rc==0 这里收工 —— 那正是今天实测到的失败形态:
    #   缺字形时 chromium 依然 rc=0 并输出合法 PDF。真判据在 verify_cjk()。


async def export_pdf(run: dict, results: list, retest_runs: list = None,
                    boot_evidence: str = None,
                    cpu_evidence: str = None,
                    clock_skew: str = None) -> str:
    """导出一次 run 的 PDF, 返回最终文件路径。

    `retest_runs`: 可选, §7.7 重测轮列表 —— 它的日志会另起一节印在
    「日志附录」后面。传 None(默认)表示"这一轮没有重测", 报告里就不出现
    那一节。

    `boot_evidence`: §9.3 板子重启的死因(上一次启动的内核日志尾巴)。
    **默认 None = 自己按 run_id 去读旁挂文件** —— 自动导出、手动重导、
    父轮合并重导三条路都拿得到同一份证据, 不会有一条路漏掉。
    传空串 = 明确表示"这一轮没有证据"(测试用)。
    **没有证据就不画那一节** —— 编一段「疑似原因」比没有更糟。

    `cpu_evidence`: 加压现场的逐秒记录(温度/负载/线程数) —— 同样默认
    自己按 run_id 去读旁挂文件。

    `clock_skew`: §9.3.4 时钟推算值旁挂文件 —— 同样默认自己按 run_id 去读。

    任何一步失败都抛 `ReportError` —— **调用方负责降级**(§8.2: 结果已入库,
    PDF 失败不阻断, `pdf_path` 留空 + warning + UI 提示可手动重导)。
    """
    if boot_evidence is None:
        boot_evidence = boot_evidence_mod.read_evidence(run.get("id"))
    if cpu_evidence is None:
        cpu_evidence = boot_evidence_mod.read_cpu_evidence(run.get("id"))
    if clock_skew is None:
        clock_skew = clock.read_run_skew(run.get("id"))
    chromium = find_chromium()
    if not chromium:
        raise ReportError(
            "未找到 chromium(试过: " + " / ".join(_CHROMIUM_NAMES) + ")。"
            "PDF 导出依赖它, 见 §11.7 打包必装清单"
        )

    out_dir = output_dir()
    out_dir.mkdir(parents=True, exist_ok=True)

    sn = (run.get("sn") or "unknown").strip() or "unknown"
    filename = "{}_{}.pdf".format(
        sn, time.strftime("%Y%m%d_%H%M%S", time.localtime(_run_finished_ts(run)))
    )
    pdf_path = out_dir / filename

    # 临时名带点开头 + .pdf 后缀(chromium 靠后缀认格式): 半成品藏起来, 不会
    # 被归档脚本当成正式报告捡走。验过 CJK 才 replace 成正式名。
    staging = out_dir / f".staging-{os.getpid()}-{int(clock.now())}.pdf"
    # HTML 写到系统临时目录, 用 run_id 命名 —— 同一个 run 重复导出会覆盖同一个
    # 文件, 不会攒垃圾。覆盖写不是删除, 不涉及清理。
    html_file = Path(tempfile.gettempdir()) / f"aging-report-{run.get('id', 'x')}.html"
    html_file.write_text(render_html(run, results, filename,
                                     retest_runs=retest_runs,
                                     boot_evidence=boot_evidence,
                                     cpu_evidence=cpu_evidence,
                                     clock_skew=clock_skew),
                         encoding="utf-8")

    await _print_pdf(chromium, html_file, staging)

    if not staging.exists() or staging.stat().st_size == 0:
        raise ReportError(
            f"chromium 报成功但没有产出文件: {staging} —— "
            "别信它的退出码, 以文件为准"
        )
    verify_cjk(staging)      # ← 关键: 拦住「合法但全空白」的 PDF
    os.replace(staging, pdf_path)
    return str(pdf_path)