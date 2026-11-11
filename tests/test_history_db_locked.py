"""回归测试: 体检要分清「**被占着**」和「**坏了**」(2026-11-11)。

起因:
    `server/db.py` 的启动体检(`probe_health`)把**任何**异常都当成
    「库坏了」⇒ 立刻走 `_isolate_if_corrupt` ⇒ **把库改名隔离**、另起
    一个空库。

    而 `database is locked` **不是损坏** —— 它只说明「此刻有别的进程
    开着这个库」。真实场景:
      · 上一个服务进程还没退干净, 服务重启时新进程先体检 ⇒ locked;
      · 另一个工具(比如查库用的脚本)正开着它;
      · 体检正好撞上别人在写。
    ⇒ 老代码会把**好端端的库**改名隔离掉 ⇒ **工人这根盘的历史记录
    整个归零**(旧库还在盘上, 但应用读不到了)⇒ 而板子一点毛病都没有
    ⇒ **冤枉好板子**。

这个测试守三件事:
  ① 被占着的库 ⇒ 体检结论**不是「坏」**(结论带 `locked:` 标记);
  ② 被占着的库 ⇒ **不许**被隔离(盘上不许冒出来 `history.db.corrupt-*`,
     而且原库一个字节都不许动);
  ③ **真坏的库照旧隔离** —— 别把护栏拆得太宽(locked 的口子不能变成
     「什么都不断言」的借口)。

全程用**临时目录**里的真 SQLite 文件 —— 不碰 reports/history.db、
不碰 reports/pdf/。
跑法: .venv/bin/python -m tests.test_history_db_locked
"""
import asyncio
import contextlib
import io
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import db as DB                    # noqa: E402
from tests._tmp import mkdtemp                 # noqa: E402

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


def make_db(path, n_results=200):
    """造一个跟生产同形状的库(两张表 + 索引)。"""
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA page_size=4096")
    conn.execute("CREATE TABLE runs (id INTEGER PRIMARY KEY AUTOINCREMENT,"
                 " started_at REAL, finished_at REAL, status TEXT,"
                 " config_json TEXT, summary_json TEXT, mini_of INTEGER,"
                 " error TEXT, sn TEXT, operator TEXT, batch TEXT,"
                 " pdf_path TEXT, updated_at REAL, retest_count INTEGER)")
    conn.execute("CREATE TABLE results (id INTEGER PRIMARY KEY AUTOINCREMENT,"
                 " run_id INTEGER, test_name TEXT, status TEXT,"
                 " started_at REAL, finished_at REAL, duration_sec REAL,"
                 " metrics_json TEXT, log TEXT, error TEXT, attempt INTEGER)")
    conn.execute("CREATE INDEX idx_results_run ON results(run_id)")
    conn.execute("CREATE INDEX idx_runs_started ON results(run_id)")
    conn.execute("INSERT INTO runs (started_at, status) VALUES (1.0, 'passed')")
    for i in range(n_results):
        conn.execute("INSERT INTO results (run_id, test_name, status, log)"
                     " VALUES (1, 'cpu', 'passed', ?)", ("x" * 120,))
    conn.commit()
    conn.close()


def corrupt_data_pages(path, first_page=5, n_pages=3):
    """把**数据页**写坏(header 留着 —— 模拟「拔盘时写到一半」)。"""
    conn = sqlite3.connect(path)
    page_size = conn.execute("PRAGMA page_size").fetchone()[0]
    conn.close()
    with open(path, "r+b") as f:
        for p in range(first_page, first_page + n_pages):
            f.seek(p * page_size)
            f.write(b"\xa5" * 512)


def digest(path):
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


async def main():
    tmp = Path(mkdtemp(prefix="aging-locked-"))
    DB.QUARANTINED.clear()

    # ─────────────────────────────────────────────────────────
    section("① 被占着的库 ⇒ 体检结论**不是「坏」**")
    # ─────────────────────────────────────────────────────────
    lk_dir = tmp / "locked"
    lk_dir.mkdir()
    lk = lk_dir / "history.db"
    make_db(lk)

    # 用**另一个连接**持有排他锁 ⇒ 体检会撞上 `database is locked`
    holder = sqlite3.connect(lk)
    holder.execute("BEGIN EXCLUSIVE")
    holder.execute("INSERT INTO runs (started_at, status) VALUES (2.0, 'x')")

    ok, why = await DB.probe_health(lk)
    check(ok is True, "被占着的库**不判为坏**", (ok, why))
    check(DB.is_locked_reason(why), "结论里带 `locked:` 标记(能分辨出来)", why)
    check("locked" in (why or "").lower(), "结论里说清楚了是 locked", why)

    # ─────────────────────────────────────────────────────────
    section("② 被占着的库 ⇒ **不许**被隔离")
    # ─────────────────────────────────────────────────────────
    before = digest(lk)
    DB.QUARANTINED.clear()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        await DB._isolate_if_corrupt(lk)
    log_text = buf.getvalue()
    check(DB.find_quarantined(lk) == [],
          "盘上没有冒出隔离件 history.db.corrupt-*", DB.find_quarantined(lk))
    check(len(DB.QUARANTINED) == 0, "隔离记录是空的", DB.QUARANTINED)
    check(digest(lk) == before, "原库一个字节都没动", "digest 变了")
    check("占着" in log_text,
          "日志里说清楚了「是占着、不是坏了」(留个痕, 免得下次又猜)",
          log_text[-200:])

    holder.rollback()
    holder.close()

    # ─────────────────────────────────────────────────────────
    section("③ **真坏的库照旧隔离**(别把护栏拆太宽)")
    # ─────────────────────────────────────────────────────────
    bad_dir = tmp / "bad"
    bad_dir.mkdir()
    bad = bad_dir / "history.db"
    make_db(bad)
    corrupt_data_pages(bad)
    DB.QUARANTINED.clear()
    ok2, why2 = await DB.probe_health(bad)
    check(ok2 is False, "坏库仍然判为坏", (ok2, why2))
    check(not DB.is_locked_reason(why2),
          "坏库的结论**不是** locked(两个结论分得开)", why2)
    await DB._isolate_if_corrupt(bad)
    check(len(DB.find_quarantined(bad)) == 1,
          "坏库还是被改名隔离了", DB.find_quarantined(bad))
    check(len(DB.QUARANTINED) == 1, "隔离记录有它", DB.QUARANTINED)

    # ─────────────────────────────────────────────────────────
    section("④ 好库(没人占)⇒ 照样「好」, 不隔离")
    # ─────────────────────────────────────────────────────────
    good_dir = tmp / "good"
    good_dir.mkdir()
    good = good_dir / "history.db"
    make_db(good)
    DB.QUARANTINED.clear()
    ok3, why3 = await DB.probe_health(good)
    check(ok3 is True, "好库判为好", (ok3, why3))
    check(not DB.is_locked_reason(why3), "好库的结论不是 locked", why3)
    await DB._isolate_if_corrupt(good)
    check(DB.find_quarantined(good) == [], "好库没有隔离件")

    print("\n" + "─" * 60)
    if FAILS:
        print("✗ %d 项失败 / 共 %d 项" % (len(FAILS), PASSES + len(FAILS)))
        for f in FAILS:
            print("   · " + f)
        return 1
    print("✓ 全部通过 (%d 项)" % PASSES)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
