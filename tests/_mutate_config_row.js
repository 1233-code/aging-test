/* 变异测试: 故意把「这一项的旋钮全都摆在右侧、跟它平齐」这个版式改坏,
 * 看 tests/test_frontend_config_row.js 会不会红。
 *
 * 为什么要有这么个东西: 「测试全绿」本身说明不了什么 —— 断言写错了、
 * 写松了、或者压根没跑到, 都是绿的。唯一能证明这测试**咬得动**的办法,
 * 就是亲手把页面改坏, 看它是不是真的红。
 *
 * 这一份守的是 2026-11-04 的版式改动(用户提了两版):
 *   第一版「次数和时长放到配置项右侧」;
 *   第二版(打回第一版)「凡是这些要自己填的次数也好时间也好, 最好全都放到
 *   右边去 …… 这些位置的提示词全都去掉, gpu 那个只需要在右边放轮次和
 *   场景时间这两个输入框和对应提示」。
 * ⇒ 终版: 右列一格 = 这一项的**全部**旋钮; 名称下面那第二行撤掉;
 *   项级别的说明文字全删; 只留 GPU 那两条紧挨着自己输入框的提示。
 *
 * 纯版式改动**最容易被人顺手改回去**(下一个人觉得「下面那排摆着更整齐」
 * 就给挪回去了), 所以得用测试钉住。
 *
 * 做法: **每个变异单开一个全新目录**, 把 static/ 的三个文件复制进去,
 * 在**副本**上做一处改动, 用 AGING_STATIC_DIR 指过去跑测试。仓库里
 * static/ 是产线在跑的那份 —— 只在副本上改, 副本也不删(留在 /tmp 里可查)。
 *
 * 跑法: node tests/_mutate_config_row.js
 */
'use strict';

const fs = require('fs');
const path = require('path');
const os = require('os');
const { execFileSync } = require('child_process');

const REPO = path.join(__dirname, '..');
const SRC = path.join(REPO, 'static');
const TEST = path.join(__dirname, 'test_frontend_config_row.js');

/* 每个变异: 把某个文件里的 from 换成 to, 测试必须变红。
 * edits 数组 = 一次改多处(用来模拟「把 </label> 挪个地方」这种搬动)。 */
const MUTATIONS = [
    {
        name: 'HTML: 右列那一格被改回 .test-options(老类名 ⇒ 网格里没有第 2 列规则 ⇒ 掉回下面)',
        file: 'index.html',
        from: `                        <div class="test-inline-opts" v-show="tests[t.name].enabled">`,
        to: `                        <div class="test-options" v-show="tests[t.name].enabled">`,
    },
    {
        name: 'HTML: 第二行(空的 .test-options)又被加回来了(名字下面多一行 ⇒ 一行一项没了)',
        file: 'index.html',
        from: `                            <span v-if="'cycles' in tests[t.name]">
                                轮转: <input type="number" v-model.number="tests[t.name].cycles" min="1" max="50" class="num-input"> 次
                            </span>
                        </div>
`,
        to: `                            <span v-if="'cycles' in tests[t.name]">
                                轮转: <input type="number" v-model.number="tests[t.name].cycles" min="1" max="50" class="num-input"> 次
                            </span>
                        </div>
                        <div class="test-options" v-show="tests[t.name].enabled">
                        </div>
`,
    },
    {
        name: 'HTML: 右列那一格被裹进 <label class="test-row"> 里了(点数字会**顺带把复选框也点一下** ⇒ 工人拧个数反而把这一项改了)',
        file: 'index.html',
        edits: [
            {
                from: `                        </label>
                        <!-- 这一项的**所有**旋钮都摆在**右侧**, 跟这一项平齐`,
                to: `                        <!-- 这一项的**所有**旋钮都摆在**右侧**, 跟这一项平齐`,
            },
            {
                from: `                                轮转: <input type="number" v-model.number="tests[t.name].cycles" min="1" max="50" class="num-input"> 次
                            </span>
                        </div>
`,
                to: `                                轮转: <input type="number" v-model.number="tests[t.name].cycles" min="1" max="50" class="num-input"> 次
                            </span>
                        </div></label>
`,
            },
        ],
    },
    {
        name: 'HTML: 「次数」的 v-model 键写错(工人拧的是空气 ⇒ 后端收到的还是默认值)',
        file: 'index.html',
        from: `v-model.number="tests[t.name].times"`,
        to: `v-model.number="tests[t.name].time"`,
    },
    {
        name: 'HTML: 「时长」的上界被改坏(最多只能 100 秒 ⇒ 想跑长一点的项拧不上去)',
        file: 'index.html',
        from: `v-model.number="tests[t.name].duration_sec" min="5" max="86400"`,
        to: `v-model.number="tests[t.name].duration_sec" min="5" max="100"`,
    },
    {
        name: 'HTML: 「次数」的守卫没了(每一项都冒出「次数」框 ⇒ 工人以为所有项都能调)',
        file: 'index.html',
        from: `<span v-if="'times' in tests[t.name]">`,
        to: `<span v-if="'duration_sec' in tests[t.name]">`,
    },
    {
        name: 'HTML: 「线程数」那一格被删了(CPU 的压力旋钮不见了 ⇒ 工人没法自己降压力)',
        file: 'index.html',
        from: `                            <span v-if="'threads' in tests[t.name]">
                                线程数: <input type="number" v-model.number="tests[t.name].threads" min="1" max="8" class="num-input" title="默认 4 核(范围 1~8; 拧到 8 = 满核, 过应力那一档)">
                            </span>
`,
        to: ``,
    },
    {
        name: 'HTML: 「轮次」那一格被删了(GPU 的轮次旋钮不见了)',
        file: 'index.html',
        from: `                            <span v-if="'rounds' in tests[t.name]">
                                轮次: <input type="number" v-model.number="tests[t.name].rounds" min="1" max="20" class="num-input">
                                <span class="hint-tag">每轮独立跑一遍, 能看分数衰减</span>
                            </span>
`,
        to: ``,
    },
    {
        name: 'HTML: 「每场景」那一格被删了(GPU 的场景时长旋钮不见了)',
        file: 'index.html',
        from: `                            <span v-if="'scene_sec' in tests[t.name]">
                                每场景: <input type="number" v-model.number="tests[t.name].scene_sec" min="0.5" max="60" step="0.5" class="num-input"> s
                                <span class="hint-tag">33 个场景 × 这个值 = 一轮时长</span>
                            </span>
`,
        to: ``,
    },
    {
        name: 'HTML: 「轮转」那一格被删了(风扇的轮转旋钮不见了)',
        file: 'index.html',
        from: `                            <span v-if="'cycles' in tests[t.name]">
                                轮转: <input type="number" v-model.number="tests[t.name].cycles" min="1" max="50" class="num-input"> 次
                            </span>
`,
        to: ``,
    },
    {
        name: 'HTML: GPU「轮次」紧挨着自己的那条提示被删了(输入框还在, 但没人知道它是干嘛的)',
        file: 'index.html',
        from: `                                <span class="hint-tag">每轮独立跑一遍, 能看分数衰减</span>
`,
        to: ``,
    },
    {
        name: 'HTML: GPU「每场景」紧挨着自己的那条提示被删了',
        file: 'index.html',
        from: `                                <span class="hint-tag">33 个场景 × 这个值 = 一轮时长</span>
`,
        to: ``,
    },
    {
        name: 'HTML: TF 卡那条「运行时自动检测 · 未插弹提示」被加回来了(项级别的说明又堆在下面)',
        file: 'index.html',
        from: `                            <span v-if="'cycles' in tests[t.name]">
                                轮转: <input type="number" v-model.number="tests[t.name].cycles" min="1" max="50" class="num-input"> 次
                            </span>
`,
        to: `                            <span v-if="'cycles' in tests[t.name]">
                                轮转: <input type="number" v-model.number="tests[t.name].cycles" min="1" max="50" class="num-input"> 次
                            </span>
                            <span v-if="t.name === 'tfcard'">
                                <span class="hint-tag">运行时自动检测 · 未插弹提示</span>
                            </span>
`,
    },
    {
        name: 'HTML: 蓝牙那条「只读探测 · 判定反过来」被加回来了',
        file: 'index.html',
        from: `                            <span v-if="'cycles' in tests[t.name]">
                                轮转: <input type="number" v-model.number="tests[t.name].cycles" min="1" max="50" class="num-input"> 次
                            </span>
`,
        to: `                            <span v-if="'cycles' in tests[t.name]">
                                轮转: <input type="number" v-model.number="tests[t.name].cycles" min="1" max="50" class="num-input"> 次
                            </span>
                            <span v-if="t.name === 'bluetooth'">
                                <span class="hint-tag">只读探测 · 判定反过来: 关着才算合格</span>
                            </span>
`,
    },
    {
        name: 'HTML: CPU「线程数」那条「默认 4 核(范围 1~8; 拧到 8 = 满核, 过应力那一档)」又被摆回版面上当提示词了',
        file: 'index.html',
        from: `                            <span v-if="'threads' in tests[t.name]">
                                线程数: <input type="number" v-model.number="tests[t.name].threads" min="1" max="8" class="num-input" title="默认 4 核(范围 1~8; 拧到 8 = 满核, 过应力那一档)">
                            </span>
`,
        to: `                            <span v-if="'threads' in tests[t.name]">
                                线程数: <input type="number" v-model.number="tests[t.name].threads" min="1" max="8" class="num-input">
                                <span class="hint-tag">默认 4 核(范围 1~8; 拧到 8 = 满核, 过应力那一档)</span>
                            </span>
`,
    },
    {
        name: 'HTML: 「线程数」输入框的 title(悬停提示)被删了(满核那句就彻底没了)',
        file: 'index.html',
        from: `v-model.number="tests[t.name].threads" min="1" max="8" class="num-input" title="默认 4 核(范围 1~8; 拧到 8 = 满核, 过应力那一档)"`,
        to: `v-model.number="tests[t.name].threads" min="1" max="8" class="num-input"`,
    },
    {
        name: 'HTML: 「线程数」的上界被从 8 改成 5(旋钮被改坏了 ⇒ 工人拧不了 6/7)',
        file: 'index.html',
        from: `v-model.number="tests[t.name].threads" min="1" max="8"`,
        to: `v-model.number="tests[t.name].threads" min="1" max="5"`,
    },
    {
        name: 'CSS: .test-item 不再用网格(右列塌了 ⇒ 旋钮掉回下面)',
        file: 'style.css',
        from: `    display: grid;\n    grid-template-columns: 1fr auto;`,
        to: `    display: block;\n    grid-template-columns: 1fr auto;`,
    },
    {
        name: 'CSS: 两列变成 auto auto(说明文字那列不再让位 ⇒ 右边被挤变形)',
        file: 'style.css',
        from: `    grid-template-columns: 1fr auto;`,
        to: `    grid-template-columns: auto auto;`,
    },
    {
        name: 'CSS: .test-inline-opts 不摆右列了(没有 grid-column: 2 ⇒ 落在第一列, 不在右侧)',
        file: 'style.css',
        from: `.test-inline-opts {\n    grid-column: 2;\n`,
        to: `.test-inline-opts {\n`,
    },
    {
        name: 'CSS: 右列那些输入框没样式了(白框 ⇒ 看不出那儿能填数)',
        file: 'style.css',
        from: `.test-inline-opts .num-input {`,
        to: `.test-inline-opts .numinput {`,
    },
    {
        name: 'CSS: .test-options 的空壳规则又加回来了(第二行撤了, 规则也不该留着)',
        file: 'style.css',
        from: `.test-inline-opts .num-input {`,
        to: `.test-options {\n    grid-column: 1 / -1;\n}\n.test-inline-opts .num-input {`,
    },
];

let bad = 0;
for (let i = 0; i < MUTATIONS.length; i++) {
    const mut = MUTATIONS[i];
    /* 每个变异一个**全新目录** —— 不在同一个副本上反复覆写 */
    const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'mut_configrow_'));
    for (const f of ['index.html', 'style.css', 'app.js']) {
        fs.copyFileSync(path.join(SRC, f), path.join(tmp, f));
    }

    const p = path.join(tmp, mut.file);
    const orig = fs.readFileSync(p, 'utf8');
    /* edits = 多处改动(搬动); 否则就是 from→to 一处 */
    const edits = mut.edits || [{ from: mut.from, to: mut.to }];
    let cur = orig;
    let missing = null;
    for (const e of edits) {
        if (!cur.includes(e.from)) {
            missing = e.from;
            break;
        }
        cur = cur.replace(e.from, e.to);
    }
    if (missing !== null) {
        console.log(`\n✗ ${mut.name}\n   → 变异点没找到(源文件变了?): ${missing.slice(0, 60)}...`);
        bad++;
        continue;
    }
    fs.writeFileSync(p, cur);

    let code = 0;
    let out = '';
    try {
        out += execFileSync('node', [TEST], {
            env: {
                ...process.env,
                AGING_STATIC_DIR: tmp,
                AGING_INDEX_HTML: path.join(tmp, 'index.html'),
                AGING_STYLE_CSS: path.join(tmp, 'style.css'),
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
