"""回归测试: `logs/server.log` 里长出「空洞」(NUL 字节)⇒ 启动时修好。

起因(**产线盘上真实存在**, 2026-11-11 实测):
    产线 U 盘 `/media/User/AGING1/logs/server.log` 里有一段 **956 个 NUL 字节**
    (21 KB 的空洞, 在文件前 69% 的位置)。

    ⇒ **grep 把整个文件当成二进制** ⇒ `grep 什么 logs/server.log`
       **只打一行「Binary file … matches」, 把真正的行全吞掉**。而部署作业手册
       **教工人去 grep 这个日志** ⇒ 工人拿到的是**静默的空**
       ⇒ 合理反应是「日志里没有 ⇒ 这条路不通」
       ⇒ **去追一个不存在的故障**(实际上有 84 条 `WebSocket /ws` 就在那儿,
          只是 grep 不肯说)。
    ⇒ 2026-11-11 那次**停产事故**的排查也被它拖了后腿。

修法: `start.sh` 的 `repair_log_holes()` —— 每次启动服务前, 把日志里的
      NUL 字节**换成换行**(内容一行不少)⇒ grep 恢复正常。
      **原件留一份**(`server.log.holes-<时间戳>`)⇒ **不删**。

这个测试守四件事:
  ① **动态**: 有空洞的日志 ⇒ 修完之后**没有 NUL 了**;
  ② **动态**: 修完之后**普通 grep(不带 -a)能出结果** —— 这才是真正要修的;
  ③ **动态**: **原件被留着**(不删)⇒ 出事能回头看;
  ④ **动态**: **没有空洞的日志不许被动**(内容逐字节不变, 也不多留副本)
     —— 免得每次启动都白折腾一遍、把日志改得面目全非。

跑法: .venv/bin/python -m tests.test_log_hole_repair
"""
import os
import re
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests._tmp import mkdtemp                      # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# ⚠️ 变异脚本会把**改坏的那一份**指过来(AGING_START_SH)
START_SH = os.environ.get("AGING_START_SH",
                           os.path.join(REPO, "start.sh"))

FAILS = []
PASSES = 0


def check(cond, msg, extra=""):
    global PASSES
    if cond:
        PASSES += 1
        print("  ✓ " + msg)
    else:
        FAILS.append(msg)
        print("  ✗ " + msg + ("  → " + str(extra) if extra else ""))


def section(title):
    print("\n" + title)


def extract_func(name):
    """从 start.sh 里原样抠出一个 shell 函数(连注释一起)。

    (跟 tests/test_usb_startup_ready.py 同一个套路 —— **测真代码**,
     不另抄一份, 免得抄的那份跟真代码分叉。)
    """
    text = open(START_SH, encoding="utf-8").read()
    m = re.search(rf"^{re.escape(name)}\(\) \{{\n.*?^\}}$", text, re.S | re.M)
    return m.group(0) if m else ""


def build_probe(tmp, logfile):
    """写一个只含 repair_log_holes() 的探针脚本。"""
    src = extract_func("repair_log_holes")
    if not src:
        return None
    sh = os.path.join(tmp, "probe.sh")
    with open(sh, "w", encoding="utf-8") as f:
        f.write("#!/bin/bash\n"
                "set -e\n"
                'LOG_FILE="%s"\n' % logfile +
                "log() { echo \"$*\"; }\n\n" + src + "\n\nrepair_log_holes\n")
    os.chmod(sh, 0o755)
    return sh


def run_probe(sh):
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": "/tmp"}
    p = subprocess.run(["/bin/bash", str(sh)], env=env,
                       capture_output=True, text=True, timeout=60)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def grep_count(path, pattern, use_a):
    """普通 grep(不给 -a)—— 这才是工人/脚本实际用的那一条。"""
    cmd = ["grep"] + (["-a"] if use_a else []) + ["-c", pattern, path]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    out = (p.stdout or "").strip()
    return out if out else "0"


def main():
    tmp = mkdtemp(prefix="aging-loghole-")
    logf = os.path.join(tmp, "server.log")

    section("① 有空洞的日志 ⇒ 修完之后没有 NUL 了")
    with open(logf, "wb") as f:
        f.write(b"INFO:     Started server process [1]\n")
        f.write(b"INFO:     WebSocket /ws accepted\n")
        f.write(b"\x00" * 956)            # ← 那段空洞
        f.write(b"INFO:     Finished server process [1]\n")
    before = open(logf, "rb").read()

    sh = build_probe(tmp, logf)
    if sh is None:
        check(False, "start.sh 里抠不出 repair_log_holes()", START_SH)
        return finish()
    code, out = run_probe(sh)
    check(code == 0, "探针跑起来了 (exit=%d)" % code, out[-300:])
    check("空洞" in out, "而且**报出来了**(让人知道日志被修过)", out[-300:])

    after = open(logf, "rb").read()
    check(b"\x00" not in after, "**没有 NUL 字节了**", after[:120])
    check(len(after) == len(before),
          "**字节数一个没少**(NUL 换成了换行, 不是把内容删了)",
          "%d → %d" % (len(before), len(after)))
    check(b"WebSocket /ws accepted" in after, "原来的内容还在")

    section("② 修完之后, 普通 grep(不带 -a)能出结果 ← 这才是要修的")
    n_plain = grep_count(logf, "WebSocket /ws", use_a=False)
    n_a = grep_count(logf, "WebSocket /ws", use_a=True)
    check(n_plain == "1",
          "**普通 grep 就能查到**(工人/脚本就是这么查的)", n_plain)
    check(n_a == "1", "带 -a 也一样(修了之后不需要 -a 了)", n_a)

    section("③ 原件被留着(不删)")
    keeps = [f for f in os.listdir(tmp) if ".holes-" in f]
    check(len(keeps) == 1, "留了 1 份原件", keeps)
    if keeps:
        kb = open(os.path.join(tmp, keeps[0]), "rb").read()
        check(kb == before, "原件**逐字节**等于修之前的(出事能回头看)",
              "%d vs %d" % (len(kb), len(before)))

    section("④ 没有空洞的日志不许被动")
    tmp2 = mkdtemp(prefix="aging-loggood-")
    logf2 = os.path.join(tmp2, "server.log")
    good = b"INFO:     Started server process [9]\nINFO:     clean line\n"
    with open(logf2, "wb") as f:
        f.write(good)
    sh2 = build_probe(tmp2, logf2)
    code2, out2 = run_probe(sh2)
    check(code2 == 0, "探针跑起来了 (exit=%d)" % code2, out2[-300:])
    check(open(logf2, "rb").read() == good,
          "**内容一字未改**", out2[-300:])
    keeps2 = [f for f in os.listdir(tmp2) if ".holes-" in f]
    check(not keeps2, "**没多留副本**(没洞就别折腾)", keeps2)
    check("空洞" not in out2, "也没瞎报(没洞就别吓人)", out2[-200:])

    section("⑤ 启动时真的调了它(函数写得再好, 没被调用等于没修)")
    text = open(START_SH, encoding="utf-8").read()
    m = re.search(r"^start_service\(\) \{.*?^\}", text, re.S | re.M)
    # ⚠️ **认「调用」不认「提到」** —— start_service 的注释里也写了
    #   `repair_log_holes()`(「见上面 repair_log_holes() 的说明」)⇒ 只
    #   find() 名字的话, **把调用删了都照样查得到** ⇒ 那个变异就溜过去了。
    #   ⇒ 只认**独占一行**的调用。
    body = m.group(0) if m else ""
    calls = list(re.finditer(r"^[ \t]*repair_log_holes[ \t]*$", body, re.M))
    check(m is not None and calls,
          "start_service 里**调用了 repair_log_holes**(启动前就修)",
          (body[:120] if body else "抠不出 start_service"))
    # ⚠️ 调用点必须在**起服务之前** ⇒ 顺序也不许错
    if m and calls:
        i_call = calls[0].start()
        # ⚠️ **锚点用 `setsid nohup`, 别用 "uvicorn"** —— start_service 的
        #   注释里也提到 uvicorn(「不能直接调 venv/bin/uvicorn」)⇒ 拿
        #   "uvicorn" 找位置会找到**注释里那处** ⇒ 顺序判断永远错。
        i_launch = body.find("setsid nohup")
        check(i_launch != -1 and i_call < i_launch,
              "而且**在起服务之前**调用(顺序不能反)",
              "call@%d launch@%d" % (i_call, i_launch))

    return finish()


def finish():
    print("\n" + "─" * 60)
    if FAILS:
        print("✗ %d 项失败 / 共 %d 项" % (len(FAILS), PASSES + len(FAILS)))
        for f in FAILS:
            print("   · " + f)
        sys.exit(1)
    print("✓ 全部通过 (%d 项)" % PASSES)


if __name__ == "__main__":
    main()
