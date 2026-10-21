#!/usr/bin/env python3
"""枚举所有 gpiochip 的所有 line, 找标签为 PI5 的 (chip, line).

依赖: apt install python3-libgpiod  (官方 v1 绑定)
"""
import sys
import os

def find_via_python_libgpiod():
    """方式 1: 官方 libgpiod v1 Python 绑定"""
    try:
        import gpiod
        if not (hasattr(gpiod, "Chip") and hasattr(gpiod, "LINE_REQ_DIR_IN")):
            return None
    except ImportError:
        return None

    found = []
    for chip_idx in range(8):
        chip_path = f"/dev/gpiochip{chip_idx}"
        if not os.path.exists(chip_path):
            continue
        try:
            chip = gpiod.Chip(chip_path)
            for offset in range(chip.num_lines()):
                try:
                    info = chip.get_line_info(offset)
                    name = (info.name or "").strip()
                    consumer = (info.consumer or "").strip()
                    if name:
                        # 如果名字里含 PI5 或 pi5, 都视为候选
                        if "pi5" in name.lower() or "PI5" in name:
                            found.append({
                                "chip": chip_idx,
                                "line": offset,
                                "name": name,
                                "consumer": consumer,
                                "method": "python-libgpiod",
                            })
                except Exception as e:
                    break
        except Exception as e:
            print(f"  /dev/gpiochip{chip_idx}: 打开失败 - {e}", file=sys.stderr)
    return found


def find_via_subprocess():
    """方式 2: 用 cat /sys/kernel/debug/gpio 读 device tree 标签"""
    found = []
    debug_path = "/sys/kernel/debug/gpio"
    if not os.path.exists(debug_path):
        return found
    try:
        with open(debug_path) as f:
            content = f.read()
    except Exception:
        return found

    # 格式:
    # gpiochip0: GPIOs 0-31, parent: ...
    #  gpio-9  (  fan  ) in  hi
    #  gpio-12 (  PI5   ) out lo

    current_chip = None
    for line in content.splitlines():
        m_chip = None
        if line.startswith("gpiochip"):
            m_chip = __import__("re").match(r"gpiochip(\d+):", line)
            if m_chip:
                current_chip = int(m_chip.group(1))
            continue
        if current_chip is None:
            continue
        # 找形如 "  gpio-12 (  PI5   ) out lo"
        m = __import__("re").match(r"\s*gpio-(\d+)\s+\(\s*([^)]+)\s*\)", line)
        if m:
            offset = int(m.group(1))
            label = m.group(2).strip()
            if "pi5" in label.lower():
                found.append({
                    "chip": current_chip,
                    "line": offset,
                    "name": label,
                    "consumer": "",
                    "method": "debugfs",
                })
    return found


def main():
    print("==> 在所有 gpiochip 里找标签为 PI5 的 line")
    print()

    # 如果不是 root 且 /dev/gpiochipN 不能访问, 试 debugfs (需要 sudo)
    import os
    if os.geteuid() != 0:
        for chip_idx in range(8):
            if os.path.exists(f"/dev/gpiochip{chip_idx}"):
                try:
                    with open(f"/dev/gpiochip{chip_idx}"): pass
                except PermissionError:
                    print("  ⚠ /dev/gpiochipN 无权限, 试 sudo 跑:")
                    print("    sudo python3", os.path.abspath(__file__))
                    print()
                    break

    found = find_via_python_libgpiod()
    if found is None:
        print("  Python libgpiod 未装 (apt install python3-libgpiod)")
        found = []

    # 补充用 debugfs
    extra = find_via_subprocess()
    if extra:
        # 去重
        seen = {(f["chip"], f["line"]) for f in found}
        for e in extra:
            if (e["chip"], e["line"]) not in seen:
                found.append(e)

    if not found:
        print("  ✗ 没找到 PI5 label")
        print()
        print("  备选 (按 A527 DTS 推断):")
        print("    gpiochip1 line 5    (DTS 写 <&pio PI 5>)")
        print("    gpiochip0 line 5    (其他 BSP 可能不同)")
        print()
        print("  最终方案: 查 dts / 原理图后手填")
        return 1

    print(f"  ★ 找到 {len(found)} 个匹配:")
    for f in found:
        c = f.get("consumer", "")
        cstr = f" (消费者: {c})" if c else ""
        print(f"    /dev/gpiochip{f['chip']} line {f['line']:3d}  name='{f['name']}'{cstr}  via {f['method']}")

    print()
    best = found[0]
    print(f"==> 填到 config.yaml:")
    print(f"    fan:")
    print(f"      gpio_chip: {best['chip']}")
    print(f"      gpio_line: {best['line']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
