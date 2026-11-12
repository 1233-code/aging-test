# 变异测试: 故意把「失败原因说不清楚」这个毛病放回去, 看
# tests/test_failure_reasons.py 会不会红。
#
# 为什么要有这么个东西: 「测试全绿」本身说明不了什么 —— 断言写错了、
# 写松了、或者压根没跑到, 都是绿的。唯一能证明这测试**咬得动**的办法,
# 就是亲手把代码改坏, 看它是不是真的红。
#
# 这一份守的是 2026-11-12 查出来的那一整类毛病:
#   · CPU 项 stress-ng 退出码非 0 那条路**一个字都没写** ⇒ 报告上这一项
#     是「不合格」+ 原因栏是空的;
#   · 缺工具的补救办法**写死** `sudo apt install …` ⇒ U 盘版上这条命令
#     是**死的**(盘上自带 fio / glmark2, 其余工具用板子出厂那份);
#   · 原因框里塞**裸异常 repr** ⇒ 工人看到的是一段代码;
#   · 报告模板印「(未记录原因 —— 属实现缺陷)」⇒ 工人以为板子坏了。
#
# 做法: **每个变异单开一个全新目录**, 把 server/ + config.yaml + 那个测试
# 文件复制进去, 在**副本**上改一处, 用 PYTHONPATH 指过去跑测试。
# 被改的那份**只写一次** —— 全程不覆写任何已存在的文件。
#
# 跑法: .venv/bin/python tests/_mutate_failure_reasons.py
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests._tmp import mkdtemp                       # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(REPO, ".venv", "bin", "python")
TEST = "tests.test_failure_reasons"

# 每个变异: (文件, from, to, 说明)。from 必须在源文件里**找得到且唯一**。
# ⚠️ 这四处**必须写字面量** —— tests/_sweep_anchors.py 要静态查锚点,
#    写成变量它就 literal_eval 不了 ⇒ 那条变异从来没被静态验过。
MUTATIONS = [
    # ── CPU 项: 那条「一个字都没写」的路(2026-11-12 修的主洞) ──
    (
        "server/tests/cpu.py",
        "                    result.error = tool_exit_reason(\n"
        "                        round_rc, tool=\"stress-ng\", stage=label,\n"
        "                        log_tail=round_text,\n"
        "                        extra=(\"这一轮加压**没跑完** ⇒ 按 §6.2「通过 = 退出码 0 \"\n"
        "                               \"且峰值温度 ≤ 判定线」, 这一项**判不合格**。\"\n"
        "                               \"⚠️ 退出码不等于板子坏了 —— 请先看下面这行原始\"\n"
        "                               \"输出, 再决定是返修还是重测。\"))",
        "                    result.error = None  # 变异: 抹掉原因",
        "CPU: stress-ng 退出码非 0 **不写原因** ⇒ 报告上「不合格」+ 原因栏空的",
    ),
    (
        "server/tests/cpu.py",
        "            result.error = result.error or (\n"
        "                \"CPU 压力测试没通过 —— 但**没记录到具体原因**(stress-ng 的\"\n"
        "                \"原始输出在本项日志里)。常见原因: 加压中途 stress-ng 自己退出\"\n"
        "                \"了 / 达到了温度收手线。请看本项日志, 再决定复测还是返修。\"\n"
        "            )",
        "            result.error = None  # 变异: 兜底也没了",
        "CPU: 判 failed 的**兜底原因**被抹掉 ⇒ 走到底也不写原因",
    ),
    # ── 内存项: 同类的洞(退出码非 0 那条路) ──
    (
        "server/tests/memory.py",
        "                        result.error = tool_exit_reason(\n"
        "                            rc, tool=\"memtester\",\n"
        "                            stage=f\"第 {round_idx}/{times} 轮 (跑了 {elapsed:.0f}s)\",\n"
        "                            log_tail=result.log,\n"
        "                            extra=(\"这一轮**没跑满挂钟**(上限 \"\n"
        "                                   f\"{timeout_sec}s)就退了 ⇒ 按 §6.10 判\"\n"
        "                                   \"**不合格**。⚠️ 退出码不等于板子坏了 —— \"\n"
        "                                   \"先看下面这行原始输出, 再决定返修还是复测。\"))",
        "                        result.error = None  # 变异: 抹掉原因",
        "内存: memtester 退出码非 0 **不写原因** ⇒ 那一项只剩「不合格」",
    ),
    # ── 助手函数的措辞 ──
    (
        "server/tests/_tools.py",
        "        f\"未找到 {tools} —— **这一项没测成**(工具没装 / 没带齐, 跟被测设备\"",
        "        f\"未找到 {tools} —— (工具没装 / 没带齐, 跟被测设备\"",
        "TOOLS: 缺工具时**不说「这一项没测成」** ⇒ 工人会读成「这盘不能用」",
    ),
    (
        "server/tests/_tools.py",
        "    msg = (f\"{head} —— 这一项**没测成**, \"\n"
        "           f\"**不代表被测设备不合格**(§9.3: 没测成 ≠ 不合格)。\")",
        "    msg = f\"{head} —— 这一项没测成。\"",
        "TOOLS: 出意外时**不说「不代表被测设备不合格」** ⇒ 好板子被冤枉",
    ),
    (
        "server/tests/_tools.py",
        "    137: \"被 **SIGKILL** 杀掉(常见于**内存不够, 被 OOM killer 干掉**)\",",
        "        137: \"被杀了\",",
        "TOOLS: 退出码 137 **不翻成人话** ⇒ 工人看到「被杀了」还是不知道咋回事",
    ),
    (
        "server/tests/_tools.py",
        "    parts.append(f\"怎么办: {hint}\")",
        "        parts.append(\"怎么办: 找技术员\")",
        "TOOLS: 缺工具时**不带补救办法** ⇒ U 盘版上工人无路可走",
    ),
    (
        "server/tests/_tools.py",
        "    raw = f\"{name}: {exc}\".strip()",
        "    raw = f\"{exc!r}\".strip()",
        "TOOLS: 原始信息用 **repr** 印 ⇒ 工人看到的是一段代码",
    ),
    # ── 补救办法写死(U 盘版上是死的) ──
    (
        "server/tests/emmc.py",
        "                \"fio\", deps_fix_hint(),\n"
        "                note=\"PATH 不含 /sbin, 或这台机器上压根没装这个工具\",",
        "                \"fio\", \"sudo apt install fio\",\n"
        "                note=\"PATH 不含 /sbin, 或这台机器上压根没装这个工具\",",
        "eMMC: 补救办法**写死成 apt install** ⇒ U 盘版上照着做也装不上",
    ),
    # ── 报告模板 ──
    (
        "server/templates/report.html.j2",
        "                    {{ c.error or '（这一项没写出原因 —— 请看这一项的原始日志；重测一次若仍然如此，请把这一轮报告交给技术员）' }}",
        "                    {{ c.error or '（未记录原因 —— 属实现缺陷: failed/error 必须写 error 字段）' }}",
        "TPL: 模板又把「属实现缺陷」印回来了 ⇒ 工人以为板子坏了",
    ),
    # ── runner 的兜底 ──
    (
        "server/runner.py",
        "        if result.status in (\"stopped\", \"skipped\") and not result.error:\n"
        "            result.error = (\n"
        "                \"这一项**没跑完 / 没测成** —— 关于被测设备**还没有任何结论**。\"\n"
        "                \"没跑完 ≠ 不合格: 补上料 / 复位之后可以重测这一项。\"\n"
        "            )\n"
        "            self.results[name] = result",
        "        if result.status in (\"stopped\", \"skipped\") and not result.error:\n"
        "            pass",
        "RUNNER: stopped/skipped 的**兜底原因**被抹掉 ⇒ 未测项明细表里是空话",
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
    for extra in ("test_failure_reasons.py", "_tmp.py"):
        shutil.copy2(os.path.join(REPO, "tests", extra),
                     os.path.join(dst, "tests", extra))

    outp = os.path.join(dst, rel)
    os.makedirs(os.path.dirname(outp), exist_ok=True)
    with open(outp, "w", encoding="utf-8") as f:
        f.write(new_text)


def main():
    root = mkdtemp(prefix="mut_failreason_")
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
                              capture_output=True, text=True, timeout=600)
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
