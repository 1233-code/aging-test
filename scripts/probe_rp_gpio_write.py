#!/usr/bin/env python3
"""🔴 已停用 —— 但**停用理由是「崩因不明」, 不是「写这个节点会崩」**。

⚠️ 更正一条曾经的错误结论:
   2026-10-22 终端手工验证过 `echo 0/1 > /proc/rp_gpio/pi5` 连写 5 次、
   `cat` 读 3 次, 电平正确回读 (1 → 0 → 1), **板子正常**。
   `/proc/rp_gpio/pi5` 可读可写, rp_gpio 驱动是好的。
   之前"一写就 kernel panic"的说法是从权限位 0666 推断出来的, **已作废**。

真正的情况: UI 上单独跑风扇测试会让板子 kernel panic
(屏幕弹 `crashdump now, please connect to tigerdump tool and keep power on`),
但**崩因至今没查清**, 而且过热 / 内存 / Python 本身 / rp_gpio 驱动都已排除。

原脚本的写入阶段已移除, 在拿到 panic 调用栈之前不放回来。
`server/tests/fan.py` 里的写路径同样已从源码删除 —— 崩因不明时,
不让一个能崩板的代码留在产线工具里。

**只读诊断直接用一行命令**, 不需要脚本:
    python3 -c "print(repr(open('/proc/rp_gpio/pi5','rb').read(4)))"

详见 docs/需求设计.md §6.8。
"""
import sys

print(__doc__, file=sys.stderr)
raise SystemExit(2)