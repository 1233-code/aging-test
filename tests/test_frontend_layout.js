/* 回归测试: 配置页的**版式** —— 「测试配置」标题行 / 「本轮测试人员」的位置 /
 * 按钮的排布 / 列表顺序是否固定。
 *
 * 起因 (2026-10-27, 产线提的): 原来那四个按钮在 11 项列表的**最末尾**另起
 * 一行, 工人勾完一轮、滚到最底下才找得到「开始测试」; 而「本轮测试人员」
 * 输入框也埋在列表末尾, 常常是勾完才发现没填, 报告里只能记「未填写」。
 *
 * 改成(最终版): 标题行左边是「测试配置」, 最右角并排放 [全选][全不选]
 * [▶ 开始测试]; 「本轮测试人员」是标题行下面的**第一块**;
 * 「↺ 重置顺序」按钮**删掉**了, 下面 11 项的顺序**固定**成后端下发的
 * 推荐顺序(§7.6), 拖不动。
 *
 * 为什么要用测试守: 这是纯版式改动, 功能代码一行没动, 「看着对不对」靠
 * 肉眼看一眼 —— 下一个人把按钮挪回列表末尾, 或者把拖拽/重置加回来,
 * 没有任何东西会拦他。而这正是产线会抱怨的那件事。
 *
 * 注意: 这一条守的是 **HTML 结构**, 不是 CSS 视觉效果。别的前端测试都走
 * `_vue_harness` 跑 app.js 的逻辑, 碰不到 index.html —— 版式得直接读文件。
 *
 * 2026-11-04 补充: 版式改版(每一项的旋钮全都搬到右列, 名称下面那第二行
 * 撤掉)之后, 【6】【7】两节跟着改了 —— HDMI 的黑屏警告不再挂配置页(改由
 * 跑测试时的大横幅负责), 蓝牙的「判定反过来」不再单独挂一条(写在项名和
 * 项说明里, 那两个字段后端下发, 每个项都渲染)。两处**信息都没丢**, 只是
 * 换了个地方说 —— 这两节守的就是「换了地方, 但没说没了」。
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

section('【2b】11 项的顺序**固定** —— 不许再拖');
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
 *
 * 2026-11-04 版式改版后, 配置页上**不再挂**这条提示了 —— 右列只摆旋钮,
 * 项级别的说明文字全部撤掉(项的说明在左边的项名/项说明里已经写着)。
 * 警告本身没丢: 它由**跑测试时**进度条底下那条大横幅负责(§6.7.3,
 * 5 秒倒计时, 「马上要黑屏, 不用操作」) —— 那是真正要黑屏的那一刻, 工人
 * 正盯着的那块屏, 横幅才是提醒到位的地方; 配置页上那条反而是提前很久
 * 说给一个还没开始跑的人听。横幅本身的行为由 tests/test_frontend_alert.js
 * 守着。 */
section('【6】HDMI 的黑屏警告不再挂配置页 —— 改由跑测试时的大横幅负责');
check(!/t\.name === 'hdmi'/.test(panel), '配置页上不再有 hdmi 那一档的提示');
check(!/断开\/恢复/.test(panel), '配置页上不再写「自动断开/恢复」');
check(!/不用操作/.test(panel), '配置页上不再写「不用操作」');
check(/wizard-alert/.test(src),
      '大横幅还在页面上(黑屏警告由它负责, 行为由 test_frontend_alert.js 守)');

/* §6.4.1 蓝牙: **判定是反的** —— 这一项要求蓝牙是**关着的**, 「通过」的
 * 意思是「蓝牙确实没在工作」。这层意思**不能丢**, 但它也不再是配置页上
 * 单独一条提示了: 它写在**项名**(「蓝牙 (应关闭)」)和**项说明**
 * (「只读探测 · 判定反过来: 蓝牙不工作才算合格」)里 —— 那两个字段是后端
 * /api/config 下发的, 配置页每个项都渲染它们, 工人看得到。 */
section('【7】蓝牙「判定反过来」不再在配置页重复一遍 —— 写在项名/项说明里');
check(!/t\.name === 'bluetooth'/.test(panel), '配置页上不再有 bluetooth 那一档的提示');
check(!/判定反过来/.test(panel), '配置页的 HTML 里不再写「判定反过来」');
check(!/关着才算合格/.test(panel), '配置页的 HTML 里不再写「关着才算合格」');
/* 那层意思还在不在 —— 去后端那两个字段里找(项名 + 项说明) */
const btSrc = fs.readFileSync(
    path.join(__dirname, '..', 'server', 'tests', 'bluetooth.py'), 'utf8');
check(/display_name = "蓝牙 \(应关闭\)"/.test(btSrc),
      '项名还是「蓝牙 (应关闭)」—— 别让人以为通过=蓝牙是好的');
check(/description = "只读探测 · 判定反过来: 蓝牙不工作才算合格"/.test(btSrc),
      '项说明还是「只读探测 · 判定反过来: 蓝牙不工作才算合格」(配置页每个项都渲染它)');
check(/t\.description/.test(panel), '配置页确实渲染了项说明({{ t.description }})');

process.exit(finish());
