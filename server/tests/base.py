"""测试用例基类 - 所有 10 个测试项继承此类"""
import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Type


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

        同步方法。默认不打扰; 需要交互的项 (tfcard / ethernet) 各自覆盖。
        """
        return None


# 全局注册表
TEST_REGISTRY: Dict[str, Type[TestCase]] = {}


def register(cls: Type[TestCase]):
    """装饰器: 注册测试类"""
    TEST_REGISTRY[cls.name] = cls
    return cls
