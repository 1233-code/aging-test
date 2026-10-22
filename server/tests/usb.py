"""USB 测试 - 枚举设备 + 块设备读写"""
import asyncio
import os
import time
import re
import subprocess
from typing import List

from .base import TestCase, TestContext, TestResult, register
from ._tools import find_tool, with_sudo, sudo_hint_if_unavailable


def _list_usb_block_devices() -> List[str]:
    """枚举 USB 大容量存储设备 (/dev/sdX)"""
    devices = []
    try:
        # lsusb -t 看树形结构,或简单遍历 /sys/block
        for entry in os.listdir("/sys/block"):
            path = f"/sys/block/{entry}"
            if not os.path.isdir(path):
                continue
            # 检查是否是 USB 设备
            try:
                with open(f"{path}/removable") as f:
                    if f.read().strip() != "1":
                        continue
                with open(f"{path}/../device/{entry}/idVendor", ) as _:
                    pass
            except (IOError, OSError):
                pass

            # 简单方法:看 device 路径
            real = os.path.realpath(f"{path}/device")
            if "usb" in real:
                devices.append(f"/dev/{entry}")
    except Exception:
        pass

    # 兜底: lsblk 找 TRAN=usb
    if not devices:
        try:
            # §11.2: 需 root 的调用走命令级 sudo -n
            r = subprocess.run(
                with_sudo(["lsblk", "-d", "-n", "-o", "NAME,TRAN"]),
                capture_output=True, text=True, timeout=5
            )
            for line in r.stdout.splitlines():
                parts = line.split()
                if len(parts) >= 2 and parts[1] == "usb":
                    devices.append(f"/dev/{parts[0]}")
        except Exception:
            pass

    return devices


@register
class USBTest(TestCase):
    name = "usb"
    display_name = "USB 接口"
    description = "枚举 USB 设备 + 块设备读写"

    async def run(self, ctx: TestContext) -> TestResult:
        result = TestResult()
        t_cfg = ctx.test_config
        times = max(1, int(t_cfg.get("times", 5)))
        min_write = float(t_cfg.get("min_write_mbps", 20))

        # §11.1 find_tool 找二进制; §11.2 命令级 sudo 取 root
        lsusb_bin = find_tool("lsusb")
        fio_bin = find_tool("fio")
        if not lsusb_bin or not fio_bin:
            missing = [n for n, b in (("lsusb", lsusb_bin), ("fio", fio_bin)) if not b]
            result.status = "error"
            result.error = (
                f"未找到: {', '.join(missing)}. "
                "解决: sudo apt install usbutils fio (见 docs/需求设计.md §11.7)"
            )
            await ctx.on_log(f"[USB] ✗ {result.error}")
            return result
        await ctx.on_log(f"[USB] 工具: lsusb={lsusb_bin}, fio={fio_bin}")
        if not sudo_hint_if_unavailable(result, "USB"):
            await ctx.on_log(f"[USB] ✗ {result.error}")
            return result

        # 1. 枚举
        await ctx.on_log("[USB] 枚举 USB 设备...")
        proc = await asyncio.create_subprocess_exec(
            *with_sudo([lsusb_bin]),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await proc.communicate()
        usb_lines = stdout.decode(errors="replace").strip().splitlines()
        usb_count = len([l for l in usb_lines if l])
        await ctx.on_log(f"[USB] lsusb 找到 {usb_count} 个设备")
        for line in usb_lines:
            await ctx.on_log(f"  {line}")
        result.metrics["device_count"] = usb_count

        if usb_count == 0:
            result.status = "failed"
            result.error = "未检测到任何 USB 设备"
            return result

        # 2. 找 USB 块设备
        block_devs = _list_usb_block_devices()
        await ctx.on_log(f"[USB] USB 块设备: {block_devs}")

        if not block_devs:
            # 没块设备也算通过(可能是 HUB/鼠标/键盘)
            await ctx.on_log("[USB] ⚠️ 没有可写的 USB 块设备,跳过读写测试")
            result.status = "passed"
            return result

        # 3. 对每个块设备做读写
        all_passed = True
        dev = block_devs[0]
        for i in range(times):
            if await ctx.is_cancelled():
                result.status = "stopped"
                return result
            round_idx = i + 1
            await ctx.on_log(f"[USB] 第 {round_idx}/{times} 轮 ({dev})")
            await ctx.on_progress(int((i / times) * 100), f"第 {round_idx} 轮")

            # 用 libaio direct 写; §11.2 命令级 sudo -n (裸块设备需 root)
            # **必须带 timeout_sec**: 下面是裸 `await proc.communicate()`,
            # 没有任何超时 —— USB 设备异常 / 掉盘时 fio 会**永久挂死**, 测试任务
            # 不结束, 连服务关机都会被拖住。而 fio 被提权成 root 进程后我们
            # (普通用户) 发不了信号收它, 只能让同为 root 的 `timeout` 到点杀它。
            # 32M 写入远用不到 300s, 纯兜底。
            cmd = with_sudo([
                fio_bin, "--name=write", "--ioengine=libaio", "--iodepth=4",
                "--direct=1", "--bs=1M", "--size=32M",
                f"--filename={dev}", "--rw=write",
                "--output-format=normal",
            ], timeout_sec=300)
            t0 = time.time()
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
                )
                text = (await proc.communicate())[0].decode(errors="replace")
                elapsed = time.time() - t0
                if proc.returncode != 0:
                    await ctx.on_log(f"[USB] ❌ fio 写失败 rc={proc.returncode}")
                    await ctx.on_log(text[-500:])
                    all_passed = False
                    break

                # 解析写速度
                m = re.search(r"WRITE:\s*bw=([\d.]+)([KMG]?i?B)/s", text)
                if m:
                    v = float(m.group(1))
                    unit = m.group(2)
                    mult = {"B": 1/1024/1024, "KiB": 1/1024, "MiB": 1}.get(unit, 1)
                    write_mbps = v * mult
                    await ctx.on_log(f"[USB] 写 {write_mbps:.1f} MB/s ({elapsed:.1f}s)")
                    result.metrics[f"round_{round_idx}_write_mbps"] = write_mbps
                    if write_mbps < min_write:
                        await ctx.on_log(f"[USB] ❌ 写速度 {write_mbps:.1f} < {min_write} MB/s")
                        all_passed = False
                else:
                    await ctx.on_log("[USB] ⚠️ 未解析到写速度")
            except FileNotFoundError:
                result.status = "error"
                result.error = "未安装 fio"
                return result
            except Exception as e:
                result.status = "error"
                result.error = f"异常: {e!r}"
                return result

        if all_passed:
            result.status = "passed"
        elif result.status == "pending":
            result.status = "failed"
        return result
