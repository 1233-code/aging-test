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
    error TEXT                  -- 系统级错误(若有)
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
    async def create_run(config: dict) -> int:
        """创建一次完整的 run."""
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "INSERT INTO runs (started_at, status, config_json) VALUES (?, ?, ?)",
                (time.time(), "running", json.dumps(config, ensure_ascii=False))
            )
            await db.commit()
            return cur.lastrowid

    @staticmethod
    async def create_mini_run(parent_run_id: int, config: dict) -> int:
        """创建 mini-run, mini_of 指回父 run."""
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "INSERT INTO runs (started_at, status, config_json, mini_of) "
                "VALUES (?, 'running', ?, ?)",
                (time.time(), json.dumps(config, ensure_ascii=False), parent_run_id)
            )
            await db.commit()
            return cur.lastrowid

    @staticmethod
    async def finish_run(run_id: int, status: str, summary: dict, error: str = None):
        """收尾 run, 写 status / finished_at / summary / error."""
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE runs SET finished_at=?, status=?, summary_json=?, error=? WHERE id=?",
                (time.time(), status, json.dumps(summary, ensure_ascii=False), error, run_id)
            )
            await db.commit()

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
    async def list_runs(limit: int = 50) -> list:
        """列出最近 N 次 run (含 mini-run)."""
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "SELECT id, started_at, finished_at, status, summary_json, mini_of, error "
                "FROM runs ORDER BY started_at DESC LIMIT ?",
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
                }
                for r in rows
            ]

    @staticmethod
    async def get_run(run_id: int) -> Optional[dict]:
        """单次 run 详情: 元数据 + 全部 results."""
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "SELECT id, started_at, finished_at, status, config_json, summary_json, mini_of, error "
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
            }
            cur = await db.execute(
                "SELECT test_name, status, started_at, finished_at, duration_sec, "
                "metrics_json, log, error FROM results WHERE run_id=?",
                (run_id,)
            )
            results = await cur.fetchall()
            run["results"] = [
                {
                    "test_name": r[0], "status": r[1],
                    "started_at": r[2], "finished_at": r[3],
                    "duration_sec": r[4],
                    "metrics": json.loads(r[5]) if r[5] else None,
                    "log": r[6], "error": r[7]
                }
                for r in results
            ]
            return run

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
