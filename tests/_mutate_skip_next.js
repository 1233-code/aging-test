/* 变异测试: 故意把 static/ 里的两个操作键改坏, 看
 * tests/test_frontend_skip_next.js 会不会红。
 *
 * 为什么要有这么个东西: 「测试全绿」本身说明不了什么 —— 断言写错了、
 * 写松了、或者压根没跑到, 都是绿的。唯一能证明这测试**咬得动**的办法,
 * 就是亲手把代码改坏, 看它是不是真的红。
 *
 * 这一份守的是 2026-10-29 的两个操作键(界面这一侧):
 *   · 「下一项」和「停止」是**两个**键, 各打各的接口;
 *   · 两个键都弹确认框(「停止」不可逆);
 *   · 跳过原因的措辞把「用户主动」和「环境原因」分开。
 *
 * 这些改坏的后果:
 *   ① 工人按「下一项」, 后端收到的是「停止」—— 以为跳了一项, 实际整轮没了;
 *   ② 确认框没了 → 误按「停止」整轮就废了(那一轮剩下的项都不测了);
 *   ③ 措辞混了 → 工人自己按的按钮, 界面上却让他去查网线/查工位环境
 *      (指错方向, 白跑一趟)。
 *
 * 做法: **每个变异单开一个全新目录**, 把 static/ 复制进去, 在**副本**上做
 * 一处改动, 用 AGING_STATIC_DIR 指过去跑测试。仓库里 static/ 是产线在跑的
 * 那份 —— 只在副本上改。
 *
 * 跑法: node tests/_mutate_skip_next.js
 */
'use strict';

const fs = require('fs');
const path = require('path');
const os = require('os');
const { execFileSync } = require('child_process');

const REPO = path.join(__dirname, '..');
const SRC = path.join(REPO, 'static');
const TEST = path.join(__dirname, 'test_frontend_skip_next.js');

/* 每个变异: 把某个文件里的 from 换成 to, 测试必须变红。 */
const MUTATIONS = [
    {
        name: 'JS: 「下一项」打的是 /api/test/stop (工人想跳一项, 实际整轮没了)',
        file: 'app.js',
        from: `const r = await fetch('/api/test/next', { method: 'POST' });`,
        to: `const r = await fetch('/api/test/stop', { method: 'POST' });`,
    },
    {
        name: 'JS: 「下一项」不弹确认框 (按错一下就白跳一项)',
        file: 'app.js',
        from: `            if (!confirm(
                '跳过当前这一项, 直接测下一项?\\n\\n' +
                '· 当前项会记为「跳过」(没测成, 不计入通过率, 之后能重测)\\n' +
                '· 后面各项照常测'
            )) return;
`,
        to: ``,
    },
    {
        name: 'JS: 「停止」不弹确认框 (误按一下整轮就废了 —— 不可逆)',
        file: 'app.js',
        from: `            if (!confirm(
                '确定停止整轮测试?\\n\\n' +
                '· 当前这一项和后面所有项都会记为「跳过」(没测成)\\n' +
                '· 收尾后直接跳到结果页\\n' +
                '· 已经测出来的通过/失败**不会被抹掉**'
            )) return;
`,
        to: ``,
    },
    {
        name: 'JS: 跳过原因的标签不分「用户主动」和「环境原因」',
        file: 'app.js',
        from: `const skipReasonLabel = (code) => SKIP_REASON_LABEL[code] || '环境原因跳过';`,
        to: `const skipReasonLabel = (code) => '环境原因跳过';`,
    },
    {
        name: 'JS: 跳过原因的说明一律指向「查工位环境」(工人自己按的按钮, 指错方向)',
        file: 'app.js',
        from: `const skipReasonNote = (code) => SKIP_REASON_LABEL[code]
            ? '不计入通过率, 需要的话可以重测这一项'
            : '不计入通过率, 建议处理工位环境后复测';`,
        to: `const skipReasonNote = (code) => '不计入通过率, 建议处理工位环境后复测';`,
    },
    {
        name: 'JS: 原因说明里不写「是工人自己按的」',
        file: 'app.js',
        from: `            aborted_by_user: '工人中止了整轮测试 —— 本项未执行',
            skipped_by_user: '工人跳过此项, 直接测了下一项',
`,
        to: ``,
    },
    {
        name: 'HTML: 「下一项」那个键整个没了 (只剩「停止」)',
        file: 'index.html',
        from: `                        <button class="btn-next" @click="nextTest"
                                title="跳过当前这一项, 直接测下一项 (当前项记「跳过」)">⏭ 下一项</button>
`,
        to: ``,
    },
    {
        name: 'HTML: 两个键绑到同一个动作 (「下一项」其实是「停止」)',
        file: 'index.html',
        from: `@click="nextTest"`,
        to: `@click="stopTest"`,
    },
    {
        name: 'HTML: 键面上的提示(title)删了 (工人不知道按下去会怎样)',
        file: 'index.html',
        from: `title="跳过当前这一项, 直接测下一项 (当前项记「跳过」)"`,
        to: ``,
    },
    {
        name: 'CSS: .btn-next 整个删了 (键没样子, 工人不知道那是干什么的)',
        file: 'style.css',
        from: `.btn-next {
    background: #fff;
    color: #e67e22;
    padding: 6px 14px;
    border: 1px solid #e67e22;
    border-radius: 4px;
    font-size: 13px;
}
.btn-next:hover:not(:disabled) { background: #fdf3e7; }
`,
        to: ``,
    },
];

let bad = 0;
for (let i = 0; i < MUTATIONS.length; i++) {
    const mut = MUTATIONS[i];
    /* 每个变异一个**全新目录** —— 不在同一个副本上反复覆写 */
    const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'mut_skipnext_'));
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
            env: { ...process.env, AGING_STATIC_DIR: tmp },
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
}

console.log(bad === 0
    ? '\n全部变异都被抓住了 ✓'
    : `\n有 ${bad} 个变异没被抓住 ✗`);
process.exit(bad === 0 ? 0 : 1);
