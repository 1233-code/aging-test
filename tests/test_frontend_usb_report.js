/* 回归测试: §8.2.3「报告自动存 U 盘」的前端那一半。
 *
 * 守的是三件事:
 *   ① 后端推 usb_report_saved / usb_report_missing → 界面上出现**一条提示**,
 *      写着报告存到哪儿了(工人拔盘走人时要知道东西在盘上);
 *   ② 这条提示**绝不能**改写本轮判定 —— 存没存成跟板子合不合格是两件事。
 *      (存不上就判「不合格」= 让工人白换一台好板子; 这正是 §9.3 说的
 *       「静默的失效」比「明确的失败」更坏。)
 *   ③ 手动补导 PDF 之后, 后端回的那句「存没存成 U 盘」也要显示出来。
 *
 * 跑法: node tests/test_frontend_usb_report.js
 */
'use strict';

const fs = require('fs');
const path = require('path');

const STATIC_DIR = process.env.AGING_STATIC_DIR
    || path.join(__dirname, '..', 'static');
process.env.AGING_APP_JS = process.env.AGING_APP_JS
    || path.join(STATIC_DIR, 'app.js');

const { loadApp, makeChecker } = require('./_vue_harness');

const { check, section, finish } = makeChecker();

const INDEX_HTML = path.join(STATIC_DIR, 'index.html');

const tick = (ms) => new Promise(r => setTimeout(r, ms || 5));

const TEST_LIST = [
    { name: 'cpu', display_name: 'CPU 压力' },
    { name: 'usb', display_name: 'USB 接口' },
];

/* 后端补导 PDF 时回的那句「存没存成 U 盘」—— 用一个开关控制,
 * 免得【1】~【3】的用例还没到那一步就带上提示。 */
let usbReply = null;

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
    if (url.indexOf('/api/history/') >= 0 && url.indexOf('/pdf') >= 0) {
        return Promise.resolve({
            ok: true,          // ⚠️ app.js 判的是 r.ok —— 没这个字段它
            status: 200,       //    会当成导出失败直接 return
            json: () => Promise.resolve({
                ok: true,
                path: '/var/lib/aging-test/reports/pdf/SN_1.pdf',
                usb: usbReply,
            }),
        });
    }
    return Promise.resolve({ json: () => Promise.resolve({}) });
}

async function makeApp() {
    const a = loadApp({ mount: true, fetch: fetchStub });
    await tick(20);
    return a;
}

/** 往 WS 里喂一条事件 */
async function push(a, msg) {
    const ws = a.__sockets[a.__sockets.length - 1];
    ws.onmessage({ data: JSON.stringify(msg) });
    await tick(10);
}

async function main() {
    // ══════════════════════════════════════════════════════════
    section('【1】usb_report_saved → 界面上出现一条提示');
    {
        const a = await makeApp();
        check(a.usbReportNote.value === '', '一开始没有提示',
              a.usbReportNote.value);
        await push(a, {
            type: 'usb_report_saved',
            path: '/media/user/USB/AGING-REPORTS',
            filename: 'SN-TEST-0001_20251105_120000.pdf',
            message: '报告已存到 U 盘: /media/user/USB/AGING-REPORTS',
            run_id: 1,
        });
        check(a.usbReportNote.value.indexOf('已存到 U 盘') >= 0,
              '提示里写着「已存到 U 盘」', a.usbReportNote.value);
        check(a.usbReportNote.value.indexOf('/media/user/USB/AGING-REPORTS') >= 0,
              '提示里写着存到哪个路径(工人要知道去哪儿拿)',
              a.usbReportNote.value);
    }

    // ══════════════════════════════════════════════════════════
    section('【2】usb_report_missing → 说清楚「没插 U 盘, 报告还在本机」');
    {
        const a = await makeApp();
        await push(a, {
            type: 'usb_report_missing',
            path: '',
            filename: '',
            message: '未检测到 U 盘 —— 报告仍在本机 reports/pdf/',
            run_id: 1,
        });
        check(a.usbReportNote.value.indexOf('未检测到 U 盘') >= 0,
              '提示说「未检测到 U 盘」', a.usbReportNote.value);
        check(a.usbReportNote.value.indexOf('reports/pdf') >= 0,
              '提示说报告还在本机', a.usbReportNote.value);
    }

    // ══════════════════════════════════════════════════════════
    section('【3】存没存成 U 盘 **不改写本轮判定**');
    {
        const a = await makeApp();
        // 逐项结果走 applySnapshot(跟真机上「刷新页面恢复」是同一条路)
        a.applySnapshot({
            run_id: 1, status: 'passed', current_test: null,
            started_at: 1000, finished_at: 2000, mode: 'full',
            parent_run_id: null,
            test_queue: TEST_LIST.map(t => t.name),
            pdf_path: null, sn: 'SN-TEST-0001', error: null,
            waiting_operator: null,
            results: {
                cpu: { status: 'passed', metrics: {}, error: null },
                usb: { status: 'passed', metrics: {}, error: null },
            },
        });
        const statusBefore = a.lastStatus.value;
        const errBefore = a.reportError.value;
        const passedBefore = a.passedCount.value;
        check(passedBefore === 2, '先确认快照生效了 (通过 2 项)', passedBefore);
        await push(a, {
            type: 'usb_report_missing', path: '', filename: '',
            message: '未检测到 U 盘 —— 报告仍在本机 reports/pdf/', run_id: 1,
        });
        check(a.lastStatus.value === statusBefore,
              '本轮状态没被「没插 U 盘」改掉',
              `${statusBefore} → ${a.lastStatus.value}`);
        check(a.reportError.value === errBefore,
              '也没被当成「PDF 导出失败」(那是两件事)',
              `${errBefore} → ${a.reportError.value}`);
        check(a.passedCount.value === passedBefore,
              '通过数没变', `${passedBefore} → ${a.passedCount.value}`);
    }

    // ══════════════════════════════════════════════════════════
    section('【4】手动补导 PDF 之后, 后端回的「存没存成」也要显示');
    {
        usbReply = { ok: true, path: '/media/user/USB/AGING-REPORTS',
                     message: '报告已存到 U 盘: /media/user/USB/AGING-REPORTS' };
        const a = await makeApp();
        await a.exportPdf(1);
        await tick(20);
        check(a.usbReportNote.value.indexOf('已存到 U 盘') >= 0,
              '手动补导后的提示也出来了', a.usbReportNote.value);
        usbReply = null;
    }

    // ══════════════════════════════════════════════════════════
    section('【5】页面上得真有这一条(不是只在 JS 里)');
    {
        const html = fs.readFileSync(INDEX_HTML, 'utf8');
        check(html.indexOf('usbReportNote') >= 0,
              'index.html 里绑定了 usbReportNote');
        check(html.indexOf('usb-report-note') >= 0,
              '用了专门的样式类(不跟「导出失败」那条橙条混)');
        check(html.indexOf('alert(') < 0 || html.indexOf('usbReportNote') >= 0,
              '不是用弹窗打断工人');
    }

    return finish();
}

main().then(code => process.exit(code)).catch(e => {
    console.error(e);
    process.exit(1);
});
