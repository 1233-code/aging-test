/* 变异测试: 故意把 static/app.js 的「后端时钟」改坏, 看
 * tests/test_frontend_server_clock.js 会不会红。
 *
 * 为什么要有这么个东西: 「测试全绿」本身说明不了什么 —— 断言写错了、
 * 写松了、或者压根没跑到, 都是绿的。唯一能证明这测试**咬得动**的办法,
 * 就是亲手把代码改坏, 看它是不是真的红。
 *
 * 这一份守的是 2026-11-06 产线原话「开头一直是 0, 后面变两个多小时,
 * 然后测试结果上的时间又对得上」换来的规矩:
 *   ① 读秒的基准是**后端**的 `server_now`, 不是浏览器的墙钟;
 *   ② 墙钟歪 51.8 天 / 快 2 小时 28 分, 读秒都得照旧;
 *   ③ 快照和 WS 事件**两条路**都要重打基准;
 *   ④ 推进读秒用的是**单调钟**, 不是墙钟。
 *
 * 做法: **每个变异单开一个全新目录**, 把 static/ 复制进去, 在副本上做
 * 一处改动, 用 AGING_STATIC_DIR (和 AGING_APP_JS) 指过去跑测试。
 * 被改的那个文件是从头到尾**只写一次**(直接写变异后的内容), 别的文件
 * 原样复制 —— 所以整个过程不覆写任何已存在的文件, 跑完也不用还原。
 *
 * ⚠️ 只在**副本**上改 —— 仓库里 static/ 是产线在跑的那份, 改坏了没人知道。
 *
 * 跑法: node tests/_mutate_frontend_server_clock.js
 */
'use strict';

const fs = require('fs');
const path = require('path');
const os = require('os');
const { execFileSync } = require('child_process');

const REPO = path.join(__dirname, '..');
const SRC = path.join(REPO, 'static');
const TEST = path.join(__dirname, 'test_frontend_server_clock.js');

/* 每个变异: 把某个文件里的 from 换成 to, 测试必须变红。 */
const MUTATIONS = [
    {
        name: 'APP: 读秒改回用浏览器的墙钟(跨时间尺度相减 ⇒ 00:00:00 / 2 个多小时)',
        file: 'app.js',
        from: `                clockNow.value = serverNow.value
                    ? serverNow.value + (perfNow() - serverNowPerf.value) / 1000
                    : Date.now() / 1000;`,
        to: `                clockNow.value = Date.now() / 1000;`,
    },
    {
        name: 'APP: 收到后端的时间戳却不对表(clockNow 不动)',
        file: 'app.js',
        from: `            clockNow.value = n;
            return true;`,
        to: `            clockNow.value = 0;
            return true;`,
    },
    {
        name: 'APP: WS 事件那条路不对表(只有快照对)',
        file: 'app.js',
        from: `                if (msg && msg.server_now) syncServerClock(msg.server_now);`,
        to: `                if (false && msg && msg.server_now) syncServerClock(msg.server_now);`,
    },
    {
        name: 'APP: 快照那条路不对表(刷新页面就退回墙钟)',
        file: 'app.js',
        from: `            if (msg.server_now) syncServerClock(msg.server_now);`,
        to: `            if (false && msg.server_now) syncServerClock(msg.server_now);`,
    },
    {
        name: 'APP: 读秒直接拿墙钟减后端的起点(回到 2026-11-06 那个写法)',
        file: 'app.js',
        from: `        const elapsedSec = computed(() => {
            if (!runStartedAt.value) return 0;
            const end = runFinishedAt.value || clockNow.value;`,
        to: `        const elapsedSec = computed(() => {
            if (!runStartedAt.value) return 0;
            const end = runFinishedAt.value || Date.now() / 1000;`,
    },
    {
        name: 'APP: 推进读秒不用单调钟了(改回 Date.now())',
        file: 'app.js',
        from: `            (typeof performance !== 'undefined' && typeof performance.now === 'function')
                ? performance.now()
                : Date.now()`,
        to: `            Date.now()`,
    },
];

/* 每个变异一个**全新目录** —— 进程退出时这些目录留着(不删任何东西)。 */
const ROOT = fs.mkdtempSync(path.join(os.tmpdir(), 'mut_server_clock_'));
console.log(`副本都在 ${ROOT} 下面\n`);

let bad = 0;
MUTATIONS.forEach((mut, i) => {
    const dir = path.join(ROOT, `m${String(i).padStart(2, '0')}`);
    fs.mkdirSync(dir, { recursive: true });
    /* 原样复制 —— 被改的那个**不复制**, 下面直接写变异后的内容。 */
    for (const f of fs.readdirSync(SRC)) {
        if (f === mut.file) continue;
        const src = path.join(SRC, f);
        if (fs.statSync(src).isDirectory()) continue;
        fs.copyFileSync(src, path.join(dir, f));
    }
    const orig = fs.readFileSync(path.join(SRC, mut.file), 'utf8');
    if (!orig.includes(mut.from)) {
        console.log(`\n✗ ${mut.name}\n   → 变异点没找到(源文件变了?): ${mut.from.slice(0, 60)}...`);
        bad++;
        return;
    }
    fs.writeFileSync(path.join(dir, mut.file), orig.replace(mut.from, mut.to));

    let code = 0, out = '';
    try {
        out = execFileSync('node', [TEST], {
            env: {
                ...process.env,
                AGING_STATIC_DIR: dir,
                AGING_APP_JS: path.join(dir, 'app.js'),
            },
            encoding: 'utf8',
            stdio: ['ignore', 'pipe', 'pipe'],
        });
    } catch (e) {
        code = e.status === undefined ? -1 : e.status;
        out = (e.stdout || '') + (e.stderr || '');
    }
    const fails = (out.match(/✗/g) || []).length;
    if (code !== 0 && fails === 0) {
        /* ⚠️ **红了但一个 ✗ 都没有** —— 那多半不是断言把它咬住的, 而是
         *   测试自己崩了(app.js 语法错 → 加载不起来)。这种红是**假的**:
         *   证明不了「断言抓得住这个缺陷」。所以这一类**不算咬得动**。 */
        console.log(`\n✗ ${mut.name}\n   → 红了, 但一个 ✗ 都没有 (exit=${code}) —— `
            + `这是**崩了**, 不是被断言咬住的, 不算数:\n${out.slice(-800)}`);
        bad++;
    } else if (code !== 0) {
        console.log(`\n✓ ${mut.name}\n   → 测试变红 (exit=${code}, ${fails} 项失败) —— 咬得动`);
    } else {
        console.log(`\n✗ ${mut.name}\n   → 测试还是绿的! 这个变异没被守住`);
        bad++;
    }
});

console.log(bad === 0
    ? '\n全部变异都被抓住了 ✓'
    : `\n有 ${bad} 个变异没被抓住 ✗`);
process.exit(bad === 0 ? 0 : 1);
