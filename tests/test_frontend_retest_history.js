/* 回归测试: 历史记录里的「重测失败项」按钮, 以及合并完成后的界面反应。
 *
 * 需求(工人原话):「测试失败的历史记录能不能点开之后在右边显示一个重新测试的
 * 按钮, 专门测试失败项, 如果通过了就更新报告和记录为通过, 和之前通过项目的
 * 报告合并, 历史记录刷新到最上面, 标记上第几次测试通过。」
 *
 * 这个测试盯住的是**前端这一半**:
 *   ① 按钮摆在详情的**右边**, 不是藏在 PDF 那一行里
 *   ② 它和总结页那个「重测失败项」是**同一个**接口、同一个请求体形状 ——
 *      区别只有目标轮次(总结页 = 当前这一轮, 历史页 = 被点开的那条记录)。
 *      两条路要是一套一套地写, 迟早会走歪 —— 这正是要钉住的地方。
 *   ③ 有测试在跑 → 不许插队重测 (runner 是单例)
 *   ④ 工人点了「取消」→ 不发请求
 *   ⑤ 启动失败 → 界面解锁 (不然就永久卡在「运行中」)
 *   ⑥ 注册表里已经没了的项 → 要**说出来**(别让工人以为少测了)
 *   ⑦ 合并完成(parent_merged)→ 历史列表刷新、详情刷新、
 *      报告按钮指向**合并后**的那一份
 *
 * 跑法: node tests/test_frontend_retest_history.js
 */
'use strict';

const fs = require('fs');
const path = require('path');
const Module = require('module');

/* 同 test_frontend_logarea.js: 给 AGING_* 换个路径就能对着别的副本跑 ——
 * 用来验证「这个测试真的会红」: 把代码改坏, 断言必须失败。 */
const STATIC_DIR = process.env.AGING_STATIC_DIR
    || path.join(__dirname, '..', 'static');
/* 让 _vue_harness 也读副本里的 app.js —— 必须在 require 之前设好
 * (模块加载时就取值了)。 */
process.env.AGING_APP_JS = process.env.AGING_APP_JS
    || path.join(STATIC_DIR, 'app.js');

const { loadApp, makeChecker } = require('./_vue_harness');

const { check, section, finish } = makeChecker();

const INDEX_HTML = path.join(STATIC_DIR, 'index.html');
const TOOLS_DIR = process.env.VUE_TOOLS_DIR || '/home/User/.claude/vue-tools';

// ── 一条「没全过」的记录: 两项过了, 一项失败, 一项出错, 一项**跳过** ──
//
// ⚠️ skipped 那项是**这次需求的主角**: 跳过的项也是「没测成」(没接显示器 /
// 没插网线 / 卡槽是空的), 补上料、补上环境之后就该能重测 —— 所以它跟
// 失败项走**同一个**重测入口(§7.7)。
//
// `summary.total` 还是 4 —— §9.2 的口径是 total = passed+failed+error,
// **skipped 不计入**。
const DETAIL = {
    id: 88,
    sn: 'SN-RETEST-01',
    operator: '产线甲',
    status: 'failed',
    started_at: 100,
    finished_at: 200,
    pdf_path: '/reports/pdf/SN-RETEST-01_20240101_120000.pdf',
    retest_count: 0,
    summary: { total: 4, passed: 2, failed: 1, error: 1 },
    results: [
        { test_name: 'cpu', status: 'passed', duration_sec: 1.2,
          metrics: {}, error: null, attempt: 1 },
        { test_name: 'usb', status: 'passed', duration_sec: 0.3,
          metrics: {}, error: null, attempt: 1 },
        { test_name: 'ethernet', status: 'failed', duration_sec: 5.0,
          metrics: {}, error: '未插网线', attempt: 1 },
        { test_name: 'tfcard', status: 'error', duration_sec: 0.1,
          metrics: {}, error: '这一项没测成', attempt: 1 },
        { test_name: 'wifi', status: 'skipped', duration_sec: 0.0,
          metrics: {}, error: '未扫描到任何 WiFi 热点', attempt: 1 },
    ],
};

const RUNS = [
    { id: 88, sn: 'SN-RETEST-01', status: 'failed', started_at: 100,
      finished_at: 200, retest_count: 0,
      summary: { total: 4, passed: 2, failed: 1, error: 1 } },
];

function tick(n = 10) {
    return new Promise(r => setTimeout(r, n));
}

/** 一个好用的 fetch 桩: 记录所有请求, 行为可配。 */
function makeFetch(opts = {}) {
    const calls = [];
    const stub = (url, init) => {
        calls.push({ url, init, body: init && init.body ? JSON.parse(init.body) : null });
        if (url === '/api/config') {
            // ⚠️ tests 必须是**数组** —— app.js 里 loadConfig 会 data.tests.map(),
            //    给个对象它会当场抛, onMounted 也就走不到 connectWS, WS 连不上。
            return Promise.resolve({
                json: () => Promise.resolve({
                    tests: [], recommended_order: [], defaults: {},
                    report: { operator: '产线甲' },
                }),
            });
        }
        if (url === '/api/deps/check') {
            return Promise.resolve({ json: () => Promise.resolve({ ok: true }) });
        }
        if (url === '/api/history') {
            return Promise.resolve({ json: () => Promise.resolve(opts.history || RUNS) });
        }
        if (/^\/api\/history\/\d+$/.test(url)) {
            const id = Number(url.split('/').pop());
            return Promise.resolve({ json: () => Promise.resolve({ ...DETAIL, id }) });
        }
        if (url === '/api/test/retest') {
            return Promise.resolve({
                json: () => Promise.resolve(opts.retestResp || { ok: true, run_id: 900 }),
            });
        }
        return Promise.reject(new Error('不该请求 ' + url));
    };
    stub.calls = calls;
    return stub;
}

async function main() {
    // ══════════════════════════════════════════════════════════
    section('【1】historyRetestableNames 挑失败/出错/**跳过**的项');
    {
        const a = loadApp({ fetch: makeFetch() });
        const names = a.historyRetestableNames(DETAIL);
        check(JSON.stringify(names) === JSON.stringify(['ethernet', 'tfcard', 'wifi']),
              '挑出失败 + 出错 + **跳过**的那三项', JSON.stringify(names));
        check(names.includes('wifi'),
              '**跳过的项也能重测**(它也是没测成, 补上料就能测)', JSON.stringify(names));
        check(!names.includes('cpu') && !names.includes('usb'),
              '已经过了的项不重测 (免得白跑一遍)', JSON.stringify(names));

        const allPassed = { results: [{ test_name: 'cpu', status: 'passed' }] };
        check(a.historyRetestableNames(allPassed).length === 0,
              '全过了 → 没有可重测的项 (按钮就不该出现)');

        check(a.historyRetestableNames(null).length === 0,
              '详情还没加载 → 也不该报错');
    }

    // ══════════════════════════════════════════════════════════
    section('【2】历史页点「重测失败/跳过项」→ 打的是**那条记录**');
    {
        const f = makeFetch();
        const a = loadApp({ fetch: f });
        await a.retestHistoryRun(DETAIL);
        const call = f.calls.find(c => c.url === '/api/test/retest');
        check(!!call, '发了 /api/test/retest', f.calls.map(c => c.url));
        if (call) {
            check(call.init.method === 'POST', '是 POST', call.init.method);
            check(call.body.run_id === 88,
                  '目标是**被点开的那条记录**(不是当前轮)', call.body.run_id);
            check(JSON.stringify(call.body.test_names)
                  === JSON.stringify(['ethernet', 'tfcard', 'wifi']),
                  '只重测失败/出错/**跳过**的项', JSON.stringify(call.body.test_names));
            check(call.body.test_names.includes('wifi'),
                  '**跳过的项也发过去了**', JSON.stringify(call.body.test_names));
            check(JSON.stringify(Object.keys(call.body).sort())
                  === JSON.stringify(['run_id', 'test_names']),
                  '请求体只有这两个字段', JSON.stringify(Object.keys(call.body)));
        }
    }

    // ══════════════════════════════════════════════════════════
    section('【3】和总结页那个按钮是**同一个**功能(同一个接口、同一个体形状)');
    {
        const f = makeFetch();
        const a = loadApp({ fetch: f });
        // 总结页: 目标 = 当前这一轮
        a.runId.value = 77;
        a.resultList.value = [
            { name: 'cpu', status: 'passed' },
            { name: 'ethernet', status: 'failed' },
            { name: 'wifi', status: 'skipped' },
        ];
        await a.retestFailedOrSkipped();
        const sumCall = f.calls.find(c => c.url === '/api/test/retest');
        check(!!sumCall, '总结页也打 /api/test/retest');
        if (sumCall) {
            check(sumCall.body.run_id === 77, '目标是当前这一轮', sumCall.body.run_id);
            check(JSON.stringify(sumCall.body.test_names)
                  === JSON.stringify(['ethernet', 'wifi']),
                  '总结页也重测**跳过**的项 (跟历史页一套口径)',
                  JSON.stringify(sumCall.body.test_names));
            // 两条路的请求体字段必须一模一样 —— 后端就认这一个形状
            const f2 = makeFetch();
            const b = loadApp({ fetch: f2 });
            await b.retestHistoryRun(DETAIL);
            const histCall = f2.calls.find(c => c.url === '/api/test/retest');
            check(JSON.stringify(Object.keys(sumCall.body).sort())
                  === JSON.stringify(Object.keys(histCall.body).sort()),
                  '两条路的请求体字段完全一样 (后端只认这一套)',
                  JSON.stringify(Object.keys(sumCall.body))
                  + ' vs ' + JSON.stringify(Object.keys(histCall.body)));
        }
    }

    // ══════════════════════════════════════════════════════════
    section('【4】有测试在跑 → 不许插队 (runner 是单例)');
    {
        const f = makeFetch();
        const a = loadApp({ fetch: f });
        a.isRunning.value = true;
        await a.retestHistoryRun(DETAIL);
        check(!f.calls.find(c => c.url === '/api/test/retest'),
              '没有发请求', f.calls.map(c => c.url));
        check(a.__alerts.some(m => m.includes('有测试在运行')),
              '弹了提示告诉工人为什么不能重测', a.__alerts);
        check(a.isRunning.value === true, '没有把正在跑的那一轮的标志弄乱');
    }

    // ══════════════════════════════════════════════════════════
    section('【5】工人点了「取消」→ 不发请求');
    {
        const f = makeFetch();
        const a = loadApp({ fetch: f, confirm: () => false });
        await a.retestHistoryRun(DETAIL);
        check(!f.calls.find(c => c.url === '/api/test/retest'),
              '没发请求', f.calls.map(c => c.url));
        check(a.isRunning.value === false, '也没把界面标成「运行中」');
    }

    // ══════════════════════════════════════════════════════════
    section('【6】启动失败 → 界面要解锁 (不然永久卡在「运行中」)');
    {
        const f = makeFetch({ retestResp: { ok: false, error: '已有测试在运行' } });
        const a = loadApp({ fetch: f });
        await a.retestHistoryRun(DETAIL);
        check(a.isRunning.value === false, 'isRunning 复位了', a.isRunning.value);
        check(a.__alerts.some(m => m.includes('重测启动失败')),
              '弹了失败原因', a.__alerts);
    }

    // ══════════════════════════════════════════════════════════
    section('【7】有项已不在册 → 要说出来 (别让工人以为少测了)');
    {
        const f = makeFetch({ retestResp: { ok: true, dropped_tests: ['old_test'] } });
        const a = loadApp({ fetch: f });
        await a.retestHistoryRun(DETAIL);
        check(a.__alerts.some(m => m.includes('old_test')),
              '被跳过的项报出来了', a.__alerts);
        check(a.isRunning.value === true,
              '启动成功了 → 保持「运行中」(等 WS 事件来解锁)');
    }

    // ══════════════════════════════════════════════════════════
    section('【8】重测过程要铺进向导页 (否则工人看到个空白页)');
    {
        const f = makeFetch();
        const a = loadApp({ fetch: f });
        await a.retestHistoryRun(DETAIL);
        check(a.resultList.value.length === 5,
              '向导页列出了那条记录的全部五项', a.resultList.value.length);
        const byName = {};
        for (const it of a.resultList.value) byName[it.name] = it;
        check(byName.ethernet && byName.ethernet.status === 'pending',
              '要重测的那项摆成「等待」', byName.ethernet && byName.ethernet.status);
        check(byName.wifi && byName.wifi.status === 'pending',
              '**跳过的项**也摆成「等待」(它这轮要重测)',
              byName.wifi && byName.wifi.status);
        check(byName.cpu && byName.cpu.status === 'passed',
              '原来通过的项保持通过 (报告要合并, 不是重测)',
              byName.cpu && byName.cpu.status);
        check(a.view.value === 'wizard', '切到向导页看重测过程', a.view.value);
    }

    // ══════════════════════════════════════════════════════════
    section('【9】合并完成(parent_merged)→ 列表刷新 + 详情刷新 + 报告指对');
    {
        const f = makeFetch();
        const a = loadApp({ fetch: f, mount: true });

        await a.goHistory();
        // ⚠️ onMounted 里 connectWS() 排在两个 await(loadConfig/loadDeps) **后面**,
        //    所以刚 loadApp 完那一瞬间 socket 还没连上 —— 得等一拍再看。
        await tick(10);
        check(a.__sockets.length === 1, 'WS 连上了', a.__sockets.length);

        await a.toggleHistoryDetail(88);
        check(a.expandedId.value === 88, '先展开 88');
        const historyCallsBefore = f.calls.filter(c => c.url === '/api/history').length;
        const detailCallsBefore = f.calls.filter(c => c.url === '/api/history/88').length;

        // ⚠️ 事件顺序是**承重的**: 重测轮自己那份 PDF 先到(report_ready),
        //    合并完成(parent_merged)后到。界面必须以**后**到的合并版为准 ——
        //    顺序反了的话, 报告按钮会指向只含被重测那两项的重测轮 PDF,
        //    工人归档的就是一份内容对不上的纸面件。
        await a.__sockets[0].onmessage({
            data: JSON.stringify({
                type: 'report_ready',
                run_id: 900,
                path: '/reports/pdf/SN-RETEST-01_20240101_130000.pdf',
            }),
        });
        await tick(10);
        check(a.reportRunId.value === 900, '先收到重测轮自己那份', a.reportRunId.value);

        await a.__sockets[0].onmessage({
            data: JSON.stringify({
                type: 'parent_merged',
                parent_run_id: 88,
                run_id: 88,
                status: 'passed',
                summary: { total: 4, passed: 4, failed: 0, error: 0 },
                pdf_path: '/reports/pdf/SN-RETEST-01_20240101_120000.pdf',
                retest_count: 1,
            }),
        });
        await tick(30);

        check(a.mergedParent.value === 88, '记住了合并到哪条记录', a.mergedParent.value);
        check(a.reportRunId.value === 88,
              '报告按钮指向**合并后**的那条记录', a.reportRunId.value);
        check(a.reportPath.value.includes('SN-RETEST-01'),
              '报告路径是合并版那一份', a.reportPath.value);

        const historyCallsAfter = f.calls.filter(c => c.url === '/api/history').length;
        check(historyCallsAfter > historyCallsBefore,
              '历史列表重新拉了 (合并过的那条要跳到最上面)',
              historyCallsBefore + ' → ' + historyCallsAfter);
        const detailCallsAfter = f.calls.filter(c => c.url === '/api/history/88').length;
        check(detailCallsAfter > detailCallsBefore,
              '正展开着的那条详情也刷新了 (角标/逐项状态要跟着变)',
              detailCallsBefore + ' → ' + detailCallsAfter);
    }

    // ══════════════════════════════════════════════════════════
    section('【10】模板: 按钮在**右边**, 有守卫, 跑着的时候禁用');
    {
        const html = fs.readFileSync(INDEX_HTML, 'utf8');

        // 模板结构用真 Vue 编译器在 AST 上断 (同 test_frontend_template.js)
        const origResolve = Module._resolveFilename;
        Module._resolveFilename = function (request, ...rest) {
            if (request === '@vue/compiler-dom') {
                try {
                    return origResolve.call(this, request, ...rest);
                } catch (e) {
                    return origResolve.call(this,
                        path.join(TOOLS_DIR, 'node_modules', request), ...rest);
                }
            }
            return origResolve.call(this, request, ...rest);
        };
        let compiler = null;
        try {
            compiler = require('@vue/compiler-dom');
        } catch (e) {
            compiler = null;
        }
        Module._resolveFilename = origResolve;

        check(!!compiler, '拿得到 Vue 模板编译器 (拿不到就跳过结构断言)');
        if (compiler) {
            const ast = compiler.parse(html, { onError() {} });
            const all = [];
            (function walk(node) {
                if (!node || typeof node !== 'object') return;
                if (Array.isArray(node)) { node.forEach(walk); return; }
                if (node.type === 1) all.push(node);
                (node.children || []).forEach(walk);
                (node.branches || []).forEach(walk);
            })(ast);

            const dirText = (node, name, arg) => {
                for (const p of (node.props || [])) {
                    if (p.type !== 7 || p.name !== name) continue;
                    if (arg !== undefined && (!p.arg || p.arg.content !== arg)) continue;
                    return p.exp ? String(p.exp.content || '') : null;
                }
                return null;
            };
            const hasClass = (node, cls) => {
                const c = (node.props || []).find(
                    p => p.type === 6 && p.name === 'class');
                if (!c || !c.value) return false;
                return String(c.value.content || '').split(/\s+/).includes(cls);
            };
            // 找某个节点的**后代** —— 「逐项结果旁边的角标」跟「重测记录里
            // 那个角标」长得一样, 只在整棵树里 find 第一个的话, 改掉逐项
            // 那个断言照样绿(踩过: 重测记录块里也有一个 attempt-tag)。
            const descendants = (node, pred) => {
                const out = [];
                (function walk(n) {
                    if (!n || typeof n !== 'object') return;
                    if (Array.isArray(n)) { n.forEach(walk); return; }
                    if (n.type === 1 && pred(n)) out.push(n);
                    (n.children || []).forEach(walk);
                    (n.branches || []).forEach(walk);
                })(node);
                return out;
            };

            const actions = all.find(n => hasClass(n, 'history-detail-actions'));
            check(!!actions, '详情右上角有操作区 .history-detail-actions');

            const btn = actions && actions.children.find(
                n => n.type === 1 && n.tag === 'button');
            check(!!btn, '操作区里有按钮');
            if (btn) {
                const vIf = dirText(btn, 'if');
                check(!!vIf && vIf.includes('historyRetestableNames'),
                      '按钮只在**有失败/出错/跳过的项**时才出现', String(vIf));
                check(!!vIf && vIf.includes('mini_of'),
                      '重测轮不给这个按钮 (归档对象始终是最初那一轮)', String(vIf));
                const click = dirText(btn, 'on', 'click');
                check(!!click && click.includes('retestHistoryRun'),
                      '点的是 retestHistoryRun', String(click));
                const disabled = dirText(btn, 'bind', 'disabled');
                check(!!disabled && disabled.includes('isRunning'),
                      '跑着的时候按钮禁用', String(disabled));
            }

            // 「第 N 次测试通过」角标 —— **在逐项结果列表里**那个
            // (重测记录块里也有一个同名的, 别认错)
            const resultsList = all.find(n => hasClass(n, 'results-list'));
            check(!!resultsList, '逐项结果列表 .results-list 在');
            const attemptTag = resultsList
                && descendants(resultsList, n => hasClass(n, 'attempt-tag'))[0];
            check(!!attemptTag, '逐项结果旁边有「第 N 次」角标');
            const atIf = attemptTag && dirText(attemptTag, 'if');
            check(!!atIf && atIf.includes('attempt'),
                  '角标只在 attempt > 1 时出现', String(atIf));

            // 历史行上的重测角标
            const retestTag = all.find(n => hasClass(n, 'retest-tag'));
            check(!!retestTag, '历史行上有重测角标');
            const rtIf = retestTag && dirText(retestTag, 'if');
            check(!!rtIf && rtIf.includes('retest_count'),
                  '角标按 retest_count 显示', String(rtIf));

            // 总结页的合并横幅
            check(html.includes('mergedParent'),
                  '总结页有「已合并到记录 #」的横幅');
        }
    }

    // ══════════════════════════════════════════════════════════
    section('【11】历史列表: **一行 = 一条归档记录**, 重测轮不单独占一行');
    /* 工人原话:「我只重测了两次的记录, 有四个记录都显示有重测」——
     *
     * 根因: 重测轮(mini-run)自己也是 runs 表里的一行, 而历史列表把**每一行**
     * 都列了出来。于是重测两次 = 2 条原记录 + 2 条重测轮 = **四条**带
     * 「重测」字样的行 —— 看着像"四条记录都重测过"。
     *
     * 改法: 列表只列归档记录(后端过滤掉重测轮), 重测轮的信息挂到它那一轮
     * 记录的**详情**里。所以这一节守两件事:
     *   ① 列表里**不许**再出现「重测轮」那种行内标记(重测轮已经不列了)
     *   ② 详情里要有「重测记录」那一块 —— 轮数、每轮重测了哪几项、日志
     */
    {
        const html = fs.readFileSync(INDEX_HTML, 'utf8');

        // ① 列表里不再有「重测轮」标记 —— 重测轮压根不列了
        check(!html.includes('mini-tag'),
              '历史列表里没有「重测轮」标记了(那一行根本不该出现)');
        check(!/class="mini-tag"/.test(html),
              '模板里没有 .mini-tag 这个类');

        // ② 详情里有「重测记录」那一块
        check(html.includes('class="retest-rounds"'),
              '详情里有「重测记录」区块 (.retest-rounds)');
        check(html.includes('historyDetail.retest_runs'),
              '它读的是 historyDetail.retest_runs(后端给的重测轮列表)');
        check(html.includes('重测记录'),
              '标题上写着「重测记录」');
        // 每一轮: 序号 + 时间 + 结果 + 每项的状态 + 日志
        check(html.includes('mr.started_at'),
              '每一轮显示时间');
        check(html.includes('statusText(mr.status)'),
              '每一轮显示那一轮的结果');
        check(html.includes('rr.test_name'),
              '每一轮列出重测了哪几项');
        check(html.includes('rr.log'),
              '那一轮的日志能在界面直接翻(不用去开 PDF)');
    }

    // ══════════════════════════════════════════════════════════
    section('【12】历史行的角标措辞: 主语是**这一条记录**');
    /* 原来写「第 N 次重测通过」—— 工人会读成「这条记录是第 N 次重测」。
     * 现在写「重测 N 轮后通过」: 主语是这条记录, N 是轮数。
     * 跟「重测轮」那种行混在一起时, 原来的措辞正是让人看不懂的原因。 */
    {
        const html = fs.readFileSync(INDEX_HTML, 'utf8');
        check(html.includes('重测 {{ h.retest_count }} 轮'),
              '历史行的角标写「重测 N 轮…」(主语是这一条记录)', html.slice(0, 0));
        check(!html.includes('第 \' + h.retest_count + \' 次重测通过'),
              '不再写「第 N 次重测通过」(会被读成「这条是第 N 次重测」)');
        check(html.includes('后通过') && html.includes('后未过'),
              '角标分得清「重测后通过」和「重测后未过」');
    }

    // ══════════════════════════════════════════════════════════
    section('【13】前端**自己**也滤掉重测轮(不用等后端重启)');
    /* `static/` 是热路径, `server/*.py` 不是 —— 后端那道过滤要等产线重启
     * 服务才生效。前端这一道是**同一套规则**, 而且刷新页面就生效: 工人
     * 当下就能看到「一行 = 一条记录」, 不用等。 */
    {
        const runs = [
            { id: 88, sn: 'SN-A', status: 'passed', retest_count: 1,
              started_at: 100, finished_at: 200 },
            { id: 90, sn: 'SN-A', status: 'passed', mini_of: 88,
              started_at: 150, finished_at: 160 },   // ← 重测轮, 不该出现
            { id: 89, sn: 'SN-B', status: 'passed', retest_count: 0,
              started_at: 90, finished_at: 95 },
        ];
        const f = makeFetch({ history: runs });
        const a = loadApp({ fetch: f });
        await a.loadHistory();
        const ids = a.history.value.map(r => r.id);
        check(!ids.includes(90),
              '列表里没有重测轮那一行', ids);
        check(ids.includes(88) && ids.includes(89),
              '归档记录都还在', ids);
        check(a.history.value.length === 2,
              '列表正好两行', ids);
    }

    return finish();
}

main().then((code) => process.exit(code));
