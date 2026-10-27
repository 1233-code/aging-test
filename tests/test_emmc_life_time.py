"""回归测试: eMMC 的**寿命健康度**判定 (§6.11 ③)。

起因: 文档 §6.11 写着「3 项全过」(写 + 读 + 寿命), 代码却只跑了 fio 写 + 读
两阶段 —— **寿命这一项压根没实现**。后果是: 一块已经磨到寿命上限的板子照样
判 passed, 报告里也没有任何寿命数据, 产线看不出来。

这轮把它实现成:

    读 /sys/block/mmcblk0/device/life_time → "0x01 0x01" (TYPE_A / TYPE_B 两条通道)
    节点给的是**已用寿命百分比** (0x00=0%, 0x0A=100%, 0xFF=未知)
    两条通道取**最差**的 (一条磨穿了就是磨穿了)
    已用寿命 **> 80%** → 判 failed (老化风险, 送修)
    节点读不到      → 判 error (不能把「读不到」当「好」)
    节点说「未知」   → 放行, 但指标里明写「未知」, 不让它悄悄变成「好」

叫法统一成「**已用寿命百分比**」(life_used_pct) —— 节点给的就是已用百分比,
别再叫 health_pct_wear(健康度): 一个说剩余一个说已用, 纸面上必然打架。

不跑真 fio、不读真节点 —— 都是喂进去的假数据。
跑法: .venv/bin/python -m tests.test_emmc_life_time
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.tests import emmc as E

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


class FakeCtx:
    def __init__(self):
        self.test_config = {"times": 1, "duration_sec": 10, "min_read_mbps": 80}
        self.logs = []

    async def on_log(self, line): self.logs.append(line)

    async def on_progress(self, pct, msg): pass

    async def on_metric(self, k, v): pass

    async def is_cancelled(self): return False


def patch(life):
    """life = (used_pct, raw, err) —— 直接替掉读节点那一步。"""
    E.sudo_hint_if_unavailable = lambda result, label: True

    async def fake_fio(test_file, size_mb, ctx, duration, on_metric=None,
                       on_progress=None):
        # 假装 fio 又快又好 (走完判定流程, 不让速度阈值把测试搅乱)
        return {"read_mbps": 200.0, "write_mbps": 150.0}

    E._run_fio_test = fake_fio
    E._read_life_time = lambda path=E.LIFE_TIME_PATH: life


async def run_emmc(life):
    patch(life)
    ctx = FakeCtx()
    return await E.EMMCTest().run(ctx)


# ───────────────────────── 解析层 ─────────────────────────

def case_parse():
    print("【1】life_time 节点的解析")
    check(E._parse_life_time("0x01 0x01") == 10,
          "0x01 0x01 → 10%", E._parse_life_time("0x01 0x01"))
    check(E._parse_life_time("0x00 0x00") == 0,
          "0x00 → 0%", E._parse_life_time("0x00 0x00"))
    check(E._parse_life_time("0x0A 0x0A") == 100,
          "0x0A → 100%", E._parse_life_time("0x0A 0x0A"))
    check(E._parse_life_time("0x02 0x09") == 90,
          "两条通道取**最差**的那条 (0x02 0x09 → 90%)",
          E._parse_life_time("0x02 0x09"))
    check(E._parse_life_time("0xFF 0x01") is None,
          "0xFF = 未知 (不是 255%, 也不是 0%)",
          E._parse_life_time("0xFF 0x01"))
    check(E._parse_life_time("") is None, "空 → None", E._parse_life_time(""))
    check(E._parse_life_time("garbage") is None,
          "认不出来的内容 → None", E._parse_life_time("garbage"))


# ───────────────────────── 判定层 ─────────────────────────

async def case_new_board():
    print("\n【2】新板 (已用 10%) → 判 passed, 指标里有寿命数据")
    r = await run_emmc((10, "0x01 0x01", None))
    check(r.status == "passed", "判成 passed", r.status)
    check(r.metrics.get("life_used_pct") == 10,
          "指标 life_used_pct = 10", r.metrics.get("life_used_pct"))
    check(r.metrics.get("life_time_hex") == "0x01 0x01",
          "指标里留了节点原文", r.metrics.get("life_time_hex"))


async def case_worn():
    print("\n【3】已用 90% (> 80%) → 判 **failed**")
    r = await run_emmc((90, "0x09 0x09", None))
    check(r.status == "failed", "判成 failed", r.status)
    check("寿命" in (r.error or ""),
          "错误原因写的是寿命 (不是速度之类的)", r.error)
    check(r.metrics.get("life_used_pct") == 90,
          "指标 life_used_pct = 90", r.metrics.get("life_used_pct"))


async def case_boundary():
    print("\n【4】边界: 恰好 80% → **放过** (口径是「大于 80% 才不通过」)")
    r = await run_emmc((80, "0x08 0x08", None))
    check(r.status == "passed",
          "80% 判 passed (大于 80% 才不通过)", r.status)
    check(r.metrics.get("life_used_pct") == 80,
          "指标 life_used_pct = 80", r.metrics.get("life_used_pct"))


async def case_node_missing():
    print("\n【5】节点读不到 → 判 **error** (不能把「读不到」当「好」)")
    r = await run_emmc((None, None, "寿命节点不存在: /sys/block/mmcblk0/device/life_time"))
    check(r.status == "error", "判成 error", r.status)
    check("寿命" in (r.error or ""), "错误原因写的是寿命检查失败", r.error)
    check("life_used_pct" not in r.metrics,
          "没写成某个百分比 (不能凭空造一个数)", r.metrics.get("life_used_pct"))


async def case_unknown():
    print("\n【6】节点说「未知」(0xFF) → 放行, 但指标里明写「未知」")
    r = await run_emmc((None, "0xFF 0xFF", None))
    check(r.status == "passed", "判成 passed (不能因为板子没报数就判它坏)",
          r.status)
    check(r.metrics.get("life_used_pct") == "未知",
          "指标写「未知」, 不让它悄悄变成「好」",
          r.metrics.get("life_used_pct"))
    check(r.metrics.get("life_time_hex") == "0xFF 0xFF",
          "节点原文也留着", r.metrics.get("life_time_hex"))


async def case_slow_read_still_fails():
    print("\n【7】读速度不达标 → 判 failed (寿命通过也一样)")
    E.sudo_hint_if_unavailable = lambda result, label: True

    async def slow_fio(test_file, size_mb, ctx, duration, on_metric=None,
                       on_progress=None):
        return {"read_mbps": 10.0, "write_mbps": 150.0}

    E._run_fio_test = slow_fio
    E._read_life_time = lambda path=E.LIFE_TIME_PATH: (10, "0x01 0x01", None)
    ctx = FakeCtx()
    r = await E.EMMCTest().run(ctx)
    check(r.status == "failed", "判成 failed", r.status)
    check("读速度" in (r.error or ""),
          "原因写的是读速度 (寿命没问题)", r.error)


async def main():
    case_parse()
    await case_new_board()
    await case_worn()
    await case_boundary()
    await case_node_missing()
    await case_unknown()
    await case_slow_read_still_fails()
    print("\n" + ("全部通过" if not FAILS else f"有失败项 ({len(FAILS)})"))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
