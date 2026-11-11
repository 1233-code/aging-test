# 变异测试: 故意把「开跑闸门关不住」这个竞态放回去, 看
# tests/test_runner_start_gate.py 会不会红。
#
# 为什么要有这么个东西: 「测试全绿」本身说明不了什么 —— 断言写错了、
# 写松了、或者压根没跑到, 都是绿的。唯一能证明这测试**咬得动**的办法,
# 就是亲手把代码改坏, 看它是不是真的红。
#
# 这一份守的是 2026-11-11 挖出来的竞态:
#   闸门(`status == "running"`)和真正把 status 设成 "running" 之间隔着
#   几个 await ⇒ 工人连点两下「重测」⇒ 两个 mini **都过了闸门** ⇒
#   两轮重测一起跑、第二个把第一个的 `self._task` 顶掉 ⇒ 第一轮的结果
#   写不回父轮、界面永远停在「⏳ 等待」。
#
# 做法: **每个变异单开一个全新目录**, 把 server/ + config.yaml + 那个测试
# 文件复制进去, 在**副本**上做一处改动, 在副本目录里跑测试。
# 被改的那个文件从头到尾**只写一次** —— 全程不覆写任何已存在的文件。
#
# 跑法: .venv/bin/python tests/_mutate_runner_start_gate.py
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests._tmp import mkdtemp                       # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(REPO, ".venv", "bin", "python")
TEST = "tests.test_runner_start_gate"

# 每个变异: (文件, from, to, 说明)。from 必须在源文件里**找得到且唯一**。
MUTATIONS = [
    (
        "server/runner.py",
        '        if self.status == "running" or self._starting:\n'
        '            return {"ok": False, "error": "已有测试在运行"}\n'
        '        self._starting = True',
        '        if self.status == "running":\n'
        '            return {"ok": False, "error": "已有测试在运行"}\n'
        '        self._starting = True',
        "RUNNER: start() 的闸门**不认「正在开跑」这个占位** ⇒ 两个满轮"
        "同时挤进来",
    ),
    (
        "server/runner.py",
        '        if self.status == "running" or self._starting:\n'
        '            return {"ok": False, "error": "已有测试在运行"}\n'
        '\n'
        '        if not test_names:',
        '        if self.status == "running":\n'
        '            return {"ok": False, "error": "已有测试在运行"}\n'
        '\n'
        '        if not test_names:',
        "RUNNER: start_mini() 的闸门**不认占位** ⇒ 工人连点两下「重测」⇒ "
        "两轮重测一起跑 ⇒ 第一轮的结果写不回父轮",
    ),
    # ⚠️ **故意没有**「start() 自己不占位」这一条: start() 的闸门到
    #    `status = "running"` 之间**今天一个 await 都没有**(同步执行)⇒
    #    它的占位目前**观察不到** ⇒ 写了变异也是「测还是绿的」⇒ 不写。
    #    (留着的意义是防将来有人在 `_start_body()` 前面加了 await。
    #     而「start() 的闸门**不认**别人(重测)的占位」是能观察到的 ——
    #     见下面第一条。)
    (
        "server/runner.py",
        '        self._starting = True\n'
        '        try:\n'
        '            return await self._start_mini_body(parent_run_id, valid_names,',
        '        self._starting = False\n'
        '        try:\n'
        '            return await self._start_mini_body(parent_run_id, valid_names,',
        "RUNNER: start_mini() **压根不占位** ⇒ 重测撞车照样发生",
    ),
    (
        "server/runner.py",
        '            return await self._start_body(config)\n'
        '        finally:\n'
        '            self._starting = False',
        '            return await self._start_body(config)\n'
        '        finally:\n'
        '            pass',
        "RUNNER: 占位**永不放掉** ⇒ 第一轮跑完之后再也开不了下一轮"
        "(工具直接废掉)",
    ),
    (
        "server/runner.py",
        '            return await self._start_mini_body(parent_run_id, valid_names,\n'
        '                                                dropped_names)\n'
        '        finally:\n'
        '            self._starting = False',
        '            return await self._start_mini_body(parent_run_id, valid_names,\n'
        '                                                dropped_names)\n'
        '        finally:\n'
        '            pass',
        "RUNNER: 重测那条路的占位**永不放掉**(同上)",
    ),
]


def build_copy(dst, rel, new_text):
    """把 server/ + config.yaml + 那个测试文件复制到 dst。

    被变异的那个文件**只写一次** —— 根本不复制进来, 直接写变异后的内容。
    """
    src = os.path.join(REPO, "server")
    for root, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        relroot = os.path.relpath(root, src)
        outroot = (os.path.join(dst, "server") if relroot == "."
                   else os.path.join(dst, "server", relroot))
        os.makedirs(outroot, exist_ok=True)
        for fn in files:
            relfile = os.path.normpath(os.path.join(relroot, fn))
            if relfile == rel:
                continue
            shutil.copy2(os.path.join(root, fn), os.path.join(outroot, fn))

    shutil.copy2(os.path.join(REPO, "config.yaml"),
                 os.path.join(dst, "config.yaml"))
    os.makedirs(os.path.join(dst, "tests"), exist_ok=True)
    for extra in ("test_runner_start_gate.py", "_tmp.py"):
        shutil.copy2(os.path.join(REPO, "tests", extra),
                     os.path.join(dst, "tests", extra))

    outp = os.path.join(dst, rel)
    os.makedirs(os.path.dirname(outp), exist_ok=True)
    with open(outp, "w", encoding="utf-8") as f:
        f.write(new_text)


def main():
    root = mkdtemp(prefix="mut_startgate_")
    print(f"副本都在 {root} 下面\n")

    bad = 0
    for i, (rel, frm, to, desc) in enumerate(MUTATIONS):
        srcp = os.path.join(REPO, rel)
        with open(srcp, encoding="utf-8") as f:
            orig = f.read()
        if orig.count(frm) != 1:
            print(f"\n✗ {desc}\n   → 锚点出现 {orig.count(frm)} 次(要恰好 1 次): "
                  f"{frm[:70]!r}")
            bad += 1
            continue

        work = os.path.join(root, f"m{i:02d}")
        os.makedirs(work, exist_ok=True)
        build_copy(work, rel, orig.replace(frm, to))

        env = dict(os.environ)
        env["PYTHONPATH"] = work
        proc = subprocess.run([PY, "-m", TEST], cwd=work, env=env,
                              capture_output=True, text=True, timeout=300)
        out = (proc.stdout or "") + (proc.stderr or "")
        fails = out.count("✗")
        if proc.returncode != 0 and fails == 0:
            print(f"\n✗ {desc}\n   → 红了, 但一个 ✗ 都没有 (exit="
                  f"{proc.returncode}) —— 这是**崩了**, 不算数:\n{out[-600:]}")
            bad += 1
        elif proc.returncode != 0:
            print(f"\n✓ {desc}\n   → 测试变红 (exit={proc.returncode}, "
                  f"{fails} 项失败) —— 咬得动")
        else:
            print(f"\n✗ {desc}\n   → 测试还是绿的! 这个变异没被守住")
            bad += 1

    print("\n" + ("全部变异都被抓住了 ✓" if bad == 0
                  else f"有 {bad} 个变异没被抓住 ✗"))
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
