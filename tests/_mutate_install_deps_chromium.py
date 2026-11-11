# 变异测试: 故意把 `scripts/install_deps.sh` 的 chromium 防护改坏, 看
# tests/test_install_deps_chromium.py 会不会红。
#
# 为什么要有这么个东西: 「测试全绿」本身说明不了什么 —— 断言写错了、
# 写松了、或者压根没跑到, 都是绿的。唯一能证明这个测试**咬得动**的办法,
# 就是亲手把代码改坏, 看它是不是真的红。
#
# 这一份守的是: **别把 chromium 又加回关键包清单**。
# 加回去的后果不是「多装一个浏览器」, 而是 apt 把出厂镜像自带的
# chromium 112 换成 focal 的 snap 过渡空壳 ⇒ **PDF 报告静默变空白**
# (不报错) ⇒ 产线以为导出坏了, 实际是自己把浏览器换没了。
#
# 做法: **每个变异单开一个全新目录**, 把脚本复制进去, 在副本上改一处,
# 用 AGING_INSTALL_DEPS_SH 指过去跑测试。
# 被改的那个文件从头到尾**只写一次** —— 全程不覆写任何已存在的文件。
#
# 跑法: .venv/bin/python tests/_mutate_install_deps_chromium.py
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests._tmp import mkdtemp                       # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(REPO, ".venv", "bin", "python")
TEST = "tests.test_install_deps_chromium"
TOOL_REL = os.path.join("scripts", "install_deps.sh")

# 每个变异: (文件, from, to, 说明)。from 必须在源文件里**找得到且唯一**。
# ⚠️ **第一个元素必须是字面量** —— `tests/_sweep_anchors.py` 要静态查锚点,
#    写成变量它就 literal_eval 不了 ⇒ 整个扫描停摆。
MUTATIONS = [
    (
        "scripts/install_deps.sh",
        '    "fonts-wqy-zenhei"\n)',
        '    "chromium-browser"\n    "fonts-wqy-zenhei"\n)',
        "把 chromium-browser 放回关键包清单 ⇒ apt 装它 ⇒ 换掉镜像自带的 "
        "chromium 112 ⇒ PDF 报告静默变空白",
    ),
    (
        "scripts/install_deps.sh",
        '    echo "    ⚠ **别再 apt 装 chromium-browser** —— focal 上那是 '
        'snap 过渡空壳,"\n'
        '    echo "      装它会把这份能用的 chromium 换掉 ⇒ PDF 报告静默变空白"',
        '    echo "    (说明略)"',
        "把「为什么不能装」的解释删掉 ⇒ 光有禁令没有理由 ⇒ 下一个人会加回去",
    ),
    (
        "scripts/install_deps.sh",
        'echo "── PDF 导出用的 chromium (只查, 不装) ──"',
        'echo "── PDF 导出用的 chromium ──"',
        "把「只查, 不装」这几个字删了 ⇒ 读者以为后面还有 apt 安装",
    ),
    (
        "scripts/install_deps.sh",
        '    echo "    ⇒ **不要**用 apt 的 chromium-browser 顶(snap 空壳, '
        '换了更糟)"',
        '    echo "    ⇒ **不要**用 apt 的 chromium-browser 顶(snap 空壳, '
        '换了更糟)"\n    apt-get install -y chromium-browser',
        "嘴上说别装, 底下真装 ⇒ 测试要能看出**代码**里有 apt 装 chromium",
    ),
]


def main():
    root = mkdtemp(prefix="mut_installchrom_")
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

        work = os.path.join(root, "m%02d" % i)
        os.makedirs(os.path.join(work, os.path.dirname(rel)), exist_ok=True)
        # 被改的那份**只写一次** —— 全程不覆写任何已存在的文件。
        outp = os.path.join(work, rel)
        with open(outp, "w", encoding="utf-8") as f:
            f.write(orig.replace(frm, to))

        env = dict(os.environ)
        env["AGING_INSTALL_DEPS_SH"] = outp
        proc = subprocess.run([PY, "-m", TEST], cwd=REPO, env=env,
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
