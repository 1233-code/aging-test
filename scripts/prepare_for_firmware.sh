#!/bin/bash
# 准备项目目录, 使其能直接打包进固件
#
# 在固件构建机 (build server) 上跑一次:
#   1. 验证关键文件就位
#   2. 打印一份固件打包清单
#
# 之后把整个项目目录打包到固件 image 中即可.
#
# 适合把整个项目烧入 eMMC / NAND / overlay 等只读分区.
# 注: GPU 走 glmark2-es2 全屏渲染 (§6.3), 不再需要任何视频素材.

set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_ROOT"

echo "==> 项目根目录: $PROJECT_ROOT"
echo ""

# Step 1: 验证最小可启动包
REQUIRED_FILES=(
    "start.sh"
    "openclaw-aging-test.desktop"
    "config.yaml"
    "requirements.txt"
    "scripts/install_deps.sh"
    "server/main.py"
    "server/runner.py"
    "static/index.html"
    "static/app.js"
)
MISSING=()
for f in "${REQUIRED_FILES[@]}"; do
    [ -f "$f" ] || MISSING+=("$f")
done
if [ ${#MISSING[@]} -gt 0 ]; then
    echo "✗ 缺失关键文件:"
    for m in "${MISSING[@]}"; do echo "    - $m"; done
    exit 1
fi

echo "==> 准备完成 ✓"
echo ""
echo "固件打包需要包含这些路径:"
echo "  - 整个项目目录 (因为 config + start.sh + desktop entry 是相对路径)"
echo ""
echo "固件首次启动后, 需做:"
echo "  1. 桌面图标已注册, 工人单击即可启动 (Terminal=false, 自动开浏览器)"
echo "  2. 服务跑后用户即可点桌面图标进入 UI"