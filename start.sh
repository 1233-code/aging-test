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

# Debian 惯例: /etc/default/<包名> 放默认值 (打包安装时由 postinst 写进去)
#   AGING_DATA_DIR=/var/lib/aging-test
# 开发机上没有这个文件, 一切照旧。
if [ -f /etc/default/aging-test ]; then
    . /etc/default/aging-test
fi

# ── 「U 盘版应用」自举 ─────────────────────────────────────────────
#
# 镜像由 scripts/build_usb_image.sh 打的, 根目录下有个 AGING_USB_MODE 标记。
# 有这个标记 ⇒ 现在是**整个应用跑在 U 盘上**:
#   · **数据根 = U 盘** ⇒ history.db / 报告 PDF / 服务日志全落在盘上,
#     拔盘即走, 设备上不留数据;
#   · **工具(fio / glmark2)也在盘上** ⇒ 把工具目录指给应用, 它才找得到
#     (系统里没装这俩);
#   · 配置文件也用盘上这份(设备上那份是 deb 装的, 可能更旧)。
#
# 设备版(deb 装)根目录**没有**这个标记 ⇒ 下面这几行一个字都不生效,
# 行为跟以前一模一样。
USB_MODE=0
if [ -f "$SCRIPT_DIR/AGING_USB_MODE" ]; then
    USB_MODE=1
    export AGING_DATA_DIR="$SCRIPT_DIR"
    [ -f "$SCRIPT_DIR/config.yaml" ] && export AGING_CONFIG="$SCRIPT_DIR/config.yaml"
    [ -d "$SCRIPT_DIR/tools/bin" ] && export AGING_TOOL_DIR="$SCRIPT_DIR/tools"
fi

PORT=${PORT:-8000}
# **数据根**: history.db / 报告 PDF / 服务日志都落这儿。
# 开发时 = 代码根 (SCRIPT_DIR); 打包后 = /var/lib/aging-test
# (代码归 root 所有, 跑测试的工人是普通用户, 写不进去);
# U 盘版 = U 盘(上面的自举段已经把它指好了)。
DATA_DIR="${AGING_DATA_DIR:-$SCRIPT_DIR}"
export AGING_DATA_DIR="$DATA_DIR"
LOG_FILE="$DATA_DIR/logs/server.log"
PID_FILE="$DATA_DIR/logs/server.pid"
# ⚠️ **用 127.0.0.1, 不用 localhost** —— 2026-11-05 新设备实测:
#   有的机器 `localhost` 先解析成 ::1, 而服务只听 0.0.0.0(IPv4)
#   ⇒ 浏览器和健康检查都可能连不上一个**明明在跑**的服务。
URL="http://127.0.0.1:$PORT"
mkdir -p "$DATA_DIR/logs"

# **虚拟环境**: 设备版是首次运行时现建(.venv); U 盘版的镜像里**已经带着
# 建好的** venv(离线装的, 见 §12.2)⇒ 别再去建一个。
VENV_DIR="$SCRIPT_DIR/.venv"
if [ ! -d "$VENV_DIR" ] && [ -x "$SCRIPT_DIR/venv/bin/python" ]; then
    VENV_DIR="$SCRIPT_DIR/venv"
fi

log() {
    echo "[$(date +%H:%M:%S)] $*"
}

# ⚠️ **失败必须让人看得见** —— 桌面双击没有终端, log() 打的那几行全进了
#   黑洞 ⇒ 工人只看到「点了没反应」(2026-11-05 实测: 盘上的 venv 解释器
#   路径写死了构建目录, 换台设备就起不来, 而失败是**静默**的)。
#   ⇒ 落一份到盘上(拔了盘也跟着走, 换台设备还能查) + 弹个桌面通知。
fail_visible() {
    local msg="$1"
    local detail="${2:-}"
    local f="$DATA_DIR/logs/启动失败.txt"
    mkdir -p "$DATA_DIR/logs" 2>/dev/null
    {
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] $msg"
        [ -n "$detail" ] && echo "$detail"
        echo "---"
    } >> "$f"
    if command -v notify-send >/dev/null 2>&1; then
        notify-send '老化测试' '启动失败, 详见 logs/启动失败.txt' >/dev/null 2>&1 &
    elif command -v zenity >/dev/null 2>&1; then
        # 加 timeout: 万一没人点, 也不能把 start.sh 永久挂住
        ( timeout 10 zenity --error --text='老化测试启动失败, 详见 logs/启动失败.txt' \
            >/dev/null 2>&1 & )
    fi
    return 0
}

# 查端口被谁占, 返回 PID (空 = 没占)
port_holder_pid() {
    local port=$1
    # 优先 lsof, fallback ss
    if command -v lsof > /dev/null 2>&1; then
        # **必须带 -sTCP:LISTEN**: `-i :8000` 匹配的是该端口上的**所有** TCP
        # 连接 (监听的服务端 + 连进来的客户端), 实测会同时吐出浏览器的
        # NetworkService (36908) 和 uvicorn (36940), head -1 随机挑一个 ——
        # 结果 restart/stop 杀的是浏览器, 服务还活着占着端口。
        # 注意 `-iTCP:"$port"` 中间**不能有空格**: `-iTCP "8000"` 会被 lsof
        # 当成两个参数, 实测一条都匹配不到 (静默失效, 比原来更糟)。
        lsof -t -iTCP:"$port" -sTCP:LISTEN 2>/dev/null | head -1
    else
        # ss -tlnp 在没 sudo 时不显示 PID, 用 fuser 兜底
        fuser "$port/tcp" 2>/dev/null | tr -d ' '
    fi
}

# 日志里长出了「空洞」(一串 NUL 字节)⇒ 把整份日志修好, 免得 grep 对它静默罢工。
#
# ⚠️ **为什么要有这道工序**(2026-11-11 产线盘上实测):
#   `logs/server.log` 里有一段 **956 个 NUL 字节**(21 KB 的空洞)。
#   ⇒ **grep 把整个文件当成二进制** ⇒ `grep 什么 logs/server.log`
#      **一个字都不输出**(连「没有匹配」都不说!)
#   ⇒ 而部署作业手册**教工人去 grep 这个日志** ⇒ 工人得到「静默的空」
#      ⇒ 合理反应是「日志里没有 ⇒ 这条路不通」⇒ **去追一个不存在的故障**
#      (84 条 'WebSocket /ws' 就在那儿, 只是 grep 不肯说)
#   ⇒ 2026-11-11 那次停产事故的排查也被它拖了后腿。
#
# 空洞是怎么来的: 现在的写方**全都是追加写**(`>>` / `open(..., "a")`)
#   ⇒ 追加写不会留空洞 ⇒ 那段空洞是**老账**(早期那轮「盘换了挂载点、
#   日志被拷来拷去」留下的)。⚠️ **成因没钉死** —— 所以这儿的做法是
#   **治标也治本**: 每次启动都把洞补上 ⇒ 活着的日志永远是能 grep 的。
#
# 做法: **原件留一份**(不删!), 再把 NUL 换成换行 ⇒ 内容一行不少, 只是
#   空洞那几行变成空行 ⇒ grep 恢复正常。
repair_log_holes() {
    [ -f "$LOG_FILE" ] || return 0
    local raw clean
    raw=$(wc -c < "$LOG_FILE" 2>/dev/null || echo 0)
    # ⚠️ **不能把 NUL 放进 grep 的参数里**(参数里放不下 NUL)⇒
    #   改用「去掉 NUL 之后还剩多少字节」来判。
    clean=$(tr -d '\000' < "$LOG_FILE" 2>/dev/null | wc -c)
    if [ "$raw" = "$clean" ]; then
        return 0                      # 没有空洞 ⇒ 什么都不用做
    fi
    local keep="$LOG_FILE.holes-$(date +%Y%m%d-%H%M%S)"
    cp "$LOG_FILE" "$keep" 2>/dev/null || keep=""
    if tr '\000' '\n' < "$LOG_FILE" > "$LOG_FILE.fix" 2>/dev/null; then
        if mv "$LOG_FILE.fix" "$LOG_FILE" 2>/dev/null; then
            log "⚠ 日志里有空洞(NUL 字节)⇒ 已修好(grep 能用了)"
            # ⚠️ **别写 `[ -n "$keep" ] && log ...`** —— 那个链在 keep 为空时
            #   返回非零 ⇒ `set -e` 会**把整个脚本带走** ⇒ 恰恰在"修日志"的
            #   时候把服务起不来 ⇒ 一律用 if。
            if [ -n "$keep" ]; then
                log "  原件留着: $(basename "$keep")"
            fi
        fi
    else
        rm -f "$LOG_FILE.fix" 2>/dev/null || true
        log "⚠ 日志里有空洞, 但没能修好(原文件没动)"
    fi
    return 0
}

# 这个进程**是不是我们自己的服务**(uvicorn 起的那种)?
#
# ⚠️ **2026-11-11: 为什么要加这道闸** —— 原来 `kill_port_holder` 是
#   「端口上是谁就杀谁」⇒ **会杀掉任何一个占着端口的进程**。
#   ⇒ 一次回归(`tests/test_usb_app_mode.py` 拿临时目录里那份 start.sh
#      跑 `start`/`stop`, 而没改 PORT ⇒ 默认 8000)在 `stop` 时把
#      **产线上正在跑的服务** SIGTERM 掉了 —— 产线当场断服务, 而且
#      **日志里查不到是谁干的**(干活的正是测试自己)。
#   ⇒ 现在只杀**像我们自己这个服务**的进程; 别的东西占着端口 ⇒
#      **不杀**, 如实报出来让人看。
#
# 认得出来的两种起法:
#   · `python3 -m uvicorn server.main:app ...`   ← 现在用的
#   · `venv/bin/uvicorn server.main:app ...`      ← 老写法
# ⇒ cmdline 里带 "uvicorn" 或 "server.main" 都算。
is_our_server() {
    local pid=$1 cl
    [ -r "/proc/$pid/cmdline" ] || return 1
    cl="$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || true)"
    case "$cl" in
        *uvicorn*|*server.main*) return 0 ;;
    esac
    return 1
}

# 杀占端口的进程 (用来清理启动失败后的僵尸)
kill_port_holder() {
    local port=$1
    local holder
    holder=$(port_holder_pid "$port")
    if [ -n "$holder" ] && [ "$holder" != "$$" ] && [ "$holder" != "$(cat "$PID_FILE" 2>/dev/null)" ]; then
        if ! is_our_server "$holder"; then
            # ⚠️ **不是我们的服务 ⇒ 不杀** —— 宁可启动失败, 也不误杀别人的进程
            log "✗ 端口 $port 被 PID $holder 占用, 但**不是本服务**, 不动它"
            log "  (cmdline: $(tr '\0' ' ' < "/proc/$holder/cmdline" 2>/dev/null | cut -c1-120))"
            log "  ⇒ 请先处理占用者(或者换个 PORT), 再起服务。"
            return 1
        fi
        log "⚠️ 端口 $port 被 PID $holder 占用(是本服务的旧进程), 杀掉"
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
    return 0
}

# PID 文件的 PID 是否真的活着
pid_alive() {
    [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE" 2>/dev/null)" 2>/dev/null
}

# 服务真的能响应 (用 /api/health)
#
# ⚠️ **不能拿 curl 当唯一判据** —— 2026-11-05 在**新设备**上实测踩到:
#   服务**明明起来了**(uvicorn 日志有 "Uvicorn running on ..."), 就绪检查
#   却连着 15 秒都说「没起来」⇒ 脚本 `return 1` ⇒ **打开浏览器那段根本
#   执行不到** ⇒ 工人看到的正是「点了没反应」+ 一个「启动失败」的通知。
#   毛毛不在服务, 在**检查这一侧**: 新设备上 curl 要么没装、要么被
#   `http_proxy` 劫持(代理把 localhost 也一起带走)、要么 `localhost`
#   解析到 ::1 而服务只听 0.0.0.0 ⇒ 证据: 那台设备的服务日志里
#   **一条 `GET /api/health` 都没进来**。
# ⇒ 改成三层判据, 从准到糙:
#   ① **python 探针**(优先用盘上 venv 自带的那个): 不依赖 curl、显式禁用
#      代理(`ProxyHandler({})`)、只打 `127.0.0.1` ⇒ 上面三个坑一个都不踩;
#   ② curl(有就用, 但加 `--noproxy '*'`);
#   ③ **端口在听就算起来了** —— 服务刚起来还没应答时也不误判成失败。
service_ready() {
    _health_probe && return 0
    if command -v curl > /dev/null 2>&1; then
        curl -s --noproxy '*' --max-time 3 "$URL/api/health" 2>/dev/null \
            | grep -q '"ok":true' && return 0
    fi
    return 1
}

# 服务**起来了**(应答了, 或者端口已经在听) —— 启动后等就绪用这个:
#   刚起来的那一两秒里健康接口可能还没应答, 但端口已经在听 ⇒ 也算起来了。
# ⚠️ **不用它判「服务健不健康」** —— 那件事必须看 /api/health
#   (见下面「在线判定」那段注释: 端口在听但进程卡死的坑)。
service_up() {
    service_ready && return 0
    port_listening && return 0
    return 1
}

# python 探针: 打 /api/health —— **禁用代理** + **只打 127.0.0.1**
#
# ⚠️ **优先用系统的 python3** —— 盘上 venv 的 python3 通常就是
#   `/usr/bin/python3` 的软链(实测: sys.prefix 跟着盘走 ✓), 而有些环境里
#   `venv/bin/python3` 未必是个正经解释器。系统的那个才是稳的。
_health_probe() {
    local py
    for py in "$(command -v python3 2>/dev/null)" "$VENV_DIR/bin/python3"; do
        [ -n "$py" ] && [ -x "$py" ] || continue
        "$py" - "$PORT" <<'PY' >/dev/null 2>&1
import sys, urllib.request
op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
try:
    body = op.open("http://127.0.0.1:%s/api/health" % sys.argv[1], timeout=3).read(65536)
except Exception:
    sys.exit(1)
sys.exit(0 if b'"ok"' in body and b"true" in body else 1)
PY
        [ $? -eq 0 ] && return 0
    done
    return 1
}

# 端口有没有人在听 —— **不依赖 lsof/fuser**(新设备上不一定装了)
# /proc/net/tcp 的 st 列: 0A = LISTEN; 本地地址是 "主机序:端口" 十六进制
port_listening() {
    local hex
    hex=$(printf '%04X' "$PORT" 2>/dev/null)
    [ -n "$hex" ] || return 1
    cat /proc/net/tcp /proc/net/tcp6 2>/dev/null | awk -v p="$hex" '
        $4 == "0A" { n = split($2, a, ":"); if (toupper(a[n]) == p) { found = 1 } }
        END { exit(found ? 0 : 1) }'
}

start_service() {
    if [ "$USB_MODE" = "1" ]; then
        log "▶ U 盘模式: 程序 / 数据 / 工具全在 U 盘上"
        log "    数据根: $DATA_DIR"
        [ -n "${AGING_TOOL_DIR:-}" ] && log "    随包工具: $AGING_TOOL_DIR/bin"
        log "    拔掉 U 盘 = 程序和历史记录一起走, 设备上不留东西"
    fi

    # 清理: PID 文件是僵尸 (进程死了) → 删
    if [ -f "$PID_FILE" ] && ! pid_alive; then
        log "⚠️ 清理僵尸 PID 文件: $(cat "$PID_FILE")"
        rm -f "$PID_FILE"
    fi

    # 清理: 端口 8000 被**我们自己的旧服务**占 (PID 文件丢了/盘换了) → 杀。
    # ⚠️ **不是我们的 ⇒ 不杀**(见 is_our_server) —— 宁可起不来, 也不误杀。
    local port_holder
    port_holder=$(port_holder_pid "$PORT")
    if [ -n "$port_holder" ] && [ "$port_holder" != "$(cat "$PID_FILE" 2>/dev/null)" ]; then
        log "⚠️ 端口 $PORT 被 PID $port_holder 占, 清理..."
        if ! kill_port_holder "$PORT"; then
            fail_visible "端口 $PORT 被别的程序占用 (PID $port_holder), 本服务不乱杀别人的进程。
请先处理占用者, 或者用 PORT=<别的端口> 起。"
            return 1
        fi
    fi

    # ── 在线判定: 以 /api/health 为准, **不能只看 pid_alive** ──
    # 被 SIGTERM 后的 uvicorn 会永久卡在 "Waiting for background tasks to complete"
    # (测试子进程是 sudo 起的 root 进程, 普通用户发不了信号 → 后台任务永不结束),
    # 此时 `kill -0` 仍然成功, 但监听端口已关, 对外完全不服务。
    #
    # 按 pid_alive 判定的后果实测: 这里判成"已在运行" → 整段启动被跳过 →
    # 直接开浏览器 → 浏览器连不上, 显示的是**缓存的旧页面**, 会弹出上一次
    # 跑测残留的「TF 卡未插入」弹窗。现场极难定位。
    if service_ready; then
        log "服务已在运行 (PID: $(cat "$PID_FILE" 2>/dev/null || echo 未知))"
    else
        # 首次运行: 建 venv
        # ⚠️ 打包安装后**一律不许联网装包**(§11.7) —— 随包带了 wheels 就离线装,
        # 免得"装个包要等 5 分钟"最后还因为没网失败。
        if [ ! -d "$VENV_DIR" ]; then
            log "首次运行, 创建虚拟环境..."
            python3 -m venv "$VENV_DIR"
            if [ -d wheels ]; then
                "$VENV_DIR"/bin/python3 -m pip install -q --no-index --find-links=wheels -r requirements.txt
            else
                "$VENV_DIR"/bin/python3 -m pip install -q -r requirements.txt
            fi
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
        # ⚠️ **必须用 `python3 -m uvicorn`, 不能用 `venv/bin/uvicorn`** ——
        #   venv 里那些 console script(uvicorn/pip/fastapi…)的**第一行写死了
        #   构建时的绝对路径**(镜像是在构建目录里建的, 形如
        #   `#!/tmp/aging-usb-image/venv/bin/python3`)⇒ 盘被搬到**别的设备**上
        #   那个路径根本不存在 ⇒ 内核直接报 "bad interpreter" ⇒ 服务起不来。
        #   而桌面双击没有终端 ⇒ 工人只看到「点了没反应」。
        #   `python3 -m uvicorn` 走的是 venv 自己的 bin/python3(它靠**同目录**
        #   的 pyvenv.cfg 定位 venv, 不解析符号链接 ⇒ 盘搬哪儿都能用)。
        # setsid: 新会话, 完全脱离调用方
        # nohup: 忽略 SIGHUP
        # < /dev/null: 关闭 stdin
        #
        # ⚠️ **先把日志里的「空洞」修好, 再让服务往里写** ——
        #   见上面 repair_log_holes() 的说明(空洞会让 grep 对整个文件
        #   静默无输出 ⇒ 工人按手册查日志 ⇒ 「查不到」⇒ 追不存在的故障)。
        repair_log_holes
        setsid nohup "$VENV_DIR"/bin/python3 -m uvicorn server.main:app \
            --host 0.0.0.0 \
            --port "$PORT" \
            >> "$LOG_FILE" 2>&1 < /dev/null &
        disown
        echo $! > "$PID_FILE"

        # 等服务就绪 (最多 ~30s —— 2026-11-05 从 15s 放宽: 新设备上
        # 慢一点就误判成失败, 而误判的直接后果是**浏览器不打开**)
        for _ in {1..60}; do
            if service_up; then
                log "服务已就绪 ✓"
                break
            fi
            sleep 0.5
        done

        if service_up; then
            # ⚠️ **端口在听但健康接口不答应** —— 服务是在跑的, 只是健康检查
            #   没打通(设备上 curl 缺失 / 被 http_proxy 劫持这类)。这种**不
            #   算失败**: 报失败 = 退出码 1 = 启动器弹「启动失败」= 工人以为
            #   没起来, 而服务其实好好的 ⇒ 该做的是把浏览器打开让他看。
            service_ready || log "⚠ 端口 $PORT 在听, 但 /api/health 没应答 ——\
浏览器打开后若显示不出来, 看 $LOG_FILE"
        else
            log "✗ 服务启动失败, 看 $LOG_FILE"
            tail -10 "$LOG_FILE" | sed 's/^/    /'
            # 启失败时清 PID, 避免下次误判
            rm -f "$PID_FILE"
            # ⚠️ **失败必须让人看得见** —— 桌面双击没有终端, 上面那几行
            #   log() 全进了黑洞 ⇒ 工人只看到「点了没反应」。
            #   落一份到盘上 + 弹个通知, 换台设备照样能查。
            fail_visible "服务启动失败 (端口 $PORT)。
细节看 $LOG_FILE 的最后 10 行, 上面已经打过。" "$LOG_FILE"
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
        # 顺便清端口占的进程 —— ⚠️ **只清我们自己的**(见 is_our_server)
        if kill_port_holder "$PORT"; then
            log "服务未运行 (端口已清理)"
        else
            log "服务未运行 (端口上的东西**不是本服务**, 没动它)"
        fi
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
