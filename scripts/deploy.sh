#!/bin/bash
# 工厂部署脚本 - 在被测设备 (全志 A527) 上一次到位
# 用法: sudo bash scripts/deploy.sh
#
# 流程:
#   1. apt 装系统工具 (stress-ng, glmark2-es2-wayland, fio, ...)
#   2. 建 Python venv + 装 requirements
#   3. 装桌面图标到 ~/Desktop/ (gsettings 单击启动)
#   4. 健康检查 (Python 依赖导入 + 服务可达)

set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_ROOT"

if [ "$EUID" -ne 0 ]; then
    echo "请用 root 跑: sudo bash $0"
    exit 1
fi

log() { echo "==> $*"; }
warn() { echo "⚠  $*"; }
ok()   { echo "✓  $*"; }

# ─── 1. 系统依赖 ───
log "1/4 apt 装系统工具"
apt-get update -qq
apt-get install -y \
    python3 python3-pip python3-venv \
    stress-ng \
    glmark2-es2-wayland \
    fio \
    memtester \
    ethtool \
    iputils-ping \
    busybox \
    usbutils \
    iw rfkill wireless-tools wpasupplicant \
    network-manager \
    libgpiod-tools \
    x11-xserver-utils \
    chromium-browser \
    fonts-wqy-zenhei \
    alsa-utils \
    pciutils \
    curl \
    xdg-utils \
    2>&1 | tail -3
ok "apt 装好"

# ─── 2. Python venv + 依赖 ───
log "2/4 Python venv + pip"
if [ ! -d ".venv" ]; then
    python3 -m venv .venv
fi
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt
ok "Python 依赖就位"

# ─── 3. 桌面图标 ───
log "3/4 桌面图标"
DESKTOP_DIR=$(xdg-user-dir DESKTOP 2>/dev/null || echo "/root/Desktop")
mkdir -p "$DESKTOP_DIR"
cp openclaw-aging-test.desktop "$DESKTOP_DIR/"
chmod +x "$DESKTOP_DIR/openclaw-aging-test.desktop"

APPS_DIR="$HOME/.local/share/applications"
mkdir -p "$APPS_DIR"
cp openclaw-aging-test.desktop "$APPS_DIR/"
chmod +x "$APPS_DIR/openclaw-aging-test.desktop"

if command -v gio > /dev/null; then
    gio set "$DESKTOP_DIR/openclaw-aging-test.desktop" \
        "metadata::trusted" true 2>/dev/null || true
    ok "桌面图标已注册 ($DESKTOP_DIR/)"
else
    ok "桌面图标已复制 (无 gio, 可能需要手动确认)"
fi

# 应用菜单刷新
if command -v update-desktop-database > /dev/null; then
    update-desktop-database "$APPS_DIR" 2>/dev/null || true
fi

# 单击启动设置 (可选, gsettings 全局设置, 不只本图标)
if [ "${ENABLE_SINGLE_CLICK:-0}" = "1" ] && command -v gsettings > /dev/null; then
    gsettings set org.gnome.desktop.interface gtk-single-click true 2>/dev/null || true
    ok "已启用单击启动 (gsettings)"
fi

# ─── 4. 试用服务 (sanity check) ───
log "4/4 健康检查"
if .venv/bin/python -c "import fastapi, uvicorn, aiosqlite, yaml" 2>/dev/null; then
    ok "Python 依赖导入 OK"
else
    warn "Python 依赖导入失败, 请检查 .venv"
fi

echo ""
echo "=========================================="
echo "  部署完成 ✓"
echo "=========================================="
echo ""
echo "  操作:"
echo "    1. 桌面单击「工厂老化测试」图标 (或双击)"
echo "    2. 浏览器自动打开 http://localhost:8000"
echo "    3. 勾选测试项 → 点「开始测试」"
echo ""
echo "  CLI 控制:"
echo "    bash start.sh {start|stop|restart|status}"
echo ""
if [ "${ENABLE_SINGLE_CLICK:-0}" != "1" ]; then
    echo "  提示: 想启用单击启动, 重跑: sudo ENABLE_SINGLE_CLICK=1 bash scripts/deploy.sh"
fi
