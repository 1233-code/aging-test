# 变异测试: 故意把「GPU 项的 X 会话探测」改坏, 看
# tests/test_gpu_xauthority.py 会不会红。
#
# 为什么要有这么个东西: 「测试全绿」本身说明不了什么 —— 断言写错了、
# 写松了、或者压根没跑到, 都是绿的。唯一能证明这测试**咬得动**的办法,
# 就是亲手把代码改坏, 看它是不是真的红。
#
# 这一份守的是 2026-10-29 新板**第一轮** GPU 判 error 的教训:
# 那轮板子刚开机十来秒, 厂商那套 x11vnc/xhost 启动脚本还没建出
# `~/.Xauthority`, 于是我们「env 里的 XAUTHORITY 指的文件不存在 ⇒
# 认证不过 ⇒ 判 error」—— 一台 GPU 完全正常的板子被自己的前置条件挡死,
# glmark2 **一次都没起来过**。
#
# 查实(2026-11-02, 老板子):
#   · 真正在用的 cookie 是 X server **自己命令行里的 -auth**
#     (`/var/run/slim.auth`, root 独占 0600), 跟 `~/.Xauthority` 是两份
#   · 这个 X server **压根没强制 MIT cookie 认证** —— 拿假 cookie、甚至
#     完全不设 XAUTHORITY, xdpyinfo 都照样连上
#   ⇒ 「找不到 cookie 文件」**不等于**「连不上」
#
# 所以这一份守两条:
#   ① cookie 的**权威来源**是 X server 自己命令行里的 `-auth`
#   ② 前置检查**实连一次**才算数 —— 连不上要判不过, 且把 X server 的原话
#      写进报告
#
# 做法: **每个变异单开一个全新目录**, 把 server/ + config.yaml + 那个测试
# 文件复制进去, 在**副本**上做一处改动, 在副本目录里跑测试。
# 被改的那个文件是从头到尾**只写一次**(直接写变异后的内容), 别的文件原样
# 复制 —— 所以整个过程不覆写任何已存在的文件, 跑完也不用还原。
# ⚠️ 仓库里 server/ 是产线在跑的那份, **只在副本上改**。
#
# 临时目录用 `tests._tmp.mkdtemp` —— 进程退出时自动删掉, 不在 /tmp 里留残骸。
#
# 跑法: .venv/bin/python tests/_mutate_gpu_xauth.py
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests._tmp import mkdtemp                       # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(REPO, ".venv", "bin", "python")
TEST = "tests.test_gpu_xauthority"

# 每个变异: (文件, from, to, 说明)。from 必须在源文件里**找得到**, 否则算失败。
MUTATIONS = [
    (
        "server/tests/gpu.py",
        "        if os.path.basename(argv[0]) in _XSERVER_NAMES:\n"
        "            argvs.append(argv)",
        "        if False:\n"
        "            argvs.append(argv)",
        "GPU: 不扫 /proc 找 X server 了(cookie 的权威来源就断了)",
    ),
    (
        "server/tests/gpu.py",
        "    # ① X server 自己的 -auth —— 最权威\n"
        "    if server_auth:\n"
        '        hit = _take(server_auth, "X server 自己命令行里的 -auth")\n'
        "        if hit:\n"
        "            return hit",
        "    # ① X server 自己的 -auth —— 最权威\n"
        "    if False:\n"
        '        hit = _take(server_auth, "X server 自己命令行里的 -auth")\n'
        "        if hit:\n"
        "            return hit",
        "GPU: cookie 不再以 X server 的 -auth 为权威(退回猜 ~/.Xauthority)",
    ),
    (
        "server/tests/gpu.py",
        "    if probed is True:\n"
        "        tail = (f\", XAUTHORITY={info.path}({info.source})\" if info.path\n"
        "                else \", 未开访问控制, 不需要 cookie\")\n"
        "        return True, detail + f\", {why}\" + tail",
        "    if probed is True:\n"
        "        tail = (f\", XAUTHORITY={info.path}({info.source})\" if info.path\n"
        "                else \", 未开访问控制, 不需要 cookie\")\n"
        "        return False, detail + f\", {why}\" + tail",
        "GPU: 实连成功了反而判不过",
    ),
    (
        "server/tests/gpu.py",
        '        return False, f"{detail}, 但**实连失败**: {why} {hint}".strip()',
        '        return True, f"{detail}, 但**实连失败**: {why} {hint}".strip()',
        "GPU: 实连失败了也放行(那还探测个什么)",
    ),
    (
        "server/tests/gpu.py",
        "    if info.server_found and not info.server_has_auth:\n"
        "        return True, (detail + \", X server 启动时没带 -auth ⇒ 没开访问控制, \"",
        "    if info.server_found and not info.server_has_auth:\n"
        "        return False, (detail + \", X server 启动时没带 -auth ⇒ 没开访问控制, \"",
        "GPU: X server 压根没开访问控制(没带 -auth)却判不过",
    ),
    (
        "server/tests/gpu.py",
        "    if info.path:\n"
        "        env[\"XAUTHORITY\"] = info.path\n"
        "    else:\n"
        "        env.pop(\"XAUTHORITY\", None)",
        "    if info.path:\n"
        "        env[\"XAUTHORITY\"] = info.path",
        "GPU: 一个 cookie 都探测不到时, 照样把那个不存在的路径塞给子进程",
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
    for extra in ("test_gpu_xauthority.py", "_tmp.py"):
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
    root = mkdtemp(prefix="mut_xauth_")
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
