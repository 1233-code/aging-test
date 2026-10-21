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
        const runId = ref(null);
        const depsInfo = ref(null);  // /api/deps/check 结果

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
        };

        const stopTest = async () => {
            if (!confirm('确定停止测试?')) return;
            userStopped.value = true;  // 标记用户主动停
            await fetch('/api/test/stop', { method: 'POST' });
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
        // 统一走 promptRetest(testName), 避免两份重复的 fetch 逻辑。
        const promptRetest = async (testName) => {
            tfPrompt.value.visible = false;
            ethPrompt.value.visible = false;
            if (!runId.value) return;
            isRunning.value = true;
            window._retesting = true;
            const r = resultList.value.find(x => x.name === testName);
            if (r) {
                r.status = 'pending';
                r.pct = 0;
                r.metrics = {};
                r.error = null;
            }
            try {
                const resp = await fetch('/api/test/retest', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        run_id: runId.value,
                        test_names: [testName],
                    }),
                });
                const data = await resp.json();
                if (!data.ok) {
                    alert('重测失败: ' + data.error);
                    isRunning.value = false;
                    window._retesting = false;
                }
            } catch (e) {
                alert('网络错误: ' + e.message);
                isRunning.value = false;
                window._retesting = false;
            }
        };

        const promptSkip = () => {
            tfPrompt.value.visible = false;
            ethPrompt.value.visible = false;
            setTimeout(() => advanceTest(), 500);
        };

        const tfInserted = () => promptRetest('tfcard');
        const tfSkip = () => promptSkip();
        const ethPlugged = () => promptRetest('ethernet');
        const ethSkip = () => promptSkip();

        // 自动跳到下一项 / 总结
        const advanceTest = () => {
            if (view.value !== 'wizard') return;
            const next = currentTestIndex.value + 1;
            if (next < resultList.value.length) {
                currentTestIndex.value = next;
            } else {
                // 全部跑完 → 总结
                view.value = 'summary';
                isRunning.value = false;
                userStopped.value = false;
            }
        };

        // ========== 历史 ==========
        const loadHistory = async () => {
            const r = await fetch('/api/history');
            history.value = await r.json();
        };
        const loadHistoryDetail = async (id) => {
            const r = await fetch('/api/history/' + id);
            historyDetail.value = await r.json();
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
        };

        const handleEvent = async (msg) => {
            switch (msg.type) {
                case 'snapshot':
                    if (msg.status === 'running') isRunning.value = true;
                    break;
                case 'run_started':
                    // mini-run 时 runId 仍指向父 run
                    runId.value = msg.mode === 'mini' && msg.parent_run_id
                        ? msg.parent_run_id
                        : msg.run_id;
                    break;
                case 'test_started': {
                    const r = resultList.value.find(x => x.name === msg.name);
                    if (r) {
                        r.status = 'running';
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
                    // TF 卡 prompt (skipped + no_mount) — 不阻塞流程
                    if (
                        msg.name === 'tfcard' &&
                        msg.result.status === 'skipped' &&
                        msg.result.metrics &&
                        msg.result.metrics.no_mount === true &&
                        !window._retesting
                    ) {
                        tfPrompt.value = {
                            visible: true,
                            testName: 'tfcard',
                            reason: msg.result.error || '未检测到 TF 卡挂载点',
                        };
                    }
                    // §6.6 网口未插网线 prompt (skipped + unplugged) — 不阻塞流程
                    if (
                        msg.name === 'ethernet' &&
                        msg.result.status === 'skipped' &&
                        msg.result.metrics &&
                        msg.result.metrics.skipped_reason === 'unplugged' &&
                        !window._retesting
                    ) {
                        ethPrompt.value = {
                            visible: true,
                            testName: 'ethernet',
                            reason: msg.result.error || '未插网线',
                        };
                    }
                    // 任何测试完成 → 自动跳下一项 (不阻塞在失败界面)
                    if (view.value === 'wizard') {
                        const delay = userStopped.value ? 200 : 1500;
                        setTimeout(() => {
                            if (view.value === 'wizard') advanceTest();
                        }, delay);
                    }
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
                    hasResult.value = true;
                    if (msg.mode !== 'mini') {
                        lastStatus.value = msg.status;
                    }
                    // 不管正常完成 / 用户中途停 / mini-run → 都直接进 summary
                    view.value = 'summary';
                    isRunning.value = false;
                    userStopped.value = false;
                    if (window._retesting) {
                        window._retesting = false;
                        tfPrompt.value.visible = false;
                        ethPrompt.value.visible = false;
                    }
                    break;
                }
            }
        };

        // ========== 启动 ==========
        onMounted(async () => {
            await loadConfig();
            await loadDeps();
            connectWS();
        });

        watch(view, (v) => {
            if (v === 'history') loadHistory();
        });

        return {
            // state
            view, config, testList, tests, testOrder,
            isRunning, resultList, logs, logEl,
            history, historyDetail, runId,
            tfPrompt, ethPrompt, currentTestIndex, lastStatus,
            depsInfo,
            // computed
            hasAnyEnabled, hasFailedItems, failedCount,
            passedCount, errorCount, skippedCount,
            currentTest, previousTest, overallProgress,
            orderedTests, draggedIdx,
            // methods
            statusText, formatTime, formatDuration, displayNameOf, envReasonText,
            selectAll, startTest, stopTest,
            goConfig, goHistory, retestFailed,
            tfInserted, tfSkip, ethPlugged, ethSkip,
            dragStart, dragOver, dropAt, resetOrder,
            loadConfig, loadDeps, loadHistory, loadHistoryDetail,
        };
    }
}).mount('#app');
