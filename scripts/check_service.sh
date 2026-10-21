#!/bin/bash
echo "==> 1. PID 文件"
PID=$(cat logs/server.pid 2>/dev/null)
echo "   PID=$PID"
if [ -n "$PID" ] && kill -0 $PID 2>/dev/null; then
    echo "   ↑ 活着, etime = $(ps -o etime= -p $PID 2>/dev/null)"
    echo "   ↑ 命令: $(ps -o cmd= -p $PID 2>/dev/null)"
else
    echo "   ↑ 死了 / PID 文件无效"
fi
echo ""
echo "==> 2. uvicorn 进程实际文件"
PID2=$(ps -ef | grep -E "uvicorn server.main" | grep -v grep | awk '{print $2}' | head -1)
if [ -n "$PID2" ]; then
    echo "   进程 PID=$PID2, 命令:"
    ls -la /proc/$PID2/exe 2>/dev/null | head -1
    echo "   启动时间: $(ps -o lstart= -p $PID2 2>/dev/null)"
    echo ""
    # 查代码是哪个版本的: 查 static/app.js 的 mtime
    if [ -f static/app.js ]; then
        echo "   static/app.js 修改时间: $(stat -c '%y' static/app.js)"
    fi
fi
echo ""
echo "==> 3. /api/health 健康"
curl -s http://localhost:8000/api/health 2>&1
echo ""
echo ""
echo "==> 4. 拉个新代码签名 (验证 userStopped / killed_by_us 等关键修复点真的在)"
grep -c "userStopped" static/app.js
grep -c "killed_by_us" server/tests/gpu.py
grep -c "killed_by_us\|ran_full" server/tests/memory.py
grep -c "is_connect_fail\|connect_fail" server/tests/ethernet.py
echo ""
echo "   ↑ 全部非 0 = 修复点在, 重启就能加载"
