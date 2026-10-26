"""测试运行器 - 状态机 + WebSocket 推送 + mini-run 支持"""
import asyncio
import json
import time
import traceback
from typing import Any, Dict, List, Optional, Tuple, Type

from .tests.base import TestCase, TestContext, TestResult, TEST_REGISTRY
from .tests.registry import get_all_tests
from .db import Database
from .config import Config
from . import identity, report


def _report_cfg() -> dict:
    # `or {}` 不是多余的: config.yaml 里写 `report:`(下面全注释掉)时 yaml 解析出
    # 的是 None 而不是 {}, 而 Config.get 的默认值只在 key **不存在**时才生效。
    return Config.get("report") or {}


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
# §7.6 静默期硬规则 - 每项结束后留 N 秒, 再开下一项。
#
# 2026-10-26: 5s → 3s(产线: 「五秒有点长了」)。这一段是**纯等待**, 不产出
# 任何测试结论, 它的唯一作用是让 CPU / 温度 / 调度器复位一下 —— 3 秒够
# 复位, 不够"让工人读完上一项结果"(要读结果有的是地方: 总结页、报告)。
# 10 项 9 个间隔 = 整轮省 18 秒。
INTERVAL_BETWEEN_TESTS_SEC: int = 3

# §7.8 等操作员: 卡槽空 / 网线没插 这类「料没到位」的结果, 整轮要**真停住**。
# 超时后自动往下走 —— 操作员可能去拿卡了、页面可能被他关掉, 不设上限的话
# 一次误操作就能把整轮老化挂到天亮, 剩下的项目一个都不跑, 报告还缺一大半。
OPERATOR_WAIT_DEFAULT_SEC: int = 300
# 同一个位置最多让工人重测几次 —— 插了三次都没认出来, 多半是卡本身的问题,
# 再点一百次也只是在耗老化时长。
OPERATOR_MAX_RETEST: int = 3

# 单项日志的行数上限。
#
# ⚠️ 这是**兜底**, 不是"截断策略" —— 正常跑一轮的日志是几百行, 远够不着。
# 它存在只为挡住一种情况: 某个测试进了死循环疯狂打日志, 把内存吃光 / 把
# PDF 撑成几百兆。真要撞上了, 报告会**明写**「日志过长, 只保留了末尾 N 行」,
# 绝不悄悄少一段 —— 少了一段还看不出来, 那比超长糟糕得多。
LOG_MAX_LINES: int = 10000


class TestRunner:
    """单例 - 全局只有一个测试运行实例"""

    def __init__(self):
        self.run_id: Optional[int] = None
        self.cancelled = False
        self.current_test: Optional[str] = None
        self.results: Dict[str, TestResult] = {}
        # 每一项的**完整**日志 (行列表) —— 报告「日志附录」的数据源。
        #
        # 为什么在这儿抓, 而不是让各项自己写 result.log: 抓点只有一个 ——
        # _on_log() 是各项推日志的**唯一出口**, 每一行都从这里过。而各项
        # 自己写的那份是**残缺**的: 十项里七项压根不写(实测产线那份报告里
        # USB 那一项的日志附录是"(无日志)"), 写了的那几项还是截断过的
        # 末尾 N 行(GPU 20 行 / 内存 200 行 / CPU 400 行)。
        #
        # 残缺的日志比没有日志**更糟** —— 读的人会以为那就是全部, 于是照着
        # 一份缺了头的日志去判断机器有没有问题。
        self._logs: Dict[str, List[str]] = {}
        # 被上限截断过的项 —— 截断了就**必须说出来**, 不许悄悄少一段。
        self._log_truncated: Dict[str, int] = {}
        # mini-run 写父轮时, 父轮那一项**原本**的日志 (第一次写之前抓下来)。
        # 有了它, 重试时每次重写「基准 + 全部尝试」而不是一次次往后接 ——
        # 不然头一次尝试的日志会在父轮里出现两遍。
        self._parent_log_base: Dict[str, str] = {}
        self.subscribers: List[asyncio.Queue] = []
        self.started_at: float = 0
        self.finished_at: float = 0
        self.status: str = "idle"   # idle / running / passed / failed / stopped / error
        self._task: Optional[asyncio.Task] = None
        # 正在跑的那一项的**真实实例** —— stop() 要靠它 kill 子进程。
        # 不能用 `cls().cancel()`: 那是新建一个空实例, self.proc 是 None,
        # 什么也杀不掉 (而且 cancel 是协程, 不 await 等于什么都没发生)。
        self._current_instance: Optional[object] = None
        # §7.8 等操作员拍板时的槽位。非 None = 当前**真的停住了**, 正在
        # 等 /api/test/operator 的决定; 里面 event 一 set, _wait_for_operator
        # 就醒过来继续往下走。
        self._operator_wait: Optional[dict] = None
        # 当前正在跑的这一项: 预计多少秒 + 从什么时候开始 (§7.5)。
        # 存在 self 上而不是局部变量, 是为了让 snapshot() 能带上它们 ——
        # 工人跑到一半刷新了页面, 重连后计时条要接着走, 而不是消失。
        self._current_expected_sec: Optional[float] = None
        self._current_started_at: Optional[float] = None

        # Mini-run 模式
        self.mode: str = "full"               # 'full' | 'mini'
        self.parent_run_id: Optional[int] = None  # mini-run 模式下指向父 run id

        # §2.2 / §8.2 报告身份。SN 从 /proc/cmdline 读一次就固定下来 —— 不能在
        # 收尾时再读, 那时万一有人重启了机器, 报告会挂到错误的 SN 上, 而这种
        # 错配在纸质归档里是查不出来的。
        self.sn: str = identity.UNKNOWN
        self.operator: Optional[str] = None
        self.batch: Optional[str] = None
        # 本轮导出的 PDF 路径 (None = 没导出/导出失败, 见 _export_pdf)
        self.pdf_path: Optional[str] = None
        # 本轮 config 快照 / 系统级错误 —— 报告的配置快照段和「系统级错误」块要用
        self.run_config: Optional[dict] = None
        self.run_error: Optional[str] = None
        # §7.6 本轮的测试队列 (执行顺序, 含**还没跑到**的那些)。
        #
        # 为什么光有 results 不够: results 只装**已经跑出结果**的项。页面刷新
        # 或 WS 重连时, 靠 results 重建列表, 前面还没跑的项会整个消失 ——
        # 进度条的分母不对, 总结页也少几行。队列是「这一轮要跑哪些、按什么
        # 顺序」, 是这一轮**开始时**就定下来的, 不随跑到哪儿而变。
        self.test_queue: List[str] = []

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
        w = self._operator_wait
        snap = {
            "run_id": self.run_id,
            "status": self.status,
            "current_test": self.current_test,
            # §7.5: 让刷新/重连后的页面把「本项计时」接上。不带这两个值的话,
            # 工人跑到一半刷新, 界面就退回成没有预计时长的样子, 得一直等到
            # 这一项跑完才知道它到底要多久。
            "current_expected_sec": self._current_expected_sec,
            "current_started_at": self._current_started_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "mode": self.mode,
            "parent_run_id": self.parent_run_id,
            # §7.6 本轮的测试队列(执行顺序, 含还没跑到的)。前端靠它重建
            # 完整的逐项列表 —— 只靠 results 的话, 还没跑的项会整个消失。
            "test_queue": list(self.test_queue),
            # §8.2 这一轮有没有导出过 PDF。前端靠它决定按钮排里显示
            # 「打开 PDF 报告」还是「导出 PDF 报告」(§8.2 规定二选一,
            # 不同时出现)。重连后靠这个恢复, 否则重连完按钮会退化成
            # 「导出」, 工人以为这轮没出过报告。
            "pdf_path": self.pdf_path,
            # 这一轮记的 SN。⚠️ **不是**当前板子的 SN —— 重连时前端要显示的
            # 是「这一轮测的是哪台机器」, 那是这一轮开始时记下的值。
            "sn": self.sn,
            # 整轮异常终止的原因 (status=error 时有)。总结页要显示它 ——
            # 光看「0 通过 / 0 失败 / 0 跳过」没人知道出了什么事。
            "error": self.run_error,
            # §7.8: 页面刷新/重连后靠这一项把弹框重新显示出来。
            # 不带它的话, 工人刷一下页面, 后端还在等, 界面上却什么提示都没有,
            # 只能干看着「运行中」等到超时。
            "waiting_operator": (
                {
                    "name": w["name"],
                    "text": w["text"],
                    "attempt": w["attempt"],
                    "timeout_sec": w["timeout_sec"],
                } if w else None
            ),
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
        self._logs = {}          # 新的一轮, 日志留档从头开始
        self._log_truncated = {}
        self._parent_log_base = {}
        self.current_test = None
        self.started_at = time.time()
        self.finished_at = 0
        self.status = "running"
        self.mode = "full"
        self.parent_run_id = None
        # 每轮重新读一次 SN —— 产线上一台机器可能连着测很多轮, 上一轮的 SN
        # 不能想当然沿用。读不到就是 unknown, 报告里会打黄条提醒人工核对。
        self.sn = identity.get_sn()
        # §8.2 测试人员 / 批次: **本轮 config 里的优先**(前端每轮手填的姓名),
        # 没有才回落到全局 config.yaml 的 report 段。
        #
        # ⚠️ 不能只读 _report_cfg() —— 那是**全局 yaml**, 拿不到本轮手输的值。
        # 只读全局的话, 工人明明填了姓名, 报告里还是「未填写」, 而且日志上
        # 一点异常都没有(静默走默认值那条路)。
        _rep = config.get("report") or {}
        self.operator = _rep.get("operator") or _report_cfg().get("operator") or None
        self.batch = _rep.get("batch") or _report_cfg().get("batch") or None
        self.run_config = config
        self.run_error = None
        self.pdf_path = None
        # §7.6 队列**在开跑前**就定下来 —— 页面刷新/重连时靠它知道这一轮
        # 一共要跑几项、什么顺序(§7.6「运行态必须能靠快照恢复」)。
        self.test_queue = [
            n for n, _ in self._build_test_list(
                config.get("tests") or {}, config.get("order"))
        ]

        # 创建 DB 记录
        self.run_id = await Database.create_run(config, self._run_meta())
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
        self._logs = {}          # 新的一轮, 日志留档从头开始
        self._log_truncated = {}
        self._parent_log_base = {}
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
        # ⚠️ 顺序要紧: 全局 config 要是也有 `tests:` 段, 上面这行 update 会把
        # 父轮那几项冲掉 —— mini-run 就变成「把整套再跑一遍」, §7.7 mini-run
        # 的意义全没了(而且是**静默**的: 界面上只显示重测了 2 项, 机器上却
        # 跑了 12 项)。所以 tests 必须在 update **之后**再定死。
        config["tests"] = retest_tests_config

        # 重测轮的 SN **沿用父轮记录的**, 不重新读 /proc/cmdline —— 父轮才是
        # "当时测的是哪台机器"的权威记录。父轮没有(老库/当时没读到)才回读。
        self.sn = parent.get("sn") or identity.get_sn()
        # 测试人员同理**沿用父轮**: 重测的是同一轮里的失败项, 还是同一个
        # 人在测 —— 换成人反而会让「这一轮谁负责」对不上(父轮是这一轮)。
        self.operator = (parent.get("operator")
                         or _report_cfg().get("operator") or None)
        self.batch = (parent.get("batch")
                      or _report_cfg().get("batch") or None)
        self.run_config = config
        self.run_error = None
        self.pdf_path = None

        self.run_id = await Database.create_mini_run(parent_run_id, config, self._run_meta())
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
        # §7.6 队列照样记下来 —— mini-run 重连后也得能重建出「只重测了哪几项」。
        self.test_queue = [n for n, _ in enabled_tests]
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

    async def _run_one(
        self,
        name: str,
        cls: Type[TestCase],
        config: dict,
        write_to_parent: bool,
        attempt: int = 1,
    ) -> Tuple[TestResult, Optional[TestCase]]:
        """跑**一遍** name 这一项, 返回 (结果, 实例)。

        拆出来是因为 §7.8 要能原地重跑同一项 —— 「插上卡了, 再试一次」
        走的是同一个实例的第二次 run(), 而不是开一个 mini-run。
        返回实例是因为只有它才知道这个结果要不要问操作员。

        attempt: 第几次尝试 (§7.8 的重试)。**不是 1 就往已有日志后面接** ——
        头一次为什么不行(卡槽空的 / 网线没插), 恰恰是复盘时最要看的东西,
        只留最后一次就等于把前几次的证据扔了。
        """
        self.current_test = name
        # 实例先建出来: 要问它「本项预计多少秒」(见 TestCase.expected_seconds)。
        # 放在 try 外面是有意的 —— 没有子类定义 __init__, 构造不会抛。
        instance: Optional[TestCase] = cls()
        test_cfg = config["tests"].get(name, {})
        try:
            expected_sec = instance.expected_seconds(test_cfg)
        except Exception:
            # 预计时长只是个「安慰性数字」, 算不出来绝不能影响测试本身
            traceback.print_exc()
            expected_sec = None
        self._current_expected_sec = expected_sec
        self._current_started_at = time.time()
        await self._broadcast({
            "type": "test_started",
            "name": name,
            # None = 这一项没有可预期的时长 (WiFi / GPU / 网口 / HDMI / 风扇,
            # 时长由驱动和环境决定)。前端据此决定**不显示**计时条 ——
            # 显示一个编出来的秒数比不显示更糟。
            "expected_sec": expected_sec,
        })
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

        # §7.8 的重试: 头一次的日志**接在后面**, 不是冲掉 —— 头一次为什么
        # 不行(卡槽空的 / 网线没插), 恰恰是复盘时最要看的东西。
        # (缓冲本身在 _run_tests_body 里, 进入这一轮 attempt 循环前清空。)
        if attempt > 1 and self._logs.get(name):
            self._logs[name].append(f"───── 第 {attempt} 次尝试 ─────")

        try:
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

        # 日志以**runner 留档的这份**为准。
        #
        # 各项自己写的 result.log 是残缺的(七项不写, 写了的是截断过的末尾
        # N 行), 而 runner 留档的是**每一行**经过 on_log 的原文 —— 跟工人
        # 屏幕上一模一样。报告要能当证据用, 就得是这份。
        captured = self._logs.get(name) or []
        if captured:
            result.log = "\n".join(captured)
        # 截断过就在指标里留个痕 —— 报告会把这句话印在那一项的日志后面。
        # 悄悄少一段 = 报告看起来是全的, 实际不是, 那比超长糟糕得多。
        if self._log_truncated.get(name):
            result.metrics["log_truncated"] = "是"
            result.metrics["log_kept_lines"] = LOG_MAX_LINES
            result.metrics["log_dropped_lines"] = self._log_truncated[name]

        common_fields = {
            "status": result.status,
            "finished_at": time.time(),
            "duration_sec": result.duration_sec,
            "metrics": result.metrics,
            "log": result.log,
            "error": result.error,
        }

        # 写 mini-run 自己的行。
        # **写库失败不许掀掉整轮** —— 结果照样广播给前端 (界面照样往下走),
        # 只是这一项没进历史。报告少一行远好过整个界面冻住: 工人看到的是
        # 「跑完了不出结果」, 什么线索都没有, 连是哪个测试出问题都无从判断。
        try:
            await Database.upsert_result(self.run_id, name, **common_fields)
            # mini-run 模式下就地更新 parent run 的对应行
            if write_to_parent and self.parent_run_id:
                # 父轮那一行是**追加**重测日志, 不是覆盖 —— 覆盖掉的话, 父轮
                # 报告里这一项的原始日志就没了, 父轮那份报告的日志也是残缺的
                # (父轮同样要能当证据用)。
                #
                # ⚠️ 追加的是「**基准** + 本项到目前为止的**全部**尝试」, 每次
                #    重写一遍, 不是一次次往后接。§7.8 重试时本项的日志会变长,
                #    一次次接的话头一次尝试的日志会在父轮里出现两遍。
                if name not in self._parent_log_base:
                    try:
                        self._parent_log_base[name] = (
                            (await Database.get_result_log(self.parent_run_id, name)) or ""
                        )
                    except Exception:
                        traceback.print_exc()
                        self._parent_log_base[name] = ""
                base = self._parent_log_base[name]
                if base:
                    parent_log = (base + "\n"
                                  + f"───── 以下是第 {self.run_id} 轮重测的日志 ─────\n"
                                  + result.log)
                else:
                    parent_log = result.log
                await Database.upsert_result(
                    self.parent_run_id, name, **{**common_fields, "log": parent_log}
                )
        except Exception as e:
            traceback.print_exc()
            await self._broadcast({
                "type": "log",
                "name": name,
                "line": f"⚠ 结果写库失败 (该项不会出现在历史里): {e!r}",
            })

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

        # 注意: failed/error **不在这里**置 any_failed —— §7.8 允许原地重测,
        # 这一遍没过不代表最终没过。标志由主循环拿到最终结果后才算。
        return result, instance

    async def _wait_for_operator(
        self, name: str, prompt: dict, attempt: int
    ) -> str:
        """§7.8 **真的停在原地**等操作员拍板。

        返回: "retest" / "skip" / "stop" / "timeout"。

        这是整个多项目流程里唯一一处「后端主动等」。之前没有它, 后端
        test_finished → 静默 3s → 下一项 test_started 一路跑到底, 工人
        还在弹框前犹豫, 后面两项已经跑完了 —— 于是屏幕停在 USB 上, 而
        HDMI 的结果早就写进库了, 日志和画面自然对不上。

        超时是必须的: 没人操作时自动往下走, 不能把整轮挂死。
        """
        flow_cfg = Config.get("flow", {}) or {}
        # **用 float, 不要 int** —— int(0.5) 是 0, 超时一旦被截成 0,
        # wait_for 会立刻抛 TimeoutError, 这个「停下来等」就等于没写,
        # 而且症状和原来的 bug 一模一样 (界面自己往下跑), 极难联想到配置。
        timeout = float(flow_cfg.get("operator_wait_sec", OPERATOR_WAIT_DEFAULT_SEC))

        event = asyncio.Event()
        # 局部持有这个槽位对象的引用: finally 里会把 self._operator_wait
        # 清成 None, 但 decision 要在清空**之前**读出来。
        slot = {
            "name": name,
            "text": prompt.get("text", ""),
            "attempt": attempt,
            "timeout_sec": int(timeout),
            "event": event,
            "decision": None,
        }
        self._operator_wait = slot
        await self._broadcast({
            "type": "waiting_operator",
            "name": name,
            "text": prompt.get("text", ""),
            "attempt": attempt,
            "max_retest": OPERATOR_MAX_RETEST,
            "timeout_sec": int(timeout),
        })

        decision = "skip"
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
            decision = slot["decision"] or "skip"
        except asyncio.TimeoutError:
            decision = "timeout"
            await self._broadcast({
                "type": "operator_wait_timeout",
                "name": name,
                "timeout_sec": int(timeout),
            })
        finally:
            self._operator_wait = None

        if self.cancelled:
            return "stop"
        return decision

    def answer_operator(self, decision: str) -> dict:
        """前端弹框上那两个按钮的落点: "retest" (已插好) / "skip" (跳过)。

        只是**唤醒**等待, 真正跑不跑下一项由 _run_tests 的主循环决定 ——
        决定只有这一个出口, 才不会出现「前端跳了、后端还在跑」那种错位。
        """
        w = self._operator_wait
        if w is None:
            return {"ok": False, "error": "当前没有在等操作员选择"}
        if decision not in ("retest", "skip"):
            return {"ok": False, "error": "decision 只能是 retest 或 skip"}
        if w.get("decision") is not None:
            # 已经拍过板了 (双击按钮 / 两个页签同时点)
            return {"ok": False, "error": "已经选择过了"}
        w["decision"] = decision
        w["event"].set()
        return {"ok": True, "decision": decision, "test": w["name"]}

    async def _run_tests(self, enabled_tests, config: dict, write_to_parent: bool):
        """外层护栏: 保证**一定会**发出 run_finished。

        没有这层时, 内部任何一处没接住的异常都会让整个任务静默死掉 —— 最常见
        的就是写库失败(比如某项的 metrics 里混进一个 json 序列化不了的对象),
        它发生在 `_run_one` 的 try 之外, 一抛就把 `_run_tests` 整个掀掉。

        后果是: 前端永远收不到 run_finished, 界面就停在最后一个测试的画面上
        不动, 工人看着像卡死; 而后端日志里**什么都没有** —— 任务是
        `create_task()` 起的, 没人 await 它, 异常对象直接被 GC 掉, 连
        "Task exception was never retrieved" 都不一定打出来。

        这一层把异常兜住、记进日志、并以 status=error 正常收尾, 让操作员
        至少能看到「这一轮出问题了」和原因, 而不是面对一个冻住的界面。
        """
        try:
            await self._run_tests_body(enabled_tests, config, write_to_parent)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            traceback.print_exc()
            print(f"[runner] 测试轮次异常终止: {e!r}", flush=True)
            self.status = "error"
            self.finished_at = time.time()
            self.current_test = None
            self.run_error = repr(e)
            try:
                await Database.finish_run(
                    self.run_id, "error", {"reason": repr(e)}
                )
            except Exception:
                pass
            await self._broadcast({
                "type": "run_finished",
                "status": "error",
                "summary": {"reason": repr(e)},
                "mode": self.mode,
                "parent_run_id": self.parent_run_id,
                # 总结页要显示「哪台机器、什么时候、出的什么事」(§7.2/§7.6)
                "sn": self.sn,
                "finished_at": self.finished_at,
                "error": self.run_error,
            })

    async def _run_tests_body(self, enabled_tests, config: dict, write_to_parent: bool):
        if not enabled_tests:
            self.status = "error"
            self.finished_at = time.time()
            await Database.finish_run(
                self.run_id, "error",
                {"reason": "未选择任何测试"}
            )
            await self._broadcast({
                "type": "run_finished",
                "status": "error",
                "summary": {"reason": "未选择任何测试"},
                "mode": self.mode,
                "parent_run_id": self.parent_run_id,
                "sn": self.sn,
                "finished_at": self.finished_at,
                "error": "未选择任何测试",
            })
            return

        await self._broadcast({
            "type": "tests_planned",
            "tests": [{"name": n, "display_name": c.display_name} for n, c in enabled_tests]
        })

        any_failed = False
        # 操作员刚在弹框上拍过板 → 下一项**立刻**开始, 不再等静默期。
        # 静默期是给「结果出来没来得及看」的工人留的; 他已经看着结果点过
        # 「跳过」了, 再闷 5 秒正是「点了跳过却像没反应」的那种卡顿。
        immediate_next = False

        for i, (name, cls) in enumerate(enabled_tests):
            # §7.6 静默期: 第一项前不需要, 其余每项跑完后等 N 秒
            if i > 0 and not immediate_next:
                ok = await self._inter_test_interval(
                    enabled_tests[i][0], INTERVAL_BETWEEN_TESTS_SEC
                )
                if not ok:
                    # 用户在静默期中止, 后面所有项 skipped
                    break
            immediate_next = False

            # ── §7.8: 本项可能被要求重测若干次 (插卡 / 插网线) ──
            attempt = 1
            final_status = "pending"
            # 本项的日志留档从零开始 —— 几次尝试的日志会连着留 (见 _run_one),
            # 但**不能**把上一项的日志带过来。
            self._logs[name] = []
            self._log_truncated.pop(name, None)
            while True:
                result, instance = await self._run_one(
                    name, cls, config, write_to_parent, attempt=attempt
                )
                final_status = result.status

                # 这一步要不要停下来问人?
                prompt = None
                if instance is not None:
                    try:
                        prompt = instance.operator_prompt(result)
                    except Exception:
                        prompt = None

                # 达到重测上限 → 不再问了, 按当前结果往下走
                if not prompt or attempt > OPERATOR_MAX_RETEST:
                    if prompt and attempt > OPERATOR_MAX_RETEST:
                        await self._broadcast({
                            "type": "log",
                            "name": name,
                            "line": (
                                f"[{cls.display_name}] 已重测 {OPERATOR_MAX_RETEST} 次仍"
                                f"未就位, 不再等待, 按 skipped 计"
                            ),
                        })
                    break

                decision = await self._wait_for_operator(name, prompt, attempt)
                if decision != "retest":
                    immediate_next = True    # skip / timeout / stop → 马上进下一项
                    break

                attempt += 1
                await self._broadcast({
                    "type": "test_retesting",
                    "name": name,
                    "attempt": attempt,
                    "max_retest": OPERATOR_MAX_RETEST,
                })

            if final_status in ("failed", "error"):
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

        # §8.2 收尾导出。**放在 finish_run 之后**: 万一导出卡住/失败, 库里
        # 这一轮已经是完整的 finished 状态, 历史页随时能看, 不受影响。
        await self._export_pdf(summary)

        await self._broadcast({
            "type": "run_finished",
            "status": self.status,
            "summary": summary,
            "mode": self.mode,
            "parent_run_id": self.parent_run_id,
            # 总结页顶部要印「哪台机器、什么时候跑的」(§7.2)。快照恢复出来
            # 的总结页尤其需要这个 —— 那个总结页可能是**上一轮**的结果, 不标
            # 出来工人会以为刚刚又测了一遍。
            "sn": self.sn,
            "finished_at": self.finished_at,
            "error": self.run_error,
        })

        # 重置 mini-run 标志
        if self.mode == "mini":
            self.mode = "full"
            self.parent_run_id = None

    def _run_meta(self) -> dict:
        """create_run / create_mini_run 共用的身份字段。"""
        return {"sn": self.sn, "operator": self.operator, "batch": self.batch}

    def _report_dict(self, summary: dict) -> dict:
        """喂给 report.export_pdf 的 run 结构 —— 字段名和 db.get_run 对齐。"""
        return {
            "id": self.run_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "status": self.status,
            "sn": self.sn,
            "operator": self.operator,
            "batch": self.batch,
            "mini_of": self.parent_run_id,
            "error": self.run_error,
            "summary": summary,
            "config": self.run_config,
        }

    def _results_as_dicts(self) -> list:
        return [
            {
                "test_name": name,
                "status": r.status,
                "duration_sec": r.duration_sec,
                "metrics": r.metrics,
                "log": r.log,
                "error": r.error,
            }
            for name, r in self.results.items()
        ]

    async def _export_pdf(self, summary: dict):
        """§8.2 收尾自动导出 PDF。

        **本方法不抛异常** —— §8.2 硬规则: 报告导出失败**不阻断**测试结果。
        结果早在 `_run_tests_body` 里就写完库了, 报告只是附加产物; 失败就
        记日志 + 推一条 warning, 让工人知道"这轮没生成报告", 历史页还能
        手动重导。绝不能因为导出失败就把已跑完的一轮标成 error。
        """
        try:
            path = await report.export_pdf(
                self._report_dict(summary), self._results_as_dicts()
            )
        except Exception as e:
            # 兜 Exception 而不是只 catch report.ReportError: 渲染模板时的
            # KeyError / 磁盘满 / 权限问题同样不该掀翻收尾流程。
            self.pdf_path = None
            print(f"[runner] ⚠ PDF 导出失败 (不影响测试结果): {e}", flush=True)
            await self._broadcast({
                "type": "report_failed",
                "reason": str(e),
                "run_id": self.run_id,
            })
            return
        self.pdf_path = path
        try:
            await Database.set_pdf_path(self.run_id, path)
        except Exception as e:
            # 报告文件已经在磁盘上了, 只是没记进 DB。**这不算导出失败** ——
            # 别去删已经生成的文件, 论文档靠文件系统还能找到。
            print(f"[runner] ⚠ PDF 已生成但回写 DB 失败: {e}  ({path})", flush=True)
        print(f"[runner] PDF 报告: {path}", flush=True)
        await self._broadcast({
            "type": "report_ready",
            "path": path,
            "run_id": self.run_id,
        })

    async def _on_progress(self, name: str, pct: float, msg: str):
        await self._broadcast({"type": "progress", "name": name, "pct": pct, "msg": msg})

    async def _on_log(self, name: str, line: str):
        # **完整留档** —— 报告的「日志附录」要的就是这个 (§8.2: 全量测试日志)。
        # 界面上看到的每一行, 报告里都得有 —— 屏幕上是全的、纸面上是半截的,
        # 那纸面这份就不能当证据用。
        buf = self._logs.setdefault(name, [])
        if len(buf) >= LOG_MAX_LINES:
            del buf[0]                       # 挤掉最早的一行, 保住最新的
            self._log_truncated[name] = self._log_truncated.get(name, 0) + 1
        buf.append(line)
        await self._broadcast({"type": "log", "name": name, "line": line})

    async def _on_metric(self, name: str, key: str, value):
        await self._broadcast({"type": "metric", "name": name, "key": key, "value": value})

    async def stop(self):
        if self.status != "running":
            return {"ok": False, "error": "没有正在运行的测试"}
        self.cancelled = True
        # §7.8: 正卡在「等操作员」时, 没有子进程可杀, 唤醒它的只有这一下。
        # 不唤醒的话「停止」会一直等到超时才生效, 工人看着什么都没发生,
        # 只好再点一次 —— 而第二次同样没反应。
        w = self._operator_wait
        if w is not None:
            w["decision"] = "stop"
            w["event"].set()
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
