#!/bin/bash
# 打 deb 包 —— 工厂老化测试工具 (docs/需求设计.md §11.7)
#
# 用法:
#   bash scripts/build_deb.sh                     # 正常打一个包
#   bash scripts/build_deb.sh --offline           # 不联网 (deps/ 必须已备好)
#   bash scripts/build_deb.sh --deps-dir /tmp/_deps
#   bash scripts/build_deb.sh --keep-staging      # 打完保留 staging 目录
#
# 出来的包长这样:
#   /usr/lib/aging-test/      代码 (root 所有, 工人只读)
#   /etc/aging-test/          配置 (dpkg conffile, 升级不覆盖)
#   /etc/default/aging-test   数据根指示 (Debian 惯例)
#   /var/lib/aging-test/      数据 (history.db / 报告 PDF / 日志) —— 装机时建
#   /usr/share/applications/  桌面图标
#   /usr/bin/aging-test       启动命令
#   /usr/lib/aging-test/deps/ 随包带的系统工具 .deb (离线仓)
#   /usr/lib/aging-test/wheels/ 随包带的 python wheel (离线仓)
#
# ⚠️ 三条硬规矩 (§11.7):
#   ① **打包结果里不能有测试数据** —— 历史记录 (history.db)、报告 PDF、
#      日志、__pycache__ 一律不带。带进去就是把那台开发机的产线数据混进
#      出货包, 别的板子装上会看到别人的记录。
#   ② **装机时不能联网** —— python 依赖和系统工具都随包带走, 装的时候
#      离线解包、离线 pip。所以这两个"离线仓"必须在打包时就备齐。
#   ③ **工具的运行时共享库必须闭环** —— 工具是 dpkg-deb -x 解过去的, 不走
#      dpkg 依赖解析, 所以"这个工具在目标机上能不能跑起来"得自己保证。
#      ③ 由 deb/check_runtime_closure.py 把关 (见下面的 ③ 步)。
#
# 【授权】用户 2026-10-29 点名授权:
#   操作: 从本机已配置的 TUNA ubuntu-ports 源 apt-get download 系统工具 .deb
#         (17 个工具 + 10 个运行时共享库), 连同代码与 wheel 一起
#         dpkg-deb --build 打成 deb; 随包的 .deb 由 postinst 用 dpkg-deb -x
#         解包, 只在缺件时装。
#   目标: 产线 A527 板子 (出货镜像同款)。
#   边界: 不装 chromium; 不改 dpkg 数据库; 不联网做别的。
#
# 【授权】用户 2026-11-02 点名授权 (0.1.0-6 这一版, 离线重打):
#   操作: 在本仓执行 `bash scripts/build_deb.sh --offline`, 产出
#         dist/aging-test_0.1.0-6_arm64.deb; 随后执行
#         `bash scripts/verify_deb.sh dist/aging-test_0.1.0-6_arm64.deb` 过闸。
#   目标: 本仓库的 aging-test 应用 (产线 A527 板子用的那一份)。
#   边界: **全程离线** —— 系统工具 .deb 用 2026-10-29/30 已缓存的
#         build/deps/*.deb (27 个 focal 官方包, 源自 TUNA ubuntu-ports),
#         python wheel 从本机 pip 缓存 ~/.cache/pip 取, 不联网;
#         只写本机 build/ 与 dist/; 不装到任何板子上; 不碰 dpkg 数据库;
#         不碰 /lib、/bin; 不带 chromium。
#
#   ⚠️ **既有产物一律保留, 不删除 / 不覆盖 / 不截断** (2026-11-02 授权时
#      明确划的边界):
#         · build/deps/*.deb  (27 个, **只读输入** —— 只被 cp -p 读走,
#           脚本自己一个字节都不写它);
#         · dist/ 里已有的 0.1.0-1 .. 0.1.0-5 **五个包** —— 留着备查,
#           本次**不碰**;
#         · 本次**只新增**两个路径: build/staging-<时间戳>/ (全新目录,
#           打完由 `mv` 挪到 /tmp/aging-staging-<时间戳>/, 不是删除)
#           与 dist/aging-test_0.1.0-6_arm64.deb (新文件名, 之前不存在)。
#         · 本脚本**通篇没有 rm -rf / rm -f**(staging 用的是**带时间戳的
#           新目录**, 每次都不跟上次撞)。
set -eu

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

OUT_DIR="$PROJECT_ROOT/dist"
DEPS_DIR=""
OFFLINE=0
NO_WHEELS=0
KEEP_STAGING=0
TS="$(date +%Y%m%d-%H%M%S)"
#: 每次用**带时间戳的新目录** —— 不用 rm -rf 清目录, 也不会误删上一次的结果
STAGING="$PROJECT_ROOT/build/staging-$TS"

# ───────────────────────── 工具包清单 ─────────────────────────
#
# 这 17 个包 = 11 个测试项跑起来要用的全部外部命令 + GPU 的场景数据。
# 逐个对应关系:
#   stress-ng          → CPU 压力
#   fio                → 存储读写 (USB / eMMC / TF)
#   memtester          → 内存
#   ethtool            → 网口链路
#   iproute2 (ip)      → 网口 / 通用
#   iputils-ping      → 网口 ping
#   busybox (udhcpc)   → 网口 DHCP
#   iw                 → WiFi 扫描
#   rfkill             → WiFi 射频屏蔽检查
#   usbutils (lsusb)   → USB 枚举
#   util-linux (lsblk) → 块设备枚举
#   x11-xserver-utils (xrandr) → HDMI 读实际输出
#   glmark2-es2        → GPU 渲染 (X11 会话)
#   glmark2-es2-wayland→ GPU 渲染 (Wayland 会话)
#   glmark2-data       → GPU 场景数据
#   xdg-utils          → 用默认浏览器打开 UI
#   fonts-wqy-zenhei   → 中文字形 (PDF 报告里是中文, 没字形会静默变空白 §8.2.1)
#
# ⚠️ **chromium 不在里面** —— 它由出货镜像自带 (§11.7)。Ubuntu 20.04 仓库里
#    的 chromium-browser 是个 48KB 的 snap 过渡包, 装上会让 PDF 报告静默
#    变成空白 —— 比没有还糟。下面的前置检查会显式断言这一点。
TOOL_PKGS=(
    stress-ng
    fio
    memtester
    ethtool
    iw
    rfkill
    busybox
    util-linux
    iproute2
    usbutils
    iputils-ping
    x11-xserver-utils
    xdg-utils
    fonts-wqy-zenhei
    glmark2-es2
    glmark2-es2-wayland
    glmark2-data
)

# ───────────────── 运行时共享库 (不是工具, 是让工具能起来的那些 .so) ─────────────────
#
# ⚠️ **为什么单独列一份**:
#   工具是 `dpkg-deb -x` 解过去的, **不走 dpkg 依赖解析** —— 也就是说
#   「工具 Needs 的那些 .so 在目标机上有没有」没人替我们保证。
#
#   fio 就是这么翻车的: 它 ELF 里 NEEDED `librbd.so.1`, 而 librbd1 / librados2 /
#   libtirpc3 / libgfapi0 / ... 全是**开发期 apt install fio 时才拖进来的**
#   (2026-10-19), 出货镜像 (2026-10-16 烤的) 里根本没有。
#   → 干净板子上 fio 装上了, 一跑就 `error while loading shared libraries`，
#     eMMC / TF 卡 / USB 三项测试全废。
#
#   下面这 10 个 = 工具的 ELF NEEDED 里, **出货镜像里没有**的那些。
#   判据由 deb/check_runtime_closure.py 逐个 ELF 核, 缺一个就不许出包。
#   哪些库算「镜像自带」, 见 deb/image-baseline.txt (出货镜像契约)。
RUNTIME_PKGS=(
    librbd1              # librbd.so.1   ← fio 直接 NEEDED
    librados2            # librados.so.2 + libceph-common.so.2
    libtirpc3            # libtirpc.so.3
    libboost-iostreams1.71.0
    libgfapi0            # gfapi 栈 (librados 用)
    libgfrpc0
    libgfxdr0
    libglusterfs0
    librdmacm1           # librdmacm.so.1 ← fio 直接 NEEDED
    libatm1              # libatm.so.1   ← iproute2 的 tc 插件要
)

# ⚠️ **python2.7 那一串故意不带** —— fio 的 *包级* Depends 里有 python2.7,
#    那是给 librbd1 的 python 绑定用的; librbd.so.1 运行时不碰它。
#    我们走 dpkg-deb -x (不解析包依赖), 所以不需要 —— 而且往产线板上塞第二个
#    Python 属于没事找事。
# ⚠️ **libtirpc-common 也不带** —— 它只带一个 /etc/netconfig, 解过去会覆盖
#    目标机的系统配置, 而运行时并不需要它。

# ───────────────────────── 参数 ─────────────────────────
while [ $# -gt 0 ]; do
    case "$1" in
        --out)        OUT_DIR="$2"; shift 2 ;;
        --deps-dir)   DEPS_DIR="$2"; shift 2 ;;
        --offline)    OFFLINE=1; shift ;;
        --no-wheels)  NO_WHEELS=1; shift ;;
        --keep-staging) KEEP_STAGING=1; shift ;;
        -h|--help)    sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "未知参数: $1" >&2; exit 2 ;;
    esac
done

log()  { echo "==> $*"; }
warn() { echo "⚠️  $*" >&2; }
die()  { echo "✗ $*" >&2; exit 1; }

# ═════════════════════ ① 前置检查 ═════════════════════
log "① 前置检查"

command -v dpkg-deb >/dev/null 2>&1 || die "没有 dpkg-deb —— 装 packaging 工具 (apt install dpkg-dev)"

ARCH="$(dpkg --print-architecture)"
CTRL_ARCH="$(sed -n 's/^Architecture: *//p' deb/control)"
[ "$ARCH" = "$CTRL_ARCH" ] || die "架构对不上: 本机 $ARCH, 包里写的是 $CTRL_ARCH"
log "   架构 = $ARCH ✓"

# python 版本必须对上 —— wheels 是 cp38 编译件, 换到 3.10 就装不上了
PYV="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
case "$PYV" in
    3.8) log "   python3 = $PYV ✓ (wheel 是 cp38 编译件, 对得上)" ;;
    *)   die "python3 = $PYV, 但随包 wheel 是 cp38 —— 必须用 python3.8 的机器打包
   (focal 自带 3.8; 若哪天升到 22.04 变成 3.10, 就得重下 cp310 的 wheel)" ;;
esac

# chromium 必须不在清单里 (§8.2.1 的坑)
for p in "${TOOL_PKGS[@]}" "${RUNTIME_PKGS[@]}"; do
    case "$p" in
        chromium*|chromium-browser*)
            die "工具清单里出现了 chromium —— 出货镜像自带它, 而且仓库里那个
   是 snap 过渡包, 装上 PDF 报告会静默变空白 (§8.2.1)" ;;
    esac
done
log "   chromium 不在清单里 ✓ (由出货镜像自带)"

# 核心库绝对不能随包带 —— 它们覆盖目标机的 libc/stdc++ 是事故
for p in "${RUNTIME_PKGS[@]}"; do
    case "$p" in
        libc6|libc-bin|libstdc++6|libgcc-s1|zlib1g|libcrypt1|libssl1.1|dpkg|systemd|bash)
            die "运行库清单里出现了核心包 '$p' —— 解过去会覆盖目标机的核心
   库 (glibc 换版本能把板子搞挂)。核心库必须由出货镜像提供。" ;;
    esac
done
log "   运行库清单里没有核心库 ✓ (libc6 / libstdc++6 之类一律不碰)"

VERSION="$(sed -n 's/^Version: *//p' deb/control)"
PKG_NAME="$(sed -n 's/^Package: *//p' deb/control)"
DEB_FILE="$OUT_DIR/${PKG_NAME}_${VERSION}_${ARCH}.deb"

# ═════════════════════ ② 备工具 .deb ═════════════════════
log "② 备系统工具 .deb (${#TOOL_PKGS[@]} 个工具 + ${#RUNTIME_PKGS[@]} 个运行时共享库)"
if [ -z "$DEPS_DIR" ]; then
    DEPS_DIR="$PROJECT_ROOT/build/deps"
fi
mkdir -p "$DEPS_DIR"

missing_deb=()
for p in "${TOOL_PKGS[@]}" "${RUNTIME_PKGS[@]}"; do
    # 目录里已经有同名的就用它 (复用之前下好的)
    found="$(ls "$DEPS_DIR"/${p}_*.deb 2>/dev/null | head -1 || true)"
    if [ -n "$found" ]; then
        continue
    fi
    if [ "$OFFLINE" = "1" ]; then
        missing_deb+=("$p")
        continue
    fi
    log "   下载 $p ..."
    ( cd "$DEPS_DIR" && apt-get download "$p" >/dev/null 2>&1 ) \
        || missing_deb+=("$p")
done

if [ ${#missing_deb[@]} -gt 0 ]; then
    die "这些工具包没备齐: ${missing_deb[*]}
   联网时重跑一次本脚本 (不加 --offline), 或者:
   在联网机器上 apt-get download ${missing_deb[*]} 后把 .deb 拷到 $DEPS_DIR"
fi

n_deb=$(ls "$DEPS_DIR"/*.deb 2>/dev/null | wc -l)
log "   工具 .deb 就位: $n_deb 个"

# ═════════════════════ ③ 解包安全性闸 ═════════════════════
# 工具是**直接解到 /** 的 —— 解包这一步本身会不会砸了板子, 得单独验。
#
# Ubuntu 20.04 是 usrmerge: /lib → usr/lib、/bin → usr/bin 都是**软链**,
# 动态链接器 (/lib/ld-linux-aarch64.so.1) 就挂在 /lib 底下。而 `dpkg-deb -x`
# 底下是裸 tar —— 遇到包里的 `./lib/` 这种**目录成员**就会**把软链换成
# 真目录** ⇒ 动态链接器消失 ⇒ 板子上所有动态链接的程序全死
# (2026-10-29 新机装机失败就是踩了这个)。
#
# 这道闸: ① 逐包查有没有这种「目录成员」; ② 查 postinst 是不是真的
# 用了保软链的解法 (`tar --keep-directory-symlink`)。没有就别出包。
log "③ 解包安全性检查 (usrmerge 软链)"
python3 "$PROJECT_ROOT/deb/check_extract_safety.py" "$DEPS_DIR" \
    || die "解包会踩坏 usrmerge 的 /lib /bin 软链 —— 动态链接器会消失, 板子就废了
   ⇒ 别出这个包 (详见上面 deb/check_extract_safety.py 的说明)"

# ═════════════════════ ④ 运行时闭包闸 ═════════════════════
# 工具是 dpkg-deb -x 解过去的, 不走 dpkg 依赖解析 ⇒「它在目标机上能不能起来」
# 得自己保证。这道闸逐个 ELF 读 NEEDED 表, 对不上「镜像自带 ∪ 随包带走」就
# 拒绝出包 (fio 缺 librbd.so.1 就是这么被逮住的)。
log "④ 运行时共享库闭包检查"
python3 "$PROJECT_ROOT/deb/check_runtime_closure.py" "$DEPS_DIR" \
    --baseline "$PROJECT_ROOT/deb/image-baseline.txt" \
    || die "运行时闭包不完整 —— 这些工具装到目标机上跑不起来, 别出这个包
   (缺口清单见上面; 补进 TOOL_PKGS / RUNTIME_PKGS 后重打)"

# ═════════════════════ ⑤ 备 python wheels ═════════════════════
log "⑤ 备 python wheels (离线仓)"
WHEELS_STAGE="$STAGING/usr/lib/aging-test/wheels"
if [ "$NO_WHEELS" = "1" ]; then
    warn "   --no-wheels: 装机时会去联网装 python 依赖 —— 违反 §11.7, 别在产线上用"
    mkdir -p "$WHEELS_STAGE"
else
    mkdir -p "$WHEELS_STAGE"
    python3 "$PROJECT_ROOT/deb/collect_wheels.py" --out "$WHEELS_STAGE" \
        || die "收 wheel 失败 —— 见上面的报错"
    n_whl=$(ls "$WHEELS_STAGE"/*.whl 2>/dev/null | wc -l)
    [ "$n_whl" -gt 0 ] || die "wheels 目录是空的"
    log "   wheel 就位: $n_whl 个"
fi

# ═════════════════════ ⑥ 铺 staging ═════════════════════
log "⑥ 铺文件到 staging"
PREFIX="$STAGING/usr/lib/aging-test"

copy_tree() {   # $1=源目录 $2=目标目录
    local src="$1" dst="$2"
    [ -d "$src" ] || return 0
    ( cd "$src" && find . -name '__pycache__' -prune -o -name '*.pyc' -prune -o -type f -print ) \
    | while IFS= read -r f; do
        mkdir -p "$dst/$(dirname "$f")"
        cp -p "$src/$f" "$dst/$f"
    done
}

# 代码
copy_tree "$PROJECT_ROOT/server" "$PREFIX/server"
copy_tree "$PROJECT_ROOT/static" "$PREFIX/static"

# 脚本 —— **白名单**, 不整目录复制:
#   probe_rp_gpio_write.py 是**开发期探针**, 产线上用不上 ⇒ 不随包走。
#   ⚠️ 这里原来写的理由是「它写 /proc/rp_gpio/pi5 会让板子 kernel panic」
#     —— **那条结论已经作废**(2026-10-22 终端实测连写 5 次板子都正常;
#     脚本自己的 docstring 里已经更正过)⇒ 现在的理由只剩「开发期工具」。
#     崩因至今没查清的是「UI 上单独跑风扇测试」那件事, 不是这个脚本。
#   其余几个是产线上真用得上的(装依赖 / 自检 / 桌面图标)。
mkdir -p "$PREFIX/scripts"
for s in check_deps.sh install_deps.sh install_desktop_icon.sh; do
    [ -f "$PROJECT_ROOT/scripts/$s" ] && cp -p "$PROJECT_ROOT/scripts/$s" "$PREFIX/scripts/$s"
done

# 单文件
cp -p "$PROJECT_ROOT/config.yaml"      "$PREFIX/config.yaml"
cp -p "$PROJECT_ROOT/requirements.txt" "$PREFIX/requirements.txt"
cp -p "$PROJECT_ROOT/start.sh"         "$PREFIX/start.sh"

# 工具离线仓
mkdir -p "$PREFIX/deps"
cp -p "$DEPS_DIR"/*.deb "$PREFIX/deps/"

# 配置与默认值
mkdir -p "$STAGING/etc/aging-test" "$STAGING/etc/default"
cp -p "$PROJECT_ROOT/config.yaml" "$STAGING/etc/aging-test/config.yaml"
cat > "$STAGING/etc/default/aging-test" <<EOF
# 数据根: history.db / 报告 PDF / 服务日志都落这儿
AGING_DATA_DIR=/var/lib/aging-test
EOF

# 桌面图标 + 启动命令
mkdir -p "$STAGING/usr/share/applications" "$STAGING/usr/bin"
cp -p "$PROJECT_ROOT/deb/aging-test.desktop" "$STAGING/usr/share/applications/aging-test.desktop"
cat > "$STAGING/usr/bin/aging-test" <<'EOF'
#!/bin/sh
# 老化测试工具的启动命令 (桌面图标调它)
exec /usr/lib/aging-test/start.sh "$@"
EOF
chmod 0755 "$STAGING/usr/bin/aging-test" "$PREFIX/start.sh"
chmod 0755 "$PREFIX/scripts"/*.sh 2>/dev/null || true

log "   代码 + 配置 + 离线仓 铺好"

# ═════════════════════ ⑦ 生成 DEBIAN/ ═════════════════════
log "⑦ 生成 DEBIAN/"
mkdir -p "$STAGING/DEBIAN"
cp -p "$PROJECT_ROOT/deb/control"   "$STAGING/DEBIAN/control"
cp -p "$PROJECT_ROOT/deb/conffiles" "$STAGING/DEBIAN/conffiles"
cp -p "$PROJECT_ROOT/deb/postinst"  "$STAGING/DEBIAN/postinst"
cp -p "$PROJECT_ROOT/deb/prerm"     "$STAGING/DEBIAN/prerm"
cp -p "$PROJECT_ROOT/deb/postrm"    "$STAGING/DEBIAN/postrm"
chmod 0755 "$STAGING/DEBIAN/postinst" "$STAGING/DEBIAN/prerm" "$STAGING/DEBIAN/postrm"
chmod 0644 "$STAGING/DEBIAN/control" "$STAGING/DEBIAN/conffiles"

# ═════════════════════ ⑧ dpkg-deb --build ═════════════════════
log "⑧ dpkg-deb --build"
mkdir -p "$OUT_DIR"
# --root-owner-group: 打包时统一算成 root:root —— 装机后代码就是 root 所有,
# 工人只读; 不用 sudo 打包也能打出正确的属主。
dpkg-deb --root-owner-group --build "$STAGING" "$DEB_FILE" >/dev/null \
    || die "dpkg-deb --build 失败"
log "   出了包: $DEB_FILE ($(du -h "$DEB_FILE" | cut -f1))"

# ═════════════════════ ⑨ 校验 ═════════════════════
log "⑨ 校验"
bash "$PROJECT_ROOT/scripts/verify_deb.sh" "$DEB_FILE" || die "校验没过 —— 别发这个包"

if [ "$KEEP_STAGING" = "0" ]; then
    # 不删, 挪到 /tmp —— 仓库里不留垃圾, 出问题也还能翻
    mv "$STAGING" "/tmp/aging-staging-$TS" 2>/dev/null || true
else
    log "   staging 保留在 $STAGING"
fi

log "✓ 打好了: $DEB_FILE"
log "  装到板子上: sudo dpkg -i $DEB_FILE"
