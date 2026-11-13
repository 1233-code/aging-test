/* 回归测试: **盘上空间提醒** —— 只在空间不够的时候才出现在界面上。
 *
 * 起因 (2026-11-13 工人提的): U 盘既是启动盘又是数据根(报告/历史库/日志
 * 都落在它上面)。盘写满时的失败是**静默丢数据**那一种:
 *   · 报告 PDF 导出失败 —— 页面会报错, 工人看得见;
 *   · `history.db` 写不进去 ⇒ 那一轮的结果记不进库, 极端情况把库写坏
 *     ⇒ 板子明明测了, 记录却没了。
 * ⇒ 在开跑之前提醒一句。
 *
 * **用户定的规矩 (这个测试就是守它)**:
 *   ① **只在空间不足的时候显示** —— 空间充足时**任何地方都不显示提醒**;
 *   ② **提醒, 不拦截** —— 空间不够也照样能开测;
 *   ③ 措辞不许吓人 —— 第一句先讲「现在还能不能测」(文案由后端出,
 *      前端一个字都不改 ⇒ 单一口径; 措辞本身由 tests/test_disk_space.py 守)。
 *
 * ⚠️ 特别要盯住的: **不许哪天又手痒挂一个「剩余 54 GB」的仪表盘** ——
 *    工人不看, 看了也只会麻木(狼来了 ⇒ 真出事那天就不当回事了)。
 *
 * 跑法: node tests/test_frontend_disk_warning.js
 */
'use strict';

const fs = require('fs');
const path = require('path');
const { loadApp, makeChecker } = require('./_vue_harness');

const { check, section, finish } = makeChecker();

/* 默认读仓库里的文件; 给环境变量换个路径就能对着别的副本跑 ——
 * 用来验证「这个测试真的会红」: 把提醒改坏, 断言必须失败。 */
const STATIC_DIR = process.env.AGING_STATIC_DIR || path.join(__dirname, '..', 'static');
const INDEX_HTML = process.env.AGING_INDEX_HTML || path.join(STATIC_DIR, 'index.html');
const APP_JS = process.env.AGING_APP_JS || path.join(STATIC_DIR, 'app.js');

const html = fs.readFileSync(INDEX_HTML, 'utf8');
const appJs = fs.readFileSync(APP_JS, 'utf8');

function tick(ms) {
    return new Promise(r => setTimeout(r, ms));
}

const DISK_LOW = {
    level: 'warn',
    free_mb: 500,
    devices_left: 294,
    message: 'U 盘剩余空间 500 MB —— 按现在这个用量, 大约还能存 294 台设备。'
        + '**现在还能正常测**, 但报告快存不下了 ⇒ 建议先清一下盘再测。',
};

/* 起一个 app: fetch 桩按 url 给不同的回答。 */
async function makeApp(configDisk, startDisk) {
    const a = loadApp({
        mount: true,
        fetch: (url) => {
            if (String(url).indexOf('/api/test/start') >= 0) {
                return Promise.resolve({
                    ok: true,
                    json: () => Promise.resolve({
                        ok: true, run_id: 1, disk: startDisk,
                    }),
                });
            }
            return Promise.resolve({
                ok: true,
                json: () => Promise.resolve({
                    sn: 'TEST-SN',
                    tests: [{ name: 'hdmi', display_name: 'HDMI 输出' }],
                    defaults: { hdmi: {} },
                    recommended_order: ['hdmi'],
                    report: {},
                    disk: configDisk,
                }),
            });
        },
    });
    await tick(30);
    return a;
}

async function main() {
    /* ── ① 空间够 ⇒ 界面上什么都没有 ─────────────────────── */
    section('【1】空间够 ⇒ 不显示任何提醒');
    {
        const a = await makeApp(null, null);
        check(a.diskWarning.value === null,
              '/api/config 的 disk 是 null ⇒ 界面上没有这条', a.diskWarning.value);
    }
    {
        /* 后端干脆不下发这个字段(老版本服务)⇒ 前端也不许自己造一个 */
        const a = await makeApp(undefined, undefined);
        check(a.diskWarning.value === null,
              '后端没带 disk 字段 ⇒ 界面上也没有', a.diskWarning.value);
    }

    /* ── ② 空间不够 ⇒ 出现, 而且是后端那句话 ─────────────── */
    section('【2】空间不够 ⇒ 出现提醒');
    {
        const a = await makeApp(DISK_LOW, null);
        check(!!a.diskWarning.value, '后端报了空间不足 ⇒ 状态里有这条');
        check(a.diskWarning.value && a.diskWarning.value.message === DISK_LOW.message,
              '显示的是后端出的那句话(单一口径, 前端不改字)');
    }

    /* ── ③ 开跑那一刻再查一次 ────────────────────────────── */
    section('【3】开跑那一刻再查一次(页面加载时的快照会过期)');
    {
        const a = await makeApp(null, DISK_LOW);
        await a.startTest();
        await tick(20);
        check(!!a.diskWarning.value,
              '开跑返回里带了空间不足 ⇒ 界面上出现提醒', a.diskWarning.value);
    }
    {
        const a = await makeApp(DISK_LOW, null);
        await a.startTest();
        await tick(20);
        check(a.diskWarning.value === null,
              '开跑时空间够 ⇒ 不显示(哪怕加载页面时不够)', a.diskWarning.value);
    }

    /* ── ④ 模板: 只在有值时才渲染 ────────────────────────── */
    section('【4】模板只在有值时才渲染');
    check(/v-if="diskWarning"/.test(html),
          '模板里是 v-if ⇒ 没有值就整块不出现');
    check(/diskWarning\.message/.test(html),
          '模板渲染的是后端那句话');
    check(!/diskWarning\.free_mb/.test(html) && !/diskWarning\.devices_left/.test(html),
          '前端**不**自己摆「剩余多少 MB / 还能存几台」的数'
          + '(那是提醒的一部分, 不是仪表盘)');

    /* ── ⑤ 不许有「空间正常」的那种常驻显示 ──────────────── */
    section('【5】不许出现「空间充足」的常驻显示');
    check(!/剩余\s*\{\{|\{\{\s*diskWarning\.free_mb/.test(html),
          '模板里没有单独摆「剩余空间」的仪表盘');
    check(!/disk_free|diskFree|spaceGauge/.test(appJs),
          'app.js 里没有另外造一个「空间仪表盘」状态');

    /* ── ⑥ 前端一个字都不许自己编文案 ────────────────────── */
    section('【6】文案由后端出, 前端不改字');
    check(appJs.indexOf('diskWarning.value = data.disk || null;') >= 0,
          '前端只是把后端给的对象整个接过来');
    check(!/diskWarning\.message\s*=/.test(appJs),
          '前端没有自己改这句话(改了就成第二个口径了)');

    console.log('');
    process.exit(finish());
}

main().catch((e) => {
    console.error(e);
    process.exit(1);
});
