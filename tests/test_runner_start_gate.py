"""回归测试: 「开跑」的闸门要**关得住**(2026-11-11 查出来的竞态)。

起因:
    `TestRunner.start_mini()` 开头有一道闸门:
        if self.status == "running": return {"ok": False, "error": "已有测试在运行"}
    而它真正把 `self.status = "running"` 设下去, 是在**好几个 await 之后**
    (取父轮配置 → 往上找根轮 → 取父轮已试次数 → 建 mini 轮记录)。

    ⇒ 那段窗口里 `status` 还是旧的 ⇒ **第二个** start_mini() 照样过得去闸门
    ⇒ 工人**连点两下「重测」**(或者前端重复发一次请求)就撞上了:
      · 两轮重测**同时**跑;
      · 第二个把第一个的 `self._task` 顶掉 ⇒ 第一轮没人管 ⇒
        它的结果**写不回父轮**、界面上的「⏳ 等待」永远等不到;
      · 轮次记录也乱了(两轮 mini 都往同一个根轮上合并)。

    `start()` 也有同样的窗口(闸门到 `status = "running"` 之间隔着建库记录)。

修法: 闸门后面**同步**占位(`self._starting = True`)⇒ 占位和放位之间
    一行 await 都没有 ⇒ 不可能有第二个 start/start_mini 插进来。

这个测试守四件事:
  ① 两个 start() 同时来 ⇒ **只有一个**真开跑, 另一个被挡;
  ② 两个 start_mini() 同时来 ⇒ **只有一个**真开跑(建库记录只建一条);
  ③ start() 和 start_mini() 撞在一起 ⇒ 也只有一个能开跑;
  ④ 开跑**炸了**(抛异常)⇒ 占位要放掉 ⇒ 之后还能再开跑(不能永久卡死)。

只动内存里的 runner 和假 Database —— 不碰真库、不碰真硬件、不写产物。
跑法: .venv/bin/python -m tests.test_runner_start_gate
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import runner as R                     # noqa: E402

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


class FakeDb:
    """慢一点的假库 —— `await asyncio.sleep()` 就是那段「窗口」。

    ⚠️ 必须**真的 await 一下**: 不 await 的话, 闸门和 `status = "running"`
       之间就没有窗口了, 竞态压根不会发生, 测试就是**绿的假象**。
    """
    calls = {"create_run": 0, "create_mini_run": 0}

    @staticmethod
    async def _nap():
        await asyncio.sleep(0.05)

    @staticmethod
    async def create_run(config, meta=None):
        FakeDb.calls["create_run"] += 1
        await FakeDb._nap()
        return 1

    @staticmethod
    async def create_mini_run(pid, config, meta=None):
        FakeDb.calls["create_mini_run"] += 1
        await FakeDb._nap()
        return 99

    @staticmethod
    async def get_run(rid):
        await FakeDb._nap()
        return {
            "id": rid, "mini_of": None, "sn": "TEST-SN",
            "operator": "工人", "batch": "B1",
            "config": {"tests": {"cpu": {"enabled": True, "duration_sec": 1}},
                       "project_root": "/tmp"},
        }

    @staticmethod
    async def get_result_attempts(rid):
        await FakeDb._nap()
        return {}

    @staticmethod
    async def upsert_result(*a, **k): pass

    @staticmethod
    async def finish_run(*a, **k): pass

    @staticmethod
    async def set_pdf_path(*a, **k): pass


R.Database = FakeDb
R.Config.get = staticmethod(lambda *a, **k: {})
R.Config.all = staticmethod(lambda: {})


def _new_runner():
    """造一个 runner, 并把「真正跑测试」那段换成一个睡大觉的假货。"""
    r = R.TestRunner()

    async def _fake_run_all(config):
        await asyncio.sleep(30)

    async def _fake_run_tests(enabled, config, write_to_parent=False):
        await asyncio.sleep(30)

    r._run_all = _fake_run_all
    r._run_tests = _fake_run_tests
    return r


async def _cleanup(r):
    """把 runner 停掉(它内部有个 sleep 30 的任务)。"""
    t = getattr(r, "_task", None)
    if t is not None:
        t.cancel()
        try:
            await t
        except BaseException:
            pass


async def main():
    # ─────────────────────────────────────────────────────────
    section("① 两个 start() 同时来 ⇒ 只有一个开跑")
    # ─────────────────────────────────────────────────────────
    FakeDb.calls["create_run"] = 0
    r = _new_runner()
    res = await asyncio.gather(r.start({"tests": {}}), r.start({"tests": {}}))
    ok = [x for x in res if x.get("ok")]
    bad = [x for x in res if not x.get("ok")]
    check(len(ok) == 1, "只有一个真开跑了", res)
    check(len(bad) == 1, "另一个被闸门挡住了", res)
    check(bad and bad[0].get("error") == "已有测试在运行",
          "挡住的理由是「已有测试在运行」", bad)
    check(FakeDb.calls["create_run"] == 1,
          "数据库只建了一条轮次记录(没建两条)", FakeDb.calls)
    await _cleanup(r)

    # ─────────────────────────────────────────────────────────
    section("② 两个 start_mini() 同时来 ⇒ 只有一个开跑")
    # ─────────────────────────────────────────────────────────
    FakeDb.calls["create_mini_run"] = 0
    r = _new_runner()
    res = await asyncio.gather(
        r.start_mini(5, ["cpu"]),
        r.start_mini(5, ["cpu"]),
    )
    ok = [x for x in res if x.get("ok")]
    bad = [x for x in res if not x.get("ok")]
    check(len(ok) == 1, "只有一个真开跑了", res)
    check(len(bad) == 1, "另一个被闸门挡住了", res)
    check(bad and bad[0].get("error") == "已有测试在运行",
          "挡住的理由是「已有测试在运行」", bad)
    check(FakeDb.calls["create_mini_run"] == 1,
          "只建了一条 mini 轮次记录(没建两条)", FakeDb.calls)
    await _cleanup(r)

    # ─────────────────────────────────────────────────────────
    section("③ start() 和 start_mini() 撞在一起 ⇒ 也只有一个")
    # ─────────────────────────────────────────────────────────
    # ⚠️ **两种顺序都要试** —— 谁先谁后, 撞车的成因不一样:
    #   · 满轮先: 满轮**同步**就把 status 设成 "running" ⇒ 重测被 status 挡;
    #   · 重测先: 重测在「取父轮配置」那里 await ⇒ status 还是旧的 ⇒
    #     只能靠**占位**(`_starting`)挡 ⇒ 占位要是漏了, 这里就撞车。
    for label, first, second in (
            ("满轮先", lambda r: r.start({"tests": {}}),
             lambda r: r.start_mini(5, ["cpu"])),
            ("重测先", lambda r: r.start_mini(5, ["cpu"]),
             lambda r: r.start({"tests": {}})),
    ):
        FakeDb.calls["create_run"] = 0
        FakeDb.calls["create_mini_run"] = 0
        r = _new_runner()
        res = await asyncio.gather(first(r), second(r))
        ok = [x for x in res if x.get("ok")]
        check(len(ok) == 1, "只有一个开跑了(%s)" % label, res)
        check(FakeDb.calls["create_run"] + FakeDb.calls["create_mini_run"] == 1,
              "只建了一条轮次记录(%s)" % label,
              (FakeDb.calls["create_run"], FakeDb.calls["create_mini_run"]))
        await _cleanup(r)

    # ─────────────────────────────────────────────────────────
    section("④ 开跑**炸了** ⇒ 占位要放掉(不能永久卡死)")
    # ─────────────────────────────────────────────────────────
    class BoomDb(FakeDb):
        @staticmethod
        async def create_run(config, meta=None):
            raise RuntimeError("建轮次记录就炸了(测试这一下)")

    R.Database = BoomDb
    r = _new_runner()
    try:
        await r.start({"tests": {}})
        check(False, "开跑抛异常了(测试这一下)", "没抛")
    except RuntimeError:
        check(True, "开跑抛异常了(测试这一下)")
    check(r._starting is False, "占位放掉了(没卡在「正在开跑」)", r._starting)
    R.Database = FakeDb

    # 之后还能再开跑
    r2 = _new_runner()
    res = await r2.start({"tests": {}})
    check(res.get("ok") is True, "炸过之后还能正常开跑", res)
    await _cleanup(r2)

    # ─────────────────────────────────────────────────────────
    section("⑤ 一轮跑着的时候再来一次 ⇒ 照旧挡住(老规矩没丢)")
    # ─────────────────────────────────────────────────────────
    r = _new_runner()
    res1 = await r.start({"tests": {}})
    check(res1.get("ok") is True, "第一轮开跑了", res1)
    res2 = await r.start({"tests": {}})
    check(res2.get("ok") is False and res2.get("error") == "已有测试在运行",
          "跑着的时候再来一次 ⇒ 挡住", res2)
    await _cleanup(r)

    # ─────────────────────────────────────────────────────────
    section("⑥ 上一轮**跑完了** ⇒ 还能再开下一轮(占位不能不放)")
    # ─────────────────────────────────────────────────────────
    r = _new_runner()
    res1 = await r.start_mini(5, ["cpu"])
    check(res1.get("ok") is True, "先跑一轮重测", res1)
    await _cleanup(r)
    # 模拟「这一轮跑完了」(收尾把 status 改成 passed)
    r.status = "passed"
    res2 = await r.start({"tests": {}})
    check(res2.get("ok") is True,
          "跑完之后还能再开下一轮(占位已经放掉了)", res2)
    await _cleanup(r)

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
