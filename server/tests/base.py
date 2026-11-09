"""测试用例基类 - 所有 11 个测试项继承此类"""
import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Type


class TestCancelled(Exception):
    """**用户中途按了「停止」** —— 不是测试失败。

    §9.3: 用户中途停止 → 正在进行的那一项记 **stopped**(不计入 total),
    其余没跑到的项也记 stopped。没跑完 ≠ 不合格 —— 所以这一项**不能**记
    failed/error, 更不能把整轮判成 failed。

    以前各项一律 `except Exception` 把取消也当成 error, 于是工人自己按的
    暂停, 在报告里变成了「失败」, 整轮也跟着判成 failed(2026-10-28 实测)。
    """


class DeviceUnresponsive(Exception):
    """被测设备(卡/eMMC)对读写**没有应答**, 一直不返回。

    跟「跑失败」不是一回事, 判定也不同:
      · 普通失败(failed) = 读出来的东西不合格(速度不达标之类);
      · 没应答           = **设备卡死了** —— 这本身就是设备不合格
        (这张卡坏了), 该判 failed 并明说「无响应」。

    为什么非要把这两种分开: 以前两者混成一句
    `RuntimeError("fio write 被中断 (取消或超时)")`, 工人看到的是「失败」,
    分不清是**工具坏了**还是**卡坏了** —— 而这两种的产线动作完全相反
    (前者修工具, 后者换卡)。
    """


#: 「料没到位」(卡槽空 / 网线没插) 这类提示的**等待上限**(秒)。
#:
#: 为什么比全局的 `flow.operator_wait_sec`(默认 300s) 短得多:
#: 这两种都是「把东西插上就能继续」的**物理动作** —— 工人要么已经在板子
#: 边上(那就几秒的事), 要么人根本不在这一台跟前(那就是这台板子上压根
#: 没这个料, 等五分钟也等不来)。产线的实际节奏是「插上就跑」。
#: ⇒ 30 秒: 30 秒内插好并点「已插好重测」照旧原地重测; 没人管就
#:   **直接跳过**, 整轮继续往下走(不挂死)。
#:
#: ⚠️ 这是**上限**, 不是「必须等满 30 秒」—— 工人随时可以点「跳过」,
#:   点了立刻往下走。
OPERATOR_WAIT_SHORT_SEC: float = 30.0


@dataclass
class TestContext:
    """测试运行时上下文 - 传给每个测试用例"""
    run_id: int
    config: dict            # 整个 config.yaml
    test_config: dict       # 当前测试的 defaults.xxx 配置
    on_progress: Any        # async callback(progress_pct: float, message: str)
    on_log: Any             # async callback(line: str)
    is_cancelled: Any       # async callable -> bool
    on_metric: Any          # async callback(key: str, value: Any) 实时指标
    # async callback(text, level, seconds) —— **界面上显眼的横幅**。
    # 默认 None = 这一项不需要横幅(现在只有 HDMI 的「马上要黑屏」用)。
    # text 为空串 = **收起**横幅。
    on_alert: Any = None


@dataclass
class TestResult:
    """测试结果"""
    status: str = "pending"     # passed / failed / skipped / error
    duration_sec: float = 0.0
    metrics: dict = field(default_factory=dict)
    log: str = ""
    error: Optional[str] = None


class TestCase:
    """所有测试项的基类"""

    # 子类必须定义
    name: str = ""                  # 内部名: "cpu"
    display_name: str = ""          # UI 显示名: "CPU 压力"
    description: str = ""           # 描述

    async def run(self, ctx: TestContext) -> TestResult:
        """子类实现测试逻辑

        约定:
          - 定期调用 await ctx.on_progress(pct, msg) 上报进度 (0-100)
          - 实时输出调用 await ctx.on_log(line) 写日志
          - 周期性指标用 await ctx.on_metric(key, value)
          - 取消检查 await ctx.is_cancelled() 在长循环中
          - 返回 TestResult
        """
        raise NotImplementedError

    async def cancel(self):
        """外部触发取消时调用,子类覆盖以终止子进程等"""
        pass

    def expected_seconds(self, test_config: dict) -> Optional[float]:
        """本项**预期**要跑多少秒 —— 只给界面提前告诉工人「还要等多久」,
        不参与任何判定, 算错了也不影响测试结果。

        为什么由测试自己算, 而不是前端按配置套公式: eMMC/TF 的 `duration_sec`
        是**每个阶段**的时长 (写一遍 + 读一遍 = 2 倍), USB 是 轮数 × 单轮,
        CPU 是 轮数 × 单轮 —— 每项语义都不一样。前端复制一份公式, 改配置时
        必然会漏掉一处, 于是界面上标的「预计 60s」和实际跑的 120s 对不上。
        让**拥有这段时长逻辑的那个测试类**自己回答, 就只有一个事实来源。

        返回 None = 算不出来 (WiFi / 网口 / HDMI / 风扇 / GPU 的时长由驱动
        和环境决定), 界面就不显示预计时间 —— 显示一个假的数字比不显示更糟。
        """
        return None

    def operator_prompt(self, result: TestResult) -> Optional[dict]:
        """这个结果要不要**停下来**等操作员插/拔硬件后再决定。

        §7.3: 有些结果不是「测完了」, 而是「设备没问题, 但料没到位」——
        TF 卡槽是空的、网线没插。工人能做的只有两个选择: 把料插上重测,
        或者跳过。**这两种都必须由人来做**, 所以整轮测试得真的停在这儿等,
        而不是弹个框就接着往下跑 (那样下一项会在工人还没决定时就开始,
        屏幕和实际跑的项目就对不上了)。

        返回 None = 不需要停, 正常进下一项。
        返回 {"text": "..."} = 需要停, runner 会广播 waiting_operator,
        并等 /api/test/operator 的 retest / skip 决定。

        **可以多带一个 `"wait_sec"`** —— 这一条提示自己要多等几秒:
          · 不带 → 用全局的 `flow.operator_wait_sec`(默认 300s);
          · 带了 → **用这一条自己的**(例: 卡槽空 / 网线没插这种
            「把东西插上就能继续」的, 用 `OPERATOR_WAIT_SHORT_SEC` = 30s;
            工人 30 秒内没处理就**直接跳过**, 不占着整轮等 5 分钟)。
        ⚠️ 别在这里写死 `int` —— 超时值被截成 0 的那个 bug 就在这附近
          (见 `server/runner.py` 里 `float(...)` 那行)。

        同步方法。默认不打扰; 需要交互的项 (tfcard / ethernet) 各自覆盖。
        """
        return None


# 全局注册表
TEST_REGISTRY: Dict[str, Type[TestCase]] = {}


def register(cls: Type[TestCase]):
    """装饰器: 注册测试类"""
    TEST_REGISTRY[cls.name] = cls
    return cls
