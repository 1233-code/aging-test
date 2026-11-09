# 变异测试: 故意把「整轮执行顺序」改坏, 看 tests/test_test_order.py 会不会红。
#
# 为什么要有这么个东西: 「测试全绿」本身说明不了什么 —— 断言写错了、
# 写松了、或者压根没跑到, 都是绿的。唯一能证明这测试**咬得动**的办法,
# 就是亲手把顺序改坏, 看它是不是真的红。
#
# 这一份守的是 2026-11-04 用户定的规矩: **WiFi 项排在 GPU 之前**
# (原来排在整轮最后)。顺序牵动着四层东西, 每一层都得有人看着:
#   ① `server/runner.py` 的 `RECOMMENDED_ORDER` —— 顺序的**唯一来源**;
#   ② `server/runner.py` 的 `_build_test_list` —— 真的按它排队;
#   ③ `server/tests/registry.py` 的导入顺序 —— 给人的那份清单;
#   ④ `server/main.py` + `static/app.js` —— 原样下发给页面;
#   ⑤ `docs/需求设计.md` —— 文档里写的跟代码一致。
#
# 做法: **每个变异单开一个全新目录**, 把 server/ + static/ + docs/ +
# config.yaml + 那个测试文件复制进去, 在**副本**上做一处改动, 在副本目录里
# 跑测试。被改的那个文件是从头到尾**只写一次**(直接写变异后的内容), 别的
# 文件原样复制 —— 所以整个过程不覆写任何已存在的文件, 跑完也不用还原。
#
# ⚠️ 只在**副本**上改 —— 仓库里 server/ 是产线在跑的那份, 改坏了没人知道。
#
# 跑法: .venv/bin/python tests/_mutate_test_order.py
import os
import shutil
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(REPO, ".venv", "bin", "python")
TEST = "tests.test_test_order"

# 需要整份复制过去的目录/文件(除了被变异的那一个)。
_COPY_DIRS = ("server", "static", "docs")
_COPY_FILES = ("config.yaml",)
_COPY_TEST = "tests/test_test_order.py"

# 每个变异: (文件, from, to, 说明)。from 必须在源文件里**找得到**, 否则算失败。
MUTATIONS = [
    (
        "server/runner.py",
        '    "wifi", "bluetooth",\n'
        '    # ④ 重载收尾 (产热最高)\n'
        '    "gpu", "cpu",',
        '    # ④ 重载收尾 (产热最高)\n'
        '    "gpu", "cpu",\n'
        '    # ⑤ 射频又挪回最后 (2026-10-30 的老规矩)\n'
        '    "wifi", "bluetooth",',
        "RUNNER: 射频项被挪回整轮最后 —— 2026-11-04 定的「WiFi 在 GPU 之前」被推翻",
    ),
    (
        "server/runner.py",
        '    "wifi", "bluetooth",',
        '    "wifi_tmp_placeholder", "bluetooth",',
        "RUNNER: 射频项从队列里掉了(整轮就不测 WiFi 了)",
    ),
    (
        "server/runner.py",
        '    "gpu", "cpu",',
        '    "cpu", "gpu",',
        "RUNNER: GPU 与 CPU 颠倒了(重载项的先后反了)",
    ),
    (
        "server/runner.py",
        '    "memory", "emmc", "tfcard",',
        '    "emmc", "tfcard", "memory",',
        "RUNNER: 轻量项被挪到了 GPU 后面(热累积会污染轻量项的读数)",
    ),
    (
        "server/runner.py",
        "        names = [n for n in RECOMMENDED_ORDER if n in enabled]",
        "        names = sorted(n for n in enabled)",
        "RUNNER: 不按推荐顺序排了(改成按字母排)—— 顺序这个整体就被架空",
    ),
    (
        "server/tests/registry.py",
        "from . import (                     # noqa: F401\n"
        "    memory, emmc, tfcard,           # ① 轻量\n"
        "    hdmi, usb, ethernet, fan,       # ② 接口与外设\n"
        "    wifi, bluetooth,                # ③ 射频(WiFi 会动全局状态, 蓝牙只读)\n"
        "    gpu, cpu,                       # ④ 重载\n"
        ")",
        "from . import (                     # noqa: F401\n"
        "    cpu, gpu, bluetooth, wifi, usb, ethernet, hdmi, fan, tfcard, "
        "memory, emmc\n"
        ")",
        "REGISTRY: 注册清单退回老顺序(跟推荐顺序对不上)",
    ),
    (
        "server/main.py",
        '"recommended_order": RECOMMENDED_ORDER,',
        '"recommended_order": [t["name"] for t in get_all_tests()],',
        "MAIN: /api/config 不再下发 RECOMMENDED_ORDER(页面拿到的顺序跟真跑的不一样)",
    ),
    (
        "static/app.js",
        "testOrder.value = data.recommended_order && data.recommended_order.length",
        "testOrder.value = data.tests && data.tests.length",
        "APP: 前端改用 data.tests 的原始顺序(页面显示的顺序跟真跑的对不上)",
    ),
    (
        "docs/需求设计.md",
        "射频（WiFi + 蓝牙，GPU 之前）",
        "射频（WiFi，最后）",
        "DOC: 文档里的顺序没跟着改(还是写着「射频最后」)",
    ),
]


def build_copy(dst, rel, new_text):
    """把 server/ + static/ + docs/ + config.yaml + 那个测试文件复制到 dst,
    并把 rel 写成 new_text。

    每个文件**只写一次** —— 被变异的那个根本不复制进来, 直接写变异后的
    内容。全程不覆写任何已存在的文件, 所以跑完不需要还原。
    """
    for d in _COPY_DIRS:
        src = os.path.join(REPO, d)
        out = os.path.join(dst, d)
        shutil.copytree(src, out,
                        ignore=shutil.ignore_patterns("__pycache__"),
                        dirs_exist_ok=True)
    for f in _COPY_FILES:
        shutil.copy2(os.path.join(REPO, f), os.path.join(dst, f))
    os.makedirs(os.path.join(dst, "tests"), exist_ok=True)
    shutil.copy2(os.path.join(REPO, _COPY_TEST), os.path.join(dst, _COPY_TEST))

    outp = os.path.join(dst, rel)
    os.makedirs(os.path.dirname(outp), exist_ok=True)
    with open(outp, "w", encoding="utf-8") as f:
        f.write(new_text)


def run_one(work):
    env = dict(os.environ)
    env["PYTHONPATH"] = work
    proc = subprocess.run(
        [PY, "-m", TEST],
        cwd=work, env=env, capture_output=True, text=True, timeout=300,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def main():
    root = tempfile.mkdtemp(prefix="mut_order_")
    print(f"副本都在 {root} 下面\n")

    bad = 0
    for i, (rel, frm, to, desc) in enumerate(MUTATIONS):
        srcp = os.path.join(REPO, rel)
        with open(srcp, encoding="utf-8") as f:
            orig = f.read()
        if frm not in orig:
            print(f"\n✗ {desc}\n   → 变异点没找到(源文件变了?): {frm[:60]!r}")
            bad += 1
            continue

        work = os.path.join(root, f"m{i:02d}")
        os.makedirs(work, exist_ok=True)
        build_copy(work, rel, orig.replace(frm, to))

        code, out = run_one(work)
        fails = out.count("✗")
        if code != 0 and fails == 0:
            # ⚠️ **红了但一个 ✗ 都没有** —— 那多半不是断言把它咬住的, 而是
            #   测试自己崩了(语法错/导入错/异常)。这不算「咬得动」。
            print(f"\n✗ {desc}\n   → 红了, 但一个 ✗ 都没有 (exit={code}) —— "
                  f"这是**崩了**, 不是被断言咬住的, 不算数:\n{out[-800:]}")
            bad += 1
        elif code != 0:
            print(f"\n✓ {desc}\n   → 测试变红 (exit={code}, {fails} 项失败) —— 咬得动")
        else:
            print(f"\n✗ {desc}\n   → 测试还是绿的! 这个变异没被守住")
            bad += 1

    print("\n" + ("全部变异都被抓住了 ✓" if bad == 0
                  else f"有 {bad} 个变异没被抓住 ✗"))
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
