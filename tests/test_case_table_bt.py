"""回归测试: **老化测试用例表**里的蓝牙用例 + 总览的计数对得上(2026-11-04)

**为什么要有这个测试**

    用户要的是「在老化应用测试用例表中也加上蓝牙测试的测试用例项」。
    `docs/测试用例表.xlsx` 是**产线照着执行的那张表** —— 它是个二进制 xlsx,
    没有测试守着的话, 这三件事会**悄悄发生**:

    ① **蓝牙用例行没了**(谁把文件换回旧版 / 有人只改代码不改表)—— 没人发现,
       产线照着表做, 少测一项;
    ② **加了用例但总览的计数没跟着改** ⇒ 表自己跟自己打架:
       「按判定结果分布」加起来 ≠ 「合计」, 「按测试项统计」的数字 ≠ 表里实际的行数;
    ③ **表里写的跟代码/文档对不上** —— 项名、章节号、指标名一旦漂了,
       工人照着表做、代码判的却是另一回事(蓝牙这一项判定还是**反的**,
       漂了更危险: 「蓝牙在工作」被写成 passed, 反而是「合格的设备」被判过)。

    ⇒ 这个测试把三件事钉死:
       **A. 蓝牙用例在不在、口径对不对**(判定是反的: 不工作才算合格)
       **B. 总览的每个数字 == sheet1 里实际的行数**(表自己跟自己算得清)
       **C. 工作簿自己的账对不对**(dimension / autoFilter / `_FilterDatabase`
          三处范围必须一致, 且等于最后一行)

**只读**: 只用 `zipfile` + `ElementTree` 读 xlsx, **一个字都不写**;
不碰真硬件, 不碰生产库(history.db / reports/pdf/ 一根手指没碰)。

跑法: .venv/bin/python -m tests.test_case_table_bt
"""
import os
import re
import sys
import zipfile
import xml.etree.ElementTree as ET
from collections import Counter, OrderedDict
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

XLSX = _REPO / "docs" / "测试用例表.xlsx"
NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
RID = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"

BT_ITEM = "蓝牙 (应关闭)"          # 与 server/tests/bluetooth.py 的 display_name 一致
BT_CHAPTER = "§6.4.1"

FAILS = []
PASSES = 0


def check(cond, msg, extra=""):
    global PASSES
    if cond:
        PASSES += 1
        print("    ✓ " + msg)
    else:
        FAILS.append(msg)
        print("    ✗ " + msg + ("  → " + str(extra) if extra else ""))


def section(title):
    print("\n" + title)


# ══════════════════════════════════════════════════════════════════════
# 读 xlsx —— 这份工作簿没有 sharedStrings.xml, 字符串全是 inlineStr
# ══════════════════════════════════════════════════════════════════════
def _sheet_parts(zf):
    """工作表名 → zip 里的 part 路径。"""
    wb = ET.fromstring(zf.read("xl/workbook.xml"))
    rels = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
    rmap = {r.get("Id"): r.get("Target") for r in rels}
    out = OrderedDict()
    for s in wb.find(NS + "sheets"):
        t = rmap[s.get(RID)]
        if t.startswith("/"):
            t = t[1:]
        elif not t.startswith("xl/"):
            t = "xl/" + t
        out[s.get("name")] = t
    return out


def _rows(zf, part):
    """[(行号, {列: (值, 样式)})] —— 值是字符串; 数字单元格原样取 <v>。"""
    sh = ET.fromstring(zf.read(part))
    out = []
    for row in sh.find(NS + "sheetData"):
        cells = {}
        for c in row:
            if c.get("t") == "inlineStr":
                val = "".join(x.text or "" for x in c.iter(NS + "t"))
            else:
                ve = c.find(NS + "v")
                val = ve.text if ve is not None else ""
            col = re.match(r"[A-Z]+", c.get("r")).group(0)
            cells[col] = (val, c.get("s"))
        out.append((int(row.get("r")), cells))
    return out


def load():
    zf = zipfile.ZipFile(XLSX, "r")
    parts = _sheet_parts(zf)
    data = {name: _rows(zf, p) for name, p in parts.items()}
    wb_xml = zf.read("xl/workbook.xml").decode("utf-8")
    dim = {}
    for name, p in parts.items():
        raw = zf.read(p).decode("utf-8")
        m = re.search(r'<dimension ref="([^"]+)"', raw)
        dim[name] = m.group(1) if m else ""
        m = re.search(r'<autoFilter ref="([^"]+)"', raw)
        dim[name + ":autofilter"] = m.group(1) if m else ""
    zf.close()
    return data, dim, wb_xml


def val(cells, col):
    return cells.get(col, ("", None))[0]


def as_int(text):
    try:
        return int(str(text).strip())
    except (TypeError, ValueError):
        return None


# ══════════════════════════════════════════════════════════════════════
section("【0】工作簿读得开吗")
# ══════════════════════════════════════════════════════════════════════
check(XLSX.is_file(), "用例表在 docs/测试用例表.xlsx", XLSX)
if not XLSX.is_file():
    print("\n✗ 没有用例表, 后面的检查跑不了")
    sys.exit(1)

DATA, DIM, WB_XML = load()
check("测试用例表" in DATA and "总览" in DATA,
      "三个 sheet 都在", list(DATA))
S1 = DATA["测试用例表"]
S2 = DATA["总览"]
S1_TEXT = "\n".join("|".join(v for v, _ in c.values()) for _, c in S1)


# ══════════════════════════════════════════════════════════════════════
section("【1】蓝牙用例行 —— 在不在、在哪儿、长什么样")
# ══════════════════════════════════════════════════════════════════════
bt_rows = [(n, c) for n, c in S1 if val(c, "A").startswith("BT-")]
check(len(bt_rows) >= 10, "蓝牙用例行都在(≥10 条)", len(bt_rows))

if bt_rows:
    ids = [val(c, "A") for _, c in bt_rows]
    expect_ids = ["BT-%02d" % i for i in range(1, len(bt_rows) + 1)]
    check(ids == expect_ids, "编号连续: BT-01 … BT-%02d" % len(bt_rows), ids)

    bad_group = [(val(c, "A"), val(c, "B")) for _, c in bt_rows if val(c, "B") != "单项"]
    check(not bad_group, "分组列都是「单项」", bad_group)
    bad_item = [(val(c, "A"), val(c, "C")) for _, c in bt_rows if val(c, "C") != BT_ITEM]
    check(not bad_item, "测试项列都是「%s」" % BT_ITEM, bad_item)
    bad_chap = [(val(c, "A"), val(c, "D")) for _, c in bt_rows if val(c, "D") != BT_CHAPTER]
    check(not bad_chap, "章节列都是「%s」" % BT_CHAPTER, bad_chap)

    # 位置: 紧跟在 WiFi 那一段的后面(§6.4), USB(§6.5)前面
    wf_last = max((n for n, c in S1 if val(c, "D") == "§6.4" and val(c, "B") == "单项"),
                  default=None)
    usb_first = min((n for n, c in S1 if val(c, "D") == "§6.5"), default=None)
    bt_nos = [n for n, _ in bt_rows]
    check(wf_last is not None and bt_nos[0] == wf_last + 1,
          "紧跟在 WiFi(§6.4)那一段的后面", "WF 末行=%s, BT 首行=%s" % (wf_last, bt_nos[0]))
    check(usb_first is not None and bt_nos[-1] == usb_first - 1,
          "排在 USB(§6.5)前面", "BT 末行=%s, USB 首行=%s" % (bt_nos[-1], usb_first))
    check(bt_nos == list(range(bt_nos[0], bt_nos[0] + len(bt_nos))),
          "蓝牙用例是**连着**的一段(中间没插别的东西)", bt_nos)
else:
    print("    – 表里一条蓝牙用例都没有, 后面跟蓝牙有关的检查跳过")


# ══════════════════════════════════════════════════════════════════════
section("【2】判定口径 —— 这一项的判定是**反的**: 不工作才算合格")
# ══════════════════════════════════════════════════════════════════════
# 「在工作」⇒ failed; 探测不到 ⇒ error; 「不工作」⇒ passed
for n, c in bt_rows:
    cid = val(c, "A")
    scen, verdict = val(c, "E"), val(c, "G")
    if "在工作" in scen:
        check(verdict == "failed",
              "%s 「蓝牙在工作」⇒ failed(判定是反的)" % cid, verdict)
    if "读不到 /sys/class" in scen:
        check(verdict == "error",
              "%s 「读不到 /sys/class」⇒ error(没测成 ≠ 合格)" % cid, verdict)
    if any(k in scen for k in ("没有蓝牙硬件", "没有活动控制器",
                                "射频被屏蔽", "没有 type=bluetooth")):
        check(verdict == "passed",
              "%s 「不工作」⇒ passed" % cid, verdict)

failed_rows = [(n, c) for n, c in bt_rows if val(c, "G") == "failed"]
check(failed_rows, "有 failed 的用例(蓝牙在工作 = 不合格)", len(failed_rows))
check(any("对不上" in val(c, "H") for _, c in failed_rows),
      "failed 的用例要点名「跟本机规格对不上」",
      [val(c, "H") for _, c in failed_rows])

scen = [val(c, "E") for _, c in bt_rows]
note = [val(c, "H") for _, c in bt_rows]


def has(*keys):
    """场景/说明里出现了这些关键词就算数(任意一个)。"""
    blob = scen + note
    return any(all(k in s for k in keys) for s in blob)


check(has("一条命令都不发"), "有「只读探测: 一条命令都不发」的约束用例")
check(has("不采用", "beacon"), "有「不采用 能不能连上 / 有没有 beacon」的约束用例")
check(has("已关闭(符合本机规格)"), "有「合格文案 = 已关闭(符合本机规格)」的约束用例")
check(has("绝不", "蓝牙: 通过"), "有一条明确写「绝不写『蓝牙: 通过』」")
check(has("不写 result.error"), "有「合格项不写 result.error」的约束用例")
check(has("位置", "WiFi"), "有「位置: 紧跟 WiFi 之后」的约束用例")
check(has("bt_present", "bt_detail"), "有「指标齐全」的用例")

metric_row = [c for _, c in bt_rows if "bt_present" in val(c, "E")]
check(metric_row, "指标齐全那一条把 8 个 bt_* 指标都列出来了")
if metric_row:
    text = val(metric_row[0], "E")
    missing = [m for m in ("bt_present", "bt_rf_state", "bt_working",
                           "bt_hci_devices", "bt_rf_name",
                           "bt_service_masked", "bt_verdict", "bt_detail")
               if m not in text]
    check(not missing, "8 个 bt_* 指标一个都不少", missing)


# ══════════════════════════════════════════════════════════════════════
section("【3】总览的数字 —— 跟 sheet1 里实际的行数**对得上**")
# ══════════════════════════════════════════════════════════════════════
ov = {n: c for n, c in S2}


def ov_val(row, col):
    return val(ov[row], col)


# 总览分三节 —— 按小节标题切开, 别把「一、」里的数字当「三、」里的判定
sec_head = {}
for n, c in ov.items():
    a = val(c, "A")
    for k, tag in (("一、", "cat"), ("二、", "item"), ("三、", "verdict")):
        if a.startswith(k):
            sec_head[tag] = n
check(len(sec_head) == 3, "总览的三节都在", sec_head)


def section_rows(tag):
    """某一节里的行(不含标题行本身, 按行号排序)。"""
    start = sec_head.get(tag)
    if start is None:
        return []
    later = [n for t, n in sec_head.items() if t != tag and n > start]
    end = min(later) if later else 10 ** 9
    return [(n, ov[n]) for n in sorted(ov) if start < n < end]


# ① 按分类统计
cat_rows = section_rows("cat")
n_item = sum(1 for _, c in S1 if val(c, "B") == "单项")
n_gen = sum(1 for _, c in S1 if val(c, "B") == "通用")
n_all = n_item + n_gen
row_item = [c for _, c in cat_rows if val(c, "A").startswith("单项")]
row_gen = [c for _, c in cat_rows if val(c, "A").startswith("跨项")]
row_sum = [c for _, c in cat_rows if val(c, "A") == "合计"]
check(row_item and as_int(val(row_item[0], "B")) == n_item,
      "「单项」条数 == sheet1 里分组=单项的行数(%d)" % n_item,
      val(row_item[0], "B") if row_item else None)
check(row_gen and as_int(val(row_gen[0], "B")) == n_gen,
      "「通用」条数 == sheet1 里分组=通用的行数(%d)" % n_gen,
      val(row_gen[0], "B") if row_gen else None)
check(row_sum and as_int(val(row_sum[0], "B")) == n_all,
      "「合计」== 单项 + 通用(%d)" % n_all,
      val(row_sum[0], "B") if row_sum else None)

# ② 按测试项统计: 每一行的用例数 == 该测试项在 sheet1 里的实际行数
#    ⚠️ 键用**章节号**而不是显示名 —— 总览里写的是短名(「USB 读写」),
#    sheet1 的测试项列写的是全名(「USB 枚举 + 读写」)⇒ 拿名字对会对不上;
#    章节号(§6.2 / §6.4.1 …)才是两边的真键。
per_item_sheet = Counter(val(c, "D") for _, c in S1 if val(c, "B") == "单项")
per_item_ov = {}
per_item_name = {}
for _, c in section_rows("item"):
    if val(c, "A") == "小计":
        continue
    k = as_int(val(c, "C"))
    if k is not None:
        per_item_ov[val(c, "B")] = k
        per_item_name[val(c, "B")] = val(c, "A")
for chap, cnt in per_item_sheet.items():
    check(per_item_ov.get(chap) == cnt,
          "「按测试项统计」%s = %d 条" % (chap, cnt), per_item_ov.get(chap))
check(per_item_ov.get(BT_CHAPTER) == len(bt_rows),
      "「按测试项统计」里 §6.4.1 蓝牙 = %d 条" % len(bt_rows),
      per_item_ov.get(BT_CHAPTER))
check(per_item_name.get(BT_CHAPTER) == BT_ITEM,
      "总览里蓝牙那一行的名字 == 「%s」" % BT_ITEM,
      per_item_name.get(BT_CHAPTER))
check(sum(per_item_ov.values()) == n_item,
      "按测试项统计加起来 == 单项条数(%d)" % n_item, sum(per_item_ov.values()))
row_sub = [c for _, c in section_rows("item") if val(c, "A") == "小计"]
check(row_sub and as_int(val(row_sub[0], "C")) == n_item,
      "「小计」== 单项条数(%d)" % n_item,
      val(row_sub[0], "C") if row_sub else None)

# ③ 按判定结果分布: 每一行的条数 == sheet1 里该判定的实际行数, 且加起来 == 合计
verdict_sheet = Counter(val(c, "G") for _, c in S1
                         if val(c, "B") in ("单项", "通用"))
dist = {}
for _, c in section_rows("verdict"):
    if val(c, "A") == "合计":
        continue
    k = as_int(val(c, "B"))
    if k is not None:
        dist[val(c, "A")] = k
for v, cnt in verdict_sheet.items():
    check(dist.get(v) == cnt, "「按判定结果分布」%s = %d 条" % (v, cnt), dist.get(v))
check(sum(dist.values()) == n_all,
      "判定分布加起来 == 合计(%d)" % n_all, sum(dist.values()))
row_vsum = [c for _, c in section_rows("verdict") if val(c, "A") == "合计"]
check(row_vsum and as_int(val(row_vsum[0], "B")) == n_all,
      "判定分布那一节的「合计」== %d" % n_all,
      val(row_vsum[0], "B") if row_vsum else None)

# ④ 「N 个测试项」得跟表里实际的测试项个数一样
m = re.search(r"(\d+)\s*个测试项", ov_val(6, "A"))
check(m is not None and int(m.group(1)) == len(per_item_sheet),
      "「N 个测试项」== 表里实际的测试项个数(%d)" % len(per_item_sheet),
      ov_val(6, "A"))


# ══════════════════════════════════════════════════════════════════════
section("【4】工作簿自己的账 —— dimension / autoFilter / _FilterDatabase")
# ══════════════════════════════════════════════════════════════════════
last = max(n for n, _ in S1)
rng = "A1:H%d" % last
check([n for n, _ in S1] == list(range(1, last + 1)),
      "sheet1 的行号连续(1..%d)且**按文档顺序**排" % last,
      [n for n, _ in S1][:5])
check([n for n, _ in S2] == sorted(n for n, _ in S2),
      "总览的行号也是按文档顺序排的(插行时别把顺序搞乱)")
check(DIM["测试用例表"] == rng, "dimension == %s" % rng, DIM["测试用例表"])
check(DIM["测试用例表:autofilter"] == rng,
      "autoFilter == %s" % rng, DIM["测试用例表:autofilter"])
check("'测试用例表'!$A$%d:$H$%d" % (1, last) in WB_XML.replace("$A$1", "$A$1"),
      "_FilterDatabase 的范围 == %s" % rng,
      re.search(r"<definedName.*?</definedName>", WB_XML).group(0))
# sheet2 的 dimension 也要跟着行数走
last2 = max(n for n, _ in S2)
check(DIM["总览"] == "A1:D%d" % last2,
      "总览的 dimension == A1:D%d" % last2, DIM["总览"])


# ══════════════════════════════════════════════════════════════════════
section("【5】表 ↔ 代码 ↔ 文档 —— 三边得说同一件事")
# ══════════════════════════════════════════════════════════════════════
try:
    from server.tests import bluetooth as B
    check(B.BluetoothTest.display_name == BT_ITEM,
          "表里的测试项名 == 代码里的 display_name(%s)" % BT_ITEM,
          B.BluetoothTest.display_name)
    code_metrics = set(re.findall(r'metrics\["(bt_[a-z_]+)"\]',
                                   Path(B.__file__).read_text(encoding="utf-8")))
    missing = [m for m in sorted(code_metrics) if m not in S1_TEXT]
    check(not missing, "代码里 set 的每个 bt_* 指标, 表里都提到了", missing)
    check(B.VERDICT_OFF in S1_TEXT,
          "合格文案「%s」表里也有" % B.VERDICT_OFF)
except Exception as e:                       # noqa: BLE001
    check(False, "读得到 server/tests/bluetooth.py", e)

doc = (_REPO / "docs" / "需求设计.md").read_text(encoding="utf-8")
check("6.4.1 蓝牙" in doc, "需求设计.md 里有 §6.4.1 蓝牙这一节")
check("已关闭(符合本机规格)" in doc, "需求设计.md 里也写了合格文案")
check("BT-01" in doc, "需求设计.md 里提到了用例编号 BT-01(表跟文档对得上)")

readme = (_REPO / "README.md").read_text(encoding="utf-8")
m = re.search(r"##\s*📋\s*测试项\s*\((\d+)\s*项\)", readme)
check(m is not None and int(m.group(1)) == len(per_item_sheet),
      "README 里写的测试项个数 == 表里实际的测试项个数(%d)" % len(per_item_sheet),
      m.group(1) if m else None)


# ══════════════════════════════════════════════════════════════════════
print("\n" + "=" * 60)
if FAILS:
    print("✗ %d/%d 项没过:" % (len(FAILS), PASSES + len(FAILS)))
    for f in FAILS:
        print("    - " + f)
    sys.exit(1)
print("✓ 全部通过 (%d 项)" % PASSES)
