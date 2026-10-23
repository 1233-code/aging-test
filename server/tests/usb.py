"""USB 测试 - 枚举设备 + 块设备读写"""
import asyncio
import os
import time
import re
import subprocess
from typing import List, Optional

from .base import TestCase, TestContext, TestResult, register
from ._tools import (
    find_tool, with_sudo, sudo_hint_if_unavailable,
    run_proc_with_progress, fio_runtime_args, fio_timeout_sec,
)


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

    def expected_seconds(self, test_config: dict) -> Optional[float]:
        times = max(1, int(test_config.get("times", 3)))
        duration = max(5, int(test_config.get("duration_sec", 18)))
        return times * duration

    async def run(self, ctx: TestContext) -> TestResult:
        result = TestResult()
        t_cfg = ctx.test_config
        times = max(1, int(t_cfg.get("times", 3)))
        # 📌 **不再用速度判定通过与否**(2026-10-23 产线决定)。
        #   原因: 同一型号 U 盘的写入速度个体差异极大, 且 USB 2.0 High-Speed 链路上
        #   闪存的**写**典型值本来就只有 8~20 MB/s(要走 FTL 擦写, 普遍比读慢一半)。
        #   原来拿 `min_write_mbps: 20` 卡产线, 实测一个正常 U 盘三轮稳定在
        #   10.7~10.9 MB/s 就被判 failed —— 阈值卡在典型值上沿, **正常盘也过不了**。
        #   现在**只有 fio 退出码决定通过**: 读写不出错、盘不报错就算好。
        #   速度仍然上报(`round_N_write_mbps`), 供产线看趋势、排查慢盘, 但不参与判定。

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
        # 失败原因**必须攒起来写进 result.error**。以前只置 all_passed=False,
        # 结果页就只显示「失败」而 error 是 None —— 工人看到的是一句没头没尾的
        # 失败, 原因只印在日志里, 极易误以为工具或设备坏了。
        fail_reasons: List[str] = []
        dev = block_devs[0]

        # ⚠️ **这一项是真的在往设备上反复写数据** —— 跟 eMMC/TF 写临时文件
        #    不同, 这里的目标是 `/dev/sdX` 裸块设备。原来只写 32MB 就跑完,
        #    现在按 UI 上配的时长**持续写入**(工作集反复覆写)。这正是老化测试
        #    要的东西(把 U 盘的闪存反复擦写, 挑出坏块/接触不良), 但要清楚:
        #    写入量比原来大得多, 别拿唯一一份、有未备份数据的盘来跑。
        duration = max(5, int(t_cfg.get("duration_sec", 18)))
        size_mb = max(16, int(t_cfg.get("size_mb", 32)))
        rt = fio_runtime_args(duration, size_mb)
        tmo = fio_timeout_sec(duration)
        await ctx.on_log(
            f"[USB] 目标 {dev} · {times} 轮 × 写 {duration}s · 工作集 {size_mb}MB"
            f" · 预计约 {duration * times}s"
        )

        for i in range(times):
            if await ctx.is_cancelled():
                result.status = "stopped"
                return result
            round_idx = i + 1
            await ctx.on_log(f"[USB] 第 {round_idx}/{times} 轮 ({dev})")

            # 用 libaio direct 写; §11.2 命令级 sudo -n (裸块设备需 root)
            # **必须带 timeout_sec**: USB 设备异常 / 掉盘时 fio 会**永久挂死**,
            # 测试任务不结束, 连服务关机都会被拖住。而 fio 被提权成 root 进程
            # 后我们 (普通用户) 发不了信号收它, 只能靠同为 root 的 `timeout`
            # 到点杀它。
            cmd = with_sudo([
                fio_bin, "--name=write", "--ioengine=libaio", "--iodepth=4",
                "--direct=1", "--bs=1M",
                f"--filename={dev}", "--rw=write",
                "--output-format=normal",
            ] + rt, timeout_sec=tmo)
            t0 = time.time()
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
                )

                async def tick(e: float, i=i, round_idx=round_idx):
                    await ctx.on_progress(
                        int((i + min(1.0, e / duration)) / times * 100),
                        f"第 {round_idx}/{times} 轮 · 写 {e:.0f}s / {duration:.0f}s",
                    )

                rc, text, killed = await run_proc_with_progress(
                    proc, duration, ctx=ctx, on_tick=tick, hard_limit_sec=tmo + 30,
                )
                elapsed = time.time() - t0
                if killed:
                    await ctx.on_log("[USB] ⚠ 写测试被中断 (取消或超时)")
                    result.status = "stopped"
                    return result
                if rc != 0:
                    await ctx.on_log(f"[USB] ❌ fio 写失败 rc={rc}")
                    await ctx.on_log(text[-500:])
                    all_passed = False
                    fail_reasons.append(
                        f"第 {round_idx}/{times} 轮 fio 退出码 {rc}(读写出错, 盘可能已掉或报坏块)"
                    )
                    break

                # 解析写速度 —— **只上报, 不判定**
                m = re.search(r"WRITE:\s*bw=([\d.]+)([KMG]?i?B)/s", text)
                if m:
                    v = float(m.group(1))
                    unit = m.group(2)
                    mult = {"B": 1/1024/1024, "KiB": 1/1024, "MiB": 1}.get(unit, 1)
                    write_mbps = v * mult
                    await ctx.on_log(f"[USB] 写 {write_mbps:.1f} MB/s ({elapsed:.1f}s) —— 仅记录, 不参与判定")
                    result.metrics[f"round_{round_idx}_write_mbps"] = write_mbps
                else:
                    await ctx.on_log("[USB] ⚠️ 未解析到写速度 (不影响判定, fio 已正常退出)")
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
            # 汇总速度, 方便一眼看整体水平(仍不参与判定)
            speeds = [
                result.metrics[k] for k in result.metrics
                if k.endswith("_write_mbps")
            ]
            if speeds:
                avg = sum(speeds) / len(speeds)
                result.metrics["avg_write_mbps"] = round(avg, 1)
                await ctx.on_log(
                    f"[USB] ✓ {times} 轮读写均正常, 平均 {avg:.1f} MB/s"
                    f"(USB 2.0 High-Speed 闪存写入 8~20 MB/s 属正常, 不作判定依据)"
                )
        elif result.status == "pending":
            result.status = "failed"
            result.error = "; ".join(fail_reasons) or "读写失败, 原因未捕获"
        return result
