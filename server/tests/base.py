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


# 全局注册表
TEST_REGISTRY: Dict[str, Type[TestCase]] = {}


def register(cls: Type[TestCase]):
    """装饰器: 注册测试类"""
    TEST_REGISTRY[cls.name] = cls
    return cls
