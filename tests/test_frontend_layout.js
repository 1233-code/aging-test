/* 回归测试: 配置页的**版式** —— 「测试配置」标题行 / 「本轮测试人员」的位置 /
 * 按钮的排布 / 列表顺序是否固定。
 *
 * 起因 (2026-10-27, 产线提的): 原来那四个按钮在 10 项列表的**最末尾**另起
 * 一行, 工人勾完一轮、滚到最底下才找得到「开始测试」; 而「本轮测试人员」
 * 输入框也埋在列表末尾, 常常是勾完才发现没填, 报告里只能记「未填写」。
 *
 * 改成(最终版): 标题行左边是「测试配置」, 最右角并排放 [全选][全不选]
 * [▶ 开始测试]; 「本轮测试人员」是标题行下面的**第一块**;
 * 「↺ 重置顺序」按钮**删掉**了, 下面 10 项的顺序**固定**成后端下发的
 * 推荐顺序(§7.6), 拖不动。
 *
 * 为什么要用测试守: 这是纯版式改动, 功能代码一行没动, 「看着对不对」靠
 * 肉眼看一眼 —— 下一个人把按钮挪回列表末尾, 或者把拖拽/重置加回来,
 * 没有任何东西会拦他。而这正是产线会抱怨的那件事。
 *
 * 注意: 这一条守的是 **HTML 结构**, 不是 CSS 视觉效果。别的前端测试都走
 * `_vue_harness` 跑 app.js 的逻辑, 碰不到 index.html —— 版式得直接读文件。
 *
 * 跑法: node tests/test_frontend_layout.js
 */
'use strict';

const fs = require('fs');
const path = require('path');
const { makeChecker } = require('./_vue_harness');

const { check, section, finish } = makeChecker();

/* 默认读仓库里的 index.html; 给 AGING_INDEX_HTML 换个路径就能对着别的副本跑 ——
 * 用来验证「这个测试真的会红」: 把页面改回旧版式, 断言必须失败, 否则这测试
 * 是个摆设。 */
const INDEX_HTML = process.env.AGING_INDEX_HTML
    || path.join(__dirname, '..', 'static', 'index.html');
const src = fs.readFileSync(INDEX_HTML, 'utf8');

/* HTML 注释里也可能出现 <div 之类的字样, 先剥掉再配对找结尾 —— 否则
 * 注释里的标签会把深度计数带偏。 */
const srcNoComment = src.replace(/<!--[\s\S]*?-->/g, '');

/* 取某个 <div class="X"> ... </div> 的**内部**内容。
 * 用配对计数找结尾, 别写死行号 —— 行号一改这个测试就假报警。 */
function innerOf(html, cls) {
    const open = html.indexOf(`<div class="${cls}">`);
    if (open < 0) return null;
    const start = html.indexOf('>', open) + 1;
    let depth = 0;
    let i = start;
    while (i < html.length) {
        const nextOpen = html.indexOf('<div', i);
        const nextClose = html.indexOf('</div>', i);
        if (nextClose < 0) return null;
        if (nextOpen >= 0 && nextOpen < nextClose) {
            depth++;
            i = nextOpen;
        } else {
            if (depth === 0) return html.slice(start, nextClose);
            depth--;
            i = nextClose;
        }
    }
    return null;
}

/* 取配置页 <section class="config-panel"> ... </section> 那一块。 */
function configPanel() {
    const open = srcNoComment.indexOf('<section class="config-panel">');
    if (open < 0) return null;
    let depth = 0;
    let i = open;
    while (i < srcNoComment.length) {
        const nextOpen = srcNoComment.indexOf('<section', i + 1);
        const nextClose = srcNoComment.indexOf('</section>', i + 1);
        if (nextClose < 0) return null;
        if (nextOpen >= 0 && nextOpen < nextClose) {
            depth++;
            i = nextOpen;
        } else {
            if (depth === 0) {
                return srcNoComment.slice(open, nextClose + '</section>'.length);
            }
            depth--;
            i = nextClose;
        }
    }
    return null;
}

const panel = configPanel();

section('【0】配置页能被找到');
check(panel !== null, '找到 <section class="config-panel">');
if (!panel) { finish(); process.exit(1); }

const headActions = innerOf(panel, 'config-head-actions');

section('【1】标题行: 「测试配置」+ 紧跟其后的开始测试 + 最右角三个按钮');
check(/<div class="config-head">/.test(panel), '有 .config-head 标题行容器');
check(/<h2>测试配置<\/h2>/.test(panel), '标题行里是「测试配置」');
check(headActions !== null, '有 .config-head-actions (最右角那组)');

check(headActions !== null && /全选/.test(headActions), '按钮组里有「全选」');
check(headActions !== null && /全不选/.test(headActions), '按钮组里有「全不选」');
check(headActions !== null && /开始测试/.test(headActions),
      '「▶ 开始测试」在 .config-head-actions 里 (顶了「重置顺序」原来的位置)');

section('【2】按钮组里只剩三个: 全选 → 全不选 → ▶开始测试 (「重置顺序」已删)');
const iSelAll = headActions ? headActions.indexOf('全选') : -1;
const iNoAll = headActions ? headActions.indexOf('全不选') : -1;
const iStart = headActions ? headActions.indexOf('开始测试') : -1;
check(iSelAll >= 0 && iNoAll > iSelAll && iStart > iNoAll,
      '顺序是 全选 → 全不选 → 开始测试', { iSelAll, iNoAll, iStart });
check(!/重置顺序/.test(panel), '「↺ 重置顺序」按钮已经删掉了');
check(!/resetOrder/.test(panel), '页面上没有残留的 resetOrder 绑定');
check(!/config-head-left/.test(panel),
      '没有多余的 .config-head-left 包裹层 (标题直接放在 .config-head 里)');

section('【2b】10 项的顺序**固定** —— 不许再拖');
const testItem = panel.slice(panel.indexOf('class="test-item"'));
check(!/draggable/.test(testItem), '测试项不再 draggable');
check(!/@dragstart|@dragover|@drop/.test(testItem), '没有 dragstart/dragover/drop 绑定');
check(!/drag-handle/.test(panel), '没有拖拽把手 (⋮⋮)');
check(!/dragging/.test(panel), '没有 .dragging 高亮态');

section('【3】「本轮测试人员」是标题行下面的**第一块** (在测试项列表之前)');
const iTester = panel.indexOf('class="tester-row"');
const iList = panel.indexOf('class="test-list"');
check(iTester >= 0, '配置页里有 .tester-row');
check(iList >= 0, '配置页里有 .test-list');
check(iTester >= 0 && iList >= 0 && iTester < iList,
      'tester-row 排在 test-list **前面**', { iTester, iList });
check(/id="tester-name"/.test(panel), '输入框 id 还是 tester-name (app.js 的 v-model 靠它)');
check(/v-model="testerName"/.test(panel), 'v-model 还是 testerName');

section('【4】配置页里不再有末尾那条 .actions 按钮行');
check(!/class="actions"/.test(panel),
      '配置页内没有 .actions 按钮行 (按钮已全部挪进标题行)');
check(/class="actions"/.test(src),
      '但页面别处(modals)还有 .actions —— 那是弹窗按钮, 不该被一起删掉');

section('【5】按钮行为没被改坏 (绑定还在)');
check(/@click="selectAll\(true\)"/.test(panel), '全选仍绑 selectAll(true)');
check(/@click="selectAll\(false\)"/.test(panel), '全不选仍绑 selectAll(false)');
check(/@click="startTest"/.test(panel), '开始测试仍绑 startTest');
check(/:disabled="!hasAnyEnabled"/.test(panel),
      '开始测试仍按 hasAnyEnabled 禁用 (一项没勾不许开跑)');

/* §6.7.3 断开→恢复轮: 工具自己用 xrandr --off/--auto 关一次屏再开回来,
 * 工人**不用动手**; 但关屏那几秒这块屏是黑的 —— 不提前说清楚, 工人会
 * 以为工具卡死了(而这时候界面正好也停在黑屏上, 连日志都看不见)。
 * 所以配置页上必须提前把这件事说清楚 —— 这是一条纯提示, 但删了它,
 * 现场表现就是「工人说工具卡住了」。 */
section('【6】HDMI 断开→恢复轮的提示在配置页上 (关屏那几秒会黑屏, 但不用操作)');
check(/t\.name === 'hdmi'/.test(panel), '配置页里有 hdmi 那一档的提示');
check(/replug_check/.test(panel), '提示跟着 replug_check 开关走 (关了就不显示)');
check(/断开\/恢复/.test(panel), '提示里写了「自动断开/恢复」');
check(/黑/.test(panel), '提示里写了「会黑几秒」');
check(/不用操作/.test(panel), '提示里说了不用工人操作');

process.exit(finish());
