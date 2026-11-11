"""回归测试: 时钟旁挂文件**被写坏**时, 报告上那条横幅不许消失 (§9.3.4)。

起因 (**产线盘上真实存在**, 2026-11-11 实测):
    产线 U 盘 `/media/User/AGING1/logs/` 下那批旁挂文件**会被写坏**:
      · `clock_skew_run20.txt` —— 258 字节**整个是乱码**
        (开头 4 个字节 `ff ff ff ff`, 后面全是随机二进制);
      · `boot_evidence_run21.txt` —— 头部一段乱码, 尾巴还是好的;
      · `cpu_evidence_run20.txt` —— 尾巴 173 个 NUL 字节。
    同一批盘上 `logs/server.log` 里还整段盖了 4096 字节的随机数据
    (ext4 块对齐) ⇒ **不是 NUL 单独的事, 是整个文件被写坏**。

病根 (在 `server/clock.py:read_run_skew`):
    旧代码 `run_skew_path(run_id).read_text(encoding="utf-8")`
    ⇒ 遇到一个坏字节就抛 `UnicodeDecodeError`
    ⇒ 旧代码 `except Exception: return ""`
    ⇒ **整份旁挂文件静默消失**
    ⇒ 报告上「⚠ 本报告的时间戳是推算值, 不是系统时钟的真实时间」
       **那条横幅不见了**
    ⇒ 工人看报告以为时间是准的 ⇒ **拿推算值当准点用**
       (板子硬复位过, 时钟实测倒跳 42 天)。

    ⇒ 兄弟模块 `boot_evidence._read_text_lenient` **早就是容错的**
       (`raw.decode("utf-8", errors="replace")`) ⇒ **两个口径不一致**
       才是真正扎眼的地方: 证据文件坏一点还能读, 横幅文件坏一点就整条没。

修法: 读字节 + `errors="replace"` ⇒ 坏的那几个字节变成 `�`,
      **前面的正经内容照样读得到**; 整个文件都坏 ⇒ 至少还知道
      「这一轮的时间戳不可信」(横幅还在, 详情是乱码)。

这个测试守四件事:
  ① **动态**: 文件中间夹坏字节 ⇒ 读回来**不抛异常**, 而且**前面的正经
     内容还在**(不是整份丢掉);
  ② **动态**: 整份都是乱码(产线实测那种)⇒ 读回来**非空**
     ⇒ 横幅**还在** —— 这是要修的那条;
  ③ **动态**: 文件不存在 ⇒ 空串(不抛异常, 正常轮次**不该**挂黄条);
  ④ **动态**: 报告默认按轮次自己读旁挂文件 ⇒ 坏文件也**照样打横幅**。

跑法: .venv/bin/python -m tests.test_clock_skew_lenient
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import clock as clock_mod                  # noqa: E402
from server import report as report_mod                # noqa: E402
from tests._tmp import mkdtemp                         # noqa: E402

FAILS = []
PASSES = 0

# ⚠️ 数据目录换成临时目录 —— 绝不能把旁挂文件写进仓库, 更不能碰真数据
DATA_DIR = mkdtemp(prefix="aging-clockskew-")
os.environ["AGING_DATA_DIR"] = DATA_DIR

RUN_ID = 4242
SKEW_REL = os.path.join("logs", f"clock_skew_run{RUN_ID}.txt")

# 正常旁挂文件长什么样(跟 clock.write_run_skew 写的一模一样)
GOOD_TEXT = ("推算值 —— 本轮报告上的时间戳不是系统时钟的真实值\n"
             "系统时钟比实际时间慢了 3628800 秒\n"
             "推算基准: 2026-09-29 12:00:00(关机前最后一次记录)\n"
             "※ 归档顺序可信(时间戳单调不减), 但**不能当准点用**。\n")

# 产线实测到的两种坏法
# ⚠️ GARBAGE_ALL 里**一个合法 UTF-8 字节都没有** —— 这样「容错解码」
#    (坏字节变 �) 和「直接忽略坏字节」(errors="ignore") 才有区别:
#    前者读回一串替换符(非空 ⇒ 横幅还在), 后者读回空串(横幅消失)。
GARBAGE_ALL = b"\xff\xff\xff\xff" + b"\x80\x81\x82" * 80 + b"\xfe\xfd"
GARBAGE_MID = b"\x80\x81\xff\xfe"


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


def write_sidecar(raw: bytes):
    p = clock_mod.run_skew_path(RUN_ID)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "wb") as f:
        f.write(raw)
    return p


def read_skew(run_id):
    """读旁挂文件 —— **不许把异常漏到外面**(崩了也算失败, 但要说清楚)。"""
    try:
        return clock_mod.read_run_skew(run_id)
    except Exception as e:                       # noqa: BLE001
        return "<<抛异常了: %s: %s>>" % (type(e).__name__, e)


def make_run():
    return {
        "id": RUN_ID,
        "sn": "SN-SKEW-0001",
        "operator": "产线甲",
        "status": "stopped",
        "started_at": 1000,
        "finished_at": None,
        "summary": {"total": 3, "passed": 2, "failed": 0, "error": 0,
                    "skipped": 0, "not_run": 1, "planned": 4},
        "config": {"tests": {}},
        "error": "服务重启, 这一轮中途夭折(没跑完)",
    }


def main():
    section("① 中间夹了坏字节 ⇒ 不抛异常, 前面的正经内容还在")
    write_sidecar(GOOD_TEXT.encode("utf-8")[:40] + GARBAGE_MID
                  + GOOD_TEXT.encode("utf-8")[40:])
    txt = read_skew(RUN_ID)
    check("<<抛异常了" not in txt, "**没有抛异常**", txt[:120])
    check("推算值" in txt, "坏字节**前面**的正经内容读得到", txt[:120])
    check("不能当准点用" in txt, "坏字节**后面**的内容也读得到(整份都在)",
          txt[-120:])
    check(len(txt) > len(GOOD_TEXT) - 40,
          "内容长度对得上(只多了坏字节占位符)", len(txt))

    section("② 整份都是乱码(产线实测那种)⇒ 横幅不许消失")
    write_sidecar(GARBAGE_ALL)
    txt = read_skew(RUN_ID)
    check("<<抛异常了" not in txt, "**没有抛异常**", txt[:120])
    check(txt != "", "读回来**不是空串**(旧代码在这里就返回空串了)")
    check(txt.strip() != "", "而且 strip 之后也不是空的(横幅靠它判断)")
    check("�" in txt or "\x00" not in txt,
          "坏字节变成了替换符(而不是把整份丢掉)")

    section("③ 文件不存在 ⇒ 空串, 不抛异常(正常轮次不该挂黄条)")
    missing = clock_mod.run_skew_path(RUN_ID + 1)
    if missing.exists():
        missing.unlink()
    txt_missing = read_skew(RUN_ID + 1)
    check("<<抛异常了" not in txt_missing, "**没有抛异常**", txt_missing[:120])
    check(txt_missing == "", "读回来是空串", repr(txt_missing))

    section("③' 文件在但内容是空的 ⇒ 横幅也不许消失")
    # ⚠️ 产线盘上实测到 **3 个 0 字节**的旁挂文件(run19/36/38) ——
    #   `write_run_skew()` 是 write_text() ⇒ 先建/截断再写 ⇒ 写到一半
    #   断电就留下一个空文件。**文件在 = 这一轮确实是推算值**
    #   (时钟正常时压根不写这个文件)⇒ 结论不该跟着内容一起丢。
    write_sidecar(b"")
    txt_empty = read_skew(RUN_ID)
    check("<<抛异常了" not in txt_empty, "**没有抛异常**", txt_empty[:120])
    check(txt_empty != "", "读回来**不是空串**(空文件也说明「这一轮是推算值」)")
    check("推算值" in txt_empty, "说清了这一轮是推算值", txt_empty[:120])
    html_empty = report_mod.render_html(make_run(), [], "x.pdf")
    check("本报告的时间戳是推算值" in html_empty,
          "**横幅还在** —— 空文件不许让「时间不可信」消失")
    # ⚠️ 只有「全是空白」才算空 —— 有内容就是有内容
    write_sidecar(b"   \n\t  \n")
    txt_ws = read_skew(RUN_ID)
    check("推算值" in txt_ws, "只有空白也算空(同样给结论)", txt_ws[:120])

    section("④ 报告上那条横幅还在(默认按轮次自己读旁挂文件)")
    # 坏文件
    write_sidecar(GARBAGE_ALL)
    html = report_mod.render_html(make_run(), [], "x.pdf")
    check("本报告的时间戳是推算值" in html,
          "**横幅还在** —— 坏文件不许让「时间不可信」这条结论消失")
    # 好文件
    write_sidecar(GOOD_TEXT.encode("utf-8"))
    html2 = report_mod.render_html(make_run(), [], "x.pdf")
    check("本报告的时间戳是推算值" in html2, "好文件当然也有横幅")
    check("不能当准点用" in html2, "而且详情是正经内容", html2[:0])
    # 没有文件 ⇒ 不打横幅(别给正常轮次乱挂黄条)
    if missing.exists():
        missing.unlink()
    html3 = report_mod.render_html(
        dict(make_run(), id=RUN_ID + 1), [], "x.pdf")
    check("本报告的时间戳是推算值" not in html3,
          "没有旁挂文件 ⇒ **不打横幅**")

    section("⑤ 正常文件读回来跟写进去的一致(别把好文件也改坏)")
    write_sidecar(GOOD_TEXT.encode("utf-8"))
    txt = read_skew(RUN_ID)
    check(txt == GOOD_TEXT.strip(), "一字不差", repr(txt[:120]))

    print("\n" + "─" * 60)
    if FAILS:
        print("✗ %d 项失败 / 共 %d 项" % (len(FAILS), PASSES + len(FAILS)))
        for f in FAILS:
            print("   · " + f)
        sys.exit(1)
    print("✓ 全部通过 (%d 项)" % PASSES)


if __name__ == "__main__":
    main()
