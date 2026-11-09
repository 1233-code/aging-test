/* 回归测试: 配置页每一项的旋钮**全都**摆在**这一项的右侧**, 跟它平齐。
 *
 * 起因 (2026-11-04, 用户提的两版):
 *   第一版: 「次数和时长这个设置的位置能不能放到配置项右侧, 跟配置项平齐」;
 *   第二版(把第一版打回来): 「凡是这些要自己填的次数也好时间也好, 最好是
 *   全都放到右边去 …… 这些位置的提示词全都去掉, gpu 那个只需要在右边放轮次
 *   和场景时间这两个输入框和对应提示就可以了」。
 * ⇒ 终版: **右边一格 = 这一项自己的全部旋钮**; 名称下面那第二行整个撤掉;
 *   项级别的说明文字(TF「运行时自动检测」那一类)全部删掉; 只留 GPU 那两条
 *   跟着自己输入框走的提示。
 *
 * 为什么要在意: 「次数」跟「线程数」「轮次」混在下面那一行时, 一眼扫过去
 * 分不清哪个数配哪一项 —— 工人把「次数」调到隔壁那一项上, 报告里看不出来
 * (那一项的次数就是错的)。
 *
 * ⚠️ 三条**别踩回去**的坑(这个测试就是守它们的):
 *   ① 右列那一格**不能**放进 <label class="test-row"> 里面 ——
 *      <label> 里的输入框点一下会把**复选框也点一下**(= 把这一项勾上/取消),
 *      工人想拧个数反而把这一项改了 ⇒ 它必须是 .test-row 的**兄弟**节点,
 *      靠 CSS 网格摆到右列去;
 *   ② 撤掉第二行以后, **不许**把某个旋钮漏在下面 —— 「全都放到右边去」;
 *   ③ GPU 那两条提示**不许**跟着第二行一起删 —— 它们是「轮次 / 每场景」
 *      这两个输入框自己的说明, 要紧挨着输入框。
 *
 * 守的是 **HTML 结构 + CSS 规则**, 不是像素(没法在 CI 里截图)。
 * 跑法: node tests/test_frontend_config_row.js
 */
'use strict';

const fs = require('fs');
const path = require('path');
const { makeChecker } = require('./_vue_harness');

const { check, section, finish } = makeChecker();

/* 默认读仓库里的文件; 给环境变量换个路径就能对着别的副本跑 ——
 * 用来验证「这个测试真的会红」: 把页面改回旧版式, 断言必须失败。 */
const STATIC_DIR = process.env.AGING_STATIC_DIR || path.join(__dirname, '..', 'static');
const INDEX_HTML = process.env.AGING_INDEX_HTML || path.join(STATIC_DIR, 'index.html');
const STYLE_CSS = process.env.AGING_STYLE_CSS || path.join(STATIC_DIR, 'style.css');

const html = fs.readFileSync(INDEX_HTML, 'utf8');
const css = fs.readFileSync(STYLE_CSS, 'utf8');

/* HTML 注释里也有 <div> 之类的字样 ⇒ 先剥掉再配对找结尾, 否则深度计数
 * 会被注释里的标签带偏。 */
const htmlNoComment = html.replace(/<!--[\s\S]*?-->/g, '');

/* 取某个 class 的**内部**内容(配对计数找结尾, 不写死行号 —— 行号一改
 * 这个测试就假报警)。 */
function innerOf(cls) {
    const open = htmlNoComment.indexOf(`class="${cls}"`);
    if (open < 0) return null;
    const start = htmlNoComment.indexOf('>', open) + 1;
    let depth = 0;
    let i = start;
    while (i < htmlNoComment.length) {
        const nextOpen = htmlNoComment.indexOf('<div', i);
        const nextClose = htmlNoComment.indexOf('</div>', i);
        if (nextClose < 0) return null;
        if (nextOpen >= 0 && nextOpen < nextClose) {
            depth++;
            i = nextOpen;
        } else {
            if (depth === 0) return htmlNoComment.slice(start, nextClose);
            depth--;
            i = nextClose;
        }
    }
    return null;
}

/* 取 <label class="test-row"> ... </label> 的内部(它不是 div, 单独一个)。 */
function labelRowInner() {
    const open = htmlNoComment.indexOf('<label class="test-row">');
    if (open < 0) return null;
    const close = htmlNoComment.indexOf('</label>', open);
    if (close < 0) return null;
    return htmlNoComment.slice(open, close);
}

/* 取右列那一格里**某一个旋钮**的那一截(从它的 <span v-if="…"> 到下一个
 * <span v-if= 之前) —— 这样才能判断「提示是不是紧挨着自己那个输入框」。 */
function chunkFor(key) {
    if (!inlineOpts) return null;
    const marker = `<span v-if="'${key}' in tests[t.name]">`;
    const i = inlineOpts.indexOf(marker);
    if (i < 0) return null;
    const rest = inlineOpts.slice(i + marker.length);
    const next = rest.indexOf('<span v-if=');
    return next < 0 ? rest : rest.slice(0, next);
}

const inlineOpts = innerOf('test-inline-opts');
const options = innerOf('test-options');
const labelRow = labelRowInner();

section('【1】有一格专放这一项的旋钮, 而且它在 .test-row 的**右边**');
check(inlineOpts !== null, '页面上有 .test-inline-opts 这一格');
check(labelRow !== null, '页面上有 <label class="test-row">');
/* 第二行整个撤掉了 —— 「这些位置的提示词全都去掉」+「全都放到右边去」 */
check(options === null,
      '.test-options 那一格(名称下面那第二行)已经撤掉了', { options });

const iRow = htmlNoComment.indexOf('<label class="test-row">');
const iInline = htmlNoComment.indexOf('class="test-inline-opts"');
check(iRow >= 0 && iInline > iRow,
      '.test-inline-opts 排在 .test-row **后面**(⇒ 网格里落在右列)',
      { iRow, iInline });

section('【2】「次数」和「时长」在右列那一格里面');
if (inlineOpts !== null) {
    check(/v-model\.number="tests\[t\.name\]\.times"/.test(inlineOpts),
          '「次数」输入框在 .test-inline-opts 里');
    check(/v-model\.number="tests\[t\.name\]\.duration_sec"/.test(inlineOpts),
          '「时长」输入框在 .test-inline-opts 里');
    check(/次数:/.test(inlineOpts), '「次数」两个字跟着输入框');
    check(/时长:/.test(inlineOpts), '「时长」两个字跟着输入框');
    /* 守卫没丢: 只有真有这两个键的项才显示 */
    check(/v-if="'times' in tests\[t\.name\]"/.test(inlineOpts),
          '「次数」按这一项有没有 times 键来显示');
    check(/v-if="'duration_sec' in tests\[t\.name\]"/.test(inlineOpts),
          '「时长」按这一项有没有 duration_sec 键来显示');
    /* 输入框的范围没被改坏 */
    check(/min="1" max="100"/.test(inlineOpts), '「次数」还是 1~100');
    check(/min="5" max="86400"/.test(inlineOpts), '「时长」还是 5~86400');
}

section('【3】其余旋钮也**全都**搬到了右列(不许有漏在下面的)');
/* 每一格: 输入框在、守卫在、范围没被改坏 */
const KNOBS = [
    { key: 'threads', model: 'threads', range: 'min="0" max="6"', label: '线程数' },
    { key: 'rounds', model: 'rounds', range: 'min="1" max="20"', label: '轮次' },
    { key: 'scene_sec', model: 'scene_sec', range: 'min="0.5" max="60"', label: '每场景' },
    { key: 'cycles', model: 'cycles', range: 'min="1" max="50"', label: '轮转' },
];
for (const k of KNOBS) {
    const chunk = chunkFor(k.key);
    check(chunk !== null, `右列里有「${k.label}」那一格`, { key: k.key });
    if (chunk === null) continue;
    check(new RegExp(`v-model\\.number="tests\\[t\\.name\\]\\.${k.model}"`).test(chunk),
          `「${k.label}」的输入框在右列那一格里`);
    check(chunk.includes(k.range), `「${k.label}」的范围没被改坏 (${k.range})`);
    check(new RegExp(`${k.label}:`).test(chunk), `「${k.label}」两个字跟着输入框`);
}

section('【4】⚠️ 右列那一格**不能**在 <label class="test-row"> 里面 —— 否则点数字会顺带勾选');
/* <label> 里的输入框点一下 ⇒ 浏览器会把点击转给 label 关联的控件(这里的
 * 复选框) ⇒ 工人想拧个「次数」反而把这一项勾上/取消了。 */
/* 「在标签里面」的机器可查版本: 闭合 .test-row 的那个 </label> 必须在
 * .test-inline-opts **之前** —— 否则那一格就被裹进标签里了。 */
const iRowOpen = htmlNoComment.indexOf('<label class="test-row">');
const iRowClose = htmlNoComment.indexOf('</label>', iRowOpen);
const iInlineOpen = htmlNoComment.indexOf('class="test-inline-opts"');
check(iRowOpen >= 0 && iRowClose > iRowOpen,
      '找得到闭合 .test-row 的那个 </label>', { iRowOpen, iRowClose });
check(iRowClose >= 0 && iInlineOpen > iRowClose,
      '.test-inline-opts 在 </label> **之后**(⇒ 没被裹进标签里)',
      { iRowClose, iInlineOpen });
check(labelRow !== null && !/tests\[t\.name\]\.(times|duration_sec|threads|rounds|scene_sec|cycles)/.test(labelRow),
      '.test-row 标签里没有任何输入框(旋钮全在右列)');
check(labelRow !== null && /type="checkbox"/.test(labelRow),
      '复选框还在 .test-row 标签里(整行点一下能勾选/取消)');
check(labelRow !== null && /v-model="tests\[t\.name\]\.enabled"/.test(labelRow),
      '复选框的 v-model 没变');

section('【5】CSS: 两列网格 —— 左列这一项, 右列它的旋钮');
check(/\.test-item\s*\{[^}]*display:\s*grid/.test(css),
      '.test-item 是 grid 布局');
check(/\.test-item\s*\{[^}]*grid-template-columns:\s*1fr auto/.test(css),
      '两列: 左列 1fr(说明文字让位), 右列 auto(数字按内容宽)');
check(/\.test-inline-opts\s*\{[^}]*grid-column:\s*2/.test(css),
      '.test-inline-opts 在**第 2 列**(⇒ 摆在右侧)');
check(/\.test-row\s*\{[^}]*grid-column:\s*1/.test(css),
      '.test-row 在第 1 列');
/* 第二行撤了 ⇒ 它的样式规则也不该留个空壳(留着以后有人会以为还能用) */
check(!/\.test-options/.test(css), 'CSS 里也没有 .test-options 的规则了(第二行真的撤干净了)');
/* 输入框的样式得套到右列上(不然那几个框会没框没底色) */
check(/\.test-inline-opts \.num-input\s*\{/.test(css),
      '.num-input 的样式套在 .test-inline-opts 上(右列的框有边框)');
/* 窄屏兜底: 右列落到下一行 */
check(/@media \(max-width: 720px\)[\s\S]*?\.test-inline-opts\s*\{[^}]*grid-column:\s*1/.test(css),
      '窄屏(≤720px)时右列落到第 1 列(换行显示)');

section('【6】提示词: 项级别的那几条全删了, 只剩 GPU 那两条「对应提示」');
/* 删掉的(它们本来就是**项**的说明, 而项的说明已经在左边的 .test-desc 里
 * 写着 —— 同一件事说两遍, 列表被撑得很长):
 *   TF 卡「运行时自动检测 · 未插弹提示」/ WiFi「4 步直测 · 不连 AP …」/
 *   蓝牙「只读探测 · 判定反过来: 关着才算合格」/ 网口「3 步 · 未插网线 …」/
 *   HDMI「含 1 轮自动断开/恢复 …」
 * ⚠️ CPU 的「默认 6 核(上限 6); 0 = 自动」是另一回事: 它不是**项**的说明,
 * 是**那个输入框**的说明(六核封顶, 拧到 8 会被夹回 6) ⇒ 版面上撤掉,
 * 但挪进了输入框的 title(悬停才看得到) —— 不能让工人以为拧 8 就真按
 * 8 核压。 */
const DEAD_HINTS = [
    ['运行时自动检测', 'TF 卡的「运行时自动检测 · 未插弹提示」'],
    ['4 步直测', 'WiFi 的「4 步直测 · 不连 AP · 无 AP 时降级通过」'],
    ['判定反过来', '蓝牙的「判定反过来」'],
    ['关着才算合格', '蓝牙的「关着才算合格」'],
    ['未插网线弹提示', '网口的「3 步 · 未插网线弹提示 · 不测吞吐」'],
    ['断开/恢复', 'HDMI 的「含 1 轮自动断开/恢复 …」'],
];
for (const [needle, what] of DEAD_HINTS) {
    check(!htmlNoComment.includes(needle), `${what} 已经从配置页上删掉了`, { needle });
}
/* CPU 那句: **不能**作为可见的提示词回来, 但也不许整个消失 —— 它得在
 * 输入框的 title 里(悬停提示), 工人拧 8 的时候能想起来这是 6 核封顶。 */
check(!/<span class="hint-tag">[^<]*默认 6 核/.test(htmlNoComment),
      'CPU「默认 6 核…」不再作为可见的提示词摆在版面上');
const threadsTag = htmlNoComment.match(/<input[^>]*tests\[t\.name\]\.threads[^>]*>/);
check(!!threadsTag, '找得到「线程数」那个 input 标签');
if (threadsTag) {
    check(/title="默认 6 核\(上限 6\); 0 = 自动"/.test(threadsTag[0]),
          '那六个字挪进了「线程数」输入框的 title(悬停提示, 版面上不占地方)',
          threadsTag[0]);
}
/* GPU 那两条**要留**, 而且要紧挨着自己那个输入框 */
const roundsChunk = chunkFor('rounds');
const sceneChunk = chunkFor('scene_sec');
check(roundsChunk !== null && roundsChunk.includes('每轮独立跑一遍, 能看分数衰减'),
      'GPU「轮次」紧挨着自己的提示「每轮独立跑一遍, 能看分数衰减」');
check(sceneChunk !== null && sceneChunk.includes('33 个场景 × 这个值 = 一轮时长'),
      'GPU「每场景」紧挨着自己的提示「33 个场景 × 这个值 = 一轮时长」');

section('【7】六核封顶那个旋钮没被改坏');
check(/v-model\.number="tests\[t\.name\]\.threads" min="0" max="6"/.test(htmlNoComment),
      '「线程数」还是 0~6(六核封顶那次的旋钮没被改坏)');

process.exit(finish());
