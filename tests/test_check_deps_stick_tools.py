"""回归测试: `scripts/check_deps.sh` 必须**认盘上的工具**。

起因 (2026-11-11 排查出来的):
    U 盘版里 **fio / glmark2-es2 / glmark2-es2-wayland 是「盘上」的** ——
    `scripts/build_usb_image.sh` 把它们做成 `<盘>/tools/bin/<名字>` 的
    wrapper(连带那 10 个共享库一起搬)。**系统 PATH 里压根没有它们**。

    而 `check_deps.sh` 找工具**只看系统 PATH**(`command -v`)⇒
      · 「① 包类·关键工具」那一栏里 **fio 报「✗ 缺」**
        ⇒ 汇总成「**拒绝出货**」;
      · GPU 那一栏也报「没装 glmark2-es2」。
    ⇒ 工具**好好地**在盘上、测试项也真调得到它(Python 侧 `find_tool`
      认 AGING_TOOL_DIR), 唯独这个脚本说「缺」⇒ **同一台机器、同一件
      工具、两个说法** ⇒ 工人拿到的结论是错的。

    ⇒ 修法: 脚本里加一个 `find_tool()`, 口径跟 Python 侧
      `server/tests/_tools.py:find_tool()` **一致**:
        ① 随包工具目录(AGING_TOOL_DIR / 盘根的 tools/bin)优先
        ② PATH ③ /usr/local/sbin /usr/sbin /sbin /usr/local/bin
      并且把**找到的路径打出来** ⇒ 「在盘上」这件事看得见。

这个测试**只在临时目录里造假工具**(就是几行 shell 脚本)—— 不碰真 U 盘、
不跑盘上/系统里的任何真二进制(fio 那些真身是 .deb 里解出来的, 不许在这儿
执行), 不写生产库、不碰 reports/pdf/。
跑法: .venv/bin/python -m tests.test_check_deps_stick_tools
"""
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests._tmp import mkdtemp                   # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: 被测的脚本 —— 默认是仓库里那份; 变异测试把**改坏的副本**的路径塞进
#: 这个环境变量里(不然变异改的是副本, 测的还是仓库里那份 ⇒ 白测)。
SCRIPT = (os.environ.get("AGING_CHECK_DEPS_SCRIPT")
          or os.path.join(REPO, "scripts", "check_deps.sh"))

FAILS = []
PASSES = 0


def check(cond, msg, extra=""):
    global PASSES
    if cond:
        PASSES += 1
        print("    ✓ " + msg)
    else:
        FAILS.append(msg)
        print("    ✗ " + msg + ("  → " + str(extra) if extra else ""))


def section(title):
    print("\n" + title)


def _fake_tool(bin_dir, name):
    """造一个**假的**工具(几行 shell)—— 只是「存在且可执行」。"""
    os.makedirs(bin_dir, exist_ok=True)
    p = os.path.join(bin_dir, name)
    with open(p, "w", encoding="utf-8") as f:
        f.write("#!/bin/sh\necho 'fake %s'\nexit 0\n" % name)
    os.chmod(p, 0o755)
    return p


def _fake_stick(root):
    """照着 U 盘的样子搭一个: <root>/scripts/check_deps.sh + <root>/tools/。

    `config.yaml` 也拷一份 —— 脚本要读它(读不到就回落到默认值, 但那样
    断言就不受控了)。
    """
    os.makedirs(os.path.join(root, "scripts"), exist_ok=True)
    shutil.copy2(SCRIPT, os.path.join(root, "scripts", "check_deps.sh"))
    shutil.copy2(os.path.join(REPO, "config.yaml"),
                 os.path.join(root, "config.yaml"))
    with open(os.path.join(root, "AGING_USB_MODE"), "w", encoding="utf-8") as f:
        f.write("1\n")
    return os.path.join(root, "tools", "bin")


def _run(root, env_extra=None):
    env = dict(os.environ)
    env.pop("AGING_TOOL_DIR", None)
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run(["bash", os.path.join(root, "scripts",
                                                "check_deps.sh")],
                          capture_output=True, text=True, timeout=180,
                          env=env, cwd=root)
    return proc.stdout + proc.stderr


def _line(out, needle):
    """找出包含 needle 的那一行(和它的下一行 —— 路径打在下一行)。"""
    lines = out.splitlines()
    for i, ln in enumerate(lines):
        if needle in ln:
            return ln, (lines[i + 1] if i + 1 < len(lines) else "")
    return "", ""


def main():
    root = mkdtemp(prefix="aging-checkdeps-")
    try:
        # ─────────────────────────────────────────────────────────
        section("【1】U 盘版: 盘上的 fio 算「有」, 不许再说缺")
        # ─────────────────────────────────────────────────────────
        bin_dir = _fake_stick(root)
        fio = _fake_tool(bin_dir, "fio")
        out = _run(root)          # 没设 AGING_TOOL_DIR ⇒ 靠「盘根的 tools/bin」
        ln, path = _line(out, "fio")
        check("✓" in ln and "✗" not in ln,
              "盘上的 fio 判为「有」(不是缺失)", (ln, out[:200]))
        check(fio in out,
              "报出来的路径是**盘上那份**", (path, fio))

        # ─────────────────────────────────────────────────────────
        section("【2】环境变量 AGING_TOOL_DIR 也认(start.sh 导出的那个)")
        # ─────────────────────────────────────────────────────────
        root2 = mkdtemp(prefix="aging-checkdeps2-")
        bin2 = _fake_stick(root2)
        fio2 = _fake_tool(bin2, "fio")
        out2 = _run(root2, {"AGING_TOOL_DIR": os.path.join(root2, "tools")})
        ln2, _ = _line(out2, "fio")
        check("✓" in ln2 and "✗" not in ln2,
              "AGING_TOOL_DIR 指过去 ⇒ 盘上的 fio 算「有」", ln2)
        check(fio2 in out2, "路径是那份", fio2)

        # ─────────────────────────────────────────────────────────
        section("【3】盘上**真的没有**这个工具 ⇒ 照旧报缺(别把护栏拆了)")
        # ─────────────────────────────────────────────────────────
        root3 = mkdtemp(prefix="aging-checkdeps3-")
        bin3 = _fake_stick(root3)
        _fake_tool(bin3, "lsblk")         # 别的工具有, 就是没有 fio
        out3 = _run(root3, {"AGING_TOOL_DIR": os.path.join(root3, "tools")})
        ln3, path3 = _line(out3, "fio")
        # ⚠️ 这台开发机上 fio 装在系统里 ⇒ 「盘上没有」不等于「没有」。
        #   ⇒ 按**系统里到底有没有**分别断言(两种都得成立):
        #     · 系统里有 ⇒ 回落到系统那份(还是「有」);
        #     · 系统里没有 ⇒ 照旧报「缺」(护栏没拆)。
        sys_fio = shutil.which("fio")
        if sys_fio:
            check("✓" in ln3 and sys_fio in out3,
                  "盘上没有 ⇒ 回落到系统那份(还是「有」)", (ln3, sys_fio))
        else:
            check("✗" in ln3,
                  "盘上没有、系统里也没有 ⇒ 照旧报「缺」", ln3)

        # ─────────────────────────────────────────────────────────
        section("【4】找工具的口径跟 Python 侧**是同一个**")
        # ─────────────────────────────────────────────────────────
        from server.tests import _tools as T
        py = open(os.path.join(REPO, "server", "tests", "_tools.py"),
                  encoding="utf-8").read()
        check("find_tool" in py, "Python 侧有 find_tool()")
        sh = open(SCRIPT, encoding="utf-8").read()
        check("find_tool()" in sh, "shell 侧也有 find_tool()")
        check("AGING_TOOL_DIR" in sh, "shell 侧认 AGING_TOOL_DIR")
        check("tools/bin" in sh, "shell 侧认「盘根的 tools/bin」")
        check(T.TOOL_DIR_ENV == "AGING_TOOL_DIR",
              "两边认的是**同一个**环境变量", T.TOOL_DIR_ENV)
    finally:
        shutil.rmtree(root, ignore_errors=True)

    print("\n" + ("全部通过" if not FAILS else f"有失败项 ({len(FAILS)})"))
    for f in FAILS:
        print("  ✗ " + f)
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
