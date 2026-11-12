# 变异测试: 故意把「板子死在哪一项上」这条通道改坏, 看
# tests/test_item_marker.py 会不会红。
#
# 为什么要有这么个东西: 「测试全绿」本身说明不了什么 —— 断言写错了、
# 写松了、或者压根没跑到, 都是绿的。唯一能证明这测试**咬得动**的办法,
# 就是亲手把代码改坏, 看它是不是真的红。
#
# 这一份守的是 2026-11-12 产线反馈换来的规矩:
#   另一台设备上, 板子在 **GPU 项**跑着的时候弹出内核崩溃画面
#   (`crashdump now, please connect to tigerdump tool and keep power on`),
#   板子挂死 ⇒ 工人断电重上 ⇒ 那一轮卡在「运行中」。
#   而 GPU / 内存这两项死在半路, 现有两条通道**都答不上来**
#   (journal 尾巴里认不出 glmark2/memtester; CPU 现场记录只管 CPU 项)
#   ⇒ 那一项只能判 `stopped` ⇒ **坏板子被放行**。
#
#   ⇒ 这条通道必须守住这几条:
#     ① 每项**起跑**写一行、**跑完**写一行;
#     ② 每一行都 **fsync**(硬复位带不走页缓存);
#     ③ 认「死在哪一项」= 最后一行是「起跑」**且**开机 ID 变了;
#     ④ 板子**没**重启过 ⇒ **不许**赖板子(冤枉好板比漏判边缘板更糟);
#     ⑤ 写不进去就当没有 —— 绝不能把整个测试搞挂。
#
# ⚠️ **这些改动全部只发生在临时副本里** —— 仓库里的文件一个字都不动。
#   副本跑完就扔, 不用还原。
#
# 跑法: .venv/bin/python tests/_mutate_item_marker.py
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests._tmp import mkdtemp                       # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(REPO, ".venv", "bin", "python")
TEST = "tests.test_item_marker"

# 每个变异: (文件, from, to, 说明)。from 必须在源文件里**找得到**, 否则算失败。
MUTATIONS = [
    (
        "server/item_marker.py",
        "    if rec_boot == cur_boot:\n"
        "        return None        # 板子没重启过 ⇒ 是我们自己的进程没了\n",
        "    if False:\n"
        "        return None\n",
        "板子**没重启过**也照样赖它"
        "(⇒ 服务自己没了也判板子不合格 ⇒ 冤枉好板)",
    ),
    (
        "server/item_marker.py",
        '    if not last or last[0] != "start":\n',
        '    if not last:\n',
        "最后一行是「结束」也说它死在那一项"
        "(⇒ 这一项明明跑完了, 却被判失败)",
    ),
    (
        "server/item_marker.py",
        "    if not rec_boot or not cur_boot:\n"
        "        return None        # 读不到开机 ID ⇒ 「不知道」⇒ 不乱猜\n",
        "    if not rec_boot or not cur_boot:\n"
        "        cur_boot = cur_boot or 'x'\n",
        "读不到开机 ID 也往下猜",
    ),
    (
        "server/item_marker.py",
        '            self._fh.write(line + "\\n")\n'
        "            self._fh.flush()\n"
        "            os.fsync(self._fh.fileno())\n",
        '            self._fh.write(line + "\\n")\n'
        "            self._fh.flush()\n",
        "写完不 fsync"
        "(⇒ 硬复位带走了页缓存 ⇒ 恰恰最该有证据的一次, 证据没了)",
    ),
    (
        "server/item_marker.py",
        '            self._fh.write(f"{MARK_BOOT_ID_MARK} {boot or \'未知\'}\\n")\n',
        '            self._fh.write("")\n',
        "文件头不记开机 ID"
        "(⇒ 事后没法确认板子到底重启过没有)",
    ),
    (
        "server/runner.py",
        "                self._marker.start(name)\n",
        "                pass\n",
        "runner 不写「起跑」"
        "(⇒ 压根不知道死在哪一项上)",
    ),
    (
        "server/runner.py",
        "                self._marker.done(name)\n",
        "                pass\n",
        "runner 不写「结束」"
        "(⇒ 跑完了也还停在「起跑」⇒ 误判成死在那一项)",
    ),
    (
        "server/main.py",
        "                                item = item_marker.marker_cut_short(r.get(\"id\"))\n",
        "                                item = None\n",
        "收尾时不用这条通道"
        "(⇒ 板子死在 GPU 项上, 那一项还是判 `stopped` ⇒ 坏板子被放行)",
    ),
]


def build_copy(dst, rel, new_text):
    """把 server/ + config.yaml + 测试文件复制到 dst, 并把 rel 写成 new_text。

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
    # ⚠️ **必须把测试依赖的模块一起复制** —— 少了 `tests/_tmp.py`,
    #    变异跑起来直接 ImportError ⇒ 一条都验不到(2026-11-09 踩过)。
    for fn in ("test_item_marker.py", "_tmp.py"):
        shutil.copy2(os.path.join(REPO, "tests", fn),
                     os.path.join(dst, "tests", fn))

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
    root = mkdtemp(prefix="mut_item_marker_")
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
        if orig.count(frm) != 1:
            print(f"\n✗ {desc}\n   → 变异点**不唯一**({orig.count(frm)} 处) "
                  f"⇒ 证据不成立: {frm[:60]!r}")
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
