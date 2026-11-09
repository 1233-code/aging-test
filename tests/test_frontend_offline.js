/* 回归测试: 界面**不依赖公网**, 而且起不来的时候**不说谎**。
 *
 * 病根 (2026-11-06 在盘 A10000000000010 上实测踩到):
 *   工人双击 U 盘里的「启动老化测试」→ 服务起来了、浏览器也开了 →
 *   **界面一闪就没了, 直接跳到「服务已关闭」那一页**。
 *   查下来是: 界面框架 Vue 是从**公网 CDN**(unpkg.com)取的, 产线网络上
 *   取不到 ⇒ app.js 第一行 `const {...} = Vue;` 就抛 ⇒ 界面根本没挂起来。
 *   而页面里那个「服务已关闭」是 `v-if="shuttingDown"` 的**全屏卡**
 *   (position:fixed, 白底, z-index:2000)—— 没有 Vue 时原始模板照样渲染,
 *   于是它盖住整个页面 ⇒ **服务好好的, 界面却写着「服务已关闭」**。
 *
 * 这里守三件事:
 *   ① 界面**不依赖公网** —— 没有任何外链 script/link, Vue 从本地取;
 *      本地那份 Vue 是**完整的**(版本头、MIT 许可声明、createApp 都在),
 *      免得拷了个残缺文件上去(那样照样白屏, 而且更难查)。
 *   ② Vue 没挂上之前, 原始模板**一个字都不许露** —— `#app` 上有 v-cloak,
 *      CSS 里有 `[v-cloak] { display: none }`。
 *   ③ 起不来的时候**如实报**: 有个纯 HTML 的兜底提示(不依赖 Vue ⇒ 它自己
 *      一定显示得出来), 写着「界面没能加载出来」, **不是**「服务已关闭」;
 *      而且它盖得住一切(z-index 高过那个告别卡)。
 *
 * 跑法: node tests/test_frontend_offline.js
 * 变异: AGING_STATIC_DIR=<副本> node tests/test_frontend_offline.js
 */
'use strict';

const fs = require('fs');
const path = require('path');

const STATIC_DIR = process.env.AGING_STATIC_DIR
    || path.join(__dirname, '..', 'static');
process.env.AGING_APP_JS = process.env.AGING_APP_JS
    || path.join(STATIC_DIR, 'app.js');

const { makeChecker } = require('./_vue_harness');

const { check, section, finish } = makeChecker();

const INDEX_HTML = path.join(STATIC_DIR, 'index.html');
const STYLE_CSS = path.join(STATIC_DIR, 'style.css');
const APP_JS = path.join(STATIC_DIR, 'app.js');
const VUE_JS = path.join(STATIC_DIR, 'vue.global.prod.js');

const html = fs.readFileSync(INDEX_HTML, 'utf8');
const css = fs.readFileSync(STYLE_CSS, 'utf8');
const appjs = fs.readFileSync(APP_JS, 'utf8');

/* 取某条 CSS 规则的内容 —— 选择器必须**行首**才是这条规则。
 * (`.app-failed .boot-fallback` 那一行不是行首的 `.boot-fallback` ⇒ 不会
 *  被误当成主规则取出来。) */
function cssRule(cssText, selector) {
    const esc = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const re = new RegExp('(?:^|\\n)\\s*' + esc + '\\s*\\{([^}]*)\\}', 'm');
    const m = re.exec(cssText);
    return m ? m[1] : null;
}

function zIndexOf(cssText, selector) {
    const body = cssRule(cssText, selector);
    if (!body) return null;
    const z = /z-index:\s*(\d+)/.exec(body);
    return z ? Number(z[1]) : null;
}

/* #app 那个 div 的**结束位置**(闭标签之后的下标) —— 靠 div 深度配对找,
 * 不能靠「从 <div id="app"> 一直取到 </body>」那种切法(会把配对标签也
 * 切进来, 判定就不准了)。 */
function appDivEndIndex(src) {
    const open = /<div[^>]*\bid=["']app["'][^>]*>/i.exec(src);
    if (!open) return -1;
    let depth = 1;
    const re = /<div\b|<\/div>/gi;
    re.lastIndex = open.index + open[0].length;
    let m;
    while ((m = re.exec(src)) !== null) {
        if (m[0].toLowerCase() === '</div>') {
            depth--;
            if (depth === 0) return m.index + m[0].length;
        } else {
            depth++;
        }
    }
    return -1;
}

// ── ① 界面不依赖公网 ─────────────────────────────────────────
section('【1】界面不依赖公网(产线网络不通也能用)');

const scriptSrcs = [...html.matchAll(/<script[^>]*\bsrc=["']([^"']+)["']/g)]
    .map(m => m[1]);
const linkHrefs = [...html.matchAll(/<link[^>]*\bhref=["']([^"']+)["']/g)]
    .map(m => m[1]);
const externals = [...scriptSrcs, ...linkHrefs]
    .filter(u => /^(https?:)?\/\//i.test(u));

check(externals.length === 0,
      '页面里没有任何外链资源(不碰公网)',
      externals.join(', '));
check(scriptSrcs.indexOf('/static/vue.global.prod.js') >= 0,
      'Vue 从本地 /static/vue.global.prod.js 取', scriptSrcs.join(', '));

// ── ② 本地那份 Vue 是完整的 ───────────────────────────────────
section('【2】本地内置的 Vue 是完整的');

const vueExists = fs.existsSync(VUE_JS);
check(vueExists, 'static/vue.global.prod.js 在', VUE_JS);
if (vueExists) {
    const vueSrc = fs.readFileSync(VUE_JS, 'utf8');
    check(/vue v3\./.test(vueSrc), '是 Vue 3(文件头有版本号)');
    check(vueSrc.indexOf('@license MIT') >= 0,
          '许可声明留着(MIT —— 随包分发要带着)');
    check(vueSrc.length > 100000,
          '是完整的全局构建, 不是残缺文件', `${vueSrc.length} 字节`);
    check(vueSrc.indexOf('createApp') >= 0,
          '带 createApp(全局构建, 能直接用)');
}

// ── ③ Vue 没挂上之前, 原始模板一个字都不许露 ───────────────────
section('【3】Vue 没挂上之前原始模板不露(免得「服务已关闭」说谎)');

const appOpen = /<div[^>]*\bid=["']app["'][^>]*>/i.exec(html);
check(!!appOpen, '找到 <div id="app">');
check(!!appOpen && appOpen[0].indexOf('v-cloak') >= 0,
      '容器 #app 上挂着 v-cloak', appOpen && appOpen[0]);
check(/\[v-cloak\]\s*\{[^}]*display:\s*none/.test(css),
      'CSS 里有 [v-cloak] { display: none }');

// ── ④ 起不来的时候如实报 ─────────────────────────────────────
section('【4】起不来的时候如实说「界面没能加载出来」');

const fbIdx = html.indexOf('id="boot-fallback"');
check(fbIdx > 0, '页面里有兜底提示 #boot-fallback');

const appEnd = appDivEndIndex(html);
check(appEnd > 0 && fbIdx > appEnd,
      '兜底提示在 #app **外面**(Vue 挂载时会把 #app 里的内容整个换掉)',
      `#app 结束于 ${appEnd}, 兜底在 ${fbIdx}`);

const fbEnd = html.indexOf('<script src="/static/app.js">');
const fbHtml = fbIdx > 0 && fbEnd > fbIdx ? html.slice(fbIdx, fbEnd) : '';
check(fbHtml.indexOf('界面没能加载出来') >= 0,
      '兜底提示写的是「界面没能加载出来」');
check(fbHtml.indexOf('服务已关闭') < 0,
      '兜底提示**没有**拿「服务已关闭」糊弄(那正是这次撞到的毛病)');
check(!/v-(if|for|show|model|bind|on|cloak)\b/.test(fbHtml)
      && fbHtml.indexOf('{{') < 0,
      '兜底提示是纯 HTML(不依赖 Vue ⇒ 它自己一定显示得出来)');

const fbZ = zIndexOf(css, '.boot-fallback');
const sdZ = zIndexOf(css, '.shutdown-screen');
check(fbZ !== null && sdZ !== null && fbZ > sdZ,
      `兜底提示盖得住告别页(z-index ${fbZ} > ${sdZ})`);

// ── ⑤ 看门狗: Vue/app.js 没起来会被发现 ────────────────────────
section('【5】看门狗: Vue 或 app.js 没起来会被发现');

check(html.indexOf("typeof Vue === 'undefined'") >= 0,
      '页面里查了 Vue 到底在不在');
check(html.indexOf('__AGING_BOOTED__') >= 0, '页面里有个「界面起来了」的标记');
check(appjs.indexOf('window.__AGING_BOOTED__ = true;') >= 0,
      'app.js 挂上之后会报「界面起来了」');

const mountIdx = appjs.indexOf(".mount('#app')");
const bootedIdx = appjs.indexOf('window.__AGING_BOOTED__ = true;');
check(mountIdx >= 0 && bootedIdx > mountIdx,
      '「界面起来了」是在挂载**之后**才报的(前面抛错就报不出来 ⇒ 看门狗如实报)',
      `mount@${mountIdx} booted@${bootedIdx}`);

// ── ⑥ 真的关闭服务时, 告别页还在(别把功能改没了) ───────────────
section('【6】真·关闭服务时那个告别页还在');

check(html.indexOf('v-if="shuttingDown"') >= 0,
      'v-if="shuttingDown" 的告别页还在(工人点了「关闭服务」还得看它)');

console.log('\n提示: 这套断言只保证「结构没写错、文件在」。'
    + '断网真机上还得人眼过一遍 —— 但「界面能不能起来」现在有据可查了:'
    + '看 /api/health 通不通 + 页面里 data-boot-fail 是什么值。');

process.exit(finish());
