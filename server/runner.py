"""测试运行器 - 状态机 + WebSocket 推送 + mini-run 支持"""
import asyncio
import json
import time
from typing import Any, Dict, List, Optional, Tuple, Type

from .tests.base import TestCase, TestContext, TestResult, TEST_REGISTRY
from .tests.registry import get_all_tests
from .db import Database
from .config import Config


# §7.6 推荐执行顺序 - 4 阶段逐级加载,避免热累积污染轻量项
RECOMMENDED_ORDER: List[str] = [
    # ① 轻量优先 (内存 + 存储)
    "memory", "emmc", "tfcard",
    # ② 接口与外设
    "hdmi", "usb", "ethernet", "fan",
    # ③ 射频 (避免被 CPU/GPU 抖动干扰)
    "wifi",
    # ④ 重载收尾 (产热最高,放最后)
    "gpu", "cpu",
]
# §7.6 静默期硬规则 - 每项结束后留 N 秒, 可设但默认 5
INTERVAL_BETWEEN_TESTS_SEC: int = 5


class TestRunner:
    """单例 - 全局只有一个测试运行实例"""

    def __init__(self):
        self.run_id: Optional[int] = None
        self.cancelled = False
        self.current_test: Optional[str] = None
        self.results: Dict[str, TestResult] = {}
        self.subscribers: List[asyncio.Queue] = []
        self.started_at: float = 0
        self.finished_at: float = 0
        self.status: str = "idle"   # idle / running / passed / failed / stopped / error
        self._task: Optional[asyncio.Task] = None
        # 正在跑的那一项的**真实实例** —— stop() 要靠它 kill 子进程。
        # 不能用 `cls().cancel()`: 那是新建一个空实例, self.proc 是 None,
        # 什么也杀不掉 (而且 cancel 是协程, 不 await 等于什么都没发生)。
        self._current_instance: Optional[object] = None

        # Mini-run 模式
        self.mode: str = "full"               # 'full' | 'mini'
        self.parent_run_id: Optional[int] = None  # mini-run 模式下指向父 run id

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        self.subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue):
        if q in self.subscribers:
            self.subscribers.remove(q)

    async def _broadcast(self, event: dict):
        for q in self.subscribers:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass

    def snapshot(self) -> dict:
        snap = {
            "run_id": self.run_id,
            "status": self.status,
            "current_test": self.current_test,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "mode": self.mode,
            "parent_run_id": self.parent_run_id,
            "results": {
                name: {
                    "status": r.status,
                    "duration_sec": r.duration_sec,
                    "metrics": r.metrics,
                    "error": r.error,
                }
                for name, r in self.results.items()
            },
        }
        return snap

    async def start(self, config: dict):
        """启动一次完整测试运行.

        config 格式:
        {
          "tests": { "cpu": {"enabled": true, "duration_sec": 60}, ... },
          "project_root": "...",
          ...
        }
        """
        if self.status == "running":
            return {"ok": False, "error": "已有测试在运行"}

        self.cancelled = False
        self.results = {}
        self.current_test = None
        self.started_at = time.time()
        self.finished_at = 0
        self.status = "running"
        self.mode = "full"
        self.parent_run_id = None

        # 创建 DB 记录
        self.run_id = await Database.create_run(config)
        await self._broadcast({"type": "run_started", "run_id": self.run_id})

        self._task = asyncio.create_task(self._run_all(config))
        return {"ok": True, "run_id": self.run_id}

    async def start_mini(self, parent_run_id: int, test_names: List[str]) -> dict:
        """启动 mini-run: 仅重跑指定 test_names (§7.7).

        - 创建新 run 记录, mini_of 指向 parent_run_id
        - 按父 run 的配置跑指定测试
        - 每个测试完成后, 结果**就地更新**回 parent run 的对应行
        - mini-run 自己也有独立 summary
        """
        if self.status == "running":
            return {"ok": False, "error": "已有测试在运行"}

        if not test_names:
            return {"ok": False, "error": "test_names 不能为空"}

        # 校验 test_names 都已注册
        valid_names = [n for n in test_names if n in TEST_REGISTRY]
        if not valid_names:
            return {"ok": False, "error": f"未知测试项: {test_names}"}

        # 拿父 run 配置 (用于复用 defaults 等)
        parent = await Database.get_run(parent_run_id)
        if not parent:
            return {"ok": False, "error": f"父 run id={parent_run_id} 不存在"}
        parent_config = parent.get("config") or {}
        parent_tests = parent_config.get("tests", {})

        # 用父 run 中这些 test 的配置
        retest_tests_config = {n: parent_tests.get(n, {}) for n in valid_names}

        self.cancelled = False
        self.results = {}
        self.current_test = None
        self.started_at = time.time()
        self.finished_at = 0
        self.status = "running"
        self.mode = "mini"
        self.parent_run_id = parent_run_id

        config = {
            "tests": retest_tests_config,
            "project_root": parent_config.get("project_root", ""),
        }
        config.update(Config.all())

        self.run_id = await Database.create_mini_run(parent_run_id, config)
        await self._broadcast({
            "type": "run_started",
            "run_id": self.run_id,
            "mode": "mini",
            "parent_run_id": parent_run_id,
        })

        # mini-run 也按推荐顺序跑
        enabled_tests = self._build_test_list(
            config["tests"], explicit_order=config.get("order")
        )
        self._task = asyncio.create_task(self._run_tests(enabled_tests, config, write_to_parent=True))
        return {
            "ok": True,
            "run_id": self.run_id,
            "parent_run_id": parent_run_id,
            "tests": valid_names,
        }

    async def _run_all(self, config: dict):
        tests_config = config.get("tests", {})
        enabled_tests = self._build_test_list(
            tests_config, explicit_order=config.get("order")
        )
        await self._run_tests(enabled_tests, config, write_to_parent=False)

    @staticmethod
    def _build_test_list(tests_config: dict, explicit_order: Optional[List[str]] = None) -> List[Tuple[str, Type[TestCase]]]:
        """根据推荐顺序或工人拖动后的顺序构建测试队列.

        §7.6 推荐顺序默认生效, 工人在 UI 拖动重排后用 explicit_order 覆盖.
        """
        enabled: List[str] = [
            n for n, cfg in tests_config.items()
            if cfg.get("enabled", False) and n in TEST_REGISTRY
        ]
        if explicit_order:
            # 用户拖动顺序: 仅保留 enabled 中存在的项
            names = [n for n in explicit_order if n in enabled]
            # 末尾补上推荐序但 explicit_order 漏掉的项
            for n in enabled:
                if n not in names:
                    names.append(n)
            return [(n, TEST_REGISTRY[n]) for n in names if n in TEST_REGISTRY]
        # 默认按 RECOMMENDED_ORDER
        names = [n for n in RECOMMENDED_ORDER if n in enabled]
        # 未知/新增测试项排在最后
        for n in enabled:
            if n not in names:
                names.append(n)
        return [(n, TEST_REGISTRY[n]) for n in names if n in TEST_REGISTRY]

    async def _inter_test_interval(self, next_name: str, total_sec: int) -> bool:
        """§7.6 静默期 + 取消感知.

        返回 True = 间隔正常完成, False = 用户在静默期中止.
        前端监听 'between_tests' 事件后可显示「等待下一项...」提示.
        """
        await self._broadcast({
            "type": "between_tests",
            "next": next_name,
            "duration_sec": total_sec,
        })
        for _ in range(total_sec):
            if await self._is_cancelled():
                return False
            await asyncio.sleep(1)
        return True

    async def _run_tests(self, enabled_tests, config: dict, write_to_parent: bool):
        if not enabled_tests:
            self.status = "error"
            self.finished_at = time.time()
            await Database.finish_run(
                self.run_id, "error",
                {"reason": "未选择任何测试"}
            )
            await self._broadcast({"type": "run_finished", "status": "error"})
            return

        await self._broadcast({
            "type": "tests_planned",
            "tests": [{"name": n, "display_name": c.display_name} for n, c in enabled_tests]
        })

        any_failed = False
        for i, (name, cls) in enumerate(enabled_tests):
            # §7.6 静默期: 第一项前不需要, 其余每项跑完后等 N 秒
            if i > 0:
                ok = await self._inter_test_interval(
                    enabled_tests[i][0], INTERVAL_BETWEEN_TESTS_SEC
                )
                if not ok:
                    # 用户在静默期中止, 后面所有项 skipped
                    break

            self.current_test = name
            await self._broadcast({"type": "test_started", "name": name})
            await Database.upsert_result(
                self.run_id, name,
                status="running",
                started_at=time.time()
            )
            # mini-run 模式下, 先在父 run 把对应行也设回 running (避免 UI 看到旧状态)
            if write_to_parent and self.parent_run_id:
                await Database.upsert_result(
                    self.parent_run_id, name,
                    status="running",
                    started_at=time.time()
                )

            ctx = TestContext(
                run_id=self.run_id,
                config=config,
                test_config=config["tests"].get(name, {}),
                on_progress=lambda pct, msg, n=name: self._on_progress(n, pct, msg),
                on_log=lambda line, n=name: self._on_log(n, line),
                is_cancelled=self._is_cancelled,
                on_metric=lambda k, v, n=name: self._on_metric(n, k, v),
            )

            try:
                instance = cls()
                self._current_instance = instance   # 供 stop() 拿到真实实例去 kill 子进程
                self.results[name] = TestResult(status="running")
                t0 = time.time()
                result = await instance.run(ctx)
                result.duration_sec = time.time() - t0
                self.results[name] = result
            except Exception as e:
                result = TestResult(status="error", error=f"异常: {e!r}")
                self.results[name] = result
            finally:
                self._current_instance = None

            common_fields = {
                "status": result.status,
                "finished_at": time.time(),
                "duration_sec": result.duration_sec,
                "metrics": result.metrics,
                "log": result.log,
                "error": result.error,
            }

            # 写 mini-run 自己的行
            await Database.upsert_result(self.run_id, name, **common_fields)
            # mini-run 模式下就地更新 parent run 的对应行
            if write_to_parent and self.parent_run_id:
                await Database.upsert_result(self.parent_run_id, name, **common_fields)

            await self._broadcast({
                "type": "test_finished",
                "name": name,
                "result": {
                    "status": result.status,
                    "duration_sec": result.duration_sec,
                    "metrics": result.metrics,
                    "error": result.error,
                }
            })

            if result.status in ("failed", "error"):
                any_failed = True
            if self.cancelled:
                break

        # 汇总 - mini-run 与 full-run 各自独立写状态
        if self.cancelled:
            self.status = "stopped"
        elif any_failed:
            self.status = "failed"
        else:
            self.status = "passed"

        self.finished_at = time.time()
        self.current_test = None
        summary = {
            "total": len(enabled_tests),
            "passed": sum(1 for r in self.results.values() if r.status == "passed"),
            "failed": sum(1 for r in self.results.values() if r.status == "failed"),
            "error": sum(1 for r in self.results.values() if r.status == "error"),
            "skipped": sum(1 for r in self.results.values() if r.status == "skipped"),
        }
        await Database.finish_run(self.run_id, self.status, summary)

        await self._broadcast({
            "type": "run_finished",
            "status": self.status,
            "summary": summary,
            "mode": self.mode,
            "parent_run_id": self.parent_run_id,
        })

        # 重置 mini-run 标志
        if self.mode == "mini":
            self.mode = "full"
            self.parent_run_id = None

    async def _on_progress(self, name: str, pct: float, msg: str):
        await self._broadcast({"type": "progress", "name": name, "pct": pct, "msg": msg})

    async def _on_log(self, name: str, line: str):
        await self._broadcast({"type": "log", "name": name, "line": line})

    async def _on_metric(self, name: str, key: str, value):
        await self._broadcast({"type": "metric", "name": name, "key": key, "value": value})

    async def stop(self):
        if self.status != "running":
            return {"ok": False, "error": "没有正在运行的测试"}
        self.cancelled = True
        # 通知当前正在跑的那一项 —— 靠真实实例, 子进程才会真被 kill 掉。
        # 取消标志本身通过 ctx.is_cancelled() 让各项自己检查退出点;
        # 这里是补刀: 让各项**立刻**停下, 而不是等它跑到下一个检查点。
        inst = self._current_instance
        if inst is not None:
            try:
                await inst.cancel()
            except Exception:
                pass
        return {"ok": True}

    async def _is_cancelled(self) -> bool:
        return self.cancelled


# 单例
runner = TestRunner()
