"""蓝牙检查 —— **判定反过来: 不工作才算合格**(§6.4.1)

这台设备**不需要蓝牙** ⇒ 下面这几种都是**正常状态**, 判 `failed` 是冤枉它:

    · 板子上压根没有 BT 芯片
    · 芯片在, 但射频关着 / 控制器没注册 / 蓝牙服务被镜像屏蔽

反过来, **蓝牙还在正常工作**才是要查的事(§6.4.1): 跟产品「这台设备没有
蓝牙」的规格对不上 —— 出了厂还能被扫到, 是个**要解释的事**。

**「在工作」的判据 —— 两个信号都要满足**(§6.4.1 的 ①②, 纯本地状态):

    ① 射频没被屏蔽   —— `/sys/class/rfkill/rfkill*` 里 type=bluetooth 的
                        那一条, soft=0 且 hard=0
    ② 有活动的控制器 —— `/sys/class/bluetooth/hci*` 或 `/dev/hci*` 在

⚠️ **不采用 ③④**(能不能连上 / 有没有 beacon / 有没有主动连接)—— 那两个
   依赖周边有没有别的蓝牙设备, **不可重复**, 不适合做产线判定。

⇒ 两个信号满足其一为「不工作」(射频关着 / 没有控制器)⇒ `passed`;
  两个都满足 ⇒ 「在工作」⇒ `failed`。

#### ⚠️ 只做**只读探测** —— 一条命令都不发

    · `/sys/class/rfkill/rfkill*/`  —— type / name / soft / hard
    · `/sys/class/bluetooth/hci*`    —— 内核认得的蓝牙控制器
    · `/dev/hci*`                    —— 控制器设备节点
    · `/etc/systemd/system/bluetooth.service` —— 是不是被镜像 mask 掉了

**不许** `rfkill unblock`、**不许** `bluetoothctl power on`、**不许**启停
`bluetooth` 服务。理由跟 §6.4 的「射频不硬拔」是同一条: 动射频/无线开关
正是最容易在**别人正在用**的时候把板子弄挂的操作(这块板子被硬复位打死的
次数已经不少, §9.3.1)—— 而这一项的**目的恰恰是确认蓝牙没在工作**, 你去
把它打开再关上, 测出来的东西**不作数**。

⇒ 真的需要打开蓝牙才能判断的, **不做** —— 只按只读能看到的判。

⇒ 附带的好处: **不需要 root, 也不需要板上装任何蓝牙工具**。实测这块板子
  上 rfkill / btmgmt / hciconfig / bluetoothctl **一个都没有**(bluetooth
  服务还被 mask 了)—— 这一项照样能测, 因为它只读 sysfs。
"""
import os
from typing import List, Optional

from .base import TestCase, TestContext, TestResult, register

# ── 被探测的路径 (模块级常量 ⇒ 回归测试可以整体换掉) ──
#
# ⚠️ **不要在运行期改这些**: 这一项是「只读探测」, 路径写死才能保证
#   「探测的就是这台机器自己的蓝牙状态」。
SYS_CLASS_DIR = "/sys/class"
RFKILL_DIR = "/sys/class/rfkill"
BLUETOOTH_CLASS_DIR = "/sys/class/bluetooth"
DEV_DIR = "/dev"
BT_SERVICE_UNIT = "/etc/systemd/system/bluetooth.service"


def _read_text(path: str) -> Optional[str]:
    """读一个文本文件, 读不到 ⇒ None(**不抛异常** —— 探测项不该炸)。"""
    try:
        with open(path, "r", errors="replace") as f:
            return f.read().strip()
    except OSError:
        return None


def _read_int(path: str) -> Optional[int]:
    """读一个整数文件 (rfkill 的 soft/hard 就是 0/1)。读不到 ⇒ None。"""
    t = _read_text(path)
    if t is None:
        return None
    try:
        return int(t.strip())
    except ValueError:
        return None


def _read_rfkill_entries() -> List[dict]:
    """`/sys/class/rfkill/` 里 **type == 'bluetooth'** 的那些条目。

    ⚠️ **必须按 type 过滤** —— 同一个目录下还有 wifi / wlan 等别的射频,
    把它们算成「蓝牙硬件在」就是凭空造出一个不存在的结论。

    每条返回 {"path", "name", "soft", "hard"}; soft/hard 读不到时是 None。
    """
    out: List[dict] = []
    try:
        names = sorted(os.listdir(RFKILL_DIR))
    except OSError:
        return out
    for name in names:
        d = os.path.join(RFKILL_DIR, name)
        if _read_text(os.path.join(d, "type")) != "bluetooth":
            continue
        out.append({
            "path": d,
            "name": _read_text(os.path.join(d, "name")) or "",
            "soft": _read_int(os.path.join(d, "soft")),
            "hard": _read_int(os.path.join(d, "hard")),
        })
    return out


def _read_hci_sysfs() -> List[str]:
    """`/sys/class/bluetooth/hci*` —— 内核认得的蓝牙控制器。"""
    try:
        names = sorted(os.listdir(BLUETOOTH_CLASS_DIR))
    except OSError:
        return []
    return [n for n in names if n.startswith("hci")]


def _read_hci_dev() -> List[str]:
    """`/dev/hci*` —— 控制器的设备节点。

    ⚠️ 设备节点**在控制器被关掉之后也可能还留着**(udev 建的)⇒ 它单独
    出现**不足以**说明蓝牙在工作 —— 还要看 rfkill 是不是屏蔽着。
    """
    try:
        names = sorted(os.listdir(DEV_DIR))
    except OSError:
        return []
    return [n for n in names if n.startswith("hci")]


def _service_masked() -> Optional[bool]:
    """`bluetooth` 服务是不是被镜像 **mask** 掉了 (→ /dev/null)。

    True = 被屏蔽; False = 有 unit 覆盖但没屏蔽; None = 读不出来
    (压根没有这个覆盖文件 —— 分发版的 unit 在 /lib/... 下, 看不到)。
    """
    try:
        target = os.readlink(BT_SERVICE_UNIT)
    except OSError:
        if os.path.exists(BT_SERVICE_UNIT):
            return False          # 普通文件 = 显式 unit, 不是 mask
        return None               # 没有覆盖文件 ⇒ 读不出来
    return target == "/dev/null"


def _judge(rfkill_entries: List[dict], hci_sysfs: List[str],
           hci_dev: List[str]) -> dict:
    """把探测到的事实变成判定 —— **纯函数**(回归测试直接调它)。

    返回:
        hw_present  内核认得出蓝牙硬件吗 (有 rfkill 条目 或 有控制器)
        rf_state    absent / blocked / unblocked / unknown
        rf_names    rfkill 条目的名字 (如 ["sunxi-bt"])
        controllers 控制器名字 (sysfs + /dev 合并去重)
        working     蓝牙**在工作**吗 (§6.4.1 的 ①② 都要满足)
    """
    rf_state = "absent"
    if rfkill_entries:
        # soft/hard 读不出来 ⇒ "unknown"(不是 blocked, 也不是明确的 unblocked)
        read_ok = False
        blocked = False
        for e in rfkill_entries:
            soft, hard = e.get("soft"), e.get("hard")
            if soft is None and hard is None:
                continue
            read_ok = True
            if soft == 1 or hard == 1:
                blocked = True
        if not read_ok:
            rf_state = "unknown"
        else:
            rf_state = "blocked" if blocked else "unblocked"

    controllers = sorted(set(hci_sysfs) | set(hci_dev))
    hw_present = bool(rfkill_entries) or bool(controllers)
    # 「在工作」= 有活动的控制器 **且** 射频没被屏蔽。
    # ⚠️ rf_state=unknown(屏蔽状态读不出来)不构成 blocked ⇒ 仍然按
    #    「可能在工作」处理 —— 但日志和指标里会明写状态读不出来。
    #
    # ⚠️⚠️ **「活动的控制器」只看 sysfs** (2026-11-11 改):
    #   `/dev/hci*` 是 **udev 建的设备节点** —— 控制器被关掉之后**它还留着**
    #   (这正是 `_read_hci_dev` 注释里那句话)。⇒ 拿它当「有活动的控制器」
    #   ⇒ 一块**蓝牙本来就该关着**的板子(射频也没屏蔽着, 只是 /dev 里
    #   留了个旧节点)会被判「蓝牙在工作」⇒ **failed ⇒ 冤枉好板子**
    #   (本机的规格就是「不需要蓝牙」⇒ 它本来就是合格的)。
    #   ⇒ 真正说明「控制器是活的」的是 **内核认得的**它:
    #     `/sys/class/bluetooth/hci*` —— hci 设备一注册, 内核就建这个 class
    #     条目; 蓝牙**关着**时它没有。
    active = sorted(set(hci_sysfs))
    working = bool(active) and rf_state != "blocked"

    return {
        "hw_present": hw_present,
        "rf_state": rf_state,
        "rf_names": [e.get("name") or "" for e in rfkill_entries],
        "controllers": controllers,
        "active_controllers": active,
        "working": working,
    }


# ── 给人看的两段话 ───────────────────────────────────────────────
#
# ⚠️ **「通过」不能写成「蓝牙是好的」** —— 工人看到「蓝牙: 通过」会以为
#    蓝牙功能正常, **恰恰相反**(§6.4.1)。所以合格时写的是
#    「蓝牙: 已关闭(符合本机规格)」。

VERDICT_OFF = "已关闭(符合本机规格)"
VERDICT_ON = "在工作 —— 跟本机规格(不需要蓝牙)对不上"


def _detail_text(v: dict) -> str:
    """「不工作」的**具体是哪种** —— §6.4.1 要求日志里说清楚是哪一种。

    「不工作」有三种, 产线的动作完全不同:
      · 压根没这块硬件      ⇒ 机器没装蓝牙, 正常
      · 硬件在但射频被屏蔽  ⇒ 镜像/BIOS 把它关了, 正常
      · 硬件在但没有控制器  ⇒ 射频没开 / 蓝牙服务没起来, 正常

    ⚠️ 只说「是哪一种」, **不带**「符合本机规格」那半句 —— 那半句由
    `VERDICT_OFF` 负责, 两边各说一遍的话日志里会重复。
    """
    if not v["hw_present"]:
        return "板上探不到蓝牙硬件 —— 这台设备本来就没有蓝牙"
    if v["rf_state"] == "blocked":
        return "蓝牙硬件在, 但射频被屏蔽着(soft/hard block)"
    if v["rf_state"] == "unknown":
        return "蓝牙硬件在, 但射频的屏蔽状态读不出来 —— 按「没在工作」处理"
    # ⚠️ `/dev` 里的残留节点**要说明白** —— 否则工人看到「蓝牙设备: (无)」
    #    而 `/dev/hci0` 明明在, 会以为工具看错了。
    leftovers = [c for c in v["controllers"] if c not in v["active_controllers"]]
    extra = ("(但 `/dev` 里还留着 %s —— 那是 udev 的残留节点, **不算控制器**)"
             % ", ".join(leftovers)) if leftovers else ""
    return ("蓝牙硬件在, 但没有活动的控制器(射频没开 / 蓝牙服务没起来)"
            + extra)


@register
class BluetoothTest(TestCase):
    name = "bluetooth"
    display_name = "蓝牙 (应关闭)"
    description = "只读探测 · 判定反过来: 蓝牙不工作才算合格"

    async def run(self, ctx: TestContext) -> TestResult:
        result = TestResult()

        await ctx.on_log(
            "[蓝牙] 只读探测 —— 不动蓝牙的任何状态(不解除射频屏蔽 / 不给蓝牙上电 / "
            "不启停蓝牙服务)"
        )
        await ctx.on_progress(20, "读蓝牙状态")

        # ── 探测不到系统状态 ⇒ **error**, 不是 passed ──
        #
        # §9.3: `error` = 「这一项没测成」, `passed` = 「测过了, 合格」。
        # sysfs 没挂上的时候我们**什么都不知道**, 判 passed 就是在骗人
        # (那正是 §9.3 说的「产线上最危险的一类故障」: 让人以为测过了)。
        if not os.path.isdir(SYS_CLASS_DIR):
            result.status = "error"
            result.error = (
                f"读不到 {SYS_CLASS_DIR} —— 这台机器的 sysfs 没挂上(或不是 Linux), "
                "判不了蓝牙状态。**这一项没测成**, 不等于「蓝牙合格」。"
            )
            await ctx.on_log(f"[蓝牙] ✗ {result.error}")
            await ctx.on_progress(100, "失败")
            return result

        rfkill_entries = _read_rfkill_entries()
        hci_sysfs = _read_hci_sysfs()
        hci_dev = _read_hci_dev()
        masked = _service_masked()

        v = _judge(rfkill_entries, hci_sysfs, hci_dev)

        # ── 指标 ──
        result.metrics["bt_present"] = v["hw_present"]
        result.metrics["bt_rf_state"] = v["rf_state"]
        result.metrics["bt_working"] = v["working"]
        # ⚠️ 这里报的是**活动的控制器**(内核认得的那几个), 不是 `/dev` 里
        #    的全部节点 —— /dev 里那个残留节点**不是控制器**(见 `_judge`
        #    的注释)⇒ 报出来会让工人以为「蓝牙设备还在」⇒ 而结论是
        #    「已关闭」⇒ 两处对不上。/dev 里残留的东西在**详情**里说明。
        result.metrics["bt_hci_devices"] = (
            ", ".join(v["active_controllers"]) or "(无)")
        result.metrics["bt_rf_name"] = ", ".join(v["rf_names"]) or "(无)"
        result.metrics["bt_service_masked"] = (
            "unknown" if masked is None else masked
        )
        result.metrics["bt_verdict"] = VERDICT_ON if v["working"] else VERDICT_OFF
        result.metrics["bt_detail"] = _detail_text(v) if not v["working"] else ""

        # ── 日志: 把**看到的事实**一条条写出来 ──
        #
        # 为什么这么啰嗦: 这一项的结论是**反的** —— 「没在工作」才算合格。
        # 只写一句「通过」的话, 读日志的人根本不知道到底发生了什么;
        # 把 rfkill / 控制器 / 服务三样都摆出来, 才看得出是哪一种「不工作」。
        if rfkill_entries:
            for e in rfkill_entries:
                await ctx.on_log(
                    f"[蓝牙] rfkill: {e.get('name') or '(无名)'} "
                    f"(soft={e.get('soft')}, hard={e.get('hard')}) "
                    f"→ 射频状态 {v['rf_state']}"
                )
        else:
            await ctx.on_log(
                "[蓝牙] rfkill: 没有 type=bluetooth 的条目(内核不认识这块射频)"
            )
        await ctx.on_log(
            f"[蓝牙] 控制器: /sys/class/bluetooth = "
            f"{', '.join(hci_sysfs) or '(无)'} | /dev/hci* = "
            f"{', '.join(hci_dev) or '(无)'}"
        )
        if masked is True:
            await ctx.on_log("[蓝牙] bluetooth 服务: 被镜像屏蔽(mask) —— 没起来")
        elif masked is False:
            await ctx.on_log("[蓝牙] bluetooth 服务: 有 unit 覆盖, 未被屏蔽")
        else:
            await ctx.on_log("[蓝牙] bluetooth 服务: 看不到 unit 覆盖(读不出来)")

        await ctx.on_progress(70, "判定中")

        if v["working"]:
            # ── 唯一会 failed 的情况 ──
            result.status = "failed"
            result.error = (
                "蓝牙**在工作**(有活动的控制器, 射频也没被屏蔽) —— 跟本机规格"
                "(这台设备不需要蓝牙)对不上, 要点名。⚠️ 本项只做只读探测, "
                "**没有**去动蓝牙的状态。"
            )
            await ctx.on_log(f"[蓝牙] ✗ 蓝牙: {VERDICT_ON}")
            await ctx.on_progress(100, "失败")
            return result

        # ── 合格: 蓝牙确实没在工作 ──
        #
        # ⚠️ **不写 result.error** —— 那个字段在页面和报告里是**红字错误**
        #    (`.summary-row-error` / `.wizard-error` 都是红的)。这一项是
        #    **合格**的, 把说明塞进红色错误栏里, 等于自己给自己报警。
        #    ⇒ 说明放在**指标**(`bt_verdict` / `bt_detail`)和**日志**里。
        result.status = "passed"
        await ctx.on_log(f"[蓝牙] ✓ 蓝牙: {VERDICT_OFF} —— {_detail_text(v)}")
        await ctx.on_progress(100, "完成")
        return result
