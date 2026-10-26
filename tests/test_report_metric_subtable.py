"""回归测试: §8.2 报告的指标表 —— 「一串字典」的值渲染成**每条一行的子表**。

起因 (2026-10-26): 产线看报告时发现 **GPU 渲染**那一项的 `rounds_detail`
「没跟前面的连接上」—— 它单独占了一页(甚至跨了页)。查下来:
`rounds_detail` 这个指标存的是**整段 glmark2 输出**, 报告把它 repr 成
**一个巨大的单元格**(一行几千字符, 比一页还高)→ 浏览器只能把这行整行推到
下一页, 指标表跟前面的行**断开**。

而且那份内容本来就不该在指标表里:
  · 它是**截断副本**(每轮只留末尾 20 行), 完整的那份在报告末尾的
    「日志附录」里 —— 同一份东西两处, 一处还是截断的;
  · 文档里早有规矩: 「报告里日志只有一处, 残缺副本比没有更糟」。

所以改法有两半:
  ① `server/tests/gpu.py`: 指标里**不存**原始输出, 只存量化字段
     (分数 / 场景数 / 耗时 / 退出码 / 状态);
  ② `server/report.py`: 值是「一串字典」时渲染成**每条一行**的子表,
     整列都是长文本(= 原始输出)的那一列**不印**。

⚠️ 本测试盯的是 **报告渲染层**(纯函数, 不渲染 PDF): 它同时覆盖
**老库里已存的旧数据**(rounds_detail 里还带着 tail 的那种) —— 老 run
重新导出报告时也得是干净的样子。

跑法: .venv/bin/python -m tests.test_report_metric_subtable
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from server import report as report_mod

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


def main():
    mv = report_mod._metric_views

    # ① 普通标量指标 —— 原样, 不能被这套逻辑动到
    views = mv({"score": 147.0, "scenes_completed": 33, "rounds": 1})
    check([v["key"] for v in views] == ["score", "scenes_completed", "rounds"],
          "标量指标的键原样保留", [v["key"] for v in views])
    check(all(v["sub_cols"] is None and v["sub_rows"] is None for v in views),
          "标量指标不生成子表")
    check(views[0]["value"] == 147.0, "标量指标的值没被改写", views[0]["value"])

    # ② 一串字典 → 每条一行的子表
    rounds = [{"round": 1, "score": 147.0, "scenes": 33, "scenes_expected": 33,
               "elapsed_sec": 105.3, "exit_code": 0, "status": "passed"}]
    v = mv({"rounds_detail": rounds})[0]
    check(v["sub_cols"] is not None, "一串字典 → 生成子表", v)
    check(v["sub_cols"] == ["轮次", "分数", "场景数", "应跑场景", "耗时(s)",
                            "退出码", "状态"],
          "子表的列名是给人看的(不是 round/score 这种字段名)", v["sub_cols"])
    check(v["sub_rows"] == [[1, 147.0, 33, 33, 105.3, 0, "passed"]],
          "子表每条一行, 值都对得上", v["sub_rows"])
    check(v["value"] == "(见下表)", "指标行本身写「见下表」", v["value"])

    # ③ **老库已存的旧形状**(带 tail) —— tail 是原始输出, 不许进指标表
    old = [{"round": 1, "score": 147.0, "scenes": 33, "scenes_expected": 33,
            "elapsed_sec": 105.3, "exit_code": 0, "status": "passed",
            "tail": "glmark2 2021.02\n[('build', 'use-vbo=false')] : FPS: 100\n" * 30}]
    v = mv({"rounds_detail": old})[0]
    check(v["sub_cols"] is not None and "tail" not in v["sub_cols"]
          and "原始输出" not in "".join(v["sub_cols"]),
          "老库里的 tail 列被剔掉(原始输出不印在指标表)", v["sub_cols"])
    check(all("glmark2" not in str(c) for row in (v["sub_rows"] or []) for c in row),
          "原始输出的内容一个字都没进子表", v["sub_rows"])

    # ④ 整列都是 None 的 → 空列不印
    with_none = [{"round": 1, "score": 147.0, "error": None}]
    v = mv({"rounds_detail": with_none})[0]
    check("说明" not in (v["sub_cols"] or []), "全 None 的列不印(免得一根空列)",
          v["sub_cols"])

    # ⑤ 每一列都是原始输出 → 什么都不印, 只指路
    all_raw = [{"tail": "x" * 500}, {"tail": "y" * 500}]
    v = mv({"rounds_detail": all_raw})[0]
    check(v["sub_cols"] is None and v["value"] == "(原始输出见日志附录)",
          "整张表都是原始输出时, 不退回 repr(那正是几千字符一个单元格的老路)",
          v)

    # ⑥ 不是「一串字典」的值 → 原样, 别乱套
    v = mv({"plain": "文本", "empty": [], "mixed": [1, "a"], "num": 3})[0]
    check(v["value"] == "文本" and v["sub_cols"] is None, "普通文本值原样")
    v = mv({"empty": []})[0]
    check(v["sub_cols"] is None and v["value"] == [], "空列表不当成子表", v)
    v = mv({"mixed": [1, "a"]})[0]
    check(v["sub_cols"] is None, "混合类型的列表不当成子表", v)

    # ⑦ 空指标
    check(mv({}) == [], "空指标 → 空列表", mv({}))
    check(mv(None) == [], "None 指标 → 空列表", mv(None))

    print("\n" + ("全部通过" if not FAILS else "有失败项 (%d)" % len(FAILS)))
    for f in FAILS:
        print("  ✗ " + f)
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
