/* 回归测试: 历史库坏过这件事, **界面上一个字都不许提**。
 *
 * 起因 (2026-11-10, 产线实况): 盘上的历史库坏了, 应用自动把它**改名隔离**
 * (不删) 并另建新库 —— 这是好事(服务照常能用, 新测试照记), 但第一版在主
 * 界面挂了一条横幅:
 *
 *     ⚠ 历史库损坏, 已自动隔离
 *     ⚠ 这根盘上的历史库曾经损坏
 *
 * 用的是 `.dep-banner` —— **跟「依赖缺失」那类真故障告警一个样式**(橙底 +
 * 红标题)。工人看到顶在主界面上, 合理反应是「**这根盘不能用了, 别插**」,
 * 而实际盘是好的: 服务正常、界面正常、测试照做, 只是**之前那批历史记录**
 * 读不出来了(旧文件还在 reports/ 里, 没删)。
 *
 * ⇒ 用户裁定 (2026-11-10): **界面全不提, 只留日志和文档**。
 *   · 界面: 一条都不许有(不许有横幅、不许有提示、不许有文字)
 *   · 留痕: `logs/server.log` 里写清楚 + `docs/` 里写清楚
 *   · 机器可查: `/api/config` 的 `history_db` 字段还在(curl 能查),
 *     但**前端不许拿它渲染任何东西**
 *
 * 这个测试就是防「谁哪天又手痒加回去」的:
 *   ① index.html 里不许出现 historyDb / 历史库 / 损坏 这类字样;
 *   ② app.js 里不许出现 historyDb(不许有那个状态, 不许读那个字段);
 *   ③ 前端渲染出来的东西里不许出现隔离件的文件名。
 *
 * ⚠️ **别把这个测试改成「横幅必须在」** —— 那是 2026-11-10 之前的写法,
 *    已经被用户明确否掉了(横幅会让人误会盘坏了)。
 *
 * 跑法: node tests/test_frontend_history_db_silent.js
 */
'use strict';

const fs = require('fs');
const path = require('path');
const { loadApp, makeChecker } = require('./_vue_harness');

const { check, section, finish } = makeChecker();

/* 默认读仓库里的文件; 给环境变量换个路径就能对着别的副本跑 ——
 * 用来验证「这个测试真的会红」: 把横幅加回去, 断言必须失败。 */
const STATIC_DIR = process.env.AGING_STATIC_DIR || path.join(__dirname, '..', 'static');
const INDEX_HTML = process.env.AGING_INDEX_HTML || path.join(STATIC_DIR, 'index.html');
const APP_JS = process.env.AGING_APP_JS || path.join(STATIC_DIR, 'app.js');

const html = fs.readFileSync(INDEX_HTML, 'utf8');
const appJs = fs.readFileSync(APP_JS, 'utf8');

/* ── ① 模板里不许出现「历史库」这三个字 ───────────────────── */
section('【1】界面上不许出现「历史库」的字样');
const htmlNoComment = html.replace(/<!--[\s\S]*?-->/g, '');
check(!/historyDb/.test(htmlNoComment),
      'index.html 里没有 historyDb', (htmlNoComment.match(/historyDb/g) || []).length + ' 处');
check(!/历史库/.test(htmlNoComment),
      'index.html 里没有「历史库」', (htmlNoComment.match(/历史库/g) || []).length + ' 处');
check(!/隔离/.test(htmlNoComment),
      'index.html 里没有「隔离」', (htmlNoComment.match(/隔离/g) || []).length + ' 处');
check(!/corrupt-/.test(htmlNoComment),
      'index.html 里没有隔离件的文件名模式', (htmlNoComment.match(/corrupt-/g) || []).length + ' 处');

/* ── ② JS 里不许再拿那个字段渲染东西 ─────────────────────── */
section('【2】app.js 不许再读 history_db');
check(!/historyDb/.test(appJs),
      'app.js 里没有 historyDb 这个状态', (appJs.match(/historyDb/g) || []).length + ' 处');
/* `history_db` 这个字符串只许出现一次, 且只许出现在那句「掐掉」里 ——
 * 出现第二次就说明有人开始拿它渲染东西了。 */
const hdHits = appJs.match(/history_db/g) || [];
check(hdHits.length === 1 && /delete\s+data\.history_db;/.test(appJs),
      'app.js 里 history_db 只出现在那句「掐掉」里, 没有别的用法',
      hdHits.length + ' 处');

/* ── ③ 就算后端报了这个字段, 界面也不许出现它 ───────────── */
section('【3】后端报了这个字段, 界面也不许渲染');
(async () => {
    const payload = {
        sn: 'TEST-SN',
        tests: [],
        defaults: {},
        recommended_order: [],
        report: {},
        history_db: {
            db_path: '/media/User/AGING1/reports/history.db',
            quarantined: {
                file: 'history.db.corrupt-20261110-144515',
                at: '2026-11-10 14:45:15',
                reason: 'DatabaseError: database disk image is malformed',
            },
            previous: [{
                file: 'history.db.corrupt-20261110-144515',
                at: '2026-11-10 14:45:15',
            }],
        },
    };
    const app = loadApp({
        fetch: async (url) => {
            if (String(url).indexOf('/api/config') >= 0) {
                return { json: async () => payload };
            }
            return { json: async () => ({}) };
        },
    });
    await app.loadConfig();
    check(app.historyDb === undefined,
          '前端状态里根本没有 historyDb 这个东西(不是「有但没显示」)');
    check(app.config.value.history_db === undefined,
          '拉下来的配置里那一项也被**掐掉**了(前端连状态里都不留它)',
          JSON.stringify(app.config.value).slice(0, 160));
    check(JSON.stringify(app.config).indexOf('corrupt-') < 0,
          '前端状态里没有隔离件的文件名', JSON.stringify(app.config).slice(0, 160));
    check(app.historyDb === undefined && !/historyDb/.test(appJs),
          '静态检查 + 运行检查两边都确认: 界面不提这件事');

    process.exit(finish());
})().catch(e => {
    console.error(e);
    process.exit(1);
});
