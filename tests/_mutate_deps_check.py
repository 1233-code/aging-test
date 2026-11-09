# 变异测试: 故意把「配置页的依赖检查」改坏, 看
# tests/test_deps_check_tool_resolve.py 会不会红。
#
# 为什么要有这么个东西: 「测试全绿」本身说明不了什么 —— 断言写错了、
# 写松了、或者压根没跑到, 都是绿的。唯一能证明这测试**咬得动**的办法,
# 就是亲手把代码改坏, 看它是不是真的红。
#
# 这一份守的是 2026-11-06 在新设备上(报告 SN A10010000000058)挖出来的
# 两个坑:
#   ① 配置页红条说「缺失 fio」—— 假的。fio 就在 U 盘里(`tools/bin/fio`),
#      而依赖检查自己写了一套只看系统 PATH 的 `_which()` ⇒ 看不见盘上那份。
#      ⇒ 工人被支去 `apt install`, 装到板子上, 拔盘就走, 白装。
#   ② 那轮 eMMC 项真的 error 了 —— `rc=127 · error while loading shared
#      libraries: libceph-common.so.2` ⇒ **盘上那份 fio 起不来**。
#      而「起不来」跟「没有」混成一句话 ⇒ 看着像板子坏了 ⇒ 白白返修好板子。
#
# ⚠️ 最坏的坏法是「**看着没毛病, 其实还在骗人**」:
#   - 把 find_tool 换回只看系统 PATH (①的病根回来);
#   - 探针不跑 (起不来的工具又被说成「有」);
#   - 「不是板子坏了」这句话删掉 (又开始骗工厂)。
#
# 做法: **每个变异单开一个全新目录**, 把 server/ + static/ 复制进去,
# 在**副本**上做一处改动, 在副本目录里跑测试。仓库里 server/ 是产线在跑
# 的那份 —— 只在副本上改, 副本也不删(留在 /tmp 里可查)。
#
# 跑法: .venv/bin/python tests/_mutate_deps_check.py
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests._tmp import mkdtemp                       # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(REPO, ".venv", "bin", "python")
TEST = "tests.test_deps_check_tool_resolve"

#: 副本里要带的目录/文件 —— **少一个测试就测不到**
#: (2026-11-04 踩过同款: 少复制 tests/_tmp.py ⇒ 6 条变异一条没验到)
COPY_TREE = ("server", "static")
COPY_FILES = ("config.yaml",)
COPY_TESTS = ("test_deps_check_tool_resolve.py",)

# 每个变异: (文件, from, to, 说明)。from 必须在源文件里**找得到且唯一**。
MUTATIONS = [
    (
        "server/main.py",
        "        path = find_tool(cmd)",
        '        path = (__import__("shutil").which(cmd) or "")',
        "deps: 找工具又退回只看系统 PATH ⇒ U 盘版盘上的 fio 又被说成「缺失」"
        "(2026-11-06 那个假警报的原样)",
    ),
    (
        "server/main.py",
        "            reason = await _probe(path, cmd)",
        "            reason = \"\"",
        "deps: 探针不跑(起不来的工具又被当成「有」)",
    ),
    (
        "server/main.py",
        '        if rc == 127 or "error while loading shared libraries" in text:',
        "        if False:",
        "deps: 认不出「缺共享库」这一类(rc=127)",
    ),
    (
        "server/main.py",
        "            if reason:\n"
        '                item["ok"] = False\n'
        '                item["broken"] = True\n'
        '                item["broken_reason"] = reason',
        "            if reason:\n"
        '                item["broken_reason"] = reason',
        "deps: 起不来的工具仍然判成「有」(all_ok 还是 True)",
    ),
    (
        "server/main.py",
        "    critical_broken = [\n"
        '        x["name"] for x in items\n'
        '        if not x["ok"] and x["name"] in CRITICAL_BINS and x.get("broken")\n'
        "    ]",
        "    critical_broken = []",
        "deps: 「起不来」这一类不进 critical_broken(前端不会显示)",
    ),
    (
        "server/main.py",
        '        "fix_command": "" if usb_mode else "sudo bash scripts/install_deps.sh",',
        '        "fix_command": "sudo bash scripts/install_deps.sh",',
        "deps: U 盘版又让工人去板子上装东西(拔盘就走, 白装)",
    ),
    (
        "server/tests/_tools.py",
        '    if rc != 127 and "error while loading shared libraries" not in (text or ""):\n'
        '        return ""',
        "    return \"\"",
        "TOOLS: 认不出「工具起不来」(这一类错误又跟「板子坏了」混成一句)",
    ),
    (
        "server/tests/_tools.py",
        '    return (\n'
        '        "工具起不来(缺共享库), 不是板子坏了 —— "',
        '    return (\n'
        '        "工具起不来(缺共享库) —— "',
        "TOOLS: 不说「不是板子坏了」⇒ 工厂会把**盘**的问题算到板子上, 白白返修",
    ),
    (
        "server/tests/emmc.py",
        "            loader_msg = loader_error_message(rc, text)\n"
        "            if loader_msg:\n"
        "                raise RuntimeError(loader_msg)\n",
        "",
        "eMMC: 起不来的 fio 又被写成「fio 写失败」(看着像板子坏了)",
    ),
    (
        "server/tests/usb.py",
        "                    loader_msg = loader_error_message(rc, text)\n"
        "                    if loader_msg:\n"
        "                        await ctx.on_log(f\"[USB] ❌ fio 起不来: {loader_msg}\")\n"
        "                        result.status = \"error\"\n"
        "                        result.error = loader_msg\n"
        "                        return result\n",
        "",
        "USB: 起不来的 fio 又被当成「盘写坏了」(fio 退出码 127 ⇒ 盘背锅)",
    ),
    (
        "static/index.html",
        "                        <code>{{ depsInfo.fix_command }}</code>",
        "                        <code>sudo bash scripts/install_deps.sh</code>",
        "前端: 红条又硬写安装命令(U 盘版那条是错的)",
    ),
    (
        "static/index.html",
        "                <div class=\"dep-banner-title\">\n"
        "                    <span v-if=\"depsInfo.critical_broken && depsInfo.critical_broken.length\">\n"
        "                        {{ depsInfo.critical_broken.length }} 个关键工具起不来",
        "                <div class=\"dep-banner-title\">\n"
        "                    <span v-if=\"false\">\n"
        "                        {{ depsInfo.critical_broken.length }} 个关键工具起不来",
        "前端: 红条标题不认「起不来」这一类(工人只看到「缺」, 照着去装没用)",
    ),
]


def build_copy(dst: str, rel: str, new_text: str):
    """把仓库复制到 dst, 并把 rel 那个文件换成改过的内容。"""
    for tree in COPY_TREE:
        shutil.copytree(os.path.join(REPO, tree), os.path.join(dst, tree),
                        dirs_exist_ok=True)
    for f in COPY_FILES:
        src = os.path.join(REPO, f)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(dst, f))
    os.makedirs(os.path.join(dst, "tests"), exist_ok=True)
    for f in COPY_TESTS:
        shutil.copy2(os.path.join(REPO, "tests", f),
                     os.path.join(dst, "tests", f))

    outp = os.path.join(dst, rel)
    os.makedirs(os.path.dirname(outp), exist_ok=True)
    with open(outp, "w", encoding="utf-8") as f:
        f.write(new_text)


def run_one(work: str):
    """在副本目录里跑测试。返回 (红了没有, 失败项数, 输出)。"""
    env = dict(os.environ)
    env["PYTHONPATH"] = work
    proc = subprocess.run(
        [PY, "-m", TEST], cwd=work, env=env,
        capture_output=True, text=True, timeout=300,
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode != 0, out.count("✗"), out


def main():
    root = mkdtemp(prefix="mut_deps_")
    print(f"副本都在 {root} 下面(进程退出时自动删掉)\n")

    bad = 0
    for i, (rel, frm, to, desc) in enumerate(MUTATIONS):
        srcp = os.path.join(REPO, rel)
        with open(srcp, encoding="utf-8") as f:
            orig = f.read()
        if frm not in orig:
            print(f"\n✗ {desc}\n   → 变异点没找到(源文件变了?): {frm[:70]!r}")
            bad += 1
            continue
        if orig.count(frm) > 1:
            print(f"\n✗ {desc}\n   → 锚点不唯一(出现 {orig.count(frm)} 次) ⇒ "
                  f"`replace` 会把所有出现处一起改 ⇒ 证据不成立")
            bad += 1
            continue

        work = os.path.join(root, f"m{i:02d}")
        os.makedirs(work, exist_ok=True)
        build_copy(work, rel, orig.replace(frm, to))

        red, fails, out = run_one(work)
        if red and fails == 0:
            # ⚠️ **红了但一个 ✗ 都没有** —— 那多半不是断言把它咬住的, 而是
            #   测试自己崩了(语法错/导入错/异常)。这不算「咬得动」。
            print(f"\n✗ {desc}\n   → 红了, 但一个 ✗ 都没有 —— "
                  f"这是**崩了**, 不是被断言咬住的, 不算数:\n{out[-800:]}")
            bad += 1
        elif red:
            print(f"\n✓ {desc}\n   → 测试变红 ({fails} 项失败) —— 咬得动")
        else:
            print(f"\n✗ {desc}\n   → 测试还是绿的! 这个变异没被守住")
            bad += 1

    print("\n" + ("全部变异都被抓住了 ✓" if bad == 0
                  else f"有 {bad} 个变异没被抓住 ✗"))
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
