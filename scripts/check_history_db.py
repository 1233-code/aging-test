#!/usr/bin/env python3
"""历史库体检 —— 看一个 `history.db` 到底是「好」「坏」还是「坏在哪儿」。

**为什么要有这个**: U 盘是**可以随时被拔走**的介质 ⇒ SQLite 的文件随时可能
被写坏(拔盘时页缓存里的内容没落盘)。坏掉的库有好几种表现, 而且**症状和病根
对不上**:

  ① **整个库读不了** —— `sqlite3.DatabaseError: database disk image is malformed`,
     打开就炸;
  ② **表能读、但写不进去** —— 读只走「活着的页」, 所以 `SELECT` 一切正常;
     而任何**要新分配页的写**都得去翻空闲页链 / 翻 b-tree ⇒ 一翻就炸 ⇒
     **应用能起来、历史页能看, 但一开测就报错**;
  ③ **某张表坏、另一张好** —— 坏的是**数据页**。

⇒ 只看「报没报错」不够。本工具把上面几种**分开验**:
读、逐表读、逐索引读(逼它走那条索引)、空闲页链、**写测试**(在临时副本上做)、
并用 SQLite 自己的 `integrity_check` / `dbstat` 给出**权威结论**。

**只读**: 目标文件**只以 `mode=ro` 打开**, 本脚本**不写、不改、不删**它
(连 mtime 都不动 —— 有测试盯着这一点)。唯一一次写操作发生在**临时副本**上
(写测试): 先把文件复制一份到临时目录, 在**副本**上写, 副本用完即弃。

⚠️ **要在库正被服务写的时候用它, 先把文件拷一份再查** —— 拷贝本身更可靠,
而且查坏了也不影响生产库。

⚠️ **旁边有「热日志」(`-journal`)时, 本工具下不了结论**(退出码 3):
   热日志 = 拔盘时写到一半留下的 ⇒ **只读**打开回滚不了它 ⇒ 每一个查询都会
   抛 `attempt to write a readonly database` ⇒ 那些「读不出来」是**只读**造成
   的, **不是**「库坏了」的证据 ⇒ 此时**以应用启动时的体检为准**(应用是**读写**
   打开的 ⇒ SQLite 自己把热日志回滚掉 ⇒ 回滚成功的话这个库其实是好的)。

用法:
    python3 scripts/check_history_db.py <history.db 路径>
    python3 scripts/check_history_db.py <路径> --json

退出码:
    0  库是好的(能读、也能写)
    1  库坏了(读不了 / 有坏页 / 表读不出来 / 写不进去 / **索引坏了** /
              空闲页链不自洽 / `results.log` 有行读不出来)
    2  用法错或文件不在
    3  **下不了结论**(旁边有热日志, 只读回滚不了 —— 见上面那段)
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from urllib.parse import quote

#: 要试读的表 —— 跟 `server/db.py` 的 SCHEMA 对齐。
#: 加了新表记得往这儿加(不然新表读不出来这儿也不报)。
TABLES = ("runs", "results")

#: 合法的 b-tree 页类型字节(见 SQLite 文件格式 §「B-tree Page Header」)
BTREE_TYPES = {0x02, 0x05, 0x0A, 0x0D}


def _open_ro(path):
    """**只读**打开一个 SQLite 文件。

    ⚠️ `mode=ro` 是 SQLite 自己保证的: 打开这个连接**写不了**那个文件。
    """
    uri = "file:" + quote(os.path.abspath(path)) + "?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=5.0)


def _quote_ident(name):
    return '"' + str(name).replace('"', '""') + '"'


#: 回滚日志(Rollback Journal)文件头的 magic —— 见 SQLite 文件格式 §
#: «Rollback Journal Format»。头 8 字节是这个 ⇒ 这才是一个**热日志**
#: (SQLite 自己认的、开机要回滚的); 不是 ⇒ 那是个 SQLite 压根不理的文件。
JOURNAL_MAGIC = b"\xd9\xd5\x05\xf9\x20\xa1\x63\xd7"


def _journal_looks_hot(path):
    """这个 `-journal` 文件**看着像热日志**吗(头 8 字节是不是 magic)。"""
    try:
        with open(path, "rb") as f:
            head = f.read(8)
    except OSError:
        return False
    return head == JOURNAL_MAGIC


# ── SQLite 自己的权威结论 ────────────────────────────────────────────

def _integrity_check(conn, limit=50):
    """`PRAGMA integrity_check` —— SQLite 自己说的「哪儿坏了」。

    ⚠️ 它**可能直接抛异常**(而不是返回错误清单)—— 库坏得厉害时就是这样
       ⇒ 抛了也得把异常记下来, 那本身就是结论。
    """
    try:
        rows = conn.execute("PRAGMA integrity_check(%d)" % limit).fetchall()
        msgs = [str(r[0]) for r in rows]
        return {"ok": bool(msgs) and msgs[0].strip().lower() == "ok",
                "messages": msgs, "raised": None}
    except Exception as e:
        return {"ok": False, "messages": [],
                "raised": "%s: %s" % (type(e).__name__, e)}


def _dbstat_pages(conn):
    """用 `dbstat` 虚拟表问出**每一页的归属**。

    ⚠️ **为什么不能自己猜页类型**: 大字段(`results.log` 那种整段日志)会
       **溢出**到独立页, 溢出页**本来就没有 b-tree 头** ⇒ 头一个字节是内容
       的一部分, 常常是 0x00 ⇒ 自己按「类型字节」判, 会把**正常的溢出页**
       误报成「坏页」(这个坑踩过)。

    返回 {页号: (页类型, 所属表)}; dbstat 用不了就返回 None。
    """
    try:
        rows = conn.execute("SELECT pageno, pagetype, name FROM dbstat"
                             ).fetchall()
    except Exception:
        return None
    return {int(r[0]): (r[1], r[2]) for r in rows}


def _index_scan(conn, table, index):
    """强制**走某个索引**扫一遍 —— 索引坏了, 表扫描可能照样正常。

    ⚠️ SQLite **不许**按名字查索引(`SELECT count(*) FROM idx_xxx` ⇒
       「no such table」)⇒ 得用 `INDEXED BY` 逼它走那条索引。
    """
    try:
        cur = conn.execute("SELECT count(*) FROM %s INDEXED BY %s"
                           % (_quote_ident(table), _quote_ident(index)))
        return {"readable": True, "rows": cur.fetchone()[0], "error": None}
    except Exception as e:
        return {"readable": False, "rows": None,
                "error": "%s: %s" % (type(e).__name__, e)}


def _payload_probe(conn, table, col):
    """**逐行把那一列读出来**(不是 count) —— 看有没有哪一行读不动。

    ⚠️ `count(*)` 只走 b-tree 的页头, **不读载荷** ⇒ 「表能读」不代表
       「每行的数据都读得动」。大字段(`results.log`)溢出到独立页,
       拔盘打断的往往正是那些页 ⇒ 某几行的日志读不出来。
    """
    try:
        rows = conn.execute("SELECT id, length(%s) FROM %s ORDER BY id"
                             % (_quote_ident(col), _quote_ident(table))
                             ).fetchall()
        sizes = [r[1] or 0 for r in rows]
        return {"ok": True, "rows": len(rows),
                "max_bytes": max(sizes) if sizes else 0,
                "empty": sum(1 for s in sizes if s == 0),
                "error": None}
    except Exception as e:
        return {"ok": False, "rows": None, "max_bytes": None, "empty": None,
                "error": "%s: %s" % (type(e).__name__, e)}


def _probe_sqlite_sequence(conn):
    """`sqlite_sequence` —— 自增计数器表。

    ⚠️ **它是隐形的关键角色**: 表里有 `AUTOINCREMENT` ⇒ SQLite 每次插一行
       都得改它。表扫描 / 索引扫描**都不碰它**, 但**插入一定碰** ⇒
       它坏了 ⇒ 「读全正常、一写就 malformed」。
    """
    try:
        rows = conn.execute("SELECT name, seq FROM sqlite_sequence").fetchall()
        return {"readable": True, "rows": rows, "error": None}
    except Exception as e:
        msg = str(e)
        # 「压根没有 sqlite_sequence 这张表」= 这个库里没有 AUTOINCREMENT 的表
        # ⇒ **不是缺陷**(只是用不上自增计数器) ⇒ 不许算进「坏」。
        if "no such table" in msg:
            return {"readable": True, "rows": [], "error": None,
                    "note": "这个库没有 AUTOINCREMENT 的表(没有 sqlite_sequence)"}
        return {"readable": False, "rows": None,
                "error": "%s: %s" % (type(e).__name__, e)}


# ── 自己看文件 ───────────────────────────────────────────────────────

def _freelist_pages(path, page_size):
    """把空闲页链走一遍 —— 返回 (空闲页总数, 空闲页号集合, 链是否自洽)。

    ⚠️ **空闲页链坏掉是最阴的一种坏法**: 读只走活着的页 ⇒ 一切正常;
       写要新分配页 ⇒ 得翻这条链 ⇒ 一翻就 `malformed`。
    """
    pages = set()
    total = None
    trunk = None
    try:
        with open(path, "rb") as f:
            head = f.read(100)
            if len(head) < 100:
                return None, pages, False
            trunk = int.from_bytes(head[32:36], "big")
            total = int.from_bytes(head[36:40], "big")
            seen = set()
            while trunk:
                if trunk in seen:          # 链绕回来了 ⇒ 不自洽
                    return total, pages, False
                seen.add(trunk)
                f.seek((trunk - 1) * page_size)
                data = f.read(page_size)
                if len(data) < 8:
                    return total, pages, False
                nxt = int.from_bytes(data[0:4], "big")
                nleaf = int.from_bytes(data[4:8], "big")
                for i in range(nleaf):
                    p = int.from_bytes(data[8 + 4 * i:12 + 4 * i], "big")
                    if p:
                        pages.add(p)
                trunk = nxt
    except Exception:
        return total, pages, False
    return total, pages, True


def _scan_pages(path, page_size, page_count, skip_pages, owner):
    """**逐页**看 b-tree 头 —— 定位「到底第几页不对」。

    判据(SQLite 文件格式): 每页第 1 字节是页类型(第 1 页除外 —— 它的
    b-tree 头在偏移 100, 前面 100 字节是数据库头); 类型必须是
    0x02/0x05/0x0A/0x0D; 偏移 1~2 是「第一个空闲块」, 3~4 是「单元数」,
    5~6 是「单元内容区起点」—— 起点越界/倒挂 ⇒ 这页被写坏了。

    ⚠️ **溢出页不算坏** —— 它们没有 b-tree 头 ⇒ 第一个字节是内容的一部分。
       `owner`(dbstat 的结果)说这是溢出页的, 直接跳过, 别误报。
    """
    bad = []
    overflow = []
    size = os.path.getsize(path)
    if page_size and page_count and size < page_count * page_size:
        bad.append({"page": 0,
                    "why": "文件被截断: 应有 %d 字节, 实际 %d"
                           % (page_count * page_size, size)})
    with open(path, "rb") as f:
        for pno in range(1, (page_count or 0) + 1):
            if pno in skip_pages:
                continue
            f.seek((pno - 1) * page_size)
            page = f.read(page_size)
            if len(page) < page_size:
                bad.append({"page": pno, "why": "页读不全(文件被截断)"})
                continue
            off = 100 if pno == 1 else 0
            ptype = page[off]
            if ptype not in BTREE_TYPES:
                # 头一个字节不是页类型 ⇒ 要么是**溢出页**(正常), 要么真坏了
                if owner is not None and pno in owner:
                    overflow.append(pno)
                    continue
                if ptype == 0x00 and owner is None:
                    # dbstat 用不了时不敢下结论 —— 只记着, 交给 integrity_check 判
                    overflow.append(pno)
                    continue
                bad.append({"page": pno,
                            "why": "b-tree 头不像样(类型字节=0x%02X)" % ptype})
                continue
            first_free = int.from_bytes(page[off + 1:off + 3], "big")
            ncell = int.from_bytes(page[off + 3:off + 5], "big")
            content = int.from_bytes(page[off + 5:off + 7], "big")
            hdr = 12 if ptype in (0x02, 0x05) else 8
            if content == 0:
                content = 65536            # 65536 字节页的特例
            if content > page_size or content < hdr:
                bad.append({"page": pno,
                            "why": "单元内容区起点越界(%d, 页大小 %d)"
                                   % (content, page_size)})
            elif first_free and not (hdr <= first_free <= page_size):
                bad.append({"page": pno,
                            "why": "空闲块指针越界(%d)" % first_free})
            elif ncell * 8 > page_size:
                bad.append({"page": pno, "why": "单元数离谱(%d)" % ncell})
    return bad, overflow


# ── 写测试 ───────────────────────────────────────────────────────────

def _write_test(path):
    """**写测试** —— 在**临时副本**上做, 目标文件一个字节都不碰。

    为什么必须单独测: 「读得好好的」不等于「写得了」。空闲页链 / b-tree
    坏了的库, 读全正常、写必炸 —— 而应用正是**先读后写** ⇒ 症状是
    「服务起得来、历史页能看, 一开测就报错」。

    ⚠️ **一步一步分开试**, 因为「插一行」和「删一行」坏法不一样:
       插一行要**新分配页**; 删一行要**进空闲页链**。混在一起测就分不清
       是哪一种坏了 —— 而应用平时只插不删 ⇒ 得分清才说得准「还能不能用」。
    """
    tmpdir = tempfile.mkdtemp(prefix="aging-db-writetest-")
    steps = []

    def step(name, fn):
        """跑一步, 记下成功还是失败(失败不往下一步走)。"""
        try:
            fn()
            steps.append({"name": name, "ok": True, "error": None})
            return True
        except Exception as e:
            steps.append({"name": name, "ok": False,
                          "error": "%s: %s" % (type(e).__name__, e)})
            return False

    try:
        scratch = os.path.join(tmpdir, "scratch.db")
        shutil.copyfile(path, scratch)
        try:
            conn = sqlite3.connect(scratch, timeout=5.0)
        except Exception as e:
            return {"ok": False, "steps": [{"name": "打开副本", "ok": False,
                                            "error": str(e)}]}
        try:
            def update_existing():
                cur = conn.execute("SELECT min(id) FROM runs")
                row = cur.fetchone()
                if row and row[0] is not None:
                    conn.execute("UPDATE runs SET status=status WHERE id=?",
                                 (row[0],))
                    conn.commit()

            def insert_run():
                conn.execute("INSERT INTO runs (started_at, status)"
                             " VALUES (0, 'probe')")
                conn.commit()

            def insert_result():
                # 应用真正做的事: 记一项结果, 带一段日志(大字段 ⇒ 溢出页)
                conn.execute("INSERT INTO results (run_id, test_name, status,"
                             " log) VALUES (1, 'probe', 'passed', ?)",
                             ("x" * 4000,))
                conn.commit()

            def delete_probes():
                conn.execute("DELETE FROM results WHERE test_name='probe'")
                conn.execute("DELETE FROM runs WHERE status='probe'")
                conn.commit()

            step("改一行现有的", update_existing)
            if step("插一轮(runs)", insert_run):
                step("记一项结果(results, 带 4KB 日志)", insert_result)
                step("删掉探针行(DELETE ⇒ 走空闲页链)", delete_probes)
        finally:
            conn.close()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    ok = all(s["ok"] for s in steps)
    failed = [s for s in steps if not s["ok"]]
    return {"ok": ok, "steps": steps,
            "stage": failed[0]["name"] if failed else None,
            "error": failed[0]["error"] if failed else None}


# ── 汇总 ─────────────────────────────────────────────────────────────

def diagnose(path, write_test=True):
    """体检一个库文件, 返回一个 dict(不抛异常)。"""
    out = {
        "path": os.path.abspath(path),
        "exists": os.path.isfile(path),
        "size_bytes": os.path.getsize(path) if os.path.isfile(path) else 0,
        "page_size": None,
        "page_count": None,
        "freelist_count": None,
        "freelist_chain_ok": None,
        "sqlite_version": sqlite3.sqlite_version,
        "open_ok": False,
        "quick_check": None,
        "quick_check_ok": None,
        "integrity_check": None,
        "tables": {},
        "indexes": {},
        "sqlite_sequence": None,
        "payload_probe": None,
        "bad_pages": [],
        "overflow_pages": [],
        "write_test": None,
        "hot_journal": False,
        "hot_journal_size": 0,
        "journal_looks_hot": False,
        "indeterminate": False,
        "problems": [],
    }
    if not out["exists"]:
        out["problems"].append("文件不存在")
        return out

    # ⚠️ **热日志**(拔盘时写到一半留下的 `-journal`)在**只读**打开时
    #    **回滚不了** ⇒ 后面每一个 PRAGMA / 查询都会抛
    #    `attempt to write a readonly database` ⇒ 那**不是库坏了的证据**,
    #    是**只读**造成的 ⇒ 这种情况下这个脚本**下不了结论**。
    #    (应用启动时的体检是**读写**打开的 ⇒ SQLite 自己把热日志回滚掉 ⇒
    #     回滚成功的话这个库其实是好的。)
    jpath = path + "-journal"
    if os.path.isfile(jpath):
        out["hot_journal"] = True
        try:
            out["hot_journal_size"] = os.path.getsize(jpath)
        except OSError:
            out["hot_journal_size"] = 0
        out["journal_looks_hot"] = _journal_looks_hot(jpath)

    try:
        conn = _open_ro(path)
    except Exception as e:
        out["problems"].append("打不开: %s" % (e,))
        return out

    out["open_ok"] = True
    owner = None
    try:
        for pragma in ("page_size", "page_count", "freelist_count"):
            try:
                row = conn.execute("PRAGMA %s" % pragma).fetchone()
                out[pragma] = row[0] if row else None
            except Exception as e:
                out["problems"].append("读 PRAGMA %s 失败: %s" % (pragma, e))

        # ① SQLite 自己的权威结论
        try:
            row = conn.execute("PRAGMA quick_check").fetchone()
            verdict = (row[0] if row else "") or ""
            out["quick_check"] = verdict
            out["quick_check_ok"] = (verdict.strip().lower() == "ok")
            if not out["quick_check_ok"]:
                out["problems"].append("quick_check: %s" % verdict)
        except Exception as e:
            out["quick_check_ok"] = False
            out["quick_check"] = None
            out["problems"].append("quick_check 跑不动: %s" % (e,))

        ic = _integrity_check(conn)
        out["integrity_check"] = ic
        if ic["raised"]:
            out["problems"].append("integrity_check 直接抛异常: %s" % ic["raised"])
        elif not ic["ok"]:
            out["problems"].append("integrity_check: %s"
                                   % "; ".join(ic["messages"][:5]))

        # ② 逐张表试读 —— 坏的是**数据页**时, 这里才看得出是哪张表
        for t in TABLES:
            info = {"readable": None, "rows": None, "error": None}
            try:
                cur = conn.execute("SELECT count(*) FROM %s" % _quote_ident(t))
                info["rows"] = cur.fetchone()[0]
                info["readable"] = True
            except Exception as e:
                info["readable"] = False
                info["error"] = "%s: %s" % (type(e).__name__, e)
                out["problems"].append("表 %s 读不出来 —— %s"
                                       % (t, info["error"]))
            out["tables"][t] = info

        # ③ 逐个索引试读 —— 强制走那条索引
        try:
            idx_rows = conn.execute(
                "SELECT name, tbl_name FROM sqlite_master WHERE type='index'"
            ).fetchall()
            for iname, tname in idx_rows:
                if not iname:
                    continue
                info = _index_scan(conn, tname, iname)
                out["indexes"][iname] = info
                if info["readable"] is False:
                    out["problems"].append("索引 %s 读不出来 —— %s"
                                           % (iname, info["error"]))
        except Exception as e:
            out["problems"].append("列索引失败: %s" % (e,))

        # ④ 每一页的归属(dbstat 是权威) —— 免得把溢出页误报成坏页
        owner = _dbstat_pages(conn)

        # ⑤ 自增计数器表 + 大字段载荷 —— 「读得动」不等于「写得进」
        seq = _probe_sqlite_sequence(conn)
        out["sqlite_sequence"] = seq
        if seq["readable"] is False:
            out["problems"].append("sqlite_sequence 读不出来 —— %s" % seq["error"])
        payload = _payload_probe(conn, "results", "log")
        out["payload_probe"] = payload
        if payload["ok"] is False:
            out["problems"].append("results.log 有行读不出来 —— %s"
                                   % payload["error"])
    finally:
        try:
            conn.close()
        except Exception:
            pass

    # ⑥ 空闲页链 + 逐页体检 —— 定位「坏在哪儿」
    ps, pc = out["page_size"], out["page_count"]
    if ps and pc:
        total, free_pages, chain_ok = _freelist_pages(path, ps)
        out["freelist_count"] = total
        out["freelist_chain_ok"] = chain_ok
        if not chain_ok:
            out["problems"].append("空闲页链不自洽(读没事、写要翻它 ⇒ 会炸)")
        try:
            bad, overflow = _scan_pages(path, ps, pc, free_pages, owner)
            out["bad_pages"] = bad
            out["overflow_pages"] = overflow
        except Exception as e:
            out["problems"].append("逐页体检失败: %s" % (e,))
        for b in out["bad_pages"][:5]:
            out["problems"].append("第 %d 页坏 —— %s" % (b["page"], b["why"]))

    # ⑦ 写测试 —— 在临时副本上做
    if write_test:
        out["write_test"] = _write_test(path)
        if not out["write_test"].get("ok"):
            out["problems"].append(
                "**写不进去** —— %s 那一步就炸了: %s"
                % (out["write_test"].get("stage"),
                   out["write_test"].get("error")))

    # ⑧ **热日志还在** ⇒ 这个脚本**下不了结论** —— 只读打开回滚不了它,
    #    上面那些「读不出来」是**只读**造成的, 不是「库坏了」的证据。
    #    ⚠️ 「看着像热日志」的判据 = 文件头的 magic(见 SQLite 文件格式 §
    #    Rollback Journal)。magic 不在 ⇒ 那是 SQLite 压根不认的残留文件 ⇒
    #    结论照常成立(只是提一句)。
    readonly_problems = [p for p in out["problems"]
                         if "readonly database" in p or "read-only" in p]
    if out["hot_journal"] and (out["journal_looks_hot"] or readonly_problems):
        out["indeterminate"] = True
        out["problems"].insert(0,
            "这个库旁边有**热日志**(-journal, %d 字节)⇒ **只读**打开回滚不了它"
            " ⇒ 上面那些「读不出来」是**只读**造成的, **不能**当成「库坏了」的"
            "证据 ⇒ 本次**下不了结论**(应用启动时是**读写**打开的 ⇒ SQLite 自己"
            "把热日志回滚掉 ⇒ 回滚成功的话这个库其实是好的 ⇒ **以应用启动体检的"
            "结论为准**)" % out["hot_journal_size"])
    elif out["hot_journal"]:
        out["problems"].append(
            "旁边有个 -journal 文件(%d 字节, 但**不像是热日志**)⇒ SQLite 不会"
            "去回滚它 ⇒ 结论不受影响" % out["hot_journal_size"])
    return out


def _fmt(out):
    lines = []
    lines.append("库: %s" % out["path"])
    if not out["exists"]:
        lines.append("  ✗ 文件不存在")
        return "\n".join(lines)
    lines.append("  大小: %d 字节   page_size=%s  page_count=%s"
                 % (out["size_bytes"], out["page_size"], out["page_count"]))
    lines.append("  SQLite 版本: %s" % out["sqlite_version"])
    lines.append("  空闲页: %s   空闲页链自洽: %s"
                 % (out["freelist_count"], out["freelist_chain_ok"]))
    if out["quick_check_ok"] is True:
        lines.append("  ✓ quick_check: ok")
    elif out["quick_check_ok"] is False:
        lines.append("  ✗ quick_check: %s"
                     % (out["quick_check"] or "(跑不动 —— 库本身有坏页)"))
    ic = out.get("integrity_check") or {}
    if ic.get("raised"):
        lines.append("  ✗ integrity_check 直接抛异常: %s" % ic["raised"])
    elif ic.get("messages") and not ic.get("ok"):
        for m in ic["messages"][:6]:
            lines.append("  ✗ integrity_check: %s" % m)
    elif ic.get("ok"):
        lines.append("  ✓ integrity_check: ok")
    for t, info in out["tables"].items():
        if info["readable"] is True:
            lines.append("  ✓ 表 %-8s 可读, %s 行" % (t, info["rows"]))
        elif info["readable"] is False:
            lines.append("  ✗ 表 %-8s 读不出来: %s" % (t, info["error"]))
    for iname, info in out["indexes"].items():
        if info["readable"] is True:
            lines.append("  ✓ 索引 %-18s 可读, %s 行" % (iname, info["rows"]))
        elif info["readable"] is False:
            lines.append("  ✗ 索引 %-18s 读不出来: %s" % (iname, info["error"]))
    seq = out.get("sqlite_sequence")
    if seq:
        if seq["readable"]:
            lines.append("  ✓ sqlite_sequence 可读: %s"
                         % (", ".join("%s=%s" % (r[0], r[1]) for r in seq["rows"])
                            or "(空)"))
        else:
            lines.append("  ✗ sqlite_sequence 读不出来: %s" % seq["error"])
    pl = out.get("payload_probe")
    if pl:
        if pl["ok"]:
            lines.append("  ✓ results.log 逐行读得动(最长 %s 字节, %s 行是空的)"
                         % (pl["max_bytes"], pl["empty"]))
        else:
            lines.append("  ✗ results.log 有行读不出来: %s" % pl["error"])
    if out["bad_pages"]:
        lines.append("  ✗ 页体检: %d 页看着不对" % len(out["bad_pages"]))
        for b in out["bad_pages"][:8]:
            lines.append("     · 第 %d 页 —— %s" % (b["page"], b["why"]))
    if out["overflow_pages"]:
        lines.append("  · 另有 %d 页是**溢出页**(大字段溢出用的, 正常)"
                     % len(out["overflow_pages"]))
    wt = out["write_test"]
    if wt:
        lines.append("  写测试(在**临时副本**上做的, 没碰目标):")
        for s in wt.get("steps", []):
            if s["ok"]:
                lines.append("     ✓ %s" % s["name"])
            else:
                lines.append("     ✗ %s —— %s" % (s["name"], s["error"]))
    if out["problems"]:
        if out.get("indeterminate"):
            lines.append("  ⇒ 结论: **下不了结论**(见下面第一条)")
        else:
            lines.append("  ⇒ 结论: **这个库是坏的**")
        for prob in out["problems"]:
            lines.append("     · %s" % prob)
    else:
        lines.append("  ⇒ 结论: **这个库是好的**")
    return "\n".join(lines)


def main(argv):
    args = [a for a in argv[1:] if not a.startswith("--")]
    want_json = "--json" in argv
    if len(args) != 1:
        sys.stderr.write(__doc__)
        return 2
    out = diagnose(args[0])
    if want_json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print(_fmt(out))
    if not out["exists"]:
        return 2                      # 文件不在 = 用法/路径不对, 不是「库坏了」
    if out.get("indeterminate"):
        # 「热日志还在, 只读回滚不了」⇒ **下不了结论** —— 不能说它坏, 也不能
        # 说它好(退出码 0 = 「库是好的」, 那是撒谎)⇒ 单独一个码。
        return 3
    # ⚠️ **这四项以前不在退出码里** ⇒ 脚本文案明明写着「这个库是坏的」,
    #    退出码却是 0(「库是好的」)⇒ **按退出码判断的流程会把坏库当成好库**。
    #    ① `integrity_check` 失败(只有**索引**坏了时 quick_check 看不出来);
    #    ② `sqlite_sequence` 读不出来(插行必碰它 ⇒ 写不进去);
    #    ③ `results.log` 有行读不出来(大字段溢出页断链);
    #    ④ 空闲页链不自洽(读没事、写要翻它 ⇒ 会炸)。
    healthy = (out["quick_check_ok"]
               and (out.get("integrity_check") or {}).get("ok")
               and all(i["readable"] for i in out["tables"].values())
               and all(i["readable"] for i in out["indexes"].values())
               and not out["bad_pages"]
               and out.get("freelist_chain_ok") is not False
               and (out.get("sqlite_sequence") or {}).get("readable") is not False
               and (out.get("payload_probe") or {}).get("ok") is not False
               and (out["write_test"] or {}).get("ok"))
    return 0 if healthy else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
