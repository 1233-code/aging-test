/* 回归测试: 顶栏读秒必须走**后端的时钟** (2026-11-06 实测)
 *
 * **起因 (产线原话)**
 *   「测试的总时间对不上, 每次都是开头一直是 0, 后面变两个多小时,
 *     然后测试结果上的时间又对得上。」
 *
 *   「报告对得上、界面对不上」正是这个病根的指纹 —— 报告只用后端的时间
 *   戳, 界面用浏览器自己的墙钟。两边**不在同一个时间尺度上**:
 *
 *     · 后端: 系统时钟被硬复位后退回 51.8 天前 ⇒ 时间戳全走
 *       「锚点 + 单调钟」的**推算值**(§9.3.4);
 *     · 前端: 拿 `Date.now()`(墙钟)去减后端的 `started_at`(推算值)
 *       ⇒ **跨尺度相减**:
 *         - 墙钟慢 51.8 天 ⇒ 差值是负数 ⇒ 被 clamp 成 00:00:00
 *           (症状一: 「开头一直是 0」);
 *         - 锚点停在几小时前(服务空转时冻着)⇒ 差值正好是那几小时
 *           (症状二: 「后面变两个多小时」, 实测 2 小时 28 分)。
 *
 * **修法**: 后端每条事件 + 快照都带 `server_now`; 前端拿它当基准, 用
 * **浏览器的单调钟** (`performance.now()`) 往前推 —— 单调钟不受墙钟
 * 跳变影响。
 *
 * **这里守的是**
 *   ① 墙钟歪 51.8 天 ⇒ 读秒照旧(后端尺度);
 *   ② 墙钟快 2 小时 28 分 ⇒ 读秒照旧(**不多算**那段时间);
 *   ③ WS 事件也会重打基准;
 *   ④ 老后端(没有 `server_now`) ⇒ 退回本机墙钟, 界面还能用;
 *   ⑤ 代码层面: 读秒的计算里不许出现 `Date.now()`。
 *
 * **不碰真服务、不碰真硬件**: app.js 跑在 vm 里, 时钟全是假的。
 * 跑法: node tests/test_frontend_server_clock.js
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

const tick = (ms) => new Promise(r => setTimeout(r, ms));

/* 「后端尺度」的此刻 —— 用一个像样的 epoch, 别用 1000 这种玩具数
 * (差 51.8 天的时候, 小数字会让两个尺度看起来一样)。 */
const NOW = 1792000000;      // 后端(推算)时钟的「现在」
const DAY = 86400;
const TWO_H_28M = 2 * 3600 + 28 * 60;

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

/** 一份「跑了一小时」的快照。 */
function snapshot(extra) {
    return Object.assign({
        run_id: 1, status: 'running',
        started_at: NOW - 3600, finished_at: 0,
        server_now: NOW,
        sn: 'SN-1', test_queue: ['cpu'], results: {},
    }, extra || {});
}

async function main() {
    // ══════════════════════════════════════════════════════════
    section('【1】墙钟**歪 51.8 天** ⇒ 读秒照旧(后端尺度)');
    /* 症状一: 墙钟慢 ⇒ 差值是负数 ⇒ 旧写法被 clamp 成 00:00:00,
     *       整轮读秒冻在 0。 */
    {
        const a = loadApp({
            fetch: makeFetch(), mount: true,
            dateNow: () => (NOW - 51.8 * DAY) * 1000,   // 墙钟慢 51.8 天
            perfNow: () => 1000,                        // 单调钟(不受跳变影响)
        });
        await tick(10);
        a.applySnapshot(snapshot());
        check(a.clockNow.value === NOW,
              '时钟用的是后端给的 server_now(不是本机墙钟)',
              a.clockNow.value);
        check(a.elapsedSec.value === 3600,
              '读秒是 3600 秒(没被歪掉的墙钟带成 0)', a.elapsedSec.value);
        check(a.elapsedText.value === '01:00:00',
              '读秒写成 01:00:00', a.elapsedText.value);
        check(a.totalDurationText.value === '60m00s',
              '总结页的总耗时是 60m00s', a.totalDurationText.value);
    }

    // ══════════════════════════════════════════════════════════
    section('【2】墙钟**快 2 小时 28 分** ⇒ 读秒照旧(不多算)');
    /* 症状二: 锚点停在几小时前(服务空转时冻着)⇒ 后端的推算时间比真实
     *       时间慢 ⇒ 墙钟相对地「快」⇒ 旧写法会**多算** 2 小时 28 分。 */
    {
        const a = loadApp({
            fetch: makeFetch(), mount: true,
            dateNow: () => (NOW + TWO_H_28M) * 1000,
            perfNow: () => 1000,
        });
        await tick(10);
        a.applySnapshot(snapshot());
        check(a.elapsedSec.value === 3600,
              '读秒还是 3600(没有多算那 2 小时 28 分)', a.elapsedSec.value);
        check(a.elapsedText.value === '01:00:00',
              '读秒写成 01:00:00(不是 03:28:00)', a.elapsedText.value);
    }

    // ══════════════════════════════════════════════════════════
    section('【1b】推进读秒用的是**单调钟**(墙钟冻着, 读秒照样走)');
    /* 单调钟 (`performance.now()`) 不受墙钟跳变影响 —— 推进读秒只能用它。
     * 墙钟要是也拿来推进, 板子被校正时钟的那一刻, 读秒会猛地跳一下。 */
    {
        let perf = 0;
        const a = loadApp({
            fetch: makeFetch(), mount: true,
            dateNow: () => (NOW - 51.8 * DAY) * 1000,   // 墙钟**冻着**不动
            perfNow: () => perf,                        // 单调钟往前走
        });
        await tick(10);
        a.applySnapshot(snapshot());
        const before = a.clockNow.value;
        perf = 3000;                                   // 单调钟走了 3 秒
        await tick(1100);                              // 等读秒那一格
        check(a.clockNow.value >= before + 2.9,
              '墙钟不动时, 读秒靠单调钟照样往前走',
              { before, after: a.clockNow.value });
    }

    // ══════════════════════════════════════════════════════════
    section('【3】WS 事件也会重打基准(不只是快照)');
    {
        const a = loadApp({
            fetch: makeFetch(), mount: true,
            dateNow: () => (NOW - 51.8 * DAY) * 1000,
            perfNow: () => 1000,
        });
        await tick(10);
        await a.__sockets[0].onmessage({
            data: JSON.stringify({
                type: 'test_started', name: 'cpu', server_now: NOW + 60,
            }),
        });
        await tick(10);
        check(a.clockNow.value === NOW + 60,
              '收到 WS 事件就重新对表', a.clockNow.value);
    }

    // ══════════════════════════════════════════════════════════
    section('【4】老后端(没有 server_now) ⇒ 退回本机墙钟, 界面还能用');
    {
        const a = loadApp({
            fetch: makeFetch(), mount: true,
            dateNow: () => NOW * 1000,
            perfNow: () => 1000,
        });
        await tick(10);
        a.applySnapshot(snapshot({ server_now: 0 }));
        await tick(1100);          // 等读秒那一格跑一次
        check(Math.abs(a.clockNow.value - NOW) < 2,
              '没有后端基准时退回本机墙钟(老服务也能用)',
              a.clockNow.value);
        check(a.elapsedSec.value >= 3600 && a.elapsedSec.value <= 3602,
              '读秒照常走(不会冻在 0)', a.elapsedSec.value);
    }

    // ══════════════════════════════════════════════════════════
    section('【5】代码层面: 读秒不许拿 `Date.now()` 去减后端的时间戳');
    {
        const src = fs.readFileSync(process.env.AGING_APP_JS, 'utf8');
        const i0 = src.indexOf('const elapsedSec = computed');
        check(i0 >= 0, '找得到读秒的计算');
        const block = src.slice(i0, src.indexOf('});', i0));
        check(!block.includes('Date.now'),
              '读秒的计算里没有 Date.now()', block.slice(0, 160));
        check(src.includes('serverNow.value + (perfNow() - serverNowPerf.value) / 1000'),
              '读秒是「后端基准 + 单调钟往前推」');
        check(src.includes('performance.now'),
              '单调钟用的是 performance.now()');
        check((src.match(/syncServerClock\(msg\.server_now\)/g) || []).length >= 2,
              '快照和 WS 事件两条路都同步了');
    }

    return finish();
}

main().then((code) => process.exit(code)).catch((e) => {
    console.error(e);
    process.exit(1);
});
