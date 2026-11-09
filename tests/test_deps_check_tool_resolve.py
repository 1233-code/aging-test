"""回归测试: 配置页的「依赖检查」必须**跟测试项用同一套找工具的逻辑**。

起因 (2026-11-06 实测, SN A10010000000058):
    工人在**新设备**上打开配置页 ⇒ 顶上红条写着「缺失 fio」⇒ 可实际上
    fio **就在 U 盘里**(`tools/bin/fio`, 由 `scripts/build_usb_image.sh`
    生成的 wrapper)。测试项跑 eMMC 的时候也真的调到了它。

病根: `/api/deps/check` 自己写了一套 `_which()`, **只看系统 PATH** ⇒
    U 盘版里 fio / glmark2-es2 是**盘上**的, 系统 PATH 里压根没有 ⇒
    配置页一律说「缺」⇒ **假警报**。工人按提示去 `apt install`, 装到
    板子上, 拔盘就走 —— 白装。

⚠️ **同一天还挖出第二层**: 那天那轮 eMMC 项真的 error 了, 原因是
    `fio write 失败: rc=127 · .../fio.bin: error while loading shared
    libraries: libceph-common.so.2` ⇒ **盘上那份 fio 缺依赖库, 起不来**
    ⇒ 而库其实**就在盘上**, 只是待在 `usr/lib/aarch64-linux-gnu/ceph/`
    这个**子目录**里, 而 wrapper 给链接器的搜索路径只列了**上一层**
    ⇒ 「找到了」不等于「起得来」。

所以这个测试守三件事:
    ① 找工具用 `find_tool`(认 AGING_TOOL_DIR) —— 盘上的 fio 算**有**;
    ② 「起不来」跟「没有」是**两回事**, 要分开报(`critical_broken`);
    ③ 「起不来」时的话要说清「**不是板子坏了**」——
       否则一台好板子会因为**盘**的问题被判失败、白白返修。

另外还查前端: 红条不能再说「请先跑 install_deps.sh」(U 盘版里那条命令
是错的 —— 装到板子上, 拔盘就走, 白装)。

只碰临时目录里的假工具 —— 不写生产库、不写 reports/pdf/、不碰真 U 盘。
跑法: .venv/bin/python -m tests.test_deps_check_tool_resolve
"""
import asyncio
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import main as M                      # noqa: E402
from server.tests import _tools as T             # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

FAILS = []
PASSES = 0


def check(cond, msg, extra=""):
    global PASSES
    if cond:
        PASSES += 1
        print(f"  ✓ {msg}")
    else:
        FAILS.append(msg)
        print(f"  ✗ {msg}" + (f"  [{extra}]" if extra else ""))


def _write_tool(bin_dir: str, name: str, body: str) -> str:
    """造一个假的工具(wrapper 脚本)。返回路径。"""
    os.makedirs(bin_dir, exist_ok=True)
    p = os.path.join(bin_dir, name)
    with open(p, "w", encoding="utf-8") as f:
        f.write(body)
    os.chmod(p, 0o755)
    return p


#: 一个「起得来」的假 fio
OK_FIO = "#!/bin/sh\necho 'fio-3.16'\nexit 0\n"

#: 一个「起不来」的假 fio —— 跟真盘上那次一模一样(缺共享库 ⇒ rc=127)
BROKEN_FIO = (
    "#!/bin/sh\n"
    "echo '/media/User/AGING/tools/libexec/fio.bin: error while loading "
    "shared libraries: libceph-common.so.2: cannot open shared object "
    "file: No such file or directory' >&2\n"
    "exit 127\n"
)


def _run_with(env_extra, coro_fn):
    """在给定环境变量下跑一次 deps_check(自己管好 env 的进出)。"""
    old = {}
    for k, v in env_extra.items():
        old[k] = os.environ.get(k)
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    try:
        return asyncio.run(coro_fn())
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _item(res, name):
    for x in res["items"]:
        if x["name"] == name:
            return x
    return None


def main():
    tmp = os.path.join(os.environ.get("TMPDIR", "/tmp"), "aging-depscheck-test")
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp, exist_ok=True)
    tool_dir = os.path.join(tmp, "tools")
    bin_dir = os.path.join(tool_dir, "bin")

    # ─────────────────────────────────────────────────────────────
    print("\n【1】U 盘版: 盘上的 fio 算「有」, 不许再说缺")
    # ─────────────────────────────────────────────────────────────
    fio_path = _write_tool(bin_dir, "fio", OK_FIO)
    res = _run_with({T.TOOL_DIR_ENV: tool_dir}, M.deps_check)
    fio = _item(res, "fio")
    check(fio is not None, "fio 这一项在结果里")
    check(fio["ok"] is True, "盘上的 fio 判为「有」(不是缺失)",
          f"ok={fio['ok']} path={fio['path']!r}")
    check(fio["path"] == fio_path, "报的路径是盘上那份",
          f"got={fio['path']!r} want={fio_path!r}")
    check(fio["source"] == "stick", "标出来源是「盘上」(stick)",
          f"source={fio['source']!r}")
    check("fio" not in res["critical_missing"], "fio 不在 critical_missing 里",
          f"missing={res['critical_missing']}")
    check(res["mode"] == "usb", "mode=usb")

    # ─────────────────────────────────────────────────────────────
    print("\n【2】U 盘版: 工具「起不来」跟「没有」要分开报")
    # ─────────────────────────────────────────────────────────────
    _write_tool(bin_dir, "fio", BROKEN_FIO)
    res = _run_with({T.TOOL_DIR_ENV: tool_dir}, M.deps_check)
    fio = _item(res, "fio")
    check(fio["ok"] is False, "起不来的 fio 判为「不可用」")
    check(fio.get("broken") is True, "标了 broken=True")
    check("libceph-common.so.2" in (fio.get("broken_reason") or ""),
          "把缺哪个库写出来了(工人/排障一眼能看懂)",
          f"reason={fio.get('broken_reason')!r}")
    check("fio" in res["critical_broken"], "进了 critical_broken")
    check("fio" not in res["critical_missing"],
          "**不在** critical_missing 里(它不是「没有」, 是「起不来」)")
    check(res["all_ok"] is False, "all_ok=False")

    # ─────────────────────────────────────────────────────────────
    print("\n【3】U 盘版: 不许让工人去板子上装东西(装完拔盘就走, 白装)")
    # ─────────────────────────────────────────────────────────────
    check(res["fix_command"] == "", "fix_command 是空的(U 盘版别给 apt 命令)",
          f"fix_command={res['fix_command']!r}")
    check("设备版" in (res["fix_hint"] or ""),
          "fix_hint 指向「改用设备版」", f"hint={res['fix_hint']!r}")

    # ─────────────────────────────────────────────────────────────
    print("\n【4】设备版: 缺工具还是得给出安装命令")
    # ─────────────────────────────────────────────────────────────
    empty_path = os.path.join(tmp, "emptybin")
    os.makedirs(empty_path, exist_ok=True)
    res = _run_with({T.TOOL_DIR_ENV: None, "PATH": empty_path}, M.deps_check)
    check(res["mode"] == "device", "mode=device")
    check(res["fix_command"] == "sudo bash scripts/install_deps.sh",
          "fix_command = install_deps.sh", f"got={res['fix_command']!r}")
    check("fio" in res["critical_missing"], "系统里没有 fio ⇒ 进 critical_missing")
    check(res["critical_broken"] == [], "系统工具没探针 ⇒ 不该有 broken",
          f"broken={res['critical_broken']}")
    check(res["all_ok"] is False, "all_ok=False")

    # ─────────────────────────────────────────────────────────────
    print("\n【5】「起不来」的话要说清: 不是板子坏了")
    # ─────────────────────────────────────────────────────────────
    msg = T.loader_error_message(
        127,
        "/media/User/AGING/tools/libexec/fio.bin: error while loading "
        "shared libraries: libceph-common.so.2: cannot open shared object "
        "file: No such file or directory",
    )
    check(bool(msg), "认得出 rc=127 的链接器报错")
    check("libceph-common.so.2" in msg, "写出了缺哪个库", f"msg={msg!r}")
    check("不是板子坏了" in msg, "明说「不是板子坏了」", f"msg={msg!r}")
    check(T.loader_error_message(1, "fio: cufile error") == "",
          "普通 fio 失败**不算**「起不来」(别把写失败说成工具坏了)")
    check(T.loader_error_message(0, "") == "", "rc=0 不算「起不来」")
    check(T.loader_error_message(
        127, "fio.bin: error while loading shared libraries: libz.so.1: x")
        != "", "换个库名也认得出")

    # ─────────────────────────────────────────────────────────────
    print("\n【6】调用点: eMMC / USB 两项都要用这个口径")
    # ─────────────────────────────────────────────────────────────
    for rel in ("server/tests/emmc.py", "server/tests/usb.py"):
        src = open(os.path.join(REPO, rel), encoding="utf-8").read()
        check("loader_error_message(" in src,
              f"{rel} 调用了 loader_error_message(工具起不来要单独说)")

    # ─────────────────────────────────────────────────────────────
    print("\n【7】前端: 红条不能硬写 install_deps.sh")
    # ─────────────────────────────────────────────────────────────
    html = open(os.path.join(REPO, "static", "index.html"), encoding="utf-8").read()
    banner = html[html.find("dep-banner"):html.find("dep-banner") + 3000]
    check("critical_broken" in banner, "红条认「起不来」这一类")
    # ⚠️ **标题**必须也分「起不来」和「缺失」 —— 只在明细里分没用:
    #    工人第一眼看的还是标题(「缺失 3 个关键工具」会让人去装, 装没用)。
    title_block = banner[banner.find("dep-banner-title"):]
    title_block = title_block[:title_block.find("</div>")]
    check('v-if="depsInfo.critical_broken' in title_block,
          "红条**标题**按「起不来」分(不是一律说「缺」)",
          f"title={title_block[:160]!r}")
    check("depsInfo.fix_command" in banner, "红条用后端给的 fix_command")
    check("sudo bash scripts/install_deps.sh" not in banner,
          "红条里不再硬写安装命令(U 盘版那条是错的)")
    check("fix_hint" in banner, "红条认 fix_hint")

    shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{'=' * 60}")
    if FAILS:
        print(f"✗ {len(FAILS)} 项失败 / 共 {PASSES + len(FAILS)} 项")
        for f in FAILS:
            print(f"   - {f}")
        return 1
    print(f"✓ 全部 {PASSES} 项通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
