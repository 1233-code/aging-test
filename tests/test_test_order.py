"""回归测试: **整轮的执行顺序**(§7.6 推荐执行顺序)

**为什么要有这个测试** (2026-11-04)

    用户把 WiFi 项的位置改了: 「wifi 还是放到 GPU 测试之前, 然后蓝牙放
    wifi 后面」⇒ 四阶段从「轻量 → 接口 → 重载 → 射频」变成
    「**轻量 → 接口 → 射频 → 重载**」。

    ⚠️ 顺序**不是**一个「排版」问题 —— 它同时是:
      · **这一轮真正跑的顺序**(后端按它跑);
      · **页面上那 11 项的显示顺序**(前端用的就是后端下发的这个值);
      · **报告里逐项结果的先后**;
      · **「板子死在第几项」这个结论的落点** —— 2026-10-30 那次
        「WiFi 之后、CPU 之前」的死亡, 就是靠这个顺序才定位到 CPU 项的
        (位置 ≠ 谁干的, 但位置是**找到谁的线索**)。

    ⇒ 顺序**只在一个地方**定义(`server/runner.py` 的 `RECOMMENDED_ORDER`),
      页面/后端/报告都从它派生。本测试把这个「唯一来源」钉死, 并且顺着
      链路**一层一层验**: 常量 → runner 真的按它排 → 注册表清单跟它一致
      → 前端拿到的就是它 → 文档里写的跟它一致。

**这个测试不碰真硬件、不碰生产库**: 只 import 常量、用假的 tests_config
走 `_build_test_list`(纯函数), 以及读几个文本文件。

跑法: .venv/bin/python -m tests.test_test_order
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.runner import RECOMMENDED_ORDER, TestRunner      # noqa: E402
from server.tests.registry import get_all_tests             # noqa: E402

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

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


# ══════════════════════════════════════════════════════════════════════
section("【1】推荐顺序的**完整序列** —— 改顺序必须先改这里")

# ⚠️ 这一整行就是「顺序」本身。**任何一项挪位置, 这行就得改** ——
#    改的是产品决定, 所以要在这里留痕, 不是悄悄在某处调换。
EXPECTED = [
    "memory", "emmc", "tfcard",            # ① 轻量
    "hdmi", "usb", "ethernet", "fan",      # ② 接口与外设
    "wifi", "bluetooth",                  # ③ 射频 (2026-11-04: WiFi 挪到 GPU 之前,
                                          #          蓝牙紧跟 WiFi — §6.4.1)
    "gpu", "cpu",                         # ④ 重载收尾
]

check(list(RECOMMENDED_ORDER) == EXPECTED,
      "推荐顺序 == 轻量 → 接口 → 射频 → 重载", list(RECOMMENDED_ORDER))
check(len(set(RECOMMENDED_ORDER)) == len(RECOMMENDED_ORDER),
      "没有重复项", list(RECOMMENDED_ORDER))

section("【2】WiFi 在 GPU 之前 —— 用户 2026-11-04 定的")


def _pos(name):
    """名字在推荐顺序里的位置; 不在队列里 ⇒ **-1**。

    ⚠️ 故意**不抛异常** —— `.index()` 找不到就抛 ValueError, 整个测试
    直接崩掉 ⇒ 那样「红了」就分不清是**被断言咬住的**还是**自己崩了**
    (变异脚本里那条「红了但一个 ✗ 都没有 = 崩了」的判据就靠这个)。
    """
    seq = list(RECOMMENDED_ORDER)
    return seq.index(name) if name in seq else -1


_i_wifi = _pos("wifi")
_i_bt = _pos("bluetooth")
_i_gpu = _pos("gpu")
_i_cpu = _pos("cpu")

check(_i_wifi >= 0, "WiFi 项在队列里(不许整轮都不测 WiFi)",
      list(RECOMMENDED_ORDER))
check(_i_wifi >= 0 and _i_gpu >= 0 and _i_wifi < _i_gpu,
      "WiFi 排在 GPU **之前**", list(RECOMMENDED_ORDER))
check(_i_wifi >= 0 and _i_cpu >= 0 and _i_wifi < _i_cpu,
      "WiFi 排在 CPU **之前**", list(RECOMMENDED_ORDER))
check(bool(RECOMMENDED_ORDER) and RECOMMENDED_ORDER[-1] == "cpu",
      "整轮最后一项是 CPU 压力", list(RECOMMENDED_ORDER))

# §6.4.1: 蓝牙项**紧跟在 WiFi 后面**(用户原话「蓝牙放 wifi 后面」),
# 且都在重载之前 —— 它是**只读**的, 排在 WiFi 后面才看得到「WiFi 动完射频
# 之后蓝牙还是不是关着的」这个**最终状态**。
check(_i_bt >= 0, "蓝牙项在队列里", list(RECOMMENDED_ORDER))
check(_i_bt >= 0 and _i_wifi >= 0 and _i_bt == _i_wifi + 1,
      "蓝牙项**紧跟在 WiFi 后面**", list(RECOMMENDED_ORDER))
check(_i_bt >= 0 and _i_gpu >= 0 and _i_bt < _i_gpu,
      "蓝牙项排在 GPU **之前**", list(RECOMMENDED_ORDER))
check(_i_bt >= 0 and _i_cpu >= 0 and _i_bt < _i_cpu,
      "蓝牙项排在 CPU **之前**", list(RECOMMENDED_ORDER))

section("【3】阶段规则: 轻量在重载前面(重载项放最后, 避免热累积污染轻量项)")

_light = ["memory", "emmc", "tfcard"]
check(all(_pos(x) >= 0 and _pos(x) < _pos("gpu") for x in _light),
      "三个轻量项都在 GPU 之前", list(RECOMMENDED_ORDER))
check(_pos("gpu") >= 0 and _pos("cpu") >= 0 and _pos("gpu") < _pos("cpu"),
      "GPU 在 CPU 之前", list(RECOMMENDED_ORDER))

section("【4】runner **真的按这个顺序**排队(不是只把常量摆在那儿好看)")

# 全部项都开 ⇒ 排出来的就应该正好是 RECOMMENDED_ORDER。
_cfg = {n: {"enabled": True} for n in RECOMMENDED_ORDER}
_names = [n for n, _ in TestRunner._build_test_list(_cfg)]
check(_names == list(RECOMMENDED_ORDER),
      "不传顺序时, 排出来的就是推荐顺序", _names)

# 前端把 `order` 原样发过来(它拿的就是 recommended_order)⇒ 后端不许再改。
_names2 = [n for n, _ in TestRunner._build_test_list(
    _cfg, explicit_order=list(RECOMMENDED_ORDER))]
check(_names2 == list(RECOMMENDED_ORDER),
      "前端传同一个顺序过来, 结果不变", _names2)

# ⚠️ 少勾两项时, 剩下的**相对先后不能变**(不能因为过滤就把顺序搅乱)。
_cfg2 = {n: {"enabled": n not in ("emmc", "fan")}
         for n in RECOMMENDED_ORDER}
_names3 = [n for n, _ in TestRunner._build_test_list(_cfg2)]
check(_names3 == [n for n in RECOMMENDED_ORDER if n not in ("emmc", "fan")],
      "少勾两项时, 其余项的相对顺序不变", _names3)

section("【5】注册表清单跟推荐顺序一致(API 下发的 `tests` 列表别跟人看的打架)")

_reg = [t["name"] for t in get_all_tests()]
check(_reg == list(RECOMMENDED_ORDER),
      "get_all_tests() 的顺序 == 推荐顺序", _reg)

section("【6】前端拿到的就是这个顺序(后端把它原样下发给页面)")

with open(os.path.join(_REPO, "server", "main.py"), encoding="utf-8") as f:
    _main_src = f.read()
check('"recommended_order": RECOMMENDED_ORDER' in _main_src,
      "/api/config 把 RECOMMENDED_ORDER 原样下发给前端")
check("from .runner import RECOMMENDED_ORDER" in _main_src,
      "/api/config 拿的是 runner 里那一个常量(不是自己另抄一份)")

# 前端**必须**用 recommended_order 排那 11 项 —— 用 data.tests 的原始顺序
# 排, 页面显示的顺序就会跟真正跑的顺序对不上(工人看着屏幕不知道跑到哪了)。
with open(os.path.join(_REPO, "static", "app.js"), encoding="utf-8") as f:
    _app_src = f.read()
check("data.recommended_order && data.recommended_order.length" in _app_src,
      "前端用 recommended_order 决定那 11 项的顺序")
check("testOrder.value = data.recommended_order && data.recommended_order.length"
      in _app_src or
      "testOrder.value = data.recommended_order" in _app_src,
      "页面的 testOrder 取的就是 recommended_order")

section("【7】文档里写的顺序跟代码一致(文档是最容易落下的那一份)")

_docp = os.path.join(_REPO, "docs", "需求设计.md")
if not os.path.exists(_docp):
    print("    (跳过: 找不到 docs/需求设计.md)")
else:
    with open(_docp, encoding="utf-8") as f:
        _doc = f.read()
    check("射频（WiFi + 蓝牙，GPU 之前）" in _doc,
          "§5 的「推荐执行顺序」写的是「射频(WiFi + 蓝牙, GPU 之前)」")
    check("射频（WiFi，最后）" not in _doc,
          "文档里**没有**残留的「射频(WiFi, 最后)」旧说法")


print("\n" + ("全部通过" if not FAILS else f"有失败项 ({len(FAILS)})"))
for f in FAILS:
    print("  ✗ " + f)
sys.exit(0 if not FAILS else 1)
