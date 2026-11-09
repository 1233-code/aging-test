"""变异测试: 「挂载点漂移」那套认盘逻辑, 测试到底咬不咬得住。

**背景**: 盘被拔了再插, udisks2 可能挂到**别的路径**
(`/media/User/AGING` → `/media/User/AGING1`)⇒ 正在跑的服务手里的路径当场
失效 ⇒ `/api/history` 500 + 数据写回设备上(违背「拔盘即无」)。

⇒ 修法: **记设备, 不记路径** —— 而且**设备名也不是稳定的身份**(实测:
同一根盘插拔一次, `/dev/sdb` → `/dev/sda`, major:minor 也变)⇒ 还得靠
**文件系统 UUID** 兜底。

每个变异 = 把 `server/config.py` 改坏一处, 看
`tests/test_usb_mount_drift.py` 会不会变红。红了而且是**被断言咬住的**
(输出里有 ✗)才算数 —— 只是崩了不算。

⚠️ **副本目录必须复制测试依赖的模块**(少复制一个, 测试在副本里就
   import 不到 ⇒ 崩了而不是「被断言咬住」⇒ 每条变异都会被误判成
   「没抓住」)。这里要复制:
     server/ 全部(config.py / db.py / clock.py / tests/_tools.py …)
     tests/test_usb_mount_drift.py  tests/_tmp.py  tests/_fake_sysfs.py
     config.yaml
每个变异一个**全新目录**, 变异后的文件用 "x" 另建(不覆写已存在的)。

跑法: .venv/bin/python tests/_mutate_usb_mount_drift.py
"""
import os
import shutil
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(REPO, ".venv", "bin", "python")

#: (文件, 锚点, 改成什么, 说明)
MUTATIONS = [
    (
        "server/config.py",
        "        if cls._stick_uuid:\n"
        "            for mp, dev, src in rows:\n"
        "                if _uuid_of(src) == cls._stick_uuid:\n"
        "                    return cls._note_stick(mp, dev, src)",
        "        if False:\n"
        "            for mp, dev, src in rows:\n"
        "                if _uuid_of(src) == cls._stick_uuid:\n"
        "                    return cls._note_stick(mp, dev, src)",
        "认盘不认 UUID ⇒ 设备名一变(重插一次)就跟丢这块盘",
    ),
    (
        "server/config.py",
        "            cls._stick_uuid = _uuid_of(best[2])",
        "            cls._stick_uuid = None",
        "启动时不记 UUID ⇒ 后面的 UUID 兜底根本没得用",
    ),
    (
        "server/config.py",
        "            if dev == cls._stick_dev or (cls._stick_src and src == cls._stick_src):\n"
        "                return cls._note_stick(mp, dev, src)",
        "            if False:\n"
        "                return cls._note_stick(mp, dev, src)",
        "认盘不认设备身份 ⇒ 挂载点一漂就找不回来",
    ),
    (
        "server/config.py",
        "        if s.startswith(cls._stick_mp + os.sep):\n"
        "            return Path(cur) / s[len(cls._stick_mp) + 1:]",
        "        if False:\n"
        "            return Path(cur) / s[len(cls._stick_mp) + 1:]",
        "漂了之后**盘上的子目录**(tools/ 等)不跟着走",
    ),
    (
        "server/config.py",
        "        if not cls._stick_dev:\n"
        "            return False\n"
        "        return cls._stick_mountpoint() is None",
        "        if not cls._stick_dev:\n"
        "            return False\n"
        "        return False",
        "永远不认「跟丢」⇒ 盘拔了还往设备上写(漂移的成因)",
    ),
    (
        "server/config.py",
        "            if mp == \"/\":\n"
        "                continue          # 根文件系统不算「一根盘」—— 设备版走的就是它",
        "            if False:\n"
        "                continue          # 根文件系统不算「一根盘」—— 设备版走的就是它",
        "把根文件系统当成 U 盘 ⇒ 设备版会把 eMMC 根当成那块盘",
    ),
]

COPY_TESTS = ("test_usb_mount_drift.py", "_tmp.py", "_fake_sysfs.py")
COPY_TOP = ("config.yaml",)


def build_copy(dst, rel, new_text):
    """把 server/ + 顶层文件 + 测试文件复制到 dst, 并把 rel 写成 new_text。

    ⚠️ **被变异的那个文件根本不复制进来**, 后面用 "x" 另建一份新的
    ⇒ 全程不覆写任何已存在的文件。
    """
    skip = os.path.relpath(rel, "server") if rel.startswith("server/") else None

    src = os.path.join(REPO, "server")
    for root, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        relroot = os.path.relpath(root, src)
        outroot = (os.path.join(dst, "server") if relroot == "."
                   else os.path.join(dst, "server", relroot))
        os.makedirs(outroot, exist_ok=True)
        for fn in files:
            relfile = os.path.normpath(os.path.join(relroot, fn))
            if skip is not None and relfile == skip:
                continue
            shutil.copy2(os.path.join(root, fn), os.path.join(outroot, fn))

    for fn in COPY_TOP:
        s = os.path.join(REPO, fn)
        if os.path.isfile(s):
            shutil.copy2(s, os.path.join(dst, fn))
    os.makedirs(os.path.join(dst, "tests"), exist_ok=True)
    for fn in COPY_TESTS:
        p = os.path.join(REPO, "tests", fn)
        if os.path.isfile(p):
            shutil.copy2(p, os.path.join(dst, "tests", fn))

    outp = os.path.join(dst, rel)
    os.makedirs(os.path.dirname(outp), exist_ok=True)
    with open(outp, "x", encoding="utf-8") as f:
        f.write(new_text)


def run_one(work):
    env = dict(os.environ)
    env["PYTHONPATH"] = work
    proc = subprocess.run(
        [PY, "-m", "tests.test_usb_mount_drift"],
        cwd=work, env=env, capture_output=True, text=True, timeout=600,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def main():
    root = tempfile.mkdtemp(prefix="mut_drift_")
    print(f"副本都在 {root} 下面\n")

    bad = 0
    for i, (rel, frm, to, desc) in enumerate(MUTATIONS):
        srcp = os.path.join(REPO, rel)
        with open(srcp, encoding="utf-8") as f:
            orig = f.read()
        if orig.count(frm) != 1:
            print(f"\n✗ {desc}\n   → 锚点在 {rel} 里出现 {orig.count(frm)} 次"
                  f"(必须**恰好一次**, 否则证据不成立): {frm[:60]!r}")
            bad += 1
            continue

        work = os.path.join(root, f"m{i:02d}")
        os.makedirs(work, exist_ok=True)
        build_copy(work, rel, orig.replace(frm, to))

        code, out = run_one(work)
        fails = out.count("✗")
        if code != 0 and fails == 0:
            print(f"\n✗ {desc}\n   → 红了, 但一个 ✗ 都没有 (exit={code}) —— "
                  f"这是**崩了**, 不是被断言咬住的, 不算数:\n{out[-800:]}")
            bad += 1
        elif code != 0:
            print(f"\n✓ {desc}\n   → 测试变红 (exit={code}, {fails} 项失败) "
                  f"—— 咬得动")
        else:
            print(f"\n✗ {desc}\n   → 测试**没红** ⇒ 这条变异漏过去了, "
                  f"测试咬不住它")
            bad += 1

    print("\n" + "─" * 60)
    if bad:
        print(f"✗ {bad} 条变异没被抓住")
        return 1
    print(f"✓ {len(MUTATIONS)} 条变异全被抓住")
    return 0


if __name__ == "__main__":
    sys.exit(main())
