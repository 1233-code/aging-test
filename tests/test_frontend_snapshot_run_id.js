/* 回归测试: 前端收到的快照必须**是当前这一轮**的 (2026-11-11 查出来的)。
 *
 * 起因:
 *   `applySnapshot()` 以前是**无条件**把快照套到界面上 —— 不看它是哪一轮的。
 *   ⇒ 工人开了**第二轮**, 第一轮的快照(排队中的 / 重连补查的)这时才到 ⇒
 *   界面把第二轮的进度抹掉、换成第一轮的结果 ⇒ 工人眼前是**上一轮**的
 *   结论 ⇒ 可能拿上一轮的「全部通过」当这一轮的结论 ⇒ **放过坏板子**。
 *
 * 判据 = **轮次号**: 轮次号是自增的 ⇒ 快照的轮次号比当前小 ⇒ 它是更早
 * 那一轮的 ⇒ 整条丢掉(连 run_id 都不许改)。
 *
 * 跑法: node tests/test_frontend_snapshot_run_id.js
 */
'use strict';

const { loadApp, makeChecker } = require('./_vue_harness');

const { check, section, finish } = makeChecker();

/* 造一个后端快照 */
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
    section('【1】第一轮跑着的时候, 第二轮的快照到了 ⇒ 界面要换成第二轮');
    {
        const a = loadApp();
        a.applySnapshot(snap({
            run_id: 120,
            status: 'running',
            current_test: 'emmc',
            results: {
                memory: { status: 'passed', duration_sec: 60.4,
                          metrics: { round_1_ok_tests: 9 }, error: null },
            },
        }));
        check(a.runId.value === 120, '当前轮次 = 120', a.runId.value);
        check(a.resultList.value.length === 3, '列表按队列重建出 3 项',
              a.resultList.value.length);

        /* 第二轮开跑了(后端广播的 run_started), 接着第一轮的快照才到 */
        a.runId.value = 121;
        a.applySnapshot(snap({
            run_id: 120,
            status: 'passed',
            finished_at: 2000,
            test_queue: ['memory', 'emmc', 'cpu'],
            results: {
                memory: { status: 'passed', duration_sec: 60.4, metrics: {}, error: null },
                emmc: { status: 'passed', duration_sec: 60.4, metrics: {}, error: null },
                cpu: { status: 'passed', duration_sec: 60.4, metrics: {}, error: null },
            },
        }));
        check(a.runId.value === 121,
              '**旧快照不许把当前轮次改回去**', a.runId.value);
        check(a.isRunning.value === true,
              '**旧快照不许把「正在跑」抹掉**(第二轮明明在跑)',
              a.isRunning.value);
        check(a.resultList.value[1].status !== 'passed',
              '旧快照不许把逐项结果换成上一轮的',
              a.resultList.value[1].status);
    }

    /* ══════════════════════════════════════════════════════════ */
    section('【2】同一轮的快照 ⇒ 照常采用(别把正常恢复也挡了)');
    {
        const a = loadApp();
        a.applySnapshot(snap({ run_id: 120, status: 'running',
                                current_test: 'emmc' }));
        check(a.runId.value === 120, '轮次号认下来了', a.runId.value);
        check(a.isRunning.value === true, 'isRunning 跟着快照走',
              a.isRunning.value);
        check(a.currentTestIndex.value === 1, '当前项也跟着快照走',
              a.currentTestIndex.value);
    }

    /* ══════════════════════════════════════════════════════════ */
    section('【3】服务重启后接着跑 ⇒ **更新的**一轮要认下来');
    {
        const a = loadApp();
        a.applySnapshot(snap({ run_id: 120, status: 'running' }));
        /* 服务重启了, 接着跑的是 121(轮次号更大) */
        a.applySnapshot(snap({ run_id: 121, status: 'running',
                                current_test: 'cpu' }));
        check(a.runId.value === 121, '更新的轮次被认下来了', a.runId.value);
        check(a.isRunning.value === true, '还在跑', a.isRunning.value);
    }

    /* ══════════════════════════════════════════════════════════ */
    section('【4】重测(mini)那一轮 ⇒ 按**父轮**的编号算');
    {
        const a = loadApp();
        /* 父轮 120, 重测轮是 121(mode=mini, parent=120) */
        a.applySnapshot(snap({ run_id: 120, status: 'running',
                                current_test: 'cpu' }));
        a.applySnapshot(snap({
            run_id: 121, mode: 'mini', parent_run_id: 120,
            status: 'running', current_test: 'cpu',
        }));
        check(a.runId.value === 120,
              '重测那一轮仍按父轮编号(跟 run_started 一个口径)', a.runId.value);
        check(a.isRunning.value === true, '重测在跑', a.isRunning.value);
    }

    /* ══════════════════════════════════════════════════════════ */
    section('【5】页面刚打开(手上没有轮次号) ⇒ 快照照收');
    {
        const a = loadApp();
        check(a.runId.value === null, '一开始没有轮次号', a.runId.value);
        a.applySnapshot(snap({ run_id: 77, status: 'running',
                                current_test: 'emmc' }));
        check(a.runId.value === 77, '快照把轮次号补上了', a.runId.value);
        check(a.resultList.value.length === 3, '逐项结果恢复了',
              a.resultList.value.length);
    }

    return finish();
}

process.exit(main());
