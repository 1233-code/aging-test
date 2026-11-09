# 变异测试: 故意把「老化测试用例表」改坏, 看 tests/test_case_table_bt.py 会不会红。
#
# 为什么要有这么个东西: 「测试全绿」本身说明不了什么 —— 断言写错了、
# 写松了、或者压根没跑到, 都是绿的。唯一能证明这测试**咬得动**的办法,
# 就是亲手把表改坏, 看它是不是真的红。
#
# 这一份守的是 2026-11-04 用户提的: 「在老化应用测试用例表中也加上蓝牙测试的
# 测试用例项」。那张表是**产线照着执行**的东西(xlsx, 二进制), 坏了没人发现:
#   ① 蓝牙用例行不见了 / 判定方向写反了(「蓝牙在工作」写成 passed —— 那是
#      **反的判定**, 写反了等于把不合格的设备判成合格);
#   ② 蓝牙用例挪错位置(跑完重载才测射频, 看的就不是最终状态了);
#   ③ 总览里的数字跟表里实际的行数对不上(表自己跟自己打架);
#   ④ 工作簿自己的三处范围(dimension / autoFilter / _FilterDatabase)没跟着走。
#
# 做法: **每个变异单开一个全新目录**, 把 docs/ + tests/ 复制进去(server/ 用
# 软链), 在**副本**上做一处改动, 在副本目录里跑测试。被改的那个 xlsx
# **从头到尾只写一次**(直接写变异后的内容), 别的文件原样复制 —— 所以整个
# 过程不覆写任何已存在的文件, 跑完也不用还原。
#
# ⚠️ 只在**副本**上改 —— 仓库里 docs/ 是产线在用的那份表, 改坏了没人知道。
#
# 跑法: .venv/bin/python tests/_mutate_case_table.py
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_VENV_PY = os.path.join(REPO, ".venv", "bin", "python")
# 仓库里自带 .venv 就用它; 没有(比如脚本被复制到别处跑)就用当前这个解释器
PY = _VENV_PY if os.path.isfile(_VENV_PY) else sys.executable
TEST = "tests.test_case_table_bt"
XLSX = os.path.join(REPO, "docs", "测试用例表.xlsx")

SHEET1 = "xl/worksheets/sheet1.xml"
SHEET2 = "xl/worksheets/sheet2.xml"
WORKBOOK = "xl/workbook.xml"


def read_parts(path):
    zf = zipfile.ZipFile(path, "r")
    parts = [(i.filename, zf.read(i.filename)) for i in zf.infolist()]
    zf.close()
    return parts


def write_zip(path, parts):
    """parts: [(名字, 内容)] —— 写一个**新**文件(不覆写任何已存在的东西)。"""
    zf = zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED)
    for name, data in parts:
        zf.writestr(name, data)
    zf.close()


def _rows(xml):
    return [(int(m.group(1)), m.group(0))
            for m in re.finditer(r'<row r="(\d+)"[^>]*>.*?</row>', xml, re.S)]


def _put(xml, num, new_block):
    """把第 num 行的整块换掉。"""
    pat = re.compile(r'<row r="%d"[^>]*>.*?</row>' % num, re.S)
    m = pat.search(xml)
    if not m:
        raise SystemExit("找不到第 %d 行" % num)
    return xml[:m.start()] + new_block + xml[m.end():]


def _set_cell(xml, num, col, new_cell):
    pat = re.compile(r'<c r="%s%d"(?:[^>/]|/(?!>))*(?:/>|>.*?</c>)' % (col, num), re.S)
    m = pat.search(xml)
    if not m:
        raise SystemExit("找不到 %s%d 这个单元" % (col, num))
    return xml[:m.start()] + new_cell + xml[m.end():]


def _text(s):
    """和表里一样的写法: 非 ASCII 一律十进制数字字符引用。"""
    out = []
    for ch in s:
        if ch == "&":
            out.append("&amp;")
        elif ch == "<":
            out.append("&lt;")
        elif ch == ">":
            out.append("&gt;")
        elif ord(ch) < 128:
            out.append(ch)
        else:
            out.append("&#%d;" % ord(ch))
    return "".join(out)


# ══════════════════════════════════════════════════════════════════════
# 变异: 每个都改一处, 改完这份表应当是**不合格**的
# ══════════════════════════════════════════════════════════════════════
def m_delete_bt05(parts):
    """删掉 BT-05(「蓝牙在工作 ⇒ failed」那一条)。"""
    out = []
    for name, data in parts:
        if name == SHEET1:
            xml = data.decode("utf-8")
            pat = re.compile(r'<row r="\d+"[^>]*>.*?</row>', re.S)
            for m in pat.finditer(xml):
                if ">BT-05<" in m.group(0):
                    xml = xml[:m.start()] + xml[m.end():]
                    break
            data = xml.encode("utf-8")
        out.append((name, data))
    return out


def m_flip_bt05_verdict(parts):
    """把 BT-05 的判定从 failed 改成 passed —— 判定方向写反了。"""
    out = []
    for name, data in parts:
        if name == SHEET1:
            xml = data.decode("utf-8")
            row = next(b for n, b in _rows(xml) if ">BT-05<" in b)
            num = re.search(r'<row r="(\d+)"', row).group(1)
            new = _set_cell(xml, int(num), "G",
                            '<c r="G%s" s="4" t="inlineStr"><is><t>passed</t></is></c>' % num)
            data = new.encode("utf-8")
        out.append((name, data))
    return out


def m_wrong_chapter(parts):
    """把 BT-01 的章节写成 §6.4.2 —— 表跟文档/代码对不上。"""
    out = []
    for name, data in parts:
        if name == SHEET1:
            xml = data.decode("utf-8")
            row = next(b for n, b in _rows(xml) if ">BT-01<" in b)
            num = re.search(r'<row r="(\d+)"', row).group(1)
            new = _set_cell(xml, int(num), "D",
                            '<c r="D%s" s="2" t="inlineStr"><is><t>%s</t></is></c>'
                            % (num, _text("§6.4.2")))
            data = new.encode("utf-8")
        out.append((name, data))
    return out


def m_move_bt_after_usb(parts):
    """把 14 条蓝牙用例挪到 USB-01 后面 —— 位置错了。"""
    out = []
    for name, data in parts:
        if name == SHEET1:
            xml = data.decode("utf-8")
            blocks = _rows(xml)
            bt = [b for n, b in blocks if ">BT-0" in b or ">BT-1" in b]
            keep = [(n, b) for n, b in blocks if b not in bt]
            # 插到 USB-01 那一行的后面
            idx = next(i for i, (n, b) in enumerate(keep) if ">USB-01<" in b)
            merged = keep[:idx + 1] + [(0, b) for b in bt] + keep[idx + 1:]
            body = "".join(b for _, b in merged)
            head = xml.split("<sheetData>", 1)[0]
            tail = xml.split("</sheetData>", 1)[1]
            data = (head + "<sheetData>" + body + "</sheetData>" + tail).encode("utf-8")
        out.append((name, data))
    return out


def m_break_total(parts):
    """总览的「合计」数字改成错的(133 → 134)。"""
    out = []
    for name, data in parts:
        if name == SHEET2:
            xml = data.decode("utf-8")
            row = next(b for n, b in _rows(xml)
                       if "<t>%s</t>" % _text("合计") in b)
            num = re.search(r'<row r="(\d+)"', row).group(1)
            new = _set_cell(xml, int(num), "B",
                            '<c r="B%s" s="15" t="n"><v>134</v></c>' % num)
            data = new.encode("utf-8")
        out.append((name, data))
    return out


def m_break_item_count(parts):
    """总览里「按测试项统计」的蓝牙条数写成 13(实际 14)。"""
    out = []
    for name, data in parts:
        if name == SHEET2:
            xml = data.decode("utf-8")
            # 按**内容**找蓝牙那一行(不写死行号 —— 行号一变就找不到了)
            row = next(b for n, b in _rows(xml)
                       if _text("蓝牙 (应关闭)") in b)
            num = re.search(r'<row r="(\d+)"', row).group(1)
            new = _set_cell(xml, int(num), "C",
                            '<c r="C%s" s="2" t="n"><v>13</v></c>' % num)
            data = new.encode("utf-8")
        out.append((name, data))
    return out


def m_break_distribution(parts):
    """总览的判定分布里 passed 写成 42(实际 43)—— 分布自己加不起来。"""
    out = []
    for name, data in parts:
        if name == SHEET2:
            xml = data.decode("utf-8")
            row = next(b for n, b in _rows(xml)
                       if "<t>passed</t>" in b and "<v>" in b)
            num = re.search(r'<row r="(\d+)"', row).group(1)
            new = _set_cell(xml, int(num), "B",
                            '<c r="B%s" s="2" t="n"><v>42</v></c>' % num)
            data = new.encode("utf-8")
        out.append((name, data))
    return out


def m_break_autofilter(parts):
    """autoFilter 的范围没跟着往后挪(还是 A1:H121)。"""
    out = []
    for name, data in parts:
        if name == SHEET1:
            data = data.replace(b'<autoFilter ref="A1:H135"/>',
                                b'<autoFilter ref="A1:H121"/>')
        out.append((name, data))
    return out


def m_break_filterdatabase(parts):
    """workbook.xml 里 _FilterDatabase 的范围没跟着往后挪。"""
    out = []
    for name, data in parts:
        if name == WORKBOOK:
            data = data.replace("'测试用例表'!$A$1:$H$135".encode("utf-8"),
                                 "'测试用例表'!$A$1:$H$121".encode("utf-8"))
        out.append((name, data))
    return out


MUTATIONS = [
    ("用例表: 删掉 BT-05(蓝牙在工作 ⇒ failed 那一条)", m_delete_bt05),
    ("用例表: BT-05 的判定被改成 passed(判定方向写反了)", m_flip_bt05_verdict),
    ("用例表: BT-01 的章节写成 §6.4.2(跟文档/代码对不上)", m_wrong_chapter),
    ("用例表: 14 条蓝牙用例被挪到 USB 后面(位置错了)", m_move_bt_after_usb),
    ("总览: 合计写成 134(≠ 单项+通用)", m_break_total),
    ("总览: 按测试项统计的蓝牙条数写成 13(≠ 表里实际的 14)", m_break_item_count),
    ("总览: 判定分布里 passed 写成 42(分布自己加不起来)", m_break_distribution),
    ("用例表: autoFilter 的范围没跟着往后挪", m_break_autofilter),
    ("用例表: _FilterDatabase 的范围没跟着往后挪", m_break_filterdatabase),
]


def build_copy(root, idx, mutate):
    """每个变异单开一个全新目录; 被改的 xlsx 从头到尾**只写一次**。"""
    d = os.path.join(root, "mut%02d" % idx)
    os.makedirs(os.path.join(d, "docs"))
    os.makedirs(os.path.join(d, "tests"))
    shutil.copy2(os.path.join(REPO, "docs", "需求设计.md"),
                 os.path.join(d, "docs", "需求设计.md"))
    shutil.copy2(os.path.join(REPO, "README.md"), os.path.join(d, "README.md"))
    shutil.copy2(os.path.join(REPO, "tests", "test_case_table_bt.py"),
                 os.path.join(d, "tests", "test_case_table_bt.py"))
    # server/ 只是被 import 一下, 软链过去就行 —— 复制一份太慢也没必要
    os.symlink(os.path.join(REPO, "server"), os.path.join(d, "server"))
    parts = mutate(read_parts(XLSX))
    write_zip(os.path.join(d, "docs", "测试用例表.xlsx"), parts)
    return d


def main():
    root = tempfile.mkdtemp(prefix="case_table_mut_")
    caught, missed = [], []
    for i, (name, mutate) in enumerate(MUTATIONS):
        d = build_copy(root, i, mutate)
        r = subprocess.run([PY, "-m", TEST], cwd=d, capture_output=True,
                           text=True, timeout=300)
        if r.returncode != 0:
            caught.append(name)
            print("  ✓ 被抓住: %s" % name)
            tail = [ln for ln in r.stdout.splitlines() if ln.startswith("    - ")]
            for ln in tail[:3]:
                print("      %s" % ln.strip())
        else:
            missed.append(name)
            print("  ✗ **没抓住**: %s  ← 测试太松了" % name)

    print("\n" + "=" * 60)
    print("变异 %d 条: 抓住 %d, 漏 %d" % (len(MUTATIONS), len(caught), len(missed)))
    for m in missed:
        print("    – 漏了: %s" % m)
    print("(副本都在 %s 下, 跑完不用清理)" % root)
    sys.exit(1 if missed else 0)


if __name__ == "__main__":
    main()
