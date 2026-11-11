# 变异测试: 故意把「网口静态兜底」改坏, 看
# tests/test_ethernet_static_fallback.py 会不会红。
#
# 为什么要有这么个东西: 「测试全绿」本身说明不了什么 —— 断言写错了、
# 写松了、或者压根没跑到, 都是绿的。唯一能证明这测试**咬得动**的办法,
# 就是亲手把代码改坏, 看它是不是真的红。
#
# 这一份守的是 2026-11-11 挖出来的两个坑:
#   ① 静态兜底配的是**配置里写死的** 192.168.1.50/24 ⇒ 而现场工位的
#      网段是 192.168.16.0/23 ⇒ 不搭界 ⇒ 静态兜底白配 ⇒ 再按「配上了
#      仍 ping 不通」判 **failed** ⇒ **冤枉好板子**;
#   ② 我们自己 `ip addr add` 加上去的地址**测完没删** ⇒ 留在板子上
#      (下一轮 DHCP 拿到 lease ⇒ 网卡上两个地址; 工人排查方向跑偏)。
#
# 做法: **每个变异单开一个全新目录**, 把本仓库自己的 server/ + config.yaml
# + 那个测试文件复制进去, 在**副本**上做一处改动, 在副本目录里跑测试。
# 被改的那个文件从头到尾**只写一次** —— 全程不覆写任何已存在的文件。
#
# 跑法: .venv/bin/python tests/_mutate_ethernet_static_fallback.py
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests._tmp import mkdtemp                       # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(REPO, ".venv", "bin", "python")
TEST = "tests.test_ethernet_static_fallback"

# 每个变异: (文件, from, to, 说明)。from 必须在源文件里**找得到且唯一**。
MUTATIONS = [
    (
        "server/tests/ethernet.py",
        "            static_ip_use = _static_ip_for_gateway(static_ip, gw_static)",
        "            static_ip_use = static_ip",
        "ETH: 静态兜底又用**配置里写死**的地址(跟工位网段不搭界 ⇒ 静态"
        "兜底白配 ⇒ 再判 failed ⇒ **冤枉好板子**)",
    ),
    (
        "server/tests/ethernet.py",
        '    return f"{gw_net}.50/24"',
        "    return static_ip",
        "ETH: 算地址时**不按工位网段推**(回到老行为)",
    ),
    (
        "server/tests/ethernet.py",
        "        addr = self._added_static\n        self._added_static = \"\"\n"
        "        if not addr:\n            return",
        "        addr = self._added_static\n        self._added_static = \"\"\n"
        "        if True:\n            return",
        "ETH: 收尾**不删**我们加上去的静态地址(留在板子上)",
    ),
    (
        "server/tests/ethernet.py",
        "        finally:\n            await self._cleanup_static(ctx)",
        "        finally:\n            pass",
        "ETH: 收尾压根**不调**清理(哪怕这一轮抛异常, 地址也留在板上)",
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
    for extra in ("test_ethernet_static_fallback.py", "_tmp.py"):
        shutil.copy2(os.path.join(REPO, "tests", extra),
                     os.path.join(dst, "tests", extra))

    outp = os.path.join(dst, rel)
    os.makedirs(os.path.dirname(outp), exist_ok=True)
    with open(outp, "w", encoding="utf-8") as f:
        f.write(new_text)


def main():
    root = mkdtemp(prefix="mut_ethstatic_")
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
