#!/usr/bin/env python3
"""用 Python libgpiod 探针 PI5 物理针脚 5 的 GPIO 映射.

策略: 你的风扇上电就转, 说明使能线 active-high.
所以测法是: 拉低 GPIO, 看风扇是否停; 拉回高, 看是否转回.

依赖: sudo apt install python3-libgpiod
权限: 必须 sudo (/dev/gpiochipN 默认只 root 能访问)
"""
import sys
import os
import time

def main():
    try:
        import gpiod
    except ImportError:
        print("✗ Python libgpiod 未装, 先跑: sudo apt install python3-libgpiod")
        return 1

    if os.geteuid() != 0:
        print(f"✗ 当前不是 root, 重新用 sudo 跑: sudo python3 {os.path.abspath(__file__)}")
        return 1

    # 候选: 各 gpiochip 的 line 5 (按 A527 DTS 推断 PI5 = Port I line 5)
    CANDIDATES = []
    for chip in range(8):
        for line in (5, 4, 6, 12, 37, 69):  # 5=常规, 37/69=大偏移
            CANDIDATES.append((chip, line))

    print("==> 交互式 PI5 探测")
    print("    你的风扇上电就转 (active-high 使能)")
    print("    测法: 拉低 GPIO → 风扇应该停; 拉回高 → 应该转")
    print("    按 y = 拉低后停了 → 找到; Ctrl+C 中止")
    print()

    for chip, line in CANDIDATES:
        chip_path = f"/dev/gpiochip{chip}"
        if not os.path.exists(chip_path):
            continue
        try:
            chip_obj = gpiod.Chip(chip_path)
        except Exception as e:
            print(f"  ✗ {chip_path}: {e}")
            continue

        print(f"── 试: {chip_path} line {line} ", end="", flush=True)
        line_obj = None
        try:
            line_obj = chip_obj.get_line(line)
            line_obj.request(
                consumer="probe-pi5",
                type=gpiod.LINE_REQ_DIR_OUT,
                default_val=0,
            )
        except Exception as e:
            print(f"→ 跳过 ({e})")
            continue

        try:
            # 先拉低 (关风扇)
            line_obj.set_value(0)
            time.sleep(0.5)
            ans = input("→ 风扇停了吗? (y/n): ").strip().lower()
            if ans == "y":
                # 找到了, 拉回高验证
                line_obj.set_value(1)
                time.sleep(0.5)
                ans2 = input("→ 拉回高, 风扇转回了吗? (y/n): ").strip().lower()
                if ans2 == "y":
                    print()
                    print(f"  ★★ 确认: {chip_path} line {line} 控制风扇")
                    print()
                    print("==> 填到 config.yaml:")
                    print(f"    fan:")
                    print(f"      gpio_chip: {chip}")
                    print(f"      gpio_line: {line}")
                    print(f"      active_level: high   # 拉高=转, 拉低=停")
                    print()
                    return 0
                else:
                    print("  → 可能是输出而非使能, 但 line 找对了")
                    print(f"     填 chip={chip} line={line} (active_level 自己定)")
                    return 0
            # 没停, 拉回高避免影响
            line_obj.set_value(1)
            time.sleep(0.1)
        except Exception as e:
            print(f"  → 错误: {e}")
        finally:
            try:
                line_obj.release()
            except Exception:
                pass

    print()
    print("==> 所有候选都没让风扇停")
    print("    可能: BSP 用了非典型 bank 聚合, 试手动测:")
    print()
    print("    import gpiod")
    print("    chip = gpiod.Chip('/dev/gpiochip0')")
    print("    print(chip.num_lines())   # 查总 line 数")
    print()
    print("    # 然后逐个测: 拉低看风扇停不停")
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n(用户中止)")
        sys.exit(130)
