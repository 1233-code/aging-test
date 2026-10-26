/* 回归测试: 历史记录「点一条就地展开」的行为。
 *
 * 产线反馈: 点一条历史记录, 详情跑到**页面最下面**去了 —— 记录一多就得
 * 往下滚半屏才看得见, 滚下去之后还分不清哪块详情是刚点的(上一条还开着,
 * 看着就是两段详情)。改成就地插在那一行下面。
 *
 * 行为规约(这几条是这个测试的全部意义):
 *   ① 点第 N 条 → 展开的是**第 N 条**, 不是别的
 *   ② 再点同一条 → 收起来
 *   ③ 点别的条 → 旧的收起、新的展开(**同时只开一条**)
 *   ④ 详情内容跟着展开的那条走
 *   ⑤ 竞态: 慢的请求后到, 不能盖掉当前展开的那条
 *   ⑥ 离开历史页 → 展开状态清掉
 *
 * 跑法: node tests/test_frontend_history.js
 */
'use strict';

const { loadApp, makeChecker } = require('./_vue_harness');

const { check, section, finish } = makeChecker();

const RUNS = [
    { id: 121, sn: 'SN-A', status: 'passed', started_at: 100, finished_at: 200,
      summary: { passed: 1, total: 1 } },
    { id: 120, sn: 'SN-B', status: 'failed', started_at: 300, finished_at: 400,
      summary: { passed: 0, total: 1 } },
    { id: 119, sn: 'SN-C', status: 'stopped', started_at: 500, finished_at: 600,
      summary: { passed: 0, total: 0 } },
];

function detailOf(id) {
    const h = RUNS.find(r => r.id === id);
    return {
        id,
        sn: h.sn,
        operator: '产线甲',
        status: h.status,
        started_at: h.started_at,
        finished_at: h.finished_at,
        pdf_path: '/reports/pdf/x.pdf',
        summary: h.summary,
        results: [
            { test_name: 'usb', status: 'passed', duration_sec: 0.2,
              metrics: {}, error: null },
        ],
    };
}

// 普通的 fetch 桩: 立即返回
function fastFetch() {
    return (url) => {
        if (url === '/api/history') {
            return Promise.resolve({ json: () => Promise.resolve(RUNS) });
        }
        const m = /^\/api\/history\/(\d+)$/.exec(url);
        if (m) {
            const id = Number(m[1]);
            return Promise.resolve({ json: () => Promise.resolve(detailOf(id)) });
        }
        return Promise.reject(new Error('不该请求 ' + url));
    };
}

async function main() {
    // ══════════════════════════════════════════════════════════
    section('【1】点一条 → 展开的是那一条, 详情也跟着它');
    {
        const a = loadApp({ fetch: fastFetch() });
        await a.goHistory();
        check(a.history.value.length === 3, '历史列表拉到了', a.history.value.length);
        check(a.expandedId.value === null, '一开始谁都没展开');

        await a.toggleHistoryDetail(120);
        check(a.expandedId.value === 120, '展开的是 120', a.expandedId.value);
        check(a.historyDetail.value && a.historyDetail.value.id === 120,
              '详情是 120 的', a.historyDetail.value && a.historyDetail.value.id);
        check(a.historyDetail.value.operator === '产线甲', '详情内容带上了测试人员');
    }

    // ══════════════════════════════════════════════════════════
    section('【2】再点同一条 → 收起来');
    {
        const a = loadApp({ fetch: fastFetch() });
        await a.goHistory();
        await a.toggleHistoryDetail(121);
        check(a.expandedId.value === 121, '先展开 121');
        await a.toggleHistoryDetail(121);
        check(a.expandedId.value === null, '再点一次收起来', a.expandedId.value);
        check(a.historyDetail.value === null, '详情也清掉了');
    }

    // ══════════════════════════════════════════════════════════
    section('【3】点别的条 → 旧的收起, 新的展开 (同时只开一条)');
    {
        const a = loadApp({ fetch: fastFetch() });
        await a.goHistory();
        await a.toggleHistoryDetail(121);
        await a.toggleHistoryDetail(119);
        check(a.expandedId.value === 119, '展开切到 119', a.expandedId.value);
        check(a.historyDetail.value.id === 119, '详情也换成 119 的',
              a.historyDetail.value.id);
    }

    // ══════════════════════════════════════════════════════════
    section('【4】竞态: 慢的请求后到, 不能盖掉当前展开的那条');
    {
        // 手动控制两个请求的返回时机: 先点的 121 慢, 后点的 119 快。
        const pending = {};
        const controlled = (url) => {
            const m = /^\/api\/history\/(\d+)$/.exec(url);
            if (url === '/api/history') {
                return Promise.resolve({ json: () => Promise.resolve(RUNS) });
            }
            const id = Number(m[1]);
            return new Promise((resolve) => {
                pending[id] = () => resolve({
                    json: () => Promise.resolve(detailOf(id)),
                });
            });
        };
        const a = loadApp({ fetch: controlled });
        await a.goHistory();

        const p1 = a.toggleHistoryDetail(121);   // 慢
        const p2 = a.toggleHistoryDetail(119);   // 快
        // 先回 119
        pending[119]();
        await p2;
        check(a.historyDetail.value.id === 119, '119 的先到了, 详情是 119');
        // 再回 121 —— 这一条**必须被丢掉**
        pending[121]();
        await p1;
        check(a.historyDetail.value.id === 119,
              '121 的迟到后没有盖掉 119', a.historyDetail.value.id);
        check(a.expandedId.value === 119, '展开的仍是 119', a.expandedId.value);
    }

    // ══════════════════════════════════════════════════════════
    section('【5】离开历史页 → 展开状态清掉');
    {
        const a = loadApp({ fetch: fastFetch() });
        await a.goHistory();
        await a.toggleHistoryDetail(120);
        check(a.expandedId.value === 120, '先展开 120');
        a.goConfig();
        check(a.expandedId.value === null, '离开后收起了', a.expandedId.value);
        check(a.historyDetail.value === null, '详情也清了');
    }
    {
        const a = loadApp({ fetch: fastFetch() });
        await a.goHistory();
        await a.toggleHistoryDetail(120);
        a.goHistory();          // 再进历史页
        check(a.expandedId.value === null, '重新进历史页也是收起的',
              a.expandedId.value);
    }

    // ══════════════════════════════════════════════════════════
    section('【6】展开的是 run id 而不是下标 (下标会随列表刷新错位)');
    {
        const a = loadApp({ fetch: fastFetch() });
        await a.goHistory();
        await a.toggleHistoryDetail(119);          // 列表里的第三条
        check(a.expandedId.value === 119, '存的是 119 这个 id', a.expandedId.value);
        check(typeof a.expandedId.value === 'number', '是数字 id, 不是下标');
    }

    return finish();
}

// main 是 async —— 不能直接 process.exit(main())(那会把 Promise 塞给
// exitCode, node 会报 "The code argument must be of type number")。
main().then((code) => process.exit(code));

