"""SQLite 数据库 - 存测试历史

Schema:
  runs: 每次完整运行 (含 mini-run, mini_of 指向父 run)
  results: 每次运行的逐项结果
"""
import aiosqlite
import json
import time
from pathlib import Path
from typing import Any, Optional

from .config import Config
from . import boot_evidence
from . import clock


#: 历史库位置 —— 落**数据根**下, 不是代码根。
#: (打包成 deb 后代码归 root、工人写不动, 数据根才是 /var/lib/aging-test/)
DB_PATH = Config.data_root() / "reports" / "history.db"

#: 导入时算出来的那个值 —— 用来分辨「这个路径是**我们**算的」还是
#: 「**有人**(回归测试)改过的」⇒ 只在是我们算的时候才让它跟着数据根走。
_AUTO_DB_PATH = DB_PATH

#: 坏库隔离件的文件名后缀 —— **隔离 ≠ 删除**: 旧库还在盘上, 人工能捞。
CORRUPT_SUFFIX = ".corrupt-"

#: 体检时库超过这个大小就**不查** —— `quick_check` 要全扫一遍, 太大的库
#: 扫完会拖慢启动(工人双击完等着, 以为没反应)。超过这个大小就只靠
#: 运行时报错兜底。
PROBE_MAX_BYTES = 256 * 1024 * 1024

#: 体检结果里「**被占着**(不是坏了)」的标记前缀 —— 用它把「locked」和
#: 「损坏」分开: locked 的库**一个字都不许动**(改了就是好板子被冤枉,
#: 工人整盘历史归零)。
LOCKED_PREFIX = "locked"
LOCKED_REASON = LOCKED_PREFIX + ": 库正被别的进程占着(不是坏了) —— %s"


def _looks_locked(exc: Exception) -> bool:
    """这个异常是不是「库被占着」(而不是「库坏了」)。

    SQLite 报占用的说法有好几种:
      · `database is locked`        —— 别的进程持有写锁
      · `database table is locked`  —— 别的进程正写同一张表
      · `database is busy`          —— 忙
    ⇒ 一律按「占着」处理, **不隔离**。
    """
    msg = str(exc).lower()
    return ("locked" in msg) or ("busy" in msg)


def is_locked_reason(reason: str) -> bool:
    """这条体检结论是不是「被占着」(= 不是坏了)。"""
    return bool(reason) and reason.startswith(LOCKED_PREFIX)

#: 本次启动隔离过库的话记在这儿 —— 给 `/api/config` 报, 界面挂横幅。
QUARANTINED: list = []


def _db_path() -> Path:
    """当前该用的库路径。

    - **回归测试会直接把 `DB.DB_PATH` 改成临时路径**(隔离真库 ——
      产线在跑的那份历史一个字都不许碰)⇒ 被人改过就**以人为准**, 绝不顶掉;
    - 否则按数据根**实时**算 ⇒ U 盘挂载点漂移了能跟上
      (数据根会按设备重新定位, 见 `Config.rebind`)。
    """
    if DB_PATH != _AUTO_DB_PATH:
        return DB_PATH
    return Config.data_root() / "reports" / "history.db"


SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at REAL NOT NULL,
    finished_at REAL,
    status TEXT NOT NULL,        -- running / passed / failed / stopped / error
    config_json TEXT,
    summary_json TEXT,
    mini_of INTEGER,            -- mini-run 模式: 指向父 run id (NULL = full run)
    error TEXT,                 -- 系统级错误(若有)
    sn TEXT,                    -- §2.2 被测设备 SN, 也用于 PDF 文件名
    operator TEXT,              -- 测试人员姓名(§8.2 报告头「测试人员」行)
    batch TEXT,                 -- 批次号
    pdf_path TEXT,              -- §8.2 导出后的 PDF 路径(NULL = 还没导出/导出失败)
    updated_at REAL,            -- 内容最近一次变更的时间(合并重测时刷新)
    retest_count INTEGER DEFAULT 0  -- 被重测合并过几轮(「第 N 次重测通过」)
);

CREATE TABLE IF NOT EXISTS results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    test_name TEXT NOT NULL,
    status TEXT NOT NULL,        -- pending / running / passed / failed / skipped / error
    started_at REAL,
    finished_at REAL,
    duration_sec REAL,
    metrics_json TEXT,
    log TEXT,
    error TEXT,
    attempt INTEGER DEFAULT 1, -- 这一项是第几次尝试(1 = 首测, 2 = 第一次重测)
    FOREIGN KEY (run_id) REFERENCES runs(id)
);

CREATE INDEX IF NOT EXISTS idx_results_run ON results(run_id);
CREATE INDEX IF NOT EXISTS idx_runs_started ON runs(started_at DESC);
-- idx_runs_mini_of 在 init() 里另行创建, 因为依赖 migration 后才有的 mini_of 列
"""


async def _ensure_column(db, table: str, column: str, typedef: str):
    """Idempotent ALTER TABLE ADD COLUMN (SQLite 没有 IF NOT EXISTS)."""
    cur = await db.execute(f"PRAGMA table_info({table})")
    cols = {row[1] for row in await cur.fetchall()}
    if column not in cols:
        await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {typedef}")


def _quarantine_prefix(path: Path) -> str:
    return path.name + CORRUPT_SUFFIX


def _pdf_path_for_db(pdf_path: str) -> str:
    """存进库的 PDF 路径 —— **相对数据根**, 不是绝对路径。

    ⚠️ **为什么**: 绝对路径会**跟着挂载点一起失效** ——
       U 盘昨天挂在 `/media/User/AGING`, 今天挂在 `/media/User/AGING1`
       ⇒ 库里那条 `/media/User/AGING/reports/pdf/x.pdf` 就指向一个**不存在**
       的地方 ⇒ 工人点「打开 PDF 报告」打不开(2026-11-11 实况: Nov 10 15:14
       那轮就是这样)。相对路径 ⇒ 挂到哪儿都对。

    不在数据根下的(比如 `output_dir` 指到别处)只能存绝对 —— 相对了就没法
    还原回去。
    """
    if not pdf_path:
        return pdf_path
    p = Path(pdf_path)
    if not p.is_absolute():
        return pdf_path          # 本来就是相对的 ⇒ 照旧
    try:
        root = Config.data_root().resolve()
        return str(p.resolve().relative_to(root))
    except (ValueError, OSError):
        return str(p)             # 不在数据根下 ⇒ 只能存绝对


def find_quarantined(path: Path) -> list:
    """盘上**已经**隔离过的库(新的在前) —— 那是「这根盘的历史重置过」的
    **永久证据**: 盘一直在, 下一个人接手也知道。

    ⚠️ 用**文件名**认, 不用 mtime 认 —— 拷盘/改时间戳会把它骗过去。
    """
    try:
        items = list(path.parent.glob(_quarantine_prefix(path) + "*"))
    except OSError:
        return []
    items.sort(key=lambda p: p.name, reverse=True)
    return items


async def probe_health(path: Path) -> tuple:
    """体检一下这个库还能不能正常用。返回 (好?, 说明)。

    ⚠️ **为什么要单独探**: 「启动没报错」**不等于**「库是好的」。
       坏掉的库有个阴险的坏法: 表和索引**都读得动**、写也**写得进**,
       但**读某几行的大字段会炸**(拔盘时把大字段的溢出页写了一半)。
       ⇒ 应用平时一切正常, 直到某一步恰好读到那几行才报
       `database disk image is malformed` —— 症状是「服务起得来、
       历史页能看, 一开测就报错」, 工人根本查不出哪儿坏了。

    ⚠️ **用读写方式打开**(不是只读): 上次是**写到一半被拔盘**的话, 盘上
       会留下一个「热日志」, 只读打开**没法回滚**(直接报错)⇒ 读写打开
       能让 **SQLite 自己把热日志回滚掉** —— 回滚成功的话这个库其实
       **是好的**, 根本不用隔离。

    ⚠️ **「被占着」和「坏了」要分开**(2026-11-11 查出来的): 体检撞上
       `database is locked`(上一个进程没退干净 / 别的工具开着它)时,
       **那不是损坏** ⇒ 结论是「好的, 但正被占着」, 结论字符串带
       `locked:` 前缀 ⇒ 上层**不会**去隔离它。
       (混为一谈 ⇒ 好端端的库被改名隔离 ⇒ 工人整盘历史归零。)
    """
    if not path.exists():
        return True, "新库(还没有)"          # 还没建过 ⇒ 没坏
    try:
        size = path.stat().st_size
    except OSError as e:
        return True, "读不到文件信息(%s), 先不动它" % e
    if size == 0:
        return True, "空文件(新建的)"        # 空库是合法的
    if size > PROBE_MAX_BYTES:
        return True, "太大(> %d MB), 不体检" % (PROBE_MAX_BYTES // 1024 // 1024)
    try:
        async with aiosqlite.connect(path) as conn:
            async with conn.execute("PRAGMA quick_check") as cur:
                row = await cur.fetchone()
            verdict = (row[0] if row else "") or ""
    except Exception as e:
        # ⚠️ **「被占着」≠「坏了」** —— 这是两个完全不同的事, 混在一起会
        #    **把好端端的库改名隔离掉** ⇒ 工人的历史记录整个归零, 而板子
        #    一点毛病都没有。
        #    「locked」的典型来源: 上一个服务进程还没退干净(重启竞态)、
        #    另一个工具正开着这个库、或者体检正好撞上别人在写。
        #    ⇒ 这种情况下**一律不动这个库**, 照常往上开。
        if _looks_locked(e):
            return True, LOCKED_REASON % e
        return False, "%s: %s" % (type(e).__name__, e)
    if verdict.strip().lower() == "ok":
        return True, "ok"
    return False, "quick_check: %s" % verdict


def isolate_corrupt_db(path: Path, reason: str) -> Optional[Path]:
    """把坏库**改名隔离** —— **不删**。

    为什么要留着: 坏的是**一部分行**(某几条大日志的溢出页写坏了),
    其余的轮次和结论**都是好的** ⇒ 直接删掉等于把能救的数据一起扔了。
    改名之后人工还能拿别的工具去捞。

    改名(而不是删)还顺手解决了**下次再坏**的问题: 隔离件叫
    `history.db.corrupt-<时间戳>`, 新库还是叫 `history.db` ⇒ 应用下次
    启动看到的是一个**干净的**库 ⇒ 不会反复隔离、也不会把坏件越攒越多。

    返回隔离件的路径; 隔离不了(没权限等)返回 None。
    """
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = path.with_name("%s%s%s" % (path.name, CORRUPT_SUFFIX, stamp))
    try:
        # 先把 WAL / 共享内存 / 热日志也一并挪走 —— 留着它们, 新库建起来时
        # SQLite 会拿旧日志去回滚 ⇒ 反而更糟。
        for suffix in ("-wal", "-shm", "-journal"):
            side = path.with_name(path.name + suffix)
            if side.exists():
                try:
                    side.rename(dest.with_name(dest.name + suffix))
                except OSError:
                    pass
        path.rename(dest)
    except OSError as e:
        print("[db] ⚠⚠️ 历史库坏了, 但**隔离失败**(%s) —— 库留在原处: %s"
              % (e, path))
        return None
    return dest


async def _isolate_if_corrupt(path: Path) -> None:
    """体检不过就把坏库隔离掉, 另起一个空的。

    ⚠️ **「被占着」不是「坏了」** —— 库被别人开着(上一个进程没退干净、
       别的工具开着它)的时候, 体检会撞上 `database is locked`。
       那**不是损坏** ⇒ **一个字都不许动它** —— 隔离掉的话工人整盘历史
       归零, 而板子一点毛病都没有 ⇒ **冤枉好板子**。
    """
    ok, reason = await probe_health(path)
    if ok:
        if is_locked_reason(reason):
            print("[db] ⚠ 历史库正被别的进程占着(locked, **不是坏了**) —— "
                  "先不动它: %s" % reason)
        return
    dest = isolate_corrupt_db(path, reason)
    if dest is None:
        return
    info = {
        "file": dest.name,
        "path": str(dest),
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "reason": reason,
    }
    QUARANTINED.append(info)
    print("[db] ⚠⚠️ 历史库损坏, 已**改名隔离**(没有删): %s" % dest.name)
    print("[db]    原因: %s" % reason)
    print("[db]    ⇒ 历史记录从这一刻起重新开始; 旧库就躺在 reports/%s, "
          "要捞数据拿别的工具去捞。" % dest.name)


def _quarantined_at(path: Path) -> str:
    """隔离件**被隔离的时刻**(显示给人看的时间)。

    ⚠️ **不能拿文件 mtime 当隔离时刻** —— 隔离是**改名**, 改名**不改 mtime**
       ⇒ 那是**旧库最后一次被写入**的时间(实况: 隔离发生在 11-10 14:45,
       mtime 却还是 11-09 17:31)⇒ 横幅会跟工人说「这根盘在 11-09 坏过」,
       而实际是 11-10 隔离的 —— 时间对不上, 排查时会被带偏。

    隔离时刻本来就**写在文件名里**(`history.db.corrupt-<YYYYmmdd-HHMMSS>`)
    ⇒ 从文件名取; 名字对不上(手工改过?)才退回 mtime。
    """
    stamp = path.name.rsplit(CORRUPT_SUFFIX, 1)[-1]
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S",
                             time.strptime(stamp, "%Y%m%d-%H%M%S"))
    except ValueError:
        try:
            return time.strftime("%Y-%m-%d %H:%M:%S",
                                 time.localtime(path.stat().st_mtime))
        except OSError:
            return "?"


def history_db_status() -> dict:
    """历史库的状态 —— 给界面挂横幅用。

    · `quarantined`: **这次启动**隔离过坏库(⇒ 工人该知道历史从头开始了)
    · `previous`:   这根盘上**以前**隔离过(隔离件就躺在 reports/ 里 ——
                     那是永久证据, 换个人接手这根盘也该知道)
    """
    path = _db_path()
    old = find_quarantined(path)
    return {
        "quarantined": QUARANTINED[-1] if QUARANTINED else None,
        "previous": [{"file": p.name, "at": _quarantined_at(p)}
                     for p in old[:3]],
        "db_path": str(path),
    }


async def init():
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # ⚠️ **先体检再开**: 坏库会被改名隔离(见 `_isolate_if_corrupt`)。不带一个
    #    「表能读、写几行才炸」的库去跑 —— 那症状是「服务起得来、历史页能看,
    #    一开测就报错」, 工人查不出哪儿坏了。
    await _isolate_if_corrupt(path)
    async with aiosqlite.connect(path) as db:
        # 1. CREATE TABLE (对老 DB 是 no-op)
        await db.executescript(SCHEMA)
        # 2. Migrations: 补加老 DB 缺的列
        await _ensure_column(db, "runs", "mini_of", "INTEGER")
        await _ensure_column(db, "runs", "error", "TEXT")
        await _ensure_column(db, "runs", "sn", "TEXT")
        await _ensure_column(db, "runs", "operator", "TEXT")
        await _ensure_column(db, "runs", "batch", "TEXT")
        await _ensure_column(db, "runs", "pdf_path", "TEXT")
        # §7.7 合并重测: updated_at 决定历史列表的排序(被重测合并过的记录
        # 要能跳到最上面), retest_count 记「第几次重测通过」。
        await _ensure_column(db, "runs", "updated_at", "REAL")
        await _ensure_column(db, "runs", "retest_count", "INTEGER DEFAULT 0")
        # §7.7 逐项「第 N 次测试通过」: 这一项是第几次尝试。
        await _ensure_column(db, "results", "attempt", "INTEGER DEFAULT 1")
        # 3. 现在 mini_of 一定存在了, 创建依赖它的索引
        try:
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_runs_mini_of ON runs(mini_of)"
            )
        except aiosqlite.OperationalError:
            pass  # 不应该走到这里, 但防御性兜底
        await db.commit()


def decide_run_status(summary: dict) -> str:
    """轮级状态判定 —— **唯一**口径(§9.2 + §9.3)。

    口径跟 runner 算 summary 时**完全一致**(同一个定义, 不许各算各的):
      ① 有 `failed` / `error` 的项 → `failed`
         —— **排在最前面**: 已经测出来的失败是**真证据**(卡真的挂死过),
         工具自己的死(服务中断 / 工人按了暂停)**不许**把它抹掉 ——
         抹掉 = 坏板子被当成「没失败」放行(§9.3)。
      ② 有没跑到的项(`not_run > 0`) → `stopped`
         —— 没测过的项合不合格谁也不知道, 不能因为别的项过了就宣布
         「整体通过」(§7.7.2)。
      ②' **工人中止了整轮**(`aborted > 0`, 2026-10-29) → `stopped`
         —— 「停止」键跳掉的那些项**根本没测**, 整轮**没跑完**。
         ⚠️ 这一条**不能省**: 那些项记的是 `skipped`(用户主动让它别测),
         而 skipped 也可能是「TF 卡没插」那种环境原因 —— 光看 skipped 的
         计数**分不出来**。少了这一条, 「3 项测过都过了 + 工人按了停止
         剩下 7 项」会被判成 `passed` —— **假通过**: 一台只测了三项的机器
         拿着「整体通过」出货(§9.3: 静默的「通过」比明确的「失败」有害得多)。
      ③ 一项都没出结论(`total == 0`) → `skipped`(§9.2: **不得**判 passed)
      ④ 其余 → `passed`

    为什么单独拎出来一个函数: 这一段口径以前在**两个地方**各写了一遍
    (runner 收尾时算一次, `recompute_run` 重算时算一次), 两份迟早会走歪
    —— 走歪的后果是「这一轮判 stopped, 重测合并时又被重算成 passed」,
    界面和报告当场打架(§7.7.2 第 2 条)。现在只有这一个函数说了算。

    ⚠️ 它是**模块级**函数, 不是 `Database` 的方法 —— 测试里会把
    `Database` 整个换成假库(只实现存/取那几个动作); 口径要是挂在类上,
    假库也得抄一份, 抄的那份迟早跟真的走歪。挂在模块上, 假库换掉了它
    也不受影响 ✓。
    """
    failed = summary.get("failed", 0) or 0
    error = summary.get("error", 0) or 0
    not_run = summary.get("not_run", 0) or 0
    aborted = summary.get("aborted", 0) or 0
    total = summary.get("total", 0) or 0
    if failed or error:
        return "failed"
    if not_run > 0 or aborted > 0:
        return "stopped"
    if total == 0:
        return "skipped"
    return "passed"


def _metrics_skipped_reason(metrics_json) -> str:
    """从一行结果的 metrics_json 里取出 `skipped_reason`(取不到就空串)。

    用来分辨两种**都记 skipped** 的跳过:
      · `aborted_by_user` —— 工人按了「停止」, 整轮没跑完(§9.3);
      · 别的(环境原因 / 用户主动跳这一项)—— 整轮照常收尾。
    光看 status 分不出来, 所以每一行自己写了这个标记。
    """
    try:
        m = json.loads(metrics_json) if metrics_json else {}
    except (TypeError, ValueError):
        return ""
    if not isinstance(m, dict):
        return ""
    return m.get("skipped_reason") or ""


class Database:
    @staticmethod
    async def init():
        await init()

    @staticmethod
    async def create_run(config: dict, meta: dict = None) -> int:
        """创建一次完整的 run.

        `meta` 里的 sn / operator / batch 落库 —— PDF 文件名和报告头都要用。
        现在存下来, 以后换机器、重装、重启服务都还能补导历史报告。
        """
        m = meta or {}
        async with aiosqlite.connect(_db_path()) as db:
            cur = await db.execute(
                "INSERT INTO runs (started_at, status, config_json, sn, operator, batch, "
                "updated_at, retest_count) VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
                (clock.now(), "running", json.dumps(config, ensure_ascii=False),
                 m.get("sn"), m.get("operator"), m.get("batch"), clock.now())
            )
            await db.commit()
            return cur.lastrowid

    @staticmethod
    async def create_mini_run(parent_run_id: int, config: dict, meta: dict = None) -> int:
        """创建 mini-run, mini_of 指回父 run.

        同样收 meta: 重测轮的 PDF 也得有 SN, 否则文件名叫 `unknown_*.pdf`,
        归档时根本认不出是哪台机器。
        """
        m = meta or {}
        async with aiosqlite.connect(_db_path()) as db:
            cur = await db.execute(
                "INSERT INTO runs (started_at, status, config_json, mini_of, sn, operator, batch, "
                "updated_at, retest_count) VALUES (?, 'running', ?, ?, ?, ?, ?, ?, 0)",
                (clock.now(), json.dumps(config, ensure_ascii=False), parent_run_id,
                 m.get("sn"), m.get("operator"), m.get("batch"), clock.now())
            )
            await db.commit()
            return cur.lastrowid

    @staticmethod
    async def set_pdf_path(run_id: int, pdf_path: str):
        """§8.2: 导出成功后回写 PDF 路径。

        导出失败时**不要**调用 —— pdf_path 留空本身就代表"没导出",
        不用再造一个 error 列去记失败原因(日志和 UI warning 已经带了)。

        ⚠️ **存的是「相对数据根」的路径, 不是绝对路径** ——
           绝对路径会**跟着挂载点一起失效**: 盘昨天挂在 /media/User/AGING,
           今天挂在 /media/User/AGING1 ⇒ 库里那条绝对路径就指向一个不存在的
           地方 ⇒ 工人点「打开 PDF 报告」打不开(2026-11-11 实况)。
           相对路径 ⇒ 挂到哪儿都对。
           (不在数据根下的 —— 比如 output_dir 指到别处 —— 只能存绝对。)
        """
        async with aiosqlite.connect(_db_path()) as db:
            await db.execute(
                "UPDATE runs SET pdf_path=? WHERE id=?",
                (_pdf_path_for_db(pdf_path), run_id))
            await db.commit()

    @staticmethod
    async def finish_run(run_id: int, status: str, summary: dict, error: str = None):
        """收尾 run, 写 status / finished_at / summary / error."""
        async with aiosqlite.connect(_db_path()) as db:
            await db.execute(
                "UPDATE runs SET finished_at=?, status=?, summary_json=?, error=? WHERE id=?",
                (clock.now(), status, json.dumps(summary, ensure_ascii=False), error, run_id)
            )
            await db.commit()

    # ──────────────────── §7.7 合并重测 ────────────────────
    #
    # mini-run 跑完之后要把结果**合并回父轮**: 父轮的逐项结果已经被就地更新了,
    # 但父轮自己的 status / summary_json 还停在重测前那一份 —— 界面上逐项写着
    # 「通过」、汇总却还是「失败 3/5」, 报告也还是失败那版。下面的方法负责把
    # 父轮重算一遍并落库。

    @staticmethod
    async def fix_stale_running(run_id: int) -> int:
        """把残留的 running 行改成 stopped (§9.3 口径: 中途停止 = 没跑到)。

        什么时候会残留: mini-run 开跑时会把父轮对应项先写成 running(让界面别
        停在旧状态上), 万一这一轮中途被停, 那一行就永远停在 running ——
        而 running 不落在 §9.2 的任何一档里, 父轮的计数会凭空少一项, 汇总对不上账。

        返回改了几行。
        """
        async with aiosqlite.connect(_db_path()) as db:
            cur = await db.execute(
                "UPDATE results SET status='stopped', "
                "error=COALESCE(error, '重测中途停止, 本项未跑完') "
                "WHERE run_id=? AND status='running'",
                (run_id,)
            )
            await db.commit()
            return cur.rowcount or 0

    @staticmethod
    async def get_result_status(run_id: int, test_name: str) -> Optional[str]:
        """取某一轮里某一项当前的 status; 那一行压根不存在时返回 None。

        合并重测时要用它判「父轮那一项**有没有结论**」(§7.7「结论只能被结论
        替换」): 有结论(passed/failed/error)就不许被重测那一轮的「没测成」
        (skipped/stopped) 覆盖掉 —— 首测那条 failed 是**真证据**。
        """
        async with aiosqlite.connect(_db_path()) as db:
            cur = await db.execute(
                "SELECT status FROM results WHERE run_id=? AND test_name=?",
                (run_id, test_name)
            )
            row = await cur.fetchone()
        return row[0] if row else None

    @staticmethod
    async def finalize_interrupted_run(run_id: int,
                                       is_parent: bool = False,
                                       hard_reset: bool = False) -> Optional[dict]:
        """收尾一个「服务中途夭折」的轮次 —— 它的 `runs` 行还停在 `running`。

        收尾口径(§9.3「中途停止 = 没跑完」, **不冤枉被测设备**):
          · 这一轮仍在 `running` 的**逐项**结果 → `stopped`(没跑完, 没结论);
          · 轮级状态: 有 failed/error 的项 → `failed`(那些结论是真的, 不该被
            工具的死抹掉); 否则**有没跑到的项** → `stopped`; 再否则按 §9.2 算。

        `hard_reset=True` —— **板子是被硬拉死的**(上一次启动的日志里**没有
        关机序列** ⇒ 掉电 / PMIC 复位 / 看门狗复位 / `reboot -f` 这类不经过
        systemd 的强制重启), 不是有人重启了服务。这时候口径**反过来**:
          · 正在跑的那一项 → **failed**, 不是 stopped ——
            「板子在加压中被硬复位」= **板子没扛住** = **被测设备不合格**;
          · 轮级 error 改成「板子硬复位 —— 被测设备不合格」。
        ⚠️ **2026-11-02 之前没有这个分支** —— 板子死在 CPU 加压里(硬复位),
        报告一律写「服务重启, 这一轮中途夭折(没跑完) —— 不代表被测设备
        不合格」, **方向是反的**: 那等于**放行一块扛不住加压的板子**。
        ⚠️ 判据只认「日志里没有关机序列」这一条**硬证据** —— 拿不到日志
        (看不出来)时**一律按老口径**(stopped), 不冤枉板子, 也不冤枉工具。

        `is_parent=True` —— 收的是**重测轮的父轮**(夭折的是那个重测轮,
        父轮只是有一项被重测轮写成了 `running`)。这时候**只**做一件事:
        把父轮里卡在 `running` 的那一行收成 `stopped`。
        父轮的 `runs` 行(status / summary_json / error / updated_at)**一个字
        都不许动** —— 三个理由:
          · **张冠李戴**: 夭折的是重测轮, 父轮自己好好地跑完了; 给父轮盖上
            「服务重启, 这一轮中途夭折」是**把别人的事写到自己头上**;
          · **信息丢失**: 父轮原来那条 error(比如整轮异常终止的原因)是
            **父轮自己的历史**, 抹掉就再也查不到了;
          · **越权**: 父轮多半是**已判定**的轮次(重测只能从跑完的轮次发起),
            它的轮级结论不该被工具顺手改掉(启动收尾的授权范围写得很死:
            「不碰已判定的轮次」)。
        ⇒ 所以 `hard_reset` **只作用在被夭折的那一轮自己**(is_parent=False)。

        返回 {"status":..., "summary":..., "is_parent":...}; 轮次不存在返回 None。
        """
        async with aiosqlite.connect(_db_path()) as db:
            cur = await db.execute("SELECT id FROM runs WHERE id=?", (run_id,))
            row = await cur.fetchone()
            if not row:
                return None

            # ① 这一轮仍在 running 的逐项结果 → stopped。
            #    ⚠️ **不能**直接复用 fix_stale_running —— 它那句「重测中途停止」
            #    是给「工人按了停止」用的; 这里是**服务自己死了**, 说法不一样,
            #    操作员看到得知道是哪个原因。
            #
            #    硬复位时改成 **failed**: 板子是在**这一项**上被拉死的 ⇒
            #    这一项没通过 ⇒ **被测设备不合格**(而不是「工具没跑完」)。
            #    ⚠️ 授权范围(用户 2026-11-02 点名): 只处理服务启动时收尾的
            #    「卡在 running 的轮次」里**正在跑的那一项**; 不碰任何已判定
            #    的轮次、不删数据、不回头改已经收好尾的历史轮次。
            if hard_reset and not is_parent:
                await db.execute(
                    "UPDATE results SET status='failed', "
                    "error=COALESCE(error, ?) "
                    "WHERE run_id=? AND status='running'",
                    (boot_evidence.HARD_RESET_ITEM_ERROR, run_id)
                )
            else:
                await db.execute(
                    "UPDATE results SET status='stopped', "
                    "error=COALESCE(error, ?) "
                    "WHERE run_id=? AND status='running'",
                    (boot_evidence.SOFT_ITEM_ERROR, run_id)
                )
            await db.commit()

        # ② 按 §9.2 重算
        #
        # ⚠️ 这里**不再**自己判一次(2026-10-28 删掉): 以前这里有一段「兜底」
        # (「有没跑到的项 → 盖成 stopped」), 那是给 recompute 的老口径打的补丁 ——
        # 老口径把这条挂在「这一轮上一回是 stopped」上, 而被服务中断的那一轮
        # 上一回可能是 failed ⇒ 补丁才补上。现在 recompute 自己就不挂任何前提
        # (见 recompute_run 的 ②), 这段兜底成了**空操作** —— 留着它等于留了
        # 一段「改不动、也看不出」的代码(变异测试咬不住: 改了照样绿)。
        # 口径只留一处(§9.2 + §9.3), 免得两处各算各的。
        out = await Database.recompute_run(run_id)
        if not out:
            return None
        summary = out["summary"]
        status = out["status"]

        async with aiosqlite.connect(_db_path()) as db:
            if is_parent:
                # 父轮: 逐项收完了(上面那句 UPDATE 已经执行), 但父轮的
                # `runs` 行**一个字都不动** —— 它的轮级结论是它自己的。
                # 只把父轮现在的状态/汇总**读出来**照实返回(调用方要照实
                # 印出来, 别把重算出来的值当成父轮现在的状态)。
                cur = await db.execute(
                    "SELECT status, summary_json FROM runs WHERE id=?", (run_id,)
                )
                prow = await cur.fetchone()
                try:
                    psummary = json.loads(prow[1]) if prow and prow[1] else {}
                except (TypeError, ValueError):
                    psummary = {}
                return {
                    "status": prow[0] if prow else status,
                    "summary": psummary if isinstance(psummary, dict) else {},
                    "is_parent": True,
                }

            await db.execute(
                "UPDATE runs SET status=?, summary_json=?, error=?, updated_at=? "
                "WHERE id=?",
                (status, json.dumps(summary, ensure_ascii=False),
                 boot_evidence.HARD_RESET_RUN_ERROR if hard_reset
                 else boot_evidence.SOFT_RUN_ERROR,
                 clock.now(), run_id)
            )
            await db.commit()
        return {"status": status, "summary": summary, "is_parent": False}

    @staticmethod
    async def list_interrupted_runs() -> list:
        """列出卡在 `running` 的轮次 —— **只读, 什么都不改**。

        卡在「运行中」只有一个原因: 上一轮服务**中途夭折**了(设备卡住把事件
        循环冻死 / 被 kill / 断电)。进程早没了, 只是那一行的状态没人收尾 ——
        界面上就一直显示「▶ 运行中」, 让人以为还有测试在跑。
        """
        async with aiosqlite.connect(_db_path()) as db:
            cur = await db.execute(
                "SELECT id, sn, status, started_at, mini_of FROM runs "
                "WHERE status='running' ORDER BY id"
            )
            rows = await cur.fetchall()
        return [
            {"id": r[0], "sn": r[1], "status": r[2],
             "started_at": r[3], "mini_of": r[4]}
            for r in rows
        ]

    @staticmethod
    async def reconcile_interrupted_runs(hard_reset: bool = False) -> list:
        """服务启动时收尾所有「上一轮服务中途夭折」的轮次。

        什么时候会有残留: 服务在跑测试的途中**死掉**了 —— 冻死(设备卡住,
        同步 I/O 堵在事件循环里, 2026-10-28 实测)、被 kill、断电。那一轮的
        `runs` 行会永远停在 `running`: 历史列表于是**一直显示「运行中」**,
        明明什么都没在跑(那次实测挂了一整天, 工人以为测试还在跑)。

        `hard_reset=True` —— 上一次是**硬复位**(板子被硬拉死, 不是有人
        重启了服务)⇒ 正在跑的那一项判 **failed**(被测设备不合格),
        而不是 stopped。判据与授权范围见 `finalize_interrupted_run`。

        mini-run(重测轮)被中断时, **父轮**里对应的行也停在 `running`
        (mini-run 开跑时会把父轮对应项先写成 running, 见 _merge_into_parent)
        ⇒ 父轮里那一行也要收, 否则父轮会出现「永远 running 的一项」(它在
        §9.2 的任何一档里都不算 ⇒ 父轮的计数凭空少一项)。
        ⚠️ 父轮**只收那一行**: 它的轮级结论(status / error)是**父轮自己的**
        —— 夭折的是重测轮, 不能把父轮已经判定的结论改掉(见
        finalize_interrupted_run 的 `is_parent` 说明)。

        返回被收尾的轮次列表 [{"id":..., "status":..., "summary":...}]。
        """
        async with aiosqlite.connect(_db_path()) as db:
            cur = await db.execute(
                "SELECT id, mini_of FROM runs WHERE status='running' ORDER BY id"
            )
            rows = await cur.fetchall()

        out = []
        done = set()
        for run_id, mini_of in rows:
            # 夭折的那一轮自己: 按「整轮收尾」处理(轮级 status/error 要写)。
            # 它的父轮: 只收父轮里卡在 running 的那一行(轮级结论不动)。
            for rid, is_parent in ((run_id, False), (mini_of, True)):
                if not rid or rid in done:
                    continue
                done.add(rid)
                r = await Database.finalize_interrupted_run(
                    rid, is_parent=is_parent, hard_reset=hard_reset)
                if r:
                    r["id"] = rid
                    out.append(r)
        return out

    @staticmethod
    async def recompute_run(run_id: int) -> Optional[dict]:
        """按 §9.2 口径重算某一轮的 status + summary。

        口径跟 runner._run_tests_body 里的 summary **完全一致**(同一个定义,
        不许各算各的):
          · total = passed + failed + error —— skipped / stopped **不计入**
          · 有 failed / error → failed
          · **有没跑到的项 → stopped** —— 没测过的项合不合格谁也不知道,
            不能因为别的项过了就宣布「整体通过」(§7.7.2)
          · total == 0 → skipped(**不得**判 passed —— 否则全跳过能拿一张空报告)
          · 否则 → passed

        ⚠️「有没跑到的项 → stopped」这条**不许**挂在「这一轮上一回是什么状态」
        上(2026-10-28 修): 挂上去的话, 父轮上一回判的是 failed, 而重测那一轮
        中途夭折(那一项记 stopped)时, 这条判不出来 ⇒ 整轮落进 passed ——
        那是**假通过**: 没测过的项合不合格谁也不知道, 好板子/坏板子一起放行。
        父轮上一回是什么状态跟「这一轮有没有没跑到的项」**无关**。

        返回 {"status":..., "summary":{...}} 或 None(轮次不存在)。
        """
        async with aiosqlite.connect(_db_path()) as db:
            cur = await db.execute(
                "SELECT config_json FROM runs WHERE id=?", (run_id,)
            )
            row = await cur.fetchone()
            if not row:
                return None
            config_json = row[0]

            cur = await db.execute(
                "SELECT test_name, status, metrics_json FROM results WHERE run_id=?",
                (run_id,)
            )
            rows = await cur.fetchall()

            counts = {"passed": 0, "failed": 0, "error": 0,
                      "skipped": 0, "not_run": 0, "aborted": 0}
            ran = set()
            for name, st, metrics_json in rows:
                ran.add(name)
                if st == "passed":
                    counts["passed"] += 1
                elif st == "failed":
                    counts["failed"] += 1
                elif st == "error":
                    counts["error"] += 1
                elif st == "skipped":
                    counts["skipped"] += 1
                    # 「因用户中止整轮而跳过」的项(2026-10-29)—— 整轮仍判
                    # stopped: 那些项**根本没测**, 不能因为别的项过了就宣布
                    # 整体通过(§9.3: 假通过最有害)。
                    # ⚠️ 光看 status 分不出来 —— skipped 也可能是「TF 卡没插」
                    # 那种环境原因, 那种**不该**把整轮判成 stopped。所以要读
                    # 那一行自己写的标记。
                    if _metrics_skipped_reason(metrics_json) == "aborted_by_user":
                        counts["aborted"] += 1
                elif st == "stopped":
                    counts["not_run"] += 1
                # 'running' / 'pending' 不落进任何一档 —— 它们不是结论。
                # (残留的 running 由 fix_stale_running 先清掉)

            # planned = 这一轮**勾选**了几项。
            # 从 config 里数(enabled 或实际跑过的), 跟报告「共勾选 K」同一口径 ——
            # 两处数字对不上是最容易被工人当场发现的破绽。
            try:
                cfg = json.loads(config_json) if config_json else {}
            except (TypeError, ValueError):
                cfg = {}
            test_cfgs = (cfg.get("tests") if isinstance(cfg, dict) else None) or {}
            planned_names = set()
            for name, tv in test_cfgs.items():
                if not isinstance(tv, dict):
                    continue
                if tv.get("enabled") or name in ran:
                    planned_names.add(name)
            # 勾了但**连行都没有**的项 = 没跑到(中途被停的) → 算 not_run
            counts["not_run"] += len(planned_names - ran)

            total = counts["passed"] + counts["failed"] + counts["error"]
            summary = {
                "total": total,
                "passed": counts["passed"],
                "failed": counts["failed"],
                "error": counts["error"],
                "skipped": counts["skipped"],
                "not_run": counts["not_run"],
                # 「工人中止整轮」而跳掉的项数 —— 跟 runner 算的**同一个口径**
                # (§9.3)。父轮被中止过、而重测只补了其中一部分时, 整轮仍判
                # stopped: 那些项还是没测过。
                "aborted": counts["aborted"],
                # 老库的 config 里可能压根没有 enabled(那时候按
                # `cfg.get("enabled", False)` 判定) —— 这时用「已测 + 跳过 +
                # 没跑到」兜底, 跟报告 render_html 里的回退一致。
                "planned": (len(planned_names)
                            or (total + counts["skipped"] + counts["not_run"])),
            }

            # 轮级判定(§9.2 + §9.3) —— 口径只留一处:
            # `decide_run_status()` 跟 runner 收尾时用的是**同一个函数**
            # (见那个函数的说明: 两处各写一遍, 迟早走歪)。
            #
            # ⚠️ 特别提醒(2026-10-28 修): 「有没跑到的项 → stopped」这条
            # **跟这一轮上一回是什么状态无关** —— 以前它挂在
            # old_status=='stopped' 上, 于是「父轮判 failed + 重测那一轮中途
            # 夭折」这一种漏判 ⇒ 整轮落进 passed —— **假通过**, 坏板子被当
            # 合格品放行。
            status = decide_run_status(summary)

            return {"status": status, "summary": summary}

    @staticmethod
    async def apply_merge(run_id: int, status: str, summary: dict) -> int:
        """把合并后的 status / summary 写回父轮, 并记一次重测。

        返回这一轮累计的重测轮数(retest_count)—— 界面上标「第 N 次重测通过」
        用的就是它。

        updated_at 一起刷: 历史列表按它排序, 被合并过的记录要能跳到最上面
        (它刚刚才动过, 是列表里最新的状态)。
        """
        async with aiosqlite.connect(_db_path()) as db:
            await db.execute(
                "UPDATE runs SET status=?, summary_json=?, updated_at=?, "
                "retest_count=COALESCE(retest_count,0)+1 WHERE id=?",
                (status, json.dumps(summary, ensure_ascii=False), clock.now(), run_id)
            )
            await db.commit()
            cur = await db.execute(
                "SELECT retest_count FROM runs WHERE id=?", (run_id,)
            )
            row = await cur.fetchone()
            return int(row[0]) if row and row[0] else 1

    @staticmethod
    async def upsert_result(run_id: int, test_name: str, **fields):
        """有就更新, 没有就插入."""
        now = clock.now()
        async with aiosqlite.connect(_db_path()) as db:
            cur = await db.execute(
                "SELECT id FROM results WHERE run_id=? AND test_name=?",
                (run_id, test_name)
            )
            row = await cur.fetchone()

            if row:
                sets = []
                vals = []
                for k, v in fields.items():
                    if k == "metrics":
                        sets.append("metrics_json=?")
                        vals.append(json.dumps(v, ensure_ascii=False) if v else None)
                    else:
                        sets.append(f"{k}=?")
                        vals.append(v)
                vals.append(row[0])
                await db.execute(f"UPDATE results SET {', '.join(sets)} WHERE id=?", vals)
            else:
                await db.execute(
                    """INSERT INTO results
                       (run_id, test_name, status, started_at, metrics_json, log, error, duration_sec, finished_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        run_id, test_name,
                        fields.get("status", "pending"),
                        fields.get("started_at", now),
                        json.dumps(fields.get("metrics"), ensure_ascii=False) if fields.get("metrics") else None,
                        fields.get("log"),
                        fields.get("error"),
                        fields.get("duration_sec"),
                        fields.get("finished_at")
                    )
                )
            await db.commit()

    @staticmethod
    async def list_runs(limit: int = 50, include_mini: bool = False) -> list:
        """列出最近 N 次 run —— **默认只列归档记录(不含重测轮)**。

        带出 sn / pdf_path: 历史列表要显示 SN 列, 并据 pdf_path 决定
        「打开 PDF」还是「重新导出 PDF」按钮。

        ⚠️ **重测轮(mini-run)默认不列** —— 它是**过程**, 不是归档对象
        (§7.7.4: 归档对象始终是最初那一轮)。列出来的话, 工人重测两次
        就会在列表里看见**四条**带「重测」字样的行(两条是原来那两条记录,
        两条是重测这个过程本身), 看着像"四条记录都重测过" —— 而实际只
        重测过两条。重测轮的信息**不丢**: 它挂在它那一轮记录的**详情**里
        (见 get_run 的 retest_runs), 写清哪一轮、什么时候、重测了哪几项、
        结果如何。

        include_mini=True 给排障用(看原始行); 界面不用。

        排序键是 **COALESCE(updated_at, started_at)** 而不是 started_at:
        被重测合并过的记录, 内容刚刚才更新过 —— 它是列表里**最新的状态**,
        该排在最上面; 可它的 started_at 还是当初那轮老时间, 按它排的话这条
        记录会永远沉在老位置, 工人刚合完却找不到它。
        """
        where = "" if include_mini else " WHERE mini_of IS NULL "
        async with aiosqlite.connect(_db_path()) as db:
            cur = await db.execute(
                "SELECT id, started_at, finished_at, status, summary_json, mini_of, error, "
                "sn, pdf_path, updated_at, retest_count "
                "FROM runs" + where +
                " ORDER BY COALESCE(updated_at, started_at) DESC LIMIT ?",
                (limit,)
            )
            rows = await cur.fetchall()
            return [
                {
                    "id": r[0], "started_at": r[1], "finished_at": r[2],
                    "status": r[3],
                    "summary": json.loads(r[4]) if r[4] else None,
                    "mini_of": r[5],
                    "error": r[6],
                    "sn": r[7],
                    "pdf_path": r[8],
                    "updated_at": r[9],
                    "retest_count": r[10] or 0,
                }
                for r in rows
            ]

    @staticmethod
    async def get_run(run_id: int) -> Optional[dict]:
        """单次 run 详情: 元数据 + 全部 results.

        结果里带 sn/operator/batch/pdf_path —— 手动补导 PDF 时正好复用这份
        run dict, 不用再回头查一遍设备身份。
        """
        async with aiosqlite.connect(_db_path()) as db:
            cur = await db.execute(
                "SELECT id, started_at, finished_at, status, config_json, summary_json, "
                "mini_of, error, sn, operator, batch, pdf_path, updated_at, retest_count "
                "FROM runs WHERE id=?",
                (run_id,)
            )
            row = await cur.fetchone()
            if not row:
                return None
            run = {
                "id": row[0], "started_at": row[1], "finished_at": row[2],
                "status": row[3],
                "config": json.loads(row[4]) if row[4] else None,
                "summary": json.loads(row[5]) if row[5] else None,
                "mini_of": row[6],
                "error": row[7],
                "sn": row[8],
                "operator": row[9],
                "batch": row[10],
                "pdf_path": row[11],
                "updated_at": row[12],
                "retest_count": row[13] or 0,
            }
            # ORDER BY id —— 逐项按**首次执行的顺序**排(报告的逐项段、
            # 合并后的结果列表都靠这个顺序), 不靠 SQLite 碰巧怎么扫表。
            cur = await db.execute(
                "SELECT test_name, status, started_at, finished_at, duration_sec, "
                "metrics_json, log, error, attempt FROM results WHERE run_id=? "
                "ORDER BY id",
                (run_id,)
            )
            results = await cur.fetchall()
            run["results"] = [
                {
                    "test_name": r[0], "status": r[1],
                    "started_at": r[2], "finished_at": r[3],
                    "duration_sec": r[4],
                    "metrics": json.loads(r[5]) if r[5] else None,
                    "log": r[6], "error": r[7],
                    # 第几次尝试(1 = 首测)。老库没这列时是 NULL → 当 1。
                    "attempt": r[8] or 1,
                }
                for r in results
            ]
        # §7.7 这一轮底下有几轮重测 —— **重测轮不单独占历史列表的一行**
        # (见 list_runs), 它的信息挂在这里, 详情里写清「哪一轮、重测了
        # 哪几项、结果如何」。出了 with 再查 —— 少开一个连接, 也免得嵌套。
        run["retest_runs"] = await Database.get_retest_runs(run_id)
        return run

    @staticmethod
    async def get_result_log(run_id: int, test_name: str) -> Optional[str]:
        """取某一项已落库的日志。"""
        async with aiosqlite.connect(_db_path()) as db:
            cur = await db.execute(
                "SELECT log FROM results WHERE run_id=? AND test_name=?",
                (run_id, test_name)
            )
            row = await cur.fetchone()
            return row[0] if row else None

    @staticmethod
    async def get_retest_runs(parent_id: int) -> list:
        """列出父轮底下的**全部重测轮**(mini_of = parent_id), 按 id 升序。

        §7.7: 重测轮的日志**不在**父轮那一行里 —— 父轮的 `log` 是首测的
        原始日志, 原封不动(那是返修时最要看的证据)。重测的日志留在**重测轮
        自己的行**里, 报告要印它的时候从这里读。

        返回 [{id, started_at, finished_at, status, results: [...]}, ...],
        每个 result 带 test_name / status / attempt / log。

        ⚠️ **只读** —— 这个方法不改任何数据。
        """
        async with aiosqlite.connect(_db_path()) as db:
            cur = await db.execute(
                "SELECT id, started_at, finished_at, status FROM runs "
                "WHERE mini_of=? ORDER BY id",
                (parent_id,)
            )
            runs = await cur.fetchall()
            out = []
            for rid, started, finished, status in runs:
                cur = await db.execute(
                    "SELECT test_name, status, attempt, log FROM results "
                    "WHERE run_id=? ORDER BY id",
                    (rid,)
                )
                rows = await cur.fetchall()
                out.append({
                    "id": rid,
                    "started_at": started,
                    "finished_at": finished,
                    "status": status,
                    "results": [
                        {
                            "test_name": r[0],
                            "status": r[1],
                            "attempt": r[2] or 1,
                            "log": r[3],
                        }
                        for r in rows
                    ],
                })
            return out

    @staticmethod
    async def get_result_attempts(run_id: int) -> dict:
        """取某一轮逐项的 attempt(这一项是第几次尝试)。

        重测时用: 父轮里这一项已经试过 N 次, 这次重测就是第 N+1 次 ——
        「第 2 次测试通过」那个记号就是这么来的。
        """
        async with aiosqlite.connect(_db_path()) as db:
            cur = await db.execute(
                "SELECT test_name, attempt FROM results WHERE run_id=?", (run_id,)
            )
            rows = await cur.fetchall()
            return {r[0]: (r[1] or 1) for r in rows}

    @staticmethod
    async def find_incomplete_results(run_id: int, statuses: list) -> list:
        """返回指定 run 中状态匹配的 test_name 列表. 用于重测时筛 failed 项."""
        async with aiosqlite.connect(_db_path()) as db:
            cur = await db.execute(
                "SELECT test_name FROM results WHERE run_id=? AND status IN "
                "(" + ",".join("?" * len(statuses)) + ")",
                [run_id] + statuses
            )
            rows = await cur.fetchall()
            return [r[0] for r in rows]
