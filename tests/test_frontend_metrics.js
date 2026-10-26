/* 回归测试: 指标值在界面上怎么显示(列表型指标不许糊成 JSON)。
 *
 * 起因 (2026-10-26): 产线看报告时发现 GPU 那一项的 `rounds_detail`
 * 「没跟前面的连接上」—— 那个指标存的是整段 glmark2 输出, 报告把它 repr 成
 * 一个几千字符的单元格, 把指标表顶断了页。修法分两半:
 *   ① 数据侧: 指标里不再存原始输出, 只存量化字段;
 *   ② 报告侧: 「一串字典」渲染成每条一行的子表。
 *
 * 但**界面这一侧**也有一份: 指标方块用的是 `{{ v }}`, Vue 默认把数组渲染成
 * `JSON.stringify(v, null, 2)` —— 十几行缩进 JSON 挤在一个小方块里, 工人
 * 既看不清、也没法一眼比分数。数据侧改完之后那段 JSON 变小了, 但形状还是
 * JSON —— 界面上该显示的是**一行摘要**。
 *
 * ⚠️ 这个测试盯的是**显示**, 不是数据: `fmtMetricValue` 只负责怎么显示,
 * 绝不改值 —— 少一个字段、或者把分数算错, 那都是 §9.3 点名的最危险失效
 * 模式(静默的「通过」)。
 *
 * 跑法: node tests/test_frontend_metrics.js
 */
'use strict';

const fs = require('fs');
const path = require('path');
const { loadApp, makeChecker } = require('./_vue_harness');

const { check, section, finish } = makeChecker();

const app = loadApp();
const fmt = app.fmtMetricValue;

const INDEX_HTML = path.join(__dirname, '..', 'static', 'index.html');

function main() {
    section('① 标量指标 —— 显示跟以前一模一样(不许被这次改动带偏)');
    check(fmt(147) === '147.0', '整数还是一位小数', fmt(147));
    check(fmt(147.25) === (147.25).toFixed(1), '小数还是一位小数(跟以前那个 toFixed(1) 一个规矩)',
          fmt(147.25));
    check(fmt('147 / 145') === '147 / 145', '字符串原样', fmt('147 / 145'));
    check(fmt('未填写') === '未填写', '中文串原样', fmt('未填写'));
    check(fmt(false) === false, '布尔值原样(界面上还是 true/false)', fmt(false));
    check(fmt(null) === null, 'null 原样', fmt(null));
    check(fmt(undefined) === undefined, 'undefined 原样', fmt(undefined));

    section('② 列表型指标(rounds_detail)—— 压成一行摘要, 不许糊 JSON');
    const one = [{ round: 1, score: 147.0, scenes: 33, scenes_expected: 33,
                   elapsed_sec: 105.3, exit_code: 0, status: 'passed' }];
    const s1 = fmt(one);
    check(s1 === '1 条 · 分数 147', '一条 → 「1 条 · 分数 147」', s1);
    check(!/[\[\]{}]/.test(s1), '显示里没有 JSON 的括号', s1);
    check(s1.length < 40, '显示很短(不是一整段 dump)', s1.length);

    const three = [
        { round: 1, score: 147.0, status: 'passed' },
        { round: 2, score: 145.4, status: 'passed' },
        { round: 3, score: 143.9, status: 'failed', error: '退出码 1' },
    ];
    const s3 = fmt(three);
    check(s3 === '3 条 · 分数 147 / 145 / 144', '多轮 → 分数串起来', s3);
    check(!/[\[\]{}]/.test(s3), '多轮也没有 JSON 括号', s3);

    section('③ 边界形状 —— 不许变成 "undefined" 或空');
    check(fmt([]) === '0 条', '空列表 → 「0 条」', fmt([]));
    check(fmt([1, 2]) === '2 项', '不是对象的列表 → 「2 项」', fmt([1, 2]));
    check(fmt([{ a: 1 }]) === '1 条', '没有 score 字段 → 只说几条', fmt([{ a: 1 }]));
    check(fmt([{ score: 'x' }]) === '1 条', 'score 不是数字 → 只说几条', fmt([{ score: 'x' }]));
    check(fmt({ a: 1, b: 2 }) === 'a=1 b=2', '对象 → 键值一行', fmt({ a: 1, b: 2 }));
    check(fmt({}) === '{}', '空对象 → {}', fmt({}));

    section('④ 模板真的用上了这个格式化函数(别退回 {{ v }})');
    const html = fs.readFileSync(INDEX_HTML, 'utf8');
    const used = (html.match(/fmtMetricValue\(v\)/g) || []).length;
    check(used === 2, '模板里两处指标显示都走 fmtMetricValue', '找到 ' + used + ' 处');
    check(!/typeof v === 'number' \? v\.toFixed\(1\) : v/.test(html),
          '旧的 {{ v }} 内联写法不在模板里了(那会把数组渲染成 JSON)');
    check(/fmtMetricValue,/.test(fs.readFileSync(
              path.join(__dirname, '..', 'static', 'app.js'), 'utf8')),
          'app.js 把 fmtMetricValue 导出给模板用了');

    return finish();
}

process.exit(main());
