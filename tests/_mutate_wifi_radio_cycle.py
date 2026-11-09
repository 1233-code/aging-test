# 变异测试: 故意把「不许硬拔一条正在工作的射频」改坏, 看
# tests/test_wifi_radio_cycle.py 会不会红。
#
# 为什么要有这么个东西: 「测试全绿」本身说明不了什么 —— 断言写错了、
# 写松了、或者压根没跑到, 都是绿的。唯一能证明这测试**咬得动**的办法,
# 就是亲手把代码改坏, 看它是不是真的红。
#
# 这一份守的是 2026-10-30 新板那次「板子两次重启」教训出来的规矩:
#   ① 射频正关联着 AP 时不许发 `ip link set` (硬拔 ⇒ 板子被硬复位);
#   ② 「看不出来」也要按「连着」处理(不能拿整块板子赌);
#   ③ 只读检查照做 —— 这一项不能测成残废;
#   ④ 降级必须**写进报告**(有一小段没测, 得让人知道);
#   ⑤ 射频项的位置 —— 排在 **GPU 之前**(2026-11-04 用户定的;
#     原先是「排在整轮最后」, 2026-10-30 的理由是「万一它把板子带崩,
#     崩的也是最后一步, 老化负载早就跑完落库了」)。
#
# 做法: **每个变异单开一个全新目录**, 把 server/ + config.yaml + 那个测试
# 文件复制进去, 在**副本**上做一处改动, 在副本目录里跑测试。
# 被改的那个文件是从头到尾**只写一次**(直接写变异后的内容), 别的文件原样
# 复制 —— 所以整个过程不覆写任何已存在的文件, 跑完也不用还原。
#
# ⚠️ 只在**副本**上改 —— 仓库里 server/ 是产线在跑的那份, 改坏了没人知道。
#
# 跑法: .venv/bin/python tests/_mutate_wifi_radio_cycle.py
import os
import shutil
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(REPO, ".venv", "bin", "python")
TEST = "tests.test_wifi_radio_cycle"

# 每个变异: (文件, from, to, 说明)。from 必须在源文件里**找得到**, 否则算失败。
MUTATIONS = [
    (
        "server/tests/wifi.py",
        "        elif busy is not False and not force:",
        "        elif busy is True:",
        "WIFI: 「射频状态看不出来」时也去开关(拿整块板子赌)",
    ),
    (
        "server/tests/wifi.py",
        "        elif busy is not False and not force:",
        "        elif False:",
        "WIFI: 射频关联着也照做开关(2026-10-30 那两次重启就是被它带走的)",
    ),
    (
        "server/tests/wifi.py",
        "        carrier = _carrier_associated(iface)",
        "        carrier = None",
        "WIFI: 不看 carrier 探针(厂商驱动答不上 iw 时就漏判成空闲)",
    ),
    (
        "server/tests/wifi.py",
        "        has_ip = await self._iface_has_ip(ip_bin, iface)",
        "        has_ip = None",
        "WIFI: 不看「接口上有 IP」探针(驱动不管 carrier 时就漏判成空闲)",
    ),
    (
        "server/tests/wifi.py",
        '            result.metrics["degraded"] = bool(degraded_reasons)',
        '            result.metrics["degraded"] = False',
        "WIFI: 降级标记不记(有一小段没测, 报告里却看不出来)",
    ),
    (
        "server/tests/wifi.py",
        '            allow_cycle = bool(sw_cfg)',
        # ⚠️ 下面这个 `to` 只作用在**临时副本**里(跑完就扔)——
        #   它的作用是证明「配置项被忽略」这种改坏**会被测试咬住**。
        "            allow_cycle = True",
        "WIFI: 配置项 software_switch 失效(现场关不掉软件开关)",
    ),
    (
        "server/tests/wifi.py",
        '                degraded_reasons.append(\n'
        '                    "射频状态看不出来, 跳过软件开关(不拿整块板子赌)"\n'
        "                )",
        "                pass",
        "WIFI: 「看不出来」这条路不记降级原因",
    ),
    (
        "server/tests/wifi.py",
        'result.metrics["radio_cycle_skipped"] = True',
        'result.metrics["radio_cycle_skipped"] = False',
        "WIFI: ① 跳过与否不记(报告里看不出这一小段到底测没测) "
        "[故意多改: 三处都是「跳过软件开关」的分支(配置关的 / 射频关联着的 / "
        "射频看不出来的), 漏掉任何一处, 那一处就会把「没跳过」说成「跳过了」]",
    ),
    (
        "server/runner.py",
        '    "wifi", "bluetooth",\n'
        '    # ④ 重载收尾 (产热最高)\n'
        '    "gpu", "cpu",',
        '    "wifi_tmp_placeholder", "bluetooth",\n'
        '    # ④ 重载收尾 (产热最高)\n'
        '    "gpu", "cpu",',
        "RUNNER: 射频项从队列里掉了(整轮就不测 WiFi 了)",
    ),
    (
        "server/runner.py",
        '    "wifi", "bluetooth",\n'
        '    # ④ 重载收尾 (产热最高)\n'
        '    "gpu", "cpu",',
        '    # ④ 重载收尾 (产热最高)\n'
        '    "gpu", "cpu",\n'
        '    # ⑤ 射频又挪回最后 (2026-10-30 的老规矩)\n'
        '    "wifi", "bluetooth",',
        "RUNNER: 射频项被挪回整轮最后 —— 2026-11-04 定的「WiFi 在 GPU 之前」被推翻",
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
    shutil.copy2(os.path.join(REPO, "tests", "test_wifi_radio_cycle.py"),
                 os.path.join(dst, "tests", "test_wifi_radio_cycle.py"))

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
    root = tempfile.mkdtemp(prefix="mut_wifi_")
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
