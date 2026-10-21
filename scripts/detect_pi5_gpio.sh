#!/bin/bash
# 多方法探测 PI5 物理针脚 5 的 GPIO 映射
# 用法: bash scripts/detect_pi5_gpio.sh
#
# 尝试 (按优先级):
#   1. libgpiod-tools (gpioinfo) - 优先
#   2. /sys/kernel/debug/gpio (需 debugfs)
#   3. /sys/class/gpio (老接口, 4.8+ 已 deprecated)
#   4. 设备树 /sys/firmware/devicetree/base/... (label)
#   5. 暴力扫描 /dev/gpiochipN + 找 PI5 label

set +e
echo "==> 探测 PI5 (物理针脚 5) 的 GPIO 映射"
echo ""

# ─── 1. libgpiod-tools (gpioget/gpioinfo) ───
GPIOINFO=$(command -v gpioinfo || true)
if [ -n "$GPIOINFO" ]; then
    echo "[方法 1] gpioinfo (libgpiod-tools) — 优先"
    FOUND_LINE=""
    FOUND_CHIP=""
    for chip in /dev/gpiochip*; do
        [ -e "$chip" ] || continue
        CNAME=$(basename "$chip")
        # 解析每一行: "  line  N: "PI5" ..."
        LINE=$($GPIOINFO "$chip" 2>/dev/null | grep -E 'line\s+[0-9]+:\s*"?PI5\b' | awk '{print $2}' | tr -d ':' | head -1)
        if [ -n "$LINE" ]; then
            FOUND_LINE="$LINE"
            FOUND_CHIP="$CNAME"
            break
        fi
    done
    if [ -n "$FOUND_LINE" ]; then
        echo ""
        echo "  ★ 找到: /dev/$FOUND_CHIP line $FOUND_LINE"
        echo ""
        echo "==> 建议 config.yaml 配置:"
        echo "    fan:"
        echo "      gpio_chip: ${FOUND_CHIP#gpiochip}"
        echo "      gpio_line: $FOUND_LINE"
        exit 0
    fi
    echo "  ✗ gpioinfo 找到了但没识别到 PI5 label"
    echo ""
fi

# ─── 2. /sys/kernel/debug/gpio (debugfs, 需 sudo) ───
if [ -r /sys/kernel/debug/gpio ]; then
    echo "[方法 2] /sys/kernel/debug/gpio (debugfs)"
    RESULT=$(grep -B1 "PI5" /sys/kernel/debug/gpio 2>/dev/null | head -5)
    if [ -n "$RESULT" ]; then
        echo "$RESULT"
        echo "  (从 /sys/kernel/debug/gpio 找到 PI5, 但需要进一步解析 line 号)"
        echo ""
    else
        echo "  ✗ debugfs 里没 PI5 标签"
        echo ""
    fi
fi

# ─── 3. 设备树 (label) ───
echo "[方法 3] 设备树 (label)"
shopt -s nullglob 2>/dev/null
for label_file in /sys/class/gpiochip/*/label; do
    if [ -f "$label_file" ]; then
        LABEL=$(cat "$label_file" 2>/dev/null)
        CHIP_DIR=$(dirname "$label_file")
        CHIP=$(basename "$CHIP_DIR")
        if [[ "$LABEL" == *PI5* ]]; then
            echo "  ★ 找到: $CHIP label='$LABEL'"
            NGPIO=$(cat "$CHIP_DIR/ngpio" 2>/dev/null)
            echo "    ($CHIP 共 $NGPIO 个 line, 找具体 line 需要其它方法)"
            echo ""
        fi
    fi
done

# ─── 4. 列出所有可用 chip ───
echo "[方法 4] 列出所有 gpiochip"
for chip in /dev/gpiochip*; do
    [ -e "$chip" ] || continue
    CNAME=$(basename "$chip")
    LABEL=$(cat /sys/class/gpiochip/${CNAME#gpiochip}/label 2>/dev/null || echo "(无 label)")
    NGPIO=$(cat /sys/class/gpiochip/${CNAME#gpiochip}/ngpio 2>/dev/null || echo "?")
    echo "  /dev/$CNAME  label=$LABEL  ngpio=$NGPIO"
done
echo ""

# ─── 5. 试装 libgpiod-tools ───
if [ -z "$GPIOINFO" ]; then
    echo "[方法 5] 试装 libgpiod-tools"
    apt-cache search libgpiod-tools 2>/dev/null | grep -q "libgpiod-tools"
    HAS_PKG=$?
    if [ $HAS_PKG -ne 0 ]; then
        echo "  ✗ apt 仓库里没有 libgpiod-tools"
        echo "  试加 TUNA mirror main 源..."
        if [ ! -f /etc/apt/sources.list.d/ubuntu-focal-main.list ]; then
            cat > /etc/apt/sources.list.d/ubuntu-focal-main.list <<EOF
deb http://mirrors.tuna.tsinghua.edu.cn/ubuntu-ports/ focal main universe
deb http://mirrors.tuna.tsinghua.edu.cn/ubuntu-ports/ focal-updates main universe
EOF
            apt-get update > /dev/null 2>&1
        fi
        apt-cache search libgpiod-tools 2>/dev/null | grep -q "libgpiod-tools"
        HAS_PKG=$?
    fi
    if [ $HAS_PKG -eq 0 ]; then
        apt-get install -y libgpiod-tools > /tmp/g.log 2>&1
        if [ -x /usr/bin/gpioinfo ]; then
            echo "  ✓ 装好了, 重跑本脚本"
            exit 0
        fi
    fi
fi

# ─── 6. 暴力试 - 用 gpioget 找稳定为 0 的 line (开漏/未配置) ───
GPIOGET=$(command -v gpioget || true)
if [ -n "$GPIOGET" ]; then
    echo "[方法 6] 暴力扫: 找 PI5 (假定初始为 inactive 状态)"
    for chip in /dev/gpiochip*; do
        [ -e "$chip" ] || continue
        CNAME=$(basename "$chip")
        echo "  扫 $CNAME ..."
        # 快速测前 16 个 line, 找初始为 0 的 (开漏/未配置默认低)
        for line in 0 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
            VAL=$($GPIOGET "$CNAME" "$line" 2>/dev/null)
            if [ $? -eq 0 ] && [ "$VAL" = "0" ]; then
                echo "    line $line = 0 (可测, 候选)"
            fi
        done
    done
    echo ""
    echo "  提示: 上面列出的 'line N = 0' 是开漏/未配置, 可以用 'gpioset ... 1' 测一个,"
    echo "        然后看风扇是否启动. 找到后填 config.yaml 的 fan.gpio_line"
fi

echo ""
echo "==> ⚠ 仍没自动找到 PI5"
echo "  最终方案: 手动尝试"
echo "    1. 查板子的原理图 (A527 40-pin 排针 PI5 一般在 gpiochip1 line 5)"
echo "    2. 试: sudo gpioget gpiochip1 5  测初始值"
echo "    3. 试: sudo gpioset gpiochip1 5=1  拉高, 看风扇是否转"
echo "    4. 找到后填到 config.yaml:"
echo "       fan:"
echo "         gpio_chip: 1"
echo "         gpio_line: 5"
