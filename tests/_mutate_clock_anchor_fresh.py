# 变异测试: 故意把「锚点记此刻」和「后端把时钟发下去」改坏, 看
# tests/test_clock_anchor_fresh.py 会不会红。
#
# 为什么要有这么个东西: 「测试全绿」本身说明不了什么 —— 断言写错了、
# 写松了、或者压根没跑到, 都是绿的。唯一能证明这测试**咬得动**的办法,
# 就是亲手把代码改坏, 看它是不是真的红。
#
# 这一份守的是 2026-11-06 产线原话「开头一直是 0, 后面变两个多小时,
# 然后测试结果上的时间又对得上」换来的规矩:
#   ① 锚点记的是「**此刻**最可信的墙钟」, 不是「最后一次有人问时间」
#      —— 服务空转时锚点不许冻着(冻着 ⇒ 整个应用慢 2 小时 28 分);
#   ② 墙钟模式下锚点记的就是系统时钟本身(不是 0);
#   ③ 后端每条事件 + 快照都带 `server_now`;
#   ④ 前端拿 `server_now` 当基准、用单调钟往前推(不用本机墙钟)。
#
# 做法: **每个变异单开一个全新目录**, 把 server/ + static/ + config.yaml
# + 那个测试文件复制进去, 在**副本**上做一处改动, 在副本目录里跑测试。
# 被改的那个文件是从头到尾**只写一次**(直接写变异后的内容), 别的文件原样
# 复制 —— 所以整个过程不覆写任何已存在的文件, 跑完也不用还原。
#
# ⚠️ 只在**副本**上改 —— 仓库里 server/ 是产线在跑的那份, 改坏了没人知道。
#
# 跑法: .venv/bin/python tests/_mutate_clock_anchor_fresh.py
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests._tmp import mkdtemp                       # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(REPO, ".venv", "bin", "python")
TEST = "tests.test_clock_anchor_fresh"

# 每个变异: (文件, from, to, 说明)。from 必须在源文件里**找得到**, 否则算失败。
MUTATIONS = [
    (
        "server/clock.py",
        "        cur = now()",
        "        cur = _last_observed",
        "CLOCK: 锚点记的是「最后一次有人问时间」—— 服务空转时它冻着",
    ),
    (
        "server/clock.py",
        "        cur = now()",
        "        cur = time.time()",
        "CLOCK: 锚点里存的是歪的墙钟(推算模式下也不推算)",
    ),
    (
        "server/runner.py",
        '        event.setdefault("server_now", clock.now())',
        '        event.setdefault("server_now", time.time())',
        "RUNNER: 广播的 server_now 用的是墙钟(前端又跨尺度了)",
    ),
    (
        "server/runner.py",
        '            "server_now": clock.now(),',
        '            "server_now": time.time(),',
        "RUNNER: 快照的 server_now 用的是墙钟(刷新页面就又不对表)",
    ),
    (
        "server/clock.py",
        "            cur = now() if _initialized else time.time()",
        "            cur = now()",
        "CLOCK: 写锚点会绕去读锚点(模块没判定过时 ⇒ 存进去的值取决于"
        "盘上那份旧锚点, 不是此刻)",
    ),
    (
        "server/clock.py",
        "    save_anchor(cur=ts)",
        "    save_anchor()",
        "CLOCK: 认下真时间之后写锚点又走『此刻』⇒ 推算模式永远结束不了",
    ),
    (
        "static/app.js",
        "            if (msg.server_now) syncServerClock(msg.server_now);",
        "            if (false && msg.server_now) syncServerClock(msg.server_now);",
        "APP: 快照那条路不对表(刷新一下就读秒就冻成 0)",
    ),
    (
        "static/app.js",
        "serverNow.value + (perfNow() - serverNowPerf.value) / 1000",
        "serverNow.value + (Date.now() - serverNowPerf.value) / 1000",
        "APP: 推进读秒改回用墙钟(墙钟一校正, 读秒猛地跳一下)",
    ),
]


def build_copy(dst, rel, new_text):
    """把 server/ + static/ + config.yaml + 那个测试文件复制到 dst, 并把 rel 写成 new_text。

    每个文件**只写一次** —— 被变异的那个根本不复制进来, 直接写变异后的
    内容。全程不覆写任何已存在的文件, 所以跑完不需要还原。
    """
    for top in ("server", "static"):
        src = os.path.join(REPO, top)
        for root, dirs, files in os.walk(src):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            relroot = os.path.relpath(root, src)
            outroot = (os.path.join(dst, top) if relroot == "."
                       else os.path.join(dst, top, relroot))
            os.makedirs(outroot, exist_ok=True)
            for fn in files:
                relfile = os.path.normpath(os.path.join(top, relroot, fn))
                if relfile == rel:
                    continue          # 这个下面单独写
                shutil.copy2(os.path.join(root, fn), os.path.join(outroot, fn))

    shutil.copy2(os.path.join(REPO, "config.yaml"),
                 os.path.join(dst, "config.yaml"))
    os.makedirs(os.path.join(dst, "tests"), exist_ok=True)
    for extra in ("test_clock_anchor_fresh.py", "_tmp.py"):
        shutil.copy2(os.path.join(REPO, "tests", extra),
                     os.path.join(dst, "tests", extra))

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
    root = mkdtemp(prefix="mut_clock_fresh_")
    print(f"副本都在 {root} 下面(进程退出时自动删掉)\n")

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
