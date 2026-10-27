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

# 项目根目录 (config.yaml 的位置) —— 与 server/config.py 的 PROJECT_ROOT 一致
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
CONFIG_FILE="$PROJECT_ROOT/config.yaml"

# ── 读 config.yaml 里的点号路径 (如 defaults.gpu.variant) ──
#
# 用 python3 + PyYAML, 因为服务端自己就是靠 PyYAML 读这份配置的 —— 凡能跑
# 这套工具的机器, 这个依赖一定在。读不到(文件缺失/键不存在/没装 PyYAML)
# 一律回落到默认值, **不阻断**: 自检脚本的意义是"挡住真缺东西的板子",
# 不是"因为读不到配置就把所有板子都拒了"。
_cfg_py() {
    local key="$1" py
    for py in python3 "$PROJECT_ROOT/.venv/bin/python"; do
        command -v "$py" >/dev/null 2>&1 || continue
        "$py" -c "import yaml" >/dev/null 2>&1 || continue
        "$py" - "$CONFIG_FILE" "$key" <<'PYEOF' 2>/dev/null
import sys, yaml
path, key = sys.argv[1], sys.argv[2]
try:
    with open(path, encoding="utf-8") as f:
        d = yaml.safe_load(f) or {}
except Exception:
    sys.exit(1)
for k in key.split("."):
    if isinstance(d, dict) and k in d:
        d = d[k]
    else:
        sys.exit(1)
if d is None:
    sys.exit(1)
print(str(d))
PYEOF
        return $?
    done
    return 1
}

read_cfg() {
    local key="$1" default="$2" val=""
    val="$(_cfg_py "$key")" || val=""
    printf "%s" "${val:-$default}"
}

BLOCK=0   # 有阻断项缺失

# ───────────────────────── ① 包类·关键 ─────────────────────────
# §11.7 "打包必装清单", 缺一拒绝出货
declare -A CRITICAL=(
    ["stress-ng"]="CPU 压力"
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
    ["xrandr"]="HDMI 读当前实际输出 (x11-xserver-utils)"
    ["chromium-browser"]="PDF 报告导出 + UI 打开 (§4.4 / §8.2)"
)
# ⚠️ glmark2 **不在**上面那张表里 —— GPU 要哪个二进制取决于会话类型
#    (x11 → glmark2-es2, wayland → glmark2-es2-wayland), 在一张固定表里
#    写死其中一个, 就会在另一种会话的机器上误判「缺关键工具 → 拒绝出货」。
#    它挪到 ③ 里按 variant 逐台判定 (见下面「GPU 变体与会话」)。

# ───────────────────────── ② 包类·诊断工具 ─────────────────────────
# §11.7 "依赖补位": libgpiod 系列是风扇的**诊断工具**, **不是备份路径** ——
# 本板风扇由厂商私有内核驱动直接控制, 标准 gpiod 接口看不到它
# (/sys/class/gpio/ 不存在), 代码里也明确禁止拿 gpioget 静默兼底成 skipped
# (见 server/tests/fan.py 的错误文案)。装它们是为了 scripts/find_pi5_label.py
# 能查 PI5 的 gpiod 标签。故缺了只警告不阻断。
declare -A BACKUP=(
    ["gpioget"]="GPIO 读 (风扇诊断工具, libgpiod-tools)"
    ["gpioinfo"]="GPIO 自动探测 (风扇诊断工具)"
    ["gpioset"]="GPIO 写 (风扇诊断工具)"
    ["gpiomon"]="GPIO 监听 (风扇诊断工具)"
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
echo "==> ② 包类·诊断工具 (缺不阻断)"
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
# ⚠️ **只测可读, 绝不测可写** —— 写这个节点会让板子 kernel panic。
#    用 `test -w` 验收一件我们根本不做的事, 只会诱导别人去试写。
GPIO_NODE="/proc/rp_gpio/pi5"
if [ -e "$GPIO_NODE" ]; then
    if [ -r "$GPIO_NODE" ]; then
        sc_block "厂商 GPIO 节点 $GPIO_NODE (只读)" ok
    else
        sc_block "厂商 GPIO 节点 $GPIO_NODE (只读)" err "节点存在但不可读, 权限被改"
    fi
else
    sc_block "厂商 GPIO 节点 $GPIO_NODE (只读)" err "内核镜像缺该节点, 非本工具可修复, 由镜像侧解决 (§6.8)"
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

# ── GPU 变体与会话 (§6.3) ──
#
# 口径必须跟 server/tests/gpu.py 的 _pick_variant() **讲同一套话**。
# 否则这个脚本跟真跑起来的测试项会互相打脸: 脚本说"缺前置 → 拒绝出货",
# 结果测试项自己跑得好好的(或者反过来) —— 两种情况产线都会开始不信这个工具。
#
# 2026-10-27 修掉的坑: 原来这里无条件要求 WAYLAND_DISPLAY, 于是**每块
# 跑 X11 会话的板子都被判"缺前置 → 拒绝出货"** —— 而 gpu.py 在 auto 模式下
# 本来就能用 DISPLAY + glmark2-es2 正常跑完(runs 41/48 那种 GPU 完全正常
# 却把自己挡死的坑, 就是这么来的)。
GPU_VARIANT_CFG="$(read_cfg defaults.gpu.variant auto)"
case "$GPU_VARIANT_CFG" in
    x11|wayland) ;;
    *) GPU_VARIANT_CFG="auto" ;;
esac

_gpu_env_val() {
    # $1 = DISPLAY | WAYLAND_DISPLAY
    if [ "$1" = "DISPLAY" ]; then printf "%s" "${DISPLAY:-}"; else printf "%s" "${WAYLAND_DISPLAY:-}"; fi
}

GPU_PICK=""; GPU_ENV=""; GPU_BIN=""; GPU_WHY=""
if [ "$GPU_VARIANT_CFG" != "auto" ]; then
    GPU_PICK="$GPU_VARIANT_CFG"
    if [ "$GPU_PICK" = "x11" ]; then
        GPU_ENV="DISPLAY"; GPU_BIN="glmark2-es2"
    else
        GPU_ENV="WAYLAND_DISPLAY"; GPU_BIN="glmark2-es2-wayland"
    fi
    GPU_WHY="config gpu.variant=$GPU_PICK 强制指定"
else
    # auto: 谁的环境变量在 + 谁装了二进制, 就用谁 (与 gpu.py 同序: wayland 优先)
    for _v in wayland x11; do
        if [ "$_v" = "x11" ]; then _e=DISPLAY; _b=glmark2-es2
        else _e=WAYLAND_DISPLAY; _b=glmark2-es2-wayland; fi
        if [ -n "$(_gpu_env_val "$_e")" ] && command -v "$_b" >/dev/null 2>&1; then
            GPU_PICK="$_v"; GPU_ENV="$_e"; GPU_BIN="$_b"; GPU_WHY="auto 选中 $_v 会话"
            break
        fi
    done
    if [ -z "$GPU_PICK" ]; then
        # 没有"会话+二进制"都齐的组合 —— GPU 项必然判 error。
        # 说清到底缺什么, 而不是笼统地报"缺前置"。
        if [ -n "${DISPLAY:-}" ] || [ -n "${WAYLAND_DISPLAY:-}" ]; then
            GPU_WHY="有图形会话环境变量, 但对应的 glmark2 二进制没装"
        else
            GPU_WHY="既无 DISPLAY 也无 WAYLAND_DISPLAY, 没有图形会话"
        fi
    fi
fi

if [ -z "$GPU_PICK" ]; then
    sc_block "GPU 图形会话前置 (§6.3)" err "$GPU_WHY, GPU 项将判 error"
elif ! command -v "$GPU_BIN" >/dev/null 2>&1; then
    sc_block "GPU 渲染二进制 $GPU_BIN" err "未安装, GPU 项将判 error (§6.3)"
elif [ -z "$(_gpu_env_val "$GPU_ENV")" ]; then
    sc_block "GPU 图形会话 ($GPU_ENV)" err "为空, 无 GLES 上下文, GPU 项将判 error (§6.3 前置 2)"
elif [ "$GPU_VARIANT_CFG" = "auto" ] && [ "$GPU_PICK" = "x11" ]; then
    # 能跑, 但不是设计稿写的那个会话 —— 降级成告警, **不阻断出货**。
    sc_warn "GPU 图形会话 (X11)" err "本机会话是 X11 而非设计稿的 Wayland; 已按 auto 选 glmark2-es2, GPU 项能跑 —— 属非阻断告警, 请确认产线用的是 X11 镜像"
    printf "      %s, 二进制 %s\n" "$GPU_WHY" "$GPU_BIN"
else
    sc_block "GPU 图形会话 ($GPU_PICK)" ok
    printf "      %s, %s=%s, 二进制 %s\n" "$GPU_WHY" "$GPU_ENV" "$(_gpu_env_val "$GPU_ENV")" "$GPU_BIN"
fi

# eMMC 健康节点 —— 不存在则 eMMC 项判 error (§6.11)
# ⚠️ 从「警告」提到「阻断」: 寿命检查进了 eMMC 项的**判定**里(已用寿命
#    百分比 > 80% → failed; 节点读不到 → error), 所以它不是"锦上添花的
#    健康信息", 而是**决定这板子能不能出货**的必读项。留成警告的话, 脚本
#    放行 → 跑到 eMMC 那一步才 error → 白跑前面十几分钟。
LIFE_TIME="/sys/block/mmcblk0/device/life_time"
if [ -r "$LIFE_TIME" ]; then
    sc_block "eMMC 健康节点 life_time" ok
    printf "      内容 = %s\n" "$(cat "$LIFE_TIME" 2>/dev/null | tr -s ' ')"
else
    sc_block "eMMC 健康节点 life_time" err "不存在, eMMC 项的寿命检查判 error → 该板不合格 (§6.11)"
fi

# ⚠️ CJK 字体 —— §8.2.1。**查的是「字体真有这个字形」, 不是「装没装中文字体包」**
#
# 为什么必须查: 本板 `fc-match sans-serif` 解析到 **DejaVu Sans**, 它**一个
# CJK 字形都没有**。浏览器渲染时不会报错、不会换字体, 只会把这些字画成空白,
# 然后**照样 rc=0 输出一份合法的 PDF** —— 产线会把一份全空白的中文报告当合格
# 品归档。用 `:charset=5de5`(工) 让 fontconfig 回答「哪个字体真的有这个字」。
CJK_FONT="$(fc-list ':charset=5de5' family 2>/dev/null | head -1 | cut -d, -f1)"
if [ -n "$CJK_FONT" ]; then
    sc_block "有含中文字形的字体" ok
    printf "      字体 = %s\n" "$CJK_FONT"
else
    sc_block "有含中文字形的字体" err "没有任何字体含 U+5DE5(工); PDF 报告会静默变成空白 (§8.2.1)。装: sudo apt install fonts-wqy-zenhei"
fi

# chromium headless 真能跑 —— 光看命令在不在不够
if command -v chromium-browser > /dev/null 2>&1; then
    if timeout 30 chromium-browser --headless --disable-gpu --version > /dev/null 2>&1; then
        sc_block "chromium headless 可用" ok
    else
        sc_block "chromium headless 可用" err "命令在但 --headless 起不来 (§8.2 PDF 导出会失败)"
    fi
else
    sc_block "chromium headless 可用" err "未找到 chromium-browser (§8.2 PDF 导出依赖它)"
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
    echo "  提示: xrandr 属于 x11-xserver-utils; libdrm-tools / modetest 已"
    echo "        不再需要 —— HDMI 模式切换整块移除 (见 docs §6.7.1)"
    exit 1
fi

if [ ${#MISSING_BAK[@]} -gt 0 ] || [ "$_selfcheck_warn" = "1" ]; then
    echo "==> ⚠ 可出货, 但有非阻断告警"
    if [ ${#MISSING_BAK[@]} -gt 0 ]; then
        echo "    缺诊断工具 (主路径不受影响): $(printf '%s ' "${MISSING_BAK[@]}")"
        echo "    风扇主路径走 $GPIO_NODE, 不受影响"
    fi
    exit 0
fi

echo "==> ✓ 全部就位, 可以跑测试"
exit 0
