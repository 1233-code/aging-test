"""「U 盘插到别的设备上双击没反应」的回归测试。

**病根**(2026-11-05 实测): 镜像是在**临时构建目录**里打的
(/tmp/aging-usb-image), venv 里那些 console script(uvicorn/pip/fastapi…)
的第一行因此**写死了构建路径**:

    #!/tmp/aging-usb-image/venv/bin/python3

⇒ 盘插到**另一台设备**上, 那个路径根本不存在 ⇒ 内核 "bad interpreter"
⇒ 服务起不来 ⇒ 而**桌面双击没有终端** ⇒ 报错没人看
⇒ 工人只看到「点了没反应」。
(本机能起来纯粹是因为构建目录还没被删掉 —— 是**碰巧**, 不是设计。)

**两条防线**:
  ① 应用侧: 一律用 `<venv>/bin/python3 -m <模块>`, **不调 console script**
     —— venv 是靠**同目录**的 pyvenv.cfg 定位的, Python 3.8 用 argv[0] 的
     目录找它、**不解析符号链接** ⇒ 盘挂在哪儿都能用。
  ② 失败要**看得见**: 启动失败 ⇒ 落一份 `logs/启动失败.txt` + 弹桌面通知。
     (启动器不能 execv 掉自己 —— exec 之后 start.sh 失败就没人报了。)

跑法: .venv/bin/python tests/test_usb_relocatable.py
"""
import os
import shutil
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
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


def read(rel):
    with open(os.path.join(REPO, rel), encoding="utf-8") as f:
        return f.read()


# ═══════════════════════ ① 静态: 应用不许调 console script ═══════════════════════
def static_checks():
    print("\n① 静态锚点")
    start = read("start.sh")

    check('"$VENV_DIR"/bin/uvicorn' not in start,
          "start.sh 不许直接调 venv 的 uvicorn console script",
          "找到了 $VENV_DIR/bin/uvicorn")
    check('"bin/python3" -m uvicorn' in start or "-m uvicorn" in start,
          "start.sh 用 `python3 -m uvicorn` 起服务")
    check("-m pip install" in start,
          "start.sh 用 `python3 -m pip` 装包(不调 pip console script)")
    check('"$VENV_DIR"/bin/pip ' not in start,
          "start.sh 不许直接调 pip console script", "找到了 $VENV_DIR/bin/pip")

    # 失败可见
    check("fail_visible" in start, "start.sh 有失败可见的路径(fail_visible)")
    check("启动失败.txt" in start, "失败会落到 logs/启动失败.txt")
    check("notify-send" in start, "失败会弹桌面通知(notify-send/zenity)")

    # 启动器: 父进程不能 execv 掉自己 —— 得先 fork, 才能看见 start.sh 的退出码
    launcher = read("scripts/usb_launcher.c")
    i_fork = launcher.find("fork()")
    i_exec = launcher.find('execv("/bin/bash"')
    check(i_fork != -1 and i_exec != -1 and i_fork < i_exec,
          "启动器先 fork 再 execv(父进程留着等退出码)",
          f"fork@{i_fork} execv@{i_exec}")
    check("waitpid" in launcher, "启动器等 start.sh 退出(waitpid)")
    check("WEXITSTATUS" in launcher, "启动器看 start.sh 的退出码")

    # 构建脚本: 可搬迁自检
    build = read("scripts/build_usb_image.sh")
    check("reloc-check" in build, "构建脚本有「挪个地方再跑」的可搬迁自检")
    check("bin/uvicorn" in build, "构建脚本检查 start.sh 不许调 console script")


# ═══════════════════════ ② 行为: venv 挪走还能用 ═══════════════════════
def venv_relocates():
    print("\n② 行为: venv 挪到别的目录还能用")
    tmp = tempfile.mkdtemp(prefix="reloc-")
    try:
        v1 = os.path.join(tmp, "place1", "venv")
        os.makedirs(os.path.dirname(v1), exist_ok=True)
        r = subprocess.run([sys.executable, "-m", "venv", "--without-pip", v1],
                           capture_output=True, text=True)
        if r.returncode != 0:
            check(False, "建个临时 venv", r.stderr[-200:])
            return

        probe = ("import sys; print(sys.prefix)")
        p1 = subprocess.run([os.path.join(v1, "bin", "python3"), "-c", probe],
                            capture_output=True, text=True)
        check(p1.stdout.strip() == v1, "venv 在原位时 prefix 指对", p1.stdout.strip())

        # 挪走 —— 这正是「盘插到别的设备/别的挂载点」
        v2 = os.path.join(tmp, "place2", "venv")
        os.makedirs(os.path.dirname(v2), exist_ok=True)
        shutil.move(v1, v2)
        p2 = subprocess.run([os.path.join(v2, "bin", "python3"), "-c", probe],
                            capture_output=True, text=True)
        check(p2.stdout.strip() == v2,
              "venv 挪到别的目录后 prefix 跟着走(不解析符号链接)",
              p2.stdout.strip())

        # 旧写法(console script)在挪走后就是坏的 —— 这就是「换设备失败」的机制
        sh = os.path.join(v2, "bin", "pip")
        if os.path.exists(sh):
            head = open(sh, encoding="utf-8").readline()
            interp = head[2:].split()[0] if head.startswith("#!") else ""
            check(not interp or not os.path.exists(interp),
                  "旁证: console script 的 shebang 指向构建路径(挪走后必然坏)",
                  interp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ═══════════════════════ ③ 行为: 启动失败要看得见 ═══════════════════════
def launcher_reports_failure():
    print("\n③ 行为: 启动失败要留下痕迹")
    if shutil.which("gcc") is None:
        check(False, "没有 gcc, 编不出启动器, 跳过")
        return
    tmp = tempfile.mkdtemp(prefix="launcher-")
    try:
        exe = os.path.join(tmp, "启动老化测试")
        r = subprocess.run(["gcc", "-O2", "-static", "-o", exe,
                            os.path.join(REPO, "scripts", "usb_launcher.c")],
                           capture_output=True, text=True)
        if r.returncode != 0:
            check(False, "编译启动器", r.stderr[-300:])
            return
        check(True, "启动器编译通过")

        # ① start.sh 失败(退出码非 0) ⇒ 启动器要非零退出 + 落盘
        with open(os.path.join(tmp, "start.sh"), "w", encoding="utf-8") as f:
            f.write("#!/bin/bash\necho '模拟失败' >&2\nexit 3\n")
        os.chmod(os.path.join(tmp, "start.sh"), 0o755)

        r = subprocess.run([exe], capture_output=True, text=True, cwd=tmp,
                           env={**os.environ, "PATH": "/usr/bin:/bin"})
        check(r.returncode != 0, "start.sh 失败时启动器非零退出", r.returncode)
        note = os.path.join(tmp, "logs", "启动失败.txt")
        check(os.path.exists(note),
              "start.sh 失败时落了 logs/启动失败.txt", note)
        if os.path.exists(note):
            body = open(note, encoding="utf-8").read()
            check("3" in body, "启动失败.txt 里写明了退出码", body[:120])

        # ② 成功时不该报错
        with open(os.path.join(tmp, "start.sh"), "w", encoding="utf-8") as f:
            f.write("#!/bin/bash\nexit 0\n")
        r = subprocess.run([exe], capture_output=True, text=True, cwd=tmp,
                           env={**os.environ, "PATH": "/usr/bin:/bin"})
        check(r.returncode == 0, "start.sh 成功时启动器零退出", r.returncode)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    static_checks()
    venv_relocates()
    launcher_reports_failure()

    print("\n" + "─" * 60)
    if FAILS:
        print(f"✗ {len(FAILS)} 项失败 / 共 {PASSES + len(FAILS)} 项")
        for f in FAILS:
            print("   · " + f)
        return 1
    print(f"✓ 全部通过 ({PASSES} 项)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
