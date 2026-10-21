#!/bin/bash
# 强制清理 + 启动 (解决 PID 文件失活 + 端口占着起不来的状态)
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_ROOT"

echo "==> 1. 找所有 uvicorn 进程"
PIDS=$(ps -ef | grep -E "uvicorn server.main" | grep -v grep | awk '{print $2}' || true)
echo "   uvicorn PIDs: $PIDS"

echo ""
echo "==> 2. 强杀所有 uvicorn"
for p in $PIDS; do
    sudo kill -9 "$p" 2>/dev/null && echo "   killed $p" || echo "   kill $p failed"
done
sleep 1

echo ""
echo "==> 3. 强杀占 8000 端口的"
PIDS2=$(sudo ss -tlnp 2>/dev/null | grep ':8000 ' | grep -oP 'pid=\K[0-9]+' || true)
for p in $PIDS2; do
    sudo kill -9 "$p" 2>/dev/null && echo "   killed port-holder $p" || true
done
sleep 1

echo ""
echo "==> 4. 清 PID 文件 + 验证 8000 没人占"
sudo rm -f logs/server.pid
PIDS3=$(sudo ss -tlnp 2>/dev/null | grep ':8000 ' | grep -oP 'pid=\K[0-9]+' || true)
if [ -n "$PIDS3" ]; then
    echo "   ✗ 8000 还被占: $PIDS3"
    sudo ps -ef | grep -E "$PIDS3" | head -3
    exit 1
fi
echo "   ✓ 8000 端口干净"

echo ""
echo "==> 5. 启动新服务"
sudo bash start.sh start

echo ""
echo "==> 6. 验证"
sleep 2
curl -s http://localhost:8000/api/health
echo ""
