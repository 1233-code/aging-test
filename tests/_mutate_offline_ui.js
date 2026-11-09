/* 变异测试: 故意把「界面不依赖公网 + 起不来时如实报」这两条改坏,
 * 看 tests/test_frontend_offline.js 会不会红。
 *
 * 为什么要有这么个东西: 「测试全绿」本身说明不了什么 —— 断言写错了、
 * 写松了、或者压根没跑到, 都是绿的。唯一能证明这测试**咬得动**的办法,
 * 就是亲手把代码改坏, 看它是不是真的红。
 *
 * 这一份守的是 2026-11-06 那个「浏览器一打开就跳到『服务已关闭』」的病根:
 * 界面框架 Vue 原来是从**公网 CDN**(unpkg.com)取的 ⇒ 产线网络上取不到 ⇒
 * app.js 第一行就抛 ⇒ 界面根本没挂起来 ⇒ 而页面里那个「服务已关闭」是
 * `v-if="shuttingDown"` 的**全屏卡**(position:fixed, 白底, z-index:2000)
 * ⇒ 没有 Vue 时原始模板照样渲染 ⇒ 它盖住整个页面 ⇒ **服务好好的, 界面
 * 却写着服务已关闭**。
 *
 * ⚠️ 最坏的坏法是「**看着没毛病, 其实还在骗人**」:
 *   ① Vue 改回 CDN —— 断网时又白屏, 而且没人知道;
 *   ② 兜底提示改说「服务已关闭」—— 那就等于把这次的毛病原样留着。
 *
 * 做法: **每个变异单开一个全新目录**, 把 static/ 复制进去, 在**副本**上做
 * 一处改动, 用 AGING_STATIC_DIR 指过去跑测试。仓库里 static/ 是产线在跑
 * 的那份 —— 只在副本上改, 副本也不删(留在 /tmp 里可查)。
 *
 * 跑法: node tests/_mutate_offline_ui.js
 */
'use strict';

const fs = require('fs');
const path = require('path');
const os = require('os');
const { execFileSync } = require('child_process');

const REPO = path.join(__dirname, '..');
const SRC = path.join(REPO, 'static');
const TEST = path.join(__dirname, 'test_frontend_offline.js');

/* 要复制进副本的文件 —— **少一个测试就测不到**。
 * 少复制 vue.global.prod.js 的话, 每个变异都会因为「Vue 文件不在」而红
 * ⇒ 看着像「全都咬得动」, 其实红的原因跟变异无关(2026-11-06 踩过同款:
 * 少复制一个依赖模块 ⇒ 结论整个作废)。 */
const FILES = ['index.html', 'style.css', 'app.js', 'vue.global.prod.js'];

/* 每个变异: 把某个文件里的 from 换成 to, 测试必须变红。 */
const MUTATIONS = [
    {
        name: 'HTML: Vue 又改回从公网 CDN 取(断网 ⇒ 界面又白屏, 而且没人知道)',
        file: 'index.html',
        from: `<script src="/static/vue.global.prod.js"></script>`,
        to: `<script src="https://unpkg.com/vue@3/dist/vue.global.prod.js"></script>`,
    },
    {
        name: 'HTML: 容器上的 v-cloak 没了(原始模板直接露出来 ⇒ 假「服务已关闭」又回来了)',
        file: 'index.html',
        from: `<div id="app" v-cloak>`,
        to: `<div id="app">`,
    },
    {
        name: 'CSS: [v-cloak] 的遮蔽规则没了(v-cloak 挂着也没用 ⇒ 原始模板照样露)',
        file: 'style.css',
        from: `[v-cloak] { display: none !important; }`,
        to: ``,
    },
    {
        name: 'HTML: 兜底提示整个没了(界面起不来 ⇒ 白屏, 工人只知道「不动」)',
        file: 'index.html',
        from: `    <div id="boot-fallback" class="boot-fallback">
        <div class="boot-fallback-card">
            <div class="boot-fallback-emoji">🖥️</div>
            <h1>界面没能加载出来</h1>
            <p class="boot-fallback-msg">
                后台服务是好的(健康检查能通), 挂掉的是**界面**这一层。
            </p>
            <p class="boot-fallback-reason boot-reason-vue">
                界面框架文件(static/vue.global.prod.js)没找到 —— 多半是 U 盘里的
                文件拷不全了, 换一根完整的盘。
            </p>
            <p class="boot-fallback-reason boot-reason-js">
                界面脚本(static/app.js)没跑起来 —— 先刷新一次; 还不行就是盘里的
                文件不完整。
            </p>
            <p class="boot-fallback-hint">
                这**跟板子合不合格没关系** —— 界面没起来的时候测试还没开始跑,
                盘上的历史记录和报告都不受影响。<br>
                服务是好的, 重新打开界面就行; 还是不行就换一根完整的盘。
            </p>
        </div>
    </div>`,
        to: ``,
    },
    {
        name: 'HTML: 看门狗瞎了(Vue 没加载到也发现不了 ⇒ 一直白屏没人管)',
        file: 'index.html',
        from: `if (typeof Vue === 'undefined') {`,
        to: `if (false) {`,
    },
    {
        name: 'JS: app.js 不报「界面起来了」(看门狗 8 秒后误报界面没起来)',
        file: 'app.js',
        from: `window.__AGING_BOOTED__ = true;`,
        to: ``,
    },
    {
        name: 'CSS: 兜底提示的 z-index 压到告别页下面(两个都出现时, 假的那张盖住真的)',
        file: 'style.css',
        from: `    z-index: 3000;`,
        to: `    z-index: 1000;`,
    },
    {
        name: 'HTML: 兜底提示改说「服务已关闭」(把这次的毛病原样留着 ⇒ 还是说谎)',
        file: 'index.html',
        from: `<h1>界面没能加载出来</h1>`,
        to: `<h1>服务已关闭</h1>`,
    },
];

/* ⚠️ 副本里少一个文件, 每个变异都会因为「文件不在」而红 ⇒ 结论整个作废。
 * 先检查一遍, 缺了就直接说清楚, 别让人以为「全都咬得动」。 */
const missing = FILES.filter(f => !fs.existsSync(path.join(SRC, f)));
if (missing.length) {
    console.log(`✗ static/ 里少了 ${missing.join(', ')} —— 副本拷不全, `
        + `每个变异都会因为「文件不在」而红, 结论不作废。`);
    console.log('  先把 static/ 补齐, 再跑这个脚本。');
    process.exit(1);
}

let bad = 0;
for (let i = 0; i < MUTATIONS.length; i++) {
    const mut = MUTATIONS[i];
    /* 每个变异一个**全新目录** —— 不在同一个副本上反复覆写 */
    const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'mut_offline_'));
    for (const f of FILES) {
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
