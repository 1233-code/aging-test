# 静态扫一遍**所有**变异脚本的锚点 —— 找出「变异点已经不存在了」的。
#
# 为什么要有这个 (2026-11-04 实测踩到):
#   变异脚本只在**跑到那一条**时才发现锚点没了, 而它报的是
#   「变异点没找到」并**当成没抓住** ⇒ 一条变异悄悄没被验过, 而报告上
#   照样写着「N/N 全抓住」。更糟的是: 改代码的人(我)自己知道刚改过哪儿,
#   不会怀疑「上次验过的那批锚点是不是还活着」。
#   ⇒ 改完代码**静态过一遍**, 比跑一遍快得多, 而且不依赖运行时。
#
# 判据:
#   · 锚点在源文件里出现 **0 次** ⇒ ❌ 失效了(这条变异从来没被验过);
#   · 出现 **>1 次** ⇒ ⚠️ 不唯一 —— `orig.replace(frm, to)` 会把**所有**
#     出现处一起改掉 ⇒ 变异比说明里写的更宽 ⇒ 「测试红了」可能是因为
#     别的地方坏了, 不一定是这条说明想守的那处。
#     · 说明里写了「**故意多改**」的 ⇒ 认可(同一段模板有两个布局分支,
#       或者同一条语句在几个同义分支里各出现一次 —— 那种**就是要一起改**,
#       少改一处反而是半个变异);
#     · 没写的 ⇒ ❌ 锚点要改长到唯一。
#
# .py 和 .js 两类变异脚本都扫 —— 前端那几个(版式)也是 .js, 它们的锚点
# 一样会烂, 一样得静态过一遍(2026-11-04 补上: 之前只扫 .py, .js 那几个
# 变异脚本的锚点**没人扫** ⇒ 烂了也没人知道)。
#
# 跑法: .venv/bin/python tests/_sweep_anchors.py
import ast
import glob
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: 说明里出现这个标记 = 「这条锚点就该命中多处」
MULTI_OK = "故意多改"


#: MUTATIONS 里用了**变量**(不是字面量)的条目 —— 静态查不了锚点。
#: 这本身就是要报的: 锚点查不了 ⇒ 那条变异**从来没被静态验过**。
NON_LITERAL = []


def mutations_of(path):
    """(文件, from, to, 说明) 四元组们。

    元素是 4 元组 (rel, frm, to, desc) 的照收; 5 元组(后面还挂了个别的
    字段, 比如 _mutate_fan_gpu_verdict.py)的**也收** —— 锚点还是头四个里的
    第 2、3 个。2 元组(名字 + 一个改文件的函数)的**收不了** ⇒ 那种脚本
    没法静态查锚点, 在汇总里单独点出来, 不当成「锚点失效」。

    ⚠️ **四个元素必须是字面量** —— 写成变量(`TOOL_REL` 那种)的话,
       这里 `literal_eval` 会炸(2026-11-11 就炸过一次, 整个扫描停在那儿,
       后面那些脚本**一个都没扫到** ⇒ 看着像「没问题」)。所以:
       · 炸了就当「查不了」记下来, 报出来, **不**让整个扫描停摆;
       · 变量写进 MUTATIONS 的一律改成字面量(见 _mutate_docs_consistency.py)。
    """
    tree = ast.parse(open(path, encoding="utf-8").read())
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        tgt = node.targets[0]
        if not (isinstance(tgt, ast.Name) and tgt.id == "MUTATIONS"):
            continue
        for e in node.value.elts:
            if not isinstance(e, ast.Tuple) or len(e.elts) < 4:
                continue
            try:
                vals = tuple(ast.literal_eval(x) for x in e.elts[:4])
            except (ValueError, SyntaxError):
                NON_LITERAL.append((path, len(e.elts)))
                continue
            yield vals


def js_unescape(s):
    """JS 模板串 → 运行时字符串(反引号里的 \\n 是**真换行**, \\n 是反斜杠+n)。

    一遍扫过去(左到右)处理转义: `\\` 先被吃掉 ⇒ `\\\\n`(反斜杠反斜杠n)
    还原成「反斜杠 + n」, 而不是「换行」—— 这两者的区别正好是 app.js
    里 `confirm('…?\\n\\n')` 那种字符串的字面写法。
    """
    def repl(m):
        c = m.group(1)
        return {'n': '\n', 't': '\t', 'r': '\r', '`': '`',
                '\\': '\\', '$': '$'}.get(c, c)
    return re.sub(r'\\(.)', repl, s)


def js_mutations_of(path):
    """(文件, from, to, 说明) 四元组们 —— 从 **.js** 变异脚本里抠出来。

    .js 的 MUTATIONS 是一个**对象数组**(from/to 用反引号模板串, 常常跨行),
    ast 解析不了 ⇒ 自己按文本抠:
      · 整个数组 = `const MUTATIONS = [` 到 `\n];` 之间;
      · 每个 entry = 以「4 个空格 + {」开头的那一段(edits 里的小对象缩进更深,
        不会被切开);
      · entry 里的 from/to 按出现顺序**一一配对**(edits 数组也是顺序写的);
      · 抠出来的模板串**要按 JS 的转义规则还原**(见 js_unescape)。
    """
    src = open(path, encoding="utf-8").read()
    i = src.find("const MUTATIONS = [")
    if i < 0:
        return
    j = src.find("\n];", i)
    if j < 0:
        return
    block = src[i:j]
    for part in re.split(r"\n    \{", block)[1:]:
        mfile = re.search(r"file:\s*'([^']*)'", part)
        if not mfile:
            continue
        rel = mfile.group(1)
        mname = re.search(r"name:\s*'([^']*)'", part)
        desc = mname.group(1) if mname else ""
        # 模板串里可能有**转义的反引号**(`\``)—— 所以不能简单「到下一个
        # 反引号为止」(那样会在转义的那个上截断, 抠出来的锚点只剩半截)。
        # ((?:[^`\\]|\\.)*) = 允许转义序列, 但不让裸反引号提前收尾。
        frs = list(re.finditer(r"from:\s*`((?:[^`\\]|\\.)*)`", part))
        tos = list(re.finditer(r"to:\s*`((?:[^`\\]|\\.)*)`", part))
        for k, fm in enumerate(frs):
            to = tos[k].group(1) if k < len(tos) else ""
            yield rel, js_unescape(fm.group(1)), js_unescape(to), desc


def resolve(rel):
    """变异脚本里的 file 字段 → 仓库里的真实路径。
    .js 脚本里的路径是相对 static/ 的(index.html / style.css)。"""
    p = os.path.join(REPO, rel)
    if os.path.exists(p):
        return p
    alt = os.path.join(REPO, "static", rel)
    if os.path.exists(alt):
        return alt
    return p


def main():
    scripts = []
    for mp in sorted(glob.glob(os.path.join(REPO, "tests", "_mutate_*.py"))):
        scripts.append((mp, mutations_of))
    for mp in sorted(glob.glob(os.path.join(REPO, "tests", "_mutate_*.js"))):
        scripts.append((mp, js_mutations_of))
    total = 0
    dead = 0
    wide = 0
    nosweep = []
    for mp, getter in scripts:
        name = os.path.basename(mp)
        try:
            entries = list(getter(mp))
        except SyntaxError as e:
            print(f"✗ {name}: 脚本自己语法错 {e}")
            dead += 1
            continue
        if not entries:
            # 这个脚本的变异**不是** from→to 形态(比如「名字 + 一个改文件的
            # 函数」)⇒ 没有锚点可查。**不算错**, 但要点出来 —— 别让人以为
            # 它被扫过了。
            nosweep.append(name)
            continue
        for rel, frm, to, desc in entries:
            total += 1
            srcp = resolve(rel)
            if not os.path.exists(srcp):
                print(f"✗ {name}: 源文件都没了 {rel}")
                dead += 1
                continue
            src = open(srcp, encoding="utf-8").read()
            n = src.count(frm)
            if n == 0:
                dead += 1
                print(f"✗ {name}: 锚点**不存在了** —— {desc[:46]}")
                print(f"    {rel} 里找不到 {frm[:70]!r}")
            elif n > 1:
                if MULTI_OK in desc:
                    continue          # 声明过「就是要一起改」
                wide += 1
                print(f"✗ {name}: 锚点**不唯一**(出现 {n} 次) —— {desc[:46]}")
                print(f"    {rel} :: {frm[:70]!r}")
                print(f"    ⇒ 把 from 改长到唯一, 或者在说明里写「{MULTI_OK}」")
    print(f"\n{len(scripts)} 个变异脚本, {total} 条变异: "
          f"锚点失效 {dead} 条, 不唯一 {wide} 条")
    if NON_LITERAL:
        dead += len(NON_LITERAL)
        print(f"✗ **有 {len(NON_LITERAL)} 条变异的锚点用了变量** ⇒ 静态查不了"
              f" ⇒ 它们从来没被静态验过:")
        for p, n in NON_LITERAL:
            print(f"    {os.path.basename(p)}: {n} 条")
        print("    ⇒ 把 MUTATIONS 里那几个元素改成**字面量**")
    if nosweep:
        print(f"⇒ 另有 {len(nosweep)} 个脚本的变异不是 from→to 形态, "
              f"锚点没法静态查: {', '.join(nosweep)}")
    if dead or wide:
        print("⇒ **有变异从来没被验过 / 验的不是说明里那处** —— 别急着报「全抓住」")
        return 1
    print("⇒ 全部锚点都在, 且都唯一(声明过「故意多改」的除外)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
