# 工厂老化测试工具

> 单机硬件体检 + 老化测试工具,带 Web UI,桌面一键启动。
> 目标平台: **全志 A527** (ARM Cortex-A55 + Mali-G57)

![status](https://img.shields.io/badge/status-v0.1-blue) ![python](https://img.shields.io/badge/python-3.8-green)

## ✨ 特性

- 🖥️ **桌面图标一键启动** —— 浏览器自动打开, 可单击也可双击
- ☑️ **极简 UI** —— 勾选 + 次数/时长 + 全选 + 开始
- 📶 **WiFi 仅扫描验证** —— 不连 AP, 4 步 iw 流水线; **射频正关联着 AP 时不硬拔它**(软件开关那一步跳过并记降级 —— 硬拔一条正在工作的射频, 实测会把整块板子带崩, §6.4)
- 🎬 **GPU 渲染压测** —— glmark2-es2 Wayland 全屏跑, 无需外部视频素材
- 📊 **实时进度** —— WebSocket 推送温度、进度、日志
- ⏱️ **本轮总耗时** —— 顶栏右上角读秒 (**整轮一个数**, 换项不归零, 刷新页面接着走); 报告里**总耗时 + 每一项的单项耗时**都印出来, 屏幕上的数跟纸上的是同一个; **这一轮被重测过**时, 报告头那三行(开始/结束/总耗时)**原值保留**, 重测的那三项**接在后面**另起一行(不累加) (§7.8)
- 📄 **自动出报告** —— pass/fail 逐项 + 历史可查, 5s 静默
- 🔂 **失败/跳过/没跑到重测** —— 只重跑 failed/error/**skipped**/**stopped** 项 (mini-run 模式; 跳过、没跑到都是「没测成」, 补上料/环境之后就该能再测); 总结页和历史记录两个入口是同一个功能, 通过后**合并回原来那一轮**并更新那一份报告; 重测那一轮的日志在报告里**另起一节**印在「日志附录」后面, 首测的原始日志一个字不动; **重测没测成时首测那条结论原样保留** —— 「结论只能被结论替换」, 坏板子不会被洗成「通过」放行 (§7.7 / §7.7.2); **点名的项一定真的跑** —— 父轮配置里没有它 / 写着没勾选也一样跑, 不会静默地什么都不做
- 💾 **SQLite 存历史** —— 每次测试都有记录
- 🪄 **报告自动存 U 盘** —— 测完**自动**把那份 PDF 拷到 U 盘的 `AGING-REPORTS/`(文件名跟板子上 `reports/pdf/` 里的**一模一样**), 同目录再写一份 `INDEX.csv`(带 BOM, Excel 双击能开); **没插盘 / 拷不进去只提示一句, 绝不阻断测试结论** —— 报告是附加产物, 附加产物出问题不许改写测试结果 (§8.2.3)
- 💾 **U 盘版应用(设备上零安装)** —— 整个工具(代码 / 历史库 / 报告 / 随包工具)装在一根 U 盘上跑, **插上盘就能用, 拔掉盘设备上什么都不剩**; **功能跟装在设备上一样**(11 项一个不少, 判据一个字不改)—— 差异全在 U 盘那一侧: 随包工具用 wrapper 把「那 10 个运行时库」和 glmark2 的场景数据指过去, **应用代码一行没改**; 报告落在**应用自己那根盘**上(插两根盘也不会拷错)。**前提**: ext4 U 盘 + 镜像在同款板子上打 (§4.1.1 / `docs/部署方式对比.md` §13)
- 🪫 **卡挂死不误判** —— 设备对读写没应答 = **这张卡不合格**(判 failed + 报错明说「无响应」+ 列出还没收掉的进程, 提醒要拔卡复位); 工人中途按的「暂停」记 **stopped**(**被中断的那一项**不算失败, 不拉低通过率 —— 但**已经测出来的失败照旧算失败**, 坏板子不会被「按了暂停」盖掉); 删测试文件走子进程, **卡死了也冻不住整个服务** (§6.9.1)

## 📋 测试项 (11 项)

> 「#」= **推荐执行顺序**(轻量 → 接口 → 射频 → 重载; WiFi 排在 GPU 之前, 蓝牙紧跟 WiFi, §5)

| # | 测试 | 工具 | 状态 |
|---|---|---|---|
| 1 | 内存 压力 | `memtester` | ✅ |
| 2 | eMMC 存储 | `fio` (稀疏文件) | ✅ |
| 3 | TF 卡 (自动探卡 + 自己挂载) | `fio` | ✅ |
| 4 | HDMI 输出 | `xrandr` (读当前实际输出) + 1 轮自动断开/恢复 | ✅ |
| 5 | USB 枚举+读写 | `lsusb` + `fio` (写30s + 读30s) | ✅ |
| 6 | 网口 链路+ping | `ethtool` + `ping` | ✅ |
| 7 | 风扇 GPIO | `/proc/rp_gpio/pi5` (echo 拉高/拉低轮转 2 轮) | ✅ |
| 8 | WiFi 验证 (扫描+切换) | `iw` / `ip` / `rfkill` | ✅ |
| 9 | **蓝牙 (应关闭)** —— 判定**反过来**: 蓝牙**不工作** = 合格 | **只读 sysfs** (不用 root, 不装任何工具) | ✅ |
| 10 | GPU 渲染 | `glmark2-es2` | ✅ |
| 11 | CPU 压力 | `stress-ng` | ✅ |

> **打包出货** — 在**同架构 (aarch64) + Ubuntu 20.04 + python3.8** 的联网打包机上跑 `bash scripts/build_deb.sh`,得到 `dist/aging-test_<版本>_arm64.deb`; 目标机上 `sudo dpkg -i aging-test_*.deb` 一步装完, **全程不联网** (工具 .deb 与 python wheel 都随包带走)。详见 `docs/需求设计.md` §11.7。

## 🚀 快速开始

### 1. 安装系统依赖 (在被测设备上)

```bash
sudo bash scripts/install_deps.sh
```

关键工具: `stress-ng` (CPU), `glmark2-es2` / `glmark2-es2-wayland` (GPU), `fio` (存储), `memtester` (内存), `ethtool`, `iputils-ping` (`ping`), `busybox` (`udhcpc`), `iw`, `rfkill` (WiFi), `iproute2`, `usbutils` (`lsusb`), `util-linux` (`lsblk`), `x11-xserver-utils` (`xrandr`, HDMI 读当前实际输出), `chromium-browser` (PDF 报告导出 + 自动打开界面), `fonts-wqy-zenhei` (中文报告字体 —— **缺了 PDF 报告会静默变成全空白**, 见 docs §8.2.1), `libgpiod-tools` / `libgpiod2-tools` (**风扇的诊断工具, 不是备份路径** —— 本板风扇是厂商私有驱动, 标准 gpiod 接口看不到它; 缺不阻断) 等 (与 docs/需求设计.md §11.7 一致)。

> **不再需要 `libdrm-tools` / `modetest`** —— HDMI 模式切换已整块移除(改用 sysfs + xrandr 读当前状态), 详见 docs §6.7.1。

### 2. 安装桌面图标 (可选)

```bash
cp openclaw-aging-test.desktop ~/Desktop/
chmod +x ~/Desktop/openclaw-aging-test.desktop
```

### 3. 启动

```bash
bash start.sh
```

浏览器自动打开 `http://localhost:8000`。

### 4. 打包出货 (在打包机)

打一个 deb 装到目标板子上, **装完解包就能用, 不需要联网**:

```bash
bash scripts/build_deb.sh           # → dist/aging-test_0.1.0-1_arm64.deb
bash scripts/verify_deb.sh dist/aging-test_0.1.0-1_arm64.deb   # 校验(打包脚本自己会跑一遍)
```

目标板上装:

```bash
sudo dpkg -i aging-test_0.1.0-1_arm64.deb
```

装完: 代码在 `/usr/lib/aging-test/` (root 所有), 配置在 `/etc/aging-test/config.yaml`,
数据 (历史记录 / 报告 PDF / 日志) 在 `/var/lib/aging-test/`, 桌面出现「工厂老化测试」图标。

> **打包机要求** —— **aarch64 + Ubuntu 20.04 (focal) + python3.8**。随包的 python wheel
> 是 cp38 编译件, 换到 3.10 就装不上 (`build_deb.sh` 会直接拒绝, 而不是打个装不上的包出来)。
>
> **包里有什么** —— 代码 + 11 个测试项 + **27 个 .deb**(17 个工具 + 10 个运行时共享库)
> + **23 个 python wheel**(两个离线仓) + 配置 + 桌面图标。**测试数据不带** —— 历史库、
> 报告 PDF、日志一律不进包。
>
> **那 10 个「运行时共享库」是干啥的** —— 工具是 `dpkg-deb -x` 解过去的, **不走 dpkg
> 依赖解析**, 所以「工具 NEEDED 的那些 `.so` 目标机上有没有」得自己保证。`fio` 就是
> 这么翻车的(它硬链 `librbd.so.1`, 而那串 ceph/gluster 库是开发期 `apt install fio`
> 时才拖进来的, 出货镜像里没有)⇒ 干净板子上 fio 跑不起来, **eMMC / TF卡 / USB
> 三项全废**。`build_deb.sh` 出包前会过一道**闭包闸**
> (`deb/check_runtime_closure.py`, 逐个 ELF 读 NEEDED 表核对, 缺一个就不许出包),
> 判据是出货镜像基线 `deb/image-baseline.txt`。详见 `docs/需求设计.md` §11.7.1。
>
> **chromium 不在包里** —— 出货镜像自带。Ubuntu 20.04 仓库里那个 `chromium-browser`
> 是个 48KB 的 snap 过渡包, 装上 PDF 报告会静默变成空白 (§8.2.1)。
>
> **卸载也讲规矩** —— 工具是解过去的、不进 dpkg 数据库, purge 时要自己收拾;
> 但删之前要**两条独立证据**都成立(装机时记的**内容指纹**对得上 + 这个包在
> 目标机上**不是**正式安装状态)。判据不可用时**一个都不删** —— 宁可留残留,
> 也不能把人家 apt 正式装的文件删掉。详见 `docs/需求设计.md` §11.7.2。
>
> **解包不许踩坏 usrmerge 的软链** —— ⚠️ **这一条踩过整台板子**。Ubuntu 20.04 的
> `/lib` → `usr/lib`、`/bin` → `usr/bin` 都是**软链**, 动态链接器
> (`/lib/ld-linux-aarch64.so.1`)就挂在 `/lib` 底下; 而 `dpkg-deb -x` 底下是**裸
> tar**, 遇到包里的 `./lib/` 这种**目录成员**就会**把软链换成真目录** ⇒ 加载器
> 当场消失 ⇒ **板子上所有动态链接的程序全部跑不起来**(只因为 dpkg 和 bash 早就在
> 内存里才还能接着跑, 于是后面每个包静默跳过, 最后报「post-installation script
> error exit status 1」)。解包因此改用
> `dpkg-deb --fsys-tarfile | tar -x --keep-directory-symlink -C /`(保住软链, 内容
> 落到软链指向的地方), 出包前还有一道闸
> (`deb/check_extract_safety.py`)。**已经装坏的板子怎么修**见
> `docs/需求设计.md` §11.7.3。

## ⚙️ 配置

编辑 `config.yaml` 调整:

- 风扇 procfs 路径 (`fan.procfs_path`) + 轮转次数 (`cycles`)
- 网口 ping 目标 (`interfaces.ping_target`, 默认网关)
- 网卡/WiFi 接口名 (`interfaces.eth` / `interfaces.wifi`)
- 各测试项默认值 (`defaults.*`)
- HDMI 断开→恢复一轮 (`defaults.hdmi.replug_check`, 仓库里这份配置是**开**的): 用 `xrandr --output <口> --off` **自动关掉**这块屏、等几秒、再 `--auto` 恢复, 按基线那套逻辑判定 —— **工人全程不用动手**。⚠️ 关屏那几秒**这块屏是黑的**(属正常现象, 工具会先**提示 5 秒**: 进度条底下摆一条**显眼的大横幅**, 写着「马上要黑屏, 不用操作」, 旁边 5→4→3→2→1 倒计时); 关不掉是桌面不支持, **不判失败**; 恢复不回来才判失败 (详见 docs §6.7.3)
- Pass 阈值 (见 `docs/需求设计.md` §10.4)

## 🏗️ 项目结构

```
aging-test/
├── config.yaml                  # 配置 + 阈值
├── start.sh                     # 启动脚本 (start | stop | restart | status)
├── openclaw-aging-test.desktop  # 桌面图标 (Terminal=false)
├── requirements.txt
├── docs/
│   ├── 需求设计.md              # 需求 + 设计 (合并文档)
│   ├── 部署方式对比.md          # 「设备本地安装」 vs 「U 盘运行」 可行性分析 + 四方案对比
│   └── 项目介绍.pptx
├── server/
│   ├── main.py                  # FastAPI 入口
│   ├── config.py                # 配置加载
│   ├── db.py                    # SQLite 历史 (含 mini_of, error)
│   ├── report.py                # PDF 报告 (中文不静默变空白; 状态结论的唯一出处)
│   ├── runner.py                # 状态机 + WS 推送 + mini-run + 5s 静默期
│   ├── storage.py               # 只读的存储识别 (USB 总线 / 根盘反查 / 挂载点最长匹配)
│   ├── usb_export.py            # §8.2.3 报告自动存 U 盘 (拷 PDF + INDEX.csv)
│   └── tests/
│       ├── base.py              # TestCase 基类
│       ├── cpu.py / gpu.py / fan.py / emmc.py / ...
│       └── registry.py          # @register 装饰器
├── static/
│   ├── index.html               # Vue 3 单页 (5s 静默期提示)
│   ├── app.js
│   └── style.css
├── scripts/
│   ├── install_deps.sh          # apt 装系统工具 (与 §11.7 清单一致)
│   ├── check_deps.sh            # 关键工具缺位自检 (装机末尾 / 产线 debug 用; --packages-only 只查包类)
│   ├── build_deb.sh             # 打包 → dist/aging-test_*.deb (§11.7)
│   ├── build_usb_image.sh       # 「U 盘版应用」镜像构建 (§4.1.1) —— 拷代码 + 解 14 个 deb + 生成 wrapper
│   ├── verify_deb.sh            # 出货前的校验闸 (禁入 / 必入 / 元数据)
│   ├── deploy.sh                # 发布辅助
│   └── install_desktop_icon.sh  # 装桌面图标 (可选)
├── deb/                         # deb 的元数据与维护脚本
│   ├── control / conffiles      # 包元数据 (Depends 故意不含工具与 chromium)
│   ├── postinst / prerm / postrm  # 装机 / 卸载 / 清场
│   ├── aging-test.desktop       # 桌面图标 (Exec 指向 /usr/bin/aging-test)
│   ├── collect_wheels.py        # 从本机 pip 缓存离线收 python 依赖 (静态算闭包)
│   ├── check_runtime_closure.py # 出包前的运行时闭包闸 (逐个 ELF 读 NEEDED)
│   ├── gen_image_baseline.py    # 生成下面的镜像基线 (出货镜像自带哪些包)
│   └── image-baseline.txt       # 出货镜像基线 —— 闭包闸靠它判断「目标机上本来有啥」
├── build/                       # 打包中间物 (deps 离线仓 / staging) —— 不入仓
└── reports/                     # SQLite + 报告 (开发机上的; 不进包)
```

> **装成 deb 之后数据搬家** —— 代码在 `/usr/lib/aging-test/` 归 root, 跑测试的工人是
> 普通用户, **写不进去**。所以历史库 / 报告 PDF / 服务日志落到 `/var/lib/aging-test/`
> (`Config.data_root()` 的口径: 环境变量 > 代码根写得动时 > `/var/lib/aging-test`)。
> 开发机上代码根写得动 ⇒ **行为一个字不变**。

## 🔌 API 速览

| 端点 | 方法 | 说明 |
|---|---|---|
| `/api/health` | GET | 健康检查 |
| `/api/config` | GET | 配置 + 测试项 + **推荐顺序** |
| `/api/test/start` | POST | 启动测试 (按 §7.6 推荐顺序) |
| `/api/test/retest` | POST | mini-run: 只重跑失败/出错/**跳过**/**没跑到**的项, **合并回被重测的那一轮**并重导那一份报告 (§7.7)。总结页与历史记录两个入口共用它, 只有目标轮次不同 |
| `/api/test/stop` | POST | 停止 |
| `/api/test/status` | GET | 当前状态 |
| `/api/deps/check` | GET | 装机时缺工具自检 (拒绝出货) |
| `/api/history` | GET | 历史列表 (**一行 = 一条归档记录**; 默认不列重测轮, `?include_mini=1` 看原始行。按 `COALESCE(updated_at, started_at) DESC` 排 —— 被重测合并过的记录刚动过, 排在最上面) |
| `/api/history/{id}` | GET | 单次详情 (带 `retest_runs`: 这一轮底下有几轮重测、每轮重测了什么、日志) |
| `/ws` | WS | 实时事件流 (`progress / log / metric / alert / between_tests / run_started / test_started / test_finished / run_finished / parent_merged / usb_report_saved / usb_report_missing`) |

## 🛠️ 后续 TODO

非阻塞, 跑产线一段时间看哪些真出问题再加:

- [x] 启动时环境自检 (缺工具拒绝出货) — 已由 `deb/postinst` + `check_deps.sh` 覆盖 (§11.7)
- [ ] 多机器配置漂移管理 (master config 同步)
- [ ] SQLite 日志轮转 (按时间/大小归档)
- [ ] 进程残留清理 (启动时 pgrep 杀 zombie)
- [ ] 时区统一 (存 UTC, 前端按 Asia/Shanghai 显示)

## 📝 开发约定

### 添加新测试项

1. 在 `server/tests/` 新建 `<name>.py`
2. 继承 `TestCase`,用 `@register` 装饰
3. 实现 `async def run(self, ctx) -> TestResult`
4. 在 `server/tests/registry.py` 添加 import
5. 在 `config.yaml` 的 `defaults` 下加默认参数
6. UI 自动出现

### 实时事件类型

通过 WebSocket 推送:

- `run_started` (带 `started_at` —— 顶栏读秒的起点) / `run_finished` (带 `started_at` + `finished_at` **这一对** —— 两个数得来自同一轮)
- `test_started` / `test_finished`
- `progress` (pct, msg)
- `log` (line)
- `metric` (key, value)
- `usb_report_saved` / `usb_report_missing` (带 `path` / `filename` / `message` —— 报告存到 U 盘了没有, **只影响那一条提示, 不影响本轮判定**)

### 跑测试

```bash
.venv/bin/python tests/run_all.py        # 一次跑完所有回归测试 (Python + 前端)
```

> **变异测试**单独跑 —— 那是「证明断言咬得动」的, 把代码亲手改坏看测试会不会红:
>
> ```bash
> node tests/_mutate_timer.js              # 前端: 计时 + 版式 (23 处)
> node tests/_mutate_retest_history.js      # 前端: 重测入口的挑项口径与合并 (22 处)
> .venv/bin/python tests/_mutate_report_durations.py   # 后端: 报告里的时间 (22 处)
> .venv/bin/python tests/_mutate_runner_finished_timing.py  # 后端: run_finished 带的那对时间 (4 处)
> .venv/bin/python tests/_mutate_tfcard_hang.py   # 后端: 卡挂死时的判定与收尾 (11 处)
> .venv/bin/python tests/_mutate_interrupted_reconcile.py  # 后端: 服务夭折后残留轮次的收尾 (8 处)
> .venv/bin/python tests/_mutate_merge.py   # 后端: 重测合并与「结论只能被结论替换」 (26 处)
> ```

## 📄 License

MIT
