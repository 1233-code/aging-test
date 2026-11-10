"""回归测试: 历史库坏掉时, 应用**改名隔离**旧库并重建(2026-11-10)。

起因(2026-11-10 产线盘实况): 盘上的 `reports/history.db` 报
`database disk image is malformed`。用 `scripts/check_history_db.py` 查出来
的真相比「库坏了」具体得多, 也阴险得多:

    · 表 `runs`(43 行)、`results`(230 行)—— **都读得动**;
    · 三个索引 —— **都读得动**;
    · `sqlite_sequence` —— **读得动**;
    · 应用平时做的三种写(改一行 / 插一轮 / 记一项结果)—— **全都成功**;
    · 只有**读某几行的大字段**会炸(`results.log` 有行读不出来)⇒ 那是
      **大字段的溢出页在写到一半时盘被拔了**, 链断了一截;
    · `quick_check` / `integrity_check` 直接抛异常。

⇒ **症状与病根对不上**: 服务起得来、历史页能看、新测试也记得进去,
只有「恰好读到那几行旧日志」时才炸 —— 启动时「收尾残留轮次」就是被
它炸掉的(那一轮永远停在「运行中」, 历史列表一直显示有测试在跑)。

这个测试守五件事:
  ① 坏库 → **改名隔离**(旧库还在、内容一个字没改) + 另建一个新库;
  ② 隔离之后**应用照常能用**(能插轮、能记结果)—— 这才是修复的意义;
  ③ 好库 → **不许**被隔离(隔离是最后手段, 不是常规操作);
  ④ **热日志能回滚的库不许被隔离** —— 上次写到一半被拔盘, SQLite 自己
     回滚一下就没事了; 只读打开回滚不了, 会误判成「坏」⇒ 体检必须
     用**读写**方式打开;
  ⑤ 隔离这件事得**留痕**: 日志里写清楚 + `/api/config` 里查得到(机器可读)。
     ⚠️ **但界面上一个字都不许提**(2026-11-10 用户定)—— 挂横幅会让人误会
     「这根盘坏了不能用」, 而盘其实是好的: 服务正常、测试照做, 只是**之前
     那批历史记录**读不出来了(旧文件还在 reports/ 里没删)。
     ⇒ 界面那侧由 `tests/test_frontend_history_db_silent.js` 盯着。

全程用**临时目录**里的真 SQLite 文件 —— 不碰 reports/history.db、
不碰 reports/pdf/。
跑法: .venv/bin/python -m tests.test_history_db_quarantine
"""
import asyncio
import contextlib
import io
import os
import shutil
import sqlite3
import struct
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import db as DB
from tests._tmp import mkdtemp

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


def make_db(path, n_results=500):
    """造一个跟生产同形状的库(两张表 + 三个索引 + AUTOINCREMENT)。"""
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
    conn.execute("CREATE INDEX idx_runs_started ON runs(started_at DESC)")
    conn.execute("CREATE INDEX idx_runs_mini_of ON runs(mini_of)")
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


def fabricate_hot_journal(db_path):
    """手造一个**真热**的日志(等价于「提交写到一半掉电」)。

    ⚠️ **为什么不用「起个事务然后强杀」**: 那样造出来的日志**不热** ——
       事务没提交 ⇒ 数据还在页缓存里、DB 文件一个字没改 ⇒ 改动计数器没变
       ⇒ SQLite 判定「这日志不是热的」直接忽略 ⇒ 只读打开照样成功。
       (这个坑踩过一轮才想明白。)

    「热」的真条件(SQLite 文件格式 §Hot Journal): 日志头合法 **且**
    **DB 文件的改动计数器跟日志里第 1 页的副本对不上** ⇒ 说明「提交写了一半」。

    做法: ① 按日志格式写一个只含第 1 页的日志(头 28 字节 + 页数据 + 校验和)
          ② 把 DB 文件里的改动计数器 **+1** ⇒ 两边对不上 ⇒ 日志是热的。
    """
    with open(db_path, "r+b") as f:
        hdr = f.read(100)
        page_size = struct.unpack(">H", hdr[16:18])[0] or 65536
        n_page = struct.unpack(">I", hdr[28:32])[0]
        sector = struct.unpack(">I", hdr[20:24])[0]
        f.seek(0)
        page1 = f.read(page_size)
        nonce = 0x12345678

        def pager_cksum(data):
            """SQLite 自己的页校验和算法(pager_cksum)。"""
            c = nonce
            i = len(data) - 200
            while i > 0:
                c = (c + data[i]) & 0xffffffff
                i -= 200
            for i in range(0, 200):
                c = (c + data[i]) & 0xffffffff
            return c

        jhdr = (b"\xd9\xd5\x05\xf9\x20\xa1\x63\xd7" + struct.pack(">I", 1)
                + struct.pack(">I", nonce) + struct.pack(">I", n_page)
                + struct.pack(">I", sector) + struct.pack(">I", page_size))
        rec = (struct.pack(">I", 1) + page1
               + struct.pack(">I", pager_cksum(page1)))
        with open(str(db_path) + "-journal", "wb") as jf:
            jf.write(jhdr + rec)
        # 改动计数器 +1 ⇒ 跟日志里的副本对不上 ⇒ 日志是热的
        cc = struct.unpack(">I", page1[24:28])[0]
        f.seek(24)
        f.write(struct.pack(">I", (cc + 1) & 0xffffffff))
    return cc


async def main():
    tmp = mkdtemp(prefix="aging-quarantine-")
    DB.QUARANTINED.clear()

    # ── ① 坏库 → 隔离 + 重建 ──────────────────────────────────────
    section("① 坏库 → 改名隔离 + 重建")
    reports = Path(tmp) / "reports"
    reports.mkdir(parents=True)
    db_path = reports / "history.db"
    make_db(db_path)
    corrupt_data_pages(db_path)
    # 把 mtime 拨到「三天前」—— 模拟产线实况: 那个坏库是**上次拔盘前**
    # 最后写的, 之后没人碰过它。⇒ 隔离件(改名来的)的 mtime 也会是那天,
    #    而**隔离时刻是今天** —— 两者必须分得开。
    import time as _time
    old_ts = _time.mktime(_time.strptime("2026-11-09 17:31:22", "%Y-%m-%d %H:%M:%S"))
    os.utime(db_path, (old_ts, old_ts))
    before = digest(db_path)
    DB.DB_PATH = db_path                      # 回归测试把库指到临时目录

    # ⚠️ **界面上不许提**(2026-11-10 用户定)⇒ 这件事的留痕就落在**日志**里
    #    ⇒ 日志那一行是**唯一的**人话记录, 必须在场。
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        await DB.init()
    log_text = buf.getvalue()

    isolates = DB.find_quarantined(db_path)
    check(len(isolates) == 1, "坏库被改名隔离了(就一个隔离件)", isolates)
    if isolates:
        iso = isolates[0]
        check(iso.exists(), "隔离件还在盘上(**没有删**)", iso)
        check(digest(iso) == before, "隔离件内容跟原来一模一样(没动过)")
        check(iso.name.startswith("history.db.corrupt-"),
              "隔离件名字带前缀+时间戳: %s" % iso.name)
    check(db_path.exists(), "新库建起来了(history.db 还在原处)")
    conn = sqlite3.connect(db_path)
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")]
    conn.close()
    check("runs" in tables and "results" in tables,
          "新库有完整的表结构", tables)
    check(len(DB.QUARANTINED) == 1, "隔离这件事被记下来了(要给界面报)")
    check(DB.QUARANTINED and DB.QUARANTINED[0].get("file"),
          "隔离记录里有文件名", DB.QUARANTINED)
    # 界面上不提 ⇒ **日志是唯一的留痕** ⇒ 它必须在场, 且要写清楚
    check("隔离" in log_text, "日志里写了「隔离」这件事", log_text[-200:])
    check(isolates and isolates[0].name in log_text,
          "日志里写出了隔离件的文件名(要能找得到)", log_text[-200:])
    check("没有删" in log_text, "日志里说清楚了「没有删」", log_text[-200:])
    st = DB.history_db_status()
    check(st.get("quarantined") is not None,
          "/api/config 拿得到「这次隔离过」", st)
    check(st.get("quarantined", {}).get("file") == isolates[0].name
          if isolates else False,
          "报出来的文件名跟盘上的隔离件对得上", st.get("quarantined"))

    section("② 隔离之后应用照常能用")
    run_id = await DB.Database.create_run({"x": 1}, {"sn": "TEST"})
    check(isinstance(run_id, int) and run_id > 0,
          "能插一轮(这才是修复的意义)", run_id)
    await DB.Database.upsert_result(run_id, "cpu", status="passed",
                                    log="y" * 3000)
    got = await DB.Database.get_run(run_id)
    check(got is not None, "刚插的那轮读得回来", got)

    section("③ 好库不许被隔离")
    DB.QUARANTINED.clear()
    good_dir = Path(tmp) / "good"
    good_dir.mkdir()
    good = good_dir / "history.db"
    make_db(good)
    DB.DB_PATH = good
    await DB.init()
    check(DB.find_quarantined(good) == [], "好库没有隔离件")
    check(len(DB.QUARANTINED) == 0, "好库不该报隔离")
    conn = sqlite3.connect(good)
    n = conn.execute("SELECT count(*) FROM results").fetchone()[0]
    conn.close()
    check(n == 500, "好库的数据还在(没被清掉)", n)

    section("④ 热日志能回滚的库不许被隔离")
    # ⚠️ **两份**: 「只读打开行不行」必须在**回滚发生之前**问 ——
    #    一旦体检(读写打开)回滚掉了, 再问就只剩一个健康的库, 问不出所以然。
    ro_dir = Path(tmp) / "hot_ro"
    ro_dir.mkdir()
    ro_db = ro_dir / "history.db"
    make_db(ro_db)
    cc0 = fabricate_hot_journal(ro_db)
    check(True, "造出了**真热**日志(改动计数器 %d → %d)" % (cc0, cc0 + 1))
    ro_ok = True
    ro_err = ""
    try:
        ro = sqlite3.connect("file:%s?mode=ro" % ro_db, uri=True)
        ro.execute("SELECT count(*) FROM runs").fetchone()
        ro.close()
    except Exception as e:
        ro_ok = False
        ro_err = str(e)
    check(not ro_ok, "只读打开**回滚不了**(所以体检不能只读)", ro_err)

    hot_dir = Path(tmp) / "hot"
    hot_dir.mkdir()
    hot = hot_dir / "history.db"
    make_db(hot)
    cc1 = fabricate_hot_journal(hot)
    ok, why = await DB.probe_health(hot)
    check(ok is True, "热日志回滚后这个库是好的 ⇒ 不该隔离", why)
    check(not Path(str(hot) + "-journal").exists(),
          "回滚完日志被清掉了(回滚真的发生了)")
    with open(hot, "rb") as f:
        f.seek(24)
        cc_after = struct.unpack(">I", f.read(4))[0]
    # ⚠️ 回滚完 SQLite 会把改动计数器**再 +1** —— 这是它「那份日志已经作废」
    #    的标记(下次打开不会再回滚一遍)。所以这里是 cc1+1, 不是 cc1。
    check(cc_after == cc1 + 1,
          "改动计数器 %d → %d(回滚后 +1 把日志作废)" % (cc1, cc_after), cc_after)
    ro2 = sqlite3.connect("file:%s?mode=ro" % hot, uri=True)
    ro2.execute("SELECT count(*) FROM runs").fetchone()
    ro2.close()
    check(True, "回滚之后只读打开也行了(日志已经不热)")
    conn = sqlite3.connect(hot)
    ic = conn.execute("PRAGMA integrity_check").fetchone()[0]
    conn.close()
    check(ic == "ok", "回滚后 integrity_check=ok", ic)
    check(DB.find_quarantined(hot) == [], "回滚-able 的库没有隔离件")

    section("⑤ 隔离这件事得留痕(日志 + 机器可查), 界面不提")
    DB.DB_PATH = db_path
    # 模拟**服务重启**: 内存里那条「本次隔离过」没了(新进程),
    # 但盘上的隔离件还在 ⇒ 界面仍然该告诉工人「这根盘的历史重置过」。
    DB.QUARANTINED.clear()
    status = DB.history_db_status()
    check(status.get("quarantined") is None,
          "重启后不再说「这次隔离过」", status)
    check(status.get("previous"), "但报得出「以前隔离过」(永久证据)", status)
    check(status["previous"][0]["file"].startswith("history.db.corrupt-"),
          "以前那个隔离件的名字也在", status["previous"])
    # ⚠️ **隔离时刻 ≠ 旧库最后一次写入的时刻** —— 隔离是**改名**, 改名不改
    #    mtime ⇒ 拿 mtime 当「什么时候坏的」会把排查带偏(实况: 隔离在
    #    11-10 14:45, mtime 却还是 11-09 17:31)。隔离时刻写在**文件名**里。
    prev_file = status["previous"][0]["file"]
    stamp = prev_file.rsplit(".corrupt-", 1)[-1]
    expect_at = _time.strftime("%Y-%m-%d %H:%M:%S",
                               _time.strptime(stamp, "%Y%m%d-%H%M%S"))
    check(status["previous"][0]["at"] == expect_at,
          "报的是**隔离时刻**(从文件名取), 不是旧库的 mtime",
          "%s vs %s" % (status["previous"][0]["at"], expect_at))
    check(status["previous"][0]["at"] != "2026-11-09 17:31:22",
          "确实没拿旧库的写入时刻当隔离时刻", status["previous"][0])

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
