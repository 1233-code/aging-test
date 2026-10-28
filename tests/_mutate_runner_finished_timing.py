# 变异测试(后端): 故意把「run_finished 带的那对时间」改坏, 看
# tests/test_runner_finished_timing.py 会不会红。
#
# 为什么要有这么个东西: 「测试全绿」本身说明不了什么 —— 断言写错了、
# 写松了、或者压根没跑到, 都是绿的。唯一能证明这测试**咬得动**的办法,
# 就是亲手把代码改坏, 看它是不是真的红。
#
# 做法: 把 server/ + config.yaml + 那个测试文件复制一份到临时目录, 在**副本**
# 上做一处改动, 在副本目录里跑测试。每个变异跑完把副本还原。
#
# ⚠️ 只在**副本**上改 —— 仓库里 server/ 是产线在跑的那份, 改坏了没人知道。
#
# 跑法: .venv/bin/python tests/_mutate_runner_finished_timing.py
import os
import shutil
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(REPO, ".venv", "bin", "python")
TEST = "tests.test_runner_finished_timing"

# 每个变异: (文件, from, to, 说明)。from 必须在副本里**找得到**, 否则算失败。
MUTATIONS = [
    (
        "server/runner.py",
        '            "started_at": self.started_at,\n            "finished_at": self.finished_at,',
        '            "finished_at": self.finished_at,',
        "RUNNER: 正常收尾的 run_finished 不带 started_at (前端配不上起点)",
    ),
    (
        "server/runner.py",
        '                "started_at": self.started_at,\n                "finished_at": self.finished_at,',
        '                "finished_at": self.finished_at,',
        "RUNNER: 出错收尾的 run_finished 不带 started_at",
    ),
    (
        "server/runner.py",
        '                "sn": self.sn,\n                "started_at": self.started_at,\n'
        '                "finished_at": self.finished_at,\n'
        '                "error": "未选择任何测试",',
        '                "sn": self.sn,\n                "finished_at": self.finished_at,\n'
        '                "error": "未选择任何测试",',
        "RUNNER: 一项都没勾选时的那条 run_finished 不带 started_at",
    ),
    (
        "server/runner.py",
        '            "started_at": self.started_at,\n            "finished_at": self.finished_at,',
        '            "started_at": 0,\n            "finished_at": self.finished_at,',
        "RUNNER: run_finished 的起点写死成 0 (界面上的总耗时变成整轮时长)",
    ),
]


def run_one(tmp):
    env = dict(os.environ)
    env["PYTHONPATH"] = tmp
    proc = subprocess.run(
        [PY, "-m", TEST],
        cwd=tmp, env=env, capture_output=True, text=True, timeout=300,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def main():
    tmp = tempfile.mkdtemp(prefix="mut_fintiming_")
    # 复制一份: server/ + config.yaml + 那个测试文件
    shutil.copytree(os.path.join(REPO, "server"), os.path.join(tmp, "server"),
                    ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy2(os.path.join(REPO, "config.yaml"),
                 os.path.join(tmp, "config.yaml"))
    os.makedirs(os.path.join(tmp, "tests"), exist_ok=True)
    shutil.copy2(os.path.join(REPO, "tests", "test_runner_finished_timing.py"),
                 os.path.join(tmp, "tests", "test_runner_finished_timing.py"))
    print(f"副本在 {tmp}\n")

    bad = 0
    for rel, frm, to, desc in MUTATIONS:
        p = os.path.join(tmp, rel)
        with open(p, encoding="utf-8") as f:
            orig = f.read()
        if frm not in orig:
            print(f"\n✗ {desc}\n   → 变异点没找到(源文件变了?): {frm[:60]!r}")
            bad += 1
            continue
        with open(p, "w", encoding="utf-8") as f:
            f.write(orig.replace(frm, to))
        code, out = run_one(tmp)
        fails = out.count("✗")
        if code != 0 and fails == 0:
            # ⚠️ 红了但一个 ✗ 都没有 → 那是**崩了**, 不是被断言咬住的,
            #    那种红是假的(证明不了「断言抓得住这个缺陷」)。
            print(f"\n✗ {desc}\n   → 红了, 但一个 ✗ 都没有 (exit={code}) —— "
                  f"这是**崩了**, 不算数:\n{out[-800:]}")
            bad += 1
        elif code != 0:
            print(f"\n✓ {desc}\n   → 测试变红 (exit={code}, {fails} 项失败) —— 咬得动")
        else:
            print(f"\n✗ {desc}\n   → 测试还是绿的! 这个变异没被守住")
            bad += 1
        with open(p, "w", encoding="utf-8") as f:
            f.write(orig)      # 还原

    print("\n" + ("全部变异都被抓住了 ✓" if bad == 0
                  else f"有 {bad} 个变异没被抓住 ✗"))
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
