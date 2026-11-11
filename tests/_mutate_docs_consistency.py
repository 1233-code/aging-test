# 变异测试: 故意把 README / postinst 改回**跟代码打架**的样子, 看
# tests/test_docs_consistency.py 会不会红。
#
# 为什么要有这么个东西: 「测试全绿」本身说明不了什么 —— 断言写错了、
# 写松了、或者压根没跑到, 都是绿的。唯一能证明这个测试**咬得动**的办法,
# 就是亲手把东西改坏, 看它是不是真的红。
#
# 这一份守的是「文档不许说假话」:
#   · USB 项明明**只写**, 文档写成「写30s + 读30s」;
#   · 把**不能装**的 chromium-browser 列成「关键工具」(照着装就把镜像自带的
#     chromium 112 换成 snap 空壳 ⇒ PDF 报告静默变空白);
#   · scripts/ 清单漏掉真实存在的脚本(工人根本不知道它存在);
#   · 把「N 个工具 .deb」写死 ⇒ 清单一漂就成了假话。
#
# 做法: **每个变异单开一个全新目录**, 把 README / postinst 复制进去,
# 在副本上改一处, 用 AGING_README / AGING_POSTINST 指过去跑测试。
# 被改的那些文件从头到尾**只写一次** —— 全程不覆写任何已存在的文件,
# 仓库里的 README / postinst **一个字都不动**。
#
# 跑法: .venv/bin/python tests/_mutate_docs_consistency.py
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests._tmp import mkdtemp                       # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(REPO, ".venv", "bin", "python")
TEST = "tests.test_docs_consistency"
README_REL = "README.md"
POSTINST_REL = os.path.join("deb", "postinst")

# 每个变异: (改哪个文件, from, to, 说明)。from 必须**找得到且唯一**。
# ⚠️ **第一个元素必须是字面量** —— `tests/_sweep_anchors.py` 要静态查锚点,
#    写成变量(`README_REL`)它就 literal_eval 不了 ⇒ 整个扫描停摆。
MUTATIONS = [
    (
        "README.md",
        "`fio` (**只写 30s** —— **只写不读**, docs §6.5)",
        "`fio` (写30s + 读30s)",
        "README: USB 项又写成「写30s + 读30s」⇒ 把**没做的读测试**说成做了",
    ),
    (
        "README.md",
        "`x11-xserver-utils` (`xrandr`, HDMI 读当前实际输出), "
        "`fonts-wqy-zenhei`",
        "`x11-xserver-utils` (`xrandr`, HDMI 读当前实际输出), "
        "`chromium-browser` (PDF 报告导出), `fonts-wqy-zenhei`",
        "README: 把 chromium-browser 又列回「关键工具」⇒ 照着装就换掉镜像"
        "自带的 chromium 112 ⇒ PDF 报告静默变空白",
    ),
    (
        "README.md",
        "│       ├── pdf_report_text.py        # 报告 PDF 文本抽检 (空白报告自查)\n",
        "",
        "README: scripts/ 清单里漏掉一个真实存在的脚本",
    ),
    (
        "README.md",
        "**27 个 .deb**(17 个工具 + 10 个运行时共享库)",
        "**27 个 .deb**(16 个工具 + 10 个运行时共享库)",
        "README: 写死的工具个数跟 TOOL_PKGS 对不上",
    ),
    (
        "deb/postinst",
        "#         不改 dpkg 数据库; 工具只解**点名清单**里的那些包",
        "#         不改 dpkg 数据库; 工具只解点名的那 16 个包",
        "postinst: 又把包个数**写死**了(清单一漂就成了假话)",
    ),
]


def main():
    def read_text(path):
        with open(path, encoding="utf-8") as f:
            return f.read()

    root = mkdtemp(prefix="mut_docs_")
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
        os.makedirs(os.path.join(work, "deb"), exist_ok=True)
        # 两个文件**都要**在副本里(测试两个都读) ⇒ 先把没改的那份也拷过去。
        # 每个副本文件**只写一次** —— 全程不覆写任何已存在的文件。
        with open(os.path.join(work, README_REL), "w", encoding="utf-8") as f:
            if rel == README_REL:
                f.write(orig.replace(frm, to))
            else:
                f.write(read_text(os.path.join(REPO, README_REL)))
        with open(os.path.join(work, POSTINST_REL), "w", encoding="utf-8") as f:
            if rel == POSTINST_REL:
                f.write(orig.replace(frm, to))
            else:
                f.write(read_text(os.path.join(REPO, POSTINST_REL)))

        env = dict(os.environ)
        env["AGING_README"] = os.path.join(work, README_REL)
        env["AGING_POSTINST"] = os.path.join(work, POSTINST_REL)
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
