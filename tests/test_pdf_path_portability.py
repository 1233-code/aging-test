"""回归测试: 报告路径**跟着挂载点漂**这件事, 得在代码里就根治(2026-11-11)。

起因(产线实况): 盘昨天挂在 `/media/User/AGING`, 今天挂在 `/media/User/AGING1`
⇒ 库里存的那条**绝对路径** `/media/User/AGING/reports/pdf/x.pdf` 指向一个
**不存在**的地方 ⇒ 工人点「打开 PDF 报告」打不开。

    实况那一轮: Nov 10 15:14 跑的 A10000000000010, 文件本身**好好地**在盘上
    (`reports/pdf/A10000000000010_20261110_151437.pdf`), 只是**路径**过期了。

⚠️ **为什么不能只靠「改库」**: 挂载点还会再漂 ⇒ 每漂一次就得改一次库 ⇒
    根治办法是**库里存相对数据根的路径**(挂到哪儿都对)。

这个测试守三件事:
  ① 存进库的路径是**相对数据根**的(不是绝对);
  ② **已经失效**的绝对路径也能打开 —— 按文件名在当前报告目录里找回同一个;
  ③ 越权的路径仍然被拒(不能变成任意文件下载口)。

全程用**临时目录** —— 不碰 reports/history.db、reports/pdf/。
跑法: .venv/bin/python -m tests.test_pdf_path_portability
"""
import asyncio
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import db as DB
from server import report as R

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


def section(title):
    print("\n" + title)


async def main():
    tmp = Path(tempfile.mkdtemp(prefix="aging-pdfpath-"))
    try:
        # ── 准备: 一个临时的「数据根」, 报告目录 reports/pdf/ ─────────
        data_root = tmp / "AGING1"          # 假装盘挂在这儿
        pdf_dir = data_root / "reports" / "pdf"
        pdf_dir.mkdir(parents=True)

        # 数据根指到临时目录 ⇒ 归一化和还原都围着它转
        old_env = os.environ.get("AGING_DATA_DIR")
        os.environ["AGING_DATA_DIR"] = str(data_root)
        # Config 的数据根是**导入时**算的 ⇒ 得让它重新算
        DB.DB_PATH = data_root / "reports" / "history.db"

        section("① 存进库的路径是「相对数据根」的")
        pdf = pdf_dir / "SN-TEST-0001_20261111_101010.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake")
        stored = DB._pdf_path_for_db(str(pdf))
        check(stored == os.path.join("reports", "pdf", pdf.name),
              "绝对路径被换成相对数据根的路径", stored)
        check(not os.path.isabs(stored), "存的不是绝对路径", stored)

        # 不在数据根下的 ⇒ 只能存绝对(相对了还原不回去)
        outside = Path("/var/somewhere/else/x.pdf")
        out_stored = DB._pdf_path_for_db(str(outside))
        check(out_stored == str(outside),
              "不在数据根下的路径保持绝对(相对了还原不回去)", out_stored)

        # 本来就是相对的 ⇒ 照旧
        check(DB._pdf_path_for_db("reports/pdf/y.pdf") == "reports/pdf/y.pdf",
              "本来就是相对的原样保留")

        section("② 已失效的绝对路径也能打开(挂载点漂了)")
        # 盘「昨天」挂在别的地方 ⇒ 库里存的是那个旧路径
        old_mount = tmp / "AGING"           # 昨天的挂载点(现在不存在了)
        stale = str(old_mount / "reports" / "pdf" / pdf.name)
        check(not os.path.exists(stale), "旧挂载点下的路径确实不存在了", stale)

        got = R.resolve_pdf_path(stale)
        check(got is not None and os.path.exists(str(got)),
              "按文件名在当前报告目录里找回了同一个文件", got)
        check(got is not None and Path(got).name == pdf.name,
              "找回来的就是那个文件", got)

        # 相对路径也能还原
        got2 = R.resolve_pdf_path(stored)
        check(got2 is not None and os.path.exists(str(got2)),
              "相对路径能还原成真文件", got2)

        # 文件真没了 ⇒ None(由端点报 404)
        missing = R.resolve_pdf_path(
            str(old_mount / "reports" / "pdf" / "根本没有这个.pdf"))
        check(missing is None, "文件真的没了 ⇒ 返回 None(端点报 404)", missing)
        check(R.resolve_pdf_path("") is None, "空路径 ⇒ None")
        check(R.resolve_pdf_path(None) is None, "None ⇒ None")

        section("③ 越权的路径仍然被拒(不能变成任意文件下载口)")
        # 造一个**真的存在**、但在报告目录外面的文件
        secret = tmp / "secret.txt"
        secret.write_text("不该被读走")
        got3 = R.resolve_pdf_path(str(secret))
        check(got3 is not None and str(got3) == str(secret),
              "还原器如实还原了它(是否放行由端点的边界检查管)", got3)
        root = str(R.output_dir().resolve())
        try:
            inside = os.path.commonpath([str(Path(got3).resolve()), root]) == root
        except ValueError:
            inside = False
        check(not inside, "但它在报告目录外 ⇒ 端点会 403 拒掉", root)

        section("④ 端点那一层(工人点「打开 PDF 报告」的那一下)")
        from fastapi import HTTPException
        from server import main as M

        async def fake_get_run(run_id):
            return {"pdf_path": FAKE["pdf_path"]}

        FAKE = {"pdf_path": None}
        orig_get_run = M.db.Database.get_run
        M.db.Database.get_run = staticmethod(fake_get_run)
        try:
            # 4a 路径失效 ⇒ 但盘上有同名文件 ⇒ **要能打开**(修好那条)
            FAKE["pdf_path"] = stale
            resp = await M.history_pdf(1)
            check(resp is not None and getattr(resp, "filename", "") == pdf.name,
                  "失效路径也能打开(端点返回了那个文件)", resp)

            # 4b 相对路径 ⇒ 能打开
            FAKE["pdf_path"] = stored
            resp = await M.history_pdf(1)
            check(resp is not None, "相对路径也能打开(端点)", resp)

            # 4c 文件真没了 ⇒ 404
            FAKE["pdf_path"] = str(old_mount / "reports" / "pdf" / "没了.pdf")
            try:
                await M.history_pdf(1)
                check(False, "文件真没了 ⇒ 端点该报 404")
            except HTTPException as e:
                check(e.status_code == 404, "文件真没了 ⇒ 端点报 404", e.status_code)

            # 4d 越权路径 ⇒ 403
            FAKE["pdf_path"] = str(secret)
            try:
                await M.history_pdf(1)
                check(False, "越权路径 ⇒ 端点该报 403")
            except HTTPException as e:
                check(e.status_code == 403, "越权路径 ⇒ 端点报 403", e.status_code)

            # 4e 还没导出过 ⇒ 404
            FAKE["pdf_path"] = None
            try:
                await M.history_pdf(1)
                check(False, "没导出过 ⇒ 端点该报 404")
            except HTTPException as e:
                check(e.status_code == 404, "没导出过 ⇒ 端点报 404", e.status_code)
        finally:
            M.db.Database.get_run = orig_get_run

        if old_env is not None:
            os.environ["AGING_DATA_DIR"] = old_env
        else:
            os.environ.pop("AGING_DATA_DIR", None)

        print("\n" + "─" * 60)
        if FAILS:
            print("✗ %d 项失败 / 共 %d 项" % (len(FAILS), PASSES + len(FAILS)))
            for f in FAILS:
                print("   · " + f)
            return 1
        print("✓ 全部通过 (%d 项)" % PASSES)
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
