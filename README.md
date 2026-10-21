# 工厂老化测试工具

> 单机硬件体检 + 老化测试工具,带 Web UI,桌面一键启动。
> 目标平台: **全志 A527** (ARM Cortex-A55 + Mali-G57)

![status](https://img.shields.io/badge/status-v0.1-blue) ![python](https://img.shields.io/badge/python-3.11+-green)

## ✨ 特性

- 🖥️ **桌面图标一键启动** —— 浏览器自动打开, 可单击也可双击
- ☑️ **极简 UI** —— 勾选 + 次数/时长 + 全选 + 开始
- 📶 **WiFi 仅扫描验证** —— 不连 AP, 4 步 iw 流水线
- 🎬 **GPU 渲染压测** —— glmark2-es2 Wayland 全屏跑, 无需外部视频素材
- 📊 **实时进度** —— WebSocket 推送温度、进度、日志
- 📄 **自动出报告** —— pass/fail 逐项 + 历史可查, 拖动排序 + 5s 静默
- 🔂 **失败重测** —— 只重跑 failed 项 (mini-run 模式)
- 💾 **SQLite 存历史** —— 每次测试都有记录

## 📋 测试项 (10 项)

| # | 测试 | 工具 | 状态 |
|---|---|---|---|
| 1 | CPU 压力 | `stress-ng` | ✅ |
| 2 | GPU 渲染 | `glmark2-es2` | ✅ |
| 3 | WiFi 验证 (扫描+切换) | `iw` / `ip` / `rfkill` | ✅ |
| 4 | USB 枚举+读写 | `lsusb` + `fio` (写30s + 读30s) | ✅ |
| 5 | 网口 链路+ping | `ethtool` + `ping` | ✅ |
| 6 | HDMI 输出 | `modetest` | ✅ |
| 7 | 风扇 GPIO | `/proc/rp_gpio/pi5` (echo 拉高/拉低轮转 2 轮) | ✅ |
| 8 | TF 卡 (auto + 提示) | `fio` | ✅ |
| 9 | 内存 压力 | `memtester` | ✅ |
| 10 | eMMC 存储 | `fio` (稀疏文件) | ✅ |

> **打包出货** — 在联网打包机上跑 `bash scripts/build_bundle.sh` 下载所有依赖到本地仓,装机后 `scripts/install.sh` 脱网安装即可 (详见 `docs/需求设计.md` §12.7)。

## 🚀 快速开始

### 1. 安装系统依赖 (在被测设备上)

```bash
sudo bash scripts/install_deps.sh
```

这会安装 `glmark2-es2-wayland`, `stress-ng`, `fio`, `memtester`, `ethtool`, `iputils-ping`, `busybox` (`udhcpc`), `libdrm-tests` (`modetest`), `iw`, `rfkill`, `iproute2`, `usbutils` (`lsusb`), `util-linux` (`lsblk`), `libgpiod-tools`, `python3-libgpiod` (备路径) 等 (与 docs/需求设计.md §11.7 一致)。

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

### 4. 首次运行 - 打包构建 (在打包机,可选)

如果想重新烤制出货镜像,在**联网的同架构打包机**上跑:

```bash
sudo bash scripts/build_bundle.sh    # 一次下载所有 apt + pip wheel 到本地仓 (详见 docs/需求设计.md §12.7)
```

打包完成后,通过 `scripts/install.sh` 在目标设备一次性安装 (`dpkg -i *.deb` + `pip install --no-index`),**不需联网**。

## ⚙️ 配置

编辑 `config.yaml` 调整:

- 风扇 procfs 路径 (`fan.procfs_path`) + 轮转次数 (`cycles`)
- 网口 ping 目标 (`interfaces.ping_target`, 默认网关)
- 网卡/WiFi 接口名 (`interfaces.eth` / `interfaces.wifi`)
- 各测试项默认值 (`defaults.*`)
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
│   └── 项目介绍.pptx
├── server/
│   ├── main.py                  # FastAPI 入口
│   ├── config.py                # 配置加载
│   ├── db.py                    # SQLite 历史 (含 mini_of, error)
│   ├── runner.py                # 状态机 + WS 推送 + mini-run + 5s 静默期
│   └── tests/
│       ├── base.py              # TestCase 基类
│       ├── cpu.py / gpu.py / fan.py / emmc.py / ...
│       └── registry.py          # @register 装饰器
├── static/
│   ├── index.html               # Vue 3 单页 (带拖动排序 + 5s 静默期提示)
│   ├── app.js
│   └── style.css
├── scripts/
│   ├── install_deps.sh          # apt 装系统工具 (与 §12.7 清单一致)
│   ├── check_deps.sh            # 关键工具缺位自检 (装机末尾 / 产线 debug 用)
│   ├── build_bundle.sh          # 打包机脱网烧录准备 (§12.7)
│   ├── install.sh               # 目标机一次性装 (§12.7)
│   ├── deploy.sh                # 发布辅助
│   └── install_desktop_icon.sh  # 装桌面图标 (可选)
└── reports/                     # SQLite + 报告
```

## 🔌 API 速览

| 端点 | 方法 | 说明 |
|---|---|---|
| `/api/health` | GET | 健康检查 |
| `/api/config` | GET | 配置 + 测试项 + **推荐顺序** |
| `/api/test/start` | POST | 启动测试 (workers 拖动后的顺序) |
| `/api/test/retest` | POST | mini-run: 只重跑 failed 项 |
| `/api/test/stop` | POST | 停止 |
| `/api/test/status` | GET | 当前状态 |
| `/api/deps/check` | GET | 装机时缺工具自检 (拒绝出货) |
| `/api/history` | GET | 历史列表 (含 mini-run) |
| `/api/history/{id}` | GET | 单次详情 |
| `/ws` | WS | 实时事件流 (`progress / log / metric / between_tests / run_started / test_started / test_finished / run_finished`) |

## 🛠️ 后续 TODO

非阻塞, 跑产线一段时间看哪些真出问题再加:

- [x] 启动时环境自检 (缺工具拒绝出货) — 已由 `scripts/install.sh` + `check_deps.sh` 覆盖 (§12.7)
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

- `run_started` / `run_finished`
- `test_started` / `test_finished`
- `progress` (pct, msg)
- `log` (line)
- `metric` (key, value)

## 📄 License

MIT
