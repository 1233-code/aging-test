"""变异测试: 「U 盘版应用」那套新逻辑, 测试到底咬不咬得住。

每个变异 = 把一处代码改坏, 看**指定的**那个测试会不会变红。
红了而且是**被断言咬住的**(输出里有 ✗)才算数 —— 只是崩了不算。

覆盖的新逻辑:
  · `server/usb_export.py`  报告跟着「应用自己那根盘」走
  · `server/tests/_tools.py` 随包工具目录优先 / sudo「不许执行」的特征
  · `start.sh`               U 盘模式自举(数据根 / 工具目录 / 配置文件)
  · `server/runner.py`      「不许执行」→ 改判 error(不误报板子坏了)

⚠️ **副本目录必须复制测试依赖的模块** —— 少复制一个, 测试在副本里就
   import 不到 ⇒ 崩了而不是「被断言咬住」⇒ 每条变异都会被误判成
   「没抓住」(这个坑踩过)。这里要复制:
     tests/test_usb_app_mode.py  tests/test_sudo_nopasswd.py
     tests/_tmp.py  tests/_fake_sysfs.py
     start.sh  static/  config.yaml
   —— `scripts/` **不要**复制: 那个目录里有镜像构建脚本, 而本脚本只
   需要跑**运行期**的测试(锚点的静态检查在 tests/test_usb_image_anchors.py
   里, 单独管)。少复制一个文件, 副本里的测试就可能假红。

跑法: .venv/bin/python tests/_mutate_usb_app_mode.py
"""
import os
import shutil
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(REPO, ".venv", "bin", "python")

# 每个变异: (文件, from, to, 说明, 该由哪个测试咬住)
MUTATIONS = [
    (
        "server/usb_export.py",
        "        for p in _preferred_paths():",
        "        for p in ():",
        "报告不再跟着「应用自己那根盘」走(会落到枚举到的第一根盘上)",
        "tests.test_usb_app_mode",
    ),
    (
        "server/usb_export.py",
        "    m = storage.mount_point_for_path(path, mounts)",
        "    m = None",
        "认不出「某个路径落在哪根盘上」(应用在 U 盘上时找不到自己那根)",
        "tests.test_usb_app_mode",
    ),
    (
        "server/tests/_tools.py",
        "    if d:\n        cand = os.path.join(d, \"bin\", name)",
        "    if False:\n        cand = os.path.join(d, \"bin\", name)",
        "随包工具目录不再优先(去系统 PATH 里找 fio ⇒ 找不到盘上那份)",
        "tests.test_usb_app_mode",
    ),
    (
        "start.sh",
        '    export AGING_DATA_DIR="$SCRIPT_DIR"',
        "    :",
        "U 盘模式不把数据根指到 U 盘(历史记录/报告仍写在 /var/lib 里)",
        "tests.test_usb_app_mode",
    ),
    (
        "start.sh",
        '    [ -d "$SCRIPT_DIR/tools/bin" ] && export AGING_TOOL_DIR="$SCRIPT_DIR/tools"',
        "    :",
        "U 盘模式不指工具目录(盘上的 fio/glmark2 找不到)",
        "tests.test_usb_app_mode",
    ),
    (
        "start.sh",
        '    [ -f "$SCRIPT_DIR/config.yaml" ] && export AGING_CONFIG="$SCRIPT_DIR/config.yaml"',
        "    :",
        "U 盘模式不用盘上那份 config.yaml(还在用设备上那份)",
        "tests.test_usb_app_mode",
    ),
    (
        "server/tests/_tools.py",
        "    return any(s in low for s in _SUDO_NOT_ALLOWED_ERRORS)",
        "    return False",
        "认不出「sudo 不许执行这条命令」(白名单漏了盘上工具)",
        "tests.test_sudo_nopasswd",
    ),
    (
        "server/runner.py",
        '        if result.status == "failed" and is_sudo_not_allowed_error(\n'
        '            (result.error or "") + "\\n" + (result.log or "")\n'
        '        ):\n'
        '            result.status = "error"\n'
        '            result.error = sudo_not_allowed_hint(result.error or result.log or "")',
        '        pass  # 变异: 不把「不许执行这条命令」改判 error',
        "runner 不把「不许执行」改判 error(会误报成「板子坏了」)",
        "tests.test_sudo_nopasswd",
    ),
]

#: 副本里必须带的文件/目录。
COPY_TESTS = ("test_usb_app_mode.py", "test_sudo_nopasswd.py",
              "_tmp.py", "_fake_sysfs.py")
COPY_TOP = ("start.sh", "config.yaml")
COPY_DIRS = ("static",)


def build_copy(dst, rel, new_text):
    """把 server/ + 顶层文件 + 测试文件复制到 dst, 并把 rel 写成 new_text。

    每个文件**只写一次** —— 被变异的那个根本不复制进来, 直接写变异后的
    内容。全程不覆写任何已存在的文件, 所以跑完不需要还原。

    ⚠️ **被变异的那个文件必须真的被跳过** —— 复制阶段先把它放进去,
    后面再「打开清空重写」就是在覆盖一个**已经存在**的文件(旧脚本
    就是这么干的)。这里改成: 复制时跳过它, 后面用 "x" 另建一份新的。
    """
    # 被变异的那个: server/ 里的用相对 server/ 的路径比, 顶层的用文件名比
    if rel.startswith("server/" + os.sep) or rel.startswith("server/"):
        skip_in_server = os.path.relpath(rel, "server")
        skip_top = None
    else:
        skip_in_server = None
        skip_top = rel

    src = os.path.join(REPO, "server")
    for root, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        relroot = os.path.relpath(root, src)
        outroot = (os.path.join(dst, "server") if relroot == "."
                   else os.path.join(dst, "server", relroot))
        os.makedirs(outroot, exist_ok=True)
        for fn in files:
            relfile = os.path.normpath(os.path.join(relroot, fn))
            if skip_in_server is not None and relfile == skip_in_server:
                continue          # 这个下面单独写
            shutil.copy2(os.path.join(root, fn), os.path.join(outroot, fn))

    for fn in COPY_TOP:
        if skip_top is not None and fn == skip_top:
            continue              # 这个下面单独写
        s = os.path.join(REPO, fn)
        if os.path.isfile(s):
            shutil.copy2(s, os.path.join(dst, fn))
    for d in COPY_DIRS:
        s = os.path.join(REPO, d)
        if os.path.isdir(s):
            shutil.copytree(s, os.path.join(dst, d),
                            ignore=shutil.ignore_patterns("__pycache__"))
    os.makedirs(os.path.join(dst, "tests"), exist_ok=True)
    for fn in COPY_TESTS:
        shutil.copy2(os.path.join(REPO, "tests", fn),
                     os.path.join(dst, "tests", fn))

    outp = os.path.join(dst, rel)
    os.makedirs(os.path.dirname(outp), exist_ok=True)
    # ⚠️ **用 "x"(只在文件不存在时才建), 不用 "w"** ——
    #    "w" 是「打开就清空」: 万一这个路径上已经有东西(复制阶段漏跳了
    #    某个文件), 那一刀就把**别人的东西**抹了。"x" 撞上已存在的文件
    #    会直接报错 ⇒ 宁可崩, 也不覆盖。
    with open(outp, "x", encoding="utf-8") as f:
        f.write(new_text)


def run_one(work, module):
    env = dict(os.environ)
    env["PYTHONPATH"] = work
    proc = subprocess.run(
        [PY, "-m", module],
        cwd=work, env=env, capture_output=True, text=True, timeout=600,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def main():
    root = tempfile.mkdtemp(prefix="mut_usbapp_")
    print(f"副本都在 {root} 下面\n")

    bad = 0
    for i, (rel, frm, to, desc, module) in enumerate(MUTATIONS):
        srcp = os.path.join(REPO, rel)
        with open(srcp, encoding="utf-8") as f:
            orig = f.read()
        if orig.count(frm) != 1:
            print(f"\n✗ {desc}\n   → 锚点在源文件里出现 {orig.count(frm)} 次"
                  f"(必须**恰好一次**, 否则证据不成立): {frm[:60]!r}")
            bad += 1
            continue

        work = os.path.join(root, f"m{i:02d}")
        os.makedirs(work, exist_ok=True)
        build_copy(work, rel, orig.replace(frm, to))

        code, out = run_one(work, module)
        fails = out.count("✗")
        if code != 0 and fails == 0:
            # ⚠️ **红了但一个 ✗ 都没有** —— 那多半不是断言把它咬住的, 而是
            #   测试自己崩了(语法错/导入错/异常)。这不算「咬得动」。
            print(f"\n✗ {desc}\n   → 红了, 但一个 ✗ 都没有 (exit={code}) —— "
                  f"这是**崩了**, 不是被断言咬住的, 不算数:\n{out[-800:]}")
            bad += 1
        elif code != 0:
            print(f"\n✓ {desc}\n   → 测试变红 (exit={code}, {fails} 项失败, "
                  f"由 {module} 咬住) —— 咬得动")
        else:
            print(f"\n✗ {desc}\n   → 测试还是绿的! 这个变异没被守住")
            bad += 1

    print("\n" + ("全部变异都被抓住了 ✓" if bad == 0
                  else f"有 {bad} 个变异没被抓住 ✗"))
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
