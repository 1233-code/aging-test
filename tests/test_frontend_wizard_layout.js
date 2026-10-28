/* 回归测试: 测试界面的**版式** —— 日志区往上挪 + 结果药丸挪到测试项名字右边。
 *
 * 需求 (2026-10-28, 产线原话):「一个是测试时候日志的显示占比可以往上面
 * 多移动一点, 不要留太多空白, 然后测试结果展示的一些信息可以移动到测试项
 * 文字的右边」。
 *
 * 为什么这值得钉死:
 *   ① **留白不是审美问题, 是能看见的**。工人盯着这块屏干活, 中间那一大片
 *      白就意味着真正要看的日志被挤到屏幕最底下, 得低头才能看到。
 *   ② **根因是 flex 抢高度**: .wizard-current 和 .wizard-log 都是 flex: 1,
 *      剩下的高度被**平分**, 于是日志框被推到下面, 上面空一截。
 *      要让日志往上, .wizard-current 必须是 0 0 auto(只占内容的高度),
 *      剩下的全给 .wizard-log。
 *   ③ 结果药丸另起一行 = 白占一整行高度。同样的道理, 挪到名字右边。
 *
 * 这测试盯的是 **CSS/HTML 本身**(不跑 Vue) —— 版式问题在 JS 里看不出来,
 * 必须直接读文件。样式改坏了, 断言必须红。
 *
 * 跑法: node tests/test_frontend_wizard_layout.js
 */
'use strict';

const fs = require('fs');
const path = require('path');

/* 变异测试: AGING_STATIC_DIR 指到副本上就能对着改坏的副本跑。 */
const STATIC_DIR = process.env.AGING_STATIC_DIR
    || path.join(__dirname, '..', 'static');

const INDEX_HTML = path.join(STATIC_DIR, 'index.html');
const STYLE_CSS = path.join(STATIC_DIR, 'style.css');

let pass = 0;
let fail = 0;
const section = (t) => console.log('\n' + t);
const check = (cond, label, extra) => {
    if (cond) { pass++; console.log('    ✓ ' + label); }
    else {
        fail++;
        console.log('    ✗ ' + label +
            (extra !== undefined ? '  → ' + JSON.stringify(extra) : ''));
    }
};

/* ⚠️ **先把注释剥掉再抠规则** —— CSS 里到处是「原来写死 height: 180px」这种
 *   解释性的注释, 里面也带着冒号和花括号, 不剥的话抠出来的「规则体」是
 *   注释和声明混在一起的, 读出来的值会张冠李戴(踩过: 读 .wizard-current
 *   的 flex 读到 null, 一看是注释挡在前面)。 */
function stripComments(css) {
    return css.replace(/\/\*[\s\S]*?\*\//g, '');
}

/** 从 CSS 里抠出某个选择器的规则体(只认第一个匹配到的)。 */
function cssRule(css, selector) {
    const i = css.indexOf(selector);
    if (i < 0) return null;
    const open = css.indexOf('{', i);
    if (open < 0) return null;
    // 找到与之配对的右花括号
    let depth = 0;
    for (let j = open; j < css.length; j++) {
        if (css[j] === '{') depth++;
        else if (css[j] === '}') {
            depth--;
            if (depth === 0) return css.slice(open + 1, j);
        }
    }
    return null;
}

/** 从 CSS 规则体里读一个声明的值(认 `prop: value;`)。
 *
 *  ⚠️ 取的是**最后一个** —— CSS 里同一个属性写两遍时, **后写的赢**
 *    (层叠规则)。要是取第一个, 有人在一段规则的末尾补一句
 *    `margin-bottom: 12px` 把前面的盖掉, 这个测试照样绿 —— 而浏览器
 *    看到的是**盖掉之后**那个。
 */
function decl(rule, prop) {
    if (!rule) return null;
    const re = new RegExp('(?:^|;)\\s*' + prop + '\\s*:\\s*([^;]+)', 'g');
    let m, last = null;
    while ((m = re.exec(rule)) !== null) last = m[1].trim();
    return last;
}

function main() {
    const html = fs.readFileSync(INDEX_HTML, 'utf8');
    const css = stripComments(fs.readFileSync(STYLE_CSS, 'utf8'));

    // ══════════════════════════════════════════════════════════
    section('【1】日志区往上挪 —— .wizard-current **不许**再 flex: 1');
    {
        const cur = cssRule(css, '.wizard-current');
        check(cur !== null, 'CSS 里有 .wizard-current');
        check(decl(cur, 'flex') === '0 0 auto',
              '.wizard-current 是 flex: 0 0 auto (只占内容该占的高度)',
              decl(cur, 'flex'));

        const log = cssRule(css, '.wizard-log');
        check(log !== null, 'CSS 里有 .wizard-log');
        check(decl(log, 'flex') === '1',
              '.wizard-log 是 flex: 1 (吃掉剩下的全部高度)', decl(log, 'flex'));
        check(decl(log, 'min-height') === '0',
              '.wizard-log 有 min-height: 0 (否则它撑不扁, 白还是留着)',
              decl(log, 'min-height'));

        // ⚠️ **这两个不能同时是 flex: 1** —— 那会把剩下的高度平分,
        //    日志框又被推回下面去, 白照旧。
        check(!(decl(cur, 'flex') === '1' && decl(log, 'flex') === '1'),
              '两个区没有**同时** flex: 1 (不然高度被平分, 白又回来了)');

        // 日志框在 DOM 里排在 .wizard-current **后面** —— 顺序反了的话,
        // 即使高度对, 视觉上也是「先一大块空白再日志」。
        const iCur = html.indexOf('class="wizard-current"');
        const iLog = html.indexOf('class="wizard-log"');
        check(iCur >= 0 && iLog > iCur,
              'DOM 里日志框排在当前测试区**后面** (日志在下、贴着底)', { iCur, iLog });
    }

    // ══════════════════════════════════════════════════════════
    section('【2】结果药丸挪到**测试项名字的右边**(同一行)');
    {
        const row = cssRule(css, '.wizard-title-row');
        check(row !== null, 'CSS 里有 .wizard-title-row');
        check(decl(row, 'display') === 'flex',
              '.wizard-title-row 是 flex (名字和药丸同一行)', decl(row, 'display'));
        check(decl(row, 'align-items') === 'center',
              '.wizard-title-row 上下居中 (文字跟药丸齐平)',
              decl(row, 'align-items'));
        check(decl(row, 'justify-content') === 'space-between',
              '.wizard-title-row 两头分开 (药丸顶到右边)',
              decl(row, 'justify-content'));

        // HTML: 名字和药丸必须**在同一个 .wizard-title-row 里**
        const iRow = html.indexOf('<div class="wizard-title-row">');
        const iName = html.indexOf('class="wizard-name"');
        const iStatus = html.indexOf("'wizard-status', currentTest.status");
        check(iRow >= 0, '模板里有 .wizard-title-row');
        check(iName > iRow, '测试项名字在这个行里', { iRow, iName });
        check(iStatus > iRow && iStatus > iName,
              '结果药丸也**在这个行里**, 且在名字**后面**(右边)', { iName, iStatus });

        // 药丸自己不许再带下边距 —— 同行里再带下边距, 药丸会往下坠一点
        const st = cssRule(css, '.wizard-status');
        check(decl(st, 'margin-bottom') === '0',
              '.wizard-status 没有下边距 (同行里加了会歪)', decl(st, 'margin-bottom'));
        const nm = cssRule(css, '.wizard-name');
        check(decl(nm, 'margin-bottom') === '0',
              '.wizard-name 没有下边距 (不然字比右边的药丸高半格)',
              decl(nm, 'margin-bottom'));

        // ⚠️ **药丸不许再另起一行** —— 原来它在 .wizard-current 里单摆,
        //    白占一整行高度, 正好是这次要省掉的那一行的。
        const iCur = html.indexOf('<div class="wizard-current"');
        const iCurEnd = html.indexOf('class="wizard-title-row"');
        const iStatusInCur = html.indexOf("'wizard-status', currentTest.status");
        check(iStatusInCur > iCurEnd,
              '药丸被**包进**了 .wizard-title-row, 没留在外面单摆一行');
    }

    // ══════════════════════════════════════════════════════════
    section('【3】读秒摆在**右上角**');
    {
        const iRight = html.indexOf('<div class="header-right">');
        const iTimer = html.indexOf('class="run-timer"');
        const iBtn = html.indexOf('class="btn-text"');
        check(iRight >= 0, '顶栏有 .header-right');
        check(iTimer > iRight, '读秒在顶栏的右半边里 (右上角)', { iRight, iTimer });
        check(iBtn > iTimer, '读秒排在按钮前面', { iTimer, iBtn });

        const timer = cssRule(css, '.run-timer');
        check(timer !== null, 'CSS 里有 .run-timer');
        check(decl(timer, 'white-space') === 'nowrap',
              '读秒不折行 (右上角窄, 折行会把顶栏撑开)', decl(timer, 'white-space'));
        check(/tabular-nums/.test(timer || ''),
              '读秒用等宽数字 (秒数跳动时不会左右抖)', null);
    }

    // ══════════════════════════════════════════════════════════
    section('【4】日志框本身**吃满**剩下的高度(别留半截空的黑框)');
    {
        const stream = cssRule(css, '.wizard-log-stream');
        check(stream !== null, 'CSS 里有 .wizard-log-stream');
        const fh = decl(stream, 'flex');
        check(fh === '1' || fh === '1 1 auto' || fh === '1 1 0%',
              '.wizard-log-stream 自己也是 flex: 1 (撑满日志区)', fh);
        check(decl(stream, 'height') === null,
              '.wizard-log-stream 没写死 height (写死了框就短一截, 下面空着)',
              decl(stream, 'height'));
    }

    console.log(`\n全部通过 (${pass} 通过 / ${fail} 失败)`);
    return fail === 0 ? 0 : 1;
}

process.exit(main());
