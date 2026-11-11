/* 变异测试: 故意把 static/app.js 的「工人走开的那一轮不许拽走他」改坏, 看
 * tests/test_frontend_config_no_yank.js 会不会红。
 *
 * 为什么要有这么个东西: 「测试全绿」本身说明不了什么 —— 断言写错了、
 * 写松了、或者压根没跑到, 都是绿的。唯一能证明这测试**咬得动**的办法,
 * 就是亲手把代码改坏, 看它是不是真的红。
 *
 * 这一份守的是 2026-11-11 查出来的坑:
 *   工人跑完一轮 → 点「← 返回配置」准备测下一台 → WS 重连补推一帧**上一轮**
 *   的快照 ⇒ 界面被拽到上一轮的总结页 ⇒ 工人可能把**上一轮的结论**当成
 *   这一轮的 ⇒ **放过坏板子**。
 *
 * 做法: **每个变异单开一个全新目录**, 把 static/ 复制进去, 在副本上做
 * 一处改动, 用 AGING_STATIC_DIR (和 AGING_APP_JS) 指过去跑测试。
 * 被改的那个文件从头到尾**只写一次** —— 全程不覆写任何已存在的文件。
 *
 * ⚠️ 只在**副本**上改 —— 仓库里 static/ 是产线在跑的那份。
 *
 * 跑法: node tests/_mutate_frontend_config_no_yank.js
 */
'use strict';

const fs = require('fs');
const path = require('path');
const os = require('os');
const { execFileSync } = require('child_process');

const REPO = path.join(__dirname, '..');
const SRC = path.join(REPO, 'static');
const TEST = path.join(__dirname, 'test_frontend_config_no_yank.js');

/* 每个变异: 把某个文件里的 from 换成 to, 测试必须变红。 */
const MUTATIONS = [
    {
        name: 'APP: 守门整个没设防(上一轮的快照照样把界面拽到总结页)',
        file: 'app.js',
        from: `            if (leftRunId.value !== null && snapRunId
                && Number(snapRunId) === Number(leftRunId.value)) {`,
        to: `            if (false) {`,
    },
    {
        name: 'APP: goConfig 不记「从哪一轮走开的」',
        file: 'app.js',
        from: `            if (runId.value !== null && runId.value !== undefined) {
                leftRunId.value = runId.value;
            }`,
        to: `            // (变异: 不记了)`,
    },
    {
        name: 'APP: mini 那一轮按**自己**的号算(不看父轮 ⇒ 认不出是哪一轮)',
        file: 'app.js',
        from: `            if (leftRunId.value !== null && snapRunId
                && Number(snapRunId) === Number(leftRunId.value)) {`,
        to: `            if (leftRunId.value !== null && msg.run_id
                && Number(msg.run_id) === Number(leftRunId.value)) {`,
    },
    {
        name: 'APP: 守门太宽(连**新**一轮的快照也被挡了 ⇒ 新结果出不来)',
        file: 'app.js',
        from: `            if (leftRunId.value !== null && snapRunId
                && Number(snapRunId) === Number(leftRunId.value)) {`,
        to: `            if (leftRunId.value !== null && snapRunId) {`,
    },
];

/* 每个变异一个**全新目录** —— 进程退出时这些目录留着(不删任何东西)。 */
const ROOT = fs.mkdtempSync(path.join(os.tmpdir(), 'mut_noyank_'));
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
    const hits = orig.split(mut.from).length - 1;
    if (hits !== 1) {
        console.log(`\n✗ ${mut.name}\n   → 锚点出现 ${hits} 次(要恰好 1 次): `
            + `${mut.from.slice(0, 60)}...`);
        bad++;
        return;
    }
    /* ⚠️ JS 的 replace **只换第一处** ⇒ 锚点必须唯一(否则证据不成立)。 */
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
