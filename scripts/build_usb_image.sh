#!/bin/bash
# 打「U 盘版应用」镜像 —— 整个老化测试工具装在一根 U 盘上。
#
# 用法:
#   bash scripts/build_usb_image.sh --out <目录>
#   bash scripts/build_usb_image.sh --out <目录> --wheels <目录> --no-venv
#
# ⚠️ **烧盘得手工来** (本脚本只出镜像目录, 不碰盘):
#   sudo /sbin/mkfs.ext4 -L AGING /dev/sdX
#   sudo mount /dev/sdX /mnt/aging
#   sudo cp -a <镜像目录>/. /mnt/aging/
#   sudo chmod a+rwx /mnt/aging        ← 挂载点本身也要可写 (U 盘模式数据根 = 挂载点)
#
# 出来的镜像长这样 (挂到板子上就是 /mnt/aging):
#   /mnt/aging/
#     AGING_USB_MODE          ← 标记 (start.sh 看到它就进 U 盘模式)
#     启动老化测试              ← **工人双击的就是它** (原生二进制, 见 §⑦b)
#     start.sh                ← 服务启动脚本 (启动器内部调它)
#     怎么启动.txt             ← 给工人看的说明
#     config.yaml  requirements.txt
#     server/  static/  docs/  scripts/
#     wheels/                 ← 离线 python wheel 仓
#     venv/                   ← 离线建好的 venv (不联网)
#     tools/
#       bin/                  ← wrapper (fio / glmark2-es2 / glmark2-es2-wayland)
#       libexec/              ← 真二进制
#       share/glmark2/        ← glmark2 的场景数据
#       root/                 ← 解包出来的 sysroot (那 14 个 deb)
#
# ⚠️ **为什么 U 盘必须是 ext4** (docs/部署方式对比.md §12.2):
#   venv 的 bin/python 是**符号链接**(FAT 不支持) ⇒ 建不起来;
#   console script 靠**执行位**(FAT 没有 unix 权限位) ⇒ 全变成 0644 不能执行;
#   还有 10 个 C 扩展(编译件)。⇒ 产线要用专用 ext4 U 盘 + 烧录工序。
#
# ⚠️ **镜像必须在同款板子上打** (aarch64 + focal + python3.8):
#   wheels 是 **cp38 aarch64** 的编译件 —— 在 x86 开发机上装不上。
#   本脚本会检查, 不对就**直接拒绝**(别打出一个装不上的镜像)。
#   `--no-venv` 可以在别的机器上先把目录树打好, 到板子上再补 venv。
#
# 【本脚本不碰什么】
#   · 不写系统任何目录 (只写 --out 指的那个目录);
#   · 不动 dpkg 数据库 (工具是 dpkg-deb -x 解出来的, 不注册);
#   · 不联网 (wheels 必须已经备好);
#   · 不 rm -rf (输出目录必须不存在或为空 —— 免得误删上一次的结果)。
set -eu

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

OUT_DIR=""
DEPS_DIR=""
WHEELS_DIR=""
DEVICE=""
NO_VENV=0
SELF_CHECK=1
TS="$(date +%Y%m%d-%H%M%S)"

log()  { printf '\033[1;34m[USB-IMG]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[USB-IMG] ⚠\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[USB-IMG] ✗\033[0m %s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
    case "$1" in
        --out)       OUT_DIR="$2"; shift 2 ;;
        --deps-dir)  DEPS_DIR="$2"; shift 2 ;;
        --wheels)    WHEELS_DIR="$2"; shift 2 ;;
        --device)    DEVICE="$2"; shift 2
                      warn "--device 只是占位: 本脚本**不写盘**, 烧盘请手工做(见脚本开头)" ;;
        --no-venv)   NO_VENV=1; shift ;;
        --no-self-check) SELF_CHECK=0; shift ;;
        -h|--help)   sed -n '2,40p' "$0"; exit 0 ;;
        *) die "看不懂的参数: $1" ;;
    esac
done

[ -n "$OUT_DIR" ] || die "得给 --out <目录>"
[ -d "$DEPS_DIR" ] || DEPS_DIR="$PROJECT_ROOT/build/deps"
[ -d "$WHEELS_DIR" ] || WHEELS_DIR=""

# ───────────────────────── ① 环境自检 ─────────────────────────
#
# wheels 是 cp38 aarch64 的编译件 ⇒ 架构 / python 版本对不上, 打出来的镜像
# 在板子上装不上 venv。**在这里就拒掉**, 别等到产线上才发现。
ARCH="$(uname -m)"
PYV="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo '?')"
log "① 环境: 架构=$ARCH python=$PYV"

if [ "$NO_VENV" = "0" ]; then
    if [ "$ARCH" != "aarch64" ]; then
        die "当前架构是 $ARCH, 不是 aarch64
   ⇒ **镜像必须在同款板子上打** —— wheels 是 cp38 aarch64 的编译件。
   想在别的机器上只打个目录树, 加 --no-venv, 到板子上再补 venv。"
    fi
    case "$PYV" in
        3.8|3.8.*) : ;;
        *) die "当前 python 是 $PYV, 不是 3.8
   ⇒ wheels 是 cp38 编译件, 换版本就装不上。用板子上的 python3.8 跑本脚本。" ;;
    esac
fi

# ───────────────────────── ② 输出目录 ─────────────────────────
#
# ⚠️ **不用 rm -rf 清目录** —— 输出目录必须不存在或是空的。
#    免得一条手滑的参数把上一次打好的镜像删了(那个镜像可能是产线在用的)。
if [ -e "$OUT_DIR" ]; then
    if [ -n "$(ls -A "$OUT_DIR" 2>/dev/null)" ]; then
        die "输出目录不是空的: $OUT_DIR
   本脚本**不删东西** —— 请换个新目录 (例如加个时间戳)。"
    fi
else
    mkdir -p "$OUT_DIR"
fi
OUT_DIR="$(cd "$OUT_DIR" && pwd)"
log "② 输出目录: $OUT_DIR"

# ───────────────────────── ③ 拷应用代码 ─────────────────────────
log "③ 拷应用代码"
copy_tree() {   # $1=源目录 $2=目标目录
    local src="$1" dst="$2"
    [ -d "$src" ] || return 0
    ( cd "$src" && find . -name '__pycache__' -prune -o -name '*.pyc' -prune \
        -o -name '.git' -prune -o -type f -print ) \
    | while IFS= read -r f; do
        mkdir -p "$dst/$(dirname "$f")"
        cp -p "$src/$f" "$dst/$f"
    done
}

for d in server static docs scripts; do
    copy_tree "$PROJECT_ROOT/$d" "$OUT_DIR/$d"
done
for f in start.sh config.yaml requirements.txt; do
    [ -f "$PROJECT_ROOT/$f" ] && cp -p "$PROJECT_ROOT/$f" "$OUT_DIR/$f"
done
chmod +x "$OUT_DIR/start.sh" 2>/dev/null || true
log "   代码就位"

# ───────────────────────── ④ wheels (离线仓) ─────────────────────────
log "④ wheels 离线仓"
if [ -z "$WHEELS_DIR" ]; then
    # 兜底: 从已经打好的 deb 里把 wheels 掏出来 (离线, 不用联网)
    newest_deb="$(ls -t "$PROJECT_ROOT"/dist/aging-test_*_arm64.deb 2>/dev/null | head -1 || true)"
    if [ -n "$newest_deb" ]; then
        WHEELS_DIR="$OUT_DIR/wheels"
        mkdir -p "$WHEELS_DIR"
        log "   从 $newest_deb 里取 wheels"
        dpkg-deb --fsys-tarfile "$newest_deb" 2>/dev/null \
            | tar -x -C "$WHEELS_DIR" --strip-components=5 ./usr/lib/aging-test/wheels 2>/dev/null \
            || warn "   从 deb 里取 wheels 失败, 请用 --wheels <目录> 指定"
        # strip 后目录里可能套了一层 wheels/
        if [ -d "$WHEELS_DIR/wheels" ]; then
            ( cd "$WHEELS_DIR/wheels" && mv ./*.whl "$WHEELS_DIR/" 2>/dev/null || true )
            rmdir "$WHEELS_DIR/wheels" 2>/dev/null || true
        fi
    fi
else
    mkdir -p "$OUT_DIR/wheels"
    cp -p "$WHEELS_DIR"/*.whl "$OUT_DIR/wheels/" 2>/dev/null || true
fi
n_whl="$(ls "$OUT_DIR"/wheels/*.whl 2>/dev/null | wc -l)"
log "   wheel: $n_whl 个"
if [ "$n_whl" -eq 0 ] && [ "$NO_VENV" = "0" ]; then
    die "一个 wheel 都没有 —— venv 建不起来
   请用 --wheels <目录> 指定离线仓 (或先打一个 deb, 里面带着 wheels)。"
fi

# ───────────────────────── ⑤ 离线建 venv ─────────────────────────
log "⑤ 离线建 venv"
if [ "$NO_VENV" = "1" ]; then
    warn "   --no-venv: 这次不建 venv —— 到板子上跑 start.sh 会自己建 (要联网就完了)"
    warn "   ⇒ 在板子上补建:  python3 -m venv venv && venv/bin/pip install -q --no-index --find-links=wheels -r requirements.txt"
else
    python3 -m venv "$OUT_DIR/venv"
    "$OUT_DIR/venv/bin/pip" install -q --no-index --find-links="$OUT_DIR/wheels" \
        -r "$OUT_DIR/requirements.txt"
    log "   venv 就位: $("$OUT_DIR/venv/bin/python" -V 2>&1)"
fi

# ───────────────────────── ⑥ 解 14 个 deb 到 tools/root ─────────────────────────
#
# 只有 **fio / glmark2 / glmark2-data + 那 10 个运行时共享库** 要搬。
# 其余工具(stress-ng / memtester / ethtool / iw …) 出厂镜像自带, 不搬。
#
# ⚠️ **dpkg-deb -x 不走 dpkg 依赖解析** —— 「这个工具在目标机上能不能起来」
#    没人替我们保证。搬完之后 §⑥ 的自检会真跑一遍 fio --version 验。
log "⑥ 解包系统工具 → tools/root"
TOOLS="$OUT_DIR/tools"
SYSROOT="$TOOLS/root"
mkdir -p "$SYSROOT" "$TOOLS/bin" "$TOOLS/libexec" "$TOOLS/share"

USB_PKGS="fio glmark2-es2 glmark2-es2-wayland glmark2-data
          librbd1 librados2 libtirpc3 libboost-iostreams1.71.0
          libgfapi0 libgfrpc0 libgfxdr0 libglusterfs0 librdmacm1 libatm1"

n_deb=0
for p in $USB_PKGS; do
    deb="$(ls "$DEPS_DIR"/${p}_*.deb 2>/dev/null | head -1 || true)"
    if [ -z "$deb" ]; then
        warn "   缺 $p 的 .deb —— 跳过 (这个工具在 U 盘版里就没有)"
        continue
    fi
    dpkg-deb -x "$deb" "$SYSROOT"
    n_deb=$((n_deb + 1))
done
log "   解了 $n_deb 个包"

# 场景数据: /usr/share/glmark2 → tools/share/glmark2
if [ -d "$SYSROOT/usr/share/glmark2" ]; then
    cp -a "$SYSROOT/usr/share/glmark2" "$TOOLS/share/glmark2"
    log "   glmark2 场景数据就位"
fi

# 真二进制搬到 libexec/, 名字加 .bin (免得跟 wrapper 重名)
for b in fio glmark2-es2 glmark2-es2-wayland; do
    src=""
    for cand in "$SYSROOT/usr/bin/$b" "$SYSROOT/bin/$b"; do
        [ -f "$cand" ] && { src="$cand"; break; }
    done
    [ -n "$src" ] || { warn "   没找到 $b 的可执行文件"; continue; }
    cp -p "$src" "$TOOLS/libexec/$b.bin"
    chmod +x "$TOOLS/libexec/$b.bin"
    log "   $b → tools/libexec/$b.bin"
done

# ───────────────────────── ⑦ 生成 wrapper ─────────────────────────
#
# 那 10 个运行时共享库在 U 盘上, 不在系统路径里 ⇒ 链接器找不到 ⇒
# 「error while loading shared libraries」(当初 fio 就是这么翻车的)。
#
# wrapper 在 exec 真二进制**之前**把 LD_LIBRARY_PATH 指好。
# ⚠️ **sudo 默认 env_reset 会清掉 LD_LIBRARY_PATH** ⇒ 调用方式是
#    `sudo -n <wrapper>`, 不是 `sudo -n env LD_LIBRARY_PATH=… <真二进制>`。
log "⑦ 生成 wrapper"
#
# ⚠️ **模板只有一份**: `scripts/usb_wrapper.sh.tpl` ——
#    「构建新镜像」和「给现成的盘打补丁」都从它生成 ⇒ 两边**不会**对不上。
#    (2026-11-06: 盘上的 wrapper 少列了库的**子目录** ⇒ 换个板子 fio 就
#     起不来 ⇒ 好板子被返修。而构建脚本里那另一份拷贝迟早漂。)
WRAPPER_TPL="$PROJECT_ROOT/scripts/usb_wrapper.sh.tpl"
[ -f "$WRAPPER_TPL" ] || die "找不到 wrapper 模板: $WRAPPER_TPL"
gen_wrapper() {   # $1=工具名 $2=真二进制名
    local name="$1" real="$2"
    sed -e "s|@REAL@|$real|g" "$WRAPPER_TPL" > "$TOOLS/bin/$name"
    chmod +x "$TOOLS/bin/$name"
    if grep -q '@[A-Z_]*@' "$TOOLS/bin/$name"; then
        die "wrapper 里有没替换掉的占位符: $TOOLS/bin/$name"
    fi
    log "   wrapper: tools/bin/$name"
}
gen_wrapper fio fio.bin
gen_wrapper glmark2-es2 glmark2-es2.bin
gen_wrapper glmark2-es2-wayland glmark2-es2-wayland.bin

# ───────────────────────── ⑦b 一键启动器 (原生二进制) ─────────────────────────
#
# 工人**双击**盘里的启动器就能用 —— 不开终端、不改任何设备设置。
#
# 为什么不能直接双击 start.sh: 文件管理器(Nautilus)对**可执行的文本文件**
# 默认是「显示」而不是「运行」(executable-text-activation 默认 'display'),
# 这板子也没给 application/x-shellscript 注册默认程序 ⇒ 双击只会用文本
# 编辑器把源码显示出来。想让它跑, 要么改**每台设备每个用户**的 dconf 设置,
# 要么每次右键「以程序方式运行」—— 产线上都不现实。
# 而 Nautilus 对**原生可执行文件**默认就是「运行」⇒ 这个小二进制双击就跑。
#
# 它只做一件事: 找到自己所在目录 → exec bash <目录>/start.sh。
# 应用逻辑一行没动(还在 start.sh 里)。
#
# ⚠️ **静态链接**(-static): 目标板子的 libc 版本不一定跟构建机一样 ⇒
#    静态链接的启动器换台设备照样能跑。
log "⑦b 打一键启动器"
if command -v gcc >/dev/null 2>&1; then
    if gcc -O2 -static -Wall -o "$OUT_DIR/启动老化测试" \
              "$PROJECT_ROOT/scripts/usb_launcher.c" 2>"$OUT_DIR/.gcc.log"; then
        chmod 755 "$OUT_DIR/启动老化测试"
        log "   启动老化测试 ✓ ($(file -b "$OUT_DIR/启动老化测试" 2>/dev/null | cut -c1-40))"
    else
        warn "   编不出来 —— 看 $OUT_DIR/.gcc.log"
        warn "   ⇒ 工人双击 start.sh 会变成「显示源码」, 得用终端 bash start.sh"
    fi
    rm -f "$OUT_DIR/.gcc.log"
else
    warn "   没有 gcc —— 编不出启动器"
    warn "   ⇒ 工人双击 start.sh 会变成「显示源码」, 得用终端 bash start.sh"
fi

# ───────────────────────── ⑧ 安全检查: 别把核心库搬上去 ─────────────────────────
#
# LD_LIBRARY_PATH 排在系统路径**前面** ⇒ 盘上那份会盖住系统那份。
# 要是 core 库( libc / libstdc++ / ld-linux )也搬上来了, 动态链接器会拿
# 盘上那份去启动所有程序 —— 那是在拿整台机器赌。**发现就停手**。
log "⑧ 核心库检查"
core_bad=""
for so in "$SYSROOT"/usr/lib/*/libc.so.6 "$SYSROOT"/usr/lib/*/libstdc++.so.6 \
          "$SYSROOT"/usr/lib/*/libm.so.6 "$SYSROOT"/usr/lib/*/libpthread.so.0 \
          "$SYSROOT"/usr/lib/*/ld-linux-aarch64.so.1 \
          "$SYSROOT"/usr/lib/*/*/libc.so.6 "$SYSROOT"/usr/lib/*/*/libstdc++.so.6 \
          "$SYSROOT"/usr/lib/*/*/libm.so.6 "$SYSROOT"/usr/lib/*/*/libpthread.so.0 \
          "$SYSROOT"/usr/lib/*/*/ld-linux-aarch64.so.1; do
    [ -e "$so" ] && core_bad="$core_bad $so"
done
[ -z "$core_bad" ] || die "sysroot 里有核心库:$core_bad
   ⇒ LD_LIBRARY_PATH 会拿盘上那份盖住系统的 ⇒ 不能出这个镜像。
   (检查一下解包时混进了什么包 —— core 库一律不许搬。
    ⚠️ 子目录也扫: 2026-11-06 加 —— 有的包把库放在子目录里
       (usr/lib/aarch64-linux-gnu/ceph/), 只扫顶层会漏。)"
log "   没有核心库 ✓"

# ───────────────────────── ⑨ 标记 + 工人入口 ─────────────────────────
log "⑨ 写标记 + 工人入口"
cat > "$OUT_DIR/AGING_USB_MODE" <<EOF
U 盘版应用 —— 由 scripts/build_usb_image.sh 生成
构建时间: $(date '+%Y-%m-%d %H:%M:%S')
应用版本: $(sed -n 's/^Version: *//p' "$PROJECT_ROOT/deb/control" 2>/dev/null || echo unknown)
构建主机: $(uname -m) / python $PYV
EOF

# ── 版本戳 ───────────────────────────────────────────────────────────
#   产线那边**分不清手上这根盘是哪一版** ⇒ 旧盘(满核 8 线程)一直流出去 ⇒
#   板子成批硬复位。⇒ 这一份给工人/产线一个能看懂的答案 + **自查命令**。
#
#   ⚠️ **这里只读文本、只写文本**, 不执行镜像里的任何东西。
_cfg_threads="$(grep -m1 -E '^[[:space:]]*threads:' "$OUT_DIR/config.yaml" 2>/dev/null \
                | sed -e 's/#.*//' -e 's/[^0-9]*\([0-9]\+\).*/\1/')"
_code_cap="$(grep -m1 -E '^MAX_STRESS_THREADS[[:space:]]*=' "$OUT_DIR/server/tests/cpu.py" 2>/dev/null \
                | sed -e 's/[^0-9]*\([0-9]\+\).*/\1/')"
_cfg_threads="${_cfg_threads:-0}"
_code_cap="${_code_cap:-0}"
# 旋钮能拧到几核 = 页面上那个输入框的 max(从镜像里的 index.html 读)
# ⚠️ **2026-11-09 晚(更晚)加的**: 默认值从 8 改成 4 之后, 「满核警告」如果
#   继续看**出厂默认**就永远触发不了(默认是 4)⇒ 而旋钮**照样能拧到 8**
#   ⇒ 警告必须看**用户拧得到几核**(= min(旋钮 max, 代码硬上限))。
_knob_max="$(grep -m1 -oE 'tests\[t\.name\]\.threads[^>]*max="[0-9]+"' \
            "$OUT_DIR/static/index.html" 2>/dev/null \
            | grep -oE 'max="[0-9]+"' | grep -oE '[0-9]+')"
_knob_max="${_knob_max:-0}"
_eff=99                        # 出厂默认(受硬上限夹住); 99 = 不设限
if [ "$_cfg_threads" -gt 0 ] 2>/dev/null; then _eff=$_cfg_threads; fi
if [ "$_code_cap" -gt 0 ] 2>/dev/null && [ "$_code_cap" -lt "$_eff" ]; then _eff=$_code_cap; fi
_reach=99                      # 用户拧得到的上限(旋钮 max ∧ 代码硬上限)
if [ "$_knob_max" -gt 0 ] 2>/dev/null; then _reach=$_knob_max; fi
if [ "$_code_cap" -gt 0 ] 2>/dev/null && [ "$_code_cap" -lt "$_reach" ]; then _reach=$_code_cap; fi

# ── 出厂断言: 这根盘**不许**能压到**超过核数** ──────────────────────
#
#   ⚠️ **2026-11-09 晚: 这条断言的口径改了(用户授权)** —— 原来是「不许能
#   压到 7 核以上」(那时上限是 6 核, 满核 8 线程 = 过应力 ⇒ 板子成批硬
#   复位, 「宁可打不出来, 也不打出一根满核 8 线程的盘」)。现在上限放回
#   **8 核 = 满核**(用户决定: 「既然降到 5 核都还是会重启, 那就直接改回
#   八核默认, 我可以手动降低核心 …… 我先找到一个平衡点再来修改默认
#   参数」)⇒ 断言改成「**不许超过核数**」—— 它守的是「别做出一个能让
#   板子压到比核数还多的盘」(那种是配置写坏了), 不再代替「不许满核」。
#
#   ⚠️ **满核 = 过应力这件事没消失** ⇒ 满核时**必须警告**, 让产线知道
#   「开跑前先在页面上把『线程数』调低」。
if [ "$_eff" -gt 8 ] 2>/dev/null; then
    die "这镜像的出厂默认是 $_eff 核(>8 = 比核数还多 ⇒ 配置写坏了)
     ⇒ 修法: ① config.yaml 的 cpu.threads 改成 ≤ 8(出厂默认建议 4)
              ② 或在 server/tests/cpu.py 里保留 MAX_STRESS_THREADS = 8 这个硬上限"
fi
if [ "$_code_cap" -gt 8 ] 2>/dev/null; then
    die "代码里的硬上限是 $_code_cap 核(>8 = 比核数还多 ⇒ 代码写坏了)
     ⇒ 修法: server/tests/cpu.py 的 MAX_STRESS_THREADS 改回 8"
fi
if [ "$_code_cap" -eq 0 ] 2>/dev/null; then
    warn "代码里没有 MAX_STRESS_THREADS 硬上限 —— 只靠 config.yaml 兜着
     ⇒ 页面「线程数」旋钮一改就能压到 8 核 ⇒ 建议把硬上限加回去"
fi
# ⚠️ **2026-11-09 晚(更晚): 默认值定在 4 核, 上限仍是 8**(用户点名授权:
#   「确定了, 默认四核, 但是像现在这样支持用户自己选择核数, 四核比较保险」)
#   ⇒ **默认这一档现在是保险档**(4 核 + 应用自己的背景 ≈ 5.7 核, 离
#     「整板 ≈ 7 核」的天花板有余量);
#   ⇒ 但**旋钮照样能拧到 8** ⇒ 这条「满核 = 过应力」的警告**必须跟着
#     「用户拧得到几核」走**(`$_reach` = min(旋钮 max, 代码硬上限)),
#     **不能跟着出厂默认走** —— 默认改成 4 之后, 若还看 `$_eff`(那是 4),
#     这条警告就**永远不会触发**了, 产线就收不到「别往上调」的提醒
#     ⇒ **这条改动是让警告多触发, 不是让它少触发**。
if [ "$_reach" -ge 8 ] 2>/dev/null; then
    warn "这镜像的旋钮能拧到**满核 $_reach 线程**(出厂默认是 $_eff 核)
     ⇒ 满核是过应力那一档(2026-11-04 实测会硬复位)
     ⇒ **开跑前请确认页面上 CPU 的「线程数」**, 别往上调(填 0 = 满核)"
fi
log "   出厂断言: 出厂默认 $_eff 核 / 旋钮上限 $_reach 核(≤8)✓"

_dead_guard="无"
if grep -rq 'cpu_evidence_cut_short' "$OUT_DIR/server/" 2>/dev/null; then _dead_guard="有"; fi
_git_ver="$(git -C "$PROJECT_ROOT" rev-parse --short HEAD 2>/dev/null || echo 无git)"
cat > "$OUT_DIR/版本.txt" <<EOF
老化测试 U 盘镜像 —— 版本戳
============================
构建时间: $(date '+%Y-%m-%d %H:%M:%S')
代码版本: $_git_ver
构建主机: $(uname -m) / python ${PYV:-未知}

CPU 加压:
  · 出厂默认线程数: $([ "$_cfg_threads" = 0 ] && echo "0(按核数)" || echo "$_cfg_threads")
  · 代码硬上限: $([ "$_code_cap" = 0 ] && echo "无" || echo "$_code_cap 核")
  · 旋钮上限  : $([ "$_reach" = 0 ] && echo "?" || echo "$_reach 核")
  · 「死在加压途中 ⇒ 判 failed 不放行」的兜底: $_dead_guard

怎么确认手上这根盘是哪一版(在盘根目录下跑):
  grep -m1 "threads:" config.yaml
      => 必须是  threads: 4
      => 若是   threads: 8  = 旧盘(出厂默认满核 8 线程, 板子会成批硬复位)
  grep -h "本轮加压峰值" logs/cpu_evidence_*.txt | tail -3
      => 写「4 线程」= 这一版(出厂默认 4 核); 写「8 线程」= 有人把旋钮拧到满核了
EOF
log "   写了「版本.txt」(产线一眼能认出手上这根盘是哪一版)"

cat > "$OUT_DIR/怎么启动.txt" <<'EOF'
怎么启动老化测试
=================

① 双击盘里的「启动老化测试」        ← 推荐: 不用开终端, 不用改任何设置
② 终端里一行:  bash <这个盘>/start.sh
③ 右键 start.sh → 「以程序方式运行」

⚠️ 别双击 start.sh 本身 —— 那只会「显示源码」: 文件管理器(Nautilus)对
   **可执行的文本文件**默认是显示而不是运行(它的 executable-text-activation
   默认值是 'display'), 这板子也没给 .sh 注册默认程序 ⇒ 会用文本编辑器打开。
   「启动老化测试」是原生程序, 双击直接跑。

1. 把这根 U 盘插到板子上。
2. 打开「文件」, 点一下这个盘 —— 系统会把它挂上(这一步是自动的)。
3. 双击「启动老化测试」。
4. 浏览器会自动打开 http://127.0.0.1:8000, 勾选要测的项目, 点开始。
   (打 127.0.0.1 不打 localhost: 有的机器 localhost 先解析成 ::1,
    而服务只听 0.0.0.0(IPv4)⇒ 浏览器会连不上)

跑完拔盘就走 —— 程序、历史记录、报告全在这根盘上, 设备上不留东西。

要停服务: 打开终端, cd 到这个盘, bash ./start.sh stop
看服务日志: 这个盘里的 logs/server.log
报告:        这个盘里的 reports/pdf/  (另有一份索引 AGING-REPORTS/INDEX.csv)

⚠️ fio / glmark2 那两项如果显示「error」而不是结果, 是**没被授权执行**
   (sudo 的白名单里缺了它要跑的那条命令), 不是板子坏了。
EOF
log "   写了「怎么启动.txt」(工人双击「启动老化测试」即可)"

# ───────────────────────── ⑩ 自检 ─────────────────────────
#
# ⚠️ **这一步会执行刚解包出来的二进制**(fio / glmark2)。那是从 .deb 里来的
#    外部代码 —— 构建脚本默认就执行它, 等于「构建时替产线跑一遍陌生代码」。
#    所以它**可以关掉**(`--no-self-check`), 关掉时只做静态检查。
#    要跑自检的场景: 在板子上构建(那台机器上的二进制是可信的)。
log "⑩ 自检"
if [ "$SELF_CHECK" = "0" ]; then
    warn "   --no-self-check: 不执行刚解出来的二进制 (只做静态检查)"
else
    if [ -x "$TOOLS/bin/fio" ]; then
        if out="$("$TOOLS/bin/fio" --version 2>&1)"; then
            log "   fio 能起来 ✓ ($(echo "$out" | head -1))"
        else
            warn "   fio 起不来: $(echo "$out" | head -3)"
            warn "   ⇒ 这镜像在板子上 fio 项会判 error。检查 sysroot 里 .so 齐不齐。"
        fi
    fi
    if [ -x "$TOOLS/bin/glmark2-es2" ]; then
        if out="$("$TOOLS/bin/glmark2-es2" --help 2>&1)"; then
            if echo "$out" | grep -q -- "--data-path"; then
                log "   glmark2 支持 --data-path ✓"
            else
                warn "   glmark2 的 --help 里没有 --data-path —— wrapper 加的参数它不认"
                warn "   ⇒ GPU 项会起不来。glmark2 版本不对?"
            fi
        else
            warn "   glmark2 起不来: $(echo "$out" | head -3)"
        fi
    fi
fi
[ -x "$OUT_DIR/venv/bin/python" ] && log "   venv ✓"
[ -f "$OUT_DIR/AGING_USB_MODE" ] && log "   标记 ✓"

# ───────────────────────── ⑩b 「换台板子还能用吗」检查 ─────────────────────────
#
# ⚠️ **这一步是静态的**(只解析 ELF 头, **不执行**盘里的任何二进制) ——
#    所以就算加了 `--no-self-check` 它也照跑。
#
#    它问的是: 每个依赖库, 到了**别的板子**上还找得到吗?
#    链接器的找法: RPATH → LD_LIBRARY_PATH(wrapper 给的) → RUNPATH
#                 → ld.so.cache → 标准目录
#    ⇒ 「**只能靠库里写死的绝对路径(RUNPATH)找到**」= 危险: 那个路径是
#      **目标板子**的 ⇒ 换个没这目录的板子就 rc=127「起不来」
#      (2026-11-06 实测: libceph-common.so.2 ⇒ eMMC 项 error ⇒
#       **好板子被当成坏板子返修**)
#
# ⚠️ 搜索路径**必须**跟 §⑦ wrapper 里那份一致(两边都写着同一串 glob)
#    ⇒ `tests/test_usb_image_anchors.py` 会静态比对这两份, 不一致就红。
log "⑩b 检查「换台板子还能用吗」(静态解析 ELF, 不执行盘里的二进制)"
if [ -f "$PROJECT_ROOT/scripts/check_usb_selfcontained.py" ]; then
    if ! python3 "$PROJECT_ROOT/scripts/check_usb_selfcontained.py" "$TOOLS"; then
        die "这根盘在**别的板子**上起不来(见上面)⇒ 不能出这个镜像。
   修法: ① wrapper 的搜索路径带上库所在的**子目录**(§⑦)
        ② 或者把缺的 .deb 也解进 tools/root(§⑥ USB_PKGS)"
    fi
else
    warn "   找不到 scripts/check_usb_selfcontained.py —— 跳过「换台板子」检查"
fi

# ⭐⭐⭐ **可搬迁自检** —— 把镜像**整个挪到另一个目录**再跑一遍。
#
#   为什么必须做: 镜像是在**临时构建目录**里打的(形如 /tmp/aging-usb-image),
#   venv 里那些 console script(uvicorn/pip/fastapi…)的第一行因此**写死了
#   构建路径**(`#!/tmp/aging-usb-image/venv/bin/python3`)⇒ 盘插到**别的设备**
#   上那个路径根本不存在 ⇒ 内核 "bad interpreter" ⇒ 服务起不来 ⇒
#   而桌面双击**没有终端** ⇒ 工人只看到「点了没反应」(2026-11-05 实测:
#   本机能起来纯粹是因为构建目录还没被删掉)。
#
#   ⇒ 这里直接把镜像挪走再跑: 挪之前能跑、挪之后跑不了 = 里面有写死的路径。
#   ⇒ 挪完再挪回来; 中途要是 die 了, 镜像留在 <OUT_DIR>-reloc-check,
#     报错里写清楚了在哪儿。
log "   可搬迁自检 (挪到别的路径再跑一遍)"
RELOC_DIR="${OUT_DIR}-reloc-check"
if [ -e "$RELOC_DIR" ]; then
    warn "   (自检目录已存在, 跳过挪动: $RELOC_DIR)"
else
    mv "$OUT_DIR" "$RELOC_DIR"
    if "$RELOC_DIR/venv/bin/python3" -m uvicorn --version >/dev/null 2>&1; then
        log "   ✓ 挪走后 venv 照样能跑 (可搬迁)"
        mv "$RELOC_DIR" "$OUT_DIR"
    else
        die "挪到 $RELOC_DIR 之后 venv 起不来 ⇒ 镜像里有写死的绝对路径
     ⇒ 盘插到别的设备上必然失败(新设备上双击「启动老化测试」没反应)。
     ⇒ 应用侧的正确做法: 用 <venv>/bin/python3 -m uvicorn, 别调 console script。
     ⇒ 镜像现在留在: $RELOC_DIR"
    fi
fi

# 应用**不许**调 venv 里的 console script —— 它们的 shebang 写死构建路径。
# (start.sh 已改成 `python3 -m uvicorn` / `python3 -m pip`; 这里防回退)
#
# ⚠️ **判据不能写成 `if grep ... | head -5; then`** (2026-11-09 修):
#    管道的退出码取自**最后一个命令(head)**, 它**永远成功** ⇒
#    **没匹配也会进 then 分支** ⇒ 这道闸门**恒为真** ⇒
#    每次打包都在最后一步 die 掉(而镜像是好的、start.sh 是干净的)⇒
#    **新镜像出不来 ⇒ 产线一直用旧盘 ⇒ 板子成批硬复位**。
#    ⇒ 必须**先把匹配结果存进变量, 再判非空**。
_bad="$(sed -e 's/#.*//' "$OUT_DIR/start.sh" 2>/dev/null \
        | grep -nE '\$VENV_DIR"?/bin/' \
        | grep -v '/bin/python' | head -5)"
if [ -n "$_bad" ]; then
    printf '%s\n' "$_bad"
    die "start.sh 里还在直接调 venv 的 console script ⇒ 换台设备必坏
     ⇒ 一律改成 \"\$VENV_DIR\"/bin/python3 -m <模块>"
fi
log "   start.sh 不调 console script ✓"

log ""
log "✅ 镜像打好了: $OUT_DIR"
log ""
log "下一步:"
log "  1. 烧到 U 盘 (ext4!):"
log "       sudo /sbin/mkfs.ext4 -L AGING /dev/sdX"
log "       sudo mkdir -p /mnt/aging && sudo mount /dev/sdX /mnt/aging"
log "       sudo cp -a $OUT_DIR/. /mnt/aging/"
log "       sudo chmod a+rwx /mnt/aging   # 挂载点本身也要可写"
log "  2. 在板子上插好盘, 在文件管理器里点一下这个盘(会挂上), 双击盘里的「启动老化测试」"
log ""
log "⚠️ sudoers **不用改**: 应用让 sudo 跑的是 /usr/bin/timeout(或 /usr/bin/env),"
log "   盘上的 fio/glmark2 只是它的参数 ⇒ 产线现有的白名单继续有效。"
