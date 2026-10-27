/* 加载 static/app.js 的测试脚手架 —— 不起服务、不碰数据库、不碰真硬件。
 *
 * 做法: 把 app.js 丢进一个 vm context, 把 Vue 换成够用的假实现
 * (ref / reactive / computed / watch / onMounted), 然后调 setup() 拿到它
 * 导出的状态和方法, 直接拿合成数据驱动。
 *
 * 为什么要这么绕: 前端那些「状态恢复」「就地展开」的逻辑全在浏览器这一侧,
 * 真起服务 + 驱动浏览器既慢又脆(还得跟产线上的实例抢端口)。而这些逻辑
 * 恰恰是最容易出回归的 —— 「点一行展开在它下面」这种, 靠肉眼看一次根本
 * 验不全(收起、切换、竞态、离开页面重置)。
 *
 * 假实现的两个约定:
 *   · computed 做成 getter —— 每次读都重算。真 Vue 是缓存的, 但这里要的
 *     就是「读到最新值」, 缓存与否对断言没影响。
 *   · 每个用例都要一份**全新**的 app 实例(状态互不干扰), 所以 Vue 假实现
 *     是每次 loadApp() 现造的, 不是模块级单例。
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

/* AGING_APP_JS: 指到别处去, 就能对着**副本**跑 app.js ——
 * 用来做「变异测试」: 把副本改坏, 断言必须红, 否则这测试是个摆设。
 * (配合 AGING_STATIC_DIR 一起用, 副本里 html/css/js 是同一套。) */
const APP_JS = process.env.AGING_APP_JS
    || path.join(__dirname, '..', 'static', 'app.js');

function makeVue(grab, sockets, mount) {
    return {
        createApp(options) {
            return {
                mount() {
                    grab(options.setup());
                    return {};
                },
            };
        },
        ref: (v) => ({ value: v }),
        reactive: (o) => o,
        computed: (fn) => ({ get value() { return fn(); } }),
        watch: () => {},
        // ⚠️ 默认**不跑** onMounted 的回调 —— 它会去 loadConfig / connectWS,
        //    那是启动流程, 不是每个用例都要的东西。
        //    opts.mount = true 时才真跑 (想测「收到某条 WS 事件之后界面变成
        //    什么样」就得开这个 —— WS 是在 onMounted 里连上的)。
        onMounted: (fn) => {
            if (!mount) return;
            try {
                const r = fn();
                if (r && typeof r.catch === 'function') r.catch(() => {});
            } catch (e) { /* 启动失败不该让用例炸掉 */ }
        },
        nextTick: () => Promise.resolve(),
    };
}

/**
 * @param {object} [opts]
 * @param {function} [opts.fetch]  fetch 桩 —— 默认直接 reject(测试里不该
 *                                   真发网络请求)。要测网络交互就自己传。
 * @param {boolean} [opts.mount]   真跑 onMounted(连 WS)。开了之后
 *                                   app.__sockets 里能拿到假 socket,
 *                                   调 ws.onmessage({data: JSON.stringify(msg)})
 *                                   就能喂一条 WS 事件进去。
 */
function loadApp(opts) {
    opts = opts || {};
    const src = fs.readFileSync(APP_JS, 'utf8');
    let app = null;
    const sockets = [];
    const ctx = {
        Vue: makeVue((a) => { app = a; }, sockets, opts.mount),
        // window / document 只提供 app.js 在**启动流程**里用到的那几个空壳方法
        // (addEventListener / visibilitychange) —— 少了它们, onMounted 会在
        // 第一行就抛, 后面的 loadConfig / connectWS 全都跑不到。
        window: { addEventListener: () => {}, removeEventListener: () => {} },
        document: { addEventListener: () => {}, removeEventListener: () => {} },
        console,
        setTimeout, clearTimeout, setInterval, clearInterval,
        fetch: opts.fetch || (() => Promise.reject(new Error('测试里不该发网络请求'))),
        // confirm / alert 是浏览器里**会打断人的**两个函数 —— 测试里必须换成
        // 假实现, 否则一点「重测」就卡在那儿等真人点确定。
        // 默认: confirm 一律同意, alert 记进 opts.alerts 里供断言。
        confirm: opts.confirm || (() => true),
        alert: opts.alert || ((msg) => { ctx.__alerts.push(String(msg)); }),
        location: { protocol: 'http:', host: '127.0.0.1:8000' },
        navigator: {},
        WebSocket: function () {
            this.close = () => {};
            this.send = () => {};
            this.readyState = 1;
            sockets.push(this);
        },
        JSON, Math, Date, Object, Array, Map, Set, String, Number, Boolean,
        Promise, Error, isNaN, parseInt, parseFloat,
    };
    ctx.WebSocket.OPEN = 1;
    ctx.__alerts = [];
    ctx.globalThis = ctx;
    vm.createContext(ctx);
    vm.runInContext(src, ctx, { filename: 'app.js' });
    if (!app) throw new Error('setup() 没跑起来');
    app.__sockets = sockets;
    app.__alerts = ctx.__alerts;
    return app;
}

/** 简易断言器 —— 打印成 ✓/✗, 统计通过/失败数。 */
function makeChecker() {
    const state = { passed: 0, fails: [] };
    state.check = (cond, msg, extra) => {
        if (cond) {
            state.passed++;
            console.log('    ✓ ' + msg);
        } else {
            state.fails.push(msg);
            console.log('    ✗ ' + msg + (extra ? '  → ' + extra : ''));
        }
    };
    state.section = (title) => console.log('\n' + title);
    state.finish = () => {
        console.log(`\n${state.fails.length === 0 ? '全部通过' : '有失败项'} `
            + `(${state.passed} 通过 / ${state.fails.length} 失败)`);
        return state.fails.length === 0 ? 0 : 1;
    };
    return state;
}

module.exports = { loadApp, makeChecker, APP_JS };
