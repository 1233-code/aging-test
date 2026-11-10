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

echo "###############################################"
echo "# 网口诊断 (对齐 §6.6)  iface=$IFACE"
echo "###############################################"
echo ""

# ── ① 链路 ──
echo "==> ① 链路检测 (纯 PHY 层, 零网络依赖)"
if ! command -v ethtool > /dev/null 2>&1; then
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
LINK=$(ethtool "$IFACE" 2>/dev/null | grep -oP '^\s*Link detected:\s*\K\w+')
SPEED=$(ethtool "$IFACE" 2>/dev/null | grep -oP '^\s*Speed:\s*\K\S+')
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
IPV4=$(ip -4 addr show "$IFACE" 2>/dev/null | grep -oP '^\s*inet\s+\K[\d.]+' | head -1)
if [ -n "$IPV4" ]; then
    echo "  ✓ 已有 IP: $IPV4  ($(ip route get 1.1.1.1 2>/dev/null | grep -oP 'src\s+\K[\d.]+' || echo '来源未知'))"
    echo "    注: 本脚本不主动跑 udhcpc (会改配置)。测试流程里是 DHCP 优先、静态兜底。"
else
    echo "  ! 当前无 IPv4 地址 —— 测试流程会先试 udhcpc, 拿不到则静态兜底"
    if command -v udhcpc > /dev/null 2>&1; then
        echo "    udhcpc: $(command -v udhcpc) ✓"
    else
        echo "    ✗ 缺 udhcpc —— DHCP 与静态兜底都会走 §6.6 的 no_ip → skipped 分支"
    fi
fi
echo ""

# ── ③ 出网 (三级降级) ──
echo "==> ③ 出网 (三级降级, 任一级通即通过)"
DNS="223.5.5.5"
PUB="www.baidu.com"
GW=$(ip route show default 2>/dev/null | grep -oP '^default via \K[\d.]+' | head -1)

_try_ping() {
    # $1=标签 $2=目标
    if [ -z "$2" ]; then
        echo "  [$1] 未取到目标, 跳过该级"
        return 2
    fi
    OUT=$(ping -I "$IFACE" -c 3 -W 2 "$2" 2>&1)
    if echo "$OUT" | grep -qP '^\d+ packets transmitted, \1 received'; then
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
