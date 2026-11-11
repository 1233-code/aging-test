"""回归测试: 历史库体检工具的**结论**和它的**退出码**得说同一件事。

起因(2026-11-11 审查查出来的两个洞):

  ① **退出码漏了四类问题** —— `scripts/check_history_db.py` 打印的结论是
     「⇒ 结论: **这个库是坏的**」, 但它的退出码 `healthy` 表达式里**没有**
     `integrity_check` ⇒ 只有**索引**坏掉的库(`quick_check` 看不出来,
     `integrity_check` 才看得见)会「文案说坏、退出码说 0(= 库是好的)」
     ⇒ **按退出码判断的流程会把坏库当成好库**。同一批漏掉的还有:
     `sqlite_sequence` 读不出来 / `results.log` 有行读不出来 / 空闲页链不自洽。

  ② **热日志下不下结论** —— 盘上留着 `-journal`(拔盘时写到一半)时,
     **只读**打开回滚不了它 ⇒ 每个查询都抛
     `attempt to write a readonly database` ⇒ 那些「读不出来」是**只读**
     造成的, **不是**「库坏了」的证据 ⇒ 而应用启动时的体检是**读写**打开的
     (能回滚) ⇒ 同一个库, 脚本说「坏」、应用说「好」⇒ 工人照脚本的结论
     动手改名/清理 ⇒ **好端端的历史数据被工人自己弄丢**。

这个测试守三件事:
  ① 索引坏的库 ⇒ **退出码 1**(不是 0)+ 文案说「坏」;
  ② 旁边有**热日志** ⇒ **退出码 3**(=「下不了结论」)+ 文案**不许**说「坏」;
  ③ 好库 ⇒ 退出码仍然 0(别把好库误判 —— 误杀好板 > 漏判边缘板)。

跑法: .venv/bin/python -m tests.test_history_db_check_verdict
"""
import importlib.util
import os
import shutil
import sqlite3
import struct
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests._tmp import mkdtemp                       # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(REPO, ".venv", "bin", "python")
TOOL_REL = os.path.join("scripts", "check_history_db.py")
# 变异脚本会把**改坏的那一份**指过来(AGING_HISTORY_DB_CHECK_TOOL)。
TOOL_PATH = os.environ.get("AGING_HISTORY_DB_CHECK_TOOL",
                            os.path.join(REPO, TOOL_REL))

#: 回滚日志的文件头 magic(见 SQLite 文件格式 §Rollback Journal Format)。
JOURNAL_MAGIC = b"\xd9\xd5\x05\xf9\x20\xa1\x63\xd7"

FAILS = []
PASSES = 0


def check(cond, msg, extra=""):
    global PASSES
    if cond:
        PASSES += 1
        print("  ✓ " + msg)
    else:
        FAILS.append(msg)
        print("  ✗ " + msg + ("  → " + str(extra) if extra else ""))


def load_tool():
    path = TOOL_PATH
    spec = importlib.util.spec_from_file_location("_check_history_db_v", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def make_db(path, n_results=300):
    """造一个**正常**的库(跟生产 schema 一样的形状)。"""
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA page_size=4096")
    conn.execute("CREATE TABLE runs (id INTEGER PRIMARY KEY AUTOINCREMENT,"
                 " started_at REAL, status TEXT)")
    conn.execute("CREATE TABLE results (id INTEGER PRIMARY KEY AUTOINCREMENT,"
                 " run_id INTEGER, test_name TEXT, status TEXT, log TEXT)")
    conn.execute("CREATE INDEX idx_results_run ON results(run_id)")
    conn.execute("INSERT INTO runs (started_at, status) VALUES (1.0, 'passed')")
    for _ in range(n_results):
        conn.execute("INSERT INTO results (run_id, test_name, status, log)"
                     " VALUES (1, 'cpu', 'passed', ?)", ("x" * 120,))
    conn.commit()
    conn.close()


def break_index_only(path):
    """只把**索引**弄坏 —— `quick_check` 看不出来, `integrity_check` 看得出来。

    做法: 把索引的定义换掉(writable_schema)⇒ 索引里的内容跟它自己的定义
    对不上 ⇒ SQLite 报「row N missing from index」, 而 `quick_check` 照样说 ok。
    ⚠️ 这正是「症状跟病根对不上」的典型: **表、索引、写入全都正常**,
       只有深查才炸。
    """
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA writable_schema=ON")
    conn.execute("UPDATE sqlite_master SET sql='CREATE INDEX idx_results_run"
                 " ON results(status)' WHERE name='idx_results_run'")
    conn.commit()
    conn.close()


def fabricate_hot_journal(db_path):
    """手造一个**真热**的日志(等价于「提交写到一半掉电」)。

    ⚠️ **为什么不用「起个事务然后强杀」**: 那样造出来的日志**不热** ——
       事务没提交 ⇒ 数据还在页缓存里、DB 文件一个字没改 ⇒ 改动计数器没变
       ⇒ SQLite 判定「这日志不是热的」直接忽略 ⇒ 只读打开照样成功。
       (这个坑踩过一轮才想明白。)

    「热」的真条件(SQLite 文件格式 §Hot Journal): 日志头合法 **且**
    **DB 文件的改动计数器跟日志里第 1 页的副本对不上** ⇒ 说明「提交写了一半」。
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

        jhdr = (JOURNAL_MAGIC + struct.pack(">I", 1)
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


def write_stale_journal(path):
    """放一个 SQLite 压根不理的残留 `-journal`(头不是 magic)。"""
    with open(path + "-journal", "wb") as f:
        f.write(b"\x00" * 512)


def run_tool(path):
    r = subprocess.run([PY, TOOL_PATH, path],
                       capture_output=True, text=True)
    return r.returncode, r.stdout


def main():
    tool = load_tool()
    tmp = mkdtemp(prefix="aging-dbcheck-verdict-")

    # ── ① 只有索引坏 ────────────────────────────────────────────────
    print("① 只有**索引**坏的库(quick_check 说 ok, integrity_check 说得坏)")
    idx_bad = os.path.join(tmp, "idx_bad.db")
    make_db(idx_bad)
    break_index_only(idx_bad)
    out = tool.diagnose(idx_bad)
    check(out["quick_check_ok"] is True,
          "前提: quick_check 看不出来(它说 ok)", out["quick_check"])
    check((out.get("integrity_check") or {}).get("ok") is False,
          "integrity_check 看得出索引坏了", out.get("integrity_check"))
    code, text = run_tool(idx_bad)
    check(code == 1,
          "**退出码 1**(库坏了)—— 以前是 0(= 库是好的)⇒ 按码判断的流程会"
          "把坏库当成好库", code)
    check("这个库是坏的" in text, "文案也说「坏」(跟退出码一个口径)",
          text[-400:])

    # ── ② 好库不许被误判 ───────────────────────────────────────────
    print("② 好库 ⇒ 退出码还是 0(别把好库误判)")
    good = os.path.join(tmp, "good.db")
    make_db(good)
    code, text = run_tool(good)
    check(code == 0, "好库 ⇒ 退出码 0", code)
    check("库是好的" in text, "好库 ⇒ 文案说「好」", text[-300:])

    # ── ③ 旁边有热日志 ⇒ 下不了结论 ────────────────────────────────
    print("③ 旁边有**热日志** ⇒ 下不了结论(退出码 3), 不许说「坏」")
    hot = os.path.join(tmp, "hot.db")
    make_db(hot)
    fabricate_hot_journal(hot)
    out = tool.diagnose(hot)
    check(out["hot_journal"] is True, "认出了旁边的 -journal")
    check(out["journal_looks_hot"] is True, "认出了它是**热日志**(头有 magic)")
    check(out["indeterminate"] is True, "结论标成「下不了结论」")
    code, text = run_tool(hot)
    check(code == 3, "退出码 3(= 下不了结论), 不是 1(= 库坏了)", code)
    check("这个库是坏的" not in text,
          "文案**不许**说「这个库是坏的」—— 只读回滚不了热日志, 那些"
          "「读不出来」是只读造成的, 不是库坏的证据", text[-500:])
    check("下不了结论" in text, "文案说「下不了结论」", text[-500:])

    # ── ④ 不像热日志的残留 -journal ⇒ 结论照常成立 ──────────────────
    print("④ 残留的 -journal(头**不是** magic ⇒ SQLite 压根不理它)")
    stale = os.path.join(tmp, "stale.db")
    make_db(stale)
    write_stale_journal(stale)
    code, text = run_tool(stale)
    check(code == 0, "不是热日志 ⇒ 退出码照常是 0(好库还是好库)", code)

    shutil.rmtree(tmp, ignore_errors=True)
    print("\n" + "─" * 60)
    if FAILS:
        print("✗ %d 项失败 / 共 %d 项" % (len(FAILS), PASSES + len(FAILS)))
        for f in FAILS:
            print("   · " + f)
        return 1
    print("✓ 全部通过 (%d 项)" % PASSES)
    return 0


if __name__ == "__main__":
    sys.exit(main())
