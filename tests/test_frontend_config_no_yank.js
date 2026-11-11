/* 回归测试: 工人**主动回到配置页**之后, 上一轮迟到的快照不许把他拽走
 * (2026-11-11 查出来的)。
 *
 * 起因:
 *   `applySnapshot()` 以前只要看见「整轮已结束 + 有 run_id」就 `view='summary'`
 *   (只躲开了历史页)。⇒ 工人跑完一轮 → 点「← 返回配置」, 准备测**下一台**
 *   → 这时 WS 抖一下重连, 服务端补推一帧**上一轮**的快照 ⇒ 界面被拽到
 *   **上一轮**的总结页 ⇒ 工人正填下一台的 SN, 眼前却是上一轮的「全部通过」
 *   ⇒ **可能把上一轮的结论当成这一轮的** ⇒ 放过坏板子。
 *
 * 修法: 走开的时候记下「是从哪一轮走开的」(`leftRunId`)⇒ 那一轮后来到的
 *   快照**整条丢掉**。
 *
 * ⚠️ 别把「恢复能力」一起修没了: 工人**没**走开过的那一轮(比如他压根没
 *   看见结果), 快照照常把总结页摆出来(§7.6「运行态必须能靠快照恢复」)。
 *
 * 跑法: node tests/test_frontend_config_no_yank.js
 */
'use strict';

const { loadApp, makeChecker } = require('./_vue_harness');

const { check, section, finish } = makeChecker();

function snap(over) {
    return Object.assign({
        run_id: 120,
        status: 'running',
        current_test: null,
        current_expected_sec: null,
        current_started_at: null,
        started_at: 1000,
        finished_at: 0,
        mode: 'full',
        parent_run_id: null,
        test_queue: ['memory', 'emmc', 'cpu'],
        pdf_path: null,
        sn: 'SN-TEST-0001',
        error: null,
        waiting_operator: null,
        results: {},
    }, over || {});
}

function main() {
    /* ══════════════════════════════════════════════════════════ */
    section('【1】工人跑完一轮 → 点「返回配置」→ 那一轮的快照迟到 ⇒ 不许被拽走');
    {
        const a = loadApp();
        /* 跑完一轮, 界面在总结页 */
        a.applySnapshot(snap({
            run_id: 120, status: 'passed', finished_at: 2000,
            sn: 'SN-OLD-0001',
            results: {
                memory: { status: 'passed', duration_sec: 60.4, metrics: {}, error: null },
                emmc: { status: 'passed', duration_sec: 60.4, metrics: {}, error: null },
                cpu: { status: 'passed', duration_sec: 60.4, metrics: {}, error: null },
            },
        }));
        check(a.view.value === 'summary', '跑完一轮 ⇒ 总结页', a.view.value);

        /* 工人点「← 返回配置」, 准备测下一台 */
        a.goConfig();
        check(a.view.value === 'main', '点了返回配置 ⇒ 回到配置页', a.view.value);
        check(a.leftRunId.value === 120,
              '记下了「从哪一轮走开的」', a.leftRunId.value);

        /* ⚠️ 这时 WS 重连, 服务端补推了**上一轮**的快照 */
        a.applySnapshot(snap({
            run_id: 120, status: 'passed', finished_at: 2000,
            sn: 'SN-OLD-0001',
            results: {
                memory: { status: 'passed', duration_sec: 60.4, metrics: {}, error: null },
                emmc: { status: 'passed', duration_sec: 60.4, metrics: {}, error: null },
                cpu: { status: 'passed', duration_sec: 60.4, metrics: {}, error: null },
            },
        }));
        check(a.view.value === 'main',
              '**界面留在配置页** —— 不许被上一轮的快照拽到总结页',
              a.view.value);
        check(a.resultList.value.length === 0,
              '上一轮的结果也不许被塞回来(他在配下一台)',
              a.resultList.value.length);
    }

    /* ══════════════════════════════════════════════════════════ */
    section('【2】**新的一轮**跑完了 ⇒ 总结页照常要出来');
    {
        const a = loadApp();
        a.applySnapshot(snap({ run_id: 120, status: 'passed', finished_at: 2000 }));
        a.goConfig();
        /* 下一轮(121)开跑, 跑完 */
        a.applySnapshot(snap({ run_id: 121, status: 'running', sn: 'SN-NEW-0002' }));
        check(a.view.value === 'wizard', '新一轮开跑 ⇒ 向导页', a.view.value);
        a.applySnapshot(snap({
            run_id: 121, status: 'passed', finished_at: 3000, sn: 'SN-NEW-0002',
            results: {
                memory: { status: 'passed', duration_sec: 60.4, metrics: {}, error: null },
                emmc: { status: 'passed', duration_sec: 60.4, metrics: {}, error: null },
                cpu: { status: 'passed', duration_sec: 60.4, metrics: {}, error: null },
            },
        }));
        check(a.view.value === 'summary',
              '新一轮跑完 ⇒ 总结页(别把新结果也一起挡了)', a.view.value);
        check(a.runSn.value === 'SN-NEW-0002',
              '顶栏是**新**那一轮的 SN', a.runSn.value);
    }

    /* ══════════════════════════════════════════════════════════ */
    section('【3】工人**没**走开过 ⇒ 快照恢复照常(§7.6 别修没了)');
    {
        const a = loadApp();
        a.applySnapshot(snap({
            run_id: 120, status: 'passed', finished_at: 2000,
            results: {
                memory: { status: 'passed', duration_sec: 60.4, metrics: {}, error: null },
                emmc: { status: 'passed', duration_sec: 60.4, metrics: {}, error: null },
                cpu: { status: 'passed', duration_sec: 60.4, metrics: {}, error: null },
            },
        }));
        check(a.view.value === 'summary',
              '没走开过 ⇒ 刷新/重连后照样能看见结果', a.view.value);
    }

    /* ══════════════════════════════════════════════════════════ */
    section('【4】重测(mini)那一轮 ⇒ 按**父轮**算「是不是走开的那一轮」');
    {
        const a = loadApp();
        a.applySnapshot(snap({ run_id: 120, status: 'passed', finished_at: 2000 }));
        a.goConfig();
        /* mini 快照: run_id=121, parent_run_id=120 ⇒ 它讲的还是父轮 120 */
        a.applySnapshot(snap({
            run_id: 121, mode: 'mini', parent_run_id: 120,
            status: 'passed', finished_at: 2500,
        }));
        check(a.view.value === 'main',
              'mini 的快照(父轮=走开的那一轮)⇒ 也不许拽走', a.view.value);
    }

    return finish();
}

process.exit(main());
