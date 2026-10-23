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


def _stderr_tail(log: str, lines: int = 15) -> str:
    """从该项日志里挑出末尾的报错行(§8.2: failed/error 附 stderr 末 N 行)。

    挑「含典型报错关键词的行 + 兜底取末尾若干行」, 而不是把整段日志倒序 ——
    倒序拿到的往往是无关的收尾输出, 报错本身埋在中段, 而那正是要给人看的。
    """
    if not log:
        return ""
    all_lines = [ln for ln in log.splitlines() if ln.strip()]
    if not all_lines:
        return ""
    kw = re.compile(r"error|fail|denied|timeout|异常|失败|错误|拒绝", re.I)
    picked = [ln for ln in all_lines if kw.search(ln)][-lines:]
    if not picked:
        picked = all_lines[-lines:]
    return "\n".join(picked)


def _report_cfg() -> dict:
    # 用 `or {}`: config.yaml 里写 `report:`(下面全注释掉)时 yaml 解析出的是
    # **None** 而不是 {}。`Config.get("report", {})` 的默认值只在 key 不存在时
    # 才生效, 那种情况下它会返回 None, 后面 .get() 直接炸。
    return Config.get("report") or {}


def render_html(run: dict, results: list, filename: str = "") -> str:
    """把一条 run + 它的逐项结果渲染成完整 HTML。"""
    from .tests.registry import get_all_tests
    names = {t["name"]: t["display_name"] for t in get_all_tests()}

    summary = dict(run.get("summary") or {})
    for k in ("total", "passed", "failed", "error", "skipped"):
        summary.setdefault(k, 0)

    cases = []
    for r in results:
        cases.append({
            "display_name": names.get(r.get("test_name"), r.get("test_name") or "?"),
            "status": r.get("status") or "pending",
            "duration": _fmt_dur(r.get("duration_sec")),
            "metrics": list((r.get("metrics") or {}).items()),
            "error": r.get("error"),
            "stderr_tail": _stderr_tail(r.get("log") or ""),
        })
    skipped_cases = [c for c in cases if c["status"] == "skipped"]

    full_log = "\n".join(
        f"===== {c['display_name']} [{c['status']}] =====\n" + (r.get("log") or "(无日志)")
        for c, r in zip(cases, results)
    ) or "(本次运行无逐项日志)"

    cfg = run.get("config") or {}
    test_cfgs = (cfg.get("tests") if isinstance(cfg, dict) else None) or {}
    config_items = []
    for tname, tv in test_cfgs.items():
        if isinstance(tv, dict):
            config_items.append((names.get(tname, tname),
                                 ", ".join(f"{k}={v}" for k, v in tv.items())))
    if not config_items:
        config_items = [("(未记录配置快照)", "")]

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
        skipped_cases=skipped_cases,
        config_items=config_items,
        full_log=full_log,
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


async def export_pdf(run: dict, results: list) -> str:
    """导出一次 run 的 PDF, 返回最终文件路径。

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
    html_file.write_text(render_html(run, results, filename), encoding="utf-8")

    await _print_pdf(chromium, html_file, staging)

    if not staging.exists() or staging.stat().st_size == 0:
        raise ReportError(
            f"chromium 报成功但没有产出文件: {staging} —— "
            "别信它的退出码, 以文件为准"
        )
    verify_cjk(staging)      # ← 关键: 拦住「合法但全空白」的 PDF
    os.replace(staging, pdf_path)
    return str(pdf_path)