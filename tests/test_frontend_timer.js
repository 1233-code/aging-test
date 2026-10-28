/* 回归测试: 本轮总耗时 —— 顶栏右上角的读秒 + 总结页上的「总耗时」。
 *
 * 需求 (2026-10-28, 产线原话):「测试的时候加一个时间读秒, 就是记录测试
 * 花的时间在右上角做一个记录, 不管选择多少项, 就读秒记录最后一个总的时间,
 * 然后最后这个时间在测试界面和报告中都要体现。」
 *
 * 三条要盯住的:
 *   ① **不管勾了几项, 计时器都只记一个数** —— 整轮的总时间, 不是每项
 *      各记一个(工人要的是「这台机器一共测了多久」)。
 *   ② **中途刷新页面, 计时接着走** —— 起点是**后端**记的 started_at,
 *      不是浏览器自己记的「我什么时候开的页」。否则刷新一次, 计时就归零,
 *      工人看到的总时间比实际短一大截。
 *   ③ **屏幕上的数跟报告里的「总耗时」必须是同一个数** —— 工人盯着屏幕
 *      记个数, 又翻报告对一眼, 两个写法不一样他会以为其中一个是错的。
 *
 * 跑法: node tests/test_frontend_timer.js
 */
'use strict';

const fs = require('fs');
const path = require('path');

/* 同别的前端测试: AGING_STATIC_DIR 指到别处就能对着**副本**跑 ——
 * 变异测试要把 static/ 改坏, 断言必须红, 否则这测试是个摆设。 */
const STATIC_DIR = process.env.AGING_STATIC_DIR
    || path.join(__dirname, '..', 'static');
process.env.AGING_APP_JS = process.env.AGING_APP_JS
    || path.join(STATIC_DIR, 'app.js');

const { loadApp, makeChecker } = require('./_vue_harness');

const { check, section, finish } = makeChecker();

const INDEX_HTML = path.join(STATIC_DIR, 'index.html');

const tick = (ms) => new Promise(r => setTimeout(r, ms));

/** 一份最小可用的配置桩 —— app.js 的 onMounted 会去拉它。 */
function makeFetch() {
    return (url) => {
        if (url === '/api/config') {
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
        return Promise.resolve({ json: () => Promise.resolve({}) });
    };
}

async function main() {
    // ══════════════════════════════════════════════════════════
    section('【1】计时器摆在**右上角**(顶栏右侧, 按钮排的前面)');
    {
        const html = fs.readFileSync(INDEX_HTML, 'utf8');
        // 顶栏右半边 = .header-right
        const iRight = html.indexOf('<div class="header-right">');
        const iTimer = html.indexOf('class="run-timer"');
        const iBtn = html.indexOf('class="btn-text"');
        check(iRight >= 0, '顶栏有 .header-right');
        check(iTimer >= 0, '顶栏里有一个 .run-timer (读秒)');
        check(iRight >= 0 && iTimer > iRight,
              '读秒在顶栏的**右半边**里 (不是左边的标题/SN 那一段)');
        check(iTimer >= 0 && iBtn > iTimer,
              '读秒排在按钮排**前面** —— 它是这一屏最该一眼看到的东西',
              { iTimer, iBtn });
        check(html.includes('{{ elapsedText }}'),
              '读秒的数字绑的是 elapsedText');
        check(/v-if="runStartedAt"/.test(html),
              '还没开跑就不摆(免得一个 00:00:00 杵在顶上)');
    }

    // ══════════════════════════════════════════════════════════
    section('【2】起点来自**后端**的 started_at (刷新页面也接着走)');
    {
        const a = loadApp({ fetch: makeFetch() });
        check(a.runStartedAt.value === 0, '还没开跑 → 没有起点');

        // 模拟「跑到一半刷新页面」: 页面重新加载, 只收到一份快照。
        a.applySnapshot({
            run_id: 1, status: 'running', started_at: 1000, finished_at: 0,
            sn: 'SN-1', test_queue: ['cpu', 'gpu'],
            results: { cpu: { status: 'passed', metrics: {}, error: null } },
        });
        check(a.runStartedAt.value === 1000,
              '起点取自快照的 started_at (不是浏览器自己记的)', a.runStartedAt.value);

        // 假装现在是 1000+125 秒
        a.clockNow.value = 1125;
        check(a.elapsedSec.value === 125, '已经过了 125 秒', a.elapsedSec.value);
        check(a.elapsedText.value === '00:02:05',
              '读秒写成 00:02:05', a.elapsedText.value);
    }

    // ══════════════════════════════════════════════════════════
    section('【3】**不管勾了几项**, 计时器只记一个总数');
    {
        const a = loadApp({ fetch: makeFetch(), mount: true });
        await tick(10);
        a.applySnapshot({
            run_id: 1, status: 'running', started_at: 1000, finished_at: 0,
            sn: 'SN-1', test_queue: ['cpu', 'gpu', 'usb'],
            results: {},
        });
        a.clockNow.value = 1100;
        check(a.elapsedSec.value === 100, '第一项跑完: 100 秒', a.elapsedSec.value);

        // 换到第二项 —— 计时**不许归零**
        await a.__sockets[0].onmessage({
            data: JSON.stringify({ type: 'test_started', name: 'gpu' }),
        });
        await tick(10);
        check(a.runStartedAt.value === 1000,
              '换项之后起点没变', a.runStartedAt.value);
        check(a.elapsedSec.value === 100,
              '换项之后读秒没归零 (整轮一个总数)', a.elapsedSec.value);

        a.clockNow.value = 1180;
        check(a.elapsedText.value === '00:03:00',
              '跑到第三项时读秒是 00:03:00', a.elapsedText.value);
    }

    // ══════════════════════════════════════════════════════════
    section('【4】跑完之后**锁死**在总耗时上');
    {
        const a = loadApp({ fetch: makeFetch(), mount: true });
        await tick(10);
        a.applySnapshot({
            run_id: 1, status: 'running', started_at: 1000, finished_at: 0,
            sn: 'SN-1', test_queue: ['cpu'], results: {},
        });
        a.clockNow.value = 1100;
        await a.__sockets[0].onmessage({
            data: JSON.stringify({
                type: 'run_finished', status: 'passed',
                summary: { total: 1, passed: 1, failed: 0, error: 0 },
                sn: 'SN-1', finished_at: 1100,
            }),
        });
        await tick(10);
        check(a.runFinishedAt.value === 1100, '结束时间记下了', a.runFinishedAt.value);

        // 时间继续走, 但读数不许再动
        a.clockNow.value = 9999;
        check(a.elapsedSec.value === 100,
              '结束之后读数锁死 (不再跟着时钟走)', a.elapsedSec.value);
        check(a.elapsedText.value === '00:01:40',
              '锁在 00:01:40', a.elapsedText.value);
        check(a.totalDurationText.value === '1m40s',
              '总结页上的总耗时是 1m40s', a.totalDurationText.value);
    }

    // ══════════════════════════════════════════════════════════
    section('【5】总耗时的写法跟**报告里那个数**一模一样');
    /* 报告里的 `_fmt_dur` 和前端的 `fmtDur` 必须**逐字一样** —— 工人盯着
       屏幕记个数, 又翻报告对一眼, 两个写法不一样他会以为其中一个是错的。

       ⚠️ 这张表 (tests/fixtures/duration_formats.json) 是**两边共用的**:
       tests/test_report_durations.py 拿它钉 Python 那边, 这里拿它钉前端
       那边 —— 同一张表, 两个实现, 谁改歪了谁红。

       表里 58.25 / 58.35 这种「正好卡在中间」的数是**故意挑的**:
       Python 的 f"{x:.1f}" 是「五成双」(58.25 → 58.2), JS 的 toFixed(1)
       是「五入」(58.25 → 58.3)。两边原本会对不上, 现在报告那边也改成
       五入了, 所以表里写的都是「五入」的结果。 */
    {
        const a = loadApp({ fetch: makeFetch() });
        const table = JSON.parse(fs.readFileSync(
            path.join(__dirname, 'fixtures', 'duration_formats.json'), 'utf8'));
        check(table.length > 0, '共用的对照表读到了');
        for (const [sec, want] of table) {
            check(a.fmtDur(sec) === want,
                  `${sec} 秒 → "${want}"`, a.fmtDur(sec));
        }
        check(a.fmtDur(-1) === '—', '负数 → —', a.fmtDur(-1));

        check(a.fmtHms(0) === '00:00:00', '0 秒 → 00:00:00', a.fmtHms(0));
        check(a.fmtHms(65) === '00:01:05', '65 秒 → 00:01:05', a.fmtHms(65));
        check(a.fmtHms(3661) === '01:01:01', '3661 秒 → 01:01:01', a.fmtHms(3661));
    }

    // ══════════════════════════════════════════════════════════
    section('【6】总结页上要印「总耗时」');
    {
        const html = fs.readFileSync(INDEX_HTML, 'utf8');
        check(html.includes('totalDurationText'), '模板用了 totalDurationText');
        check(/总耗时/.test(html), '模板里出现了「总耗时」这两个字');
    }

    // ══════════════════════════════════════════════════════════
    section('【7】读秒自己会走 (每秒一格)');
    {
        const a = loadApp({ fetch: makeFetch(), mount: true });
        await tick(10);
        a.applySnapshot({
            run_id: 1, status: 'running', started_at: 1000, finished_at: 0,
            sn: 'SN-1', test_queue: ['cpu'], results: {},
        });
        const before = a.clockNow.value;
        // 真的等一秒多 —— 证明那不是个摆设
        await tick(1100);
        check(a.clockNow.value > before,
              '时钟每秒往前走一格', { before, after: a.clockNow.value });
        check(a.elapsedSec.value >= 1,
              '读秒跟着走了', a.elapsedSec.value);
    }

    // ══════════════════════════════════════════════════════════
    section('【8】光是 run_started 事件 (没有快照) 也得把表起起来');
    /* 快照不一定赶得上 —— 后端可能先把这一轮标成 running 之前就把
     * run_started 广播出去了, 而那一刻快照里报的还是 idle。
     * 光靠快照起表, 工人就会看到读秒一直杵在 00:00:00 不动。 */
    {
        const b = loadApp({ fetch: makeFetch(), mount: true });
        await tick(10);
        const before = b.clockNow.value;
        await b.__sockets[0].onmessage({
            data: JSON.stringify({ type: 'run_started', run_id: 2, started_at: 1000 }),
        });
        await tick(1100);
        check(b.clockNow.value > before,
              '只收到 run_started, 时钟也走起来了',
              { before, after: b.clockNow.value });
        check(b.runStartedAt.value === 1000,
              '起点取自 run_started 里的 started_at', b.runStartedAt.value);
    }

    // ══════════════════════════════════════════════════════════
    section('【9】run_finished 带的**一对**时间说了算(合并重测那一下)');
    /* 合并重测(§7.7)时后端把 runner 切到了父轮, 所以 `run_finished` 报的
     * 是**父轮**的一对时间 —— 而上一条 `run_started` 报的是**重测轮**的
     * 起点。前端要是拿「重测轮的起点」去配「父轮的结束时间」, 界面上那个
     * 总耗时 = 父轮结束 − 重测轮开始, 大得离谱。
     * 所以: run_finished 带来的这一对**必须**整体盖掉之前的。 */
    {
        const a = loadApp({ fetch: makeFetch(), mount: true });
        await tick(10);
        await a.__sockets[0].onmessage({
            data: JSON.stringify({ type: 'run_started', run_id: 7, started_at: 1000 }),
        });
        await tick(10);
        check(a.runStartedAt.value === 1000, '起跑时起点是 1000', a.runStartedAt.value);

        await a.__sockets[0].onmessage({
            data: JSON.stringify({
                type: 'run_finished', status: 'passed',
                summary: { total: 1, passed: 1, failed: 0, error: 0 },
                sn: 'SN-1',
                started_at: 5000,        // ← 合并后: 父轮的起点
                finished_at: 5300,      // ← 父轮的结束时间
            }),
        });
        await tick(10);
        check(a.runStartedAt.value === 5000,
              '起点换成 run_finished 给的那个 (不是沿用 run_started 的)',
              a.runStartedAt.value);
        check(a.runFinishedAt.value === 5300, '结束时间是 5300', a.runFinishedAt.value);
        check(a.elapsedSec.value === 300,
              '总耗时 = 5300 − 5000 = 300 秒 (不是 4300)', a.elapsedSec.value);
        check(a.totalDurationText.value === '5m00s',
              '总耗时写成 5m00s', a.totalDurationText.value);
    }

    return finish();
}

main().then((code) => process.exit(code)).catch((e) => {
    console.error(e);
    process.exit(1);
});
