"""回归测试: 历史库体检工具(`scripts/check_history_db.py`)说得对不对。

起因(2026-11-10 产线盘实况): 盘上的 `reports/history.db` 报
`database disk image is malformed` ⇒ 启动时「收尾残留轮次」失败、
历史页 500。**光看一条报错看不出坏在哪儿** —— 有三种坏法, 表现完全不同:

  ① **整个库读不了**(文件头就坏了)     ⇒ 一打开就炸;
  ② **库能开、坏的是数据页**(表头没事)  ⇒ `CREATE TABLE` 照样成功,
     一直到**扫数据**才炸 ⇒ 「启动没报错」不等于「库是好的」;
  ③ **某一张表坏、另一张好**             ⇒ 还能抢救出另一张表的数据。

⇒ 工具必须能**分清这三种**, 而且**绝不能写目标文件**(它查的就是产线库)。

这个测试守三件事:
  ① 好库 ⇒ 说「好」, 并把两张表的行数报对;
  ② **数据页坏**的库 ⇒ 说得清「哪张表坏、哪张表还能读」(而不是笼统一句「坏了」);
  ③ **一个字节都不许改目标文件**(sha256 + mtime 前后一致) —— 一个「体检工具」
     要是能改坏产线库, 那它比坏库本身还危险。

跑法: .venv/bin/python -m tests.test_history_db_check
"""
import hashlib
import importlib.util
import os
import shutil
import sqlite3
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests._tmp import mkdtemp                       # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(REPO, ".venv", "bin", "python")
TOOL_REL = os.path.join("scripts", "check_history_db.py")
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
    """把体检脚本装进来(它在 scripts/ 下, 不是包)。"""
    path = os.path.join(REPO, TOOL_REL)
    spec = importlib.util.spec_from_file_location("_check_history_db", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def make_good_db(path, n_results=500):
    """造一个**正常**的库: 两张表, 跟生产 schema 一样的形状。

    ⚠️ `results` 塞够行数是为了让它**跨好几页** ⇒ 后面才有「坏在数据页」
       这回事可测(行少的话整个表都在第 2 页里, 一坏就全坏, 分不出
       「哪张表坏」)。
    """
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA page_size=4096")
    conn.execute("CREATE TABLE runs (id INTEGER PRIMARY KEY AUTOINCREMENT,"
                 " started_at REAL, status TEXT)")
    conn.execute("CREATE TABLE results (id INTEGER PRIMARY KEY AUTOINCREMENT,"
                 " run_id INTEGER, test_name TEXT, status TEXT, log TEXT)")
    conn.execute("INSERT INTO runs (started_at, status) VALUES (1.0, 'passed')")
    for i in range(n_results):
        conn.execute("INSERT INTO results (run_id, test_name, status, log)"
                     " VALUES (1, 'cpu', 'passed', ?)", ("x" * 120,))
    conn.commit()
    conn.close()


def corrupt_data_pages(path, first_page=5, n_pages=3):
    """把**数据页**写坏(header 留着 —— 模拟「拔盘时写到一半」)。

    ⚠️ 为什么不写坏第 1 页: 第 1 页是**表头**(schema) ⇒ 写坏它 = 整个库
       打不开(那是另一种坏法)。这里要造的是**表头没事、扫数据才炸**那种。
    """
    conn = sqlite3.connect(path)
    page_size = conn.execute("PRAGMA page_size").fetchone()[0]
    conn.close()
    with open(path, "r+b") as f:
        for p in range(first_page, first_page + n_pages):
            f.seek(p * page_size)
            f.write(b"\xa5" * 512)


def main():
    tool = load_tool()
    tmp = mkdtemp(prefix="aging-history-db-")
    good = os.path.join(tmp, "good.db")
    bad = os.path.join(tmp, "bad.db")

    print("① 好库")
    make_good_db(good)
    out = tool.diagnose(good)
    check(out["exists"] and out["open_ok"], "好库能打开")
    check(out["quick_check_ok"] is True, "quick_check 说 ok", out["quick_check"])
    check(out["tables"]["runs"]["readable"] is True, "runs 表能读")
    check(out["tables"]["results"]["readable"] is True, "results 表能读")
    check(out["tables"]["runs"]["rows"] == 1, "runs 行数报对(1)",
          out["tables"]["runs"]["rows"])
    check(out["tables"]["results"]["rows"] == 500, "results 行数报对(500)",
          out["tables"]["results"]["rows"])
    check(not out["problems"], "好库不该有问题", out["problems"])

    print("② 坏在数据页的库")
    make_good_db(bad)
    corrupt_data_pages(bad)
    out = tool.diagnose(bad)
    check(out["open_ok"] is True, "坏库**能打开**(坏的是数据页, 不是表头)")
    check(out["quick_check_ok"] is False, "quick_check 认出它坏了",
          out["quick_check"])
    check(out["tables"]["runs"]["readable"] is True,
          "runs 表还能读(坏页在 results 那边)", out["tables"]["runs"])
    check(out["tables"]["results"]["readable"] is False,
          "results 表读不出来", out["tables"]["results"])
    check(any("results" in p for p in out["problems"]),
          "问题清单里点名了 results", out["problems"])

    print("③ 工具**不许**写目标文件")
    for name, path in (("好库", good), ("坏库", bad)):
        h0, m0 = sha256(path), os.stat(path).st_mtime_ns
        tool.diagnose(path)
        check(sha256(path) == h0, "%s: 内容没变" % name)
        check(os.stat(path).st_mtime_ns == m0, "%s: mtime 没变" % name)

    print("④ 命令行(退出码)")
    r = subprocess.run([PY, os.path.join(REPO, TOOL_REL), good],
                       capture_output=True, text=True)
    check(r.returncode == 0, "好库 ⇒ 退出码 0", r.returncode)
    check("库是好的" in r.stdout, "好库 ⇒ 打印「库是好的」", r.stdout[-300:])
    r = subprocess.run([PY, os.path.join(REPO, TOOL_REL), bad],
                       capture_output=True, text=True)
    check(r.returncode == 1, "坏库 ⇒ 退出码 1", r.returncode)
    check("库是坏的" in r.stdout, "坏库 ⇒ 打印「库是坏的」", r.stdout[-300:])
    r = subprocess.run([PY, os.path.join(REPO, TOOL_REL),
                        os.path.join(tmp, "没有这个.db")],
                       capture_output=True, text=True)
    check(r.returncode == 2, "文件不在 ⇒ 退出码 2", r.returncode)
    r = subprocess.run([PY, os.path.join(REPO, TOOL_REL), good, "--json"],
                       capture_output=True, text=True)
    check(r.returncode == 0 and '"quick_check_ok": true' in r.stdout,
          "--json 也能用", r.stdout[-300:])

    print("⑤ 别把工具的名字写错(脚本没了/改名了 ⇒ 体检就落空)")
    check(os.path.isfile(os.path.join(REPO, TOOL_REL)),
          "体检脚本在: scripts/check_history_db.py")

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
