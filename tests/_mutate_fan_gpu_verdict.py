# 变异测试: 故意把「风扇 / GPU 的判定口径」改回旧样子, 看
# tests/test_fan_verdict.py + tests/test_gpu_scene_judgement.py 会不会红。
#
# 为什么要有这么个东西: 「测试全绿」本身说明不了什么 —— 断言写错了、
# 写松了、或者压根没跑到, 都是绿的。唯一能证明这测试**咬得动**的办法,
# 就是亲手把代码改坏, 看它是不是真的红。
#
# 这一份守的是 2026-10-29 的判定口径改动(风扇 + GPU 两项):
#   · 风扇 IO 层异常       → error   (原来 failed)
#   · 风扇只读模式         → skipped (原来 passed)
#   · 风扇通过时的文案     → 不再声称「虚焊/不转被覆盖到了」
#   · GPU 一个场景都没渲染 → error   (原来 failed)
#   · GPU 场景数是清单前缀 → stopped (原来 failed)
#
# 这几处改坏的后果都是**冤枉被测设备** —— 好板子因为 IO 抖一下、或者因为
# 工人误关了全屏窗口, 被判成不合格送去修。
#
# 做法: **每个变异单开一个全新目录**, 把本仓库自己的 server/ + config.yaml
# + 那两个测试文件复制进去(源就是**这个仓库**, 不是任何外部下载的东西), 在
# **副本**上做一处改动, 在副本目录里跑测试。
# 被改的那个文件是从头到尾**只写一次**(直接写变异后的内容), 别的文件原样
# 复制 —— 所以整个过程不覆写任何已存在的文件, 跑完也不用还原。
#
# ⚠️ 只在**副本**上改 —— 仓库里 server/ 是产线在跑的那份, 改坏了没人知道。
#
# 跑法: .venv/bin/python tests/_mutate_fan_gpu_verdict.py
import os
import shutil
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(REPO, ".venv", "bin", "python")

FAN_TEST = "tests.test_fan_verdict"
GPU_TEST = "tests.test_gpu_scene_judgement"

# 每个变异: (文件, from, to, 说明, 跑哪个测试)。
# from 必须在源文件里**找得到**, 否则算失败。
MUTATIONS = [
    (
        "server/tests/fan.py",
        '            result.status = "error"\n'
        '            result.error = (\n'
        '                f"{len(mismatch)}/{len(steps)} 步写后回读不一致 —— "',
        '            result.status = "failed"\n'
        '            result.error = (\n'
        '                f"{len(mismatch)}/{len(steps)} 步写后回读不一致 —— "',
        "风扇: 回读不一致又判回 failed (IO 异常 = 板子不合格 → **冤枉被测设备**)",
        FAN_TEST,
    ),
    (
        "server/tests/fan.py",
        '            result.status = "error"\n'
        '            result.error = (\n'
        '                f"{bad}/{cycles} 次读失败, 读出电平 {sorted(levels)} —— 读回不稳定 —— "',
        '            result.status = "failed"\n'
        '            result.error = (\n'
        '                f"{bad}/{cycles} 次读失败, 读出电平 {sorted(levels)} —— 读回不稳定 —— "',
        "风扇: 只读模式读回不稳又判回 failed",
        FAN_TEST,
    ),
    (
        "server/tests/fan.py",
        '            result.status = "skipped"\n'
        '            result.metrics["skipped_reason"] = "readonly_no_drive"',
        '            result.status = "passed"\n'
        '            result.metrics["skipped_reason"] = "readonly_no_drive"',
        "风扇: 只读模式又判回 passed (对**没测过**风扇转动的事给结论 = 静默的通过)",
        FAN_TEST,
    ),
    (
        "server/tests/fan.py",
        '                f"[风扇]   这一项证明的是**控制链闭环**(写进去能读回来)。\\n"\n',
        '                f"[风扇]   风扇已确认能启停 —— 虚焊/不转这条被本项覆盖到了。\\n"\n',
        "风扇: 通过时的文案又声称「虚焊/不转被本项覆盖到了」 (与 §6.8 覆盖缺口说反)",
        FAN_TEST,
    ),
    (
        "server/tests/gpu.py",
        "    if not rendered and score is None:\n"
        '        return "error", (',
        "    if not rendered and score is None:\n"
        '        return "failed", (',
        "GPU: 一个场景都没渲染出来 → 又判回 failed (工具没跑起来 = 板子不合格)",
        GPU_TEST,
    ),
    (
        "server/tests/gpu.py",
        "    if not rendered and score is None:\n"
        '        return "error", (\n'
        '            f"{tag}glmark2 **一个场景都没渲染出来**就退出了 (exit code = {rc}) —— "\n'
        '            f"**工具/环境没跑起来, 这一项没测成**。\\n"\n'
        '            f"常见原因: sudo 提权没成功 / 图形会话连不上 / 二进制起不来。\\n"\n'
        '            f"**不是「板子不合格」** —— 不判 failed(§9.3: 不冤枉被测设备)。\\n"\n'
        '            f"输出末尾:\\n{out}"\n'
        "        )\n\n",
        "",
        "GPU: 「一个场景都没渲染出来 → error」那条整个删掉 "
        "(0 场景时又落回 rc != 0 → failed)",
        GPU_TEST,
    ),
    (
        "server/tests/gpu.py",
        '                return "stopped", (\n'
        '                    f"{tag}glmark2 **提前退出了** (exit code 0), 只渲染了 "',
        '                return "failed", (\n'
        '                    f"{tag}glmark2 **提前退出了** (exit code 0), 只渲染了 "',
        "GPU: 场景数是清单前缀 → 又判回 failed (工人误关窗口 = 板子不合格)",
        GPU_TEST,
    ),
    # ── 2026-10-29 追加: 提权收尾那一路 (G11) ──
    (
        "server/tests/gpu.py",
        "        for _pid, _comm, _state, pgrp, cmdline in members:\n"
        "            if cmdline.startswith(bin_path):\n"
        "                return pgrp\n",
        "        for _pid, _comm, _state, pgrp, cmdline in members:\n"
        "            if cmdline.startswith(bin_path):\n"
        "                return self.pgid\n",
        "GPU 收尾: 又去杀「我们起的那个进程」的空组 (打空组 → root 的 glmark2 "
        "收不到信号, 却报「窗口应已消失」)",
        GPU_TEST,
    ),
    (
        "server/tests/gpu.py",
        "        for _pid, _comm, _state, _pgrp, cmdline in _session_members(self.sid):\n"
        "            if cmdline.startswith(bin_path):\n"
        "                return True\n",
        "        for _pid, _comm, _state, cmdline in _proc_group_members(self.pgid):\n"
        "            if cmdline.startswith(bin_path):\n"
        "                return True\n",
        "GPU 安全网: 又按进程组找「glmark2 还在不在」 (那个组里没有它 → "
        "永远认不出 → root 的 glmark2 赖着不走时没人收)",
        GPU_TEST,
    ),
    (
        "server/tests/gpu.py",
        "            for _ in range(wait_rounds):\n"
        "                await asyncio.sleep(0.2)\n"
        "                if not _proc_group_members(target):\n"
        "                    if ctx:\n"
        "                        await ctx.on_log(\n"
        "                            f\"[GPU] {reason}: 进程组 {target} 已清空 —— \"\n"
        "                            f\"全屏渲染进程不在了, 窗口应已消失\")\n"
        "                    return True\n",
        "            for _ in range(wait_rounds):\n"
        "                await asyncio.sleep(0.2)\n"
        "                if not _proc_group_members(target):\n"
        "                    if ctx:\n"
        "                        await ctx.on_log(\n"
        "                            f\"[GPU] {reason}: 进程组 {target} 已清空 —— \"\n"
        "                            f\"全屏渲染进程不在了, 窗口应已消失\")\n"
        "                    return True\n"
        "            return True          # ← 变异: 不验证就报成功\n",
        "GPU 收尾: 信号发出去就**不验证**、直接报成功 (收不掉也报「窗口应已消失」)",
        GPU_TEST,
    ),
    (
        "server/tests/gpu.py",
        "    outsiders = [m for m in members if m[4] != session]\n"
        "    if outsiders:\n"
        "        names = \", \".join(f\"{m[0]}({m[1]}, sid={m[4]})\" for m in outsiders[:5])\n"
        "        return False, (f\"组里有不属于我们会话 {session} 的进程 ({names}) —— \"\n"
        "                       f\"拒绝 (防 pid 复用撞车)\")\n",
        "",
        "GPU 提权前提: 去掉「组里每个进程都必须属于我们会话」这道 (pid 复用撞车)",
        GPU_TEST,
    ),
    (
        "server/tests/gpu.py",
        "    if not isinstance(session, int) or session <= 1:\n"
        "        return False, \"没说是哪个会话的进程组, 拒绝 (会话才是防 pid 复用的依据)\"\n",
        "",
        "GPU 提权前提: 去掉「必须说明是哪个会话」这道",
        GPU_TEST,
    ),
    (
        "server/tests/gpu.py",
        "    return [(pid, comm, state, pgrp, cmdline)\n"
        "            for (pid, comm, state, pgrp, s, cmdline) in _scan_proc()\n"
        "            if s == sid]\n",
        "    return [(pid, comm, state, pgrp, cmdline)\n"
        "            for (pid, comm, state, pgrp, s, cmdline) in _scan_proc()\n"
        "            if pgrp == sid]\n",
        "找那棵树: 又**按进程组**找 (会话才是可靠的标识 —— 进程组里没有 "
        "glmark2, 于是永远找错/找不到)",
        GPU_TEST,
    ),
]


def build_copy(dst, rel, new_text):
    """把本仓库的 server/ + config.yaml + 那两个测试文件复制到 dst。

    被变异的那个文件**只写一次** —— 根本不复制进来, 直接写变异后的内容。
    目标目录 `dst` 是**这个变异专属的新目录**(外面套着 mkdtemp), 里面
    每个文件都只写一次 —— 全程不覆写任何已存在的文件, 跑完不用还原。
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
    for t in ("test_fan_verdict.py", "test_gpu_scene_judgement.py"):
        shutil.copy2(os.path.join(REPO, "tests", t),
                     os.path.join(dst, "tests", t))

    outp = os.path.join(dst, rel)
    os.makedirs(os.path.dirname(outp), exist_ok=True)
    with open(outp, "w", encoding="utf-8") as f:
        f.write(new_text)


def run_one(work, test_mod):
    env = dict(os.environ)
    env["PYTHONPATH"] = work
    proc = subprocess.run(
        [PY, "-m", test_mod],
        cwd=work, env=env, capture_output=True, text=True, timeout=300,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def main():
    root = tempfile.mkdtemp(prefix="mut_fan_gpu_verdict_")
    print(f"副本都在 {root} 下面\n")

    bad = 0
    for i, (rel, frm, to, desc, test_mod) in enumerate(MUTATIONS):
        srcp = os.path.join(REPO, rel)
        with open(srcp, encoding="utf-8") as f:
            orig = f.read()
        if frm not in orig:
            print(f"\n✗ {desc}\n   → 变异点没找到(源文件变了?): {frm[:60]!r}")
            bad += 1
            continue
        if orig.count(frm) != 1:
            print(f"\n✗ {desc}\n   → 变异点在源文件里出现 {orig.count(frm)} 次, "
                  f"不唯一 —— 这个变异脚本自己写错了")
            bad += 1
            continue

        work = os.path.join(root, f"m{i:02d}")
        os.makedirs(work, exist_ok=True)
        build_copy(work, rel, orig.replace(frm, to))

        code, out = run_one(work, test_mod)
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
