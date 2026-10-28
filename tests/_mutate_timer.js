/* 变异测试: 故意把 static/ 改坏, 看 tests/test_frontend_timer.js 和
 * tests/test_frontend_wizard_layout.js 会不会红。
 *
 * 为什么要有这么个东西: 「测试全绿」本身说明不了什么 —— 断言写错了、
 * 写松了、或者压根没跑到, 都是绿的。唯一能证明这测试**咬得动**的办法,
 * 就是亲手把代码改坏, 看它是不是真的红。
 *
 * 做法: 把 static/ 复制一份到临时目录, 在副本上做一处改动, 用
 * AGING_STATIC_DIR (和 AGING_APP_JS) 指过去跑测试。每个变异跑完把副本还原。
 *
 * ⚠️ 只在**副本**上改 —— 仓库里 static/ 是产线在跑的那份, 改坏了没人知道。
 *
 * 跑法: node tests/_mutate_timer.js
 */
'use strict';

const fs = require('fs');
const path = require('path');
const os = require('os');
const { execFileSync } = require('child_process');

const REPO = path.join(__dirname, '..');
const SRC = path.join(REPO, 'static');
const TEST_TIMER = path.join(__dirname, 'test_frontend_timer.js');
const TEST_WIZARD = path.join(__dirname, 'test_frontend_wizard_layout.js');

/* 从源文件里切出要改的整块 —— 手抄一大段迟早抄错(一个空格变了, 变异点
 * 就找不到, 还会以为「变异没被抓住」)。切出来的字符串跟源文件逐字一致。 */
const HTML_SRC = fs.readFileSync(path.join(SRC, 'index.html'), 'utf8');
const APP_SRC = fs.readFileSync(path.join(SRC, 'app.js'), 'utf8');

const TIMER_HTML = `                <span
                    class="run-timer"
                    v-if="runStartedAt"
                    :class="{ 'run-timer-done': !isRunning }"
                    :title="isRunning ? '本轮已跑这么久' : '本轮总耗时 (与报告里的「总耗时」同一个数)'"
                >⏱ {{ elapsedText }}</span>
`;
const HEADER_RIGHT_OPEN = `            <div class="header-right">`;
const BTN_HISTORY = `                <button class="btn-text" v-if="view === 'main'" @click="goHistory">历史记录</button>
`;

/* 每个变异: 把某个文件里的 from 换成 to, 测试必须变红。
 *
 * `steps` 是**多步**的(每步一个 from/to, 按顺序做) —— 挪位置这种改动
 * 光靠一步换不掉: 得先把它从原处摘掉, 再插到新位置。 */
const MUTATIONS = [
    /* ── CSS: 日志区往上挪 (§10.5) ── */
    {
        name: 'CSS: .wizard-current 退回 flex: 1 (日志又被推下去, 白回来了)',
        file: 'style.css',
        from: `    flex: 0 0 auto;
    padding: 16px 0;`,
        to: `    flex: 1;
    padding: 16px 0;`,
    },
    {
        name: 'CSS: .wizard-log 不再 flex: 1 (日志框不肯长, 底下留一截)',
        file: 'style.css',
        from: `.wizard-log {
    padding-top: 16px;
    border-top: 1px solid #eef0f4;
    flex: 1;`,
        to: `.wizard-log {
    padding-top: 16px;
    border-top: 1px solid #eef0f4;
    flex: 0 0 auto;`,
    },
    {
        name: 'CSS: .wizard-log-stream 退回写死 height:180px (框短一截)',
        file: 'style.css',
        from: `    flex: 1;
    min-height: 0;
    overflow-y: auto;
    white-space: pre-wrap;`,
        to: `    height: 180px;
    overflow-y: auto;
    white-space: pre-wrap;`,
    },
    /* ── CSS: 结果药丸挪到名字右边 (§10.5) ── */
    {
        name: 'CSS: .wizard-title-row 退回 display:block (名字和药丸又分开两行)',
        file: 'style.css',
        from: `.wizard-title-row {
    display: flex;`,
        to: `.wizard-title-row {
    display: block;`,
    },
    {
        /* ⚠️ 锚点必须带上 `.wizard-title-row {` —— 只写
         * 「align-items/space-between/gap」那三行的话, .config-head 里
         * 有一模一样的三行, 变异会改到**它**头上, .wizard-title-row
         * 纹丝不动, 断言照样绿 —— 变异根本没落在想改的地方。 */
        name: 'CSS: .wizard-title-row 不再 space-between (药丸不顶到右边)',
        file: 'style.css',
        from: `.wizard-title-row {
    display: flex;
    align-items: center;
    justify-content: space-between;`,
        to: `.wizard-title-row {
    display: flex;
    align-items: center;
    justify-content: flex-start;`,
    },
    {
        /* ⚠️ 必须是**改掉**那一条 margin-bottom, 不能在旁边**再加**一条 ——
         * CSS 里同一个属性写两遍时**后写的赢**, 加在前面的话浏览器看的
         * 还是后面那个 0, 这个变异在浏览器里**根本没生效**(等于没变异)。 */
        name: 'CSS: .wizard-status 把下边距加回来 (药丸自己占一行的高度)',
        file: 'style.css',
        from: `    margin-bottom: 0;
    flex: 0 0 auto;
}`,
        to: `    margin-bottom: 20px;
    flex: 0 0 auto;
}`,
    },
    {
        name: 'CSS: .wizard-name 把下边距加回来 (字比右边的药丸高半格)',
        file: 'style.css',
        from: `    margin-bottom: 0;
}`,
        to: `    margin-bottom: 12px;
}`,
    },
    /* ── CSS/HTML: 读秒摆在右上角 (§10.5) ── */
    {
        name: 'CSS: .run-timer 整条规则删掉 (读秒没样式, 跟旁边的字一个样)',
        file: 'style.css',
        from: `.run-timer {
    background: #37474f;
    color: #fff;
    padding: 4px 12px;
    border-radius: 4px;
    font-size: 15px;
    font-weight: 600;
    font-family: ui-monospace, "DejaVu Sans Mono", "Courier New", monospace;
    white-space: nowrap;
    font-variant-numeric: tabular-nums;   /* 每个数字一样宽, 不跳版 */
}`,
        to: `/* 删了 */`,
    },
    {
        name: 'CSS: .run-timer 不再 nowrap (右上角窄, 折行把顶栏撑开)',
        file: 'style.css',
        from: `    font-family: ui-monospace, "DejaVu Sans Mono", "Courier New", monospace;
    white-space: nowrap;`,
        to: `    font-family: ui-monospace, "DejaVu Sans Mono", "Courier New", monospace;`,
    },
    {
        name: 'HTML: 读秒挪到左边那一段 (不是右上角了)',
        file: 'index.html',
        steps: [
            { from: TIMER_HTML, to: `` },
            { from: HEADER_RIGHT_OPEN, to: `            ` + TIMER_HTML + HEADER_RIGHT_OPEN },
        ],
    },
    {
        name: 'HTML: 读秒整个删掉 (顶栏里就没这个数了)',
        file: 'index.html',
        from: TIMER_HTML,
        to: ``,
    },
    {
        name: 'HTML: 读秒挪到按钮后面 (不再是这一屏最该先看到的东西)',
        file: 'index.html',
        steps: [
            { from: TIMER_HTML, to: `` },
            { from: BTN_HISTORY, to: BTN_HISTORY + TIMER_HTML },
        ],
    },
    {
        name: 'HTML: 读秒绑的不是 elapsedText (数字是死的)',
        file: 'index.html',
        from: `>⏱ {{ elapsedText }}</span>`,
        to: `>⏱ {{ elapsedSec }}</span>`,
    },
    {
        name: 'HTML: 读秒不看 runStartedAt (还没开跑也杵在顶上)',
        file: 'index.html',
        from: `                    v-if="runStartedAt"`,
        to: `                    v-if="true"`,
    },
    /* ── JS: 计时本身 (§10.5) ── */
    {
        name: 'JS: 快照不取 started_at (刷新页面后计时归零)',
        file: 'app.js',
        from: `            if (msg.started_at) runStartedAt.value = msg.started_at;`,
        to: `            /* 不取了 */`,
    },
    {
        name: 'JS: 换项时把起点也改了 (每项各记一个, 不是整轮一个)',
        file: 'app.js',
        from: `                    setAlert('', 'warn', 0);
                    // **画面只由这一条事件推动。**`,
        to: `                    setAlert('', 'warn', 0);
                    runStartedAt.value = clockNow.value;
                    // **画面只由这一条事件推动。**`,
    },
    {
        name: 'JS: 快照里 status=running 也不起表 (刷新页面后读秒不走了)',
        file: 'app.js',
        from: `            if (msg.status === 'running') startClock();`,
        to: `            /* 不起表 */`,
    },
    {
        name: 'JS: run_started 不起表 (读秒永远不动)',
        file: 'app.js',
        from: `                    runStartedAt.value = msg.started_at || (Date.now() / 1000);
                    startClock();`,
        to: `                    runStartedAt.value = msg.started_at || (Date.now() / 1000);
                    /* 不起表 */`,
    },
    {
        name: 'JS: fmtDur 四舍五入改成四舍六入五成双 (屏幕跟报告对不上)',
        file: 'app.js',
        from: `            return s < 60 ? \`\${s.toFixed(1)}s\` : \`\${Math.floor(s / 60)}m\${String(Math.floor(s % 60)).padStart(2, '0')}s\`;`,
        to: `            return s < 60 ? \`\${(Math.round(s * 10) / 10).toFixed(2)}s\` : \`\${Math.ceil(s / 60)}m\${String(Math.floor(s % 60)).padStart(2, '0')}s\`;`,
    },
    {
        name: 'JS: fmtHms 的分钟算错 (读秒显示跟真时间对不上)',
        file: 'app.js',
        from: `            const m = Math.floor((s % 3600) / 60);`,
        to: `            const m = Math.floor(s / 60) % 100;`,
    },
    {
        name: 'JS: 总耗时算成「从现在倒推」(跑完之后数字还在涨)',
        file: 'app.js',
        from: `            const end = runFinishedAt.value || clockNow.value;
            return fmtDur(Math.max(0, end - runStartedAt.value));`,
        to: `            return fmtDur(Math.max(0, clockNow.value - runStartedAt.value));`,
    },
    {
        /* 合并重测那一下: run_finished 带的是**父轮**的一对时间, 前端要是
         * 只换结束时间、起点还沿用 run_started(重测轮的), 界面上那个总耗时
         * 就是「父轮结束 − 重测轮开始」—— 大得离谱。 */
        name: 'JS: run_finished 不更新起点(合并重测后总耗时错位)',
        file: 'app.js',
        from: `                    if (msg.started_at) runStartedAt.value = msg.started_at;
                    stopClock();`,
        to: `                    stopClock();`,
    },
    {
        name: 'JS: run_finished 不写结束时间(读秒锁不住)',
        file: 'app.js',
        from: `                    if (msg.finished_at) runFinishedAt.value = msg.finished_at;`,
        to: `                    /* 不写 */`,
    },
];

/* 每次跑用全新的临时目录(mkdtemp), 不删任何东西 —— 免得误删。 */
const TMP = fs.mkdtempSync(path.join(os.tmpdir(), 'mut_timer_'));
for (const f of ['index.html', 'style.css', 'app.js']) {
    fs.copyFileSync(path.join(SRC, f), path.join(TMP, f));
}
console.log(`副本在 ${TMP}\n`);

let bad = 0;
for (const mut of MUTATIONS) {
    const p = path.join(TMP, mut.file);
    const orig = fs.readFileSync(p, 'utf8');
    /* 单步的写成 steps, 多步的照原样 —— 挪位置这种改动得先摘再插。 */
    const steps = mut.steps || [{ from: mut.from, to: mut.to }];
    let missing = null;
    let text = orig;
    for (const st of steps) {
        if (!text.includes(st.from)) { missing = st.from; break; }
        text = text.replace(st.from, st.to);
    }
    if (missing !== null) {
        console.log(`\n✗ ${mut.name}\n   → 变异点没找到(源文件变了?): ${missing.slice(0, 60)}...`);
        bad++;
        continue;
    }
    fs.writeFileSync(p, text);
    let code = 0, out = '';
    /* 计时那套断言盯不到版式, 版式那套盯不到读秒 —— 两个都跑,
     * 任意一个变红就算守住了。 */
    for (const t of [TEST_TIMER, TEST_WIZARD]) {
        try {
            const o = execFileSync('node', [t], {
                env: {
                    ...process.env,
                    AGING_STATIC_DIR: TMP,
                    AGING_APP_JS: path.join(TMP, 'app.js'),
                },
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
    if (code !== 0 && fails === 0) {
        // ⚠️ **红了但一个 ✗ 都没有** —— 那多半不是断言把它咬住的, 而是
        //   测试自己崩了(app.js 语法错 → 加载不起来)。这种红是**假的**:
        //   证明不了「断言抓得住这个缺陷」。所以这一类**不算咬得动**。
        console.log(`\n✗ ${mut.name}\n   → 红了, 但一个 ✗ 都没有 (exit=${code}) —— `
            + `这是**崩了**, 不是被断言咬住的, 不算数:\n${out.slice(-800)}`);
        bad++;
    } else if (code !== 0) {
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
