"""变异测试: 「启动就绪判定」那套判据, 测试到底咬不咬得住。

**背景**: 2026-11-05 在新设备上实测 —— 服务**明明起来了**, 就绪检查却
连着 15 秒说「没起来」⇒ 脚本 return 1 ⇒ **浏览器那段永远执行不到**
⇒ 工人看到「点了没反应」+ 一个「启动失败」通知。
根因在**检查这一侧**: curl 缺失 / 被 http_proxy 劫持 / localhost 解析到 ::1。

⇒ 修法是三层判据: **python 探针**(禁代理 + 只打 127.0.0.1) → curl
(`--noproxy`) → **端口在听就算起来了**。

每个变异 = 把 `start.sh` 改坏一处, 看 `tests/test_usb_startup_ready.py`
会不会变红。红了而且是**被断言咬住的**(输出里有 ✗)才算数 —— 只是崩了不算。

⚠️ **副本只需要 start.sh + 这一个测试文件**(这个测试不 import 服务端代码),
   每个变异一个**全新目录**, 变异后的文件用 "x" 另建(不覆写已存在的)。

跑法: .venv/bin/python tests/_mutate_usb_startup_ready.py
"""
import os
import shutil
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(REPO, ".venv", "bin", "python")

#: (文件, 锚点, 改成什么, 说明) —— **四元组**: tests/_sweep_anchors.py
#: 只认这个形状(少一个字段它就「没法静态查锚点」⇒ 锚点烂了没人知道)
MUTATIONS = [
    (
        "start.sh",
        "op = urllib.request.build_opener(urllib.request.ProxyHandler({}))",
        "op = urllib.request.build_opener(urllib.request.ProxyHandler())",
        "python 探针不再禁用代理 ⇒ 有 http_proxy 的设备上照样够不着服务",
    ),
    (
        "start.sh",
        "    _health_probe && return 0\n    if command -v curl > /dev/null 2>&1; then",
        "    if command -v curl > /dev/null 2>&1; then",
        "判据里没有 python 探针 ⇒ 新设备上没装 curl 就判成「没起来」",
    ),
    (
        "start.sh",
        "curl -s --noproxy '*' --max-time 3",
        "curl -s --max-time 3",
        "curl 那条没禁代理 ⇒ 被 http_proxy 劫持",
    ),
    (
        "start.sh",
        'body = op.open("http://127.0.0.1:%s/api/health" % sys.argv[1], timeout=3).read(65536)',
        'body = op.open("http://localhost:%s/api/health" % sys.argv[1], timeout=3).read(65536)',
        "探针改打 localhost ⇒ 解析到 ::1 而服务只听 0.0.0.0 时够不着",
    ),
    (
        "start.sh",
        "service_ready && return 0\n    port_listening && return 0\n    return 1",
        "service_ready && return 0\n    return 1",
        "没有「端口在听就算起来了」这层兜底 ⇒ 接口一哑就误报失败",
    ),
    (
        "start.sh",
        "        if service_up; then\n            # ⚠️ **端口在听但健康接口不答应**",
        "        if service_ready; then\n            # ⚠️ **端口在听但健康接口不答应**",
        "启动后的成败判定又换回严格判据 ⇒ 端口在听也报「启动失败」",
    ),
]

#: 副本里必须带的文件
COPY_TOP = ("start.sh",)
COPY_TESTS = ("test_usb_startup_ready.py",)


def build_copy(dst, new_text):
    """把 start.sh(变异后的)+ 测试文件放进 dst。

    ⚠️ 被变异的那个文件**根本不复制进来**, 直接新建("x")⇒ 全程不覆写
    任何已存在的文件。
    """
    for fn in COPY_TOP:
        if fn == "start.sh":
            continue                     # 下面单独写
        s = os.path.join(REPO, fn)
        if os.path.isfile(s):
            shutil.copy2(s, os.path.join(dst, fn))
    os.makedirs(os.path.join(dst, "tests"), exist_ok=True)
    for fn in COPY_TESTS:
        shutil.copy2(os.path.join(REPO, "tests", fn),
                     os.path.join(dst, "tests", fn))
    with open(os.path.join(dst, "start.sh"), "x", encoding="utf-8") as f:
        f.write(new_text)


def run_one(work):
    env = dict(os.environ)
    env["PYTHONPATH"] = work
    proc = subprocess.run(
        [PY, "-m", "tests.test_usb_startup_ready"],
        cwd=work, env=env, capture_output=True, text=True, timeout=600,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def main():
    root = tempfile.mkdtemp(prefix="mut_ready_")
    print(f"副本都在 {root} 下面\n")

    bad = 0
    for i, (rel, frm, to, desc) in enumerate(MUTATIONS):
        srcp = os.path.join(REPO, rel)
        with open(srcp, encoding="utf-8") as f:
            orig = f.read()
        if orig.count(frm) != 1:
            print(f"\n✗ {desc}\n   → 锚点在 start.sh 里出现 {orig.count(frm)} 次"
                  f"(必须**恰好一次**, 否则证据不成立): {frm[:60]!r}")
            bad += 1
            continue

        work = os.path.join(root, f"m{i:02d}")
        os.makedirs(work, exist_ok=True)
        build_copy(work, orig.replace(frm, to))

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
