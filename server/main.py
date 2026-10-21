"""FastAPI 主入口"""
import asyncio
import json
import os
import subprocess
from pathlib import Path
from typing import Optional, List

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
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
    q = runner.subscribe()
    # 先发一帧当前快照
    await ws.send_json({"type": "snapshot", **runner.snapshot()})
    try:
        while True:
            # 同时检查 client 发来的消息 (如停止)
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
