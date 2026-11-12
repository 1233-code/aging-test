# 变异测试: 故意把「卡挂死时的判定与收尾」改坏, 看 tests/test_tfcard_hang.py
# 会不会红。
#
# 为什么要有这么个东西: 「测试全绿」本身说明不了什么 —— 断言写错了、
# 写松了、或者压根没跑到, 都是绿的。唯一能证明这测试**咬得动**的办法,
# 就是亲手把代码改坏, 看它是不是真的红。
#
# 这一份守的是 2026-10-28 那次事故的三个坑:
#   ① 用户按「暂停」被记成 error → 整轮判 fail;
#   ② 卡对读写没应答没人认出来(笼统一句「异常」);
#   ③ 收尾在事件循环里 os.unlink 卡的路径 → 整个服务冻死。
#
# 做法: **每个变异单开一个全新目录**, 把 server/ + config.yaml + 那个测试
# 文件复制进去, 在**副本**上做一处改动, 在副本目录里跑测试。
# 被改的那个文件是从头到尾**只写一次**(直接写变异后的内容), 别的文件原样
# 复制 —— 所以整个过程不覆写任何已存在的文件, 跑完也不用还原。
#
# ⚠️ 只在**副本**上改 —— 仓库里 server/ 是产线在跑的那份, 改坏了没人知道。
#
# 跑法: .venv/bin/python tests/_mutate_tfcard_hang.py
import os
import shutil
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(REPO, ".venv", "bin", "python")
TEST = "tests.test_tfcard_hang"

# 每个变异: (文件, from, to, 说明)。from 必须在源文件里**找得到**, 否则算失败。
MUTATIONS = [
    (
        "server/tests/tfcard.py",
        '            result.status = "stopped"\n'
        '            result.error = "用户中途停止 (本项未跑完)"',
        '            result.status = "error"\n'
        '            result.error = "用户中途停止 (本项未跑完)"',
        "TF: 用户按「停止」被判成 error (2026-10-28 事故那天的老样子)",
    ),
    (
        "server/tests/tfcard.py",
        '            result.status = "failed"\n'
        '            result.error = (\n'
        '                f"TF 卡**无响应**(对读写没有应答, 卡卡住了) —— 这一项**没测完**, "\n'
        '                f"但这张卡**不合格**, 请换一张。具体经过: {e}"\n'
        '            )',
        '            result.status = "error"\n'
        '            result.error = (\n'
        '                f"TF 卡**无响应**(对读写没有应答, 卡卡住了) —— 这一项**没测完**, "\n'
        '                f"但这张卡**不合格**, 请换一张。具体经过: {e}"\n'
        '            )',
        "TF: 卡没应答被判成 error (设备不合格该判 failed, 该拦下来换卡)",
    ),
    (
        "server/tests/tfcard.py",
        'f"TF 卡**无响应**(对读写没有应答, 卡卡住了) —— 这一项**没测完**, "',
        'f"TF 卡异常(卡卡住了) —— 这一项**没测完**, "',
        "TF: 报错里不说「无响应」(工人分不清是卡的问题还是工具的问题)",
    ),
    (
        "server/tests/tfcard.py",
        "    ok = await remove_file_async(path)",
        "    if os.path.exists(path):\n"
        "        os.unlink(path)\n"
        "    ok = True",
        "TF: 收尾又改回同步 os.unlink (卡挂死时会把整个服务冻死)",
    ),
    (
        "server/tests/emmc.py",
        '        if out.reason == "cancelled":',
        '        if out.reason == "hard_limit":',
        "eMMC: 用户中止跟「到点没应答」混了 (两种结果判反)",
    ),
    (
        "server/tests/emmc.py",
        "        if rc == 124:",
        "        if rc == 125:",
        "eMMC: 不认 `timeout` 的 124 退出码 (设备没应答会落进普通失败)",
    ),
    (
        "server/tests/emmc.py",
        '        if out.reason == "hard_limit":\n'
        '            raise DeviceUnresponsive(\n'
        '                f"fio {rw} 跑了 {tmo + 30}s 还没结束 —— 设备没有应答"',
        '        if out.reason == "hard_limit":\n'
        '            raise RuntimeError(\n'
        '                f"fio {rw} 跑了 {tmo + 30}s 还没结束 —— 设备没有应答"',
        "eMMC: 设备没应答退回普通 RuntimeError (又变回笼统的「异常」)",
    ),
    (
        "server/tests/emmc.py",
        '    return (f"; ⚠ 还有 {len(pids)} 个 fio 进程没收掉"\n'
        '            f"(PID {\', \'.join(str(p) for p in pids)}): "\n'
        '            f"设备卡死时进程会卡在内核不可中断状态, 任何信号都杀不掉它, "\n'
        '            f"需要把卡拔出来复位才能清掉")',
        '    return ""',
        "eMMC: 遗留进程一句都不提 (操作员以为收干净了, 其实还在写卡)",
    ),
    (
        "server/tests/base.py",
        "class DeviceUnresponsive(Exception):",
        "class DeviceUnresponsive(TestCancelled):",
        "BASE: 两个异常类搞成继承关系 (except 顺序一反, 无响应分支永远走不到)",
    ),
    (
        "server/runner.py",
        "        except TestCancelled as e:\n"
        "            # 用户中途按了「停止」→ 这一项记 **stopped**(§9.3), 不是 error。\n"
        "            # 没跑完 ≠ 不合格: 记成 error 会把整轮判成 failed(2026-10-28)。\n"
        '            result = TestResult(status="stopped", error=str(e) or "用户中途停止")\n'
        "            self.results[name] = result\n",
        "",
        "RUNNER: 不认 TestCancelled (测试项抛出来就落进 error, 整轮判 fail)",
    ),
    (
        "server/tests/_tools.py",
        '    rc, _out, err = await run_cmd(["rm", "-f", path], timeout=timeout)',
        "    if os.path.exists(path):\n"
        "        os.unlink(path)\n"
        '    rc, _out, err = 0, "", ""',
        "TOOLS: remove_file_async 退回同步 os.unlink (事故的根)",
    ),
]


def build_copy(dst, rel, new_text):
    """把 server/ + config.yaml + 那个测试文件复制到 dst, 并把 rel 写成 new_text。

    每个文件**只写一次** —— 被变异的那个根本不复制进来, 直接写变异后的
    内容。全程不覆写任何已存在的文件, 所以跑完不需要还原。
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
                continue          # 这个下面单独写
            shutil.copy2(os.path.join(root, fn), os.path.join(outroot, fn))

    shutil.copy2(os.path.join(REPO, "config.yaml"),
                 os.path.join(dst, "config.yaml"))
    os.makedirs(os.path.join(dst, "tests"), exist_ok=True)
    shutil.copy2(os.path.join(REPO, "tests", "test_tfcard_hang.py"),
                 os.path.join(dst, "tests", "test_tfcard_hang.py"))

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
    root = tempfile.mkdtemp(prefix="mut_tfhang_")
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
            #   测试自己崩了(语法错/导入错/异常)。这不算「咬得动」——
            #   崩了只能证明「跑不起来」, 证明不了「断言抓得住这个缺陷」。
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
