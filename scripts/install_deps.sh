#!/bin/bash
# 安装所有系统依赖 - 在被测设备 (全志 A527) 上运行
# 用法: sudo bash scripts/install_deps.sh

set -e

if [ "$EUID" -ne 0 ]; then
    echo "请用 root 权限运行: sudo bash $0"
    exit 1
fi

# 确保 sbin 在 PATH (覆盖 sudo 默认 secure_path)
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

# ─── 准备 apt 仓库 ───
echo "=== 准备 apt 仓库 ==="

# 1. 如果没 add-apt-repository, 先装 software-properties-common
if ! command -v add-apt-repository > /dev/null 2>&1; then
    echo "  ✗ 缺 add-apt-repository, 装 software-properties-common..."
    if ! apt-get install -y software-properties-common > /tmp/apt-ppr.log 2>&1; then
        echo "  ✗ software-properties-common 装失败, 手动跑: sudo apt install software-properties-common"
        cat /tmp/apt-ppr.log | tail -3 | sed 's/^/      /'
    fi
fi

# 2. 检查 universe 是否启用 (无 universe 找不到 libgpiod-tools 等)
HAS_UNIVERSE=$(grep -lE "(^| )universe( |$)" /etc/apt/sources.list /etc/apt/sources.list.d/*.list 2>/dev/null | head -1)
if [ -z "$HAS_UNIVERSE" ]; then
    echo "  universe 未启用, 正在启用..."
    if command -v add-apt-repository > /dev/null 2>&1; then
        if add-apt-repository -y universe 2>&1 | tail -2; then
            echo "  ✓ universe 启用"
        else
            echo "  ✗ 启用 universe 失败, 手动: sudo add-apt-repository universe"
        fi
    else
        echo "  ✗ add-apt-repository 仍缺失, 手动操作:"
        echo "    1. sudo apt install software-properties-common"
        echo "    2. sudo add-apt-repository universe"
        echo "    3. sudo apt update"
    fi
else
    echo "  ✓ universe 已启用"
fi

apt-get update 2>&1 | tail -3

# ─── 补全 focal main 仓库 (idempotent) ───
# 问题现象: 很多 A527 镜像只装了 focal-security, 找不到 focal main 的包
# 解决: 加一个 TUNA mirror 的 focal main + universe 源
SOURCES_FILE="/etc/apt/sources.list.d/ubuntu-focal-main.list"
if [ ! -f "$SOURCES_FILE" ]; then
    echo "  ⚠ /etc/apt/sources.list 缺 focal main (只看到 security), 补上 TUNA main 源..."
    cat > "$SOURCES_FILE" <<'SOURCES'
# Added by install_deps.sh — 补充 focal main 仓库 (装 libgpiod-tools 等)
deb http://mirrors.tuna.tsinghua.edu.cn/ubuntu-ports/ focal main universe
deb http://mirrors.tuna.tsinghua.edu.cn/ubuntu-ports/ focal-updates main universe
SOURCES
    apt-get update 2>&1 | tail -3
else
    echo "  ✓ focal main 源已就位"
fi

echo ""
echo "=== 安装老化测试工具的系统依赖 ==="

# 关键包
CRITICAL_PKGS=(
    "stress-ng"
    "glmark2-es2"
    "glmark2-es2-wayland"
    "fio"
    "memtester"
    "ethtool"
    "iputils-ping"
    "busybox"
    "iw"
    "rfkill"
    "x11-xserver-utils"
    "libgpiod-tools"
    "libgpiod2-tools"
)

# 基础包
COMMON_PKGS=(
    "python3" "python3-pip" "python3-venv" "python3-dev"
    "iproute2" "util-linux"
    "curl" "xdg-utils"
    "pciutils" "usbutils" "alsa-utils"
)

# 可选
OPTIONAL_PKGS=(
    "wireless-tools"
    "wpasupplicant"
    "network-manager"
)

declare -A CATEGORY=(
    [stress-ng]="CPU 压力"
    [glmark2-es2]="GPU 渲染 (X11 变体, 需 DISPLAY)"
    [glmark2-es2-wayland]="GPU 渲染 (Wayland 变体, 需 WAYLAND_DISPLAY)"
    [fio]="存储 读写"
    [memtester]="内存"
    [ethtool]="网口 链路"
    [iputils-ping]="网口 ping"
    [busybox]="网口 DHCP (udhcpc)"
    [iw]="WiFi 扫描"
    [rfkill]="WiFi 射频屏蔽检查"
    [x11-xserver-utils]="HDMI 读当前实际输出 (xrandr)"
    [libgpiod-tools]="GPIO 工具 v1 (gpioget, gpioinfo)"
    [libgpiod2-tools]="GPIO 工具 v2 (Ubuntu 20.04 universe)"
)

# 装关键包 - 真正检查 apt 返回码
echo ""
echo "── 装关键包 ──"
FAIL_CRIT=()
for pkg in "${CRITICAL_PKGS[@]}"; do
    if command -v "${pkg%%:*}" > /dev/null 2>&1; then
        echo "  ✓ $pkg [已装]"
        continue
    fi
    if apt-get install -y "$pkg" > /tmp/apt-install.log 2>&1; then
        echo "  ✓ $pkg"
    else
        echo "  ✗ $pkg 装失败"
        tail -2 /tmp/apt-install.log | sed 's/^/      /'
        FAIL_CRIT+=("$pkg")
    fi
done

# 基础包
echo ""
echo "── 装基础包 ──"
for pkg in "${COMMON_PKGS[@]}"; do
    if command -v "${pkg%%:*}" > /dev/null 2>&1; then
        echo "  ✓ $pkg [已装]"
        continue
    fi
    if apt-get install -y "$pkg" > /tmp/apt-install.log 2>&1; then
        echo "  ✓ $pkg"
    else
        echo "  ⚠ $pkg 装失败 (非阻塞)"
    fi
done

# 可选
echo ""
echo "── 装可选包 ──"
for pkg in "${OPTIONAL_PKGS[@]}"; do
    if apt-get install -y "$pkg" > /tmp/apt-install.log 2>&1; then
        echo "  ✓ $pkg"
    else
        echo "  ⚠ $pkg 装失败 (非阻塞)"
    fi
done

# Python 依赖
echo ""
echo "=== Python 依赖 ==="
cd "$(dirname "$0")/.."
if [ ! -d ".venv" ]; then
    python3 -m venv .venv
fi
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

# 验证
echo ""
echo "=== 验证 ==="
bash "$(dirname "$0")/check_deps.sh" || true

echo ""
if [ ${#FAIL_CRIT[@]} -gt 0 ]; then
    echo "=== ✗ 关键包缺失 ==="
    for pkg in "${FAIL_CRIT[@]}"; do
        echo "    $pkg"
    done
    echo ""
    echo "  排查步骤:"
    echo "    1. 网络: ping mirrors.tuna.tsinghua.edu.cn"
    echo "    2. universe: cat /etc/apt/sources.list | grep universe"
    echo "    3. apt-cache search $FAIL_CRIT  (看看仓库里有没有)"
    echo "    4. 手动: sudo apt install ${FAIL_CRIT[*]}"
    exit 1
fi
echo "=== ✓ 全部装好 ==="
echo "  桌面图标: cp openclaw-aging-test.desktop ~/Desktop/"
echo "  启动: bash start.sh"
