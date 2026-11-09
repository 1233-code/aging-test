#!/usr/bin/env python3
"""从 PDF 里抠文本 (Skia 生成的 PDF: /Type0 + /Identity-H ⇒ 2 字节码)。

用法: python3 pdftext.py <file.pdf> [更多文件...]
"""
import re
import sys
import zlib


def get_objects(data):
    objs = {}
    for m in re.finditer(rb'(?<![0-9])(\d+)\s+(\d+)\s+obj\b', data):
        num = int(m.group(1))
        start = m.end()
        end = data.find(b'endobj', start)
        if end < 0:
            end = len(data)
        objs[num] = data[start:end]
    return objs


def stream_bytes(body):
    m = re.search(rb'stream\r?\n', body)
    if not m:
        return None
    start = m.end()
    end = body.rfind(b'endstream')
    if end < 0:
        end = len(body)
    raw = body[start:end]
    head = body[:m.start()]
    if b'/FlateDecode' in head:
        try:
            raw = zlib.decompress(raw)
        except Exception:
            try:
                raw = zlib.decompressobj().decompress(raw)
            except Exception:
                return None
    return raw


def parse_cmap(text):
    """ToUnicode CMap → {code(int): str}。支持 bfchar 和 bfrange。"""
    mapping = {}

    def hex2str(h):
        h = h.decode('ascii').strip()
        try:
            b = bytes.fromhex(h)
        except ValueError:
            return ''
        try:
            return b.decode('utf-16-be')
        except UnicodeDecodeError:
            return b.decode('latin-1', 'replace')

    for blk in re.findall(rb'beginbfchar(.*?)endbfchar', text, re.S):
        for m in re.finditer(rb'<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]*)>', blk):
            src = int(m.group(1), 16)
            mapping[src] = hex2str(m.group(2))

    for blk in re.findall(rb'beginbfrange(.*?)endbfrange', text, re.S):
        # <lo> <hi> <dststart>
        for m in re.finditer(
                rb'<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>', blk):
            lo = int(m.group(1), 16)
            hi = int(m.group(2), 16)
            base = int(m.group(3), 16)
            width = len(m.group(3))
            for i in range(hi - lo + 1):
                v = base + i
                mapping[lo + i] = chr(v) if width <= 4 else hex2str(
                    ('%0*x' % (width, v)).encode())
        # <lo> <hi> [ <d1> <d2> ... ]
        for m in re.finditer(
                rb'<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*\[(.*?)\]', blk, re.S):
            lo = int(m.group(1), 16)
            items = re.findall(rb'<([0-9A-Fa-f]*)>', m.group(3))
            for i, it in enumerate(items):
                mapping[lo + i] = hex2str(it)
    return mapping


def font_map(objs, font_obj):
    """字体对象 → (码宽, {code: str})。"""
    body = objs.get(font_obj, b'')
    two_byte = b'/Type0' in body
    m = re.search(rb'/ToUnicode\s+(\d+)\s+\d+\s+R', body)
    mapping = {}
    if m:
        cm = stream_bytes(objs.get(int(m.group(1)), b''))
        if cm:
            mapping = parse_cmap(cm)
    return (2 if two_byte else 1), mapping


def page_fonts(objs, page_body):
    """页面里 /F1 等名字 → 字体对象号。"""
    out = {}
    res = re.search(rb'/Resources\s*(\d+)\s+\d+\s+R', page_body)
    res_body = page_body
    if res:
        res_body = objs.get(int(res.group(1)), b'')
    fm = re.search(rb'/Font\s*<<(.*?)>>', res_body, re.S)
    if not fm:
        rf = re.search(rb'/Font\s+(\d+)\s+\d+\s+R', res_body)
        if rf:
            fm = re.search(rb'<<(.*?)>>', objs.get(int(rf.group(1)), b''), re.S)
    if fm:
        for m in re.finditer(rb'/(\w+)\s+(\d+)\s+\d+\s+R', fm.group(1)):
            out[m.group(1).decode()] = int(m.group(2))
    return out


# ⚠️ 组号: 1=字体名 2=TJ数组 3=十六进制串 4,5=Td 的 x,y
#    (字面量字符串那条分支**不占组号**, 用 s.startswith(b"(") 判定)
TOKENS = re.compile(
    rb'/(\w+)\s+[-\d.]+\s+Tf|'
    rb'\[(.*?)\]\s*TJ|'
    rb'\((?:\\.|[^\\)])*\)\s*Tj|'
    rb'<([0-9A-Fa-f\s]*)>\s*Tj|'
    rb'([-\d.]+)\s+([-\d.]+)\s+(?:Td|TD)|'
    rb'T\*|ET|BT', re.S)


def decode_hex(h, width, mapping):
    h = re.sub(rb'\s', b'', h)
    out = []
    # ⚠️ **2 字节的码 = 4 个十六进制字符** —— 切 2 个就等于只取了高字节,
    #    查表全查不到(出来一片空白)。
    step = 4 if width == 2 else 2
    for i in range(0, len(h) - step + 1, step):
        code = int(h[i:i + step], 16)
        out.append(mapping.get(code, ''))
    return ''.join(out)


def extract(path):
    data = open(path, 'rb').read()
    objs = get_objects(data)
    lines = []
    for num, body in sorted(objs.items()):
        if b'/Type' not in body or b'/Page' not in body or b'/Pages' in body:
            continue
        fonts = page_fonts(objs, body)
        cm = re.search(rb'/Contents\s+(\d+)\s+\d+\s+R', body)
        if not cm:
            continue
        content = stream_bytes(objs.get(int(cm.group(1)), b''))
        if not content:
            continue
        cur = (1, {})
        curline = []
        cury = None
        for m in TOKENS.finditer(content):
            if m.group(1):
                fo = fonts.get(m.group(1).decode())
                cur = font_map(objs, fo) if fo else (1, {})
            elif m.group(2) is not None:
                arr = m.group(2)
                for hm in re.finditer(rb'<([0-9A-Fa-f\s]*)>', arr):
                    curline.append(decode_hex(hm.group(1), cur[0], cur[1]))
            elif m.group(3) is not None:
                curline.append(decode_hex(m.group(3), cur[0], cur[1]))
            elif m.group(4) is not None:
                x, y = float(m.group(4)), float(m.group(5))
                if cury is None:
                    cury = y
                elif abs(y - cury) > 0.5:
                    lines.append(''.join(curline))
                    curline = []
                    cury = y
            elif m.group(0) in (b'T*', b'ET'):
                if curline:
                    lines.append(''.join(curline))
                    curline = []
                cury = None
        if curline:
            lines.append(''.join(curline))
    return [ln for ln in lines if ln.strip()]


if __name__ == '__main__':
    for p in sys.argv[1:]:
        print(f"\n{'=' * 70}\n{p}\n{'=' * 70}")
        for ln in extract(p):
            print(ln)
