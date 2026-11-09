"""回归测试: WebSocket 断开后**不许空转** —— 一个空转的 handler 能吃满一整核。

**起因 (2026-11-02, 新板 A10000000000010 每次跑 CPU 项都硬复位)**
    老化测试应用**每次**都在「CPU 压力」这一项里把板子**硬复位**, 而同样的
    加压命令在**终端**里手跑, 板子全程没事 ⇒ 触发条件一定在**应用与终端的
    差别**里。

    查应用的进程占用, 逮到一个要命的:
        $ ps -eo pid,pcpu,comm --sort=-pcpu | head
          2836 94.6 uvicorn          ← **服务自己吃满一核**
    `/proc/2836/syscall` 一直是 `running` ⇒ 不是等 I/O, 是**纯 CPU 空转**。

    翻 `server/main.py` 的 WebSocket 处理函数, 找到这一段:

        while True:
            send_task = asyncio.create_task(_drain_queue(ws, q))
            recv_task = asyncio.create_task(ws.receive_text())
            done, pending = await asyncio.wait([send_task, recv_task], ...)
            for t in pending: t.cancel()
            if recv_task in done:
                try:
                    msg = json.loads(recv_task.result())
                    ...
                except Exception:
                    pass          # ⚠️⚠️ **这里把「页面断了」也咽了**
            else:
                pass

    页面一断, `ws.receive_text()` 立刻抛 `WebSocketDisconnect`(starlette);
    下一轮它抛 `RuntimeError`(「disconnect 之后再 receive」)。
    **两个都被上面那个 `except Exception: pass` 咽掉** ⇒ `while True`
    每一圈都「秒完」⇒ **空转** ⇒ 一个 handler 吃满一整核, 而且
    `finally: runner.unsubscribe(q)` **永远也走不到**(订阅队列也一起漏掉)。

    这就是「终端好好的, 一进应用就不行」的差别:
      · 终端 ⇒ 没有服务在吃核 ⇒ 加压就是加压;
      · 应用 ⇒ 服务已经占住 1~2 个核(每断一次连接多一个空转 handler)
        ⇒ CPU 项再加压时**整板满载** ⇒ 电流台阶 ⇒ 板子被拉死。

    ⇒ 这个测试要盯住的就是: **页面断开后, handler 必须自己退出, 不许空转。**

**这个测试不碰真硬件, 也不碰产线数据**: 假 WebSocket, 数据目录换成临时目录。
跑法: .venv/bin/python -m tests.test_ws_disconnect_spin
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests._tmp import mkdtemp                   # noqa: E402

# ── 数据目录换成临时目录 (不许在真仓库/真产线数据目录里落东西) ──
DATA_DIR = mkdtemp(prefix="aging-wsspin-")
os.environ["AGING_DATA_DIR"] = DATA_DIR

from fastapi import WebSocketDisconnect          # noqa: E402
from server import main as main_mod             # noqa: E402
from server.runner import runner                # noqa: E402

FAILS = []
PASSES = 0


def check(cond, msg):
    global PASSES
    if cond:
        PASSES += 1
        print(f"  ✓ {msg}")
    else:
        FAILS.append(msg)
        print(f"  ✗ {msg}")


class FakeWS:
    """假装是 starlette 的 WebSocket —— 页面**已经断了**。

    页面断开时 starlette 的行为:
      · 第一次 `receive_text()` ⇒ 抛 `WebSocketDisconnect`;
      · 之后每一次 ⇒ 抛 `RuntimeError`("disconnect 之后再 receive")。
    这里两种都如实模拟 —— 修复必须对**两种**都收得住。
    """

    def __init__(self, disconnect_after=0):
        self.recv_calls = 0
        self.sent = []
        self._disconnect_after = disconnect_after

    async def accept(self):
        pass

    async def send_json(self, data):
        self.sent.append(data)

    async def receive_text(self):
        self.recv_calls += 1
        if self.recv_calls == 1 and self._disconnect_after == 0:
            raise WebSocketDisconnect(1000)     # 页面一上来就断了
        if self.recv_calls == 1:
            return '{"type": "ping"}'           # 页面还在, 心跳来了
        if self.recv_calls == 2:
            raise WebSocketDisconnect(1000)     # 页面断了
        raise RuntimeError('Cannot call "receive" once a disconnect '
                           "message has been received.")


async def observe(ws, seconds=0.5):
    """跑一会儿 handler, 看它**自己**有没有退出、空转了多少圈。"""
    task = asyncio.create_task(main_mod.ws_endpoint(ws))
    await asyncio.sleep(seconds)
    finished = task.done()
    calls = ws.recv_calls
    if not task.done():
        task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass
    return finished, calls


async def main():
    print("── 场景 1: 页面一上来就断了 ──")
    ws = FakeWS(disconnect_after=0)
    finished, calls = await observe(ws)
    check(finished, "handler 自己退出了(没在原地空转)")
    check(calls <= 3, f"receive_text 只调了 {calls} 次(空转的话是成千上万次)")
    check(len(runner.subscribers) == 0,
          f"订阅队列收干净了(现在剩 {len(runner.subscribers)} 个)")

    print("── 场景 2: 页面先正常待着, 之后才断 ──")
    ws = FakeWS(disconnect_after=1)
    finished, calls = await observe(ws)
    check(finished, "handler 自己退出了(没在原地空转)")
    check(calls <= 4, f"receive_text 只调了 {calls} 次")
    check(len(runner.subscribers) == 0,
          f"订阅队列收干净了(现在剩 {len(runner.subscribers)} 个)")

    print("── 场景 3: 连续断开 5 次, 一次都不许留下空转的 handler ──")
    before = len(runner.subscribers)
    for i in range(5):
        ws = FakeWS(disconnect_after=0)
        finished, calls = await observe(ws, seconds=0.2)
        if not finished:
            check(False, f"第 {i + 1} 次断开后 handler 没退出")
            break
    else:
        check(True, "5 次断开, handler 每次都退出了")
    check(len(runner.subscribers) == before,
          f"5 次断开后订阅队列没涨(涨了 {len(runner.subscribers) - before} 个)")

    print()
    if FAILS:
        print(f"✗ {len(FAILS)}/{PASSES + len(FAILS)} 项没过:")
        for f in FAILS:
            print(f"    – {f}")
        return 1
    print(f"✓ 全部通过 ({PASSES} 个)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
