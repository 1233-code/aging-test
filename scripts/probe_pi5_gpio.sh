#!/bin/bash
# 交互式探测 PI5: 在常见 A527 GPIO 候选里一个个拉高, 听风扇是否启动
# 用法: bash scripts/probe_pi5_gpio.sh
#
# 假定 gpioget/gpioset 不可用, 用 sysfs 直接操作:
#   /sys/class/gpio/export N → 导出 line N
#   /sys/class/gpio/gpioN/direction = out
#   /sys/class/gpio/gpioN/value = 1 (拉高) → 看风扇
#   /sys/class/gpio/gpioN/value = 0 (拉低) → 风扇停
#
# 注意: 4.8+ 内核 sysfs GPIO 已 deprecated, 大部分 A527 镜像仍然支持

set -e
echo "==> 交互式 PI5 探测"
echo ""
echo "前提: 你风扇接到 A527 的 PI5 物理针脚 5"
echo ""
echo "我会按顺序拉高候选 GPIO, 你注意:"
echo "  1. 风扇是否开始转"
echo "  2. 风扇声音变化 / 转速改变"
echo ""
echo "找到后, 风扇在转的时候按 y 确认; 不转就按 n 继续试下一个"
echo ""

# A527 40-pin 排针 PI5 的常见候选 (按可能性排序)
# 格式: "chip:line,描述"
CANDIDATES=(
    "1:5,A527 PI5 → GPIOI_5 (port I line 5, 最常见)"
    "0:5,A527 PI5 → bank A line 5 (部分板子用此映射)"
    "1:4,A527 PI4 (备选: PI4 经常被复用到 PI5)"
    "1:6,A527 PI6 (备选: PI6 经常被复用到 PI5)"
    "0:12,尝试 bank A line 12"
    "0:7,尝试 bank A line 7"
)

probe_pin() {
    local chip=$1
    local line=$2
    local desc=$3
    local gpio_sysfs="/sys/class/gpio/gpio${line}"
    local chip_dev="/dev/gpiochip${chip}"

    if [ ! -e "$chip_dev" ]; then
        echo "  ✗ $chip_dev 不存在, 跳过"
        return 1
    fi

    echo ""
    echo "── 试: /dev/gpiochip${chip} line ${line} (${desc})"

    # 1) 导出 (sysfs 方式)
    if [ ! -d "$gpio_sysfs" ]; then
        if ! echo "${line}" > /sys/class/gpio/export 2>/dev/null; then
            echo "  ✗ sysfs export 失败 (内核可能未启用 legacy GPIO sysfs)"
            echo "     只能装 libgpiod-tools 后用 gpioget 测"
            return 1
        fi
        sleep 0.1
    fi

    if [ ! -d "$gpio_sysfs" ]; then
        echo "  ✗ export 之后仍无 sysfs 目录"
        return 1
    fi

    # 2) 设为输出, 拉高
    if ! echo "out" > "$gpio_sysfs/direction" 2>/dev/null; then
        echo "  ✗ direction 设置失败"
        return 1
    fi
    echo 1 > "$gpio_sysfs/value"
    sleep 0.3

    echo "  → 已拉高 /sys/class/gpio/gpio${line}/value = 1"
    echo "  → 现在听风扇是否开始转 / 声音变化"
    read -p "  风扇转了吗? (y/n): " answer

    # 拉低
    echo 0 > "$gpio_sysfs/value"
    sleep 0.2
    echo "  → 已拉低"

    if [[ "$answer" == "y" || "$answer" == "Y" ]]; then
        echo ""
        echo "  ★ 找到了: /dev/gpiochip${chip} line ${line}"
        echo ""
        echo "==> 填到 config.yaml:"
        echo "    fan:"
        echo "      gpio_chip: $chip"
        echo "      gpio_line: $line"
        return 0
    fi
    return 1
}

# 顺序试
for cand in "${CANDIDATES[@]}"; do
    chip=$(echo "$cand" | cut -d: -f1)
    line=$(echo "$cand" | cut -d: -f2)
    desc=$(echo "$cand" | cut -d, -f2-)
    if probe_pin "$chip" "$line" "$desc"; then
        exit 0
    fi
done

echo ""
echo "==> 常见候选都试了, 没找到"
echo "  最终方案:"
echo "    1. 查 A527 板子原理图 (40-pin 排针 PI5 → SoC 的哪个 GPIO)"
echo "    2. 装 libgpiod-tools 后: sudo gpioget / sudo gpioset 逐个试"
echo "    3. 联系板子供应商要 GPIO mapping"
echo ""
echo "  找到后填到 config.yaml 的 fan.gpio_chip + fan.gpio_line"
