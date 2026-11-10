# 变异测试: 故意把「缺工具的补救办法按形态分开说」改坏, 看
# tests/test_deps_fix_hint.py 会不会红。
#
# 为什么要有这么个东西: 「测试全绿」本身说明不了什么 —— 断言写错了、
# 写松了、或者压根没跑到, 都是绿的。唯一能证明这测试**咬得动**的办法,
# 就是亲手把代码改坏, 看它是不是真的红。
#
# 这一份守的是 2026-11-10 修的那个真缺陷:
#   以前 cpu / 内存 / 网口 / WiFi 四个测试项 + 两个诊断脚本都把
#   「sudo bash scripts/install_deps.sh」**写死**在报错里 ⇒ 但 U 盘版的
#   盘上根本没有那个脚本 ⇒ 工人照着做只会得到「No such file or directory」
#   ⇒ **补救办法是死的**(越是在出事的时候越没用)。
#
# ⚠️ **锚点必须唯一** —— 不唯一的话, replace 会一次改掉好几处, 证据就不成立。
#    `tests/_sweep_anchors.py` 也会静态查这一条。
# ⚠️ **这些改动全部只发生在临时副本里** —— 仓库里的脚本一个字都不动。
#
# 跑法: ./.venv/bin/python tests/_mutate_deps_fix_hint.py
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests._tmp import mkdtemp                       # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(REPO, ".venv", "bin", "python")
TEST_REL = "tests/test_deps_fix_hint.py"

# 每个变异: (目标文件, 锚点原文, 改坏成什么, 说明)。
# ⚠️ 目标文件那一栏写**字面量**(扫描器用 ast.literal_eval 解析)。
MUTATIONS = [
    # ── ① 那个共享的补救办法函数 ──────────────────────────────────────
    (
        "server/tests/_tools.py",
        "    if usb_mode():",
        "    if False:",
        "补救办法**不再按形态分**了(U 盘版也说那条命令 ⇒ 盘上根本没有它 ⇒ "
        "工人照着做只会得到「No such file or directory」)",
    ),
    # ⚠️⚠️ **2026-11-10 加: 判据的**两个信号**各掉一个都要被抓**
    #   以前 Python 只认 `AGING_TOOL_DIR`, shell 只认标记文件 ⇒ 两边能
    #   同时对不上 ⇒ 盘上有标记、tools/ 不在时, Python 会误判成设备版 ⇒
    #   那条死命令又回来了(2026-11-10 修的 P0 在另一种配置下复活)。
    (
        "server/tests/_tools.py",
        '        return bool(root and (Path(root) / "AGING_USB_MODE").is_file())',
        "        return False",
        "「标记文件」那个判据被拆掉了(只剩环境变量一个信号 ⇒ 上面那个 P0 "
        "在另一种配置下复活)",
    ),
    (
        "server/tests/_tools.py",
        '    if os.environ.get(TOOL_DIR_ENV, "").strip():\n        return True',
        "    if False:\n        return True",
        "「环境变量」那个判据被拆掉了(只剩标记文件一个信号)",
    ),
    (
        "server/tests/_tools.py",
        '        return ("U 盘版: 盘上自带 fio / glmark2-es2, 其余工具用板子出厂那份 ⇒ "\n'
        '                "缺工具或工具起不来, 说明这版出厂镜像没带齐, 请改用设备版(deb)安装")',
        '        return "sudo bash scripts/install_deps.sh"',
        "U 盘版那句**改回那条命令**(⇒ 又变成一个死掉的补救办法)",
    ),
    (
        "server/tests/_tools.py",
        '    return "请运行 sudo bash scripts/install_deps.sh"',
        '    return "请找厂家处理"',
        "设备版**丢掉那条命令**(deb 包里明明带着那个脚本 ⇒ 白白不给办法)",
    ),
    # ── ② 四个测试项: 有一个偷偷写死就红 ──────────────────────────────
    (
        "server/tests/cpu.py",
        '"error": f"未安装 stress-ng,{deps_fix_hint()}"',
        '"error": "未安装 stress-ng,sudo bash scripts/install_deps.sh"',
        "cpu 项**写死**那条命令(U 盘版上=dead 的补救办法)",
    ),
    (
        "server/tests/memory.py",
        '                f"解决: {deps_fix_hint()}"',
        '                "解决: sudo bash scripts/install_deps.sh"',
        "内存项**写死**那条命令",
    ),
    (
        "server/tests/ethernet.py",
        '                f"未找到: {\', \'.join(missing)}. 解决: {deps_fix_hint()} "',
        '                f"未找到: {\', \'.join(missing)}. 解决: sudo bash scripts/install_deps.sh "',
        "网口项**写死**那条命令",
    ),
    (
        "server/tests/wifi.py",
        '                f"未找到: {\', \'.join(missing)}. 解决: {deps_fix_hint()} "',
        '                f"未找到: {\', \'.join(missing)}. 解决: sudo bash scripts/install_deps.sh "',
        "WiFi 项**写死**那条命令",
    ),
    # ── ③ 诊断脚本 check_deps.sh ───────────────────────────────────────
    (
        "scripts/check_deps.sh",
        '[ -f "$PROJECT_ROOT/AGING_USB_MODE" ] && USB_MODE=1',
        'USB_MODE=0',
        "check_deps.sh **认不出 U 盘版**(标记文件不看 ⇒ 盘上照样教人跑"
        "那个不存在的脚本)",
    ),
    (
        "scripts/check_deps.sh",
        '        if [ "$USB_MODE" = "1" ]; then\n'
        '            echo "    修复: U 盘版: 盘上自带 fio / glmark2-es2, 其余工具用板子出厂那份 ⇒"',
        '        if false; then',
        "check_deps.sh 的 U 盘版分支**永远走不到**(⇒ 盘上照样印那条死命令)",
    ),
    (
        "scripts/check_deps.sh",
        '            echo "    修复: U 盘版: 盘上自带 fio / glmark2-es2, 其余工具用板子出厂那份 ⇒"',
        '            echo "    修复: sudo bash scripts/install_deps.sh"',
        "check_deps.sh 的 U 盘版**改回那条命令**",
    ),
    # ── ④ 诊断脚本 check_eth.sh ────────────────────────────────────────
    (
        "scripts/check_eth.sh",
        '[ -f "$PROJECT_ROOT/AGING_USB_MODE" ] && USB_MODE=1',
        'USB_MODE=0',
        "check_eth.sh **认不出 U 盘版**",
    ),
    (
        "scripts/check_eth.sh",
        '    if [ "$USB_MODE" = "1" ]; then',
        '    if false; then',
        "check_eth.sh 的 U 盘版分支**永远走不到**",
    ),
    (
        "scripts/check_eth.sh",
        '        echo "  ✗ 未装 ethtool —— U 盘版: 盘上自带 fio / glmark2-es2, 其余工具用板子出厂那份 ⇒"',
        '        echo "  ✗ 未装 ethtool —— sudo bash scripts/install_deps.sh"',
        "check_eth.sh 的 U 盘版**改回那条命令**",
    ),
    # ── ④b 判据统一: shell 侧也得认**环境变量**那一半(2026-11-10 加) ──
    #   Python 侧 `usb_mode()` = 环境变量 **或** 标记文件 ⇒ 取或;
    #   shell 侧以前只看标记文件 ⇒ 两边在「只有环境变量」的配置下对不上。
    (
        "scripts/check_deps.sh",
        '[ -n "${AGING_TOOL_DIR:-}" ] && USB_MODE=1',
        ':',
        "check_deps.sh 只认标记文件、不认环境变量(⇒ 判据跟 Python 侧不是同一个)",
    ),
    (
        "scripts/check_eth.sh",
        '[ -n "${AGING_TOOL_DIR:-}" ] && USB_MODE=1',
        ':',
        "check_eth.sh 只认标记文件、不认环境变量(⇒ 判据跟 Python 侧不是同一个)",
    ),
    # ── ④c U 盘镜像**必须不带** install_deps.sh(2026-11-10 加) ─────────
    (
        "scripts/build_usb_image.sh",
        "    install_deps.sh probe_rp_gpio_write.py",
        "    probe_rp_gpio_write.py",
        "build_usb_image.sh 把 install_deps.sh **带上盘**了(⇒ 工人照着那条"
        "补救办法跑 ⇒ 产线取不到公网 ⇒ 白跑; 而且跟「U 盘版那句」自相矛盾)",
    ),
    # ── ⑤ 两处拷贝**措辞漂了**(2026-11-10 加) ──────────────────────────
    #   同一句话曾经有四份拷贝/三种措辞 ⇒ 改一处忘一处, 页面和报错说的
    #   就不一样了 ⇒ 现在有一条断言比**逐字一致**。
    (
        "scripts/check_deps.sh",
        '            echo "          缺工具或工具起不来, 说明这版出厂镜像没带齐, 请改用设备版(deb)安装"',
        '            echo "          缺工具说明这版出厂镜像没带齐, 请改用设备版(deb)安装"',
        "check_deps.sh 那句话**跟 Python 的不是同一句了**(两份拷贝 ⇒ 各改各的)",
    ),
    (
        "server/main.py",
        '        "fix_hint": deps_fix_hint() if usb_mode else "",',
        '        "fix_hint": ("U 盘版: 盘上自带 fio / glmark2-es2, 其余工具用板子出厂那份 ⇒ "\n'
        '                     "缺工具或工具起不来, 说明这版出厂镜像没带齐, 请改用设备版(deb)安装。"\n'
        '                     if usb_mode else ""),',
        "server/main.py **又自己抄了一份**那句话(Python 侧出现第二份拷贝)",
    ),
    # ── ⑤ /api/config 那个 fix_command(早就按形态分, 但要钉住) ────────
    (
        "server/main.py",
        '        "fix_command": "" if usb_mode else "sudo bash scripts/install_deps.sh",',
        '        "fix_command": "sudo bash scripts/install_deps.sh",',
        "/api/config 的 fix_command **不再按形态分**(U 盘版页面上也给那条死命令)",
    ),
]


def build_copy(dst, rel, new_text):
    """把 server/ + static/ + scripts/(两个诊断脚本) + config.yaml + 那个测试
    文件复制到 dst。

    每个文件**只写一次** —— 被变异的那个根本不复制进来, 直接写变异后的内容。
    全程不覆写任何已存在的文件, 所以跑完不需要还原。

    ⚠️ **2026-11-10 补上 `static/`** —— 以前副本里**没有** `static/`
    ⇒ 那个测试的 §5 白名单扫描在副本里**只扫了 server/ + scripts/**,
    `static/` 子树根本没被扫到(今天没有变异打 static/, 所以不漏证据;
    但要加「前端里写了那条命令」这类变异, 副本得先补齐)。
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

    src = os.path.join(REPO, "static")
    for root, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        relroot = os.path.relpath(root, src)
        outroot = (os.path.join(dst, "static") if relroot == "."
                   else os.path.join(dst, "static", relroot))
        os.makedirs(outroot, exist_ok=True)
        for fn in files:
            relfile = os.path.normpath(os.path.join(relroot, fn))
            if relfile == rel:
                continue
            shutil.copy2(os.path.join(root, fn), os.path.join(outroot, fn))

    os.makedirs(os.path.join(dst, "scripts"), exist_ok=True)
    for fn in ("check_deps.sh", "check_eth.sh", "build_usb_image.sh"):
        relfile = os.path.join("scripts", fn)
        if relfile == rel:
            continue
        shutil.copy2(os.path.join(REPO, relfile), os.path.join(dst, relfile))

    shutil.copy2(os.path.join(REPO, "config.yaml"),
                 os.path.join(dst, "config.yaml"))
    os.makedirs(os.path.join(dst, "tests"), exist_ok=True)
    for extra in (TEST_REL, "tests/_tmp.py"):
        relfile = os.path.join(extra)
        if relfile == rel:
            continue
        shutil.copy2(os.path.join(REPO, relfile), os.path.join(dst, relfile))

    outp = os.path.join(dst, rel)
    os.makedirs(os.path.dirname(outp), exist_ok=True)
    with open(outp, "w", encoding="utf-8") as f:
        f.write(new_text)


def main():
    print("== 变异测试: 缺工具的补救办法(按形态分开说) ==\n")
    root = mkdtemp(prefix="aging-depshint-mut-")
    escaped = []
    broken = []
    for i, (rel, old, new, why) in enumerate(MUTATIONS, 1):
        srcp = os.path.join(REPO, rel)
        with open(srcp, encoding="utf-8") as f:
            orig = f.read()
        n = orig.count(old)
        if n != 1:
            print("  ⚠ 变异 %d: 锚点出现 %d 次(要求唯一)⇒ 证据不成立 —— %s"
                  % (i, n, why))
            broken.append(why)
            continue

        work = os.path.join(root, "m%02d" % i)
        os.makedirs(work, exist_ok=True)
        build_copy(work, rel, orig.replace(old, new))

        env = dict(os.environ)
        env["PYTHONPATH"] = work
        r = subprocess.run([PY, os.path.join(work, TEST_REL)],
                           cwd=work, env=env, capture_output=True, text=True,
                           timeout=300)
        out = (r.stdout or "") + (r.stderr or "")
        if r.returncode != 0:
            print("  ✓ 变异 %d 被抓住: %s" % (i, why))
        else:
            print("  ✗ 变异 %d **逃了**: %s" % (i, why))
            escaped.append(why)

    print("")
    if broken:
        print("⚠️ %d 条变异因为锚点不唯一没验成(证据不成立)" % len(broken))
        for b in broken:
            print("    - " + b)
    if escaped:
        print("✗ %d 条变异逃了(测试咬不动)" % len(escaped))
        for e in escaped:
            print("    - " + e)
        return 1
    print("✅ 全部 %d 条变异都被抓住了" % len(MUTATIONS))
    return 0


if __name__ == "__main__":
    sys.exit(main())
