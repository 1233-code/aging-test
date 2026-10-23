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
        const draggedIdx = ref(null);
        const isRunning = ref(false);
        const resultList = ref([]);
        const logs = ref([]);
        const logEl = ref(null);
        const history = ref([]);
        const historyDetail = ref(null);
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

        // 向导专用
        const currentTestIndex = ref(0);
        const userStopped = ref(false);   // 用户点过停止, 下次到 test_finished 加快进 summary

        // 保留兼容 (summary 用)
        const lastStatus = ref('');

        // TF 卡 prompt
        const tfPrompt = ref({ visible: false, testName: '', reason: '' });
        // §6.6 网口未插线 —— 与 TF 卡同构的交互 prompt
        const ethPrompt = ref({ visible: false, testName: '', reason: '' });

        // ========== Computed ==========
        const hasAnyEnabled = computed(() =>
            Object.values(tests).some(t => t.enabled)
        );

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

        // ========== 加载 + 拖动 ==========
        const loadConfig = async () => {
            const r = await fetch('/api/config');
            const data = await r.json();
            config.value = data;
            sn.value = data.sn || 'unknown';
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

        const dragStart = (e, idx) => {
            draggedIdx.value = idx;
            if (e.dataTransfer) {
                e.dataTransfer.effectAllowed = 'move';
                e.dataTransfer.setData('text/plain', String(idx));
            }
        };
        const dragOver = (e) => {
            e.preventDefault();
            if (e.dataTransfer) e.dataTransfer.dropEffect = 'move';
        };
        const dropAt = (e, targetIdx) => {
            e.preventDefault();
            const from = draggedIdx.value;
            draggedIdx.value = null;
            if (from === null || from === targetIdx) return;
            const order = [...testOrder.value];
            const [moved] = order.splice(from, 1);
            order.splice(targetIdx, 0, moved);
            testOrder.value = order;
        };
        const resetOrder = () => {
            if (!config.value || !config.value.recommended_order) return;
            testOrder.value = [...config.value.recommended_order];
        };

        // ========== 测试运行 ==========
        const selectAll = (val) => {
            for (const name in tests) tests[name].enabled = val;
        };

        const startTest = async () => {
            const req = { tests: { ...tests }, order: testOrder.value };

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

            // 初始化 resultList - 按用户拖动顺序 (testOrder)
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

        // 把后端的运行快照套到界面上。
        // WS 的 `snapshot` 和启动后的补查都走这里 —— 同一套恢复逻辑, 不写两份。
        const applySnapshot = (msg) => {
            if (!msg) return;
            if (msg.run_id) runId.value = msg.run_id;
            if (msg.status === 'running') isRunning.value = true;

            // 逐项结果。**不恢复 pct** —— 那是瞬时值, 下一条 progress 就覆盖了,
            // 硬塞一个陈旧数字反而会让人以为卡在某个百分比上。
            if (msg.results) {
                for (const [name, r] of Object.entries(msg.results)) {
                    const row = resultList.value.find(x => x.name === name);
                    if (!row) continue;
                    row.status = r.status;
                    row.metrics = r.metrics || {};
                    row.error = r.error;
                    if (r.duration_sec !== undefined) row.duration_sec = r.duration_sec;
                }
            }

            // 当前项 (§7.6 快照恢复)。**不能漏这一步** —— 单项测试时唯一那条
            // test_started 往往早于启动接口的响应到达(两条 TCP 连接无顺序保证),
            // 少了这里, 单项测试就没有当前项、没有进度条。
            if (msg.status === 'running' && msg.current_test) {
                const ri = resultList.value.findIndex(x => x.name === msg.current_test);
                if (ri >= 0) {
                    if (view.value === 'wizard') currentTestIndex.value = ri;
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

            // 整轮已经结束 → 直接出总结。
            // run_finished 同样是 WS 推的, 和这条 HTTP 查询不在一条连接上,
            // 短测试可能在上面那个窗口期里就把整轮跑完并把事件推完了 ——
            // 那时 resultList 还没建好, 总结页就再也不会出现。
            // resultList 为空时不跳: 那是「从没跑过 / 已重置」, 跳过去是空白页。
            const ended = msg.status && msg.status !== 'idle' && msg.status !== 'running';
            if (ended && resultList.value.length > 0) {
                tfPrompt.value.visible = false;
                ethPrompt.value.visible = false;
                userStopped.value = false;
                lastStatus.value = msg.status;
                view.value = 'summary';
                window._retesting = false;
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
            view.value = 'main';
            isRunning.value = false;
            resultList.value = [];
            logs.value = [];
            tfPrompt.value.visible = false;
            ethPrompt.value.visible = false;
            lastStatus.value = '';
        };
        const goHistory = () => {
            view.value = 'history';
            loadHistory();
        };

        // ========== 失败重测 (§7.7) ==========
        const retestFailed = async () => {
            const names = failedItemNames.value;
            if (!names.length || !runId.value) return;
            if (!confirm(
                `将重新测试 ${names.length} 项失败/错误:\n${names.join(', ')}\n\n继续?`
            )) {
                return;
            }
            isRunning.value = true;
            window._retesting = true;
            for (const name of names) {
                const r = resultList.value.find(x => x.name === name);
                if (r) {
                    r.status = 'pending';
                    r.pct = 0;
                    r.metrics = {};
                    r.error = null;
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
                    body: JSON.stringify({ run_id: runId.value, test_names: names }),
                });
                const data = await resp.json();
                if (!data.ok) {
                    alert('重测启动失败: ' + data.error);
                    isRunning.value = false;
                    window._retesting = false;
                }
            } catch (e) {
                alert('网络错误: ' + e.message);
                isRunning.value = false;
                window._retesting = false;
            }
        };

        const ENV_REASON_TEXT = {
            unplugged: '未插网线 —— PHY 层无链路 (ethtool Link detected: no)',
            no_ip: 'DHCP 与静态兜底均取不到 IP —— 工位网络环境不具备条件',
            no_egress: '链路与 DHCP 正常, 但网关/DNS/公网全不通 —— 上游网络问题',
            no_mount: '未检测到 TF 卡挂载点',
            no_ap: '未扫描到任何 WiFi 热点 —— 环境无 AP',
            dhcp_failed: 'WiFi 关联成功但 DHCP 未拿到地址 —— 地址池耗尽或 AP 侧限制',
            no_route: '关联成功但无默认路由 —— 网络配置异常',
        };
        const envReasonText = (code) => ENV_REASON_TEXT[code] || code;

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
            history.value = await r.json();
        };
        const loadHistoryDetail = async (id) => {
            const r = await fetch('/api/history/' + id);
            historyDetail.value = await r.json();
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
                    break;
                case 'test_started': {
                    // **画面只由这一条事件推动。**
                    // 后端说「现在开始跑 X」, 界面就显示 X —— 不再用
                    // `当前索引 + 1` 自己猜下一项是谁。原来那种前端自增的写法,
                    // 只要有一处没对齐 (提示框、停止、超时), 画面就会跑到后端
                    // 前面去, 而日志仍在写真正在跑的那一项。
                    // §7.8 的原地重测会再发一次**同名**事件, 索引不变, 屏幕
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
                    // §7.8 后端**真的停住了**。弹框只是把这件既成事实告诉工人。
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

                    // 这里**只出总结**。§7.8 的中途重测不走 mini-run 了 ——
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
                    view.value = 'summary';
                    window._retesting = false;
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
        });

        watch(view, (v) => {
            if (v === 'history') loadHistory();
        });

        return {
            // state
            view, config, testList, tests, testOrder,
            isRunning, resultList, logs, logEl,
            history, historyDetail, runId,
            exportingId, exportError, reportPath, reportError, reportRunId,
            tfPrompt, ethPrompt, currentTestIndex, lastStatus,
            depsInfo, shuttingDown,
            // computed
            snUnknown, snDisplay, snTitle,
            hasAnyEnabled, hasFailedItems, failedCount,
            passedCount, errorCount, skippedCount,
            currentTest, previousTest, overallProgress,
            orderedTests, draggedIdx,
            // methods
            statusText, formatTime, formatDuration, displayNameOf, envReasonText,
            selectAll, startTest, stopTest, shutdownService,
            goConfig, goHistory, retestFailed,
            tfInserted, tfSkip, ethPlugged, ethSkip,
            dragStart, dragOver, dropAt, resetOrder,
            loadConfig, loadDeps, loadHistory, loadHistoryDetail, exportPdf,
        };
    }
}).mount('#app');
