/* 回归测试: 界面上的**大横幅**(alert 事件) —— 「屏幕马上要黑了」。
 *
 * 起因 (2026-10-28, 产线提的): 断开→恢复那一轮, 提示只写进了**日志框**。
 * 但马上要黑屏的那一块, 就是工人正盯着的那一块 —— 屏就在那儿变黑, 而
 * 提示写在屏幕**最下面**的日志框里, 工人根本来不及看见; 而屏幕一黑,
 * 连日志框都没了, 只剩一片黑。现场表现就是「工具卡死了 / 屏坏了」。
 *
 * 所以提示得挪到**进度条底下**, 摆成大字 + 重底色 + 倒计时, 抢那一眼。
 * 这个测试盯的就是这条横幅:
 *   · 收到 alert → 横幅摆出来, 带文字/颜色/倒计时
 *   · 倒计时真的在走 (前端自己数, 不是干等着)
 *   · 空 text → 横幅收起来 (倒计时走完、后端开始真操作了)
 *   · 换一项 / 整轮结束 → 横幅也得收 (不能杵在下一项界面上)
 *
 * 跑法: node tests/test_frontend_alert.js
 */
'use strict';

const fs = require('fs');
const path = require('path');

/* 同别的前端测试: 给 AGING_* 换个路径就能对着别的副本跑 ——
 * 用来验证「这个测试真的会红」: 把横幅改坏, 断言必须失败。
 * (必须在 require 之前设好 —— 模块加载时就取值了。) */
const STATIC_DIR = process.env.AGING_STATIC_DIR
    || path.join(__dirname, '..', 'static');
process.env.AGING_APP_JS = process.env.AGING_APP_JS
    || path.join(STATIC_DIR, 'app.js');

const { loadApp, makeChecker } = require('./_vue_harness');

const { check, section, finish } = makeChecker();

const html = fs.readFileSync(path.join(STATIC_DIR, 'index.html'), 'utf8');
const css = fs.readFileSync(path.join(STATIC_DIR, 'style.css'), 'utf8');

function tick(ms) {
    return new Promise(r => setTimeout(r, ms));
}

/* 起一个「跟真浏览器一样」的 app: 跑 onMounted(拉配置 + 连 WS),
 * 这样才能拿假 socket 喂事件进去。 */
async function makeApp() {
    const a = loadApp({
        mount: true,
        fetch: () => Promise.resolve({
            ok: true,
            json: () => Promise.resolve({
                tests: [{ name: 'hdmi', display_name: 'HDMI 输出', enabled: true }],
                defaults: { hdmi: { replug_check: true } },
            }),
        }),
    });
    await tick(20);
    return a;
}

async function send(app, msg) {
    await app.__sockets[0].onmessage({ data: JSON.stringify(msg) });
    await tick(5);
}

async function main() {
    /* ══════════════════════════════════════════════════════════
     * 【1】页面结构: 横幅**摆在进度条底下**, 而且够显眼
     * ══════════════════════════════════════════════════════════ */
    section('【1】横幅在页面上: 摆在进度条底下, 大字 + 重色');
    {
        const iBar = html.indexOf('class="wizard-progress-line"');
        // 横幅的 class 是**动态**的(:class="['wizard-alert', 颜色]"),
        // 所以只能按名字找, 找 'class="wizard-alert"' 永远找不到。
        const iAlert = html.indexOf('wizard-alert');
        check(iBar >= 0, '页面上有进度条那一行');
        check(iAlert >= 0, '页面上有横幅那个元素');
        check(iBar >= 0 && iAlert > iBar, '横幅在**进度条底下**',
              { iBar, iAlert });
        check(/alertBanner/.test(html), '横幅绑的是 alertBanner');
        check(/alertBanner\.level/.test(html),
              '横幅绑的是 alertBanner.level(颜色得跟着后端走, 不能所有提示一个样)');
        check(/alert-countdown/.test(html), '横幅里有倒计时那个格子');
        check(/alert-text/.test(html), '横幅里有文字那格');
        check(/alertBanner\.countdown > 0/.test(html),
              '倒计时归零之后数字那一格就藏掉(但横幅还在, 等后端收)');

        check(/\.wizard-alert\s*\{/.test(css), 'CSS 里有 .wizard-alert');
        const m = css.match(/\.wizard-alert\s*\{[^}]*\}/);
        const blk = m ? m[0] : '';
        const fs2 = blk.match(/font-size:\s*(\d+)px/);
        check(fs2 && Number(fs2[1]) >= 18,
              '字号够大(≥18px, 工人一眼能看见)', fs2 && fs2[1]);
        check(/\.wizard-alert\.danger\s*\{/.test(css), '有 danger 这一档(红)');
        check(/animation:\s*alert-blink/.test(css), '横幅会一闪一闪(抢注意力)');
    }

    /* ══════════════════════════════════════════════════════════
     * 【2】收到 alert → 横幅摆出来
     * ══════════════════════════════════════════════════════════ */
    section('【2】收到 alert 事件 → 横幅摆出来');
    {
        const a = await makeApp();
        check(a.__sockets.length === 1, 'WS 连上了', a.__sockets.length);
        check(a.alertBanner.value === null, '一开始没有横幅', a.alertBanner.value);

        await send(a, {
            type: 'alert',
            name: 'hdmi',
            text: '屏幕即将黑屏 —— 这块屏(HDMI-1)会被自动关掉 3 秒, 属正常现象; 不用拔线、不用操作',
            level: 'danger',
            seconds: 3,
        });
        const b = a.alertBanner.value;
        check(b !== null, '横幅摆出来了', b);
        if (b) {
            check(/黑屏/.test(b.text), '横幅文字是后端给的那句', b.text);
            check(b.level === 'danger', '颜色是 danger', b.level);
            check(b.countdown === 3, '倒计时从 3 开始', b.countdown);
        }
    }

    /* ══════════════════════════════════════════════════════════
     * 【3】倒计时自己会走 (前端数, 不是干等后端)
     * ══════════════════════════════════════════════════════════ */
    section('【3】倒计时每秒减 1');
    {
        const a = await makeApp();
        await send(a, { type: 'alert', name: 'hdmi', text: 'x', level: 'danger', seconds: 3 });
        check(a.alertBanner.value.countdown === 3, '一开始是 3', a.alertBanner.value.countdown);
        await tick(1100);
        check(a.alertBanner.value && a.alertBanner.value.countdown === 2,
              '过了一秒变成 2', a.alertBanner.value && a.alertBanner.value.countdown);
        await tick(1100);
        check(a.alertBanner.value && a.alertBanner.value.countdown === 1,
              '又过一秒变成 1', a.alertBanner.value && a.alertBanner.value.countdown);
        check(a.alertBanner.value !== null,
              '数到 0 之后**横幅还在**(撤不撤由后端说了算)');
    }

    /* ══════════════════════════════════════════════════════════
     * 【4】空 text = 收起横幅
     * ══════════════════════════════════════════════════════════ */
    section('【4】空 text → 收起横幅(倒计时走完、后端开始真操作了)');
    {
        const a = await makeApp();
        await send(a, { type: 'alert', name: 'hdmi', text: '马上黑屏', level: 'danger', seconds: 3 });
        check(a.alertBanner.value !== null, '先摆出来');
        await send(a, { type: 'alert', name: 'hdmi', text: '', level: 'warn', seconds: 0 });
        check(a.alertBanner.value === null, '收起来了', a.alertBanner.value);
    }

    /* ══════════════════════════════════════════════════════════
     * 【5】换一项 / 整轮结束 → 横幅不许留在界面上
     * ══════════════════════════════════════════════════════════ */
    section('【5】换一项、整轮结束 → 横幅都收掉');
    {
        const a = await makeApp();
        await send(a, { type: 'alert', name: 'hdmi', text: '马上黑屏', level: 'danger', seconds: 3 });
        await send(a, { type: 'test_started', name: 'hdmi' });
        check(a.alertBanner.value === null, '换项时收掉了', a.alertBanner.value);

        await send(a, { type: 'alert', name: 'hdmi', text: '马上黑屏', level: 'danger', seconds: 3 });
        await send(a, {
            type: 'run_finished', status: 'passed',
            summary: { total: 1, passed: 1, failed: 0, error: 0, skipped: 0, not_run: 0, planned: 1 },
        });
        check(a.alertBanner.value === null, '整轮结束时收掉了', a.alertBanner.value);
    }

    /* ══════════════════════════════════════════════════════════
     * 【6】没有倒计时(seconds=0)的横幅也摆得出来
     * ══════════════════════════════════════════════════════════ */
    section('【6】不带倒计时的横幅也照摆');
    {
        const a = await makeApp();
        await send(a, { type: 'alert', name: 'x', text: '只是一条通知', level: 'info', seconds: 0 });
        check(a.alertBanner.value !== null, '横幅摆出来了', a.alertBanner.value);
        check(a.alertBanner.value.countdown === 0, '倒计时是 0(不显示数字)', a.alertBanner.value.countdown);
        check(a.alertBanner.value.level === 'info', '颜色是 info', a.alertBanner.value.level);
    }

    console.log('');
    process.exit(finish());
}

main().catch((e) => {
    console.error(e);
    process.exit(1);
});
