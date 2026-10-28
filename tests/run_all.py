#!/usr/bin/env python3
"""跑一遍所有回归测试 (Python + 前端 JS), 最后给个汇总。

为什么要有这么个东西: 测试有 30 多个, 一个个手动跑一遍既慢又容易漏 ——
漏跑的那几个, 回归的时候没人知道它坏了。改完代码跑一次这个, 一眼看出
哪儿红了。

跑法: .venv/bin/python tests/run_all.py

⚠️ **不跑变异脚本** (_mutate_*.js / _mutate_*.py) —— 那些是「证明测试
   咬得动」的, 单独跑, 慢。
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
PY = os.path.join(REPO, ".venv", "bin", "python")

RED = "\033[31m"
GREEN = "\033[32m"
DIM = "\033[2m"
OFF = "\033[0m"


def collect(suffix):
    return sorted(f for f in os.listdir(HERE)
                  if f.startswith("test_") and f.endswith(suffix)
                  and not f.startswith("_"))


def run(cmd, env=None):
    p = subprocess.run(cmd, cwd=REPO, env=env, capture_output=True,
                       text=True, timeout=600)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def main():
    only = sys.argv[1] if len(sys.argv) > 1 else ""
    failed = []
    total = 0

    def wanted(f):
        return not only or only in f

    print(f"{DIM}== Python 测试 =={OFF}")
    for f in collect(".py"):
        if not wanted(f):
            continue
        mod = "tests." + f[:-3]
        code, out = run([PY, "-m", mod])
        total += 1
        if code == 0:
            print(f"  {GREEN}✓{OFF} {f}")
        else:
            failed.append(mod)
            print(f"  {RED}✗{OFF} {f}")
            print("    " + out.strip().replace("\n", "\n    ")[-1500:])

    print(f"\n{DIM}== 前端测试 (node) =={OFF}")
    for f in collect(".js"):
        if not wanted(f):
            continue
        code, out = run(["node", os.path.join(HERE, f)])
        total += 1
        if code == 0:
            print(f"  {GREEN}✓{OFF} {f}")
        else:
            failed.append(f)
            print(f"  {RED}✗{OFF} {f}")
            print("    " + out.strip().replace("\n", "\n    ")[-1500:])

    print()
    if failed:
        print(f"{RED}{len(failed)} 个失败{OFF} (共 {total} 个):")
        for f in failed:
            print("   - " + f)
        return 1
    print(f"{GREEN}全部通过 ({total} 个){OFF}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
