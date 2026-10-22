"""FastAPI 主入口"""
import asyncio
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Optional, List

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import Config
from . import db
from .runner import runner
from .tests.registry import get_all_tests

app = FastAPI(title="工厂老化测试工具", version="0.1.0")

PROJECT_ROOT = Config.project_root()


# ==================== 启动事件 ====================
@app.on_event("startup")
async def startup():
    Config.load()
    await db.Database.init()
    print(f"[server] 项目根目录: {PROJECT_ROOT}")
    asyncio.create_task(_idle_watchdog())


# ==================== 静态资源 ====================
app.mount("/static", StaticFiles(directory=str(PROJECT_ROOT / "static")), name="static")


@app.get("/")
async def index():
    return FileResponse(PROJECT_ROOT / "static" / "index.html")


# ==================== 健康检查 ====================
@app.get("/api/health")
async def health():
    import os as _os
    from .tests._tools import sudo_available
    return {
        "ok": True,
        "status": runner.status,
        # §11.2: 服务以普通用户身份启动, 需 root 的调用在测试项内部走命令级
        # sudo -n。sudo_available=false 时 UI 顶部红色 banner 阻断测试。
        "sudo_available": sudo_available(),
        "is_root": _os.geteuid() == 0,
    }


@app.get("/api/deps/check")
async def deps_check():
    """检查系统工具是否齐全 (供 UI 在点开始前 preflight)"""
    import shutil as _shutil
    # 搜索多个路径 (普通用户 PATH 可能不含 /sbin /usr/sbin)
    SEARCH_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    def _which(cmd: str) -> str:
        p = _shutil.which(cmd)
        if p:
            return p
        for d in SEARCH_PATH.split(":"):
            import os
            full = os.path.join(d, cmd)
            if os.path.isfile(full) and os.access(full, os.X_OK):
                return full
        return ""

    # §11.7 打包必装清单 —— 缺任一即打包缺陷, 拒绝出货
    # §6.6 改版已删除 iperf3 测速, 网口只测 链路+DHCP+出网, 故不再列入
    REQUIRED = {
        "stress-ng": "CPU 压力",
        "glmark2-es2": "GPU 渲染 (glmark2-es2-wayland)",
        "fio": "存储 读写 (USB/eMMC/TF)",
        "memtester": "内存",
        "ethtool": "网口 链路",
        "ping": "网口 ping (iputils-ping)",
        "udhcpc": "网口 DHCP (busybox)",
        "iw": "WiFi 扫描 (nl80211)",
        "rfkill": "WiFi 射频屏蔽检查 (独立包)",
        "ip": "通用 (iproute2)",
        "lsusb": "USB 枚举 (usbutils)",
        "lsblk": "块设备枚举 (util-linux)",
        "modetest": "HDMI 检测 (libdrm-tests)",
    }
    items = []
    for cmd, label in REQUIRED.items():
        path = _which(cmd)
        items.append({
            "name": cmd,
            "label": label,
            "ok": path != "",
            "path": path,
        })
    all_ok = all(x["ok"] for x in items)
    CRITICAL_BINS = (
        "stress-ng", "glmark2-es2", "fio", "memtester", "ethtool",
        "ping", "udhcpc", "iw", "rfkill", "modetest",
    )
    critical_missing = [
        x["name"] for x in items
        if not x["ok"] and x["name"] in CRITICAL_BINS
    ]
    return {
        "all_ok": all_ok,
        "critical_missing": critical_missing,
        "items": sorted(items, key=lambda x: (x["ok"], x["name"])),
        "fix_command": "sudo bash scripts/install_deps.sh",
    }


# ==================== 配置 & 测试列表 ====================
@app.get("/api/config")
async def get_config():
    """返回前端需要的配置 + 测试项元信息"""
    from .runner import RECOMMENDED_ORDER
    defaults = Config.get("defaults", {})
    return {
        "defaults": defaults,
        "tests": get_all_tests(),
        "interfaces": Config.get("interfaces", {}),
        "recommended_order": RECOMMENDED_ORDER,
    }


# ==================== 测试运行 ====================
class StartRequest(BaseModel):
    tests: dict                     # {"cpu": {"enabled": true, "duration_sec": 60}, ...}
    order: Optional[List[str]] = None  # §7.6 用户拖动后的执行顺序 (空则用推荐序)


@app.post("/api/test/start")
async def test_start(req: StartRequest):
    config = {
        "tests": req.tests,
        "order": req.order,
        "project_root": str(PROJECT_ROOT),
    }
    # 也把整个 yaml config 灌进去
    config.update(Config.all())
    return await runner.start(config)


@app.post("/api/test/stop")
async def test_stop():
    return await runner.stop()


@app.get("/api/test/status")
async def test_status():
    return runner.snapshot()


# ==================== 关闭服务 (UI 按钮) ====================
# 工人 / 非专业用户不会敲 `bash start.sh stop` —— 界面上必须有个能关服务的
# 按钮, 否则服务会一直留着占住 8000 端口, 下次点桌面图标就打不开。
_PID_FILE = PROJECT_ROOT / "logs" / "server.pid"
_LOG_FILE = PROJECT_ROOT / "logs" / "server.log"


@app.post("/api/shutdown")
async def shutdown(request: Request):
    """关掉自己 —— UI 上「⏻ 关闭服务」按钮的落点。"""
    # 服务绑在 0.0.0.0, 同网段任何人都能打到这个接口。关服务比「开始/停止测试」
    # 危险得多 (它会把整台机器上的服务弄没), 所以默认**只允许本机操作**。
    # 产线确实要用平板远程关的话, 在 config.yaml 的 `service:` 里开
    # allow_remote_shutdown —— 那是显式的取舍, 不该是默认。
    if not (Config.get("service", {}) or {}).get("allow_remote_shutdown", False):
        peer = request.client.host if request.client else ""
        if not (peer.startswith("127.") or peer in ("::1", "localhost")):
            raise HTTPException(
                403,
                f"「关闭服务」只允许从本机操作 (当前来源 {peer or '未知'})。"
                "产线需远程关闭请在 config.yaml 设 service.allow_remote_shutdown: true",
            )
    if runner.status == "running":
        # 测试子进程是 §11.2 命令级 sudo 起的 root 进程, 服务一死就再没人管
        # 它们, 只能各自等 `timeout` 到点 —— 一次烧机会留成孤儿进程继续烧
        # CPU/内存, 结果也丢了。所以必须先「停止」再关。
        raise HTTPException(
            409, "测试正在运行, 请先点「停止」, 等测试结束后再关闭服务"
        )
    asyncio.create_task(_exit_soon())
    return {"ok": True, "message": "服务正在关闭"}


async def _exit_soon(delay: float = 1.0):
    """把 HTTP 响应送出去之后再退。

    **先 delay**: 响应此刻还没写回 socket, 立刻退出会让前端的 fetch 抛
    "网络错误", 用户看到红色报错而不是干净的告别页。
    """
    await asyncio.sleep(delay)
    _do_exit("收到 UI 关闭请求, 服务退出")


def _do_exit(reason: str):
    """服务唯一的退出路径: 记一行日志 → 清 PID 文件 → 退。

    **用 os._exit 而不是发信号**: 调用到这里已经确认没有测试在跑, 没有正在
    写的数据库、没有要收尾的子进程, 优雅关闭那套仪式对这里没有意义, 直接退
    干净即可。也不需要 SIGKILL 兜底 —— uvicorn 收到 SIGTERM 卡在
    "Waiting for background tasks" 不退的那个毛病, 在这里绕开了。
    """
    pid = os.getpid()
    line = f"{reason} (PID {pid})"
    print(line, flush=True)
    try:
        with open(_LOG_FILE, "a") as f:
            f.write(f"[{time.strftime('%H:%M:%S')}] {line}\n")
    except Exception:
        pass
    # 清 PID 文件, 别给 start.sh 留一个「以为还活着」的文件。只在记的确实
    # 是本进程时才删 —— 万一记的是别的进程, 宁可留给 service_ready 去判僵尸。
    try:
        if _PID_FILE.exists() and _PID_FILE.read_text().strip() == str(pid):
            _PID_FILE.unlink()
    except Exception:
        pass
    os._exit(0)


# ==================== 空闲自动关闭 (关掉浏览器 → 服务跟着退) ====================
# **不能直接拿「WebSocket 断开」当「浏览器关了」** —— 刷新页面 / 切后台 /
# 锁屏 / 网线掉了 / 浏览器崩溃, 在服务端看全是同一个信号: 连接没了, 分不出
# 来。而这个工具自己就会制造断连 (WiFi 测试扫频切接口、网口测试重配 DHCP),
# 照直做会把跑到一半的烧机直接杀掉, 结果全丢。
#
# 所以做成「心跳 + 宽限期 + 只在空闲时才退」:
#   · 页面每 20s 经 WS 发一个 ping。后台标签页最坏被 Chrome 节流到 1min 一次,
#     对 10 分钟的窗口来说完全够。
#   · 心跳断了、**且当前没有测试在跑**、且静默超过 idle_exit_sec → 退出
#   · 心跳断了、**但正在跑测试** → 绝不退出, 等测试跑完再说
#   · 页面关窗时用 sendBeacon 主动告别 → 走快通道, goodbye_grace_sec 后就退
_SERVICE_T0 = time.time()
_last_seen = time.time()
_ever_connected = False
_goodbye_at = None            # Optional[float]


def _touch(connected: bool = False):
    """收到客户端活动 —— 刷新空闲计时, 并作废过期的「告别」快通道。

    **这里有个必须踩过的坑**: `pagehide` 在**刷新页面时同样会触发**, 所以
    「告别」标记不能一设了就长期有效 —— 否则工人每刷新一次页面, 就等于按下
    一枚 30 秒引信, 而页面明明活得好好的、心跳一直在发, 服务还是会自杀。
    实测就是这么挂的: 日志里退出行写「静默 2s」—— 心跳 2 秒前刚来过。
    """
    global _last_seen, _ever_connected, _goodbye_at
    _last_seen = time.time()
    if _goodbye_at is not None and _last_seen > _goodbye_at:
        # 告别之后**又**有活动 → 页面回来了, 那是刷新/切页, 不是真关窗。
        _goodbye_at = None
    if connected:
        _ever_connected = True


@app.middleware("http")
async def _count_http_activity(request, call_next):
    # 页面上任何一次 API 调用 (点开始/停止/看历史) 都算「人还在用」——
    # 即使 WebSocket 恰好断了, 也不该把人正在操作的会话判成空闲。
    _touch()
    return await call_next(request)


@app.post("/api/goodbye")
async def goodbye():
    """页面关窗时由 navigator.sendBeacon 调 —— 走快通道, 不用等满 10 分钟。

    sendBeacon 在页面卸载时也能发出去, 但**刷新页面同样会触发**, 所以它只能
    在「此后一直没有任何活动」的前提下缩短宽限期 (见 _touch / _idle_watchdog)。
    """
    global _goodbye_at
    _touch()                  # 算作一次活动, 把 _last_seen 推到当下
    _goodbye_at = _last_seen  # 盖在刚才那一刻上 —— 之后任何活动都会作废它
    return {"ok": True}


async def _idle_watchdog():
    """定时看一眼: 现在该不该退。时长全部可配 (config.yaml 的 `service:`)。"""
    cfg = Config.get("service", {}) or {}
    idle_exit = int(cfg.get("idle_exit_sec", 600))
    goodbye_grace = int(cfg.get("goodbye_grace_sec", 30))
    never_connected_exit = int(cfg.get("never_connected_exit_sec", 3600))
    tick = 15

    print(
        f"[server] 空闲自退已启用: 无活动 {idle_exit}s 退; "
        f"关窗告别 {goodbye_grace}s 退; 测试运行中永不退",
        flush=True,
    )
    while True:
        await asyncio.sleep(tick)
        # ① 测试在跑 → 永不退出。这是硬红线: 一次 8 小时烧机不能被心跳误杀。
        if runner.status == "running":
            continue
        now = time.time()
        if not _ever_connected:
            # 起了服务但没人连上来 (比如 xdg-open 失败) —— 给足 1 小时, 别刚
            # 启动就被当成空闲杀掉。
            deadline = _SERVICE_T0 + never_connected_exit
        elif _goodbye_at is not None and _last_seen <= _goodbye_at:
            # 快通道成立的前提是「告别之后一直没有任何活动」。心跳/请求一旦
            # 刷新过 _last_seen, 就说明页面回来了 —— 这时必须走 idle 分支,
            # 绝不能让一条过期的告别标记压过活跃的心跳。
            deadline = _goodbye_at + goodbye_grace
        else:
            deadline = _last_seen + idle_exit
        if now >= deadline:
            _do_exit(f"页面已关闭且无测试在跑, 空闲自动退出 (静默 {int(now - _last_seen)}s)")


# ==================== 重测 / mini-run (§7.7) ====================
class RetestRequest(BaseModel):
    run_id: int
    test_names: List[str]


@app.post("/api/test/retest")
async def test_retest(req: RetestRequest):
    """重测指定的 test_names 项, 生成 mini-run, 结果就地更新回原 run."""
    return await runner.start_mini(req.run_id, req.test_names)


# ==================== 历史 ====================
@app.get("/api/history")
async def history(limit: int = 50):
    return await db.Database.list_runs(limit)


@app.get("/api/history/{run_id}")
async def history_detail(run_id: int):
    data = await db.Database.get_run(run_id)
    if not data:
        raise HTTPException(404, "记录不存在")
    return data


# ==================== WebSocket ====================
@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    _touch(connected=True)   # 空闲自退的「有人在线」起点
    q = runner.subscribe()
    # 先发一帧当前快照
    await ws.send_json({"type": "snapshot", **runner.snapshot()})
    try:
        while True:
            # 同时检查 client 发来的消息 (心跳 ping / 停止)
            send_task = asyncio.create_task(_drain_queue(ws, q))
            recv_task = asyncio.create_task(ws.receive_text())
            done, pending = await asyncio.wait(
                [send_task, recv_task],
                return_when=asyncio.FIRST_COMPLETED
            )
            for t in pending:
                t.cancel()

            if recv_task in done:
                try:
                    msg = json.loads(recv_task.result())
                    # 收到什么帧都算「页面还活着」, ping 只是为了在没有别的
                    # 消息流的时候 (比如盯着总结页发呆) 维持心跳。
                    _touch()
                    if msg.get("type") == "stop":
                        await runner.stop()
                except Exception:
                    pass
            else:
                # send_task 完成,继续循环
                pass
    except WebSocketDisconnect:
        pass
    finally:
        runner.unsubscribe(q)


async def _drain_queue(ws: WebSocket, q: asyncio.Queue):
    try:
        while True:
            event = await q.get()
            await ws.send_json(event)
    except Exception:
        return
