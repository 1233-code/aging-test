"""回归测试: 服务重启时收尾「上一轮中途夭折」的轮次 (§9.3)。

起因(2026-10-28 产线事故的尾巴): 服务在跑 TF 卡测试的途中**冻死**了
(设备对读写没应答 → 收尾时在事件循环里同步摸卡 → 整个事件循环冻结 →
服务从此不响应任何请求)。那之后:

    · 进程早没了(拔卡复位之后内核才让它们退出);
    · 但那一轮在数据库里的状态**永远停在 `running`** ——
      历史记录上一直显示「▶ 运行中」, 明明什么都没在跑。

根因: 服务**没有**任何「清理残留 running 轮」的逻辑。`fix_stale_running`
只处理**逐项**结果, 而且只在「重测合并」那一步被调用(§7.7)——
**服务自己死在半路**这条路压根没人管。

这个测试守的:
  ① 残留的 running 轮 → 轮级判 **stopped**(没跑完 = 没结论, §9.3),
     逐项也在跑的那几项 → **stopped**, 并写明「服务重启, 本项没跑完」;
  ② **不冤枉被测设备**: 这一轮里已经判 failed/error 的项**算数** ——
     轮级仍是 failed(工具自己的死不该抹掉板子真不合格的结论);
  ③ mini-run(重测轮)被中断时, **父轮**里对应的行也停在 running
     (mini-run 开跑时会把父轮对应项先写成 running) ⇒ 父轮也要一起收,
     而且**不能**被重算成 **passed**(假通过! 没测过的项合不合格谁也不知道);
  ④ 幂等: 再收一次不会把已经收好的轮次再改一遍;
  ⑤ **没被中断**的轮次(passed / failed / stopped)一个都不许动;
  ⑥ 逐项都跑完了、只是轮级状态没写(服务死在最后一刻)→ 按 §9.2 判,
     **不**当成「没跑完」;
  ⑦ `list_interrupted_runs()` **只读** —— 调它不改任何东西。

用**真库**(临时 SQLite 文件), 临时文件建在系统临时目录 ——
**不碰** reports/history.db, 也**不碰** reports/pdf/。

跑法: .venv/bin/python -m tests.test_interrupted_run_reconcile
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import db as DB
from server.db import Database
from tests._tmp import mkdtemp

# ── 数据目录换成临时目录 ──
# 这个用例会走**真正的启动流程**(main.startup()) —— 启动时会**打开时钟锚点
# 的维护**(§9.3.4), 锚点写在数据目录的 logs/ 下。不换目录的话, 跑一次回归
# 就在**仓库里**落一个 logs/clock_anchor.json —— 仓库不是存放运行时数据
# 的地方(实测就是这个用例干的)。
DATA_DIR = mkdtemp(prefix="aging-reconcile-")
os.environ["AGING_DATA_DIR"] = DATA_DIR

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


CFG = {"tests": {"fx_a": {"enabled": True}, "fx_b": {"enabled": True}}}


async def make_run(names_status, status="running", mini_of=None, config=None):
    """建一轮: names_status = {项名: 状态}。"""
    cfg = config or CFG
    if mini_of is None:
        rid = await Database.create_run(cfg, {"sn": "TESTSN0001"})
    else:
        rid = await Database.create_mini_run(mini_of, cfg, {"sn": "TESTSN0001"})
    for name, st in names_status.items():
        await Database.upsert_result(rid, name, status=st)
    if status != "running":
        await Database.finish_run(rid, status, {"total": 0})
    return rid


async def get_row(rid):
    async with DB.aiosqlite.connect(DB.DB_PATH) as db:
        cur = await db.execute(
            "SELECT status, summary_json, error FROM runs WHERE id=?", (rid,))
        return await cur.fetchone()


async def get_item(rid, name):
    async with DB.aiosqlite.connect(DB.DB_PATH) as db:
        cur = await db.execute(
            "SELECT status, error FROM results WHERE run_id=? AND test_name=?",
            (rid, name))
        return await cur.fetchone()


# ══════════════════════════════════════════════════════════════════════
section("【1】残留的 running 轮 → 轮级 stopped, 逐项也在跑的 → stopped")


async def case1():
    rid = await make_run({"fx_a": "passed", "fx_b": "running"})
    fixed = await Database.reconcile_interrupted_runs()
    ids = [f["id"] for f in fixed]
    check(rid in ids, "收尾时认出了那一轮", ids)

    st, summary_json, err = await get_row(rid)
    check(st == "stopped",
          "轮级判 **stopped**(没跑完 = 没结论, §9.3 —— 不能判 passed)", st)

    item_st, item_err = await get_item(rid, "fx_b")
    check(item_st == "stopped", "还在跑的那一项 → stopped", item_st)
    check("服务重启" in (item_err or ""),
          "逐项的说明写明是「服务重启」导致没跑完", item_err)

    item_st2, _ = await get_item(rid, "fx_a")
    check(item_st2 == "passed", "已经跑完的那一项**不受影响**", item_st2)

    check("夭折" in (err or "") or "中途" in (err or ""),
          "轮级也留了一句说明(操作员看得懂为什么这轮没结论)", err)


section("【2】这一轮里已经判 failed 的项 → 轮级仍是 failed(不冤枉板子)")


async def case2():
    # 三项: 一个通过、一个**已经判失败**、一个还在跑(服务死在这时候)。
    # 「有 failed 的项」和「有没跑到的项」**同时**存在 —— 最容易判错的那种组合:
    # 收尾的代码要是图省事写成「只要有没跑到的就判 stopped」, 板子真不合格
    # 的结论就被抹掉了(那等于**放行坏板子**)。
    cfg = {"tests": {"fx_a": {"enabled": True}, "fx_b": {"enabled": True},
                     "fx_c": {"enabled": True}}}
    rid = await make_run({"fx_a": "passed", "fx_b": "failed", "fx_c": "running"},
                         config=cfg)
    await Database.reconcile_interrupted_runs()
    st, _, _ = await get_row(rid)
    check(st == "failed",
          "已经出结论的失败项**算数** —— 轮级判 failed, 不被工具的死抹成 stopped",
          st)

    # 只有 failed、没有没跑到的项时也一样
    rid2 = await make_run({"fx_a": "passed", "fx_b": "failed"})
    await Database.reconcile_interrupted_runs()
    st2, _, _ = await get_row(rid2)
    check(st2 == "failed", "逐项都判完了的失败轮 → failed", st2)


section("【3】mini-run 被中断 → 父轮里那一行也收, 且**不能**被重算成 passed")


async def case3():
    # 父轮那一项本来**没有结论**(上一轮中途没跑到) ⇒ 重测轮开跑时会把
    # 它写成 running ⇒ 服务夭折后它卡在 running ⇒ 收尾时父轮**这一行**
    # 也要收(否则它在 §9.2 的任何一档里都不算, 父轮的计数凭空少一项)。
    parent = await make_run({"fx_a": "passed", "fx_b": "stopped"},
                            status="stopped")
    # 重测轮开跑: 把父轮对应项写成 running, 自己那轮也是 running
    await Database.upsert_result(parent, "fx_b", status="running")
    mini = await make_run({"fx_b": "running"}, mini_of=parent)

    await Database.reconcile_interrupted_runs()

    m_st, _, _ = await get_row(mini)
    check(m_st == "stopped", "被中断的重测轮 → stopped", m_st)

    p_item_st, p_item_err = await get_item(parent, "fx_b")
    check(p_item_st == "stopped",
          "父轮里那一项(被重测轮写成 running 的)也收尾了", p_item_st)
    check("服务重启" in (p_item_err or ""),
          "父轮那一项的说明也写明是服务重启", p_item_err)

    p_st, _, _ = await get_row(parent)
    check(p_st != "passed",
          "父轮**不能**被判成 passed —— 没跑完的项合不合格谁也不知道 "
          "(§9.3: 假通过比假失败更坑)", p_st)
    check(p_st == "stopped",
          "父轮判 stopped —— 它自己的结论原样保留(见第 9 节)", p_st)


section("【4】幂等: 再收一次不会把已经收好的轮次再改一遍")


async def case4():
    rid = await make_run({"fx_a": "passed", "fx_b": "running"})
    await Database.reconcile_interrupted_runs()
    first = (await get_row(rid))[0]
    again = await Database.reconcile_interrupted_runs()
    second = (await get_row(rid))[0]
    check(not any(f["id"] == rid for f in again),
          "第二次没有再收到这一轮(已经不在 running 里了)", again)
    check(first == second, "已经收好的轮次状态没被改动", (first, second))


section("【5】没被中断的轮次(passed / failed / stopped)一个都不许动")


async def case5():
    keep = []
    for st in ("passed", "failed", "stopped", "skipped", "error"):
        rid = await make_run({"fx_a": "passed"}, status=st)
        keep.append((rid, st))
    await Database.reconcile_interrupted_runs()
    for rid, st in keep:
        now = (await get_row(rid))[0]
        check(now == st, f"status={st} 的轮次不受收尾影响", (st, now))


section("【6】逐项都跑完了、只是轮级状态没写 → 按 §9.2 判, 不当成没跑完")


async def case6():
    # 勾了两项, 两项都跑完了 —— 只是服务死在写轮级状态之前那一瞬
    rid = await make_run({"fx_a": "passed", "fx_b": "passed"}, status="running")
    await Database.reconcile_interrupted_runs()
    st, _, _ = await get_row(rid)
    check(st == "passed",
          "这一轮其实跑完了(逐项都有结论), 轮级按 §9.2 判通过", st)


section("【7】list_interrupted_runs 只读 —— 调它不改任何东西")


async def case7():
    rid = await make_run({"fx_a": "running"})
    rows = await Database.list_interrupted_runs()
    check(any(r["id"] == rid for r in rows), "列得出卡在 running 的那一轮", rows)
    st, _, _ = await get_row(rid)
    check(st == "running", "**只读** —— 列一遍不会顺手把它改掉", st)

    await Database.reconcile_interrupted_runs()
    after = await Database.list_interrupted_runs()
    check(not any(r["id"] == rid for r in after),
          "收完之后就列不出来了", after)

    st, _, _ = await get_row(rid)
    check(st == "stopped",
          "一个结论都没出的那一轮判 **stopped**(没跑完), 不是 skipped "
          "(「跳过」是用户主动跳的, 这里的原因是服务死了)", st)


section("【8】服务启动钩子真的调了收尾(接线没漏)")


# 「上一次启动的日志」的两个样本 ——
#   ① 板子死在**我们自己的测试**里(尾巴里有 stress-ng) ⇒ 该判 failed;
#   ② 板子死在**别的地方**(只有厂商看门狗在跑) ⇒ 不该赖到板子头上。
_EV_BY_OUR_TEST = (
    "[上一次启动的 journal]\n"
    "【上一次是硬复位】上一次启动的日志里**没有**关机序列 ⇒ …\n"
    "Nov 02 11:47:55 ubuntu2004 stress-ng[23114]: memory (MB): total 3902.11\n"
    "Nov 02 11:47:55 ubuntu2004 sudo[23108]:     User : TTY=unknown ; "
    "PWD=/usr/lib/aging-test ; USER=root ; "
    "COMMAND=/usr/bin/timeout -k 5.0 35.0 /usr/bin/stress-ng --cpu 4 "
    "--timeout 5.0s --metrics-brief\n")
_EV_ELSEWHERE = (
    "[上一次启动的 journal]\n"
    "【上一次是硬复位】上一次启动的日志里**没有**关机序列 ⇒ …\n"
    "Oct 30 18:01:01 ubuntu2004 watchdog.sh[275235]: [anfuhua] watchdog "
    "check complete\n"
    "Oct 30 18:01:01 ubuntu2004 systemd[1]: Finished Anfuhua service "
    "watchdog check.\n")


async def _startup_with_evidence(text, cpu_evidence=None):
    """把「上一次启动的日志」换成指定内容, 走**真正的启动流程**。

    为什么要**换掉**而不是直接读真日志: 这一台开发机上也有 journal,
    内容跟产线无关 —— 直接读, 断言就变成「这台机器上恰好发生了什么」,
    换掉才测的是**接线**: 「取日志 → 判软硬 → 判是不是我们的测试 →
    收尾」这一条链有没有接上。

    `cpu_evidence` —— 给这一轮写一份「加压现场记录」文件的内容
    (None = 不写 ⇒ 现场上压根没有这份记录)。
    """
    from server import boot_evidence as BE
    from server import main as M
    orig = BE.previous_boot_evidence

    async def fake(max_lines=3000, max_chars=8000):
        return text
    BE.previous_boot_evidence = fake
    try:
        rid = await make_run({"fx_a": "passed", "fx_b": "running"})
        if cpu_evidence is not None:
            p = BE.cpu_evidence_path(rid)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(cpu_evidence, encoding="utf-8")
        await M.startup()
        return rid
    finally:
        BE.previous_boot_evidence = orig


async def case8():
    """走**真正的启动流程**(main.startup())—— 接线漏了就白写了。

    ⚠️ 启动钩子里还有别的动作(Config.load / 读 SN / 起 idle watchdog)——
    都在临时库上跑, 不碰生产库; 读 SN 是只读的。
    """
    # ① 拿不到日志(读不到 / 没持久化)⇒ 老口径: stopped
    rid = await _startup_with_evidence("")
    st, _, _ = await get_row(rid)
    check(st == "stopped",
          "服务一启动, 卡在「运行中」的那一轮就被收尾了", st)

    # ② 上一次是硬复位, 而且死的时候**我们的测试正在上面跑** ⇒ failed
    rid2 = await _startup_with_evidence(_EV_BY_OUR_TEST)
    st2, _, err2 = await get_row(rid2)
    check(st2 == "failed",
          "板子死在我们的测试里 ⇒ 那一轮判 failed(被测设备不合格)", st2)
    check("被测设备不合格" in (err2 or ""),
          "轮级写明「被测设备不合格」", err2)

    # ③ 上一次是硬复位, 但尾巴里**看不见**我们的测试 ⇒ 不赖到板子头上
    rid3 = await _startup_with_evidence(_EV_ELSEWHERE)
    st3, _, _ = await get_row(rid3)
    check(st3 == "stopped",
          "硬复位但死的时候不是我们的测试 ⇒ 照旧 stopped(不冤枉板子)", st3)


section("【9】父轮的轮级结论(status / error / 汇总)**一个字都不许动**")


async def case9():
    # 收尾的**授权范围**写得很死(用户点名): 「只处理 status='running' 的
    # 轮次, 以及它们**父轮里同样卡在 running 的那一行**」——
    #   · 父轮**只**收那一行;
    #   · 父轮的轮级 status / error / 汇总是**父轮自己的**, 不许被顺手改掉
    #     (「不碰已判定的轮次」—— 重测只能从跑完的轮次发起, 父轮多半已判定)。
    #
    # 以前父轮跟夭折的那一轮走**同一条路**(整轮收尾)⇒ 父轮被盖上
    # 「服务重启, 这一轮中途夭折」—— **张冠李戴**(夭折的是重测轮, 父轮自己
    # 好好地跑完了); 父轮原来那条 error(整轮异常终止的原因)当场蒸发。
    parent = await make_run({"fx_a": "passed", "fx_b": "failed"},
                            status="failed")
    # 给父轮记一条「它自己的」轮级错误(模拟这一轮整轮异常终止过)
    async with DB.aiosqlite.connect(DB.DB_PATH) as db:
        await db.execute("UPDATE runs SET error=? WHERE id=?",
                         ("异常: RuntimeError('炸了')", parent))
        await db.commit()
    before = await get_row(parent)

    # 重测轮开跑: 把父轮里**没有结论**的那一项写成 running, 自己那轮也 running
    await Database.upsert_result(parent, "fx_a", status="running")
    mini = await make_run({"fx_a": "running"}, mini_of=parent)

    await Database.reconcile_interrupted_runs()

    # ── 夭折的是**重测轮**: 它自己照常整轮收尾 ──
    m_st, _, m_err = await get_row(mini)
    check(m_st == "stopped", "夭折的重测轮 → 轮级 stopped", m_st)
    check("夭折" in (m_err or "") or "重启" in (m_err or ""),
          "重测轮自己记着「服务重启, 这一轮中途夭折」", m_err)

    # ── 父轮: 只收那一行, 轮级结论不动 ──
    p_item_st, _ = await get_item(parent, "fx_a")
    check(p_item_st == "stopped",
          "父轮里卡在 running 的那一行被收成 stopped(授权范围里的第二条)",
          p_item_st)

    after = await get_row(parent)
    check(after[0] == before[0],
          "父轮的轮级 status **原样保留**(不碰已判定的轮次)",
          (before[0], after[0]))
    check(after[2] == before[2],
          "父轮自己那条 error **原样保留**(不许被工具的死抹掉)",
          (before[2], after[2]))
    check(after[1] == before[1],
          "父轮的汇总也原样保留(收尾只收那一行)", (before[1], after[1]))


section("【10】板子硬复位(上一次启动的日志里**没有关机序列**)→ 那一项判 failed")


async def case10():
    """硬复位 ⇒ 正在跑的那一项判 **failed**(被测设备不合格), 不是 stopped。

    2026-11-02 改的: 板子死在 CPU 加压里(硬复位), 报告却写「服务重启,
    这一轮中途夭折(没跑完) —— **不代表被测设备不合格**」⇒ **方向是反的**:
    等于放行一块扛不住加压的板子。
    """
    rid = await make_run({"fx_a": "passed", "fx_b": "running"})
    await Database.reconcile_interrupted_runs(hard_reset=True)
    st, _, err = await get_row(rid)
    check(st == "failed",
          "轮级判 failed(那一项 failed ⇒ 按 §9.2 整轮 failed)", st)
    item_st, item_err = await get_item(rid, "fx_b")
    check(item_st == "failed", "还在跑的那一项 → failed(不是 stopped)", item_st)
    check("硬复位" in (item_err or ""),
          "逐项写明是**硬复位**(不是「服务重启」)", item_err)
    check("被测设备不合格" in (item_err or ""),
          "逐项写明「被测设备不合格」", item_err)
    check("被测设备不合格" in (err or ""),
          "轮级也写明「被测设备不合格」", err)
    check("不代表" not in (err or ""),
          "轮级**没有**那句「不代表被测设备不合格」(那是软重启才有的说法)",
          err)
    check("硬复位" in (err or ""),
          "轮级写明是硬复位", err)
    item_st2, _ = await get_item(rid, "fx_a")
    check(item_st2 == "passed", "已经跑完的那一项**不受影响**", item_st2)


section("【11】判不出硬复位(默认)→ 仍旧 stopped(老口径一点没变)")


async def case11():
    rid = await make_run({"fx_a": "passed", "fx_b": "running"})
    await Database.reconcile_interrupted_runs()          # 不传 hard_reset
    st, _, err = await get_row(rid)
    check(st == "stopped",
          "默认还是 stopped(拿不到硬复位的证据, 就不赖到板子头上)", st)
    item_st, item_err = await get_item(rid, "fx_b")
    check(item_st == "stopped", "逐项还是 stopped", item_st)
    check("服务重启" in (item_err or ""),
          "逐项说的是「服务重启, 本项没跑完」", item_err)
    check("不代表被测设备不合格" in (err or ""),
          "轮级说的是「不代表被测设备不合格」(跟硬复位那句不是同一句)", err)


section("【12】硬复位时, **父轮**那一行仍然只收成 stopped(不判 failed)")


async def case12():
    # 硬复位打死的**不是父轮**, 是那个重测轮 ⇒ 父轮里被重测轮写成 running
    # 的那一行, 只能收成 stopped(不能收成 failed —— 那是**张冠李戴**:
    # 父轮自己好好地跑完了, 它的结论不能被重测轮的死亡改掉)。
    parent = await make_run({"fx_a": "passed", "fx_b": "failed"}, status="failed")
    await Database.upsert_result(parent, "fx_a", status="running")
    mini = await make_run({"fx_a": "running"}, mini_of=parent)

    await Database.reconcile_interrupted_runs(hard_reset=True)

    m_st, _, m_err = await get_row(mini)
    check(m_st == "failed",
          "夭折的重测轮 → failed(板子把它硬复位了)", m_st)
    check("被测设备不合格" in (m_err or ""),
          "重测轮写明「被测设备不合格」", m_err)

    p_item_st, p_item_err = await get_item(parent, "fx_a")
    check(p_item_st == "stopped",
          "父轮里那一行收成 stopped(不判 failed —— 硬复位打死的不是父轮)",
          p_item_st)
    check("服务重启" in (p_item_err or ""),
          "父轮那一项的说法还是「服务重启」", p_item_err)

    p_st, _, _ = await get_row(parent)
    check(p_st == "failed",
          "父轮的轮级结论**原样保留**(它自己就判过 failed)", p_st)


section("【13】journal 尾巴认不出我们的测试 ⇒ 换「加压现场记录」这条通道")


# ── 「加压现场记录」的几份样本 ──
#
# 现场情形(报告 A10000000000010_20261104_140033.pdf): 板子死在 CPU 项
# **起手约 2 秒**, 硬复位 ⇒ journald 最后几秒**没落盘** ⇒ 尾巴里连一条
# stress-ng 都没有 ⇒ 照旧 stopped ⇒ **不计入通过率分母** ⇒ 一块扛不住
# 加压的板子, 只要其它项都过, 报告上看着像**合格**的 ⇒ 产线会放行它。
# ⇒ 改看这份**逐行 fsync 落盘**的文件(硬复位带不走)。
#
# ⚠️ 认的是「起跑 **」(**带空格**): 文件**头**里那行是
# 「# 起跑时板子背景负载: …」—— 那是「起跑**时**」(加压**之前**量的
# 背景负载), 不是「加压起跑」⇒ 认错会把「压根没加压」当成「起手了」。
_EV_HEAD = (
    "# 本轮加压峰值: 6 线程(连续加压, 一个进程跑满整轮; 每一行自带 "
    "threads= 是当时那一片的值)\n"
    "# 起跑时板子背景负载: 16.6% (≈1.3 核)\n")
_EV_LINE_START = (
    "t=+0.2s threads=6 hot=60.9 cpul=60.9 cpub=59.4 ddr=51.8 gpu=47.2 "
    "npu=44.9 freq=1344MHz 起跑 6 线程\n")
_EV_LINE_SAMPLE = (
    "t=+2.2s threads=6 hot=61.4 cpul=61.3 cpub=59.9 ddr=52.1 gpu=47.6 "
    "npu=45.1 freq=1800MHz\n")
_EV_LINE_DONE = (
    "t=+95.4s threads=6 hot=88.1 cpul=86.2 cpub=83.7 ddr=61.0 gpu=52.3 "
    "npu=47.9 freq=1800MHz 加压结束\n")

#: 加压**起手了、没跑完** ⇒ 板子死在加压途中
_EV_CUT_SHORT = _EV_HEAD + _EV_LINE_START + _EV_LINE_SAMPLE
#: 加压**跑完了** ⇒ 板子不是死在加压里
_EV_FINISHED = _EV_HEAD + _EV_LINE_START + _EV_LINE_SAMPLE + _EV_LINE_DONE
#: 上一片跑完了, 死在**下一片**起手 ⇒ 看**最后一个**标记(是「起跑」)
_EV_NEXT_SLICE = (_EV_HEAD + _EV_LINE_START + _EV_LINE_DONE
                  + _EV_LINE_START)
#: 只有文件头(压根没加压) ⇒ **不算**「起手了」
_EV_HEADER_ONLY = _EV_HEAD


async def case13():
    """journal 尾巴认不出来时, 改看「加压现场记录」(2026-11-04 补的第二条通道)。

    背景: 板子死在 CPU 加压**起手约 2 秒** ⇒ 硬复位 ⇒ journald 那几秒**没
    落盘** ⇒ 尾巴里只有上一项的 `sudo … ip link show wlan0` 和厂商看门狗
    ⇒ `our_run_at_death` = False ⇒ 这一项判 `stopped` ⇒ **不计入通过率
    分母** ⇒ **一块扛不住加压的板子会被放行**(方向反了)。
    """
    # ① 硬复位 + 尾巴看不见我们的测试 + 现场记录显示「起手了、没跑完」
    rid = await _startup_with_evidence(_EV_ELSEWHERE, _EV_CUT_SHORT)
    st, _, err = await get_row(rid)
    check(st == "failed",
          "板子死在加压途中(现场记录: 起跑、没有加压结束)⇒ 那一轮判 failed", st)
    check("被测设备不合格" in (err or ""),
          "轮级写明「被测设备不合格」", err)
    item_st, item_err = await get_item(rid, "fx_b")
    check(item_st == "failed",
          "正在跑的那一项 → failed(不是 stopped ⇒ 计入通过率分母)", item_st)
    check("被测设备不合格" in (item_err or ""),
          "逐项写明「被测设备不合格」", item_err)

    # ② 加压**跑完了** ⇒ 不是死在加压里 ⇒ 不赖到板子头上
    rid2 = await _startup_with_evidence(_EV_ELSEWHERE, _EV_FINISHED)
    st2, _, _ = await get_row(rid2)
    check(st2 == "stopped",
          "现场记录里最后是「加压结束」⇒ 照旧 stopped(不冤枉板子)", st2)

    # ③ 压根没有这份记录 ⇒ 老口径一点没变
    rid3 = await _startup_with_evidence(_EV_ELSEWHERE)
    st3, _, _ = await get_row(rid3)
    check(st3 == "stopped",
          "没有「加压现场记录」⇒ 照旧 stopped(没证据不编)", st3)

    # ④ 跑完一片又死在下一片起手 ⇒ 看**最后一个**标记
    rid4 = await _startup_with_evidence(_EV_ELSEWHERE, _EV_NEXT_SLICE)
    st4, _, _ = await get_row(rid4)
    check(st4 == "failed",
          "上一个标记是「起跑」(上一片跑完了, 死在下一片)⇒ failed", st4)

    # ⑤ 文件头那行「起跑**时**板子背景负载」**不是**「加压起跑」
    rid5 = await _startup_with_evidence(_EV_ELSEWHERE, _EV_HEADER_ONLY)
    st5, _, _ = await get_row(rid5)
    check(st5 == "stopped",
          "只有文件头那行「起跑时板子背景负载」⇒ 不算起手 ⇒ stopped", st5)


async def main():
    tmp = mkdtemp(prefix="aging_reconcile_")
    DB.DB_PATH = Path(tmp) / "history.db"
    await Database.init()

    for c in (case1, case2, case3, case4, case5, case6, case7, case8, case9,
              case10, case11, case12, case13):
        await c()

    # 启动钩子起了 idle watchdog —— 事件循环关掉前把它取消掉, 免得刷一屏
    # "Task was destroyed but it is pending"。⚠️ **不能**把当前这个 task 也
    # 取消掉 —— 那会把整个测试自己取消掉(退出码 1, 明明全过却像失败)。
    cur = asyncio.current_task()
    for t in asyncio.all_tasks():
        if t is not cur:
            t.cancel()

    print("\n" + ("全部通过" if not FAILS else f"有失败项 ({len(FAILS)})"))
    for f in FAILS:
        print("  ✗ " + f)
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
