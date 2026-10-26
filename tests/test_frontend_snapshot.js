/* §7.6 回归测试: 「运行态必须能靠快照恢复」。
 *
 * 这个 bug 的形态很阴: WS 断一下, run_finished 事件就丢了, 页面停在向导页
 * 不动, 看着像还在跑。修它的关键不是「收到事件时处理好」, 而是**没收到事件
 * 也能把状态补回来** —— 也就是 applySnapshot() 得能**重建**整个界面状态。
 *
 * 这里直接加载 static/app.js, 把 Vue 换成一个够用的假实现(ref/reactive/
 * computed/watch/onMounted), 然后调 setup() 拿到它导出的东西, 拿合成的
 * 快照喂给 applySnapshot(), 断言界面摆到了该在的位置。
 *
 * 为什么不写进 python 测试: 逻辑全在浏览器这一侧, 用 node 跑最直接, 也不
 * 需要起服务、不碰数据库、不碰真硬件。
 *
 * 跑法: node tests/test_frontend_snapshot.js
 */
'use strict';

const { loadApp, makeChecker } = require('./_vue_harness');

const { check, section, finish } = makeChecker();

// ── 造一个后端快照 ─────────────────────────────────────────────
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
    // ══════════════════════════════════════════════════════════
    section('【1】刷新页面时测试正在跑: 界面必须回到向导页, 且逐项结果要恢复');
    {
        const a = loadApp();
        a.applySnapshot(snap({
            status: 'running',
            current_test: 'emmc',
            results: {
                memory: { status: 'passed', duration_sec: 60.4,
                          metrics: { round_1_ok_tests: 9 }, error: null },
            },
        }));
        check(a.view.value === 'wizard',
              '跳到向导页', 'view=' + a.view.value);
        check(a.isRunning.value === true, 'isRunning=true');
        check(a.resultList.value.length === 3,
              '列表按 test_queue 重建出 3 项(含还没跑的)',
              'len=' + a.resultList.value.length);
        check(a.resultList.value[0].status === 'passed',
              '已跑完的 memory 恢复了状态', a.resultList.value[0].status);
        check(a.resultList.value[1].status === 'running',
              '当前项 (emmc) 标成 running', a.resultList.value[1].status);
        check(a.resultList.value[2].status === 'pending',
              '还没跑到的 cpu 是 pending', a.resultList.value[2].status);
        check(a.currentTestIndex.value === 1,
              '向导停在正在跑的那一项 (emmc)',
              'idx=' + a.currentTestIndex.value);
        check(a.passedCount.value === 1, '通过数 = 1', a.passedCount.value);
    }

    // ══════════════════════════════════════════════════════════
    section('【2】WS 断了、跑完之后才重连: 必须自己跳到总结页 (原 bug)');
    {
        const a = loadApp();
        a.applySnapshot(snap({
            status: 'passed',
            finished_at: 2000,
            current_test: null,
            results: {
                memory: { status: 'passed', duration_sec: 60.4, metrics: {}, error: null },
                emmc: { status: 'passed', duration_sec: 62.0, metrics: {}, error: null },
                cpu: { status: 'failed', duration_sec: 60.0, metrics: {}, error: '温度超限' },
            },
        }));
        check(a.view.value === 'summary',
              '跳到总结页 —— 这就是原来「跑完不跳转」的那个症状',
              'view=' + a.view.value);
        check(a.lastStatus.value === 'passed', 'lastStatus = passed');
        check(a.isRunning.value === false,
              'isRunning 归零 —— 「关闭服务」按钮随之解封');
        check(a.resultList.value.length === 3, '三项结果都恢复出来了');
        check(a.failedCount.value === 1, '失败数 = 1', a.failedCount.value);
        check(a.passedCount.value === 2, '通过数 = 2', a.passedCount.value);
        check(a.hasFailedItems.value === true, '「重测失败项」按钮会出现');
    }

    // ══════════════════════════════════════════════════════════
    section('【3】旧代码卡在这条: resultList 空的时候不许跳总结页');
    {
        // 复现旧条件 `ended && resultList.length > 0`: 空列表 → 不跳 →
        // 页面永远停在向导页。修好后必须跳。
        const a = loadApp();
        a.applySnapshot(snap({
            status: 'failed',
            finished_at: 2000,
            results: {
                memory: { status: 'failed', duration_sec: 1, metrics: {}, error: 'x' },
            },
        }));
        check(a.view.value === 'summary',
              '空列表也照样跳总结页 (旧代码在这里不跳)',
              'view=' + a.view.value);
        // 队列是这一轮**计划跑什么**的权威来源, 比 results 全 ——
        // results 里只有跑完的, 所以列表要按队列补出没跑的项。
        check(a.resultList.value.length === 3,
              '列表按 test_queue 补齐成 3 项', a.resultList.value.length);
        check(a.resultList.value[0].status === 'failed',
              '跑完的那项状态是 failed', a.resultList.value[0].status);
        check(a.resultList.value[1].status === 'pending',
              '没跑到的项补成 pending', a.resultList.value[1].status);
    }

    // ══════════════════════════════════════════════════════════
    section('【4】从没跑过 (idle): 不要跳到一个空总结页');
    {
        const a = loadApp();
        a.applySnapshot(snap({ status: 'idle', run_id: null, test_queue: [] }));
        check(a.view.value === 'main', '留在配置页', 'view=' + a.view.value);
        check(a.isRunning.value === false, 'isRunning=false');
    }

    // ══════════════════════════════════════════════════════════
    section('【5】工人正在翻历史记录时, 别把他拽到总结页');
    {
        const a = loadApp();
        a.view.value = 'history';
        a.applySnapshot(snap({
            status: 'passed',
            finished_at: 2000,
            results: { memory: { status: 'passed', duration_sec: 1, metrics: {}, error: null } },
        }));
        check(a.view.value === 'history',
              '留在历史页', 'view=' + a.view.value);
    }

    // ══════════════════════════════════════════════════════════
    section('【6】§8.2 报告按钮: 有 pdf_path 就显示「打开」, 没有就不动它');
    {
        const a = loadApp();
        a.applySnapshot(snap({
            status: 'passed',
            finished_at: 2000,
            pdf_path: '/reports/pdf/SN_20261026_092509.pdf',
            results: { memory: { status: 'passed', duration_sec: 1, metrics: {}, error: null } },
        }));
        check(a.reportPath.value === '/reports/pdf/SN_20261026_092509.pdf',
              'pdf_path 恢复成「打开 PDF 报告」', String(a.reportPath.value));
        check(a.reportRunId.value === 120, 'reportRunId = 本轮的 run id');
    }
    {
        // 快照里 pdf_path 是 null (导出还没做完) —— **不能**把已经有的
        // 报告路径清掉, 否则 report_ready 事件到达前按钮会闪回「导出」。
        const a = loadApp();
        a.reportPath.value = '/reports/pdf/已有的.pdf';
        a.reportRunId.value = 119;
        a.applySnapshot(snap({ status: 'passed', finished_at: 2000, pdf_path: null,
                               results: { memory: { status: 'passed', duration_sec: 1, metrics: {}, error: null } } }));
        check(a.reportPath.value === '/reports/pdf/已有的.pdf',
              'pdf_path 为空时不清掉已有的报告', String(a.reportPath.value));
    }

    // ══════════════════════════════════════════════════════════
    section('【7】这一轮的身份 (SN / 时间 / 异常原因) 要带到总结页');
    {
        const a = loadApp();
        a.applySnapshot(snap({
            status: 'error',
            finished_at: 2000,
            sn: 'SN-TEST-0001',
            error: "KeyError('x')",
            results: {},
        }));
        check(a.runSn.value === 'SN-TEST-0001', 'SN 记下来了', a.runSn.value);
        check(a.runFinishedAt.value === 2000, '结束时间记下来了');
        check(a.runError.value === "KeyError('x')", '异常原因记下来了', a.runError.value);
        check(a.summaryStatusText.value === '本轮出错',
              '结论词是「本轮出错」, 不是「已完成 (部分跳过)」',
              a.summaryStatusText.value);
        check(a.summaryEmoji.value === '⚠️', '图标是 ⚠️', a.summaryEmoji.value);
    }
    {
        const a = loadApp();
        a.lastStatus.value = 'stopped';
        check(a.summaryStatusText.value === '已停止',
              'stopped 也得单独说, 不能混成「部分跳过」',
              a.summaryStatusText.value);
    }

    // ══════════════════════════════════════════════════════════
    section('【8】mini-run: runId 仍指向父轮, 但报告指向 mini 自己那份');
    {
        const a = loadApp();
        a.applySnapshot(snap({
            run_id: 121, parent_run_id: 120, mode: 'mini',
            status: 'passed', finished_at: 2000,
            pdf_path: '/reports/pdf/mini.pdf',
            results: { cpu: { status: 'passed', duration_sec: 60, metrics: {}, error: null } },
        }));
        check(a.runId.value === 120, 'runId 指向父轮 (跟 run_started 一致)', a.runId.value);
        check(a.reportRunId.value === 121, 'reportRunId 指向 mini 自己', a.reportRunId.value);
    }

    // ══════════════════════════════════════════════════════════
    section('【9】快照里没有 test_queue (老后端) 时退回 results 的键序');
    {
        const a = loadApp();
        a.applySnapshot({
            run_id: 5, status: 'passed', finished_at: 2000, mode: 'full',
            results: {
                hdmi: { status: 'passed', duration_sec: 0.1, metrics: {}, error: null },
                usb: { status: 'passed', duration_sec: 0.2, metrics: {}, error: null },
            },
        });
        check(a.resultList.value.length === 2, '按 results 的键序建出 2 项');
        check(a.resultList.value[0].name === 'hdmi', '第一项是 hdmi');
        check(a.view.value === 'summary', '照样跳总结页');
    }

    return finish();
}

process.exit(main());
