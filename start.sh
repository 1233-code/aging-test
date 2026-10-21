#!/bin/bash
# 工厂老化测试工具 - 启动入口
# 用法: bash start.sh [start|stop|restart|status]
# 默认 start
#
# 由桌面图标 (.desktop) 调用, Terminal=false, 故脚本应尽快返回
# 后台服务会持续跑 (setsid + nohup + disown 保证脱离)

set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PORT=${PORT:-8000}
LOG_FILE="$SCRIPT_DIR/logs/server.log"
PID_FILE="$SCRIPT_DIR/logs/server.pid"
URL="http://localhost:$PORT"
mkdir -p logs

log() {
    echo "[$(date +%H:%M:%S)] $*"
}

# 查端口被谁占, 返回 PID (空 = 没占)
port_holder_pid() {
    local port=$1
    # 优先 lsof, fallback ss
    if command -v lsof > /dev/null 2>&1; then
        lsof -t -i ":$port" 2>/dev/null | head -1
    else
        # ss -tlnp 在没 sudo 时不显示 PID, 用 fuser 兜底
        fuser "$port/tcp" 2>/dev/null | tr -d ' '
    fi
}

# 杀占端口的进程 (用来清理启动失败后的僵尸)
kill_port_holder() {
    local port=$1
    local holder
    holder=$(port_holder_pid "$port")
    if [ -n "$holder" ] && [ "$holder" != "$$" ] && [ "$holder" != "$(cat "$PID_FILE" 2>/dev/null)" ]; then
        log "⚠️ 端口 $port 被 PID $holder 占用, 杀掉"
        # 优先软杀
        kill "$holder" 2>/dev/null || true
        for _ in 1 2 3 4 5; do
            if ! kill -0 "$holder" 2>/dev/null; then
                break
            fi
            sleep 0.5
        done
        if kill -0 "$holder" 2>/dev/null; then
            kill -9 "$holder" 2>/dev/null || true
        fi
        sleep 0.5
    fi
}

# PID 文件的 PID 是否真的活着
pid_alive() {
    [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE" 2>/dev/null)" 2>/dev/null
}

# 服务真的能响应 (用 /api/health)
service_ready() {
    curl -s "$URL/api/health" 2>/dev/null | grep -q '"ok":\s*true'
}

start_service() {
    # 清理: PID 文件是僵尸 (进程死了) → 删
    if [ -f "$PID_FILE" ] && ! pid_alive; then
        log "⚠️ 清理僵尸 PID 文件: $(cat "$PID_FILE")"
        rm -f "$PID_FILE"
    fi

    # 清理: 端口 8000 被别的进程占 (但不是我们服务) → 杀
    local port_holder
    port_holder=$(port_holder_pid "$PORT")
    if [ -n "$port_holder" ] && [ "$port_holder" != "$(cat "$PID_FILE" 2>/dev/null)" ]; then
        log "⚠️ 端口 $PORT 被 PID $port_holder 占, 清理..."
        kill_port_holder "$PORT"
    fi

    if pid_alive; then
        log "服务已在运行 (PID: $(cat "$PID_FILE"))"
    else
        # 首次运行: 建 venv
        if [ ! -d ".venv" ]; then
            log "首次运行, 创建虚拟环境..."
            python3 -m venv .venv
            .venv/bin/pip install -q -r requirements.txt
        fi

        # §11.2: 服务以普通用户身份启动是**正常**的 —— 需 root 的系统调用
        # 由各测试项内部以 `sudo -n <命令>` 取得。真正的阻断条件只有一条:
        # sudo 不可用 / 未配 NOPASSWD。
        if command -v sudo > /dev/null 2>&1 && sudo -n true 2>/dev/null; then
            log "✓ sudo 免密可用 —— 需 root 的测试项会在内部以 sudo -n 执行"
        else
            log "✗ sudo 不可用或未配 NOPASSWD —— 会导致多项测试判 error"
            log "    前提 (§11.2): 普通用户需在 sudo 组 + /etc/sudoers 配 NOPASSWD"
            log "    自检: sudo -n true"
            log "    注: 不要用 'sudo bash start.sh' 整体提权启动, 那是 §11.2 已废除的做法"
        fi

        log "启动服务 (端口 $PORT)..."
        # setsid: 新会话, 完全脱离调用方
        # nohup: 忽略 SIGHUP
        # < /dev/null: 关闭 stdin
        setsid nohup .venv/bin/uvicorn server.main:app \
            --host 0.0.0.0 \
            --port "$PORT" \
            >> "$LOG_FILE" 2>&1 < /dev/null &
        disown
        echo $! > "$PID_FILE"

        # 等服务就绪 (最多 ~15s)
        for _ in {1..30}; do
            if service_ready; then
                log "服务已就绪 ✓"
                break
            fi
            sleep 0.5
        done

        if ! service_ready; then
            log "✗ 服务启动失败, 看 $LOG_FILE"
            tail -10 "$LOG_FILE" | sed 's/^/    /'
            # 启失败时清 PID, 避免下次误判
            rm -f "$PID_FILE"
            return 1
        fi
    fi

    # 启动浏览器 (后台, 不阻塞)
    if command -v xdg-open > /dev/null; then
        xdg-open "$URL" > /dev/null 2>&1 &
    elif command -v open > /dev/null; then
        open "$URL" > /dev/null 2>&1 &
    else
        log "请手动打开浏览器: $URL"
    fi

    # 依赖检查 (只显示汇总)
    if [ -x "scripts/check_deps.sh" ]; then
        echo ""
        bash scripts/check_deps.sh 2>&1 | grep -E "缺失|全部 .* 就位" | head -3
    fi
}

stop_service() {
    if pid_alive; then
        local PID
        PID=$(cat "$PID_FILE")
        log "停止服务 (PID: $PID)..."
        kill "$PID" 2>/dev/null || true
        for _ in {1..10}; do
            kill -0 "$PID" 2>/dev/null || break
            sleep 0.5
        done
        if kill -0 "$PID" 2>/dev/null; then
            kill -9 "$PID" 2>/dev/null || true
        fi
        rm -f "$PID_FILE"
        log "已停止"
    else
        rm -f "$PID_FILE"
        # 顺便清端口占的进程
        kill_port_holder "$PORT"
        log "服务未运行 (端口已清理)"
    fi
}

case "${1:-start}" in
    start)
        start_service
        ;;
    stop)
        stop_service
        ;;
    restart)
        stop_service
        start_service
        ;;
    status)
        if pid_alive; then
            echo "运行中 (PID: $(cat "$PID_FILE"))  URL: $URL"
        else
            echo "未运行"
        fi
        ;;
    *)
        echo "用法: $0 {start|stop|restart|status}"
        exit 1
        ;;
esac
