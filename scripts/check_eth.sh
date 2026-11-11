#!/bin/bash
# 网口诊断 —— 与 docs/需求设计.md §6.6 的三步流程对齐
# 只看状态, 不改任何配置。用于工位排障: 跑一遍就知道卡在哪一步。
#
#   ① 链路   ethtool <iface> → Link detected: yes/no   (纯 PHY, 零网络依赖)
#   ② 取 IP  udhcpc -n 拿 lease; 拿不到 → 静态兜底
#   ③ 出网   三级降级 ping: 网关 → DNS → 公网
#
# 用法: bash scripts/check_eth.sh [iface]   (默认 eth0)

IFACE="${1:-eth0}"

# ⚠️ **2026-11-10**: 补救办法**按形态分开说** —— U 盘版的盘上**没有**
#   `scripts/install_deps.sh`(`scripts/build_usb_image.sh` 只往盘上拷
#   「运行时 + 诊断」那几类脚本, 它属于「打包/部署类」,**不带**)⇒ 照着
#   跑只会得到「No such file or directory」⇒ **补救办法是死的**。U 盘版缺
#   工具 = **这版出厂镜像没带齐**(而且产线网络取不到公网, 本来也不该在
#   板子上装)。
# ⚠️ 这个判据**跟 Python 侧是同一个**(`server/tests/_tools.py:usb_mode()`:
#   环境变量 `AGING_TOOL_DIR` 在、**或** 盘根目录有 `AGING_USB_MODE`
#   标记 ⇒ 取或)—— 以前两边各认各的 ⇒ 能同时对不上。
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
# ⚠️ 判据跟 Python 侧 `server/tests/_tools.py:usb_mode()` **完全一样**:
#   「环境变量 `AGING_TOOL_DIR` 在」**或**「盘根目录有 `AGING_USB_MODE` 标记」
#   ⇒ 取或。少一个判据 ⇒ 两种形态能同时对不上(以前两边各认各的)。
USB_MODE=0
[ -f "$PROJECT_ROOT/AGING_USB_MODE" ] && USB_MODE=1
[ -n "${AGING_TOOL_DIR:-}" ] && USB_MODE=1

# ── 找工具: 口径跟 `server/tests/_tools.py:find_tool()` **完全一致** ──
#
# ⚠️ **为什么不能只靠 `command -v`**(2026-11-11 上板时实测):
#   `ethtool` 装在 **`/usr/sbin`** —— 而 `/usr/sbin` **不在**非登录 shell 的
#   PATH 里(worker 从桌面终端跑时有, 从别的会话跑时没有)⇒ `command -v ethtool`
#   找不到 ⇒ 这个脚本报「**未装 ethtool**」。
#   ⇒ 而 Python 侧(`/api/deps/check`)是**按标准目录**找的 ⇒ 它说「装好了」
#   ⇒ **同一台机器两个说法** ⇒ 互相打脸 ⇒ 工人照着这个脚本的结论去排障,
#      追的是一个**不存在的故障**(真故障反而被这条假象盖住)。
#   ⇒ 口径改成跟 Python 侧一致: ① **随包工具目录**(`AGING_TOOL_DIR` 或
#      盘根 `tools/bin`) ② PATH ③ **/usr/local/sbin /usr/sbin /sbin /usr/local/bin**
TOOL_DIR="${AGING_TOOL_DIR:-}"
if [ -z "$TOOL_DIR" ] && [ -d "$PROJECT_ROOT/tools/bin" ]; then
    TOOL_DIR="$PROJECT_ROOT/tools"
fi

# find_tool <名字> ⇒ 打得开就打出绝对路径, 打不开返回 1
find_tool() {
    local name="$1" p d
    if [ -n "$TOOL_DIR" ] && [ -x "$TOOL_DIR/bin/$name" ]; then
        printf "%s" "$TOOL_DIR/bin/$name"
        return 0
    fi
    p="$(command -v "$name" 2>/dev/null)"
    if [ -n "$p" ]; then
        printf "%s" "$p"
        return 0
    fi
    for d in /usr/local/sbin /usr/sbin /sbin /usr/local/bin; do
        if [ -x "$d/$name" ]; then
            printf "%s" "$d/$name"
            return 0
        fi
    done
    return 1
}

echo "###############################################"
echo "# 网口诊断 (对齐 §6.6)  iface=$IFACE"
echo "###############################################"
echo ""

# ── ① 链路 ──
echo "==> ① 链路检测 (纯 PHY 层, 零网络依赖)"
ETHTOOL="$(find_tool ethtool || true)"
if [ -z "$ETHTOOL" ]; then
    if [ "$USB_MODE" = "1" ]; then
        echo "  ✗ 未装 ethtool —— U 盘版: 盘上自带 fio / glmark2-es2, 其余工具用板子出厂那份 ⇒"
        echo "     缺工具或工具起不来, 说明这版出厂镜像没带齐, 请改用设备版(deb)安装"
    else
        echo "  ✗ 未装 ethtool —— sudo bash scripts/install_deps.sh"
    fi
    exit 1
fi
if [ ! -d "/sys/class/net/$IFACE" ]; then
    echo "  ✗ 网口 $IFACE 不存在。本机网口:"
    ls /sys/class/net/ 2>/dev/null | grep -E '^(eth|en|eno|enp)' | sed 's/^/      /'
    exit 1
fi
echo "  operstate: $(cat /sys/class/net/$IFACE/operstate 2>/dev/null)"
LINK=$("$ETHTOOL" "$IFACE" 2>/dev/null | grep -oP '^\s*Link detected:\s*\K\w+')
SPEED=$("$ETHTOOL" "$IFACE" 2>/dev/null | grep -oP '^\s*Speed:\s*\K\S+')
echo "  Link detected: ${LINK:-未知}"
[ -n "$SPEED" ] && echo "  Speed: $SPEED"
if [ "$LINK" != "yes" ]; then
    echo ""
    echo "  ✗ 链路 DOWN —— 网线未插 / PHY 不通。"
    echo "    §6.6 判定: skipped(env: unplugged), **不判设备 failed**。"
    echo "    插好网线后跑测试会走「已插好·重测」分支。"
    exit 0
fi
echo "  ✓ 链路 UP"
echo ""

# ── ② 取 IP ──
echo "==> ② 取 IP"
IP="$(find_tool ip || true)"
if [ -z "$IP" ]; then
    echo "  ✗ 缺 ip 命令 ⇒ 取不到 IP 也没法判断出网(装 iproute2)"
    exit 1
fi
IPV4=$("$IP" -4 addr show "$IFACE" 2>/dev/null | grep -oP '^\s*inet\s+\K[\d.]+' | head -1)
if [ -n "$IPV4" ]; then
    echo "  ✓ 已有 IP: $IPV4  ($("$IP" route get 1.1.1.1 2>/dev/null | grep -oP 'src\s+\K[\d.]+' || echo '来源未知'))"
    echo "    注: 本脚本不主动跑 udhcpc (会改配置)。测试流程里是 DHCP 优先、静态兜底。"
else
    echo "  ! 当前无 IPv4 地址 —— 测试流程会先试 udhcpc, 拿不到则静态兜底"
    UDHCPC="$(find_tool udhcpc || true)"
    if [ -n "$UDHCPC" ]; then
        echo "    udhcpc: $UDHCPC ✓"
    else
        echo "    ✗ 缺 udhcpc —— DHCP 与静态兜底都会走 §6.6 的 no_ip → skipped 分支"
    fi
fi
echo ""

# ── ③ 出网 (三级降级) ──
echo "==> ③ 出网 (三级降级, 任一级通即通过)"
DNS="223.5.5.5"
PUB="www.baidu.com"
GW=$("$IP" route show default 2>/dev/null | grep -oP '^default via \K[\d.]+' | head -1)
PING="$(find_tool ping || true)"
if [ -z "$PING" ]; then
    echo "  ✗ 缺 ping 命令 ⇒ 出网这一项判不了(装 iputils-ping)"
    exit 1
fi

_try_ping() {
    # $1=标签 $2=目标
    if [ -z "$2" ]; then
        echo "  [$1] 未取到目标, 跳过该级"
        return 2
    fi
    OUT=$("$PING" -I "$IFACE" -c 3 -W 2 "$2" 2>&1)
    # ⚠️ 这里原来写的是 `'^\d+ packets transmitted, \1 received'` —— `\1` 是
    #    **反向引用**, 而整个模式里**没有捕获组** ⇒ GNU grep 在**编译阶段**就
    #    报 `reference to non-existent subpattern` 并 exit 2 ⇒ 这个 `if` 恒为
    #    假 ⇒ _try_ping **永远走「未通」分支** ⇒ 哪怕网络完全正常, 这个诊断
    #    脚本也恒定报「三级全败」⇒ 工人照它排障会去追一个不存在的故障。
    #    现在改成「**收到的包数不为 0**」(`[1-9]\d*`)。
    if echo "$OUT" | grep -qP '^\d+ packets transmitted, [1-9]\d* received'; then
        RTT=$(echo "$OUT" | grep -oP 'min/avg/max\S* = [\d.]+/[\d.]+/[\d.]+' | head -1)
        echo "  ✓ [$1] $2 通过  ($RTT)"
        return 0
    fi
    LOSS=$(echo "$OUT" | grep -oP '\d+% packet loss' | head -1)
    echo "  ✗ [$1] $2 未通 ($LOSS)"
    return 1
}

if _try_ping "网关" "$GW"; then
    echo "  → 网关即通, 链路+IP+二层全正常 (§6.6 判 passed)"
elif _try_ping "DNS" "$DNS"; then
    echo "  → 网关不通但 DNS 通, 多半是静态路由/网关配错, 仍判 passed(第 2 级)"
elif _try_ping "公网" "$PUB"; then
    echo "  → 只有公网通, DNS 解析或上游有问题, 仍判 passed(第 3 级)"
else
    echo ""
    echo "  ⏭ 三级全败 —— §6.6 判定:"
    [ -n "$IPV4" ] && echo "     · 有 IP → skipped(no_egress), 工位上游网络问题, 不判设备 failed"
    [ -z "$IPV4" ] && echo "     · 无 IP → skipped(no_ip), 环境不具备取 IP 条件"
fi
echo ""
