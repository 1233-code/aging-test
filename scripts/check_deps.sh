#!/bin/bash
# 检查老化测试所需的依赖是否齐全 (只查不装)
# 用法: bash scripts/check_deps.sh
#
# 分三级 (依 docs/需求设计.md §11.7):
#   ① 包类·关键     —— 缺 → 拒绝出货 (对应测试项直接判 error)
#   ② 包类·备份     —— 缺 → 警告不阻断 (主路径另有实现)
#   ③ 非包类逐台自检 —— 缺 → 拒绝出货 / 或该项判 error
#
# 退出码: 0 = 可出货; 1 = 有阻断项缺失

# 重要: 普通用户 PATH 不一定含 /sbin /usr/sbin, 这里强制加上
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:$PATH"

BLOCK=0   # 有阻断项缺失

# ───────────────────────── ① 包类·关键 ─────────────────────────
# §11.7 "打包必装清单", 缺一拒绝出货
declare -A CRITICAL=(
    ["stress-ng"]="CPU 压力"
    ["glmark2-es2"]="GPU 渲染 (glmark2-es2-wayland)"
    ["fio"]="存储 读写 (USB/eMMC/TF)"
    ["memtester"]="内存"
    ["ethtool"]="网口 链路"
    ["ping"]="网口 ping (iputils-ping)"
    ["udhcpc"]="网口 DHCP (busybox)"
    ["iw"]="WiFi 扫描 (nl80211)"
    ["rfkill"]="WiFi 射频屏蔽检查 (独立包, 不由 util-linux 提供)"
    ["ip"]="通用 (iproute2)"
    ["lsusb"]="USB 枚举 (usbutils)"
    ["lsblk"]="块设备枚举 (util-linux)"
    ["modetest"]="HDMI 切模式 (libdrm-tests)"
)

# ───────────────────────── ② 包类·备份路径 ─────────────────────────
# §11.7 "依赖补位": libgpiod 系列是风扇的备份路径, 主路径走
# /proc/rp_gpio/pi5 (厂家内核节点), 故缺了只警告不阻断
declare -A BACKUP=(
    ["gpioget"]="GPIO 读 (风扇备份路径, libgpiod-tools)"
    ["gpioinfo"]="GPIO 自动探测 (风扇备份路径)"
    ["gpioset"]="GPIO 写 (风扇备份路径)"
    ["gpiomon"]="GPIO 监听 (风扇备份路径)"
)

echo "==> ① 包类·关键工具 (§11.7 打包必装清单)"
MISSING_CRIT=()
for cmd in "${!CRITICAL[@]}"; do
    if command -v "$cmd" > /dev/null 2>&1; then
        printf "    ✓ %-14s %s\n" "$cmd" "${CRITICAL[$cmd]}"
    else
        MISSING_CRIT+=("$cmd")
        printf "    ✗ %-14s %s\n" "$cmd" "${CRITICAL[$cmd]}"
    fi
done

echo ""
echo "==> ② 包类·备份路径 (缺不阻断)"
MISSING_BAK=()
for cmd in "${!BACKUP[@]}"; do
    if command -v "$cmd" > /dev/null 2>&1; then
        printf "    ✓ %-14s %s\n" "$cmd" "${BACKUP[$cmd]}"
    else
        MISSING_BAK+=("$cmd")
        printf "    ⚠ %-14s %s\n" "$cmd" "${BACKUP[$cmd]}"
    fi
done

# ─────────────────── ③ 非包类逐台自检项 (§11.7) ───────────────────
echo ""
echo "==> ③ 非包类逐台自检 (逐台验证, 不属于 apt)"

_selfcheck_fail=0
_selfcheck_warn=0

sc_block() {
    # $1=名称 $2=结果(ok/err) $3=说明
    if [ "$2" = "ok" ]; then
        printf "    ✓ %s\n" "$1"
    else
        printf "    ✗ %s —— %s\n" "$1" "$3"
        _selfcheck_fail=1
    fi
}
sc_warn() {
    if [ "$2" = "ok" ]; then
        printf "    ✓ %s\n" "$1"
    else
        printf "    ⚠ %s —— %s\n" "$1" "$3"
        _selfcheck_warn=1
    fi
}

# sudo 免密可用 —— §11.2 前提, 缺此项整条命令级提权方案失效
if command -v sudo > /dev/null 2>&1 && sudo -n true 2>/dev/null; then
    sc_block "sudo 免密可用 (sudo -n true)" ok
else
    sc_block "sudo 免密可用 (sudo -n true)" err "未配 NOPASSWD, 需 root 的测试项会判 error (§11.2)"
fi

# 厂商 GPIO 节点 —— 由出货内核镜像提供, apt 包补不了 (§6.8)
GPIO_NODE="/proc/rp_gpio/pi5"
if [ -e "$GPIO_NODE" ]; then
    if [ -w "$GPIO_NODE" ]; then
        sc_block "厂商 GPIO 节点 $GPIO_NODE" ok
    else
        sc_block "厂商 GPIO 节点 $GPIO_NODE" err "节点存在但不可写, 权限被改"
    fi
else
    sc_block "厂商 GPIO 节点 $GPIO_NODE" err "内核镜像缺该节点, 非本工具可修复, 由镜像侧解决 (§6.8)"
fi

# /dev/dri 可用 —— GPU / HDMI 项前提
if ls /dev/dri/card* > /dev/null 2>&1 && ls /dev/dri/renderD* > /dev/null 2>&1; then
    sc_block "/dev/dri 可用 (card* + renderD*)" ok
else
    sc_block "/dev/dri 可用 (card* + renderD*)" err "无渲染设备, GPU / HDMI 项将判 error (§6.3)"
fi

# HDMI 已连接 —— §6.3 前置条件 1, 产线常态连接
HDMI_STATUS=""
for s in /sys/class/drm/*HDMI*/status; do
    [ -f "$s" ] || continue
    if [ "$(cat "$s" 2>/dev/null)" = "connected" ]; then
        HDMI_STATUS="$(basename "$(dirname "$s")")"
        break
    fi
done
if [ -n "$HDMI_STATUS" ]; then
    sc_block "HDMI 已连接 ($HDMI_STATUS)" ok
else
    sc_block "HDMI 已连接" err "无 connected 的 HDMI connector, GPU 项将判 error (§6.3 前置 1)"
fi

# Wayland 会话 —— §6.3 前置条件 2
if [ -n "${WAYLAND_DISPLAY:-}" ]; then
    sc_block "Wayland 会话 (WAYLAND_DISPLAY=$WAYLAND_DISPLAY)" ok
else
    sc_block "Wayland 会话" err "WAYLAND_DISPLAY 为空, 无 GLES 上下文, GPU 项将判 error (§6.3 前置 2)"
fi

# eMMC 健康节点 —— 不存在则 eMMC 项判 error (§6.11)
LIFE_TIME="/sys/block/mmcblk0/device/life_time"
if [ -r "$LIFE_TIME" ]; then
    sc_warn "eMMC 健康节点 life_time" ok
else
    sc_warn "eMMC 健康节点 life_time" err "不存在, eMMC 项的寿命检查判 error (§6.11)"
fi

# SN 可读 —— §2.2, 非阻断但必须提示核对
SN=""
if [ -r /proc/cmdline ]; then
    SN=$(grep -oP 'snum=\K[^ ]*' /proc/cmdline 2>/dev/null)
    if [ -z "$SN" ]; then
        SN=$(grep -oP 'androidboot.serialno=\K[^ ]*' /proc/cmdline 2>/dev/null)
    fi
fi
if [ -n "$SN" ]; then
    sc_warn "被测设备 SN" ok
    printf "      SN = %s\n" "$SN"
else
    sc_warn "被测设备 SN" err "提取失败, 记为 unknown —— 测试照常, 但需人工核对设备 (§2.2)"
fi

# ───────────────────────── 汇总 ─────────────────────────
echo ""
if [ ${#MISSING_CRIT[@]} -gt 0 ] || [ "$_selfcheck_fail" = "1" ]; then
    BLOCK=1
fi

if [ "$BLOCK" = "1" ]; then
    echo "==> ✗ 存在阻断项 —— 拒绝出货 (§11.7)"
    if [ ${#MISSING_CRIT[@]} -gt 0 ]; then
        echo "    缺失关键工具: $(printf '%s ' "${MISSING_CRIT[@]}")"
        echo "    修复: sudo bash scripts/install_deps.sh"
    fi
    echo ""
    echo "  提示: libgpiod-tools 在 Ubuntu universe 仓库, 装它前需要:"
    echo "    sudo apt install software-properties-common"
    echo "    sudo add-apt-repository universe && sudo apt update"
    echo ""
    echo "  提示: modetest 属于 libdrm-tests; libdrm-tools 这个包名在"
    echo "        Ubuntu(focal) 仓库中不存在, 写错会让 apt-get download 直接失败"
    exit 1
fi

if [ ${#MISSING_BAK[@]} -gt 0 ] || [ "$_selfcheck_warn" = "1" ]; then
    echo "==> ⚠ 可出货, 但有非阻断告警"
    if [ ${#MISSING_BAK[@]} -gt 0 ]; then
        echo "    缺备份路径 (主路径不受影响): $(printf '%s ' "${MISSING_BAK[@]}")"
        echo "    风扇主路径走 $GPIO_NODE, 不受影响"
    fi
    exit 0
fi

echo "==> ✓ 全部就位, 可以跑测试"
exit 0
