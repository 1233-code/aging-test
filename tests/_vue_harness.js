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

const APP_JS = path.join(__dirname, '..', 'static', 'app.js');

function makeVue(grab) {
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
        onMounted: () => {},
        nextTick: () => Promise.resolve(),
    };
}

/**
 * @param {object} [opts]
 * @param {function} [opts.fetch]  fetch 桩 —— 默认直接 reject(测试里不该
 *                                   真发网络请求)。要测网络交互就自己传。
 */
function loadApp(opts) {
    opts = opts || {};
    const src = fs.readFileSync(APP_JS, 'utf8');
    let app = null;
    const ctx = {
        Vue: makeVue((a) => { app = a; }),
        window: {},
        console,
        setTimeout, clearTimeout, setInterval, clearInterval,
        fetch: opts.fetch || (() => Promise.reject(new Error('测试里不该发网络请求'))),
        location: { protocol: 'http:', host: '127.0.0.1:8000' },
        navigator: {},
        WebSocket: function () { this.close = () => {}; },
        JSON, Math, Date, Object, Array, Map, Set, String, Number, Boolean,
        Promise, Error, isNaN, parseInt, parseFloat,
    };
    ctx.globalThis = ctx;
    vm.createContext(ctx);
    vm.runInContext(src, ctx, { filename: 'app.js' });
    if (!app) throw new Error('setup() 没跑起来');
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
