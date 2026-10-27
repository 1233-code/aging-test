"""回归测试: sudo 没配免密时, 要判 **error** 而不是 failed (§11.2)。

起因: 报错口径不能混。

    failed → 工人眼里是「板子坏了, 送修」
    error  → 「这一项没测成, 查环境」

sudo 在、但没配 NOPASSWD 时, 运行期 `sudo -n <cmd>` 会失败, 那一项就判成
**failed** —— 工人会把**产线的权限配置的锅**算到板子上, 白白送修一批好板子。

这轮加了两道:
    ① 预检 (`sudo_available`) 真跑一次 `sudo -n true` —— 以前只查
       `find_tool("sudo") is not None`, 那只说明**装了 sudo 二进制**,
       不说明**配了免密**, 于是预检一律放行, 故障拖到运行期才爆。
    ② 兜底 (runner) —— 万一某一项漏了预检, 运行期真撞上要密码, 把
       failed 改判成 error, 并把原因换成统一文案。

这个测试验六件事:
    ① 四种 sudo 报错特征都能认出来
    ② 不相干的报错不会被误认
    ③ 统一文案写清了「这是产线环境配置问题」
    ④ sudo_available: 免密可用 → True; 要密码 / 没装 → False
    ⑤ runner 兜底: 带 sudo 密码特征的 failed → 改判 error + 换文案
    ⑥ runner 兜底: 普通 failed (比如温度超了) → 保持 failed, 不被误伤

只碰内存里的假 Database / 假 report —— 不写生产库、不写 reports/pdf/。
跑法: .venv/bin/python -m tests.test_sudo_nopasswd
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import runner as R
from server.tests import _tools as T
from server.tests.base import TestCase, TestResult

FAILS = []
PASSES = 0

WRITTEN = []


def check(cond, msg, extra=""):
    global PASSES
    if cond:
        PASSES += 1
        print("    ✓ " + msg)
    else:
        FAILS.append(msg)
        print("    ✗ " + msg + ("  → " + str(extra) if extra else ""))


class FakeDb:
    @staticmethod
    async def create_run(config, meta=None): return 1

    @staticmethod
    async def create_mini_run(pid, config, meta=None): return 99

    @staticmethod
    async def get_run(rid): return None

    @staticmethod
    async def upsert_result(run_id, test_name, **fields):
        WRITTEN.append((run_id, test_name, fields))

    @staticmethod
    async def get_result_log(run_id, test_name): return None

    @staticmethod
    async def finish_run(*a, **k): pass

    @staticmethod
    async def set_pdf_path(*a, **k): pass


class FakeReport:
    @classmethod
    async def export_pdf(cls, run, results): return "/fake/report.pdf"

    @staticmethod
    def find_chromium(): return "/fake/chromium"

    @staticmethod
    def output_dir(): return None


R.Database = FakeDb
R.report = FakeReport
R.INTERVAL_BETWEEN_TESTS_SEC = 0


# ───────────────────────── 识别层 ─────────────────────────

def case_recognize():
    print("【1】sudo 的四种报错特征都要认得出来")
    for s in ("sudo: a password is required",
              "sudo: a terminal is required to read the password",
              "sudo: no tty present and no askpass program specified",
              "sudo: 3 incorrect password attempts\nsudo: sorry, try again"):
        check(T.is_sudo_password_error(s),
              f"认得出: {s.splitlines()[0][:46]}", s)

    print("\n【2】不相干的报错不能被误认")
    for s in ("温度 91°C 超过阈值 85°C",
              "读速度 10.5 MB/s 低于阈值 80 MB/s",
              "fio 退出码 1",
              ""):
        check(not T.is_sudo_password_error(s),
              f"不误认: {s[:36] or '(空串)'}", s)


def case_hint():
    print("\n【3】统一文案: 说清是产线环境配置问题, 且带得上原始报错")
    h = T.sudo_nopasswd_hint("sudo: a password is required")
    check("NOPASSWD" in h, "写了 NOPASSWD", h)
    check("产线环境配置问题" in h, "写明是产线环境配置问题", h)
    check("a password is required" in h, "带上了原始报错", h)
    h2 = T.sudo_nopasswd_hint()
    check("NOPASSWD" in h2, "没原文时也有完整文案", h2)


# ───────────────────────── 预检层 ─────────────────────────

def case_sudo_available():
    print("\n【4】sudo_available 要**真跑** `sudo -n true`, 不能只看装没装")
    orig_run = T.subprocess.run
    orig_find = T.find_tool
    try:
        T.find_tool = lambda name: "/usr/bin/sudo" if name == "sudo" else None

        class R0:
            returncode = 0
        T.subprocess.run = lambda *a, **k: R0()
        check(T.sudo_available() is True, "免密可用 → True", T.sudo_available())

        class R1:
            returncode = 1
        T.subprocess.run = lambda *a, **k: R1()
        check(T.sudo_available() is False, "要密码 (rc≠0) → False", T.sudo_available())

        # sudo 压根没装
        T.find_tool = lambda name: None
        T.subprocess.run = lambda *a, **k: R0()
        check(T.sudo_available() is False, "没装 sudo → False", T.sudo_available())

        # sudo 在但跑起来炸了 (超时等) → 也不能当成可用
        T.find_tool = lambda name: "/usr/bin/sudo"

        def boom(*a, **k):
            raise OSError("boom")
        T.subprocess.run = boom
        check(T.sudo_available() is False, "跑 sudo 抛异常 → False", T.sudo_available())

        # 确认真的调了 `sudo -n true` (而不是随便找个 sudo 跑跑)
        calls = []

        class R2:
            returncode = 0
        def rec(*a, **k):
            calls.append(a[0] if a else k.get("args"))
            return R2()
        T.subprocess.run = rec
        T.sudo_available()
        check(bool(calls) and "-n" in calls[0] and "true" in calls[0],
              "跑的是 `sudo -n true`", calls)
    finally:
        T.subprocess.run = orig_run
        T.find_tool = orig_find


# ───────────────────────── 兜底层 (runner) ─────────────────────────

async def run_one(status, error, log=""):
    class Tst(TestCase):
        name = "fake"
        display_name = "假测试项"

        async def run(self, ctx):
            r = TestResult(status=status, error=error)
            r.log = log
            return r

    WRITTEN.clear()
    rn = R.TestRunner()
    rn.run_id = 1
    cfg = {"tests": {"fake": {"enabled": True}}, "project_root": ""}
    res, _ = await rn._run_one("fake", Tst, cfg, write_to_parent=False)
    return res


async def case_runner_backstop():
    print("\n【5】runner 兜底: 带 sudo 密码特征的 failed → 改判 error")
    res = await run_one("failed", "sudo: a password is required")
    check(res.status == "error", "改判成 error (不是 failed)", res.status)
    check("NOPASSWD" in (res.error or ""),
          "错误原因换成了统一文案", res.error)
    check("产线环境配置问题" in (res.error or ""),
          "写明是产线环境配置问题, 不是设备不合格", res.error)

    print("\n【5b】报错藏在**日志**里也一样认得出来")
    res = await run_one("failed", "执行失败", log="sudo: no tty present and no askpass program specified")
    check(res.status == "error", "改判成 error", res.status)

    print("\n【6】普通 failed 不能被误伤")
    res = await run_one("failed", "温度 91°C 超过阈值 85°C")
    check(res.status == "failed", "仍然是 failed", res.status)
    check("温度" in (res.error or ""), "原来的原因没被改写", res.error)

    res = await run_one("failed", "fio 退出码 1")
    check(res.status == "failed", "仍然是 failed", res.status)

    print("\n【6b】本来是 error 的不受影响")
    res = await run_one("error", "sudo: a password is required")
    check(res.status == "error", "仍然是 error", res.status)


async def main():
    case_recognize()
    case_hint()
    case_sudo_available()
    await case_runner_backstop()
    print("\n" + ("全部通过" if not FAILS else f"有失败项 ({len(FAILS)})"))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
