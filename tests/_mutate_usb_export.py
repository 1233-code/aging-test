# 变异测试: 故意把「报告自动存 U 盘」和「USB 写测试别自伤」改坏, 看
# tests/test_usb_export.py 会不会红。
#
# 为什么要有这么个东西: 「测试全绿」本身说明不了什么 —— 断言写错了、
# 写松了、或者压根没跑到, 都是绿的。唯一能证明这测试**咬得动**的办法,
# 就是亲手把代码改坏, 看它是不是真的红。
#
# 这一份守的是 §8.2.3 的每一条硬规矩:
#   ① 「怎么算 U 盘」的三条判据(系统路径 / USB 总线 / 可写);
#   ② 目录名 `AGING-REPORTS/`、文件名跟板子上那份一样;
#   ③ `INDEX.csv` 带 BOM、表头只有一行、按「SN+开始时间」替换不追加;
#   ④ 拷的是**同一份** PDF(不重新生成);
#   ⑤ 没有 U 盘 / 拷不进去 ⇒ **不阻断**;
#   ⑥ USB 写测试别拿「正在用着的盘」当目标;
#   ⑦ runner / main 的接线别被拆掉。
#
# 做法: **每个变异单开一个全新目录**, 把 server/ + config.yaml + 那个测试
# 文件复制进去, 在**副本**上做一处改动, 在副本目录里跑测试。
# 被改的那个文件是从头到尾**只写一次**(直接写变异后的内容), 别的文件原样
# 复制 —— 所以整个过程不覆写任何已存在的文件, 跑完也不用还原。
#
# ⚠️ 只在**副本**上改 —— 仓库里 server/ 是产线在跑的那份, 改坏了没人知道。
#
# 跑法: .venv/bin/python tests/_mutate_usb_export.py
import os
import shutil
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(REPO, ".venv", "bin", "python")
TEST = "tests.test_usb_export"

# 每个变异: (文件, from, to, 说明)。from 必须在源文件里**找得到**, 否则算失败。
MUTATIONS = [
    (
        "server/usb_export.py",
        '    return False\n\n\ndef _stick_from_mount',
        '    return True\n\n\ndef _stick_from_mount',
        "U 盘判定: 「系统路径」这条判据被废掉(板子自己也算 U 盘 ⇒ 报告写回板子)",
    ),
    (
        "server/storage.py",
        '        if _USB_BUS_NAME.match(os.path.basename(p)):',
        '        if "usb" in p.lower():',
        "U 盘判定: 退回「路径里有 usb 子串」(分不清 USB **控制器**和 USB 设备)",
    ),
    (
        "server/storage.py",
        '            if (n.startswith(d) and n[len(d):].isdigit()) or \\\n'
        '               (n.startswith(d + "p") and n[len(d) + 1:].isdigit()):',
        '            if False or \\\n'
        '               (n.startswith(d + "p") and n[len(d) + 1:].isdigit()):',
        "U 盘判定: 分区也算「盘」(同一块盘被数两遍, 挂载时去挂一个分区)",
    ),
    (
        "server/usb_export.py",
        'USB_REPORT_DIRNAME = "AGING-REPORTS"',
        'USB_REPORT_DIRNAME = "reports"',
        "U 盘上的目录名改了(工人按约定去 AGING-REPORTS/ 里找会找不到)",
    ),
    (
        "server/usb_export.py",
        '            shutil.copyfile(pdf_path, dest)',
        '            open(dest, "wb").close()',
        "拷贝变成了「只建个空文件」(盘上的报告是空的)",
    ),
    (
        "server/usb_export.py",
        '    with open(tmp, "w", newline="", encoding="utf-8-sig") as f:',
        '    with open(tmp, "w", newline="", encoding="utf-8") as f:',
        "INDEX.csv 不带 BOM 了(Excel 双击打开会乱码)",
    ),
    (
        "server/usb_export.py",
        '    if rows and list(rows[0]) == list(INDEX_HEADER):\n'
        '        rows = rows[1:]',
        '    if False:\n'
        '        rows = rows[1:]',
        "INDEX.csv 每写一次就多一行表头(第二次写完表头有两行)",
    ),
    (
        "server/usb_export.py",
        '    return f"{sn}|{span.split(\' ~ \')[0]}"',
        '    return sn',
        "INDEX.csv 的去重键只剩 SN(第二轮会把第一轮那行顶掉)",
    ),
    (
        "server/usb_export.py",
        '            return UsbExportResult(\n'
        '                False, "", "",\n'
        '                "未检测到 U 盘 —— 报告仍在本机 reports/pdf/")',
        '            raise RuntimeError("未检测到 U 盘")',
        "没有 U 盘时直接抛异常(把已经跑完、已经入库的那一轮掀翻)",
    ),
    (
        "server/usb_export.py",
        '        return UsbExportResult(\n'
        '            True, report_dir, filename,\n'
        '            f"报告已存到 U 盘: {report_dir}")',
        '        return UsbExportResult(True, "", filename, "")',
        "存成了但**不报路径**(工人不知道去哪儿拿报告)",
    ),
    (
        "server/tests/usb.py",
        '        if disk and disk in protected:',
        '        if False:',
        "USB 写测试: 不再剔除「正在用着的盘」(会往报告所在的盘上裸写)",
    ),
    (
        "server/runner.py",
        '        await self._export_to_usb(path, self._report_dict(summary))',
        '        pass  # 变异: 不存 U 盘了',
        "接线被拆: runner 收尾不再存 U 盘",
    ),
    (
        "server/runner.py",
        '                await self._export_to_usb(pdf_path, run_for_pdf)',
        '                pass  # 变异: 父轮合并版不同步',
        "接线被拆: 父轮合并重导的那份不同步到 U 盘(盘上留着旧版)",
    ),
    (
        "server/main.py",
        '        res = await usb_export.export_report_to_usb(path, data)',
        '        res = None  # 变异: 手动补导不存 U 盘',
        "接线被拆: 手动补导 PDF 不存 U 盘",
    ),
]


def build_copy(dst, rel, new_text):
    """把 server/ + config.yaml + 测试文件复制到 dst, 并把 rel 写成 new_text。

    每个文件**只写一次** —— 被变异的那个根本不复制进来, 直接写变异后的
    内容。全程不覆写任何已存在的文件, 所以跑完不需要还原。

    ⚠️ **测试依赖的模块也得复制** —— `tests/_tmp.py`(mkdtemp)不复制过去,
    测试在副本里根本 import 不到 ⇒ 崩了而不是「被断言咬住」⇒
    每条变异都会被误判成「没抓住」。
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
    # static/ 也要 —— `server/main.py` 在**导入时**就把 static/ 挂上去,
    # 目录不存在会直接 RuntimeError ⇒ 测试是「崩了」而不是「被断言咬住」。
    shutil.copytree(os.path.join(REPO, "static"),
                    os.path.join(dst, "static"),
                    ignore=shutil.ignore_patterns("__pycache__"))
    os.makedirs(os.path.join(dst, "tests"), exist_ok=True)
    for fn in ("test_usb_export.py", "_tmp.py", "_fake_sysfs.py"):
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
    root = tempfile.mkdtemp(prefix="mut_usbexp_")
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

        work = os.path.join(root, f"m{i:02d}")
        os.makedirs(work, exist_ok=True)
        build_copy(work, rel, orig.replace(frm, to))

        code, out = run_one(work)
        fails = out.count("✗")
        if code != 0 and fails == 0:
            # ⚠️ **红了但一个 ✗ 都没有** —— 那多半不是断言把它咬住的, 而是
            #   测试自己崩了(语法错/导入错/异常)。这不算「咬得动」——
            #   崩了只能证明「跑不起来」, 证明不了「断言抓得住这个缺陷」。
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
