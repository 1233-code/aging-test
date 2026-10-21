#!/bin/bash
# 安装桌面图标 + 应用菜单项
# 用法:
#   bash scripts/install_desktop_icon.sh                # 仅安装图标
#   bash scripts/install_desktop_icon.sh --single-click # 同时启用 GNOME 单击启动
#
# 启用单击后, 桌面所有图标单击都会打开 (不只本项目),
# 没有 GNOME / 未启用 gsettings 时此参数无效.

set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
DESKTOP_FILE="$PROJECT_ROOT/openclaw-aging-test.desktop"
SINGLE_CLICK=false

for arg in "$@"; do
    case "$arg" in
        --single-click) SINGLE_CLICK=true ;;
        -h|--help)
            cat <<EOF
用法: bash scripts/install_desktop_icon.sh [--single-click]

选项:
  --single-click   启用 GNOME 单击启动 (gsettings gtk-single-click true)
EOF
            exit 0
            ;;
        *) ;;
    esac
done

if [ ! -f "$DESKTOP_FILE" ]; then
    echo "✗ 找不到 $DESKTOP_FILE"
    exit 1
fi

echo "==> 桌面图标安装"
echo "    源文件: $DESKTOP_FILE"
echo ""

# 1. 应用菜单 (~/.local/share/applications/)
APPS_DIR="$HOME/.local/share/applications"
mkdir -p "$APPS_DIR"
cp "$DESKTOP_FILE" "$APPS_DIR/"
chmod +x "$APPS_DIR/openclaw-aging-test.desktop"
echo "  ✓ 应用菜单: $APPS_DIR/openclaw-aging-test.desktop"

# 2. 桌面 (~/.Desktop 或其他; 通过 xdg-user-dir 查)
DESKTOP_DIR=$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")
if [ -d "$DESKTOP_DIR" ]; then
    cp "$DESKTOP_FILE" "$DESKTOP_DIR/"
    chmod +x "$DESKTOP_DIR/openclaw-aging-test.desktop"
    echo "  ✓ 桌面:    $DESKTOP_DIR/openclaw-aging-test.desktop"

    # 标记为可信任 (避免 GNOME 弹"未受信任的应用程序")
    if command -v gio > /dev/null; then
        gio set "$DESKTOP_DIR/openclaw-aging-test.desktop" \
            "metadata::trusted" true 2>/dev/null || true
        echo "  ✓ 桌面图标已标记可信任 (gio)"
    fi
else
    echo "  - 桌面目录不存在, 跳过 ($DESKTOP_DIR)"
fi

# 3. 刷新应用菜单缓存
if command -v update-desktop-database > /dev/null; then
    update-desktop-database "$APPS_DIR" 2>/dev/null || true
fi

# 4. 单击启动设置 (GNOME 全局设置)
if $SINGLE_CLICK; then
    if command -v gsettings > /dev/null && [ -n "$XDG_CURRENT_DESKTOP" ]; then
        gsettings set org.gnome.desktop.interface gtk-single-click true 2>/dev/null && \
            echo "  ✓ 已启用单击启动 (gsettings)"
    else
        echo "  - 单击启动需 GNOME 环境 + gsettings, 当前不可用"
    fi
fi

echo ""
echo "==> 安装完成 ✓"
echo ""
echo "  操作:"
echo "    桌面单击/双击 「工厂老化测试」图标 → 启动"
echo "    CLI 控制: bash start.sh {start|stop|restart|status}"
echo ""

if ! $SINGLE_CLICK; then
    echo "  提示: 想启用单击启动 (默认是双击), 可:"
    echo "    1. 重跑本脚本并加 --single-click 参数"
    echo "    2. 或手动执行: gsettings set org.gnome.desktop.interface gtk-single-click true"
    echo "       (注: 此设置会影响所有桌面文件, 不只本图标)"
fi
