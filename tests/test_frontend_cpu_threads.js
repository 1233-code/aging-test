/* 回归测试: CPU 压力项的「线程数」旋钮 (§6.2 加压封顶值可调)。
 *
 * 为什么要有这个旋钮 (2026-11-03 板子实测逼出来的):
 * 板子 A10000000000010 在 CPU 加压项**每次都被硬复位**, 而同一台板子在
 * 终端里跑同样的 stress-ng 却没事。五轮死亡的现场记录折算下来:
 *   · 死在**总占用 4.4~5.7 核**(8 核机器)的时候;
 *   · 而应用**自己**在这台机器上就占了 0.7~2.3 核(网页 + 后台),
 *     所以「2 线程」那一片的实际总占用就已经 4.4 核了。
 * ⇒ 供电的崖在 ~4.3 核附近, 而**加压封顶在几线程**是这一项唯一能立刻
 *   拧小的旋钮 —— 必须**在页面上就能拧**, 不用改代码、不用出包、不用
 *   重启服务(static/ 是热的, 刷新页面就生效)。
 *
 * 这个测试守四件事:
 *   ① 页面上真的有这个输入框, 而且绑的是 `tests[t.name].threads`
 *      —— 绑错了键, 工人拧的是空气, 后端收到的还是 8;
 *   ② 只有**带 threads 键的项**才显示它 (现在只有 CPU 项有)
 *      —— 别的项冒出一个「线程数」框, 工人会以为那些项也能调线程;
 *   ③ 工人拧的那个数**真的发给了后端** (startTest 的请求体里带着它)
 *      —— 这一条最要紧: 输入框好看但值没送出去, 是最难发现的坏法;
 *   ④ **范围 1~8**(2026-11-10 用户点名: 「cpu核数控制在最低是1,最高是8」)
 *      —— 以前下界是 0, 而 **0 = 「自动」= 按核数 = 满核 8** ⇒ 那正是
 *      2026-11-04 实测会把板子压死的那一档 ⇒ 0 现在填不进来(夹到 1)。
 *
 * 跑法: node tests/test_frontend_cpu_threads.js
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
const html = fs.readFileSync(INDEX_HTML, 'utf8');

/** 后端 /api/config 的载荷 —— 只留这个测试用得上的部分。
 *  defaults.cpu.threads 来自 config.yaml 的 `defaults.cpu`
 *  (现在是 **4** = 默认 4 核「保险档」—— 2026-11-09 晚更晚定的: 「确定了,
 *   默认四核, 但是像现在这样支持用户自己选择核数, 四核比较保险」;
 *   ⚠️ **默认值 ≠ 上限**: 旋钮 max 仍是 8, 硬上限也仍是 8 ⇒ 用户照样能
 *   自己拧到 8; 2026-11-09 白天是 5, 2026-11-04 是 6, 更早是 0 = 全部核),
 *  页面靠它把这个框**填上初值** —— 后端不下发, 框就是空的, 工人拧不动。 */
const CONFIG_PAYLOAD = {
    defaults: {
        cpu: {
            enabled: true,
            times: 1,
            duration_sec: 60,
            threads: 4,
            max_temp_c: 85,
            abort_temp_c: 95,
            slice_sec: 5,
        },
        gpu: { enabled: true, rounds: 1, scene_sec: 3 },
        memory: { enabled: true, times: 1 },
    },
    tests: [
        { name: 'cpu', display_name: 'CPU 压力', description: 'stress-ng 加压' },
        { name: 'memory', display_name: '内存', description: 'memtester' },
        { name: 'gpu', display_name: 'GPU', description: 'glmark2' },
    ],
    recommended_order: ['memory', 'cpu'],
    interfaces: {},
    sn: 'SN-TEST-0001',
    report: { operator: '' },
};

/** 起一个 app, 把 fetch 换成桩, 顺手记下 POST 的请求体。 */
function makeApp() {
    const sent = { start: null };
    const fetchStub = async (url, opts) => {
        if (String(url).indexOf('/api/test/start') >= 0) {
            sent.start = JSON.parse((opts && opts.body) || '{}');
            return { json: async () => ({ ok: true, run_id: 7 }) };
        }
        if (String(url).indexOf('/api/config') >= 0) {
            return { json: async () => JSON.parse(JSON.stringify(CONFIG_PAYLOAD)) };
        }
        if (String(url).indexOf('/api/deps/check') >= 0) {
            return { json: async () => ({}) };
        }
        return { json: async () => ({}) };
    };
    const app = loadApp({ fetch: fetchStub, mount: true });
    return { app, sent };
}

async function main() {
    // ══════════════════════════════════════════════════════════
    section('【1】页面上有「线程数」输入框, 而且绑对了键');
    {
        check(/v-model(?:\.number)?="tests\[t\.name\]\.threads"/.test(html),
            '有绑定 tests[t.name].threads 的输入框');
        check(/v-if="'threads' in tests\[t\.name\]"/.test(html),
            '这个框用 \'threads\' in tests[t.name] 守卫 (只有带这键的项才显示)');
        check(/线程数/.test(html), '框旁边有「线程数」的字样 (工人认得出来)');
        // min="1": **下限 1**(2026-11-10 用户点名: 「cpu核数控制在最低是1,
        //   最高是8」)—— 以前是 min="0", 而 **0 = 「自动」= 按核数 = 满核 8**
        //   ⇒ 那正是 2026-11-04 实测会把板子压死的那一档 ⇒ 0 填不进来。
        // max="8": **加压上限 8 核(满核)**(2026-11-09 晚放回; 当天白天是 5,
        //   再之前是 6)—— 输入框的上限要跟后端那道夹子一致, 否则工人填 6
        //   会被浏览器挡掉(上限 5 的时候他连 6 都试不了)。
        const m = html.match(/<input[^>]*v-model(?:\.number)?="tests\[t\.name\]\.threads"[^>]*>/);
        check(!!m, '找得到那个 input 标签');
        if (m) {
            check(/min="1"/.test(m[0]),
                'min="1" (下限 1 —— 「0 = 自动 = 满核」那条路封了)',
                m[0]);
            check(!/min="0"/.test(m[0]),
                '**不是** min="0"(0 = 自动 = 按核数 = 满核, 会把板子压死)',
                m[0]);
            check(/type="number"/.test(m[0]), '是 number 类型 (能填数字, 不填串)');
            check(/max="8"/.test(m[0]),
                'max="8" (加压上限 8 核 = 满核 —— 工人要自己拧线程数找平衡点)',
                m[0]);
        }
        // 2026-11-04: 「默认 4 核(上限 8); 0 = 自动」那句从**版面上**撤掉了
        // (右列只摆旋钮) ⇒ 挪进了这个输入框的 title(悬停才看得到)。
        // ⚠️ **不许整个消失** —— 上限是反直觉的(框上写 8, 后端也只给 8),
        // 工人得知道拧 8 = 满核。
        check(!!m && /title="默认 4 核\(范围 1~8; 拧到 8 = 满核, 过应力那一档\)"/.test(m[0]),
            '「默认 4 核(范围 1~8; 拧到 8 = 满核, 过应力那一档)」在那个输入框的 title 里(悬停提示)',
            m ? m[0] : '(没找到那个 input)');
    }

    // ══════════════════════════════════════════════════════════
    section('【2】后端下发的默认值进到框里 (默认 4 核)');
    {
        const { app } = makeApp();
        await new Promise((r) => setTimeout(r, 0));
        await new Promise((r) => setTimeout(r, 0));
        check(!!app.tests.cpu, 'tests.cpu 拿到了');
        check(app.tests.cpu && app.tests.cpu.threads === 4,
            'tests.cpu.threads = 4 (来自后端 defaults.cpu.threads)',
            app.tests.cpu ? JSON.stringify(app.tests.cpu) : '(没有 tests.cpu)');
        check(app.tests.memory && app.tests.memory.threads === undefined,
            '没有 threads 键的项 (memory) 不带这个字段 —— 框不会误显示');
    }

    // ══════════════════════════════════════════════════════════
    section('【3】工人拧的那个数**真的发给了后端**');
    {
        const { app, sent } = makeApp();
        await new Promise((r) => setTimeout(r, 0));
        await new Promise((r) => setTimeout(r, 0));

        app.tests.cpu.threads = 2;
        await app.startTest();

        check(!!sent.start, 'POST /api/test/start 发出去了');
        check(sent.start && sent.start.tests
            && sent.start.tests.cpu
            && sent.start.tests.cpu.threads === 2,
            '请求体里 tests.cpu.threads = 2 (后端收到的就是拧小后的值)',
            sent.start ? JSON.stringify(sent.start.tests && sent.start.tests.cpu) : '(没抓到)');
    }

    // ══════════════════════════════════════════════════════════
    section('【4】填 0 / 填 99 都会被夹进 1~8(「0 = 自动 = 满核」那条路封死了)');
    {
        // ⚠️ **为什么前端也要夹一道**: 光靠后端夹, 工人看见框里是 0、这一轮
        //   实际压 1 核 ⇒ 界面跟实际对不上(这套系统栽过这种跟头)。
        //   ⇒ 夹完**写回框里**, 让人看得见。
        const { app, sent } = makeApp();
        await new Promise((r) => setTimeout(r, 0));
        await new Promise((r) => setTimeout(r, 0));

        app.tests.cpu.threads = 0;
        await app.startTest();

        check(sent.start && sent.start.tests
            && sent.start.tests.cpu
            && sent.start.tests.cpu.threads === 1,
            '填 0 ⇒ 请求体里是 1(夹到下限; **不是**「自动 = 满核 8」)',
            sent.start ? JSON.stringify(sent.start.tests && sent.start.tests.cpu) : '(没抓到)');
        check(app.tests.cpu.threads === 1,
            '填 0 ⇒ 框里也变成 1(夹完写回 —— 界面不能跟实际压的核数对不上)',
            app.tests.cpu ? String(app.tests.cpu.threads) : '(没有 tests.cpu)');

        app.tests.cpu.threads = 99;
        await app.startTest();
        check(sent.start && sent.start.tests
            && sent.start.tests.cpu
            && sent.start.tests.cpu.threads === 8,
            '填 99 ⇒ 请求体里是 8(夹到上限)',
            sent.start ? JSON.stringify(sent.start.tests && sent.start.tests.cpu) : '(没抓到)');
    }
}

main().then(() => {
    process.exit(finish());
}).catch((e) => {
    console.error(e);
    process.exit(1);
});
