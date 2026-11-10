# U 盘版部署作业手册

> **这份手册是干什么的**:「设备上零安装 · 插盘即用 · 拔盘即无」这条路线（`docs/部署方式对比.md` §13 方案三）的**作业手册** —— 怎么打镜像、怎么烧盘、怎么在板子上验收、批量部署多根盘时怎么记账、出事了怎么查。
>
> **配套读**:`docs/部署方式对比.md`（为什么走这条路、四个方案对比、风险清单）、`docs/需求设计.md`（功能与判定口径）。
> **不管的事**:设备版（`.deb`）那条路（见 `docs/部署方式对比.md` §1）；「从 U 盘启动」（改引导链）——不做。
> **最后更新**:2026-11-10。

---

## 0. 流程总览

三段，顺序固定：

```
① 在**同款板子**上打镜像  →  ② 手工烧 ext4 U 盘  →  ③ 插盘双击启动 + 验收
   scripts/build_usb_image.sh      mkfs.ext4 + cp -a + 权限      启动老化测试 + 六条核对
        ↓                                   ↓                        ↓
   一个镜像目录 ──────────────►  可以烧 N 根盘 ──────────────►  每根盘一份台账
```

产物之间的关系：**镜像目录 → U 盘**（一对一或多对一）；**U 盘 → 板子**（一对一）。板子、数据、历史记录都不留在设备上。

---

## 1. 开工前必读的 6 条硬前提

| # | 前提 | 不满足会怎样 |
|---|---|---|
| 1 | **镜像只能在同款板子上打**（aarch64 + focal + Python 3.8） | wheels 是 **cp38 aarch64** 编译件，在 x86 上装不上；脚本 §① 自己就会拒绝 |
| 2 | **U 盘必须是 ext4** | FAT 没有符号链接、没有 unix 权限位 ⇒ venv 的 `bin/python` 建不起来、console script 全变 0644 不能执行 |
| 3 | **构建脚本不写盘**（`--device` 只是占位，会 warn） | 烧盘是手工四步，见 §3 |
| 4 | **sudoers 不用改** | 应用让 sudo 跑的一直是 `/usr/bin/timeout`（GPU 项是 `/usr/bin/env` → timeout），盘上的 fio/glmark2 只是它的**参数** ⇒ 产线现有白名单继续有效 |
| 5 | 构建脚本默认会**执行刚解出来的二进制**（fio / glmark2 自检） | 在板子上构建时这些二进制可信；`--no-self-check` 可关掉（只做静态检查） |
| 6 | **构建不联网**，wheels 必须已经备好 | 离线仓默认从 `dist/` 里最新的 deb 掏；掏不到就用 `--wheels <目录>` |

---

## 2. 阶段一: 打镜像

### 2.1 命令

```bash
# 在板子环境上打(带时间戳 —— 输出目录必须不存在或是空的)
bash scripts/build_usb_image.sh --out /tmp/usbimg-$(date +%m%d-%H%M)

# 指定离线 wheel 仓(自动掏失败时)
bash scripts/build_usb_image.sh --out /tmp/usbimg-$(date +%m%d-%H%M) --wheels <wheels 目录>

# 在别的机器上只打个目录树, 到板子上再补 venv(要联网就完了, 一般不用)
bash scripts/build_usb_image.sh --out <目录> --no-venv
```

参数：

| 参数 | 含义 |
|---|---|
| `--out <目录>` | 镜像输出目录（**必填**）。目录必须不存在或是空的 —— 脚本**不删东西** |
| `--wheels <目录>` | 离线 wheel 仓。不给就从 `dist/` 里最新的 `aging-test_*_arm64.deb` 里掏 |
| `--no-venv` | 不建 venv |
| `--no-self-check` | 不执行刚解出来的二进制（只做静态检查） |
| `--device <盘>` | **空壳**，只 warn「本脚本不写盘」 |

### 2.2 关口一览（脚本里每一道拦的是什么）

| 关口 | 拦什么 | 处理 |
|---|---|---|
| §① 环境 | 架构不是 aarch64 / Python 不是 3.8 | 换到同款板子上打 |
| §② 输出目录 | 目录非空 | 换一个新目录（带时间戳） |
| §④ wheels | 一个 wheel 都没有 | `--wheels <目录>`，或先打一个 deb |
| §⑤ venv | 离线建 venv 失败 | 检查 wheel 齐不齐 |
| §⑧ 核心库 | `libc` / `libstdc++` / `ld-linux` 混进镜像 | 解包来源不对 —— 盘上那份会盖住系统那份 |
| §⑩ 自检 | fio 起不来 / glmark2 不认 `--data-path` | sysroot 里的 `.so` 齐不齐；glmark2 版本对不对 |
| §⑩b 可搬迁 | 某个依赖库**只能靠库里写死的绝对路径**找到 | 换台没那个目录的板子就 rc=127 ⇒ 存储项 error ⇒ 好板子被返修 |
| 出厂断言 | 出厂默认**超过核数**；出厂默认**读不到** | `config.yaml` 的 `defaults.cpu.threads` 写成一个 1~8 的整数 |
| 旋钮闸门 | 页面旋钮 `min≠1`；旋钮 `max` 超过代码硬上限；前端夹子的常量跟旋钮对不上；旋钮 title 里的「默认 N 核」跟 `config.yaml` 对不上 | 「范围 1~8」这个事实散在**四处**（`static/index.html` 的 min/max、`static/app.js` 的 `THREADS_MIN/MAX`、`server/tests/cpu.py` 的 `MAX_STRESS_THREADS`、`config.yaml` 的 `defaults.cpu.threads`），四处要一起改 |
| console script 闸门 | `start.sh` 直接调 venv 里的 console script | 一律 `python3 -m`（shebang 写死构建路径 ⇒ 换设备必坏） |
| 脚本清单 | 整目录拷 `scripts/` ⇒ 打包/部署类脚本上盘 | 排除法（`copy_tree_except` + 9 个排除名），见 §7 红线第 2 条 |

### 2.3 打完镜像，目录里有什么

```
<镜像目录>/
  AGING_USB_MODE          ← U 盘模式标记（start.sh / Python / 两个诊断脚本都认它）
  启动老化测试            ← 原生二进制（双击就跑；没有 gcc 时这一项会缺）
  start.sh                ← 服务启动脚本（start|stop|restart|status）
  怎么启动.txt            ← 给工人看的说明（镜像自生成）
  版本.txt                ← 版本戳（产线一眼认出手上这根盘是哪一版）
  config.yaml  requirements.txt
  server/  static/  docs/    ← docs/ 整个上盘 ⇒ **本手册每根盘上都有一份**
  scripts/                ← 只有「运行时 + 诊断」那几类
  wheels/  venv/          ← 离线 wheel 仓 + 离线建好的 venv
  tools/
    bin/                  ← wrapper（fio / glmark2-es2 / glmark2-es2-wayland）
    libexec/              ← 真二进制
    share/glmark2/        ← glmark2 的场景数据
    root/                 ← 解包出来的 sysroot（那批 deb）
```

**「打包/部署类」脚本不在里面**（构建脚本用排除法保证，下面这九个）：

```
install_deps.sh
build_deb.sh
build_usb_image.sh
verify_deb.sh
deploy.sh
install_desktop_icon.sh
prepare_for_firmware.sh
find_pi5_label.py
probe_rp_gpio_write.py
```

`check_deps.sh` / `check_eth.sh` / `check_usb_selfcontained.py` 那几类「运行时 + 诊断」脚本**照带**（产线上没人能自检等于没有）。理由见 §7。

---

## 3. 阶段二: 烧盘

### 3.1 认盘

`/dev/sdX` 的字母会变（重插一次就可能从 `sda` 变 `sdb`）⇒ **按容量和序列号认，不要按字母猜**：

```bash
lsblk -o NAME,SIZE,LABEL,UUID,FSTYPE,MOUNTPOINT
```

### 3.2 四条命令

```bash
sudo wipefs -a /dev/sdX                     # ① 清掉旧的文件系统标记
sudo /sbin/mkfs.ext4 -L AGING /dev/sdX      # ② 格式化（/sbin 要写全路径）
sudo mkdir -p /mnt/aging && sudo mount /dev/sdX /mnt/aging   # ③ 挂
sudo cp -a <镜像目录>/. /mnt/aging/          # ④ 拷
```

### 3.3 权限这一步（最容易漏）

```bash
sudo chmod -R a+rwX /mnt/aging      # 子项
sudo chmod a+rwx /mnt/aging         # ← 挂载点本身
sudo chown -R root:root /mnt/aging  # 中性属主
```

⚠️ **`chmod -R` 改不到挂载点本身** —— 文件系统根目录的 inode 模式不在目录树里。而 **U 盘模式下数据根 = 挂载点**（`start.sh` 要在数据根下建 `logs/`，`history.db` 落在数据根的 `reports/` 里）⇒ 挂载点不可写 ⇒ **工人用户不是 uid 1000 时应用根本起不来**。

（`reports/`、`reports/pdf/`、`AGING-REPORTS/` 运行时由应用自己建；`logs/` 由 `start.sh` 建。预先建好只是为了省事，不是必须。）

### 3.4 收尾

```bash
sudo umount /mnt/aging
# 然后**断电再拔** —— 直接拔会让 ext4 挂载点的日志来不及落盘
```

---

## 4. 阶段三: 上板验收

### 4.1 插盘与挂载

插上 U 盘 → 打开「文件」→ 点一下这个盘 ⇒ 系统挂上（udisks2 会自动挂，标签 `AGING` ⇒ 通常落在 `/media/<用户>/AGING`）。

⚠️ **挂载点会漂**：盘重新插一次，挂载点可能变（老目录没清掉会挂在别处）⇒ 应用侧**记设备不记路径**，靠**文件系统 UUID** 兜底。工人不用管。

### 4.2 启动

双击盘里的 **「启动老化测试」**（原生二进制）。

⚠️ **别双击 `start.sh`** —— 文件管理器（Nautilus）对可执行的文本文件默认是「显示」而不是「运行」（`executable-text-activation` 默认 `'display'`），这板子也没给 `.sh` 注册默认程序 ⇒ 会用文本编辑器打开源码。

启动器只做一件事：找到自己所在目录 → `exec bash <目录>/start.sh`。失败会写 `logs/启动失败.txt` 并弹通知。

**就绪判定有三层**（python 探针 → `curl --noproxy` → 端口在听）⇒ 新设备上没 curl / 有 http_proxy / localhost 先解析成 ::1 时不会误报失败。

### 4.3 验收清单（每根盘都过一遍）

| # | 查什么 | 命令 | 期望 |
|---|---|---|---|
| 1 | 服务活着 | `curl --noproxy '*' http://127.0.0.1:8000/api/health` | `{"ok":true,...}` |
| 2 | **U 盘模式生效** | `curl --noproxy '*' http://127.0.0.1:8000/api/deps/check` | `"mode": "usb"` |
| 3 | **没有那条死命令** | 同上 | `"fix_command": ""`（U 盘版**不给** `sudo bash scripts/install_deps.sh`，因为盘上根本没那个脚本） |
| 4 | 出厂默认核数 | `grep -m1 "threads:" <盘>/config.yaml` | `threads: 4`（**旧盘是 8** ⇒ 板子会成批硬复位） |
| 5 | 旋钮下界 | `grep -m1 -oE 'tests\[t\.name\]\.threads[^>]*min="[0-9]+"' <盘>/static/index.html` | `min="1"`（0 = 「0 = 自动 = 满核」那条路还开着） |
| 6 | 盘的身份（**记这个**） | `lsblk -o UUID,LABEL,FSTYPE` | 该盘的 UUID |

`版本.txt` 里还记着构建时间、代码版本、出厂默认核数、代码硬上限、以及「怎么确认手上这根盘是哪一版」的自查命令。

### 4.4 停服务

```bash
cd <盘> && bash ./start.sh stop        # 或 status / restart
```

⚠️ **设备版还开着的时候别插盘跑** —— 两边都占 8000。

---

## 5. 阶段四: 批量部署（多根盘）

### 5.1 一个镜像烧多根盘

镜像目录跟盘**没有绑定**：同一个 `<镜像目录>` 可以 `cp -a` 到 N 根盘 ⇒ **内容完全一致** ⇒ 一次构建、多次烧录。
（反过来：**U 盘不是构建产物**，别指望把盘拷到另一台机器就是一根新盘 —— UUID 是每根盘自己的身份。）

### 5.2 盘的身份 = 文件系统 UUID

**不是**挂载点（会漂）、**不是** `/dev/sdX`（会变）⇒ 台账记 **UUID**：

```bash
lsblk -o UUID,LABEL,FSTYPE,MOUNTPOINT     # 或 blkid
```

### 5.3 台账（每根盘一行）

| 列 | 内容 | 从哪来 |
|---|---|---|
| 盘标签 | `AGING` | 烧录时 `-L AGING` |
| **UUID** | 文件系统 UUID | `lsblk -o UUID` |
| 烧录时间 | | 作业记录 |
| 镜像构建时间 | | `版本.txt` 里那行 |
| 出厂默认核数 | 应为 `4` | `grep -m1 "threads:" config.yaml` |
| 旋钮下界 | 应为 `1` | `static/index.html` 那个 `min=` |
| 验收人 / 结果 | | 作业记录 |
| 备注 | 坏在哪、返修单号 | |

### 5.4 换盘、补盘时先核对版本

旧盘（出厂默认满核 8 线程）流出去过 ⇒ **上手第一件事是核对版本**：

```bash
grep -m1 "threads:" <盘>/config.yaml
#   threads: 4  ⇒ 这一版
#   threads: 8  ⇒ 旧盘, 别用
```

### 5.5 每根盘的数据互不干扰

数据根 = 挂载点 ⇒ 每根盘有自己的 `logs/`、`reports/history.db`、`reports/pdf/`、`AGING-REPORTS/`。换一根盘 = 换一套历史记录。

⚠️ **USB 存储项会把这根在用盘自动剔掉**（fio 是裸块设备写，往正在用的盘上写会砸自己的脚）⇒ 只剩一块盘时该项会判 `passed` 并记 `write_skipped_in_use=true`，日志会说清楚是「被剔掉了」而不是「压根没有」。

---

## 6. 排障表

| 症状 | 多半是 | 处理 |
|---|---|---|
| 双击「启动老化测试」没反应 | 没有那个二进制（构建机没 gcc）或 Nautilus 设置 | 看盘上有没有 `启动老化测试`；没有就用终端 `bash <盘>/start.sh` |
| 终端里跑起来了但浏览器打不开 | 用了 `localhost`（有的机器先解析成 ::1，而服务只听 IPv4） | 改打 `http://127.0.0.1:8000` |
| 界面显示「服务已关闭」但服务在跑 | 前端脚本挂了（老版本从公网取 Vue） | 看 `logs/server.log` 里有没有 `/api/config` 和 `WebSocket /ws` |
| fio / glmark2 那两项显示 **error** | 工具起不来（缺共享库） | 盘上 `tools/bin/fio --version` 试一下；`版本.txt` 旁的自检结果；换一根完整的盘 |
| 缺工具时给的办法是「sudo bash scripts/install_deps.sh」 | 那是**设备版**的办法，U 盘版不该给（盘上没这个脚本） | 确认 `/api/deps/check` 的 `mode` 是 `usb` 且 `fix_command` 是空 |
| `/api/history` 500 | 数据根失效（挂载点漂了）或盘被拔了 | `lsblk` 看盘还在不在；应用记设备不记路径 ⇒ 重插即可 |
| 报告没落到盘上 | 没检测到 U 盘 | 日志里会写「未检测到 U 盘 —— 报告仍在本机 reports/pdf/」 |
| 板子在加压途中硬复位 | **满核 8 线程**是过应力那一档（2026-11-04 实测，死因还没查清） | 出厂默认 4 核；开跑前在页面上把「线程数」调低；**别填 0**（下限是 1） |
| 收尾残留轮次失败 `database disk image is malformed` | 盘上的历史库坏了 —— 多半是**拔盘时写到一半**，大字段的溢出页断了一截（不是「整个库烂了」：表、索引、甚至写入**都还正常**，只有读某几行旧日志才炸） | 新版**启动时自动体检**：坏库**改名**成 `history.db.corrupt-<时间戳>`（**不删**），另建新库 ⇒ **盘照常能用**，只是**之前那批历史记录**读不出来了。⚠️ **界面上不会提示**（挂出来会被误会成「这根盘坏了不能用」）⇒ 要查就看 `logs/server.log` 里的 `[db]` 那几行，或 `curl 127.0.0.1:8000/api/config` 的 `history_db` 字段。手工体检（只读）：`python3 scripts/check_history_db.py <盘>/reports/history.db` |

---

## 7. 红线（不要做的事）

1. **不要用 FAT/exFAT 装这个应用** —— 建不起 venv，console script 也不能执行。
2. **不要把 `install_deps.sh` 带上盘** —— U 盘版缺工具时给的是「这版出厂镜像没带齐 ⇒ 改用设备版(deb)安装」；盘上要是躺着那个脚本，工人照着 `sudo bash scripts/install_deps.sh` 跑只会白跑（产线取不到公网；装上了也是装在板子上，拔盘带走）。构建脚本用**排除法**保证它不上盘。
3. **不要让盘上出现 `.bak-*`** —— 备份放在**设备**上（`/tmp/…`）。⚠️ 板子重启 `/tmp` 会清空，别指望它还在。
4. **不要在设备版还开着的时候插盘跑**（占 8000）。
5. **不要用满核（8 线程）跑** —— 出厂默认 4 核；满核是 2026-11-04 实测会把板子压死的那一档。
6. **不要把「0」当成「自动」** —— 旋钮下界是 1，`0` 会被夹成 1 核，不是满核。
7. **覆盖盘上文件时不要用 `cp -p`** —— 它会把源的权限/属主带过去（仓库里是 664/775，盘上是 666/777）。普通 `cp` 覆盖已有文件会保留目标文件的权限与属主。
8. **不要改「范围 1~8」里的任何一处而不改另外三处** —— 页面 min/max、前端夹子常量、代码硬上限、出厂默认，四处要一起（打包闸门会拦，但那是最后一道）。
9. **不要在服务还开着的时候拔盘** —— 服务正往盘上写历史库，**拔在写到一半**就会把大字段的溢出页写坏（2026-11-10 产线盘实况：表和索引都读得动、写也写得进，**只有读某几行旧日志才炸** ⇒ 工人根本查不出哪儿坏了，只看到「历史记录凭空少了几十轮」）。⇒ **拔盘前先 `cd <盘> && bash ./start.sh stop`**（§4.4）；收工、换盘、搬机器都按这个来。

---

## 8. 命令速查

```bash
# ── 打镜像 ───────────────────────────────────────────
bash scripts/build_usb_image.sh --out /tmp/usbimg-$(date +%m%d-%H%M)

# ── 烧盘 ─────────────────────────────────────────────
sudo wipefs -a /dev/sdX
sudo /sbin/mkfs.ext4 -L AGING /dev/sdX
sudo mkdir -p /mnt/aging && sudo mount /dev/sdX /mnt/aging
sudo cp -a /tmp/usbimg-XXXX/. /mnt/aging/
sudo chmod -R a+rwX /mnt/aging && sudo chmod a+rwx /mnt/aging && sudo chown -R root:root /mnt/aging
sudo umount /mnt/aging

# ── 板子上验收 ───────────────────────────────────────
lsblk -o UUID,LABEL,FSTYPE,MOUNTPOINT
curl --noproxy '*' http://127.0.0.1:8000/api/health
curl --noproxy '*' http://127.0.0.1:8000/api/deps/check      # mode=usb, fix_command=""
grep -m1 "threads:" <盘>/config.yaml                        # 必须是 4
grep -m1 -oE 'tests\[t\.name\]\.threads[^>]*min="[0-9]+"' <盘>/static/index.html   # 必须是 min="1"

# ── 启停 ─────────────────────────────────────────────
cd <盘> && bash ./start.sh start | stop | restart | status
```

---

## 9. 相关文件速查

| 文件 | 干什么的 |
|---|---|
| `scripts/build_usb_image.sh` | 打镜像（§2 的那些关口都在里面） |
| `scripts/usb_launcher.c` | 「启动老化测试」这个原生二进制（§4.2） |
| `scripts/usb_wrapper.sh.tpl` | fio / glmark2 的 wrapper（exec 真二进制之前先设 `LD_LIBRARY_PATH`） |
| `scripts/check_usb_selfcontained.py` | 静态查「换台板子还能用吗」（解析 ELF，不执行盘里的二进制） |
| `scripts/check_deps.sh` / `check_eth.sh` | 诊断脚本（U 盘版说 U 盘版的办法） |
| `start.sh` | 服务启动脚本（U 盘模式自举：数据根 / 工具目录 / config.yaml 全指到盘上） |
| `server/tests/_tools.py` | `usb_mode()`（形态判据）与 `deps_fix_hint()`（补救办法） |
| `server/usb_export.py` | 报告自动存到 U 盘（`AGING-REPORTS/`） |
| `tests/test_usb_image_anchors.py` | 镜像构建脚本的静态锚点检查 + 「打包/部署类脚本不上盘」的行为检查 + **「本手册跟脚本对得上」**（名单逐个比对、验收值逐行查） |
| `tests/test_usb_image_cpu_gate.py` | 打包闸门的正反对照 |
| `tests/_mutate_usb_manual.py` | 变异用例：把手册改坏（格式写成 FAT / 少写一个不上盘的脚本 / 验收期望值写错）⇒ 上面那个对账检查会红 |
| `tests/_mutate_usb_scripts_exclusion.py` | 变异用例：把构建脚本的排除机制改坏 ⇒ 行为检查会红 |
| `tests/_sweep_anchors.py` | 所有变异脚本的锚点静态扫描（改完代码先跑这个） |
| `scripts/check_history_db.py` | 历史库**只读**体检（排障表里那条 `database disk image is malformed`）：逐项报「表 / 索引 / 序列 / 大字段读得动吗 / 写会炸吗」，`--json` 出机器可读结果 |
| `tests/test_history_db_check.py` / `test_history_db_quarantine.py` | 历史库体检工具 + 「坏库改名隔离（不删）、好库不许隔离、热日志能回滚的库不许隔离」的回归 |
| `tests/test_frontend_history_db_silent.js` | 护栏：**界面不许再提**历史库这件事（第一版挂过横幅，工人会误以为「这根盘坏了不能用」⇒ 用户裁定全撤） |
