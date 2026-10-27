/* 回归测试: 跑测试这一屏的**日志区** —— 日志框要撑到接近屏幕底, 里面的行数
 * 要跟着框高走, 不能留一大截空白。
 *
 * 起因 (2026-10-27, 产线提的): 「测试界面可以把日志显示拉长一点, 顶到界面
 * 底部最好是」; 随后又补了一句「也不用完全顶到底部, 稍微留一点点空间, 更美观
 * 一点」—— 所以底边**留 20px**, 不贴死屏幕(留白跟左右两边一样宽)。
 * 原来 .wizard-log-stream 写死 height:180px, 屏是 1080 高, 下面空出七八百
 * 像素; 屏幕只显示最后 30 行 (§10.2 就是这么写的)。
 *
 * 这里守的是三样东西, 少一样整个改动就塌:
 *   ① HTML: 日志渲染用 `logs.slice(-logVisibleLines)` —— 跟着框高走,
 *      不是写死的 -30。
 *   ② CSS: 一条**高度确定的** flex 链 —— #app 在跑测试这一屏要有
 *      `height`(确定高度), 日志框自己 `flex:1; min-height:0`, 且
 *      **不能**再有写死的像素高度。
 *      ⚠️ 这里踩过一个坑: 只写 `min-height:100vh` 是不够的 —— flex 容器
 *      的 main size 不确定时, flex-basis 的百分比会退化成「内容高度」,
 *      日志框会��到内容那么高(实测 90 行 → 1644px), 整页被撑到 1893px,
 *      1080 的屏根本看不到底。这条断言就是为了不让它退回去。
 *   ③ JS: 行数是**量出来的**(clientHeight), 框高变了要重量
 *      (ResizeObserver) —— 顶栏的「当前测试」标题一换行, 框就矮了。
 *
 * 注意: 这一条读的是 index.html + style.css + app.js 三个文件的**结构**,
 * 不起服务、不碰数据库、不碰真硬件。视觉上「到底顶没顶到屏幕底」靠
 * 板子上的 chromium 截图人工确认(见 docs 变更记录)。
 *
 * 跑法: node tests/test_frontend_logarea.js
 */
'use strict';

const fs = require('fs');
const path = require('path');

/* 同 test_frontend_layout.js: 给 AGING_* 换个路径就能对着别的副本跑 ——
 * 用来验证「这个测试真的会红」: 把页面改回旧版式, 断言必须失败。 */
const STATIC_DIR = process.env.AGING_STATIC_DIR
    || path.join(__dirname, '..', 'static');
/* 让 _vue_harness 也读副本里的 app.js —— 不然测函数行为时读的还是仓库里
 * 那份, 改副本根本测不到。必须在 require 之前设好(模块加载时就取值了)。 */
process.env.AGING_APP_JS = process.env.AGING_APP_JS
    || path.join(STATIC_DIR, 'app.js');

const { makeChecker, loadApp } = require('./_vue_harness');

const { check, section, finish } = makeChecker();

const html = fs.readFileSync(path.join(STATIC_DIR, 'index.html'), 'utf8');
const css = fs.readFileSync(path.join(STATIC_DIR, 'style.css'), 'utf8');
const js = fs.readFileSync(path.join(STATIC_DIR, 'app.js'), 'utf8');

/* 拆 CSS 规则: [{sel, body}]。注释先剥掉 —— 注释里也可能有 { }。 */
function cssBlocks(src) {
    const noComment = src.replace(/\/\*[\s\S]*?\*\//g, '');
    const out = [];
    const re = /([^{}]+)\{([^{}]*)\}/g;
    let m;
    while ((m = re.exec(noComment))) {
        out.push({
            sel: m[1].trim().replace(/\s+/g, ' '),
            body: m[2].replace(/\s+/g, ' ').trim(),
        });
    }
    return out;
}

const BLOCKS = cssBlocks(css);

/* 同一个选择器可能被写好几遍, 后写的赢(同优先级时) —— 取最后一个。
 * 选择器组(逗号分隔)也要能拆开单看。 */
function ruleFor(selector) {
    let found = null;
    for (const b of BLOCKS) {
        const sels = b.sel.split(',').map((s) => s.trim());
        if (sels.includes(selector)) found = b.body;
    }
    return found;
}

/* 所有同名选择器的规则块 —— 有时要全看: #app 有两条(一条定宽和留白,
 * 一条定 flex 方向), 只看最后一条会漏掉留白那条。 */
function rulesFor(selector) {
    return BLOCKS.filter((b) =>
        b.sel.split(',').map((s) => s.trim()).includes(selector));
}

function has(selector, decl) {
    const body = ruleFor(selector);
    return !!body && body.includes(decl);
}

section('【0】三个文件都读得到');
check(html.length > 0, 'index.html 读到了');
check(css.length > 0, 'style.css 读到了');
check(js.length > 0, 'app.js 读到了');

section('【1】HTML: 跑测试这一屏的 <main> 带 main-fill, 日志按算出来的行数渲染');
check(/<main v-else-if="view === 'wizard'" class="main-fill">/.test(html),
      "向导页的 <main> 挂了 class=\"main-fill\"");
check(/logs\.slice\(-logVisibleLines\)/.test(html),
      '日志渲染用 logs.slice(-logVisibleLines) (跟着框高走)');
check(!/logs\.slice\(-30\)/.test(html),
      '没有写死的 slice(-30) —— 那是 180px 小框时代的写法');
check(/ref="logEl"/.test(html), '日志框有 ref="logEl" (app.js 靠它量高度)');

section('【2】CSS: 一条高度**确定**的 flex 链');
const appRule = ruleFor('#app');
check(!!appRule && appRule.includes('display: flex') && appRule.includes('flex-direction: column'),
      '#app 是纵向 flex 容器', { appRule });
check(!!appRule && appRule.includes('min-height: 100vh'),
      '#app 至少撑满一屏 (别的屏靠它滚动)');

// ⚠️ 关键的一条: 跑测试这一屏必须给**确定**高度。
// 只有 min-height 的话, flex 容器高度不确定 → flex-basis 百分比退化成
// 内容高度 → 日志框长到内容那么高 → 整页被撑出屏幕(实测 1893px > 1080px)。
const wizardShell = ruleFor('#app:has(> main.main-fill)');
check(!!wizardShell, '有一条规则专门管「跑测试这一屏」的 #app 高度');
check(!!wizardShell && /height:\s*100vh/.test(wizardShell),
      '那一屏给的是 **height: 100vh** (确定高度), 不是只有 min-height',
      { wizardShell });
/* 底边**留 20px, 不贴死屏幕** —— 产线原话: 「也不用完全顶到底部, 稍微留
 * 一点点空间, 更美观一点」。所以 #app 本来的 20px 四周留白不能被抵掉:
 * 抵掉了日志框就正好压在屏幕边上, 看着像被切掉一截。 */
check(!/padding-bottom:\s*0/.test(wizardShell || ''),
      '向导页**不**把下留白抵掉 (底边留 20px, 不贴死屏幕)');
check(rulesFor('#app').some((b) => /padding:\s*20px/.test(b.body)),
      '#app 的四周留白还是 20px (底边那一截留白就是它给的)');

section('【3】CSS: 链上每一节都能收缩, 日志框不再有写死的高度');
for (const [sel, label] of [
    ['#app > main.main-fill', 'main'],
    ['#app > main.main-fill > .wizard-panel', '面板'],
    ['.wizard-log', '日志区'],
    ['.wizard-log-stream', '日志框'],
]) {
    check(has(sel, 'flex: 1'), `${label} (${sel}) 是 flex: 1, 吃掉剩下的高度`);
    check(has(sel, 'min-height: 0'), `${label} (${sel}) 有 min-height: 0, 才缩得下去`);
}
const streamRule = ruleFor('.wizard-log-stream');
check(!!streamRule && streamRule.includes('overflow-y: auto'),
      '日志框内部滚动 (内容多了在框里滚, 不把框撑高)');
check(!!streamRule && !/height:\s*\d+px/.test(streamRule),
      '日志框**没有**写死的像素高度 (原来那句 height:180px)', { streamRule });

section('【4】JS: 行数是量出来的, 框高变了会重量');
check(/logVisibleLines\s*=\s*ref\(/.test(js), 'logVisibleLines 是个 ref');
check(/const h = el\.clientHeight/.test(js),
      '高度取自 el.clientHeight (不是猜的常数)');
/* ⚠️ 下面两条要卡得**紧**: 只查 "js 里出现过 ResizeObserver" 是不够的 ——
 * 注释和变量名(logResizeObserver)里都有这四个字, 把真正的观察者删了照样
 * 查得到。同理, 光查 "读过 clientHeight" 也不够: 量了高度却不用它, 也是
 * 坏的。所以要查**真的 new 了一个观察者**、**真的拿它算了行数**。 */
check(/new ResizeObserver\(/.test(js),
      '真的 new 了一个 ResizeObserver (顶栏标题换行、窗口变了, 框高跟着变)');
check(/const lines = computeLogLines\(h\)/.test(js),
      '显示的行数就是拿量到的高度算出来的 (computeLogLines(h))');
check(/LOG_MIN_LINES\s*=\s*\d+/.test(js), '有行数下限 (框太矮也不至于一行都不显示)');

section('【5】算式本身: 框里不能留空白, 也不能少于下限');
const app = loadApp();
const f = app.computeLogLines;
check(typeof f === 'function', 'app.js 导出了 computeLogLines (能直接测)');
if (typeof f === 'function') {
    const LINE_PX = 18;
    check(f(0) === 30, '量不到高度(0) 时退回下限 30 行', { got: f(0) });
    check(f(200) === 30, '框很矮(200px) 时也是下限 30 行', { got: f(200) });
    check(f(832) === 70, '832px 的框 → 70 行 (板子上实测值)', { got: f(832) });
    check(f(2000) > 70, '框越高行数越多', { got: f(2000) });
    for (const h of [400, 600, 832, 1000, 1400]) {
        const lines = f(h);
        check(lines * LINE_PX >= h,
              `${h}px 高的框, ${lines} 行能铺满 (框底不留空白)`);
    }
}

process.exit(finish());
