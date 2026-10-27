const { createApp, reactive, ref, computed, onMounted, watch, nextTick } = Vue;

createApp({
    setup() {
        // ========== 视图状态 ==========
        // 'main' = 配置页
        // 'wizard' = 测试中, 一测试一屏, 跑完自动跳下一项
        // 'summary' = 跑测完毕总结报告
        // 'history' = 历史记录浏览
        const view = ref('main');

        // ========== 数据 ==========
        const config = ref(null);
        const testList = ref([]);
        const tests = reactive({});
        const testOrder = ref([]);
        const isRunning = ref(false);
        const resultList = ref([]);
        const logs = ref([]);
        const logEl = ref(null);
        // 日志框能显示多少行 —— 按框的**实际高度**算, 不写死。
        // §10.2 原来写「屏幕上只显示最后 30 行」, 那是按 180px 高的小框定的;
        // 框拉高到贴屏幕底(2026-10-27)之后, 30 行只占一半, 下面一片空白。
        //
        // 往多了算(×1.4 再 +5): 行会换行时一行可能占两行, 宁可多渲染几条让
        // 框里滚 —— 框空着比框滚着难看。
        const LOG_LINE_PX = 18;          // 12px 字 × line-height 1.5
        const LOG_MIN_LINES = 30;
        const logVisibleLines = ref(LOG_MIN_LINES);

        // 框高(px) → 能显示多少行。单独拎出来是为了能直接测它 ——
        // 这条算式的值错了, 症状是「日志框下面空一大截」或者「滚不到头」,
        // 光看截图很难看出是差 5 行还是差 50 行。
        const computeLogLines = (h) => {
            if (!h || h <= 0) return LOG_MIN_LINES;   // 还没量到, 先用下限
            return Math.max(LOG_MIN_LINES, Math.ceil(h / LOG_LINE_PX * 1.4) + 5);
        };

        const measureLogLines = () => {
            const el = logEl.value;
            if (!el) return;
            const h = el.clientHeight;
            if (h <= 0) return;          // 还没布局好, 下次再量
            const lines = computeLogLines(h);
            if (lines !== logVisibleLines.value) logVisibleLines.value = lines;
        };

        // 日志框高度变了 → 能显示多少行得跟着变。
        // 用 ResizeObserver 盯着**框本身**, 而不是只听 window.resize ——
        // 框高不只受窗口影响: 向导页顶部的「当前测试」标题一换行, 框就矮了,
        // 而窗口一动没动。少了这个观察者, 换行那一刻框底会空出一截。
        //
        // 框是 flex 撑出来的(flex: 1 = basis 0% + grow, 高度全由 flex 分配),
        // 内容增减**不会**改变它的高度 —— 所以观察到的每次变化都是真高度变化,
        // 不会因为来一行日志就重算一遍(那会让每条日志都触发一次重排)。
        let logResizeObserver = null;
        watch(logEl, (el) => {
            if (logResizeObserver) { logResizeObserver.disconnect(); logResizeObserver = null; }
            if (!el) return;                       // 不是向导页, 日志框不存在
            measureLogLines();
            if (typeof ResizeObserver !== 'undefined') {
                logResizeObserver = new ResizeObserver(() => measureLogLines());
                logResizeObserver.observe(el);
            }
        });
        const history = ref([]);
        const historyDetail = ref(null);
        // 历史页当前**就地展开**的那一条(存 run id, 不是下标 —— 下标会随
        // 列表刷新而错位, 刷新完就张冠李戴了)。null = 全部收起。
        //
        // 同时只展开一条: 展开两处的话, 视线要在两个地方来回跳, 而且分不清
        // 哪块详情是刚点的 —— 那个更糟。
        const expandedId = ref(null);
        // §8.2 PDF 重导状态。导出是异步的, 没有这两个 ref 就只能给个
        // 「点了没反应」的黑洞按钮 —— 工人不知道是没点上还是正在跑。
        const exportingId = ref(null);
        const exportError = ref('');
        // 本轮收尾导出的结果。run_finished 之后才到, 所以单列一组状态,
        // 不跟 isRunning / lastStatus 混用 —— 混用会让人以为报告导出不过
        // 就是测试没过。
        const reportPath = ref('');
        const reportError = ref('');
        // ⚠️ **不能用 `runId` 拼下载链接** —— `runId` 在 mini-run(重测失败项)
        // 时是**故意指向父轮**的(见 run_started 处理), 而刚导出的 PDF 属于
        // mini-run 自己那一份。不另记一份 id 的话, 重测完点「打开 PDF 报告」
        // 会下到父轮(更早那轮)的报告 —— 一份内容对不上的纸质件进了归档。
        const reportRunId = ref(null);
        const runId = ref(null);
        const depsInfo = ref(null);  // /api/deps/check 结果
        // §2.2 被测设备 SN, 由 /api/config 一起下发; 取不到是 'unknown'
        const sn = ref('');
        // §8.2 本轮测试人员**姓名** —— 每轮手输, 因为同一工位会轮班, 报告上
        // 得写当班的人。初始值是 config.yaml 的 report.operator (后端预填),
        // 工人不改就直接用。空着也能跑 —— 报告里显示「未填写」, 但**不拦停**。
        const testerName = ref('');

        // 向导专用
        const currentTestIndex = ref(0);
        const userStopped = ref(false);   // 用户点过停止, 下次到 test_finished 加快进 summary

        // 保留兼容 (summary 用)
        const lastStatus = ref('');
        // §7.2 总结页要标出「这一轮测的是哪台机器、什么时候跑的」——
        // 快照恢复出来的总结页尤其需要: 它可能是**上一轮**的结果, 不标出来
        // 工人会以为刚刚又测了一遍。
        const runSn = ref('');
        const runFinishedAt = ref(0);
        // §7.6 整轮异常终止的原因(status=error)。没有它, 总结页上只有
        // 「0 通过 / 0 失败 / 0 跳过」, 工人根本看不出出了什么事。
        const runError = ref('');
        // §7.7 合并重测: 刚才这一轮是「记录 #N 的重测轮」, 结果已经并回
        // 那条记录了。记着这个 id, 总结页才能说清「你现在看到的是合并后
        // 的结果」, 而不是让人以为刚才只测了重测的那几项。
        const mergedParent = ref(null);

        // TF 卡 prompt
        const tfPrompt = ref({ visible: false, testName: '', reason: '' });
        // §6.6 网口未插线 —— 与 TF 卡同构的交互 prompt
        const ethPrompt = ref({ visible: false, testName: '', reason: '' });

        // ========== Computed ==========
        const hasAnyEnabled = computed(() =>
            Object.values(tests).some(t => t.enabled)
        );

        // 总结页顶部的结论词。
        //
        // ⚠️ 以前只有 passed / failed 两个分支, 剩下的**一律**显示
        //    「已完成 (部分跳过)」—— 停止(stopped)和异常(error)也这么说。
        //    「异常终止」被说成「部分跳过」, 工人会以为只是跳过了几项,
        //    拿着一个根本没跑完的结果去出货。两种状态必须分开说。
        // §9.2 行 1585-1589: total=0(一项都没实际执行)→ 整轮判 **skipped**,
        // 不判 passed —— 否则工人把所有项都选「跳过」就能拿到一份
        // 「100% 通过」的空报告。这个 run 级状态以前后端根本不会产生,
        // 这里把它补上(样式见 style.css 的 .summary-status.skipped)。
        const SUMMARY_EMOJI = {
            passed: '✅', failed: '❌', skipped: '⏭', stopped: '🛑', error: '⚠️',
        };
        const SUMMARY_STATUS_TEXT = {
            passed: '整体通过',
            failed: '存在失败项',
            skipped: '本次未实际执行任何测试项',
            stopped: '已停止',
            error: '本轮出错',
        };
        const summaryEmoji = computed(() => SUMMARY_EMOJI[lastStatus.value] || '❌');
        const summaryStatusText = computed(
            () => SUMMARY_STATUS_TEXT[lastStatus.value] || '已完成');

        // ── SN 展示 (§7.2) ──
        // **整串原样显示, 不截断。** 原来截成前 8 位 + 省略号, 靠 title 悬停
        // 才看得到全串 —— 那是产线反馈「往左边挪一点, 看清全貌」后改的: SN 是
        // 工人要**照着念**去核对机器的编号, 藏在悬停提示里的那一半没有任何用处。
        // 挪到标题旁边单独占一段就放得下了(见 index.html 的 .header-left)。
        // 真遇上超长 SN(某些板子塞了完整序列号) 由 CSS 的 ellipsis 兜底, 不在
        // 这里按长度截 —— JS 截断是无声的, 截到第几位没人说得清。
        const snUnknown = computed(() => !sn.value || sn.value === 'unknown');
        const snDisplay = computed(() => snUnknown.value ? 'unknown' : sn.value);
        const snTitle = computed(() => {
            if (snUnknown.value) {
                return '未能从 /proc/cmdline 读到设备 SN —— 本轮报告将记为 unknown, '
                     + '出货前请人工核对设备';
            }
            return `被测设备 SN: ${sn.value}`;
        });

        const failedItemNames = computed(() =>
            resultList.value
                .filter(r => r.status === 'failed' || r.status === 'error')
                .map(r => r.name)
        );
        const hasFailedItems = computed(() => failedItemNames.value.length > 0);
        const failedCount = computed(() => failedItemNames.value.length);
        const passedCount = computed(() =>
            resultList.value.filter(r => r.status === 'passed').length
        );
        const errorCount = computed(() =>
            resultList.value.filter(r => r.status === 'error').length
        );
        const skippedCount = computed(() =>
            resultList.value.filter(r => r.status === 'skipped').length
        );

        // ── §9.2 的通过率口径 ──
        //
        // total = 实际执行并出结论的项数(passed+failed+error);
        // **skipped / stopped 都不计入** —— 没跑的项不该拉低通过率。
        //
        // 光有「8 通过 / 0 失败 / 0 跳过」是不够的: 屏幕上分不清
        // 「勾了 8 项全测过」和「勾了 10 项只测了 8 项、2 项没测」。
        // 所以把通过率、(已测 M / 共勾选 K) 和未测项名一起摆出来。
        const executedCount = computed(() =>
            passedCount.value + failedCount.value + errorCount.value
        );
        const plannedCount = computed(() => resultList.value.length);
        const passRateText = computed(() => {
            if (!executedCount.value) return '—';
            return `${passedCount.value}/${executedCount.value}`;
        });
        // 未测 = skipped(用户主动跳 / 环境原因) + stopped(中途停止没跑到)
        const notTestedItems = computed(() =>
            resultList.value
                .filter(r => r.status === 'skipped' || r.status === 'stopped')
                .map(r => ({ name: r.display_name, status: r.status }))
        );
        const notTestedText = computed(() =>
            notTestedItems.value.map(x => x.name).join('、')
        );

        // 当前向导显示的测试
        const currentTest = computed(() =>
            resultList.value[currentTestIndex.value] || null
        );
        const previousTest = computed(() => {
            if (currentTestIndex.value <= 0) return null;
            return resultList.value[currentTestIndex.value - 1] || null;
        });
        const overallProgress = computed(() => {
            if (!resultList.value.length) return 0;
            return Math.round((currentTestIndex.value / resultList.value.length) * 100);
        });

        // ---- 本项计时: 2026-10-23 移除 ----
        // 原来是 nowMs / startTicking / timing computed 三件套, 支撑界面上那块
        // 「本项预计 / 已过 / 剩余」方块。已按产线要求移除 —— 理由见
        // docs/需求设计.md §7.5: 方块里的秒数是按挂钟估的, 与后端报的 pct 是
        // 两套数, 并排显示时两个百分比常常对不上。
        // 页面上现存一根进度条, 数据源是 pct。
        //
        // 后端 TestCase.expected_seconds() 与 test_started.expected_sec 仍在,
        // 那是接口上的机器可读估值 (排障、接口调用方用), 与本文件无关。

        const orderedTests = computed(() => {
            const map = new Map(testList.value.map(t => [t.name, t]));
            return testOrder.value.map(name => map.get(name)).filter(Boolean);
        });

        // ========== 工具函数 ==========
        const statusText = (s) => ({
            pending: '⏳ 等待',
            running: '▶ 运行中',
            passed: '✓ 通过',
            failed: '✗ 失败',
            skipped: '⏭ 跳过',
            error: '⚠ 错误',
            stopped: '🛑 停止',
            idle: '空闲',
        }[s] || s);

        const formatTime = (ts) => {
            if (!ts) return '-';
            return new Date(ts * 1000).toLocaleString('zh-CN');
        };

        const formatDuration = (start, end) => {
            if (!start || !end) return '-';
            const s = Math.round(end - start);
            if (s < 60) return s + 's';
            const m = Math.floor(s / 60);
            const sec = s % 60;
            if (m < 60) return `${m}m${sec}s`;
            const h = Math.floor(m / 60);
            return `${h}h${m % 60}m`;
        };

        const displayNameOf = (name) => {
            const t = testList.value.find(x => x.name === name);
            return t ? t.display_name : name;
        };

        // ========== 加载 ==========
        const loadConfig = async () => {
            const r = await fetch('/api/config');
            const data = await r.json();
            config.value = data;
            sn.value = data.sn || 'unknown';
            // §8.2 预填本轮测试人员: 后端把 config.yaml 的 report.operator
            // 一并发下来当默认值。工人可以直接改 (轮班换人), 改了才发。
            //
            // ⚠️ loadConfig() 只在挂载时调一次, goConfig() **不重载** ——
            //    所以这里预填的值不会把工人已经改好的名字冲掉。
            testerName.value = (data.report && data.report.operator) || '';
            testList.value = data.tests;
            testOrder.value = data.recommended_order && data.recommended_order.length
                ? [...data.recommended_order]
                : data.tests.map(t => t.name);
            for (const t of data.tests) {
                const def = data.defaults[t.name] || {};
                tests[t.name] = { ...def };
            }
        };

        const loadDeps = async () => {
            try {
                const r = await fetch('/api/deps/check');
                depsInfo.value = await r.json();
            } catch (e) {
                console.error('deps check failed:', e);
            }
        };

        // 2026-10-27: 拖拽排序和「↺ 重置顺序」一起去掉了 —— 10 项的顺序
        // 固定成后端下发的推荐顺序(§7.6), 工人拖不动也不需要复位。
        // `testOrder` 留着: 它是**这一轮的执行顺序**的唯一来源, 开始测试
        // 时原样发给后端, 后端按它跑。

        // ========== 测试运行 ==========
        const selectAll = (val) => {
            for (const name in tests) tests[name].enabled = val;
        };

        const startTest = async () => {
            // §8.2 本轮测试人员姓名随请求带上; 空字符串 = 后端回落到
            // config.yaml 的 report.operator, 再没有才在报告里写「未填写」。
            const req = {
                tests: { ...tests },
                order: testOrder.value,
                tester_name: String(testerName.value || '').trim(),
            };

            const r = await fetch('/api/test/start', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(req),
            });
            const data = await r.json();
            if (!data.ok) {
                alert('启动失败: ' + data.error);
                return;
            }
            runId.value = data.run_id;
            isRunning.value = true;
            logs.value = [];
            window._retesting = false;

            // 初始化 resultList - 按执行顺序 (testOrder, 恒等于后端下发的
            // 推荐顺序 §7.6; 2026-10-27 起 UI 上不可拖动)
            const enabled = testOrder.value.filter(n => tests[n] && tests[n].enabled);
            resultList.value = enabled.map(name => {
                const t = testList.value.find(x => x.name === name);
                return {
                    name,
                    display_name: t ? t.display_name : name,
                    status: 'pending',
                    pct: 0,
                    metrics: {},
                    error: null,
                };
            });

            // 切到向导视图
            currentTestIndex.value = 0;
            view.value = 'wizard';

            // ---- 补一次状态同步 (关键, 别删) ----
            //
            // 为什么必须主动拉一次, 不能光靠 WS:
            //   `POST /api/test/start` 里 runner.start() 是 `create_task` 之后
            //   **立刻 return**, 测试任务下一轮事件循环就跑起来并广播
            //   `test_started`。而上面那段建 resultList 的代码, 要等 fetch 的
            //   响应**回到浏览器**才执行。
            //   两条消息走的是**两条不同的 TCP 连接** (HTTP 响应 vs WebSocket),
            //   谁先到没有任何保证 —— 于是 test_started 有可能先到, 那时
            //   resultList 还是空的, findIndex 返回 -1, 事件被静默丢掉。
            //
            // 症状: 单项测试时唯一那条 test_started 丢了 → 屏幕不推进、pct 不
            // 更新、计时条也不出现 (它们全都挂在 test_started 上)。全测时
            // 只有第一项受影响, 后面几项事件都赶得上, 所以「全测才有进度条」。
            //
            // 这里主动查一次当前状态, 把窗口期里漏掉的事件补回来。
            await syncStatus();
        };

        // 按快照**重建**「这一轮有哪几项」的列表。
        //
        // 以前 applySnapshot 里是「往已有的 resultList 上贴」—— 可**刷新页面
        // 或 WS 断线重连时 resultList 是空的**, 贴了个寂寞: 前面已经跑完的
        // 几项一个都没恢复, 整页全是「等待」。注释里说的「刷新恢复只补了
        // 一半」, 补的就是这一半 —— 恢复的**数据**有了, 缺的是**列表本身**。
        //
        // 现在改成以快照为准重建:
        //   · 顺序取 msg.test_queue —— 这一轮**开跑时**定下的队列, 含还没跑
        //     到的那些。只靠 results 的话, 还没跑的项会整个消失, 进度条的分母
        //     也不对。
        //   · 每一项的状态/指标/耗时取 msg.results[name]
        //   · 快照里还没结果的 → pending
        // 已经在列表里的项**就地更新**而不重建 —— 保住 pct / 间隔提示这些
        // 快照里没有的瞬时值。
        const rebuildResultList = (msg) => {
            const results = msg.results || {};
            const queue = (msg.test_queue && msg.test_queue.length)
                ? msg.test_queue
                : Object.keys(results);
            if (!queue.length) return;

            const byName = new Map(resultList.value.map(r => [r.name, r]));
            const next = [];
            for (const name of queue) {
                const snap = results[name];
                const cur = byName.get(name);
                if (cur) {
                    if (snap) {
                        cur.status = snap.status || cur.status;
                        cur.metrics = snap.metrics || cur.metrics;
                        cur.error = snap.error ?? cur.error;
                        if (snap.duration_sec !== undefined) {
                            cur.duration_sec = snap.duration_sec;
                        }
                    }
                    next.push(cur);
                } else {
                    next.push({
                        name,
                        display_name: displayNameOf(name),
                        status: (snap && snap.status) || 'pending',
                        pct: 0,
                        metrics: (snap && snap.metrics) || {},
                        error: (snap && snap.error) || null,
                        duration_sec: snap ? snap.duration_sec : undefined,
                    });
                }
            }
            resultList.value = next;
        };

        // 把后端的运行快照套到界面上。
        // WS 的 `snapshot` 和启动后的补查都走这里 —— 同一套恢复逻辑, 不写两份。
        const applySnapshot = (msg) => {
            if (!msg) return;
            // mini-run 时 runId 仍指向父轮 —— 跟 run_started 的约定一致。
            // 以前这里是无条件 runId = msg.run_id, mini-run 时会跟那条约定
            // 打架(一会儿指父轮一会儿指自己), 顺着改掉。
            if (msg.run_id) {
                runId.value = (msg.mode === 'mini' && msg.parent_run_id)
                    ? msg.parent_run_id
                    : msg.run_id;
            }
            // isRunning 跟着快照走, **两个方向都要跟**。
            //
            // ⚠️ 以前只写 `if (status === 'running') isRunning = true` —— 只进
            //    不出。isRunning 的另两个出口是 `run_finished` 事件和「返回配置」,
            //    两条路**都要求 WS 活着**。于是 WS 一断, run_finished 收不到,
            //    isRunning 就永远卡在 true: 「关闭服务」按钮灰着点不了, 页面也
            //    不跳结果页 —— 同一个根因的两个症状。
            //
            //    快照说「已经跑完了」就必须能把它掰回来: 重连后服务端推的
            //    snapshot 里 status 已经是 passed/failed/stopped, 照着走就行。
            //    (status 只有 idle/running/passed/failed/stopped/error 六种)
            if (msg.status) isRunning.value = (msg.status === 'running');
            // 这一轮的身份 —— 总结页顶部要印 (§7.2)
            if (msg.sn) runSn.value = msg.sn;
            if (msg.finished_at) runFinishedAt.value = msg.finished_at;
            // 整轮异常终止的原因 (status=error)。快照里没带就不动它 ——
            // 跟 pdf_path 一个道理, 别拿一个空值把已有的原因冲掉。
            if (msg.error) runError.value = msg.error;

            // 逐项结果: **以快照为准重建**(见 rebuildResultList 的注释)。
            // pct 不恢复 —— 那是瞬时值, 下一条 progress 就覆盖了, 硬塞一个
            // 陈旧数字反而会让人以为卡在某个百分比上。
            rebuildResultList(msg);

            // 当前项 (§7.6 快照恢复)。**不能漏这一步** —— 单项测试时唯一那条
            // test_started 往往早于启动接口的响应到达(两条 TCP 连接无顺序保证),
            // 少了这里, 单项测试就没有当前项、没有进度条。
            //
            // ⚠️ 这里**无条件**设索引(以前是 `if (view === 'wizard')` 才设):
            //    刷新页面时页面在配置页上, 条件不成立 → 索引留在 0 → 就算
            //    切到向导页, 显示的也是**第一项**而不是正在跑的那一项。
            if (msg.status === 'running' && msg.current_test) {
                const ri = resultList.value.findIndex(x => x.name === msg.current_test);
                if (ri >= 0) {
                    currentTestIndex.value = ri;
                    resultList.value[ri].status = 'running';
                }
            }

            if (msg.waiting_operator) {
                const w = msg.waiting_operator;
                if (w.name === 'tfcard') {
                    tfPrompt.value = { visible: true, testName: w.name, reason: w.text };
                } else if (w.name === 'ethernet') {
                    ethPrompt.value = { visible: true, testName: w.name, reason: w.text };
                }
            }

            // §8.2 报告状态。快照带 pdf_path —— 重连后才知道按钮该显示
            // 「打开 PDF 报告」还是「导出 PDF 报告」(§8.2 规定二选一)。
            //
            // ⚠️ **只在有路径时写**, 不能因为快照没带就把已有的清掉: 导出是
            //    run_finished **之后**才做的, 重连完全可能赶在导出完成之前,
            //    那时快照里 pdf_path 还是 null, 而 report_ready 事件随后才来。
            //    无脑清空会把刚出的报告按钮又变回「导出」。
            if (msg.pdf_path) {
                reportPath.value = msg.pdf_path;
                reportRunId.value = msg.run_id ?? runId.value;
            }

            // 整轮已经结束 → 直接出总结。
            // run_finished 同样是 WS 推的, 和这条 HTTP 查询不在一条连接上,
            // 短测试可能在上面那个窗口期里就把整轮跑完并把事件推完了 ——
            // 那时 resultList 还没建好, 总结页就再也不会出现。
            //
            // ⚠️ 以前这里还带一个 `resultList.length > 0` 的条件, 用来挡
            //    「从没跑过」的空总结页。现在 resultList 由快照重建, 条件改成
            //    「**这一轮真的存在**」(run_id 有值) —— 空的快照(从没跑过)
            //    照样不跳, 但跑完的轮次**一定**能跳出总结页: 哪怕 WS 断过、
            //    哪怕页面刷新过。
            const ended = msg.status && msg.status !== 'idle' && msg.status !== 'running';
            if (ended && msg.run_id) {
                // 工人正在翻历史记录时**别把他拽走** —— 那是他在主动看别的东西。
                if (view.value !== 'history') {
                    tfPrompt.value.visible = false;
                    ethPrompt.value.visible = false;
                    userStopped.value = false;
                    lastStatus.value = msg.status;
                    view.value = 'summary';
                    window._retesting = false;
                }
            } else if (msg.status === 'running' && view.value === 'main') {
                // 跑着, 而页面还停在配置页 → 把在跑的这一轮摆出来。
                // 工人刷新一下页面就以为「没在跑」是最糟的: 他会以为可以再点
                // 一次开始, 然后被后端一句「已有测试在运行」顶回来, 而界面上
                // 一个「在跑」的迹象都没有。§7.6「运行态必须能靠快照恢复」。
                view.value = 'wizard';
            }
        };

        const syncStatus = async () => {
            try {
                const r = await fetch('/api/test/status');
                applySnapshot(await r.json());
            } catch (e) {
                // 拉不到就靠 WS 推 —— 这只是补漏, 失败不该打断启动流程
                console.warn('同步测试状态失败, 交给 WS:', e);
            }
        };

        const stopTest = async () => {
            if (!confirm('确定停止测试?')) return;
            userStopped.value = true;  // 标记用户主动停
            await fetch('/api/test/stop', { method: 'POST' });
        };

        // ========== 关闭服务 ==========
        // 工人 / 非专业用户不会去敲 `bash start.sh stop` —— 界面上必须有
        // 一个能关服务的按钮, 否则服务一直占着 8000 端口, 下次点桌面图标
        // 就打不开。
        const shuttingDown = ref(false);
        const shutdownService = async () => {
            if (isRunning.value) {
                alert('测试正在运行, 请先点「停止」, 等测试结束后再关闭服务。');
                return;
            }
            if (!confirm(
                '确定关闭服务?\n\n' +
                '· 服务会立刻退出, 本页面随之失效, 之后可以关掉浏览器了\n' +
                '· 本次测试结果已保存, 重启后仍可在「历史记录」里查看\n' +
                '· 要再测, 双击桌面「工厂老化测试」图标即可重新启动'
            )) return;
            shuttingDown.value = true;
            try {
                const r = await fetch('/api/shutdown', { method: 'POST' });
                if (!r.ok) {
                    const data = await r.json().catch(() => ({}));
                    shuttingDown.value = false;
                    alert('关闭失败: ' + (data.detail || ('HTTP ' + r.status)));
                }
            } catch (e) {
                // fetch 抛异常说明连接断了 = 服务大概率已经退了, 就算成功,
                // 保持 shuttingDown 让用户看到告别页而不是一条红色报错。
            }
        };

        // 向导 → 总结 / 重测
        const goConfig = () => {
            // 跑测期间**不许**回配置页 —— §7.1 的状态机里本来就没有
            // 「运行中 → 配置」这条转移。
            //
            // 以前这一下把 resultList 整个清空, 后端却照跑不误: 等
            // run_finished 到达时, 总结页就是一张「✅ 整体通过 / 0 通过 /
            // 0 失败 / 0 跳过」的空页 —— 工人会以为机器一项都没测。
            // 按钮已经 disable 了, 这里再挡一道(按钮 disable 只挡点击,
            // 挡不住代码里别处调 goConfig)。
            if (isRunning.value) return;
            view.value = 'main';
            isRunning.value = false;
            resultList.value = [];
            logs.value = [];
            tfPrompt.value.visible = false;
            ethPrompt.value.visible = false;
            lastStatus.value = '';
            // 离开历史页时把展开的详情收掉 —— 回来时列表是新的, 留着旧的
            // 展开位会让人以为那条记录还在原地。
            expandedId.value = null;
            historyDetail.value = null;
        };
        const goHistory = async () => {
            view.value = 'history';
            expandedId.value = null;
            historyDetail.value = null;
            // 等列表真的拉回来再返回 —— 之前是「发射后不管」, 调用方没法知道
            // 什么时候能看。测试里要「进历史页 → 立刻点某一行」, 不等的话
            // 那时列表还是空的, 点的根本不是屏幕上那一行。
            await loadHistory();
        };

        // ========== 重测失败项 (§7.7) ==========
        //
        // **总结页和历史页调的是同一个函数** —— 两者唯一的差别是「重测哪一轮」:
        //   · 总结页: 刚跑完这一轮 → 目标 = 当前这一轮
        //   · 历史页: 点开的那条记录 → 目标 = **那条记录**(可能是刚跑完, 也可能
        //     是过了一会儿、期间还开了别的测试)
        // 两条路走的是同一个后端接口, 都会把结果**合并回被重测的那一轮**,
        // 并更新那一份报告 —— 这才是「重测失败项」该有的样子。
        //
        // seedItems: 历史页进来时向导页的结果列表是空的, 得用那条记录的
        // 逐项结果把它铺上, 否则工人看到的是个空白的向导页。
        const retestRun = async (targetRunId, names, sourceLabel, seedItems) => {
            if (!targetRunId || !names || !names.length) return;
            // runner 是**单例** —— 一次只能跑一轮。别的测试正跑着的时候不能
            // 插队重测: 那会把正在跑的那一轮的状态搅乱(当前项、结果、日志
            // 全是同一份)。
            if (isRunning.value) {
                alert('当前有测试在运行, 结束后才能重测。');
                return;
            }
            if (!confirm(
                `将重新测试 ${names.length} 项失败/错误:\n${names.join(', ')}\n\n` +
                `结果会合并回${sourceLabel}, 并更新那一份报告。\n继续?`
            )) {
                return;
            }
            isRunning.value = true;
            window._retesting = true;
            if (seedItems && seedItems.length) {
                // 历史页来的: 把那条记录的逐项结果铺成向导页的列表,
                // 要重测的几项先摆成「等待」。
                resultList.value = seedItems.map(it => ({
                    ...it,
                    status: names.includes(it.name) ? 'pending' : it.status,
                    pct: 0,
                }));
            } else {
                for (const name of names) {
                    const r = resultList.value.find(x => x.name === name);
                    if (r) {
                        r.status = 'pending';
                        r.pct = 0;
                        r.metrics = {};
                        r.error = null;
                    }
                }
            }
            // 切回 wizard 显示重测过程
            const firstRetestIdx = resultList.value.findIndex(x => names.includes(x.name));
            if (firstRetestIdx >= 0) {
                currentTestIndex.value = firstRetestIdx;
                view.value = 'wizard';
            }
            try {
                const resp = await fetch('/api/test/retest', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ run_id: targetRunId, test_names: names }),
                });
                const data = await resp.json();
                if (!data.ok) {
                    alert('重测启动失败: ' + data.error);
                    isRunning.value = false;
                    window._retesting = false;
                } else if (data.dropped_tests && data.dropped_tests.length) {
                    // 注册表里已经没了的项(后来被删掉的测试项)没法重测 ——
                    // 说出来, 别让工人以为「说好的 3 项怎么只测了 2 项」。
                    alert(`以下测试项已不在册, 本次跳过: ${data.dropped_tests.join(', ')}`);
                }
            } catch (e) {
                alert('网络错误: ' + e.message);
                isRunning.value = false;
                window._retesting = false;
            }
        };

        // 总结页的「重测失败项」—— 目标就是当前这一轮
        const retestFailed = async () => {
            await retestRun(runId.value, failedItemNames.value, '这一轮');
        };

        // 历史页的「重测失败项」—— 目标是**被点开的那条记录**。
        // 只给正式轮(不是重测轮)出这个入口: 重测轮只是过程, 归档对象
        // 始终是最初那一轮 —— 在重测轮上再重测, 只会套出一串套娃记录。
        const historyFailedNames = (detail) => (detail && detail.results || [])
            .filter(r => r.status === 'failed' || r.status === 'error')
            .map(r => r.test_name);
        const retestHistoryRun = async (detail) => {
            if (!detail) return;
            const names = historyFailedNames(detail);
            if (!names.length) return;
            // 历史详情的结果用 test_name, 向导页用 name —— 铺列表时得对上。
            const seed = (detail.results || []).map(r => ({
                name: r.test_name,
                display_name: displayNameOf(r.test_name),
                status: r.status,
                pct: 0,
                metrics: r.metrics || {},
                error: r.error || null,
                duration_sec: r.duration_sec,
            }));
            await retestRun(detail.id, names, `记录 #${detail.id}`, seed);
        };

        const ENV_REASON_TEXT = {
            unplugged: '未插网线 —— PHY 层无链路 (ethtool Link detected: no)',
            no_ip: 'DHCP 与静态兜底均取不到 IP —— 工位网络环境不具备条件',
            no_egress: '链路与 DHCP 正常, 但网关/DNS/公网全不通 —— 上游网络问题',
            no_card: '未检测到 TF 卡 —— 卡槽是空的',
            no_ap: '未扫描到任何 WiFi 热点 —— 环境无 AP',
            dhcp_failed: 'WiFi 关联成功但 DHCP 未拿到地址 —— 地址池耗尽或 AP 侧限制',
            no_route: '关联成功但无默认路由 —— 网络配置异常',
        };
        const envReasonText = (code) => ENV_REASON_TEXT[code] || code;

        // 指标值怎么显示在界面上。
        //
        // ⚠️ **列表型的指标不能把 JSON 糊在界面上** —— Vue 默认把数组渲染成
        // `JSON.stringify(v, null, 2)`, 十几行缩进 JSON 挤在一个小方块里,
        // 工人既看不清、也没法一眼比分数。`rounds_detail`(每轮一条明细)就是
        // 这种形状: 这里压成**一行摘要**(几条 + 分数), 完整明细在报告(PDF)
        // 里有一张每轮一行的子表(§8.2.2)。
        //
        // 只动**显示**, 不动数据 —— 少一个字段、或者把值算错, 那都是
        // §9.3 点名的最危险失效模式(静默的「通过」)。
        const fmtMetricValue = (v) => {
            if (typeof v === 'number') return v.toFixed(1);
            if (Array.isArray(v)) {
                if (!v.length) return '0 条';
                const rows = v.filter((x) => x && typeof x === 'object' && !Array.isArray(x));
                if (rows.length !== v.length) return v.length + ' 项';
                const scores = rows.map((x) => x.score)
                    .filter((x) => typeof x === 'number');
                const head = rows.length + ' 条';
                if (!scores.length) return head;
                return head + ' · 分数 ' + scores.map((s) => Math.round(s)).join(' / ');
            }
            if (v && typeof v === 'object') {
                const kv = Object.entries(v).map(([k, x]) => k + '=' + x);
                return kv.length ? kv.join(' ') : '{}';
            }
            return v;
        };

        // ========== 交互 prompt (TF 卡 / 网口) ==========
        // 两者同构: 工人处理完物理动作后二选一 —— 重测该项 / 跳过该项。
        //
        // ⚠️ 这里**不做任何界面跳转**, 只把决定告诉后端。
        //    画面由后端下一条 `test_started` 决定:
        //      · 选「已插好」→ 后端原地重跑同一项 → 又发一次同名 test_started
        //        → 索引不变, 屏幕停在原地, 正是想要的
        //      · 选「跳过」  → 后端立刻开下一项 → test_started(下一项)
        //        → 屏幕跟着切过去
        //    以前这里是 setTimeout(advanceTest, 500) 自己在前端推索引, 而后端
        //    同时也在往下跑 —— 两套进度各走各的, 于是「跳过 TF 卡」看到的却是
        //    USB 的界面 (HDMI 已经在前 5 秒里跑完了), 日志和画面对不上。
        const answerOperator = async (decision) => {
            tfPrompt.value.visible = false;
            ethPrompt.value.visible = false;
            try {
                const resp = await fetch('/api/test/operator', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ decision: decision }),
                });
                const data = await resp.json();
                if (!data.ok) {
                    // 后端不在等待 (超时自动往下走了, 或页面刷新丢了一次点击)。
                    // 这不是致命错误 —— 界面会在下一条 test_started 时自己对上,
                    // 但要让工人知道刚才那一下没生效, 别以为按了没用。
                    alert('后端提示: ' + data.error);
                }
            } catch (e) {
                alert('网络错误: ' + e.message);
            }
        };

        const tfInserted = () => answerOperator('retest');
        const tfSkip = () => answerOperator('skip');
        const ethPlugged = () => answerOperator('retest');
        const ethSkip = () => answerOperator('skip');

        // ========== 历史 ==========
        const loadHistory = async () => {
            const r = await fetch('/api/history');
            const rows = await r.json();
            // §7.2: 历史列表**一行 = 一条归档记录** —— 重测轮(mini-run)是
            // **过程**, 不是归档对象, 不单独占一行(它的信息在它那一轮的
            // 详情里)。后端也过滤了, 这里**再滤一道**: 后端没重启时这一道
            // 照样管用 —— 前端是热路径, 刷新页面就生效, 不用等产线安排重启。
            // (不滤的话: 工人重测两次, 列表里就出现四条带「重测」的行,
            //  实际只重测过两条 —— 工人原话:「我都有点看不懂」。)
            history.value = (Array.isArray(rows) ? rows : [])
                .filter(x => x && !x.mini_of);
        };
        const loadHistoryDetail = async (id) => {
            const r = await fetch('/api/history/' + id);
            const data = await r.json();
            // ⚠️ 竞态: 请求是异步的, 期间用户可能已经点了别的行。迟到的
            //    响应不能盖掉当前展开的那一条 —— 否则展开的是 A 行, 显示的
            //    却是 B 行的结果(而且**界面上完全看不出错**: 详情块跟着
            //    展开的行走, 内容却是另一轮的)。
            if (expandedId.value !== id) return;
            historyDetail.value = data;
        };
        // 点一行 → 就地展开它的详情; 再点同一条 → 收起来。
        const toggleHistoryDetail = async (id) => {
            if (expandedId.value === id) {
                expandedId.value = null;
                historyDetail.value = null;
                return;
            }
            expandedId.value = id;
            historyDetail.value = null;      // 清掉上一条的内容, 别闪现
            await loadHistoryDetail(id);
        };

        // §8.2 手动重导 PDF。**只在这一轮上补导**, 不重跑测试 —— 自动导出失败
        // (缺 chromium / 字体装晚了 / 当时磁盘满) 时, 工人装完依赖就能补上,
        // 不用把整轮老化重新来一遍。
        const exportPdf = async (id) => {
            if (exportingId.value !== null) return;      // 别让连点排出 N 个 chromium
            exportingId.value = id;
            exportError.value = '';
            try {
                const r = await fetch(`/api/history/${id}/pdf`, { method: 'POST' });
                const body = await r.json().catch(() => ({}));
                if (!r.ok) {
                    // 后端 500 的 detail 里带着"PDF 里没有中文字形"这类真正有用的
                    // 原因, 原样透出来 —— 换成一句笼统的"导出失败"等于把刚查了
                    // 半天的诊断信息又丢了。
                    exportError.value = body.detail || `导出失败 (HTTP ${r.status})`;
                    return;
                }
                // 汇总页那个按钮是靠 reportPath 决定显示「打开 PDF 报告」还是
                // 「导出 PDF 报告」的。不在这里回填, 手动补导成功了按钮还是老样子
                // —— 工人点了没反应, 只能靠猜。id 对不上(在历史页给别的轮次补导)
                // 就别动汇总页的状态。
                if (id === runId.value || id === reportRunId.value) {
                    reportPath.value = body.path || '';
                    reportRunId.value = id;
                }
                await loadHistoryDetail(id);
                await loadHistory();
            } catch (e) {
                exportError.value = `导出请求没发出去: ${e.message}`;
            } finally {
                exportingId.value = null;
            }
        };

        // ========== WebSocket ==========
        const connectWS = () => {
            const proto = location.protocol === 'https:' ? 'wss' : 'ws';
            const ws = new WebSocket(`${proto}://${location.host}/ws`);

            ws.onmessage = async (ev) => {
                const msg = JSON.parse(ev.data);
                await handleEvent(msg);
            };
            ws.onclose = () => setTimeout(connectWS, 2000);
            ws.onerror = () => ws.close();
            window._agingTestWS = ws;
            // 心跳: 页面每 20s 报个到, 服务据此判断「人还开着页面」。
            // 后台标签页最坏被 Chrome 节流到 1min 一次, 对 10 分钟的空闲
            // 窗口来说够用。服务端只在**没有测试在跑**时才会因为静默而退出。
            clearInterval(window._hbTimer);
            window._hbTimer = setInterval(window._hbSend, 20000);
        };

        // 单独抽出来, 因为「从 bfcache 恢复」时 onMounted 不会重跑, setInterval
        // 可能已经没了 —— 那种情况下靠 visibilitychange 立刻补一个心跳。
        window._hbSend = () => {
            const ws = window._agingTestWS;
            if (ws && ws.readyState === WebSocket.OPEN) {
                ws.send(JSON.stringify({ type: 'ping' }));
            }
        };

        const handleEvent = async (msg) => {
            switch (msg.type) {
                case 'snapshot':
                    // 刷新/重连后把「正在等操作员」这件事重新显示出来。
                    // 不恢复的话: 后端还在等 (最多 300s), 工人刷新一下页面,
                    // 界面上却什么提示都没有, 只能干等 —— 最后超时自动往下走,
                    // 工人还以为测试挂了。
                    //
                    // 逻辑与启动后的补查 (syncStatus) 完全共用 applySnapshot ——
                    // 两处各写一份的年代, 刷新恢复只补了一半: 逐项结果没恢复,
                    // 于是工人刷新一次, 前面跑完的几项全变成「等待」。
                    applySnapshot(msg);
                    break;
                case 'run_started':
                    // mini-run 时 runId 仍指向父 run
                    runId.value = msg.mode === 'mini' && msg.parent_run_id
                        ? msg.parent_run_id
                        : msg.run_id;
                    // ⚠️ **每开一轮必须清掉上一轮的报告状态** —— 漏清的话,
                    // 连着跑两轮时, 第二轮的汇总页会挂着第一轮的
                    // 「打开 PDF 报告」按钮, 而那按钮下的是上一轮的报告。
                    // 导出是收尾才做的, 新一轮刚开始必然还没有报告。
                    reportPath.value = '';
                    reportRunId.value = null;
                    reportError.value = '';
                    // 上一轮的「已合并到记录 #N」也一并清掉 —— 新的一轮跟它
                    // 没关系了, 挂着那条提示只会让人以为这一轮也是重测。
                    mergedParent.value = null;
                    break;
                case 'test_started': {
                    // **画面只由这一条事件推动。**
                    // 后端说「现在开始跑 X」, 界面就显示 X —— 不再用
                    // `当前索引 + 1` 自己猜下一项是谁。原来那种前端自增的写法,
                    // 只要有一处没对齐 (提示框、停止、超时), 画面就会跑到后端
                    // 前面去, 而日志仍在写真正在跑的那一项。
                    // §7.3 的原地重测会再发一次**同名**事件, 索引不变, 屏幕
                    // 自然停在原地。
                    const idx = resultList.value.findIndex(x => x.name === msg.name);
                    if (idx >= 0 && view.value === 'wizard') {
                        currentTestIndex.value = idx;
                    }
                    const r = resultList.value.find(x => x.name === msg.name);
                    if (r) {
                        r.status = 'running';
                        r.pct = 0;
                        r.interval_pending = false;
                        r.interval_remaining = 0;
                    }
                    break;
                }
                case 'test_finished': {
                    const r = resultList.value.find(x => x.name === msg.name);
                    if (r) {
                        r.status = msg.result.status;
                        r.metrics = msg.result.metrics || {};
                        r.error = msg.result.error;
                        if (msg.result.duration_sec !== undefined) {
                            r.duration_sec = msg.result.duration_sec;
                        }
                    }
                    // **这里不跳转, 也不弹框。**
                    // 结果怎么呈现 (进下一项 / 停下来问人) 由后端随后发的事件
                    // 决定: 要停就发 `waiting_operator`, 要继续就发下一条的
                    // `test_started`。前端在这里自作主张跳一次, 就会和后端的
                    // 进度形成两套独立推进 —— 那正是「画面是 USB、日志在写
                    // HDMI」的根源。
                    break;
                }
                case 'waiting_operator': {
                    // §7.3 后端**真的停住了**。弹框只是把这件既成事实告诉工人。
                    const why = msg.text || '需要人工介入';
                    if (msg.name === 'tfcard') {
                        tfPrompt.value = { visible: true, testName: msg.name, reason: why };
                    } else if (msg.name === 'ethernet') {
                        ethPrompt.value = { visible: true, testName: msg.name, reason: why };
                    }
                    // 别的项将来也可能声明要等操作员 —— 一律记进日志, 别静默吞掉
                    if (msg.name !== 'tfcard' && msg.name !== 'ethernet') {
                        logs.value.push(`[${msg.name}] ⏸ 等待人工处理: ${why}`);
                    }
                    break;
                }
                case 'operator_wait_timeout': {
                    tfPrompt.value.visible = false;
                    ethPrompt.value.visible = false;
                    logs.value.push(
                        `[${msg.name}] ⚠ 等了 ${msg.timeout_sec}s 没人处理, 自动继续下一项`
                    );
                    break;
                }
                case 'test_retesting': {
                    const r = resultList.value.find(x => x.name === msg.name);
                    if (r) {
                        r.status = 'running';
                        r.pct = 0;
                    }
                    logs.value.push(
                        `[${msg.name}] ↻ 操作员已处理, 重测 (第 ${msg.attempt}/${msg.max_retest} 次)`
                    );
                    break;
                }
                case 'progress': {
                    const r = resultList.value.find(x => x.name === msg.name);
                    if (r) {
                        r.pct = msg.pct;
                        r.progress_msg = msg.msg;
                    }
                    break;
                }
                case 'log': {
                    logs.value.push(`[${msg.name}] ${msg.line}`);
                    if (logs.value.length > 500) logs.value = logs.value.slice(-500);
                    await nextTick();
                    if (logEl.value) logEl.value.scrollTop = logEl.value.scrollHeight;
                    break;
                }
                case 'metric': {
                    const r = resultList.value.find(x => x.name === msg.name);
                    if (r) {
                        r.metrics = r.metrics || {};
                        r.metrics[msg.key] = msg.value;
                    }
                    break;
                }
                case 'between_tests': {
                    const r = resultList.value.find(x => x.name === msg.next);
                    if (r) {
                        r.interval_pending = true;
                        r.interval_remaining = msg.duration_sec;
                    }
                    break;
                }
                case 'run_finished': {
                    isRunning.value = false;
                    userStopped.value = false;
                    tfPrompt.value.visible = false;
                    ethPrompt.value.visible = false;

                    // 这里**只出总结**。§7.3 的中途重测不走 mini-run 了 ——
                    // 插卡重测是在主循环里原地重跑, 不会产生 run_finished,
                    // 所以这个事件只可能意味着整轮真的全部跑完了。
                    // §7.7「失败项重测」是另一种 mini-run, 发生在整轮之后,
                    // 也该出总结。
                    //
                    // ⚠️ 这里原来还写着 `hasResult.value = true`, 而 hasResult
                    //    **全文件根本没声明过** —— 一执行就 ReferenceError,
                    //    下面两行 (lastStatus / view='summary') 永远轮不到。
                    //    症状: 跑完了停在向导页, 永远不跳结果页; 而 WS 本身
                    //    没断, 后面照常收消息, 所以别的地方看着都正常, 极难查。
                    //    index.html 也从来没读过它, 属于纯死代码, 直接删。
                    lastStatus.value = msg.status;
                    // 这一轮的身份 —— 总结页顶部要印 (§7.2)
                    if (msg.sn) runSn.value = msg.sn;
                    if (msg.finished_at) runFinishedAt.value = msg.finished_at;
                    // 整轮异常终止的原因 (§7.6)
                    if (msg.error) runError.value = msg.error;
                    view.value = 'summary';
                    window._retesting = false;
                    // 逐项结果**以快照为准重建** ——
                    //
                    // run_finished 事件本身**不带**逐项结果, 只带一个 summary
                    // 计数。以前那份列表是 test_started 事件一路攒出来的, 所以
                    // 工人中途点了「← 返回配置」(goConfig 会清空 resultList)
                    // 的话, 这里就凑不出列表了 → 总结页变成一张
                    // 「✅ 整体通过 / 0 通过 / 0 失败 / 0 跳过」的空页,
                    // 工人会以为机器一项都没测。
                    //
                    // 补一次拉取: 快照里有 test_queue + results(含中途停止时
                    // 补上的 stopped 行), 列表就有内容了。
                    syncStatus();
                    break;
                }
                // §7.7 合并重测完成 —— 被重测的那条记录现在是「通过」了,
                // 报告也换成了合并后的那一版。
                //
                // 这个事件在 run_finished **之前**到(后端先合并再广播收尾),
                // 所以这里把报告指向**合并后**的那一份 —— 重测轮自己那份
                // (只含被重测的那几项)留在库里当过程记录, 但工人要归档的
                // 是合并版, 报告按钮必须指对。
                case 'parent_merged': {
                    const pid = msg.parent_run_id;
                    mergedParent.value = pid;
                    reportRunId.value = pid;
                    reportPath.value = msg.pdf_path || '';
                    // 历史列表刷新 —— 合并过的那条记录会跳到最上面
                    // (它刚刚才动过, 是列表里最新的状态)。
                    await loadHistory();
                    // 正展开着它的话, 详情也要跟着刷新(逐项状态/计数/角标)。
                    if (expandedId.value === pid) {
                        await loadHistoryDetail(pid);
                    }
                    break;
                }
                // §8.2 收尾导出。run_finished 之后**还会**来这两个事件之一 ——
                // 导出是 finish_run 之后才做的, 慢一拍。所以 UI 不能在 run_finished
                // 就把报告状态定死, 得留着口子等它。
                case 'report_ready': {
                    reportPath.value = msg.path;
                    // 记**这份 PDF 自己的** run id, 不是 runId.value ——
                    // mini-run 时后者指向父轮(见上面 reportRunId 的注释)。
                    reportRunId.value = msg.run_id ?? runId.value;
                    reportError.value = '';
                    break;
                }
                case 'report_failed': {
                    reportPath.value = '';
                    reportRunId.value = null;
                    // ⚠️ **绝不改成"本轮测试失败"** —— §8.2 硬规则。测试结果早
                    // 就写完库了, 这里只是附加产物没导出来。汇总页照常显示通过/
                    // 失败数, 按钮排里那个按钮保持「导出 PDF 报告」, 底下补一条
                    // 说明原因 —— 让工人知道是没生成, 而不是设备不合格。
                    reportError.value = msg.reason || '未知原因';
                    break;
                }
            }
        };

        // ========== 启动 ==========
        onMounted(async () => {
            // 关窗告别: 让服务走快通道 (goodbye_grace_sec 内退), 不用干等满
            // 10 分钟。用 pagehide 而不是 beforeunload —— 前者在移动端/标签页
            // 被回收时也准, 且不挡 bfcache。
            // 注意**刷新页面同样会触发**它, 所以服务端只是把宽限期缩短, 判断
            // 「人还在不在」的依据仍然是心跳, 刷新误触发不会把服务弄死。
            window.addEventListener('pagehide', () => {
                if (navigator.sendBeacon) {
                    navigator.sendBeacon('/api/goodbye');
                }
            });
            await loadConfig();
            await loadDeps();
            connectWS();
            // 补心跳的两个场景, 都因为「定时器可能已经不在了」而必须手动补一发:
            //   · 标签页从 bfcache 恢复 —— onMounted 不会重跑, interval 也没了
            //   · 息屏 / 锁屏很久后回来 —— 定时器被浏览器节流到近乎停摆
            // 不补的话, 页面明明在眼前开着, 服务却会因为「静默」把自己关掉。
            document.addEventListener('visibilitychange', () => {
                if (document.visibilityState === 'visible') {
                    window._hbSend && window._hbSend();
                }
            });
            // 窗口尺寸变了 → 日志框高度变了 → 能显示的行数也得跟着变。
            // 产线的屏是固定的 1920×1080, 但工人偶尔会拖一下窗口。
            window.addEventListener('resize', measureLogLines);
        });

        watch(view, (v) => {
            if (v === 'history') loadHistory();
            // 切到跑测试这一屏 → 量一次日志框高度, 算出能显示多少行。
            // nextTick: 得等 DOM 更新完, logEl 才存在、clientHeight 才准。
            if (v === 'wizard') nextTick(measureLogLines);
        });

        return {
            // state
            view, config, testList, tests, testOrder,
            isRunning, resultList, logs, logEl, logVisibleLines, computeLogLines,
            history, historyDetail, runId, expandedId,
            exportingId, exportError, reportPath, reportError, reportRunId,
            tfPrompt, ethPrompt, currentTestIndex, lastStatus,
            depsInfo, shuttingDown,
            runSn, runFinishedAt, runError, mergedParent,
            // computed
            snUnknown, snDisplay, snTitle,
            hasAnyEnabled, hasFailedItems, failedCount,
            passedCount, errorCount, skippedCount,
            executedCount, plannedCount, passRateText, notTestedItems, notTestedText,
            currentTest, previousTest, overallProgress,
            orderedTests,
            testerName,
            summaryEmoji, summaryStatusText,
            // methods
            statusText, formatTime, formatDuration, displayNameOf, envReasonText,
            fmtMetricValue,
            selectAll, startTest, stopTest, shutdownService,
            goConfig, goHistory, retestFailed, retestHistoryRun, historyFailedNames,
            toggleHistoryDetail,
            tfInserted, tfSkip, ethPlugged, ethSkip,
            loadConfig, loadDeps, loadHistory, loadHistoryDetail, exportPdf,
            // 导出 applySnapshot 是为了**可测** —— §7.6 的「靠快照恢复」这条
            // 规则要能自动验证, 就得能在测试里直接调它 (tests/test_frontend_snapshot.js)。
            // 它不是给模板用的, 模板里读的都是上面那些 state。
            applySnapshot,
        };
    }
}).mount('#app');
