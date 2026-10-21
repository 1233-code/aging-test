#!/usr/bin/env python3
"""诊断 vendor 'fan_gpio_control' 驱动, 找它暴露的 sysfs 接口.

这个驱动直接在 kernel 里控制了风扇使能 GPIO, 标准 gpiod 看不到.

用法: sudo python3 scripts/probe_fan_driver.py
"""
import os
import sys
import subprocess

def main():
    if os.geteuid() != 0:
        print("✗ 需要 root, 重跑: sudo python3", os.path.abspath(__file__))
        return 1

    print("==> 诊断 fan_gpio_control 驱动")
    print()

    # 1. dmesg 里搜 fan
    print("[1] dmesg 里 fan 相关日志")
    r = subprocess.run(["dmesg"], capture_output=True, text=True)
    for line in r.stdout.splitlines():
        if "fan" in line.lower():
            print(f"    {line}")
    print()

    # 2. /sys 里搜 fan 相关节点
    print("[2] /sys 里 fan 相关节点")
    r = subprocess.run(
        "find /sys -name '*fan*' 2>/dev/null | head -20",
        shell=True, capture_output=True, text=True
    )
    for line in r.stdout.strip().splitlines():
        if line:
            print(f"    {line}")
    print()

    # 3. /sys/devices/platform 里搜
    print("[3] /sys/devices/platform 下 fan 相关")
    r = subprocess.run(
        "ls -la /sys/devices/platform/ 2>/dev/null | grep -i fan",
        shell=True, capture_output=True, text=True
    )
    for line in r.stdout.strip().splitlines():
        if line:
            print(f"    {line}")
    print()

    # 4. 设备树里 fan_gpio_control 节点
    print("[4] /proc/device-tree 里 fan_gpio_control")
    r = subprocess.run(
        "ls -la /proc/device-tree/ 2>/dev/null | grep -i fan",
        shell=True, capture_output=True, text=True
    )
    for line in r.stdout.strip().splitlines():
        if line:
            print(f"    {line}")
    if not r.stdout.strip():
        # 直接找
        r2 = subprocess.run(
            "find /proc/device-tree -name '*fan*' 2>/dev/null | head",
            shell=True, capture_output=True, text=True
        )
        for line in r2.stdout.strip().splitlines():
            if line:
                print(f"    {line}")
    print()

    # 5. /proc/device-tree/soc 下所有 GPIO chip
    print("[5] SoC 上所有 GPIO chip 节点")
    r = subprocess.run(
        "ls /proc/device-tree/soc/ 2>/dev/null | grep -i pio || echo '(none)'",
        shell=True, capture_output=True, text=True
    )
    for line in r.stdout.strip().splitlines():
        if line:
            print(f"    /proc/device-tree/soc/{line}")
    print()

    print("==> 看完输出后, 告诉我接下来怎么操作:")
    print()
    print("  A) 如果 [2]/[3] 找到了 fan 节点, 我可以看它暴露的 enable 文件")
    print("  B) 如果 [1] dmesg 里有 'fan_gpio_control' init 消息, 看它初始化的 GPIO")
    print("  C) 都不行 → 风扇受 vendor 驱动控, 改 fan test 测 '驱动是否响应' 而非 'GPIO 直控'")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
