# 变异测试: 故意把「旁挂文件读坏了就整份丢掉」这个毛病放回去, 看
# tests/test_clock_skew_lenient.py 会不会红。
#
# 为什么要有这么个东西: 「测试全绿」本身说明不了什么 —— 断言写错了、
# 写松了、或者压根没跑到, 都是绿的。唯一能证明这测试**咬得动**的办法,
# 就是亲手把代码改坏, 看它是不是真的红。
#
# 这一份守的是 2026-11-11 在**产线盘**上实测到的坑:
#   盘上 `logs/clock_skew_run20.txt` 整个 258 字节全是乱码
#   (开头 `ff ff ff ff` + 随机二进制) ⇒ 严格解码直接抛 UnicodeDecodeError
#   ⇒ 旧代码 `except Exception: return ""` ⇒ **整份旁挂文件静默消失**
#   ⇒ 报告上「⚠ 本报告的时间戳是推算值, 不是系统时钟的真实时间」
#      **那条横幅不见了** ⇒ 工人以为时间是准的 ⇒ 拿推算值当准点用。
#
# 做法: **每个变异单开一个全新目录**, 把 server/ + config.yaml + 那个测试
# 文件复制进去, 在**副本**上改一处, 用 PYTHONPATH 指过去跑测试。
# 被改的那份**只写一次** —— 全程不覆写任何已存在的文件。
#
# 跑法: .venv/bin/python tests/_mutate_clock_skew_lenient.py
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests._tmp import mkdtemp                       # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(REPO, ".venv", "bin", "python")
TEST = "tests.test_clock_skew_lenient"

# 每个变异: (文件, from, to, 说明)。from 必须在源文件里**找得到且唯一**。
# ⚠️ 这四处**必须写字面量** —— tests/_sweep_anchors.py 要静态查锚点,
#    写成变量它就 literal_eval 不了 ⇒ 那条变异从来没被静态验过。
MUTATIONS = [
    (
        "server/clock.py",
        "        raw = run_skew_path(run_id).read_bytes()",
        '        raw = run_skew_path(run_id).read_text(encoding="utf-8").encode("utf-8")',
        "CLOCK: 改回**严格解码** ⇒ 坏字节直接抛 UnicodeDecodeError "
        "⇒ 整份旁挂文件读不出来(旧病根)",
    ),
    (
        "server/clock.py",
        '    text = raw.decode("utf-8", errors="replace").strip()',
        '    text = raw.decode("utf-8", errors="strict").strip()',
        "CLOCK: 解码**不容错** ⇒ 一个坏字节就抛 UnicodeDecodeError "
        "⇒ 整份旁挂文件读不出来",
    ),
    (
        "server/clock.py",
        "    except OSError:",
        "    except OSError:\n        raise",
        "CLOCK: 读不到文件**直接往上抛** ⇒ 报告导出整个炸掉",
    ),
    (
        "server/clock.py",
        '    text = raw.decode("utf-8", errors="replace").strip()\n'
        '    if text:\n'
        '        return text',
        '    text = raw.decode("utf-8", errors="replace").strip()\n'
        '    if True:\n'
        '        return text',
        "CLOCK: **空文件也当空串返回** ⇒ 写到一半断电留下的 0 字节旁挂文件"
        "⇒ 横幅静默消失(跟整份乱码同一个后果)",
    ),
    (
        "server/report.py",
        "        has_clock_skew=bool(clock_skew),",
        "        has_clock_skew=False,",
        "REPORT: 报告**永远不打**那条「时间戳是推算值」的横幅",
    ),
]


def build_copy(dst, rel, new_text):
    """把 server/ + config.yaml + 那个测试文件复制到 dst。

    被变异的那个文件**只写一次** —— 根本不复制进来, 直接写变异后的内容。
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

    shutil.copy2(os.path.join(REPO, "config.yaml"),
                 os.path.join(dst, "config.yaml"))
    os.makedirs(os.path.join(dst, "tests"), exist_ok=True)
    for extra in ("test_clock_skew_lenient.py", "_tmp.py"):
        shutil.copy2(os.path.join(REPO, "tests", extra),
                     os.path.join(dst, "tests", extra))

    outp = os.path.join(dst, rel)
    os.makedirs(os.path.dirname(outp), exist_ok=True)
    with open(outp, "w", encoding="utf-8") as f:
        f.write(new_text)


def main():
    root = mkdtemp(prefix="mut_clockskew_")
    print(f"副本都在 {root} 下面\n")

    bad = 0
    for i, (rel, frm, to, desc) in enumerate(MUTATIONS):
        srcp = os.path.join(REPO, rel)
        with open(srcp, encoding="utf-8") as f:
            orig = f.read()
        if orig.count(frm) != 1:
            print(f"\n✗ {desc}\n   → 锚点出现 {orig.count(frm)} 次(要恰好 1 次): "
                  f"{frm[:70]!r}")
            bad += 1
            continue

        work = os.path.join(root, f"m{i:02d}")
        os.makedirs(work, exist_ok=True)
        build_copy(work, rel, orig.replace(frm, to))

        env = dict(os.environ)
        env["PYTHONPATH"] = work
        proc = subprocess.run([PY, "-m", TEST], cwd=work, env=env,
                              capture_output=True, text=True, timeout=600)
        out = (proc.stdout or "") + (proc.stderr or "")
        fails = out.count("✗")
        if proc.returncode != 0 and fails == 0:
            print(f"\n✗ {desc}\n   → 红了, 但一个 ✗ 都没有 (exit="
                  f"{proc.returncode}) —— 这是**崩了**, 不算数:\n{out[-600:]}")
            bad += 1
        elif proc.returncode != 0:
            print(f"\n✓ {desc}\n   → 测试变红 (exit={proc.returncode}, "
                  f"{fails} 项失败) —— 咬得动")
        else:
            print(f"\n✗ {desc}\n   → 测试还是绿的! 这个变异没被守住")
            bad += 1

    print("\n" + ("全部变异都被抓住了 ✓" if bad == 0
                  else f"有 {bad} 个变异没被抓住 ✗"))
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
