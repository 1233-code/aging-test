"""从 chromium 打印的 PDF 里**逐页**提取文本 —— 排版回归测试要用。

为什么需要它: 「长表项有没有跳到下一页」「续页有没有重复表头」这类问题,
**只看 HTML 源码看不出来** —— 那是排版结果, 只有 PDF 落地之后才存在。
所以测试得能回答「这一页上有什么字」。

做法: 直接解析 PDF 的对象表, 按页对象拿到各自的内容流, 再按**每个字体
自己的** ToUnicode CMap 把字形码解回 Unicode。

⚠️ 为什么不能把所有字体的 CMap 合成一张表: chromium 给每个子集字体
各自编号字形, 同一个码位在字体 A 和字体 B 里是**不同的字**。合成一张表
会解出乱码。所以这里严格按 `/Fn` 切换字体时的那张表来解。

⚠️ 这不是通用 PDF 解析器 —— 只覆盖 chromium `--print-to-pdf` 的输出
(FlateDecode 的内容流 + Type0/TrueType 子集字体 + ToUnicode CMap)。
换别的工具生成 PDF, 这个模块可能解不出东西 —— 所以调用方要**核对解出来的
文本是不是空的**, 别把「解不出来」当成「页面上没这些字」。
"""
import re
import zlib
from pathlib import Path


def _objects(data: bytes) -> dict:
    """PDF 的间接对象表: {对象号: 对象体}。"""
    return {
        int(m.group(1)): m.group(2)
        for m in re.finditer(rb"(\d+)\s+0\s+obj\b(.*?)\bendobj", data, re.S)
    }


def _stream(body: bytes) -> bytes:
    """取对象体里的 stream 并解压。"""
    m = re.search(rb"stream\r?\n(.*?)\r?\nendstream", body, re.S)
    if not m:
        return b""
    try:
        return zlib.decompress(m.group(1))
    except zlib.error:
        return m.group(1)          # 没压缩的(字体程序之类), 原样给出去


def _dict_after(body: bytes, key: bytes) -> bytes:
    """取 body 里 key 后面那个**配平的** `<<...>>` 字典的内容 (嵌套也算对)。

    不用 `<<(.*?)>>` 这种非贪婪写法 —— chromium 的 /Resources 里嵌着
    /ProcSet、/ExtGState、/Font 三层字典, 非贪婪会在第一层就截断。
    """
    i = body.find(key)
    if i < 0:
        return b""
    j = body.find(b"<<", i)
    if j < 0:
        return b""
    depth, k = 0, j
    while k < len(body) - 1:
        if body[k:k + 2] == b"<<":
            depth += 1
            k += 2
            continue
        if body[k:k + 2] == b">>":
            depth -= 1
            k += 2
            if depth == 0:
                return body[j + 2:k - 2]
            continue
        k += 1
    return b""


def _cmap(obj: dict, font_body: bytes):
    """取一个字体的 ToUnicode 表: (码位 → 字符, 码位字节数)。"""
    m = re.search(rb"/ToUnicode\s+(\d+)\s+0\s+R", font_body)
    if not m:
        return {}, 2
    s = _stream(obj.get(int(m.group(1)), b""))
    mp = {}
    for blk in re.findall(rb"beginbfchar(.*?)endbfchar", s, re.S):
        for src, dst in re.findall(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", blk):
            mp[int(src, 16)] = "".join(
                chr(int(dst[i:i + 4], 16)) for i in range(0, len(dst), 4))
    for blk in re.findall(rb"beginbfrange(.*?)endbfrange", s, re.S):
        for lo, hi, dst in re.findall(
                rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", blk):
            lo_i, hi_i = int(lo, 16), int(hi, 16)
            base = int(dst[:4], 16)
            for i in range(min(hi_i - lo_i + 1, 4096)):
                mp[lo_i + i] = chr(base + i)
    nbytes = 2
    cm = re.search(rb"begincodespacerange(.*?)endcodespacerange", s, re.S)
    if cm:
        h = re.search(rb"<([0-9A-Fa-f]+)>", cm.group(1))
        if h:
            nbytes = max(1, len(h.group(1)) // 2)
    return mp, nbytes


def page_texts(path) -> list:
    """返回这份 PDF **每一页**的文本, 顺序跟页码一致。"""
    data = Path(path).read_bytes()
    obj = _objects(data)
    pages = []
    for _num, body in sorted(obj.items()):
        if b"/Type /Page" not in body or b"/Type /Pages" in body:
            continue
        rm = re.search(rb"/Resources\s+(\d+)\s+0\s+R", body)
        res = obj.get(int(rm.group(1)), b"") if rm else body
        fonts = {}
        for name, ref in re.findall(
                rb"/(\w+)\s+(\d+)\s+0\s+R", _dict_after(res, b"/Font")):
            fonts[name.decode()] = _cmap(obj, obj.get(int(ref), b""))
        cm = re.search(rb"/Contents\s+(\d+)\s+0\s+R", body)
        if not cm:
            pages.append("")
            continue
        s = _stream(obj.get(int(cm.group(1)), b""))
        cur, out = ({}, 2), []
        for tok in re.finditer(
                rb"/(\w+)\s+[\d.]+\s+Tf|<([0-9A-Fa-f]+)>\s*Tj"
                rb"|\(((?:[^()\\]|\\.)*)\)\s*Tj", s, re.S):
            if tok.group(1):
                cur = fonts.get(tok.group(1).decode(), ({}, 2))
            elif tok.group(2):
                h = tok.group(2)
                mp, nb = cur
                codes = [int(h[i:i + nb * 2], 16) for i in range(0, len(h), nb * 2)]
                out.append("".join(mp.get(c, "") for c in codes))
            else:
                out.append(tok.group(3).decode("latin-1"))
        pages.append("".join(out))
    return pages
