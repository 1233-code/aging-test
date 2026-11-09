# 变异测试: 故意把「板子重启后时钟倒跳」这摊改坏, 看
# tests/test_clock.py 会不会红。
#
# 为什么要有这么个东西: 「测试全绿」本身说明不了什么 —— 断言写错了、
# 写松了、或者压根没跑到, 都是绿的。唯一能证明这测试**咬得动**的办法,
# 就是亲手把代码改坏, 看它是不是真的红。
#
# 这一份守的是 2026-10-30 查清的「板子硬复位 ⇒ 系统时钟被退回上次干净
# 关机那一刻(倒跳 42 天)」换来的规矩:
#   ① 开机要**认得出**时钟倒退(拿锚点比) —— 认不出就默默用错的墙钟;
#   ② 倒退后的时间戳要**单调不减** —— 归档序列不许倒挂;
#   ③ 小幅回拨(NTP 校时)**不算**倒退 —— 否则真时间被标成不可信;
#   ④ 时长要用**单调钟** —— 墙钟跳变算不出负数时长;
#   ⑤ 落库的时间戳一律走 `clock.now()` —— 别谁又直接 `time.time()`;
#   ⑥ 推算值要**按轮**留旁挂文件 —— 事后重导旧报告时横幅还在;
#   ⑦ 时钟正常时**不留**旁挂文件 —— 别给正常轮次也挂个黄条;
#   ⑧ 报告上要**打横幅** —— 纸面归档的人得知道这批时间戳不是准点;
#   ⑨ 导出时要**自动**带上(不用每个调用点自己记得传);
#   ⑩ 喂进来的「已知正确时间」要**只收往前的** —— 歪的不能收。
#
# 做法: **每个变异单开一个全新目录**, 把 server/ + config.yaml + 那个测试
# 文件复制进去, 在**副本**上做一处改动, 在副本目录里跑测试。
# 被改的那个文件是从头到尾**只写一次**(直接写变异后的内容), 别的文件原样
# 复制 —— 所以整个过程不覆写任何已存在的文件, 跑完也不用还原。
# ⚠️ 仓库里 server/ 是产线在跑的那份, **只在副本上改**。
#
# 临时目录用 `tests._tmp.mkdtemp` —— 进程退出时自动删掉, 不在 /tmp 里留残骸。
#
# 跑法: .venv/bin/python tests/_mutate_clock.py
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests._tmp import mkdtemp                       # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(REPO, ".venv", "bin", "python")
TEST = "tests.test_clock"

# 每个变异: (文件, from, to, 说明)。from 必须在源文件里**找得到**, 否则算失败。
MUTATIONS = [
    (
        "server/clock.py",
        "    return raw < _last_observed - SKEW_TOLERANCE_SEC",
        "    return False",
        "CLOCK: **认不出**时钟倒退(开机/运行中都默默用错的墙钟 ⇒ 报告时间全错)",
    ),
    (
        "server/clock.py",
        "    if _mode == \"estimated\":\n"
        "        val = _ref_wall + (time.monotonic() - _ref_mono)\n"
        "    else:",
        "    if _mode == \"estimated\":\n"
        "        val = time.time()\n"
        "    else:",
        "CLOCK: 判定了是推算值, 却还是用墙钟(倒退 42 天照旧印在报告上)",
    ),
    (
        "server/clock.py",
        "        elif raw > _last_observed:\n"
        "            val = raw\n"
        "        else:\n"
        "            # 小幅回拨或持平 —— 不许往回退(归档序列不许倒挂)\n"
        "            val = _last_observed",
        "        elif raw > _last_observed:\n"
        "            val = raw\n"
        "        else:\n"
        "            val = raw",
        "CLOCK: 小幅回拨就让时间戳往回退(归档序列倒挂)",
    ),
    (
        "server/clock.py",
        "SKEW_TOLERANCE_SEC = 300.0",
        "SKEW_TOLERANCE_SEC = 0.0",
        "CLOCK: 容差没了(NTP 校时拨 60 秒就被当成「倒退 42 天」那种)",
    ),
    (
        "server/clock.py",
        "    if not is_estimated():\n"
        "        return None",
        "    if True:\n"
        "        return None",
        "CLOCK: 推算值也不写旁挂文件(报告打不出横幅)",
    ),
    (
        "server/clock.py",
        "    if not is_estimated():\n"
        "        return None",
        "    pass",
        "CLOCK: 时钟正常也写旁挂文件(正常轮次也挂个黄条)",
    ),
    (
        "server/clock.py",
        '            "last_wall": round(cur, 3),',
        '            "last_wall": round(time.time(), 3),',
        "CLOCK: 锚点里存的是**歪的墙钟**(不是推算值)—— 下次开机连锚点都是错的",
    ),
    (
        "server/clock.py",
        "        os.replace(str(tmp), str(p))\n"
        "        return True",
        "        return True",
        "CLOCK: 锚点没真的落盘(硬复位后又是 42 天)",
    ),
    (
        "server/clock.py",
        "    if ts <= 0 or ts <= _last_observed:\n"
        "        return False",
        "    if ts <= 0:\n"
        "        return False",
        "CLOCK: 来者不拒(把一个更早的、歪的时间也当真 ⇒ 归档又倒挂)",
    ),
    (
        "server/clock.py",
        '    return Config.data_root() / "logs" / "clock_anchor.json"',
        '    return Config.data_root() / "reports" / "clock_anchor.json"',
        "CLOCK: 锚点不跟证据文件放一起(找不到/被清日志时一起丢)",
    ),
    (
        "server/report.py",
        "        clock_skew=clock_skew,",
        '        clock_skew="",',
        "REPORT: 报告里不印时钟横幅(纸面归档的人不知道时间不可信)",
    ),
    (
        "server/report.py",
        "    if cpu_evidence is None:\n"
        '        cpu_evidence = boot_evidence_mod.read_cpu_evidence(run.get("id"))\n'
        "    if clock_skew is None:\n"
        '        clock_skew = clock.read_run_skew(run.get("id"))',
        "    if cpu_evidence is None:\n"
        '        cpu_evidence = boot_evidence_mod.read_cpu_evidence(run.get("id"))\n'
        "    if clock_skew is None:\n"
        '        clock_skew = ""',
        "REPORT: 导出时不自动带上旁挂文件(得每个调用点自己记得传) "
        "[锚点带上下文 ⇒ 只改导出那处, 不动 render_html 那处]",
    ),
    (
        "server/runner.py",
        "            result.duration_sec = time.monotonic() - t0",
        "            result.duration_sec = clock.now() - t0",
        "RUNNER: 时长又用回墙钟(跳变时算出负数时长)",
    ),
    (
        "server/clock.py",
        "    if is_estimated():\n"
        "        return None            # 时钟不可信 ⇒ 存进去只会越歪越歪",
        "    if False:\n"
        "        return None",
        "CLOCK: 时钟是推算值时也去存时间(存的是歪的墙钟 ⇒ 越歪越歪)",
    ),
    (
        "server/clock.py",
        '            "sudo", "-n", FAKE_HWCLOCK, "save",',
        '            "sudo", "-n", FAKE_HWCLOCK, "load",',
        "CLOCK: 调的是 load 不是 save(存时间变成了取时间 ⇒ 板子还是记不住)",
    ),
    (
        "server/clock.py",
        "        return proc.returncode == 0",
        "        return True",
        "CLOCK: 存不上也说成功(以为板子记住了时间)",
    ),
    (
        "server/main.py",
        "                ok = await clock.save_hwclock()",
        "                ok = None",
        "MAIN: 服务根本不去调 fake-hwclock save(A1 没接线)",
    ),
    (
        "server/db.py",
        "                (clock.now(), \"running\", json.dumps(config, ensure_ascii=False),",
        "                (time.time(), \"running\", json.dumps(config, ensure_ascii=False),",
        "DB: 落库时间戳又直接用 time.time()(绕过了统一时间源)",
    ),
]


def build_copy(dst, rel, new_text):
    """把 server/ + config.yaml + 那个测试文件复制到 dst, 并把 rel 写成 new_text。

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
    for extra in ("test_clock.py", "_tmp.py"):
        shutil.copy2(os.path.join(REPO, "tests", extra),
                     os.path.join(dst, "tests", extra))

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
    root = mkdtemp(prefix="mut_clock_")
    print(f"副本都在 {root} 下面(进程退出时自动删掉)\n")

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
