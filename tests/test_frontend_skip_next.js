/* 回归测试: 界面上的两个操作键 —— 「下一项」和「停止」(2026-10-29)。
 *
 * 产线的原话: 「应该要有两个操作键, 一个停止一个下一项的按钮, 停止是停止
 * 当前和所有的测试项然后直接跳到结果界面, 当前项和后续项都标记的是跳过;
 * 下一项就是停止当前的测试项进入到下一项测试, 当前项标记跳过。」
 *
 * 为什么值得钉死:
 *   ① **两个键不能混**。以前只有一个「停止」键, 按下去整轮就没了 —— 工人
 *      只想跳过一项(比如那一项的设备明显有问题, 后面还有 9 项要测), 只能
 *      硬着头皮跑完或者整轮重来。
 *   ② 「停止」现在跳过的项记 **skipped**(用户主动让它别测), 所以界面上的
 *      措辞必须跟着改 —— 还在说「用户中途停止」的话, 工人会以为是工具自己
 *      死了(§9.3: 跳过原因必须显式披露, 不能让「跳过」看起来像「通过」)。
 *   ③ **误按**是真实风险: 「停止」是不可逆的(这一轮剩下的项都不测了)。
 *      两个键都弹确认框, 且确认框里的文字说清后果。
 *
 * 这个测试盯五件事:
 *   ① index.html 里两个键都在, 且分别绑到 nextTest / stopTest;
 *   ② style.css 里两个键有样式(不是没样子的裸按钮);
 *   ③ app.js: 「下一项」打 /api/test/next, 「停止」打 /api/test/stop
 *      —— **打错了后端就收不到**, 界面看着按了, 实际什么都没发生;
 *   ④ 两个键都弹确认框;
 *   ⑤ 跳过原因的措辞: 「中止整轮」和「跳过此项」分开说。
 *
 * 跑法: node tests/test_frontend_skip_next.js
 */
'use strict';

const fs = require('fs');
const path = require('path');

/* 同别的前端测试: AGING_STATIC_DIR 指到别处就能对着**副本**跑 ——
 * 变异测试要把 static/ 改坏, 断言必须红, 否则这测试是个摆设。 */
const STATIC_DIR = process.env.AGING_STATIC_DIR
    || path.join(__dirname, '..', 'static');
/* 让 _vue_harness 也读副本里的 app.js (模块加载时就取值, 必须先设好)。 */
process.env.AGING_APP_JS = process.env.AGING_APP_JS
    || path.join(STATIC_DIR, 'app.js');

const { loadApp, makeChecker } = require('./_vue_harness');

const { check, section, finish } = makeChecker();

const INDEX_HTML = path.join(STATIC_DIR, 'index.html');
const STYLE_CSS = path.join(STATIC_DIR, 'style.css');

/** 剥掉 CSS 注释 —— 注释里也有冒号和花括号, 不剥抠出来的规则会张冠李戴。 */
function stripComments(css) {
    return css.replace(/\/\*[\s\S]*?\*\//g, '');
}

/** 抠出某个选择器的规则体(只认第一个匹配到的)。 */
function cssRule(css, selector) {
    const i = css.indexOf(selector);
    if (i < 0) return null;
    const open = css.indexOf('{', i);
    if (open < 0) return null;
    let depth = 0;
    for (let j = open; j < css.length; j++) {
        if (css[j] === '{') depth++;
        else if (css[j] === '}') {
            depth--;
            if (depth === 0) return css.slice(open + 1, j);
        }
    }
    return null;
}

/** 从规则体里读一个声明的值(取最后一个 —— 后写的赢)。 */
function decl(rule, prop) {
    if (!rule) return null;
    const re = new RegExp('(?:^|;)\\s*' + prop + '\\s*:\\s*([^;]+)', 'g');
    let m, last = null;
    while ((m = re.exec(rule)) !== null) last = m[1].trim();
    return last;
}

/** 造一个 fetch 桩: 记下打过的 URL, 假装成功。 */
function fetchRecorder() {
    const calls = [];
    const fetch = (url, opts) => {
        calls.push({ url, opts: opts || {} });
        return Promise.resolve({
            ok: true,
            status: 200,
            json: () => Promise.resolve({ ok: true }),
        });
    };
    return { calls, fetch };
}

async function main() {
    const html = fs.readFileSync(INDEX_HTML, 'utf8');
    const css = stripComments(fs.readFileSync(STYLE_CSS, 'utf8'));

    // ══════════════════════════════════════════════════════════
    section('【1】index.html: 两个键都在, 分别绑到 nextTest / stopTest');
    {
        const iNext = html.indexOf('@click="nextTest"');
        const iStop = html.indexOf('@click="stopTest"');
        check(iNext >= 0, '有一个键绑到 nextTest(「下一项」)', iNext);
        check(iStop >= 0, '有一个键绑到 stopTest(「停止」)', iStop);
        check(iNext !== iStop, '这是**两个**不同的键, 不是同一个', { iNext, iStop });

        const iActions = html.indexOf('class="wizard-actions"');
        check(iActions >= 0, '两个键摆在 .wizard-actions 里', iActions);
        check(iActions < iNext && iNext < iStop,
              '两个键都在测试界面的顶栏里(工人跑测时手边就有)', {
                  iActions, iNext, iStop,
              });

        // 键面上的字得让人一眼分清 —— 「停止」两个字够狠, 「下一项」不够
        check(/下一项/.test(html), '界面上有「下一项」这个字');
        check(/停止/.test(html), '界面上有「停止」这个字');

        // 提示(title)里要说清后果: 工人不看字, 悬停时得告诉他按下去会怎样
        const iTitleNext = html.indexOf('跳过当前这一项');
        const iTitleStop = html.indexOf('中止整轮测试');
        check(iTitleNext >= 0, '「下一项」的提示写明它只跳当前这一项', iTitleNext);
        check(iTitleStop >= 0, '「停止」的提示写明它中止整轮', iTitleStop);
    }

    // ══════════════════════════════════════════════════════════
    section('【2】style.css: 两个键有样式(不是没样子的裸按钮)');
    {
        const actions = cssRule(css, '.wizard-actions');
        check(actions !== null, 'CSS 里有 .wizard-actions');
        check(decl(actions, 'display') === 'flex',
              '.wizard-actions 是 flex (两个键并排)', decl(actions, 'display'));

        const next = cssRule(css, '.btn-next');
        check(next !== null, 'CSS 里有 .btn-next');
        check(decl(next, 'border') !== null || decl(next, 'border-style') !== null,
              '.btn-next 有边框(看得出是个键)', decl(next, 'border'));

        const danger = cssRule(css, '.btn-danger');
        check(danger !== null, 'CSS 里有 .btn-danger');
    }

    // ══════════════════════════════════════════════════════════
    section('【3】app.js: 「下一项」打 /api/test/next, 「停止」打 /api/test/stop');
    {
        const rec = fetchRecorder();
        const app = loadApp({ fetch: rec.fetch, confirm: () => true });

        await app.nextTest();
        check(rec.calls.length === 1, '「下一项」只发了一个请求', rec.calls.length);
        check(rec.calls[0] && rec.calls[0].url === '/api/test/next',
              '「下一项」打的是 /api/test/next',
              rec.calls[0] && rec.calls[0].url);
        check(rec.calls[0] && rec.calls[0].opts.method === 'POST',
              '用的是 POST', rec.calls[0] && rec.calls[0].opts.method);

        rec.calls.length = 0;
        await app.stopTest();
        check(rec.calls.length === 1, '「停止」只发了一个请求', rec.calls.length);
        check(rec.calls[0] && rec.calls[0].url === '/api/test/stop',
              '「停止」打的是 /api/test/stop',
              rec.calls[0] && rec.calls[0].url);
        check(rec.calls[0] && rec.calls[0].opts.method === 'POST',
              '用的是 POST', rec.calls[0] && rec.calls[0].opts.method);
    }

    // ══════════════════════════════════════════════════════════
    section('【4】两个键都弹确认框(「停止」不可逆, 误按代价太大)');
    {
        const asked = [];
        const rec = fetchRecorder();
        const app = loadApp({
            fetch: rec.fetch,
            confirm: (msg) => { asked.push(String(msg)); return true; },
        });

        await app.nextTest();
        check(asked.length === 1, '「下一项」弹了确认框', asked.length);
        check(asked[0] && asked[0].includes('下一项'),
              '确认框里写明是「下一项」', asked[0]);
        check(asked[0] && asked[0].includes('跳过'),
              '确认框里写明当前项会记「跳过」', asked[0]);

        asked.length = 0;
        await app.stopTest();
        check(asked.length === 1, '「停止」弹了确认框', asked.length);
        check(asked[0] && asked[0].includes('停止'),
              '确认框里写明是「停止」', asked[0]);
        check(asked[0] && asked[0].includes('跳过'),
              '确认框里写明当前项和后面的项都会记「跳过」', asked[0]);

        // 工人点了「取消」→ **不许**发请求(不然确认框只是个摆设)
        asked.length = 0;
        rec.calls.length = 0;
        const app2 = loadApp({
            fetch: rec.fetch,
            confirm: () => false,
        });
        await app2.stopTest();
        await app2.nextTest();
        check(rec.calls.length === 0,
              '点了「取消」就**不**发请求(确认框不是摆设)', rec.calls.length);
    }

    // ══════════════════════════════════════════════════════════
    section('【5】跳过原因的措辞: 「中止整轮」和「跳过此项」分开说');
    {
        const app = loadApp({});

        check(app.skipReasonLabel('aborted_by_user') === '用户中止整轮',
              '「中止整轮」的标签',
              app.skipReasonLabel('aborted_by_user'));
        check(app.skipReasonLabel('skipped_by_user') === '用户跳过此项',
              '「跳过此项」的标签',
              app.skipReasonLabel('skipped_by_user'));
        check(app.skipReasonLabel('no_card') === '环境原因跳过',
              '环境原因的跳过**另**有一个标签(不许混成「用户主动」)',
              app.skipReasonLabel('no_card'));

        // 措辞里不许出现「建议处理工位环境」—— 那是给环境原因准备的,
        // 工人自己按的按钮, 让他去查网线是**指错方向**。
        check(app.skipReasonNote('aborted_by_user').includes('重测'),
              "「中止整轮」的说明指向「重测这一项」, 不是「查环境」",
              app.skipReasonNote('aborted_by_user'));
        check(app.skipReasonNote('no_card').includes('环境'),
              "环境原因的说明才提「工位环境」",
              app.skipReasonNote('no_card'));

        check(app.envReasonText('aborted_by_user').includes('中止'),
              "「中止整轮」的原因说明里说得明是工人自己按的",
              app.envReasonText('aborted_by_user'));
        check(app.envReasonText('skipped_by_user').includes('跳过'),
              "「跳过此项」的原因说明里说得明是工人自己按的",
              app.envReasonText('skipped_by_user'));
    }

    return finish();
}

/* main 是 async —— 必须等它跑完再拿退出码 (process.exit 不吃 Promise)。 */
main().then((code) => process.exit(code)).catch((e) => {
    console.error(e);
    process.exit(1);
});
