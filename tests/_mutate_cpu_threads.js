/* 变异测试: 故意把「线程数」旋钮改坏, 看 tests/test_frontend_cpu_threads.js
 * 会不会红。
 *
 * 为什么要有这么个东西: 「测试全绿」本身说明不了什么 —— 断言写错了、
 * 写松了、或者压根没跑到, 都是绿的。唯一能证明这测试**咬得动**的办法,
 * 就是亲手把代码改坏, 看它是不是真的红。
 *
 * 这一份守的是 2026-11-03 加的那个旋钮(CPU 压力项的加压封顶值):
 * 板子 A10000000000010 在 CPU 加压项**每次都被硬复位**, 而终端跑同样
 * 的 stress-ng 没事 ⇒ 崖在总占用 ~4.3 核(应用自己就占了 0.7~2.3 核)。
 * 「加压封顶在几线程」是唯一能立刻拧小的旋钮, 而它必须在**页面上**就能拧
 * (static/ 是热的, 刷新就生效, 不用改代码/出包/重启服务)。
 *
 * ⚠️ 这个旋钮最坏的坏法是「**看着能拧, 其实没接上**」:
 *   工人拧小了, 板子照样死在满载, 而报告上什么异常都看不出来。
 *   下面第 5 个变异就是这种坏法 —— 输入框还在, 但值被默认值覆盖了。
 *
 * 做法: **每个变异单开一个全新目录**, 把 static/ 复制进去, 在**副本**上做
 * 一处改动, 用 AGING_STATIC_DIR / AGING_APP_JS 指过去跑测试。仓库里
 * static/ 是产线在跑的那份 —— 只在副本上改, 副本也不删(留在 /tmp 里可查)。
 *
 * 跑法: node tests/_mutate_cpu_threads.js
 */
'use strict';

const fs = require('fs');
const path = require('path');
const os = require('os');
const { execFileSync } = require('child_process');

const REPO = path.join(__dirname, '..');
const SRC = path.join(REPO, 'static');
const TEST = path.join(__dirname, 'test_frontend_cpu_threads.js');

/* 每个变异: 把某个文件里的 from 换成 to, 测试必须变红。 */
const MUTATIONS = [
    {
        name: 'HTML: 「线程数」输入框整个没了 (旋钮不见了, 工人没法降压力)',
        file: 'index.html',
        from: `                            <span v-if="'threads' in tests[t.name]">
                                线程数: <input type="number" v-model.number="tests[t.name].threads" min="1" max="8" class="num-input" title="默认 4 核(范围 1~8; 拧到 8 = 满核, 过应力那一档)">
                            </span>
`,
        to: ``,
    },
    {
        name: 'HTML: 绑到了别的键 (工人拧的是空气, 后端收到的还是默认的 6)',
        file: 'index.html',
        from: `v-model.number="tests[t.name].threads"`,
        to: `v-model.number="tests[t.name].thread"`,
    },
    {
        name: 'HTML: 守卫没了 (别的项也冒出「线程数」框, 工人会以为那些项也能调)',
        file: 'index.html',
        from: `<span v-if="'threads' in tests[t.name]">`,
        to: `<span v-if="'duration_sec' in tests[t.name]">`,
    },
    {
        name: 'HTML: min 改成 0 (下界又打开了 ⇒ 「0 = 自动 = 按核数 = 满核」那条路回来)',
        file: 'index.html',
        from: `v-model.number="tests[t.name].threads" min="1"`,
        to: `v-model.number="tests[t.name].threads" min="0"`,
    },
    {
        name: 'JS: 前端那道 1~8 的夹子被拆了 (工人填 0 ⇒ 真按「自动 = 满核」发出去)',
        file: 'app.js',
        from: `            const THREADS_MIN = 1, THREADS_MAX = 8;`,
        to: `            const THREADS_MIN = 0, THREADS_MAX = 999;`,
    },
    {
        name: 'JS: 默认值把工人填的数盖掉了(框还在, 但拧了没用 ⇒ 板子照样死在满载)',
        file: 'app.js',
        from: `                tests[t.name] = { ...def };`,
        to: `                tests[t.name] = { ...def, threads: 0 };`,
    },
    {
        name: 'JS: 提交时把 threads 剔掉了(请求体里没有这一项 ⇒ 后端按全部核压)',
        file: 'app.js',
        from: `                tests: { ...tests },`,
        to: `                tests: Object.fromEntries(Object.entries(tests).map(([k, v]) => [k, { ...v, threads: undefined }])),`,
    },
];

let bad = 0;
for (let i = 0; i < MUTATIONS.length; i++) {
    const mut = MUTATIONS[i];
    /* 每个变异一个**全新目录** —— 不在同一个副本上反复覆写 */
    const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'mut_cputhreads_'));
    for (const f of ['index.html', 'style.css', 'app.js']) {
        fs.copyFileSync(path.join(SRC, f), path.join(tmp, f));
    }

    const p = path.join(tmp, mut.file);
    const orig = fs.readFileSync(p, 'utf8');
    if (!orig.includes(mut.from)) {
        console.log(`\n✗ ${mut.name}\n   → 变异点没找到(源文件变了?): ${mut.from.slice(0, 60)}...`);
        bad++;
        continue;
    }
    fs.writeFileSync(p, orig.replace(mut.from, mut.to));

    let code = 0;
    let out = '';
    try {
        out += execFileSync('node', [TEST], {
            env: {
                ...process.env,
                AGING_STATIC_DIR: tmp,
                AGING_APP_JS: path.join(tmp, 'app.js'),
            },
            encoding: 'utf8',
            stdio: ['ignore', 'pipe', 'pipe'],
        });
    } catch (e) {
        code = e.status === undefined ? -1 : e.status;
        out += (e.stdout || '') + (e.stderr || '');
    }
    const fails = (out.match(/✗/g) || []).length;
    if (code !== 0 && fails === 0) {
        console.log(`\n✗ ${mut.name}\n   → 红了, 但一个 ✗ 都没有 (exit=${code}) —— `
            + `这是**崩了**, 不是被断言咬住的, 不算数:\n${out.slice(-800)}`);
        bad++;
    } else if (code !== 0) {
        console.log(`\n✓ ${mut.name}\n   → 测试变红 (exit=${code}, ${fails} 项失败) —— 咬得动`);
    } else {
        console.log(`\n✗ ${mut.name}\n   → 测试还是绿的! 这个变异没被守住`);
        bad++;
    }
    console.log(`   (副本在 ${tmp})`);
}

console.log(bad === 0
    ? '\n全部变异都被抓住了 ✓'
    : `\n有 ${bad} 个变异没被抓住 ✗`);
process.exit(bad === 0 ? 0 : 1);
