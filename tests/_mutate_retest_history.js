/* 变异测试: 故意把「历史记录重测合并」改坏, 看 tests/test_frontend_retest_history.js
 * 会不会红。
 *
 * 为什么要有这么个东西: 「测试全绿」本身说明不了什么 —— 断言写错了、
 * 写松了、或者压根没跑到, 都是绿的。唯一能证明这测试**咬得动**的办法,
 * 就是亲手把代码改坏, 看它是不是真的红。
 *
 * 做法: 把 static/ 复制一份到临时目录, 在副本上做一处改动, 用
 * AGING_STATIC_DIR 指过去跑测试。每个变异跑完把副本还原。
 *
 * 跑法: node tests/_mutate_retest_history.js
 */
'use strict';

const fs = require('fs');
const path = require('path');
const os = require('os');
const { execFileSync } = require('child_process');

const REPO = path.join(__dirname, '..');
const SRC = path.join(REPO, 'static');
const TEST = path.join(__dirname, 'test_frontend_retest_history.js');
/* 总结页那套断言(通过率口径 + 可重测清单)住在另一个测试文件里。
 * 带 `summary: true` 的变异**两个测试都跑** —— 任意一个变红就算守住了。
 * (路径是写死的常量, 不从变异表里取 —— 不给「跑任意文件」留口子。) */
const TEST_SUMMARY = path.join(__dirname, 'test_frontend_summary.js');

/* 每个变异: 把某个文件里的 from 换成 to, 测试必须变红。 */
const MUTATIONS = [
    {
        name: 'JS: historyRetestableNames 不再挑「出错」的项 (只挑 failed)',
        file: 'app.js',
        from: `        const historyRetestableNames = (detail) => (detail && detail.results || [])
            .filter(r => r.status === 'failed' || r.status === 'error'
                     || r.status === 'skipped')
            .map(r => r.test_name);`,
        to: `        const historyRetestableNames = (detail) => (detail && detail.results || [])
            .filter(r => r.status === 'failed')
            .map(r => r.test_name);`,
    },
    {
        name: 'JS: historyRetestableNames 不再挑「跳过」的项 (跳过的不能重测了)',
        file: 'app.js',
        from: `        const historyRetestableNames = (detail) => (detail && detail.results || [])
            .filter(r => r.status === 'failed' || r.status === 'error'
                     || r.status === 'skipped')
            .map(r => r.test_name);`,
        to: `        const historyRetestableNames = (detail) => (detail && detail.results || [])
            .filter(r => r.status === 'failed' || r.status === 'error')
            .map(r => r.test_name);`,
    },
    {
        name: 'JS: 总结页的可重测清单里没有「跳过」的项',
        file: 'app.js',
        summary: true,
        from: `        const retestableItemNames = computed(() =>
            resultList.value
                .filter(r => r.status === 'failed' || r.status === 'error'
                         || r.status === 'skipped')
                .map(r => r.name)
        );`,
        to: `        const retestableItemNames = computed(() =>
            resultList.value
                .filter(r => r.status === 'failed' || r.status === 'error')
                .map(r => r.name)
        );`,
    },
    {
        name: 'JS: failedCount 又把「出错」算进去 (通过率把 error 数两遍)',
        file: 'app.js',
        summary: true,
        from: `        const failedCount = computed(() =>
            resultList.value.filter(r => r.status === 'failed').length
        );`,
        to: `        const failedCount = computed(() =>
            resultList.value.filter(r => r.status === 'failed' || r.status === 'error').length
        );`,
    },
    {
        name: 'JS: 历史页重测打的是**当前轮**而不是被点开的那条记录',
        file: 'app.js',
        from: `            await retestRun(detail.id, names, \`记录 #\${detail.id}\`, seed);`,
        to: `            await retestRun(runId.value, names, \`记录 #\${detail.id}\`, seed);`,
    },
    {
        name: 'JS: 重测时不检查「有没有别的测试在跑」(允许插队)',
        file: 'app.js',
        from: `            if (isRunning.value) {
                alert('当前有测试在运行, 结束后才能重测。');
                return;
            }`,
        to: `            /* 不检查了 */`,
    },
    {
        name: 'JS: 启动失败时不解锁界面 (永久卡在「运行中」)',
        file: 'app.js',
        from: `                if (!data.ok) {
                    alert('重测启动失败: ' + data.error);
                    isRunning.value = false;
                    window._retesting = false;
                }`,
        to: `                if (!data.ok) {
                    alert('重测启动失败: ' + data.error);
                }`,
    },
    {
        name: 'JS: 合并完成后不刷新历史列表 (那条记录不跳到最上面)',
        file: 'app.js',
        from: `                    await loadHistory();
                    // 正展开着它的话, 详情也要跟着刷新(逐项状态/计数/角标)。
                    if (expandedId.value === pid) {
                        await loadHistoryDetail(pid);
                    }`,
        to: `                    /* 不刷新了 */`,
    },
    {
        name: 'JS: 合并完成后报告按钮指向重测轮 (不是合并版)',
        file: 'app.js',
        from: `                    mergedParent.value = pid;
                    reportRunId.value = pid;`,
        to: `                    mergedParent.value = pid;
                    reportRunId.value = runId.value;`,
    },
    {
        name: 'HTML: 详情头的操作区没了 (按钮不在右边)',
        file: 'index.html',
        from: `<div class="history-detail-actions">`,
        to: `<div class="history-detail-actions-off">`,
    },
    {
        name: 'HTML: 按钮没有「有失败项才出现」的守卫 (全过了也给重测)',
        file: 'index.html',
        from: `<button v-if="!historyDetail.mini_of && historyRetestableNames(historyDetail).length"`,
        to: `<button v-if="!historyDetail.mini_of"`,
    },
    {
        name: 'HTML: 总结页的重测按钮没有守卫 (全过了也给重测)',
        file: 'index.html',
        summary: true,
        from: `                        v-if="hasRetestableItems"`,
        to: `                        v-if="true"`,
    },
    {
        name: 'HTML: 总结页按钮上的数字写死 (跳过项没算进去)',
        file: 'index.html',
        summary: true,
        from: `>🔂 重测失败/跳过项 ({{ retestableCount }})</button>`,
        to: `>🔂 重测失败/跳过项 ({{ failedCount }})</button>`,
    },
    {
        name: 'HTML: 按钮不再禁用 (跑着的时候也能点)',
        file: 'index.html',
        from: `                                                        :disabled="isRunning"`,
        to: `                                                        :disabled="false"`,
    },
    {
        name: 'HTML: 逐项「第 N 次测试通过」角标没了',
        file: 'index.html',
        from: `<span v-if="r.attempt && r.attempt > 1" class="attempt-tag">`,
        to: `<span v-if="false" class="attempt-tag">`,
    },
    {
        name: 'HTML: 列表里又把「重测轮」当成一条记录标出来了 (列表又乱了)',
        file: 'index.html',
        from: `                                <td class="history-id">
                                    <span class="history-mark">{{ expandedId === h.id ? '▾' : '▸' }}</span>{{ h.id }}
                                </td>`,
        to: `                                <td class="history-id">
                                    <span class="history-mark">{{ expandedId === h.id ? '▾' : '▸' }}</span>{{ h.id }}
                                    <span v-if="h.mini_of" class="mini-tag">重测轮</span>
                                </td>`,
    },
    {
        name: 'HTML: 详情里的「重测记录」那块没了 (重测过程看不见了)',
        file: 'index.html',
        from: `<div class="retest-rounds"`,
        to: `<div class="retest-rounds-off"`,
    },
    {
        name: 'JS: 前端不再滤掉重测轮 (后端没重启时列表又是四行)',
        file: 'app.js',
        from: `            history.value = (Array.isArray(rows) ? rows : [])
                .filter(x => x && !x.mini_of);`,
        to: `            history.value = Array.isArray(rows) ? rows : [];`,
    },
    {
        name: 'HTML: 角标又写回「第 N 次重测通过」(工人会读成「这条是第 N 次重测」)',
        file: 'index.html',
        from: `重测 {{ h.retest_count }} 轮{{ h.status === 'passed' ? '后通过' : '后未过' }}`,
        to: `第 {{ h.retest_count }} 次重测通过`,
    },
];

/* 每次跑用全新的临时目录(mkdtemp), 不删任何东西 —— 免得误删。 */
const TMP = fs.mkdtempSync(path.join(os.tmpdir(), 'mut_retest_'));
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
    /* 带 summary 标记的变异: 重测那套断言盯不到通过率口径, 两个测试都跑,
     * 任意一个变红就算守住了。 */
    const tests = mut.summary ? [TEST, TEST_SUMMARY] : [TEST];
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
