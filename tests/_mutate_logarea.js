/* 变异测试: 故意把 static/ 改坏, 看 tests/test_frontend_logarea.js 会不会红。
 *
 * 为什么要有这么个东西: 「测试全绿」本身说明不了什么 —— 断言写错了、
 * 写松了、或者压根没跑到, 都是绿的。唯一能证明这测试**咬得动**的办法,
 * 就是亲手把代码改坏, 看它是不是真的红。
 *
 * 做法: 把 static/ 复制一份到临时目录, 在副本上做一处改动, 用
 * AGING_STATIC_DIR 指过去跑测试。每个变异跑完把副本还原。
 *
 * 跑法: node tests/_mutate_logarea.js
 */
'use strict';

const fs = require('fs');
const path = require('path');
const os = require('os');
const { execFileSync } = require('child_process');

const REPO = path.join(__dirname, '..');
const SRC = path.join(REPO, 'static');
const TEST = path.join(__dirname, 'test_frontend_logarea.js');
/* §6.7.3 界面大横幅的测试。日志区那套断言盯不到横幅, 所以带 `alert: true`
 * 的变异**两个测试都跑** —— 任意一个变红就算这处变异被守住。
 * (路径是写死的常量, 不从变异表里取 —— 不给「跑任意文件」留口子。) */
const TEST_ALERT = path.join(__dirname, 'test_frontend_alert.js');

/* 现从 index.html 里切出「进度条那一块」和「横幅那一块」——
 * 手抄一大段 HTML 迟早抄错(一个空格变了, 变异点就找不到, 还会以为
 * 「变异没被抓住」)。切出来的字符串跟源文件逐字一致, 不会出这种岔。 */
const HTML_SRC = fs.readFileSync(path.join(SRC, 'index.html'), 'utf8');
const MARK_PROG = '<div v-if="currentTest.status === \'running\'" class="wizard-progress-line">';
const MARK_ALERT_COMMENT = '<!-- ⚠️ **大横幅**';
const MARK_NEXT = '<!-- 关键指标 -->';
const iP = HTML_SRC.indexOf(MARK_PROG);
const iC = HTML_SRC.indexOf(MARK_ALERT_COMMENT);
const iE = HTML_SRC.indexOf('-->', iC) + 3;
const iN = HTML_SRC.indexOf(MARK_NEXT);
const PROGRESS_BLOCK = HTML_SRC.slice(iP, iC);
const ALERT_COMMENT = HTML_SRC.slice(iC, iE);
const ALERT_BLOCK = HTML_SRC.slice(iE, iN);

/* 每个变异: 把某个文件里的 from 换成 to, 测试必须变红。 */
const MUTATIONS = [
    {
        name: 'CSS: 把确定高度 height:100vh 拿掉 (退回只有 min-height)',
        file: 'style.css',
        from: `#app:has(> main.main-fill) {
    height: 100vh;`,
        to: `#app:has(> main.main-fill) {
    /* height: 100vh; */`,
    },
    {
        name: 'CSS: 整条 #app:has(...) 规则删掉',
        file: 'style.css',
        from: `#app:has(> main.main-fill) {
    height: 100vh;
    /* 下留白**保留** 20px(跟左右一样), 不抵掉 —— 日志框底边停在屏幕底
       往上 20px。完全贴死屏幕边缘看着像被切掉一截, 留一截更稳。 */
}`,
        to: `/* 删了 */`,
    },
    {
        name: 'CSS: 把下留白抵掉 (padding-bottom: 0) —— 日志框贴死屏幕底',
        file: 'style.css',
        from: `    height: 100vh;
    /* 下留白`,
        to: `    height: 100vh;
    padding-bottom: 0;
    /* 下留白`,
    },
    {
        name: 'CSS: 日志框退回写死的 height:180px',
        file: 'style.css',
        from: `    flex: 1;
    min-height: 0;
    overflow-y: auto;
    white-space: pre-wrap;`,
        to: `    height: 180px;
    overflow-y: auto;
    white-space: pre-wrap;`,
    },
    {
        name: 'CSS: 链上少一节 min-height:0 (flex 子项缩不下去)',
        file: 'style.css',
        from: `.wizard-log {
    padding-top: 16px;
    border-top: 1px solid #eef0f4;
    flex: 1;
    min-height: 0;`,
        to: `.wizard-log {
    padding-top: 16px;
    border-top: 1px solid #eef0f4;
    flex: 1;`,
    },
    {
        name: 'HTML: 日志退回写死 slice(-30)',
        file: 'index.html',
        from: `logs.slice(-logVisibleLines)`,
        to: `logs.slice(-30)`,
    },
    {
        name: 'HTML: <main> 不挂 main-fill',
        file: 'index.html',
        from: `<main v-else-if="view === 'wizard'" class="main-fill">`,
        to: `<main v-else-if="view === 'wizard'">`,
    },
    {
        name: 'JS: 去掉 ResizeObserver (框高变了不重量)',
        file: 'app.js',
        from: `            if (typeof ResizeObserver !== 'undefined') {
                logResizeObserver = new ResizeObserver(() => measureLogLines());
                logResizeObserver.observe(el);
            }`,
        to: `            /* 没了 */`,
    },
    {
        name: 'JS: 行数不再量高度 (写死 30)',
        file: 'app.js',
        from: `            const lines = computeLogLines(h);`,
        to: `            const lines = 30;`,
    },
    {
        name: 'JS: 算式改错 (少算一半行 → 框底留空白)',
        file: 'app.js',
        from: `return Math.max(LOG_MIN_LINES, Math.ceil(h / LOG_LINE_PX * 1.4) + 5);`,
        to: `return Math.max(LOG_MIN_LINES, Math.ceil(h / LOG_LINE_PX * 0.5));`,
    },
    /* ── §6.7.3 界面大横幅 (2026-10-28): 提示得让人**看见** ── */
    {
        name: 'HTML: 横幅整个删掉 (提示又缩回日志框里了)',
        file: 'index.html',
        alert: true,
        from: ALERT_BLOCK,
        to: '',
    },
    {
        name: 'HTML: 横幅挪到进度条**上面**(工人正盯着进度条, 底下才看得见)',
        file: 'index.html',
        alert: true,
        from: PROGRESS_BLOCK + ALERT_COMMENT + ALERT_BLOCK,
        to: ALERT_BLOCK + ALERT_COMMENT + PROGRESS_BLOCK,
    },
    {
        name: 'HTML: 倒计时归零了数字还杵在那儿',
        file: 'index.html',
        alert: true,
        from: `v-if="alertBanner.countdown > 0" class="alert-countdown"`,
        to: `v-if="alertBanner.countdown >= 0" class="alert-countdown"`,
    },
    {
        name: 'HTML: 横幅不绑 level (所有提示都长一个样, 分不出轻重)',
        file: 'index.html',
        alert: true,
        from: `:class="['wizard-alert', alertBanner.level]"`,
        to: `class="wizard-alert"`,
    },
    {
        name: 'JS: 收到 alert 什么都不做(横幅永远不摆出来)',
        file: 'app.js',
        alert: true,
        from: `                case 'alert': {
                    // ⚠️ 大横幅 (见上面 setAlert 的说明): 空 text = 收起。
                    setAlert(msg.text || '', msg.level || 'warn', msg.seconds || 0);
                    break;
                }`,
        to: `                case 'alert': {
                    break;
                }`,
    },
    {
        name: 'JS: 倒计时不减(数字一直停在 3, 工人不知道还剩多久)',
        file: 'app.js',
        alert: true,
        from: `                if (b.countdown > 0) b.countdown -= 1;`,
        to: `                /* 不减了 */`,
    },
    {
        name: 'JS: 空 text 不收起横幅(一条横幅杵到下一项)',
        file: 'app.js',
        alert: true,
        from: `            if (!text) { alertBanner.value = null; return; }`,
        to: `            if (!text) { return; }`,
    },
    {
        name: 'JS: 不认 level (颜色永远 warn, 「马上要黑屏」跟普通提醒一个样)',
        file: 'app.js',
        alert: true,
        from: `            alertBanner.value = { text, level: level || 'warn', countdown: total };`,
        to: `            alertBanner.value = { text, level: 'warn', countdown: total };`,
    },
    {
        name: 'JS: 换项时不收横幅(上一项的提示挂在下一项界面上)',
        file: 'app.js',
        alert: true,
        from: `                    setAlert('', 'warn', 0);
                    // **画面只由这一条事件推动。**`,
        to: `                    // **画面只由这一条事件推动。**`,
    },
    {
        name: 'CSS: 字号缩回 12px(跟日志一样大, 抢不到那一眼)',
        file: 'style.css',
        alert: true,
        from: `    font-size: 22px;
    font-weight: 700;
    line-height: 1.4;
    color: #fff;`,
        to: `    font-size: 12px;
    font-weight: 400;
    line-height: 1.4;
    color: #fff;`,
    },
    {
        name: 'CSS: danger 那档(红底)没了',
        file: 'style.css',
        alert: true,
        from: `.wizard-alert.danger { background: #c62828; }`,
        to: `/* 没了 */`,
    },
    {
        name: 'CSS: 横幅不闪了(静态的一行字, 余光扫不到)',
        file: 'style.css',
        alert: true,
        from: `    animation: alert-blink 1s ease-in-out infinite;`,
        to: `    /* 不闪了 */`,
    },
];

/* 每次跑用全新的临时目录(mkdtemp), 不删任何东西 —— 免得误删。 */
const TMP = fs.mkdtempSync(path.join(os.tmpdir(), 'mut_static_'));
for (const f of ['index.html', 'style.css', 'app.js']) {
    fs.copyFileSync(path.join(SRC, f), path.join(TMP, f));
}
console.log(`副本在 ${TMP}\n`);

let bad = 0;
for (const mut of MUTATIONS) {
    const p = path.join(TMP, mut.file);
    const orig = fs.readFileSync(p, 'utf8');
    if (!orig.includes(mut.from)) {
        console.log(`\n✗ ${mut.name}\n   → 变异点没找到(源文件变了?): ${mut.from.slice(0, 60)}...`);
        bad++;
        continue;
    }
    fs.writeFileSync(p, orig.replace(mut.from, mut.to));
    let code = 0, out = '';
    /* 带 alert 标记的变异: 日志区那套断言盯不到横幅, 两个测试都跑,
     * 任意一个变红就算守住了。 */
    const tests = mut.alert ? [TEST, TEST_ALERT] : [TEST];
    for (const t of tests) {
        try {
            const o = execFileSync('node', [t], {
                env: { ...process.env, AGING_STATIC_DIR: TMP },
                encoding: 'utf8',
                stdio: ['ignore', 'pipe', 'pipe'],
            });
            out += o;
        } catch (e) {
            code = e.status === undefined ? -1 : e.status;
            out += (e.stdout || '') + (e.stderr || '');
        }
    }
    const fails = (out.match(/✗/g) || []).length;
    if (code !== 0) {
        console.log(`\n✓ ${mut.name}\n   → 测试变红 (exit=${code}, ${fails} 项失败) —— 咬得动`);
    } else {
        console.log(`\n✗ ${mut.name}\n   → 测试还是绿的! 这个变异没被守住`);
        bad++;
    }
    fs.writeFileSync(p, orig);   // 还原
}

console.log(bad === 0
    ? '\n全部变异都被抓住了 ✓'
    : `\n有 ${bad} 个变异没被抓住 ✗`);
process.exit(bad === 0 ? 0 : 1);
