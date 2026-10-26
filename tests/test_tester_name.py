"""§8.2 回归测试: 「本轮测试人员」这条链, 从前端输入框一路走到报告头。

这个功能出过一类**特别难查**的 bug: 工人明明在界面上填了姓名, 报告头
却写着「未填写」, 而且**日志上一点异常都没有** —— 因为代码走的是
「取不到就用默认值」那条静默兜底路, 从来没报错。所以这里测的不是
「有没有这个字段」, 而是**优先级到底按什么顺序生效**:

    前端手填  >  config.yaml 的 report.operator  >  报告头「未填写」

各环节分别测:
  ① 后端合并: `test_start()` 把手填的 tester_name 写进本轮 config
  ② 合并时机: 必须在 `config.update()` **之后**, 否则被静默冲掉
  ③ runner 认账: `start()` 读的是**本轮 config**, 不是全局 yaml
  ④ 落库:     meta.operator 是这一轮的名字
  ⑤ 重测轮:   mini-run 沿用父轮的测试人员
  ⑥ 报告头:   印出来的确实是这一轮的那个名字
  ⑦ 空名字:   **不拦停**, 照样起跑(产线不能因为没人写名字就做不出判定)

只把 Database / Config 换成内存里的假实现, **不碰真库、不碰真硬件、
不往生产归档目录 reports/pdf/ 里写任何东西**。
跑法: .venv/bin/python -m tests.test_tester_name
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import runner as R

# ══════════════════════════════════════════════════════════════
# 假实现: 全局 yaml 里的 report.operator 固定是「配置里的人」
# ══════════════════════════════════════════════════════════════
GLOBAL_OPERATOR = "配置里的人"
PARENT_OPERATOR = "父轮的人"

# 全局 yaml 的假内容。Config.get 是**按 key 取**的, 所以这里得像个查表,
# 不能一律返回 {} —— 那样 /api/config 就取不到 report.operator 了。
FAKE_YAML = {
    "report": {"operator": GLOBAL_OPERATOR, "batch": "B-1"},
    "defaults": {},
    "interfaces": {},
}
R.Config.get = staticmethod(lambda key, default=None: FAKE_YAML.get(key, default))
# ⚠️ 这里必须是**普通函数**, 不能是 staticmethod —— Python 3.8 的模块属性
#    查找不走描述符协议, `R._report_cfg` 会原样拿到 staticmethod 对象,
#    runner 里那个 `_report_cfg()` 直接 TypeError: 'staticmethod' object is
#    not callable。(3.10 之后 staticmethod 才自带 __call__。)
R._report_cfg = lambda: {"operator": GLOBAL_OPERATOR, "batch": ""}


class FakeDb:
    """记下 create_run / create_mini_run 收到的 meta, 供断言用。"""

    last_meta = None
    last_mini_meta = None

    @staticmethod
    async def create_run(config, meta=None):
        FakeDb.last_meta = meta
        return 1

    @staticmethod
    async def create_mini_run(pid, config, meta=None):
        FakeDb.last_mini_meta = meta
        return 99

    @staticmethod
    async def get_run(rid):
        # start_mini() 要拿父轮: 配置 / SN / 测试人员都从这儿来。
        return {
            "id": rid,
            "config": {"tests": {"cpu": {"enabled": True}}, "project_root": ""},
            "sn": "SN-PARENT",
            "operator": PARENT_OPERATOR,
            "batch": "B-1",
        }

    @staticmethod
    async def upsert_result(*a, **k): pass

    @staticmethod
    async def finish_run(*a, **k): pass

    @staticmethod
    async def set_pdf_path(*a, **k): pass


R.Database = FakeDb


# runner 会起后台任务真的跑测试 —— 那是真硬件, 这里一律换成挂着的空任务。
async def _never_run(self, *a, **k):
    await asyncio.sleep(3600)


R.TestRunner._run_all = _never_run
R.TestRunner._run_tests = _never_run
R.TestRunner._build_test_list = lambda self, *a, **k: []


def check(cond, msg):
    print(("    ✓ " if cond else "    ✗ ") + msg)
    return bool(cond)


async def main():
    ok = True

    # ── ① 后端合并: 手填的 tester_name 进到本轮 config ──────────
    print("【1】POST /api/test/start: 手填的 tester_name 要进到本轮 config")
    from server import main as M

    captured = {}

    async def fake_start(config):
        captured["config"] = config
        return {"ok": True, "run_id": 1}

    M.runner.start = fake_start
    M.Config.all = staticmethod(lambda: {"report": {"operator": GLOBAL_OPERATOR,
                                                     "batch": "B-1"},
                                         "tests": {}})

    await M.test_start(M.StartRequest(tests={}, order=None,
                                      tester_name="  手填的人  "))
    rep = captured["config"].get("report") or {}
    print(f"    config['report'] = {rep}")
    ok &= check(rep.get("operator") == "手填的人",
                "手填的姓名进了 config['report']['operator']")
    ok &= check(rep.get("batch") == "B-1",
                "同段的 batch 没被合并顺手冲掉")

    # ── ② 合并时机: 提前写会被 update() 静默冲掉 ───────────────
    print("\n【2】合并必须发生在 config.update() 之后")
    # 复现那个「静默冲掉」的机制: 如果代码在 update() **之前**写
    # config["report"]["operator"], update() 会用 yaml 里的整个 report 段
    # 盖掉它, 于是手填的值消失, 报告里退回全局 yaml 的名字。
    cfg = {"tests": {}}
    cfg["report"] = {"operator": "提前写的"}
    cfg.update({"report": {"operator": GLOBAL_OPERATOR, "batch": "B-1"}})
    print(f"    提前写 → update() 之后 = {cfg['report']}")
    ok &= check(cfg["report"]["operator"] == GLOBAL_OPERATOR,
                "证实: 提前写确实会被静默冲掉(所以顺序不能反)")

    # ── ③ runner 认账 ───────────────────────────────────────────
    print("\n【3】runner.start(): 本轮 config 的值必须压过全局 yaml")
    rn = R.TestRunner()
    await rn.start({"tests": {}, "report": {"operator": "手填的人"}})
    print(f"    runner.operator = {rn.operator!r}")
    ok &= check(rn.operator == "手填的人",
                "手填的姓名被 runner 认下了(没退回全局 yaml 的值)")

    print("\n【4】runner.start(): 本轮没填 → 回落全局 yaml")
    rn = R.TestRunner()
    await rn.start({"tests": {}, "report": {}})
    print(f"    runner.operator = {rn.operator!r}")
    ok &= check(rn.operator == GLOBAL_OPERATOR,
                "没手填时回落到 config.yaml 的 report.operator")

    print("\n【5】runner.start(): 本轮和全局都没有 → 留空(None)")
    saved = R._report_cfg
    R._report_cfg = lambda: {"operator": "", "batch": ""}
    try:
        rn = R.TestRunner()
        await rn.start({"tests": {}, "report": {}})
        print(f"    runner.operator = {rn.operator!r}")
        ok &= check(rn.operator is None,
                    "两处都没值时留 None(报告头印「未填写」)")
    finally:
        R._report_cfg = saved

    # ── ④ 落库: meta 里带的是这一轮的名字 ──────────────────────
    print("\n【6】落库的 meta.operator 得是这一轮的那个名字")
    FakeDb.last_meta = None
    rn = R.TestRunner()
    await rn.start({"tests": {}, "report": {"operator": "手填的人"}})
    print(f"    create_run 的 meta = {FakeDb.last_meta}")
    ok &= check((FakeDb.last_meta or {}).get("operator") == "手填的人",
                "DB 行里记的是手填的姓名")

    # ── ⑤ 重测轮沿用父轮 ────────────────────────────────────────
    print("\n【7】重测轮(mini-run)沿用父轮的测试人员")
    FakeDb.last_mini_meta = None
    rn = R.TestRunner()
    await rn.start_mini(1, ["cpu"])
    print(f"    mini meta = {FakeDb.last_mini_meta}")
    ok &= check((FakeDb.last_mini_meta or {}).get("operator") == PARENT_OPERATOR,
                "重测轮记的是父轮那个人的名字")

    # ── ⑥ 报告头: 印出来的是这一轮的名字 ────────────────────────
    print("\n【8】报告头: 印的是这一轮的名字")
    from server import report as report_mod
    html = report_mod.render_html(
        {"id": 1, "started_at": 0, "finished_at": 1, "status": "passed",
         "sn": "SN-X", "operator": "手填的人", "batch": "B-1",
         "mini_of": None, "error": None,
         "summary": {"total": 1, "passed": 1, "failed": 0, "error": 0,
                     "skipped": 0},
         "config": {"tests": {}}},
        [{"test_name": "hdmi", "status": "passed", "duration_sec": 0.1,
          "metrics": {}, "log": "", "error": None}],
        "t.pdf")
    for line in html.split("\n"):
        if "测试人员" in line:
            print("    " + line.strip()[:100])
    ok &= check("<td>手填的人</td>" in html, "报告头印出了这一轮的名字")
    ok &= check("工位号" not in html, "报告头里没有「工位号」了")

    # 没填名字的那一轮: 占位符必须是「未填写」(不是 `unknown` 也不是 `—`)。
    # ⚠️ 得**另渲染一份**空的来查 —— 上一份是有名字的, 里面根本不会出现
    #    占位符, 拿它断言「没填时是未填写」永远是假的。
    html_empty = report_mod.render_html(
        {"id": 2, "started_at": 0, "finished_at": 1, "status": "passed",
         "sn": "SN-X", "operator": None, "batch": None,
         "mini_of": None, "error": None,
         "summary": {"total": 1, "passed": 1, "failed": 0, "error": 0,
                     "skipped": 0},
         "config": {"tests": {}}},
        [{"test_name": "hdmi", "status": "passed", "duration_sec": 0.1,
          "metrics": {}, "log": "", "error": None}],
        "t.pdf")
    for line in html_empty.split("\n"):
        if "测试人员" in line:
            print("    (没填名字的那一轮) " + line.strip()[:100])
    ok &= check("<td>未填写</td>" in html_empty,
                "没填时占位符是「未填写」")

    # ── ⑦ 空名字不拦停 ──────────────────────────────────────────
    print("\n【9】空名字不拦停: 照样能起跑")
    captured.clear()
    res = await M.test_start(M.StartRequest(tests={}, order=None,
                                            tester_name="   "))
    rep = captured["config"].get("report") or {}
    print(f"    tester_name='   ' → config['report'] = {rep}")
    ok &= check(res.get("ok") is True, "空名字照样起跑, 没被拦下")
    ok &= check(rep.get("operator") == GLOBAL_OPERATOR,
                "空名字回落到全局 yaml 的值(而不是写成空)")

    # ── ⑧ 前端预填: /api/config 要把默认姓名下发下去 ────────────
    print("\n【10】GET /api/config: 要把默认姓名下发给前端预填")
    cfg_api = await M.get_config()
    rep_api = cfg_api.get("report") or {}
    print(f"    /api/config 的 report = {rep_api}")
    ok &= check(rep_api.get("operator") == GLOBAL_OPERATOR,
                "默认姓名随 /api/config 下发(前端拿它预填输入框)")
    ok &= check("operator" in rep_api,
                "report 段里确实有 operator 这个键(前端读的就是它)")

    print("\n" + ("全部通过" if ok else "有失败项"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
