/* 回归测试: 总结页要把「测了什么 / 没测什么」摆在明面上 (§9.2)。
 *
 * 起因: 后端把 total 的口径改成了「实际执行并出结论的项数」(skipped /
 * stopped 不计入), 但界面还停留在「几通过 / 几失败 / 几跳过」——
 * 光看这三个数, 工人**分不清**这两种情况:
 *
 *     A: 勾了 8 项, 8 项全测过, 全通过
 *     B: 勾了 10 项, 只测了 8 项(2 项没测), 这 8 项全通过
 *
 * 两种情况下屏幕上都是「8 通过 / 0 失败 / 0 跳过」—— B 却是一台**没测全**
 * 的机器, 拿着它出货就是漏检。所以总结页必须自己把「已测 M / 共勾选 K」
 * 和「哪几项没测」摆出来。
 *
 * 另外还有个老坑: 跑测期间点「← 返回配置」会把逐项列表清空, 而后端照跑
 * 不误 → run_finished 到达时总结页是一张「0 通过 / 0 失败 / 0 跳过」的空页,
 * 工人以为机器一项都没测。现在跑测期间不许回配置页, 跑完还要按快照重建
 * 一次列表。
 *
 * 跑法: node tests/test_frontend_summary.js
 */
'use strict';

const fs = require('fs');
const path = require('path');
const { loadApp, makeChecker } = require('./_vue_harness');

const { check, section, finish } = makeChecker();

const INDEX_HTML = path.join(__dirname, '..', 'static', 'index.html');

/** 让挂起的 promise 走完 (WS 事件处理是 async 的) */
const tick = (ms) => new Promise(r => setTimeout(r, ms || 5));

const TEST_LIST = [
    { name: 'cpu', display_name: 'CPU 压力' },
    { name: 'gpu', display_name: 'GPU 渲染' },
    { name: 'usb', display_name: 'USB 接口' },
    { name: 'wifi', display_name: 'WiFi 无线' },
];

/** 一份「跑完了」的快照 —— run_finished 之后 syncStatus 拉回来的就是它 */
const ENDED_STATUS = {
    run_id: 1,
    status: 'passed',
    current_test: null,
    started_at: 1000,
    finished_at: 2000,
    mode: 'full',
    parent_run_id: null,
    test_queue: TEST_LIST.map(t => t.name),
    pdf_path: null,
    sn: 'SN-TEST-0001',
    error: null,
    waiting_operator: null,
    results: {
        cpu: { status: 'passed', metrics: {}, error: null },
        gpu: { status: 'passed', metrics: {}, error: null },
        usb: { status: 'skipped', metrics: {}, error: '未检测到任何 USB 设备' },
        wifi: { status: 'stopped', metrics: {}, error: '用户中途停止, 本项未执行' },
    },
};

function fetchStub(url) {
    if (url === '/api/config') {
        return Promise.resolve({
            json: () => Promise.resolve({
                sn: 'SN-TEST-0001',
                tests: TEST_LIST,
                defaults: {},
                recommended_order: TEST_LIST.map(t => t.name),
                report: {},
            }),
        });
    }
    if (url === '/api/test/status') {
        return Promise.resolve({ json: () => Promise.resolve(ENDED_STATUS) });
    }
    return Promise.resolve({ json: () => Promise.resolve({}) });
}

/**
 * 起一个「跟真浏览器一样」的 app: 跑 onMounted(拉配置 + 连 WS)。
 * 不这么起, testList 是空的 → 显示名退化成英文 name, 而真机上工人看到的是
 * 「WiFi 无线」这种中文名 —— 那正是未测项 banner 上要印的东西。
 */
async function makeApp() {
    const a = loadApp({ mount: true, fetch: fetchStub });
    await tick(20);                       // 等 onMounted 的异步启动走完
    return a;
}

function snap(over) {
    return Object.assign({
        run_id: 1,
        status: 'running',
        current_test: null,
        started_at: 1000,
        finished_at: 0,
        mode: 'full',
        parent_run_id: null,
        test_queue: TEST_LIST.map(t => t.name),
        pdf_path: null,
        sn: 'SN-TEST-0001',
        error: null,
        waiting_operator: null,
        results: {},
    }, over || {});
}

async function main() {
    // ══════════════════════════════════════════════════════════
    section('【1】通过率按「已测」算, 不按「勾选」算');
    {
        const a = await makeApp();
        a.applySnapshot(snap({
            status: 'running',
            results: {
                cpu: { status: 'passed', metrics: {}, error: null },
                gpu: { status: 'passed', metrics: {}, error: null },
                usb: { status: 'passed', metrics: {}, error: null },
                wifi: { status: 'passed', metrics: {}, error: null },
            },
        }));
        check(a.passedCount.value === 4, '通过 4 项', a.passedCount.value);
        check(a.executedCount.value === 4, '已测 4 项', a.executedCount.value);
        check(a.plannedCount.value === 4, '共勾选 4 项', a.plannedCount.value);
        check(a.passRateText.value === '4/4', '通过率 4/4', a.passRateText.value);
    }

    // ══════════════════════════════════════════════════════════
    section('【2】有跳过 / 有没跑到 → 已测与勾选数要分开, 未测项点名');
    {
        const a = await makeApp();
        a.applySnapshot(snap({
            status: 'running',
            results: {
                cpu: { status: 'passed', metrics: {}, error: null },
                gpu: { status: 'passed', metrics: {}, error: null },
                usb: { status: 'failed', metrics: {}, error: '读速度不达标' },
                wifi: { status: 'skipped', metrics: {}, error: '没插网卡' },
            },
        }));
        check(a.executedCount.value === 3, '已测 3 项 (跳过的不算)',
              a.executedCount.value);
        check(a.plannedCount.value === 4, '共勾选 4 项', a.plannedCount.value);
        check(a.passRateText.value === '2/3', '通过率 2/3 (分母是已测)',
              a.passRateText.value);
        check(a.notTestedItems.value.length === 1, '未测项 1 个',
              JSON.stringify(a.notTestedItems.value));
        check(a.notTestedItems.value[0].name === 'WiFi 无线',
              '未测项写的是显示名 (工人认得的名字)',
              a.notTestedItems.value[0].name);
        check(a.notTestedItems.value[0].status === 'skipped',
              '未测项带着状态', a.notTestedItems.value[0].status);
    }

    // ══════════════════════════════════════════════════════════
    section('【3】stopped (没跑到) 也算「未测」, 且跟 skipped 分得开');
    {
        const a = await makeApp();
        a.applySnapshot(snap({
            status: 'running',
            results: {
                cpu: { status: 'passed', metrics: {}, error: null },
                gpu: { status: 'stopped', metrics: {}, error: '用户中途停止' },
                usb: { status: 'skipped', metrics: {}, error: '没插网卡' },
                wifi: { status: 'pending', metrics: {}, error: null },
            },
        }));
        check(a.notTestedItems.value.length === 2, '未测项 2 个',
              JSON.stringify(a.notTestedItems.value));
        const st = a.notTestedItems.value.map(x => x.status).sort().join(',');
        check(st === 'skipped,stopped', 'skipped 和 stopped 分得开', st);
        check(a.notTestedText.value.includes('GPU 渲染')
              && a.notTestedText.value.includes('USB 接口'),
              '未测项名字拼成一句话', a.notTestedText.value);
        // pending (还在排队) 不算「未测」—— 它还没轮到
        check(!a.notTestedText.value.includes('WiFi 无线'),
              'pending 项不算未测', a.notTestedText.value);
    }

    // ══════════════════════════════════════════════════════════
    section('【4】一项都没执行 → 通过率是「—」, 不是 0/0');
    {
        const a = await makeApp();
        a.applySnapshot(snap({
            status: 'running',
            results: {
                cpu: { status: 'skipped', metrics: {}, error: '跳过' },
                gpu: { status: 'skipped', metrics: {}, error: '跳过' },
                usb: { status: 'skipped', metrics: {}, error: '跳过' },
                wifi: { status: 'skipped', metrics: {}, error: '跳过' },
            },
        }));
        check(a.executedCount.value === 0, '已测 0 项', a.executedCount.value);
        check(a.passRateText.value === '—', '通过率显示「—」',
              a.passRateText.value);
    }

    // ══════════════════════════════════════════════════════════
    section('【5】整轮 skipped (一项都没实际执行) 要有自己的说法');
    {
        const a = await makeApp();
        a.applySnapshot(snap({
            status: 'skipped',
            run_id: 1,
            finished_at: 2000,
            results: {
                cpu: { status: 'skipped', metrics: {}, error: '跳过' },
            },
        }));
        check(a.lastStatus.value === 'skipped', 'lastStatus = skipped',
              a.lastStatus.value);
        check(a.summaryEmoji.value === '⏭', '图标是 ⏭', a.summaryEmoji.value);
        check(a.summaryStatusText.value === '本次未实际执行任何测试项',
              '文案说清「没实际执行」', a.summaryStatusText.value);
    }

    // ══════════════════════════════════════════════════════════
    section('【6】跑测期间不许「返回配置」(会把逐项列表清空 → 空总结页)');
    {
        const a = await makeApp();
        a.applySnapshot(snap({
            status: 'running',
            results: { cpu: { status: 'passed', metrics: {}, error: null } },
        }));
        check(a.isRunning.value === true, '在跑 (isRunning=true)');
        const before = a.resultList.value.length;
        a.goConfig();
        check(a.resultList.value.length === before,
              '逐项列表没被清空', before + ' → ' + a.resultList.value.length);
        check(a.view.value !== 'main', '没跳回配置页', a.view.value);

        // 跑完了就允许回去
        a.isRunning.value = false;
        a.goConfig();
        check(a.view.value === 'main', '跑完之后能回配置页', a.view.value);
        check(a.resultList.value.length === 0, '这时才清空列表',
              a.resultList.value.length);
    }

    // ══════════════════════════════════════════════════════════
    section('【7】run_finished → 总结页的逐项列表不能是空的 (走真 WS 那条路)');
    {
        const a = await makeApp();
        check(a.__sockets.length === 1, 'WS 连上了', a.__sockets.length);

        // 造一个「跑完了」的事件
        await a.__sockets[0].onmessage({
            data: JSON.stringify({
                type: 'run_finished',
                status: 'passed',
                summary: { total: 2, passed: 2, failed: 0, error: 0,
                           skipped: 1, not_run: 1, planned: 4 },
                sn: 'SN-TEST-0001',
                finished_at: 2000,
            }),
        });
        await tick(30);                       // 等 handler 里的 syncStatus 回来

        check(a.view.value === 'summary', '跳到总结页', a.view.value);
        check(a.resultList.value.length === 4,
              '逐项列表有 4 项 (不是空页)', a.resultList.value.length);
        check(a.passedCount.value === 2, '通过 2 项', a.passedCount.value);
        check(a.executedCount.value === 2, '已测 2 项', a.executedCount.value);
        check(a.plannedCount.value === 4, '共勾选 4 项', a.plannedCount.value);
        check(a.passRateText.value === '2/2', '通过率 2/2',
              a.passRateText.value);
        check(a.notTestedItems.value.length === 2, '未测项 2 个',
              JSON.stringify(a.notTestedItems.value));
    }

    // ══════════════════════════════════════════════════════════
    section('【8】模板真的把「通过率」和「未测披露」印出来');
    {
        const html = fs.readFileSync(INDEX_HTML, 'utf8');
        check(html.includes('passRateText'), '模板用了 passRateText');
        check(html.includes('executedCount'), '模板用了 executedCount');
        check(html.includes('plannedCount'), '模板用了 plannedCount');
        check(html.includes('notTestedItems'), '模板用了 notTestedItems');
        check(html.includes('notTestedText'), '模板用了 notTestedText');
        check(/未测/.test(html), '模板里出现了「未测」这两个字');
        check(/通过率/.test(html), '模板里出现了「通过率」这两个字');
        // 「返回配置」按钮运行中要禁用
        check(/goConfig[^>]*:disabled="isRunning"/.test(html)
              || /:disabled="isRunning"[^>]*goConfig/.test(html),
              '「返回配置」按钮在运行中是禁用的');
    }

    return finish();
}

main().then(code => process.exit(code)).catch(e => {
    console.error(e);
    process.exit(1);
});
