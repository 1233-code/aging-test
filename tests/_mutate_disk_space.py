# 变异测试: 故意把「盘上空间提醒」改坏, 看 tests/test_disk_space.py 和
# tests/test_frontend_disk_warning.js 会不会红。
#
# 为什么要有这么个东西: 「测试全绿」本身说明不了什么 —— 断言写错了、
# 写松了、或者压根没跑到, 都是绿的。唯一能证明这测试**咬得动**的办法,
# 就是亲手把代码改坏, 看它是不是真的红。
#
# 这一份守的是 2026-11-13 工人提的规矩:
#   ① **只在空间不足的时候显示** —— 空间充足时任何地方都不显示提醒;
#   ② **提醒, 不拦截** —— 空间不够也照样能开测;
#   ③ 读不到空间 ⇒ 当作够(不能因为读不到就把测试搞挂);
#   ④ 措辞不许吓人 —— 第一句先讲「现在还能不能测」;
#   ⑤ 文案由后端出, 前端一个字都不改(单一口径)。
#
# ⚠️ **这些改动全部只发生在临时副本里** —— 仓库里的文件一个字都不动。
#   副本跑完就扔, 不用还原。
#
# 跑法: .venv/bin/python tests/_mutate_disk_space.py
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests._tmp import mkdtemp                       # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(REPO, ".venv", "bin", "python")

# 每个变异: (文件, from, to, 说明, 跑哪个测试)。
#   kind == "py" → 跑 tests.test_disk_space
#   kind == "js" → 跑 node tests/test_frontend_disk_warning.js
MUTATIONS = [
    (
        "server/disk_space.py",
        "    if free >= WARN_FREE_MB:\n",
        "    if False:\n",
        "空间**够**的时候也提醒"
        "(⇒ 「只在空间不足时显示」这条规矩破了 —— 工人天天看一条没用的提醒, "
        "真出事那天就不当回事了)",
        "py",
    ),
    (
        "server/disk_space.py",
        "    if free is None:\n"
        "        return None          # 读不到 ⇒ 当作够(绝不能因读不到就把测试搞挂)\n",
        "    if free is None:\n"
        "        free = 0.0           # 读不到 ⇒ 当作「一点空间都没有」\n",
        "读不到空间 ⇒ 当成「满了」"
        "(⇒ 盘刚拔了一下就在界面上喊「不能用了」—— 吓人, 而且是假的)",
        "py",
    ),
    (
        "server/disk_space.py",
        '    level = "critical" if free < CRITICAL_FREE_MB else "warn"\n',
        '    level = "warn"\n',
        "空间已经很少了也不升级措辞"
        "(⇒ 工人不知道「再写下去结果可能记不下来」)",
        "py",
    ),
    (
        "server/disk_space.py",
        "PER_DEVICE_MB = 1.7\n",
        "PER_DEVICE_MB = 170.0\n",
        "「还能存几台」算错(差 100 倍)",
        "py",
    ),
    (
        "server/disk_space.py",
        "    if not usb_mode():\n",
        "    if False:\n",
        "设备版/本地部署也提醒"
        "(⇒ 用户定的规矩是「只管 U 盘版」: 设备版的数据在设备自己的系统盘上, "
        "不该由这条提醒去管)",
        "py",
    ),
    (
        "server/disk_space.py",
        'f"{left} 台设备。**现在还能正常测**, 但报告快存不下了 ⇒ "\n',
        'f"{left} 台设备。报告快存不下了 ⇒ "\n',
        "提醒里的话不先讲「现在还能正常测」"
        "(⇒ 界面提示吓人: 工人读到「空间不足」会以为这根盘废了)",
        "py",
    ),
    (
        "server/main.py",
        '        "disk": disk_space.disk_warning(),\n',
        '        "disk": None,\n',
        "/api/config 永远不带空间信息 ⇒ 界面上永远不提醒",
        "py",
    ),
    (
        "server/main.py",
        '            res["disk"] = disk_space.disk_warning()\n',
        '            res["disk"] = None\n',
        "开跑那一刻不再复查 ⇒ 页面加载时的快照过期了也不管",
        "py",
    ),
    (
        "static/app.js",
        "            testerName.value = (data.report && data.report.operator) || '';\n"
        "            // 盘上空间 (2026-11-13) —— **够 ⇒ null ⇒ 界面上一个字都不显示**。\n"
        "            diskWarning.value = data.disk || null;\n",
        "            testerName.value = (data.report && data.report.operator) || '';\n",
        "加载页面时不接后端的空间信息 ⇒ 页面开着不动的时候看不到提醒",
        "js",
    ),
    (
        "static/app.js",
        "            runId.value = data.run_id;\n"
        "            // 盘上空间 —— **开跑这一刻再查一次**(页面加载时那份是快照;\n"
        "            // 页面开着不动几小时是常事 ⇒ 到真开跑的时候可能早写满了)。\n"
        "            // 够 ⇒ null ⇒ 不显示; 不够 ⇒ 挂一条提醒(**不拦**开跑)。\n"
        "            diskWarning.value = data.disk || null;\n",
        "            runId.value = data.run_id;\n",
        "开跑时不接后端的空间信息 ⇒ 空间在这期间写满了也不提醒",
        "js",
    ),
    (
        "static/index.html",
        '        <div v-if="diskWarning" :class="[\'disk-warning\', diskWarning.level]">\n',
        '        <div v-if="true" :class="[\'disk-warning\', diskWarning.level]">\n',
        "模板不再按「有没有值」来渲染 ⇒ 空间充足时也摆一条空提醒",
        "js",
    ),
]


def build_copy(dst, rel, new_text):
    """把 server/ + static/ + config.yaml + 测试文件复制到 dst, 并把 rel 写成 new_text。

    每个文件**只写一次** —— 被变异的那个根本不复制进来, 直接写变异后的
    内容。全程不覆写任何已存在的文件, 所以跑完不需要还原。
    """
    for top in ("server", "static"):
        src = os.path.join(REPO, top)
        for root, dirs, files in os.walk(src):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            relroot = os.path.relpath(root, src)
            outroot = os.path.join(dst, top, relroot)
            os.makedirs(outroot, exist_ok=True)
            for fn in files:
                relfile = os.path.normpath(os.path.join(relroot, fn))
                if os.path.join(top, relfile) == rel:
                    continue          # 这个下面单独写
                shutil.copy2(os.path.join(root, fn), os.path.join(outroot, fn))

    shutil.copy2(os.path.join(REPO, "config.yaml"),
                 os.path.join(dst, "config.yaml"))
    os.makedirs(os.path.join(dst, "tests"), exist_ok=True)
    # ⚠️ **必须把测试依赖的模块一起复制** —— 少了 `tests/_tmp.py`,
    #    变异跑起来直接 ImportError ⇒ 一条都验不到(2026-11-09 踩过)。
    for fn in ("test_disk_space.py", "test_frontend_disk_warning.js",
               "_vue_harness.js", "_tmp.py"):
        shutil.copy2(os.path.join(REPO, "tests", fn),
                     os.path.join(dst, "tests", fn))

    outp = os.path.join(dst, rel)
    os.makedirs(os.path.dirname(outp), exist_ok=True)
    with open(outp, "w", encoding="utf-8") as f:
        f.write(new_text)


def run_one(work, kind):
    env = dict(os.environ)
    env["PYTHONPATH"] = work
    if kind == "py":
        cmd = [PY, "-m", "tests.test_disk_space"]
    else:
        env["AGING_STATIC_DIR"] = os.path.join(work, "static")
        env["AGING_APP_JS"] = os.path.join(work, "static", "app.js")
        env["AGING_INDEX_HTML"] = os.path.join(work, "static", "index.html")
        cmd = ["node", os.path.join(work, "tests", "test_frontend_disk_warning.js")]
    proc = subprocess.run(
        cmd, cwd=work, env=env, capture_output=True, text=True, timeout=300,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def main():
    root = mkdtemp(prefix="mut_disk_space_")
    print(f"副本都在 {root} 下面\n")

    bad = 0
    for i, (rel, frm, to, desc, kind) in enumerate(MUTATIONS):
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

        code, out = run_one(work, kind)
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
