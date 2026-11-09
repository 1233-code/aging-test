#!/bin/bash
# 校验打好的 deb —— 出货前的最后一道闸
#
# 用法: bash scripts/verify_deb.sh <包路径>
#
# 查三件事:
#   ① **禁入**: 打包时绝不能混进去的东西 (产线数据、开发垃圾、panic 脚本);
#   ② **必入**: 少了就装完跑不起来的文件 (代码、测试项、离线仓、配置);
#   ③ **元数据**: Depends 对不对、chromium 有没有混进依赖。
#
# 为什么要有这道闸: §11.7 说过"缺一拒绝出货"。而"多带"同样是事故 ——
# 带进去一份 history.db, 别的板子装上就看到这台开发机的产线记录。
set -u

DEB="${1:-}"
if [ -z "$DEB" ] || [ ! -f "$DEB" ]; then
    echo "用法: bash $0 <包路径>" >&2
    exit 2
fi

FAIL=0
ok()   { echo "    ✓ $1"; }
bad()  { echo "    ✗ $1"; FAIL=1; }

WORK="$(mktemp -d /tmp/aging-verify-XXXXXX)"
LIST="$WORK/files.txt"

dpkg-deb -c "$DEB" > "$LIST" 2>/dev/null || { echo "读不了包: $DEB" >&2; exit 2; }
# 文件清单里每行: <权限> <属主/属组> <大小> <日期> <时间> <路径> [-> 链接目标]
#   ⚠️ 路径是**第 6 个**字段 —— 权限和属主是分开两列, 别数成第 5 个。
paths() { awk '{print $6}' "$LIST" | sed 's|^\./||' | sort -u; }

echo "==> ① 禁入清单 (这些混进包里就是事故)"
# 产线数据 —— 绝不能带走
for pat in "history.db" "reports/pdf/" "*.pdf" "logs/server.log" "logs/server.pid"; do
    hits="$(paths | grep -E "$pat" || true)"
    if [ -n "$hits" ]; then
        bad "混进了产线数据 [$pat]: $(echo "$hits" | head -3 | tr '\n' ' ')"
    else
        ok "没有产线数据 [$pat]"
    fi
done
# 开发垃圾
for pat in "__pycache__" "\.pyc$" "^usr/lib/aging-test/\.venv/" "^usr/lib/aging-test/docs/" \
           "^usr/lib/aging-test/tests/" "imageio_ffmpeg" "chromium"; do
    hits="$(paths | grep -E "$pat" || true)"
    if [ -n "$hits" ]; then
        bad "混进了开发垃圾 [$pat]: $(echo "$hits" | head -3 | tr '\n' ' ')"
    else
        ok "没有开发垃圾 [$pat]"
    fi
done
# 写 GPIO 节点的探针 —— 随包走等于给工人埋一颗 kernel panic
hits="$(paths | grep -E "probe_rp_gpio_write" || true)"
if [ -n "$hits" ]; then
    bad "混进了会写 GPIO 的探针 (会 kernel panic): $hits"
else
    ok "没有 GPIO 写探针"
fi

echo ""
echo "==> ② 必入清单 (少了装完就跑不起来)"
need() {   # $1=路径
    if paths | grep -qx "$1"; then ok "$1"; else bad "少了 $1"; fi
}
need "usr/lib/aging-test/server/main.py"
need "usr/lib/aging-test/server/config.py"
need "usr/lib/aging-test/server/db.py"
need "usr/lib/aging-test/server/report.py"
need "usr/lib/aging-test/server/runner.py"
need "usr/lib/aging-test/server/identity.py"
# ⚠️ 这两个是 2026-10-30 新加的 —— main.py / report.py 都 import 它,
# 少了它**服务根本起不来**(连报错都看不到, 直接 ImportError 退出)。
need "usr/lib/aging-test/server/boot_evidence.py"
# ⚠️ server/clock.py 同理 —— db.py / report.py / runner.py / main.py 都 import
# 它(§9.3.4 统一时间源), 少了它一样是 ImportError 起不来。
need "usr/lib/aging-test/server/clock.py"
need "usr/lib/aging-test/server/templates/report.html.j2"
need "usr/lib/aging-test/static/index.html"
need "usr/lib/aging-test/static/app.js"
need "usr/lib/aging-test/static/style.css"
need "usr/lib/aging-test/start.sh"
need "usr/lib/aging-test/config.yaml"
need "usr/lib/aging-test/requirements.txt"
need "usr/lib/aging-test/scripts/check_deps.sh"
need "etc/aging-test/config.yaml"
need "etc/default/aging-test"
need "usr/share/applications/aging-test.desktop"
need "usr/bin/aging-test"

echo "    -- 11 个测试项 --"
for t in cpu gpu wifi bluetooth usb ethernet hdmi fan tfcard memory emmc; do
    need "usr/lib/aging-test/server/tests/$t.py"
done
need "usr/lib/aging-test/server/tests/base.py"
need "usr/lib/aging-test/server/tests/registry.py"
need "usr/lib/aging-test/server/tests/gpu_benchmarks.py"
need "usr/lib/aging-test/server/tests/_tools.py"

echo ""
echo "==> ③ 离线仓 (装机时不许联网, 东西必须随包带走)"
# 工具 .deb: 17 个, 逐个点名
EXPECT_TOOLS="stress-ng fio memtester ethtool iw rfkill busybox util-linux iproute2 usbutils iputils-ping x11-xserver-utils xdg-utils fonts-wqy-zenhei glmark2-es2 glmark2-es2-wayland glmark2-data"
for t in $EXPECT_TOOLS; do
    if paths | grep -qE "^usr/lib/aging-test/deps/${t}_.*\.deb$"; then
        ok "deps: $t"
    else
        bad "deps 里没有 $t"
    fi
done

# 运行时共享库: 10 个 —— 工具是 dpkg-deb -x 解过去的, 不走 dpkg 依赖解析,
# 所以「工具 NEEDED 的那些 .so 在目标机上有没有」得自己保证。
# fio 缺 librbd.so.1 就是这么翻车的: eMMC / TF卡 / USB 三项全废。
EXPECT_RUNTIME="librbd1 librados2 libtirpc3 libboost-iostreams1.71.0 libgfapi0 libgfrpc0 libgfxdr0 libglusterfs0 librdmacm1 libatm1"
for t in $EXPECT_RUNTIME; do
    if paths | grep -qE "^usr/lib/aging-test/deps/${t}_.*\.deb$"; then
        ok "deps(运行库): $t"
    else
        bad "deps 里没有运行库 $t —— 工具装上也跑不起来"
    fi
done

# 核心库绝对不能随包带 —— 解过去会覆盖目标机的 libc/stdc++
for t in libc6 libstdc++6 libgcc-s1 zlib1g libcrypt1 dpkg; do
    if paths | grep -qE "^usr/lib/aging-test/deps/${t}_.*\.deb$"; then
        bad "deps 里居然有核心库 $t —— 解过去会覆盖目标机的核心库"
    else
        ok "deps 里没有核心库 $t"
    fi
done

n_deb=$(paths | grep -c "^usr/lib/aging-test/deps/.*\.deb$" || true)
echo "    (deps 里共 $n_deb 个 .deb)"

# wheel: 至少要能装齐 requirements.txt, 且**必须都是 cp38 能用的**
n_whl=$(paths | grep -c "^usr/lib/aging-test/wheels/.*\.whl$" || true)
if [ "$n_whl" -ge 7 ]; then
    ok "wheels: $n_whl 个"
else
    bad "wheels 只有 $n_whl 个 —— 装机的 python 依赖会缺"
fi
bad_tag="$(paths | grep "^usr/lib/aging-test/wheels/" | grep -E "cp3[9]|cp310|py3\.[9]|py3\.1[0-9]" || true)"
if [ -n "$bad_tag" ]; then
    bad "有给别的 python 版本编的 wheel: $bad_tag"
else
    ok "wheel 全是 cp38/abi3/纯 python 能用的"
fi
# 关键依赖必须在
for w in fastapi uvicorn pydantic PyYAML aiosqlite jinja2 python_multipart; do
    if paths | grep -qE "^usr/lib/aging-test/wheels/${w}-"; then
        ok "wheels: $w"
    else
        bad "wheels 里没有 $w"
    fi
done

echo ""
echo "==> ④ 元数据"
PKG="$(dpkg-deb -f "$DEB" Package 2>/dev/null)"
VER="$(dpkg-deb -f "$DEB" Version 2>/dev/null)"
ARCH="$(dpkg-deb -f "$DEB" Architecture 2>/dev/null)"
DEP="$(dpkg-deb -f "$DEB" Depends 2>/dev/null)"
echo "    包名 $PKG  版本 $VER  架构 $ARCH"
[ "$PKG" = "aging-test" ] && ok "包名对" || bad "包名不对: $PKG"
[ "$ARCH" = "arm64" ] && ok "架构对" || bad "架构不对: $ARCH"
case "$DEP" in
    *python3*) ok "Depends 里有 python3: $DEP" ;;
    *) bad "Depends 里没有 python3: $DEP" ;;
esac
case "$DEP" in
    *chromium*) bad "Depends 里出现了 chromium —— 出货镜像自带, 别动它" ;;
    *) ok "Depends 里没有 chromium ✓" ;;
esac
# 维护脚本
dpkg-deb -e "$DEB" "$WORK/DEBIAN" 2>/dev/null
for m in control conffiles postinst prerm postrm; do
    if [ -f "$WORK/DEBIAN/$m" ]; then ok "DEBIAN/$m"; else bad "少了 DEBIAN/$m"; fi
done
for m in postinst prerm postrm; do
    if [ -f "$WORK/DEBIAN/$m" ] && [ ! -x "$WORK/DEBIAN/$m" ]; then
        bad "DEBIAN/$m 没有可执行权限 —— 装机时不会跑"
    fi
done
if [ -f "$WORK/DEBIAN/conffiles" ]; then
    while IFS= read -r line; do
        [ -n "$line" ] || continue
        case "$line" in
            /etc/aging-test/*|/etc/default/aging-test*) ok "conffile: $line" ;;
            *) bad "conffile 里有不该管的路径: $line" ;;
        esac
    done < "$WORK/DEBIAN/conffiles"
fi

echo ""
echo "==> 包内容概览"
paths | awk -F/ '{ if ($1=="usr" && $2=="lib" && $3=="aging-test" && NF>4) print "usr/lib/aging-test/" $4; else print $1"/"$2 }' \
    | sort | uniq -c | sort -rn | head -20 | sed 's/^/    /'
echo "    总大小: $(du -h "$DEB" | cut -f1)  文件数: $(paths | wc -l)"

rm -rf "$WORK"
echo ""
if [ "$FAIL" = "1" ]; then
    echo "==> ✗ 校验没过 —— 别发这个包 (§11.7 缺一拒绝出货)"
    exit 1
fi
echo "==> ✓ 校验通过"
exit 0
