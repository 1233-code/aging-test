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
    try {
        out = execFileSync('node', [TEST], {
            env: { ...process.env, AGING_STATIC_DIR: TMP },
            encoding: 'utf8',
            stdio: ['ignore', 'pipe', 'pipe'],
        });
    } catch (e) {
        code = e.status === undefined ? -1 : e.status;
        out = (e.stdout || '') + (e.stderr || '');
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
