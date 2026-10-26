/* 回归测试: 历史页的**模板结构**对不对。
 *
 * 上一条测试 (test_frontend_history.js) 验的是 JS 逻辑 —— 点一行展开哪一条、
 * 收起、竞态。但模板本身坏了它**测不出来**: 模板要是少个标签、`<template
 * v-for>` 用错位置、colspan 对不上列数, 页面直接白屏, 而 JS 那套断言照样
 * 全绿。
 *
 * 这里拿真的 Vue 3 模板编译器把 static/index.html 编译一遍:
 *   · 编译不过 → 模板有语法错(页面会白屏) → 直接判失败
 *   · 编译过了 → 在 AST 上断言结构:
 *       - 表格 7 列, 展开行的 colspan 也是 7
 *       - `<template v-for>` 里, 数据行后面**紧跟着**就是展开行
 *       - 详情块在**表格里面**(就地展开), 不在表格后面(挂最底下)
 *
 * 编译器装在仓库外面(默认 /home/User/.claude/vue-tools), 不往项目里塞
 * node_modules。没装的话这个测试会**跳过**并说明怎么装 —— 不能因为缺个
 * 依赖就假装通过。
 *
 * 跑法: node tests/test_frontend_template.js
 */
'use strict';

const fs = require('fs');
const path = require('path');
const Module = require('module');

const INDEX_HTML = path.join(__dirname, '..', 'static', 'index.html');
const TOOLS_DIR = process.env.VUE_TOOLS_DIR || '/home/User/.claude/vue-tools';

// 从仓库外面找 @vue/compiler-dom —— 不往项目里装 node_modules。
const origResolve = Module._resolveFilename;
Module._resolveFilename = function (request, ...rest) {
    if (request === '@vue/compiler-dom') {
        try {
            return origResolve.call(this, request, ...rest);
        } catch (e) {
            return origResolve.call(this, path.join(TOOLS_DIR, 'node_modules', request), ...rest);
        }
    }
    return origResolve.call(this, request, ...rest);
};

let compiler;
try {
    compiler = require('@vue/compiler-dom');
} catch (e) {
    console.log('⊘ 跳过: 找不到 @vue/compiler-dom');
    console.log('  装法(装在仓库外面, 不污染项目):');
    console.log(`    mkdir -p ${TOOLS_DIR} && cd ${TOOLS_DIR} && npm i @vue/compiler-dom@3`);
    console.log('  或者: VUE_TOOLS_DIR=/你的路径 node tests/test_frontend_template.js');
    process.exit(0);
}

const { check, section, finish } = require('./_vue_harness').makeChecker();

const html = fs.readFileSync(INDEX_HTML, 'utf8');

// 取 <div id="app"> 里的内容 —— 那是 Vue 挂载的模板。
//
// ⚠️ **不能**用「从 <div id="app"> 一直取到 <script>」这种切法: 那样会把
//    app 自己的那个 </div> 也切进来, 它在片段里就没有配对的开标签了 ——
//    编译器会报 "Invalid end tag", 而**页面其实好好的**(真实浏览器里
//    Vue 挂的是整个 DOM, 不是这个字符串)。所以按 div 深度配对地切。
function extractAppTemplate(src) {
    const open = /<div[^>]*\bid=["']app["'][^>]*>/i.exec(src);
    if (!open) return null;
    const start = open.index + open[0].length;
    let depth = 1;
    const re = /<div\b|<\/div>/gi;
    re.lastIndex = start;
    let m;
    while ((m = re.exec(src)) !== null) {
        if (m[0].toLowerCase() === '</div>') {
            depth--;
            if (depth === 0) return src.slice(start, m.index);
        } else {
            depth++;
        }
    }
    return null;
}
const templateSrc = extractAppTemplate(html);
if (templateSrc === null) {
    console.log('✗ 找不到 <div id="app"> 或者它没闭合 —— index.html 结构变了?');
    process.exit(1);
}

// ── ① 模板必须编译得过 ────────────────────────────────────────
section('【1】模板能被 Vue 3 编译 (编译不过 = 页面白屏)');
try {
    compiler.compile(templateSrc, { onError(e) { throw e; } });
    check(true, '模板编译通过');
} catch (e) {
    check(false, '模板编译通过', e.message);
    console.log('\n' + '有失败项 (0 通过 / 1 失败)');
    process.exit(1);
}

// ── ② 在**原始** AST 上找东西 ─────────────────────────────────
//
// ⚠️ 必须用 parse() 的原始 AST, 不能用 compile() 的:
//    compile() 会跑 transform, 把 `<template v-for>` 变成 Fragment、
//    把 v-if/v-else 拆成 IF 分支 —— 编译得**过**不代表源码结构是想要的那个。
//    这里要验的就是「源码长什么样」, 所以看原始 AST。
const ast = compiler.parse(templateSrc, { onError() {} });

// AST 节点没有 parent 指针(编译器不维护), 所以查找时**显式带上下文**。
function findAll(node, pred, out = [], parent = null) {
    if (!node || typeof node !== 'object') return out;
    if (Array.isArray(node)) {
        node.forEach(n => findAll(n, pred, out, parent));
        return out;
    }
    if (node.type === 1) {
        out.push({ node, parent });
        if (pred(node, parent)) return out;
    }
    (node.children || []).forEach(n => findAll(n, pred, out, node));
    (node.branches || []).forEach(n => findAll(n, pred, out, node));
    return out;
}
const all = findAll(ast, () => false);

function hasClass(node, cls) {
    const c = (node.props || []).find(p => p.type === 6 && p.name === 'class');
    if (!c || !c.value) return false;
    return String(c.value.content || '').split(/\s+/).includes(cls);
}
function propOf(node, name) {
    const p = (node.props || []).find(p => p.type === 6 && p.name === name);
    return p ? p.value : null;
}

// ⚠️ **指令在 AST 里不是按 `v-xxx` 记的**, Vue 解析时把名字规范化了:
//      v-for → 指令名 `for`      v-if → `if`      v-else → `else`
//      :key → `bind` (arg=key)   @click → `on` (arg=click)
//   而且指令的表达式在 `value.exp.content` 里, 不是 `value.content`。
//   按 `v-for` 去找的话**永远找不到** —— 踩过。
const DIRECTIVE = 7;
function directive(node, name, arg) {
    for (const p of (node.props || [])) {
        if (p.type !== DIRECTIVE || p.name !== name) continue;
        if (arg !== undefined) {
            if (!p.arg || p.arg.content !== arg) continue;
        }
        // ⚠️ 返回 **prop 自己**, 不是 p.value: 指令节点的表达式在 `p.exp`,
        //    而 `p.value` 是「指令的值」(只有 v-bind:foo.prop 这种才有),
        //    普通 v-for / v-if 上它是 undefined —— 返回它就等于没找到。
        return p;
    }
    return null;
}
function dirText(node, name, arg) {
    const d = directive(node, name, arg);
    return d && d.exp ? String(d.exp.content || '') : null;
}
function childTags(node) {
    return (node.children || []).filter(c => c.type === 1).map(c => c.tag);
}

// 表格
const tableHit = all.find(h => h.node.tag === 'table'
    && hasClass(h.node, 'history-table'));
const table = tableHit && tableHit.node;
check(!!table, '找到历史表格');

const theadHit = table && all.find(h => h.node.tag === 'thead' && h.parent === table);
const thead = theadHit && theadHit.node;
// <th> 在 <thead><tr> 里, 不在 thead 的直接孩子里 —— 得往下找一层。
const ths = thead ? all.filter(h => h.node.tag === 'th' && isUnder(h.node, thead)) : [];
function isUnder(node, ancestor) {
    let p = null;
    for (const h of all) {
        if (h.node === node) { p = h.parent; break; }
    }
    while (p) {
        if (p === ancestor) return true;
        const hit = all.find(h => h.node === p);
        p = hit && hit.parent;
    }
    return false;
}
const colCount = ths.length;
check(colCount === 7, `表格是 7 列`, colCount);

const tbodyHit = table && all.find(h => h.node.tag === 'tbody' && h.parent === table);
const tbody = tbodyHit && tbodyHit.node;
check(!!tbody, '找到 tbody');

// v-for 的 <template>
const forTpl = tbody && tbody.children.find(
    n => n.type === 1 && n.tag === 'template' && directive(n, 'for'));
check(!!forTpl, 'tbody 里有一个 <template v-for>');
if (forTpl) {
    const vfor = dirText(forTpl, 'for');
    check(vfor && vfor.includes('history'),
          'v-for 遍历的是 history', String(vfor));
    // Vue 3 要求 key 挂在 <template v-for> 上, 不能挂在它的孩子上
    const key = directive(forTpl, 'bind', 'key');
    check(!!key, '<template v-for> 上挂了 key (Vue 3 的要求)');
}

// <template> 的直接孩子: 数据行 + 展开行
const kids = forTpl ? forTpl.children.filter(c => c.type === 1) : [];
const rowTpl = kids.find(n => n.tag === 'tr' && hasClass(n, 'history-row'));
const expandTpl = kids.find(n => n.tag === 'tr' && hasClass(n, 'history-expand-row'));
check(!!rowTpl, '数据行 (tr.history-row)');
check(!!expandTpl, '展开行 (tr.history-expand-row)');
check(!!rowTpl && !!expandTpl && kids.indexOf(rowTpl) < kids.indexOf(expandTpl),
      '展开行在数据行**后面** (点开后详情紧贴着那一行)');

// 展开行里的 colspan 必须等于列数
const expandTd = expandTpl && expandTpl.children.find(n => n.type === 1 && n.tag === 'td');
const colspan = expandTd && propOf(expandTd, 'colspan');
check(!!colspan && Number(colspan.content) === colCount,
      `展开行 colspan = ${colCount}`, colspan && colspan.content);

// 详情块在**表格里面**(就地展开), 不在表格后面(挂最底下)
const detailHit = all.find(h => hasClass(h.node, 'history-detail'));
check(!!detailHit, '找到 .history-detail 块');
if (detailHit && table) {
    // 判据: 详情在 table 的子树里 —— 拿源码位置比更直白:
    // .history-detail 必须出现在 </table> **之前**。
    const iDetail = html.indexOf('class="history-detail"');
    const iTableEnd = html.indexOf('</table>', html.indexOf('history-table'));
    check(iDetail > 0 && iDetail < iTableEnd,
          '.history-detail 在 </table> **之前** —— 详情就地展开, 不是挂在页面最底下',
          `detail@${iDetail} tableEnd@${iTableEnd}`);
}

// 展开行带 v-if, 且条件是 expandedId === h.id
const vIf = expandTpl && dirText(expandTpl, 'if');
check(!!vIf && vIf.includes('expandedId'),
      '展开行由 expandedId 控制', vIf);

// 数据行的点击是 toggle
const rowClick = rowTpl && dirText(rowTpl, 'on', 'click');
check(!!rowClick && rowClick.includes('toggleHistoryDetail'),
      '数据行点的是 toggleHistoryDetail', rowClick);

// 展开标记
const mark = all.find(h => hasClass(h.node, 'history-mark'));
check(!!mark, '有展开标记 (▸/▾)');

console.log('\n' + '提示: 模板编译过了 ≠ 版面好看。'
    + '版面的事得在浏览器里看 —— 静态断言只能保证「结构没写错」。');

process.exit(finish());
