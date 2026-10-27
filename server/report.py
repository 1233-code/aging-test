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

PROJECT_ROOT = Config.project_root()
TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"

#: chromium 的可执行名。各发行版/包叫法不一样, 挨个试。
_CHROMIUM_NAMES = ("chromium-browser", "chromium", "chromium-browser-chromium",
                   "google-chrome", "google-chrome-stable")

#: 单轮导出超时。chromium 冷启动约 1~2s, 给 90s 足够, 又不至于卡死收尾流程。
EXPORT_TIMEOUT_SEC = 90


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


def _fmt_dur(sec) -> str:
    try:
        s = float(sec)
    except (TypeError, ValueError):
        return "—"
    return f"{s:.1f}s" if s < 60 else f"{int(s // 60)}m{int(s % 60):02d}s"


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
                retest_runs: list = None) -> str:
    """把一条 run + 它的逐项结果渲染成完整 HTML。

    `retest_runs` 是**重测轮**列表(§7.7), 由 `Database.get_retest_runs()` 给 ——
    里面的日志渲染成「重测日志附录」, 单独一节, 排在「日志附录」**后面**;
    前面的「日志附录」是首测原始日志, 一个字都不动。
    """
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
            "status": r.get("status") or "pending",
            "duration": _fmt_dur(r.get("duration_sec")),
            "metrics": _metric_views(r.get("metrics") or {}),
            "error": r.get("error"),
            # §7.7「第 N 次测试通过」: 这一项试了几次(1 = 首测)。
            "attempt": r.get("attempt") or 1,
            # 这一项另有一轮重测的日志(在「重测日志附录」那一节里)。
            "retested": r.get("test_name") in retested_names,
        })
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

    started, finished = run.get("started_at"), run.get("finished_at")
    total_dur = (float(finished) - float(started)
                 if started and finished else None)

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
        cases=cases,
        not_tested_cases=not_tested_cases,
        config_items=config_items,
        full_log=full_log,
        retest_log=retest_log,
        has_retest_log=bool(retest_log),
        started_at=_fmt_time(started),
        finished_at=_fmt_time(finished),
        total_duration=_fmt_dur(total_dur),
        generated_at=_fmt_time(time.time()),
        filename=filename or "(未导出)",
    )


# ==================== PDF 导出 ====================
def output_dir() -> Path:
    d = _report_cfg().get("output_dir") or "reports"
    p = Path(d)
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    return p / "pdf"


def _run_finished_ts(run: dict) -> float:
    """文件名时间戳优先用**结束**时间 —— 名字要对应报告真正成形的那一刻。"""
    try:
        return float(run.get("finished_at") or time.time())
    except (TypeError, ValueError):
        return time.time()


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


async def export_pdf(run: dict, results: list, retest_runs: list = None) -> str:
    """导出一次 run 的 PDF, 返回最终文件路径。

    `retest_runs`: 可选, §7.7 重测轮列表 —— 它的日志会另起一节印在
    「日志附录」后面。传 None(默认)表示"这一轮没有重测", 报告里就不出现
    那一节。

    任何一步失败都抛 `ReportError` —— **调用方负责降级**(§8.2: 结果已入库,
    PDF 失败不阻断, `pdf_path` 留空 + warning + UI 提示可手动重导)。
    """
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
    staging = out_dir / f".staging-{os.getpid()}-{int(time.time())}.pdf"
    # HTML 写到系统临时目录, 用 run_id 命名 —— 同一个 run 重复导出会覆盖同一个
    # 文件, 不会攒垃圾。覆盖写不是删除, 不涉及清理。
    html_file = Path(tempfile.gettempdir()) / f"aging-report-{run.get('id', 'x')}.html"
    html_file.write_text(render_html(run, results, filename,
                                     retest_runs=retest_runs),
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