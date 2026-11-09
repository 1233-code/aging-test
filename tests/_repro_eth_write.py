"""诊断脚本(不是回归测试): 复现 U 盘 run2 里 ethernet 那次「最终落库」失败。

背景(2026-11-05 实测):
    产线归档库里有一轮(run 2)的 ethernet 那一行停在 status='running',
    finished_at / log / duration_sec / metrics 全是 NULL,
    而同一轮其余 10 项都正常落了库 ⇒ runner.py:706 那次最终落库没生效。

本脚本做两件事:
    ① 现场跑一遍网口项(链路现在是 DOWN ⇒ 走的正是那一轮「没插网线」的分支),
       把真正的 result / metrics / log 打出来;
    ② 用**新建的空库**复刻 runner.py:706 那个 upsert_result 调用, 看它会不会抛。

⚠️ 不碰任何真库: DB_PATH 指向 tempfile 里的新库, 库里只有本脚本自己写的行。
"""
import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from server import db as dbmod                          # noqa: E402
from server.tests import ethernet as ethmod            # noqa: E402
from server.tests.base import TestContext              # noqa: E402


async def _noop(*a, **kw):
    pass


async def main():
    tmp = Path(tempfile.mkdtemp(prefix="repro-eth-"))
    dbmod.DB_PATH = tmp / "history.db"
    await dbmod.Database.init()
    print(f"临时空库: {dbmod.DB_PATH}")

    logs = []

    async def on_log(line):
        logs.append(line)

    ctx = TestContext(
        run_id=999, config={}, test_config={},
        on_progress=_noop, on_log=on_log, is_cancelled=_noop,
        on_metric=_noop, on_alert=_noop,
    )

    inst = ethmod.EthTest()
    t0 = time.monotonic()
    result = await inst.run(ctx)
    dur = time.monotonic() - t0

    print(f"\n── 网口项实际跑了 {dur:.3f}s ──")
    print("status  =", result.status)
    print("error   =", result.error)
    print("metrics =", result.metrics)
    print("log:")
    for line in logs:
        print("   ", line)

    # runner.py:564 —— 开跑时先落一行 running
    await dbmod.Database.upsert_result(999, "ethernet", status="running",
                                        started_at=time.time())

    # runner.py:694-706 —— common_fields 原样
    result.duration_sec = dur
    common_fields = {
        "status": result.status,
        "finished_at": time.time(),
        "duration_sec": result.duration_sec,
        "metrics": result.metrics,
        "log": "\n".join(logs),
        "error": result.error,
        "attempt": 1,
    }
    print("\n── 现在做那次最终落库 (runner.py:706) ──")
    try:
        await dbmod.Database.upsert_result(999, "ethernet", **common_fields)
        print("✅ 落库成功")
    except Exception as e:
        print(f"❌ 落库失败: {type(e).__name__}: {e}")

    import sqlite3
    con = sqlite3.connect(dbmod.DB_PATH)
    row = con.execute(
        "SELECT status, finished_at, duration_sec, length(log), length(metrics_json) "
        "FROM results WHERE run_id=999").fetchone()
    print("库里那一行现在是:", row)
    con.close()


if __name__ == "__main__":
    asyncio.run(main())
