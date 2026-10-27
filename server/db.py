"""SQLite 数据库 - 存测试历史

Schema:
  runs: 每次完整运行 (含 mini-run, mini_of 指向父 run)
  results: 每次运行的逐项结果
"""
import aiosqlite
import json
import time
from pathlib import Path
from typing import Any, Optional

from .config import Config


DB_PATH = Config.project_root() / "reports" / "history.db"


SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at REAL NOT NULL,
    finished_at REAL,
    status TEXT NOT NULL,        -- running / passed / failed / stopped / error
    config_json TEXT,
    summary_json TEXT,
    mini_of INTEGER,            -- mini-run 模式: 指向父 run id (NULL = full run)
    error TEXT,                 -- 系统级错误(若有)
    sn TEXT,                    -- §2.2 被测设备 SN, 也用于 PDF 文件名
    operator TEXT,              -- 测试人员姓名(§8.2 报告头「测试人员」行)
    batch TEXT,                 -- 批次号
    pdf_path TEXT,              -- §8.2 导出后的 PDF 路径(NULL = 还没导出/导出失败)
    updated_at REAL,            -- 内容最近一次变更的时间(合并重测时刷新)
    retest_count INTEGER DEFAULT 0  -- 被重测合并过几轮(「第 N 次重测通过」)
);

CREATE TABLE IF NOT EXISTS results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    test_name TEXT NOT NULL,
    status TEXT NOT NULL,        -- pending / running / passed / failed / skipped / error
    started_at REAL,
    finished_at REAL,
    duration_sec REAL,
    metrics_json TEXT,
    log TEXT,
    error TEXT,
    attempt INTEGER DEFAULT 1, -- 这一项是第几次尝试(1 = 首测, 2 = 第一次重测)
    FOREIGN KEY (run_id) REFERENCES runs(id)
);

CREATE INDEX IF NOT EXISTS idx_results_run ON results(run_id);
CREATE INDEX IF NOT EXISTS idx_runs_started ON runs(started_at DESC);
-- idx_runs_mini_of 在 init() 里另行创建, 因为依赖 migration 后才有的 mini_of 列
"""


async def _ensure_column(db, table: str, column: str, typedef: str):
    """Idempotent ALTER TABLE ADD COLUMN (SQLite 没有 IF NOT EXISTS)."""
    cur = await db.execute(f"PRAGMA table_info({table})")
    cols = {row[1] for row in await cur.fetchall()}
    if column not in cols:
        await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {typedef}")


async def init():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        # 1. CREATE TABLE (对老 DB 是 no-op)
        await db.executescript(SCHEMA)
        # 2. Migrations: 补加老 DB 缺的列
        await _ensure_column(db, "runs", "mini_of", "INTEGER")
        await _ensure_column(db, "runs", "error", "TEXT")
        await _ensure_column(db, "runs", "sn", "TEXT")
        await _ensure_column(db, "runs", "operator", "TEXT")
        await _ensure_column(db, "runs", "batch", "TEXT")
        await _ensure_column(db, "runs", "pdf_path", "TEXT")
        # §7.7 合并重测: updated_at 决定历史列表的排序(被重测合并过的记录
        # 要能跳到最上面), retest_count 记「第几次重测通过」。
        await _ensure_column(db, "runs", "updated_at", "REAL")
        await _ensure_column(db, "runs", "retest_count", "INTEGER DEFAULT 0")
        # §7.7 逐项「第 N 次测试通过」: 这一项是第几次尝试。
        await _ensure_column(db, "results", "attempt", "INTEGER DEFAULT 1")
        # 3. 现在 mini_of 一定存在了, 创建依赖它的索引
        try:
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_runs_mini_of ON runs(mini_of)"
            )
        except aiosqlite.OperationalError:
            pass  # 不应该走到这里, 但防御性兜底
        await db.commit()


class Database:
    @staticmethod
    async def init():
        await init()

    @staticmethod
    async def create_run(config: dict, meta: dict = None) -> int:
        """创建一次完整的 run.

        `meta` 里的 sn / operator / batch 落库 —— PDF 文件名和报告头都要用。
        现在存下来, 以后换机器、重装、重启服务都还能补导历史报告。
        """
        m = meta or {}
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "INSERT INTO runs (started_at, status, config_json, sn, operator, batch, "
                "updated_at, retest_count) VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
                (time.time(), "running", json.dumps(config, ensure_ascii=False),
                 m.get("sn"), m.get("operator"), m.get("batch"), time.time())
            )
            await db.commit()
            return cur.lastrowid

    @staticmethod
    async def create_mini_run(parent_run_id: int, config: dict, meta: dict = None) -> int:
        """创建 mini-run, mini_of 指回父 run.

        同样收 meta: 重测轮的 PDF 也得有 SN, 否则文件名叫 `unknown_*.pdf`,
        归档时根本认不出是哪台机器。
        """
        m = meta or {}
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "INSERT INTO runs (started_at, status, config_json, mini_of, sn, operator, batch, "
                "updated_at, retest_count) VALUES (?, 'running', ?, ?, ?, ?, ?, ?, 0)",
                (time.time(), json.dumps(config, ensure_ascii=False), parent_run_id,
                 m.get("sn"), m.get("operator"), m.get("batch"), time.time())
            )
            await db.commit()
            return cur.lastrowid

    @staticmethod
    async def set_pdf_path(run_id: int, pdf_path: str):
        """§8.2: 导出成功后回写 PDF 路径。

        导出失败时**不要**调用 —— pdf_path 留空本身就代表"没导出",
        不用再造一个 error 列去记失败原因(日志和 UI warning 已经带了)。
        """
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE runs SET pdf_path=? WHERE id=?", (pdf_path, run_id)
            )
            await db.commit()

    @staticmethod
    async def finish_run(run_id: int, status: str, summary: dict, error: str = None):
        """收尾 run, 写 status / finished_at / summary / error."""
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE runs SET finished_at=?, status=?, summary_json=?, error=? WHERE id=?",
                (time.time(), status, json.dumps(summary, ensure_ascii=False), error, run_id)
            )
            await db.commit()

    # ──────────────────── §7.7 合并重测 ────────────────────
    #
    # mini-run 跑完之后要把结果**合并回父轮**: 父轮的逐项结果已经被就地更新了,
    # 但父轮自己的 status / summary_json 还停在重测前那一份 —— 界面上逐项写着
    # 「通过」、汇总却还是「失败 3/5」, 报告也还是失败那版。下面的方法负责把
    # 父轮重算一遍并落库。

    @staticmethod
    async def fix_stale_running(run_id: int) -> int:
        """把残留的 running 行改成 stopped (§9.3 口径: 中途停止 = 没跑到)。

        什么时候会残留: mini-run 开跑时会把父轮对应项先写成 running(让界面别
        停在旧状态上), 万一这一轮中途被停, 那一行就永远停在 running ——
        而 running 不落在 §9.2 的任何一档里, 父轮的计数会凭空少一项, 汇总对不上账。

        返回改了几行。
        """
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "UPDATE results SET status='stopped', "
                "error=COALESCE(error, '重测中途停止, 本项未跑完') "
                "WHERE run_id=? AND status='running'",
                (run_id,)
            )
            await db.commit()
            return cur.rowcount or 0

    @staticmethod
    async def recompute_run(run_id: int) -> Optional[dict]:
        """按 §9.2 口径重算某一轮的 status + summary。

        口径跟 runner._run_tests_body 里的 summary **完全一致**(同一个定义,
        不许各算各的):
          · total = passed + failed + error —— skipped / stopped **不计入**
          · 有 failed / error → failed
          · total == 0 → skipped(**不得**判 passed —— 否则全跳过能拿一张空报告)
          · 否则 → passed

        另有一条: 当初**被工人停掉**的一轮(total > 0 但还有没跑到的项),
        重测把失败项修好之后**仍然是 stopped**, 不是 passed ——
        没测过的项合不合格谁也不知道, 不能因为重测就宣布「整体通过」。

        返回 {"status":..., "summary":{...}} 或 None(轮次不存在)。
        """
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "SELECT status, config_json FROM runs WHERE id=?", (run_id,)
            )
            row = await cur.fetchone()
            if not row:
                return None
            old_status, config_json = row[0], row[1]

            cur = await db.execute(
                "SELECT test_name, status FROM results WHERE run_id=?", (run_id,)
            )
            rows = await cur.fetchall()

            counts = {"passed": 0, "failed": 0, "error": 0,
                      "skipped": 0, "not_run": 0}
            ran = set()
            for name, st in rows:
                ran.add(name)
                if st == "passed":
                    counts["passed"] += 1
                elif st == "failed":
                    counts["failed"] += 1
                elif st == "error":
                    counts["error"] += 1
                elif st == "skipped":
                    counts["skipped"] += 1
                elif st == "stopped":
                    counts["not_run"] += 1
                # 'running' / 'pending' 不落进任何一档 —— 它们不是结论。
                # (残留的 running 由 fix_stale_running 先清掉)

            # planned = 这一轮**勾选**了几项。
            # 从 config 里数(enabled 或实际跑过的), 跟报告「共勾选 K」同一口径 ——
            # 两处数字对不上是最容易被工人当场发现的破绽。
            try:
                cfg = json.loads(config_json) if config_json else {}
            except (TypeError, ValueError):
                cfg = {}
            test_cfgs = (cfg.get("tests") if isinstance(cfg, dict) else None) or {}
            planned_names = set()
            for name, tv in test_cfgs.items():
                if not isinstance(tv, dict):
                    continue
                if tv.get("enabled") or name in ran:
                    planned_names.add(name)
            # 勾了但**连行都没有**的项 = 没跑到(中途被停的) → 算 not_run
            counts["not_run"] += len(planned_names - ran)

            total = counts["passed"] + counts["failed"] + counts["error"]
            summary = {
                "total": total,
                "passed": counts["passed"],
                "failed": counts["failed"],
                "error": counts["error"],
                "skipped": counts["skipped"],
                "not_run": counts["not_run"],
                # 老库的 config 里可能压根没有 enabled(那时候按
                # `cfg.get("enabled", False)` 判定) —— 这时用「已测 + 跳过 +
                # 没跑到」兜底, 跟报告 render_html 里的回退一致。
                "planned": (len(planned_names)
                            or (total + counts["skipped"] + counts["not_run"])),
            }

            if counts["failed"] or counts["error"]:
                status = "failed"
            elif total == 0:
                status = "skipped"
            elif old_status == "stopped" and counts["not_run"] > 0:
                status = "stopped"
            else:
                status = "passed"

            return {"status": status, "summary": summary}

    @staticmethod
    async def apply_merge(run_id: int, status: str, summary: dict) -> int:
        """把合并后的 status / summary 写回父轮, 并记一次重测。

        返回这一轮累计的重测轮数(retest_count)—— 界面上标「第 N 次重测通过」
        用的就是它。

        updated_at 一起刷: 历史列表按它排序, 被合并过的记录要能跳到最上面
        (它刚刚才动过, 是列表里最新的状态)。
        """
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE runs SET status=?, summary_json=?, updated_at=?, "
                "retest_count=COALESCE(retest_count,0)+1 WHERE id=?",
                (status, json.dumps(summary, ensure_ascii=False), time.time(), run_id)
            )
            await db.commit()
            cur = await db.execute(
                "SELECT retest_count FROM runs WHERE id=?", (run_id,)
            )
            row = await cur.fetchone()
            return int(row[0]) if row and row[0] else 1

    @staticmethod
    async def upsert_result(run_id: int, test_name: str, **fields):
        """有就更新, 没有就插入."""
        now = time.time()
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "SELECT id FROM results WHERE run_id=? AND test_name=?",
                (run_id, test_name)
            )
            row = await cur.fetchone()

            if row:
                sets = []
                vals = []
                for k, v in fields.items():
                    if k == "metrics":
                        sets.append("metrics_json=?")
                        vals.append(json.dumps(v, ensure_ascii=False) if v else None)
                    else:
                        sets.append(f"{k}=?")
                        vals.append(v)
                vals.append(row[0])
                await db.execute(f"UPDATE results SET {', '.join(sets)} WHERE id=?", vals)
            else:
                await db.execute(
                    """INSERT INTO results
                       (run_id, test_name, status, started_at, metrics_json, log, error, duration_sec, finished_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        run_id, test_name,
                        fields.get("status", "pending"),
                        fields.get("started_at", now),
                        json.dumps(fields.get("metrics"), ensure_ascii=False) if fields.get("metrics") else None,
                        fields.get("log"),
                        fields.get("error"),
                        fields.get("duration_sec"),
                        fields.get("finished_at")
                    )
                )
            await db.commit()

    @staticmethod
    async def list_runs(limit: int = 50, include_mini: bool = False) -> list:
        """列出最近 N 次 run —— **默认只列归档记录(不含重测轮)**。

        带出 sn / pdf_path: 历史列表要显示 SN 列, 并据 pdf_path 决定
        「打开 PDF」还是「重新导出 PDF」按钮。

        ⚠️ **重测轮(mini-run)默认不列** —— 它是**过程**, 不是归档对象
        (§7.7.4: 归档对象始终是最初那一轮)。列出来的话, 工人重测两次
        就会在列表里看见**四条**带「重测」字样的行(两条是原来那两条记录,
        两条是重测这个过程本身), 看着像"四条记录都重测过" —— 而实际只
        重测过两条。重测轮的信息**不丢**: 它挂在它那一轮记录的**详情**里
        (见 get_run 的 retest_runs), 写清哪一轮、什么时候、重测了哪几项、
        结果如何。

        include_mini=True 给排障用(看原始行); 界面不用。

        排序键是 **COALESCE(updated_at, started_at)** 而不是 started_at:
        被重测合并过的记录, 内容刚刚才更新过 —— 它是列表里**最新的状态**,
        该排在最上面; 可它的 started_at 还是当初那轮老时间, 按它排的话这条
        记录会永远沉在老位置, 工人刚合完却找不到它。
        """
        where = "" if include_mini else " WHERE mini_of IS NULL "
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "SELECT id, started_at, finished_at, status, summary_json, mini_of, error, "
                "sn, pdf_path, updated_at, retest_count "
                "FROM runs" + where +
                " ORDER BY COALESCE(updated_at, started_at) DESC LIMIT ?",
                (limit,)
            )
            rows = await cur.fetchall()
            return [
                {
                    "id": r[0], "started_at": r[1], "finished_at": r[2],
                    "status": r[3],
                    "summary": json.loads(r[4]) if r[4] else None,
                    "mini_of": r[5],
                    "error": r[6],
                    "sn": r[7],
                    "pdf_path": r[8],
                    "updated_at": r[9],
                    "retest_count": r[10] or 0,
                }
                for r in rows
            ]

    @staticmethod
    async def get_run(run_id: int) -> Optional[dict]:
        """单次 run 详情: 元数据 + 全部 results.

        结果里带 sn/operator/batch/pdf_path —— 手动补导 PDF 时正好复用这份
        run dict, 不用再回头查一遍设备身份。
        """
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "SELECT id, started_at, finished_at, status, config_json, summary_json, "
                "mini_of, error, sn, operator, batch, pdf_path, updated_at, retest_count "
                "FROM runs WHERE id=?",
                (run_id,)
            )
            row = await cur.fetchone()
            if not row:
                return None
            run = {
                "id": row[0], "started_at": row[1], "finished_at": row[2],
                "status": row[3],
                "config": json.loads(row[4]) if row[4] else None,
                "summary": json.loads(row[5]) if row[5] else None,
                "mini_of": row[6],
                "error": row[7],
                "sn": row[8],
                "operator": row[9],
                "batch": row[10],
                "pdf_path": row[11],
                "updated_at": row[12],
                "retest_count": row[13] or 0,
            }
            # ORDER BY id —— 逐项按**首次执行的顺序**排(报告的逐项段、
            # 合并后的结果列表都靠这个顺序), 不靠 SQLite 碰巧怎么扫表。
            cur = await db.execute(
                "SELECT test_name, status, started_at, finished_at, duration_sec, "
                "metrics_json, log, error, attempt FROM results WHERE run_id=? "
                "ORDER BY id",
                (run_id,)
            )
            results = await cur.fetchall()
            run["results"] = [
                {
                    "test_name": r[0], "status": r[1],
                    "started_at": r[2], "finished_at": r[3],
                    "duration_sec": r[4],
                    "metrics": json.loads(r[5]) if r[5] else None,
                    "log": r[6], "error": r[7],
                    # 第几次尝试(1 = 首测)。老库没这列时是 NULL → 当 1。
                    "attempt": r[8] or 1,
                }
                for r in results
            ]
        # §7.7 这一轮底下有几轮重测 —— **重测轮不单独占历史列表的一行**
        # (见 list_runs), 它的信息挂在这里, 详情里写清「哪一轮、重测了
        # 哪几项、结果如何」。出了 with 再查 —— 少开一个连接, 也免得嵌套。
        run["retest_runs"] = await Database.get_retest_runs(run_id)
        return run

    @staticmethod
    async def get_result_log(run_id: int, test_name: str) -> Optional[str]:
        """取某一项已落库的日志。"""
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "SELECT log FROM results WHERE run_id=? AND test_name=?",
                (run_id, test_name)
            )
            row = await cur.fetchone()
            return row[0] if row else None

    @staticmethod
    async def get_retest_runs(parent_id: int) -> list:
        """列出父轮底下的**全部重测轮**(mini_of = parent_id), 按 id 升序。

        §7.7: 重测轮的日志**不在**父轮那一行里 —— 父轮的 `log` 是首测的
        原始日志, 原封不动(那是返修时最要看的证据)。重测的日志留在**重测轮
        自己的行**里, 报告要印它的时候从这里读。

        返回 [{id, started_at, finished_at, status, results: [...]}, ...],
        每个 result 带 test_name / status / attempt / log。

        ⚠️ **只读** —— 这个方法不改任何数据。
        """
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "SELECT id, started_at, finished_at, status FROM runs "
                "WHERE mini_of=? ORDER BY id",
                (parent_id,)
            )
            runs = await cur.fetchall()
            out = []
            for rid, started, finished, status in runs:
                cur = await db.execute(
                    "SELECT test_name, status, attempt, log FROM results "
                    "WHERE run_id=? ORDER BY id",
                    (rid,)
                )
                rows = await cur.fetchall()
                out.append({
                    "id": rid,
                    "started_at": started,
                    "finished_at": finished,
                    "status": status,
                    "results": [
                        {
                            "test_name": r[0],
                            "status": r[1],
                            "attempt": r[2] or 1,
                            "log": r[3],
                        }
                        for r in rows
                    ],
                })
            return out

    @staticmethod
    async def get_result_attempts(run_id: int) -> dict:
        """取某一轮逐项的 attempt(这一项是第几次尝试)。

        重测时用: 父轮里这一项已经试过 N 次, 这次重测就是第 N+1 次 ——
        「第 2 次测试通过」那个记号就是这么来的。
        """
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "SELECT test_name, attempt FROM results WHERE run_id=?", (run_id,)
            )
            rows = await cur.fetchall()
            return {r[0]: (r[1] or 1) for r in rows}

    @staticmethod
    async def find_incomplete_results(run_id: int, statuses: list) -> list:
        """返回指定 run 中状态匹配的 test_name 列表. 用于重测时筛 failed 项."""
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "SELECT test_name FROM results WHERE run_id=? AND status IN "
                "(" + ",".join("?" * len(statuses)) + ")",
                [run_id] + statuses
            )
            rows = await cur.fetchall()
            return [r[0] for r in rows]
