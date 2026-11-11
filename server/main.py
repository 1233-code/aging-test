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
from . import identity
from . import report
from . import boot_evidence
from . import clock
from . import usb_export
from .runner import runner
from .tests.registry import get_all_tests

app = FastAPI(title="工厂老化测试工具", version="0.1.0")

PROJECT_ROOT = Config.project_root()


# ==================== 启动事件 ====================
@app.on_event("startup")
async def startup():
    Config.load()
    await db.Database.init()
    # §9.3.4 时钟: 先判一次「系统时钟是不是倒退过」(板子硬复位 ⇒ 时钟被
    # 退回上次干净关机那一刻, 实测倒跳 42 天)。判完再起一个定期任务,
    # 每分钟把「最近一次见到的墙钟」存进锚点文件 ⇒ 硬复位最多丢一分钟,
    # 而不是 42 天。
    try:
        # ⚠️ 先开「维护锚点」再判定 —— 判定完要立刻把锚点写回去,
        #    否则这一分钟里硬复位了, 下次开机拿到的还是旧锚点。
        clock.enable_anchor()
        st = clock.init()
        if st["skew_detected"]:
            print(f"[server] ⚠ 系统时钟倒退 {st['skew_days']:.1f} 天 ⇒ "
                  "本轮时间戳用推算值(报告上会打横幅)")
    except Exception as e:
        print(f"[server] ⚠ 时钟判定失败(不影响测试): {e!r}")
    asyncio.create_task(_clock_anchor_saver())
    # §4.4: 启动时读一次 SN 常驻内存, UI 顶部整个会话都显示它。读不到不阻断,
    # 记 unknown 转黄提示 (§2.2) —— 停机出货的后果远大于一次读不到。
    sn = identity.init()
    print(f"[server] 项目根目录: {PROJECT_ROOT}")
    print(f"[server] 被测设备 SN: {sn}" + ("  ⚠ 读不到, 报告中将记 unknown"
                                        if sn == identity.UNKNOWN else ""))
    # §9.3: 上一轮服务若是**中途夭折**的(设备卡住把事件循环冻死 / 被 kill /
    # 断电), 那一轮会永远停在「运行中」—— 历史列表一直显示有测试在跑, 明明
    # 什么都没在跑(2026-10-28 实测: 挂了一整天, 工人以为测试还在跑)。
    #
    # ⚠️ **授权范围**(用户点名): 只处理 status='running' 的轮次, 以及它们
    #   父轮里同样卡在 running 的那一行; 只写 status / summary_json / error /
    #   updated_at 四个字段; **不删**任何数据、**不碰**已判定(passed/failed/
    #   skipped/stopped/error)的轮次、**不动**任何 PDF。
    #
    #   父轮属于第二条(「它们父轮里同样卡在 running 的那一行」): 只收**那一行**,
    #   父轮的轮级结论(status / error)一个字都不动 —— 夭折的是重测轮, 父轮
    #   自己好好地跑完了, 把「服务重启, 这一轮中途夭折」盖到父轮头上是
    #   **张冠李戴**; 父轮多半是**已判定**的轮次, 它的结论不该被顺手改掉。
    fixed = []
    try:
        stuck = await db.Database.list_interrupted_runs()
        if stuck:
            print(f"[server] ⚠ 有 {len(stuck)} 轮卡在「运行中」"
                  f"(上一轮服务中途夭折的) → 收尾")

        # ① **先**取「上一次启动」的日志并判软/硬 —— 收尾要用它决定
        #    「这一项是被板子硬复位打死的」还是「只是服务被人重启了」。
        #    硬复位 ⇒ 正在跑的那一项判 **failed**(被测设备不合格);
        #    软重启 / 看不出来 ⇒ 照旧 stopped(不冤枉板子, 也不冤枉工具)。
        #    一定要**在收尾之前**取: 收尾完再取, 就分不清是硬是软了。
        evidence_text = ""
        hard_reset = False
        if stuck and boot_evidence.capture_enabled():
            try:
                evidence_text = await boot_evidence.previous_boot_evidence()
                kind = boot_evidence.classify_evidence_text(evidence_text)
                # ── ① 第一条通道: 日志尾巴里看得见我们自己的测试 ──────
                if kind == "硬复位" and boot_evidence.our_run_at_death(evidence_text):
                    # 「板子被硬拉死」还不够 —— 还得**死的时候我们的测试
                    # 正在上面跑**。
                    # 「服务自己死了、板子后来才被硬拉死」也是「没有关机
                    # 序列」, 那种判成「板子不合格」就是冤枉好板子。
                    hard_reset = True
                    print("[server] 上一次是**硬复位**, 而且日志尾巴里看得见"
                          "我们自己的测试 ⇒ 正在跑的那一项判 failed"
                          "(板子被硬拉死的, 被测设备不合格)")
                elif kind == "软重启":
                    # ⚠️ **软重启不赖到板子头上** —— 有人/有服务执行了
                    #    reboot, 板子自己没崩 ⇒ 照旧 stopped(不冤枉板子)。
                    #    ⚠️ 这里**刻意不去查「加压现场记录」**: 工人/服务在
                    #    加压途中执行了 reboot, 现场记录同样是「起跑没结束」
                    #    ⇒ 拿它当证据 = 把一次人为重启说成板子不合格。
                    print("[server] 上一次是**软重启** ⇒ 照旧 stopped"
                          "(不冤枉板子)")
                else:
                    # ── ② 第二条通道: 加压现场记录(逐行 fsync, 硬复位带不走) ──
                    #    **什么时候都要查, 不管 journal 判出了什么**:
                    #      · 硬复位, 但**尾巴里没有**我们的进程 —— 硬复位时
                    #        journald 最后几秒还没落盘, 而那几秒**正好**是
                    #        「加压刚起手」的窗口(实测板子死在起手约 2 秒)
                    #        ⇒ 尾巴里连一条 stress-ng 都没有;
                    #      · **判不出软硬**(日志读不到 / 认不出来) —— 这时
                    #        唯一的证据就是它(§9.3 的规定: 判不出软硬 ⇒
                    #        **先走第二条通道**)。
                    #    ⇒ 现场记录里「起跑」在、「加压结束」不在 ⇒ 加压没跑完
                    #      ⇒ 板子死在加压途中 ⇒ 那一项判 failed。
                    #    ⚠️ 这一关自己还带着「**板子真的重启过没有**」的
                    #        护栏(见 boot_evidence.cpu_evidence_cut_short)
                    #        —— 服务被杀/被重启时现场记录也是「起跑没结束」,
                    #        那时候**不许**赖到板子头上(冤枉好板)。
                    cut = None
                    for r in stuck:
                        try:
                            if boot_evidence.cpu_evidence_cut_short(r.get("id")):
                                # ⚠️ **「判不出软硬」这一档要额外一道**:
                                #    日志读不到 ⇒ 我们**不知道**板子死没死过
                                #    ⇒ 光凭「现场记录起跑没结束」就判板子不合格
                                #    = **冤枉好板**(服务被杀时那份记录也一样)。
                                #    ⇒ 还要有「**板子真的重启过**」的**实证**
                                #      (开机 ID 变了 —— 跟时钟无关, 板子的墙钟
                                #      会倒退, 拿时间戳比是比不出来的)。
                                #    「硬复位」那一档不用加这道 —— journal 已经
                                #    证明板子确实重启过了。
                                if kind != "硬复位":
                                    if boot_evidence.board_rebooted_since_evidence(
                                            r.get("id")) is not True:
                                        continue
                                cut = r.get("id")
                                break
                        except Exception:
                            continue
                    if cut is not None:
                        hard_reset = True
                        print("[server] 上一次是**%s**, 但日志尾巴里看不见我们"
                              "自己的测试(硬复位时最后几秒没落盘), "
                              f"**加压现场记录**显示第 {cut} 轮的加压"
                              "起手了、没跑完 ⇒ 板子死在加压途中 ⇒ "
                              "正在跑的那一项判 failed(被测设备不合格)" % kind)
                    else:
                        print("[server] 上一次是**%s**, 但日志尾巴里看不见我们"
                              "自己的测试, 加压现场记录也看不出「起手了没跑完」"
                              " ⇒ 不赖到板子头上, 照旧 stopped" % kind)
            except Exception as e:
                print(f"[server] ⚠ 取上一次启动的日志失败(不影响收尾): {e!r}")

        # ② 收尾(只动 status='running' 的轮次和它们父轮里同样 running 的那一行)
        fixed = await db.Database.reconcile_interrupted_runs(hard_reset=hard_reset)
        for f in fixed:
            if f.get("is_parent"):
                print(f"[server] ⚠ 收掉父轮 #{f['id']} 里卡在「运行中」的那一项"
                      f"(重测轮中途夭折; 父轮自己的结论不动)")
            else:
                print(f"[server] ⚠ 收尾残留轮次 #{f['id']} → {f['status']} "
                      f"(上一轮服务中途夭折, 没跑完)")

        # ③ §9.3 板子重启的**死因**只留在「上一次启动」的日志里 —— 那一轮
        #    测试的日志跟死因一起没了, 报告上只剩一句「中途夭折」。
        #    这一步要去**读系统日志**, 所以得现场授权: config.yaml 里
        #    `report.capture_boot_evidence: true` 才开(默认关)。
        #    用**刚才取的那一份**, 不再捞第二次(捞两次内容可能不一样,
        #    而且白白多跑一次 journalctl)。
        if evidence_text:
            _save_boot_evidence(fixed, evidence_text)
        elif stuck and boot_evidence.capture_enabled():
            print("[server] ⚠ 捞不到上一次启动的日志(板子上没有持久化的 journal?)"
                  " —— 死因得自己上板子看: " + boot_evidence.evidence_hint())
    except Exception as e:
        print(f"[server] ⚠ 收尾残留轮次失败: {e!r}")
    asyncio.create_task(_idle_watchdog())


async def _clock_anchor_saver():
    """每分钟把「最近一次见到的墙钟」写进锚点文件; 每 5 分钟再让**板子
    自己**存一次时间 (§9.3.4)。

    **为什么必须定期存锚点**: 板子的系统时钟在硬复位后会被退回「上次干净
    关机」那一刻 —— 不留锚点, 下次开机就完全不知道现在该是几点。留了锚点,
    最坏只丢「关机前一分钟」那点时间。

    **为什么还要 `fake-hwclock save`(A1)**: 锚点是我们自己的兜底(产品侧),
    `fake-hwclock` 是**板子自己的**那份 —— 板子自己记住了, 开机时钟就不
    用退回 42 天前。两者一起上才稳。

    写失败就等下一轮 —— 锚点只是保险, 写不进去绝不能把服务搞挂。
    """
    tick = 0
    warned = False
    while True:
        try:
            await asyncio.sleep(60)
            clock.save_anchor()
            tick += 1
            if tick % 5 == 0:
                ok = await clock.save_hwclock()
                if ok is False and not warned:
                    warned = True
                    print("[server] ⚠ 没能让板子存时间(fake-hwclock save 失败)—— "
                          "多半是 sudoers 没给这条命令免密。板子硬复位后开机, "
                          "时钟会退回上次关机那一刻(§9.3.4)", flush=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass


def _save_boot_evidence(fixed: list, text: str) -> None:
    """把**已经取好的**「上一次启动」日志存成旁挂文件, 供报告渲染。

    只存**真的收尾了残留轮次**的那些轮次 —— 没有夭折就没有「上一次启动」
    的死因可存。

    父轮和重测轮**共用同一份**证据(同一次重启), 但各存一份 —— 导出
    哪一轮的报告就取哪一份, 不用去猜死的那一轮到底是哪一轮。
    """
    ids = [f["id"] for f in (fixed or []) if f.get("id")]
    if not ids or not text:
        return
    for rid in ids:
        path = boot_evidence.save_evidence(rid, text)
        if path:
            print(f"[server] 已存上一次启动的日志: {path}")


# ==================== 静态资源 ====================
app.mount("/static", StaticFiles(directory=str(PROJECT_ROOT / "static")), name="static")


@app.middleware("http")
async def no_cache_static(request: Request, call_next):
    """静态资源每次回源校验 (仍走 ETag/304, 带宽损失可忽略)。

    StaticFiles 不发 Cache-Control 时, 浏览器按启发式规则自行缓存, 可能
    完全不发 If-None-Match 就用本地旧副本 —— 导致改了 app.js 重启服务后
    页面仍跑旧逻辑, 且很难看出是缓存问题。
    """
    response = await call_next(request)
    if request.url.path.startswith(("/static", "/")):
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return response


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
        # §9.3.4 时钟状态 —— 产线上看一眼就知道「这台板子的时间靠不靠谱」
        "clock": clock.status(),
    }


#: 探针用的参数 —— `--version` 不是所有工具都认(glmark2 认 `--help`)。
_PROBE_ARGS = {
    "fio": ["--version"],
    "glmark2-es2": ["--help"],
    "glmark2-es2-wayland": ["--help"],
}


@app.get("/api/deps/check")
async def deps_check():
    """检查系统工具是否齐全 (供 UI 在点开始前 preflight)

    ⚠️ **必须用测试项同一套找工具的逻辑**(`find_tool`) ——
    2026-11-06 踩过: 这里原来自己写了一套 `_which()`, **只看系统 PATH** ⇒
    U 盘版里 fio / glmark2-es2 是**盘上**的(`tools/bin/fio`, 由
    `scripts/build_usb_image.sh` 生成的 wrapper), 系统 PATH 里压根没有 ⇒
    配置页一律报「缺失 fio」, 而测试项跑的时候用的是 `find_tool` ⇒ 找得到
    ⇒ **「配置页说缺、实际不缺」的假警报**。

    ⚠️ **「找到了」≠「起得来」** —— 盘上那份 fio 靠 LD_LIBRARY_PATH 找盘里的
    .so; 依赖没带齐时它是 rc=127(`error while loading shared libraries`)。
    所以对**盘上**的工具额外真跑一次探针: 起不来就在这里报出来, 而不是等
    存储项跑到一半才 error(2026-11-06 那轮 0058 的 eMMC 项就是这么 error
    的 —— fio 缺 `libceph-common.so.2`)。
    设备版(deb 装)不走探针 —— apt 装的依赖是自洽的, 没必要多起进程。
    """
    from .tests._tools import find_tool, tool_dir, usb_mode as _usb_mode, deps_fix_hint

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
        "xrandr": "HDMI 读当前实际输出 (x11-xserver-utils)",
    }
    CRITICAL_BINS = (
        "stress-ng", "glmark2-es2", "fio", "memtester", "ethtool",
        "ping", "udhcpc", "iw", "rfkill", "xrandr",
    )

    tdir = tool_dir()
    # ⚠️ **2026-11-10**: 判据从「`tool_dir()` 有没有值」改成 `usb_mode()`
    #    —— 那个函数认**两个**信号(环境变量 **或** 盘根目录的
    #    `AGING_USB_MODE` 标记)⇒ **跟两个 shell 诊断脚本是同一个判据**
    #    (以前两边各认各的 ⇒ 能同时对不上 ⇒ 盘上明明是 U 盘版,
    #    这里却按设备版给出那条**盘上并不存在**的命令)。
    usb_mode = _usb_mode()

    async def _probe(path: str, name: str) -> str:
        """真跑一次工具 —— 返回空串 = 起得来; 否则返回起不来的原因。

        ⚠️ 只用来抓「动态链接失败」这一类(rc=127 / 加载共享库报错),
        别的失败(比如 `--help` 退出码非 0)一律当作「起得来」。
        """
        argv = [path] + _PROBE_ARGS.get(name, ["--version"])
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except Exception:
            return ""      # 探针自己起不来 ≠ 工具坏了
        try:
            _, err = await asyncio.wait_for(proc.communicate(), timeout=5.0)
        except asyncio.TimeoutError:
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
            return ""      # 超时不算「起不来」(有些工具 --version 会等)
        except Exception:
            return ""
        text = (err or b"").decode("utf-8", "replace")
        rc = proc.returncode
        if rc == 127 or "error while loading shared libraries" in text:
            last = [ln for ln in text.strip().splitlines() if ln.strip()]
            return last[-1].strip() if last else f"rc={rc}"
        return ""

    items = []
    for cmd, label in REQUIRED.items():
        path = find_tool(cmd)
        on_stick = bool(tdir and path and str(path).startswith(str(tdir)))
        item = {
            "name": cmd,
            "label": label,
            "ok": bool(path),
            "path": path or "",
            # 这个工具是从哪儿来的: "stick"=U 盘上那份, "system"=板子出厂那份
            "source": ("stick" if on_stick else "system") if path else "",
        }
        if path and on_stick:
            reason = await _probe(path, cmd)
            if reason:
                item["ok"] = False
                item["broken"] = True
                item["broken_reason"] = reason
        items.append(item)

    all_ok = all(x["ok"] for x in items)
    critical_missing = [
        x["name"] for x in items
        if not x["ok"] and x["name"] in CRITICAL_BINS and not x.get("broken")
    ]
    critical_broken = [
        x["name"] for x in items
        if not x["ok"] and x["name"] in CRITICAL_BINS and x.get("broken")
    ]
    return {
        "all_ok": all_ok,
        "critical_missing": critical_missing,
        "critical_broken": critical_broken,
        "items": sorted(items, key=lambda x: (x["ok"], x["name"])),
        "mode": "usb" if usb_mode else "device",
        # 设备版: 给命令; U 盘版: **别**再让工人去 apt 装(装到板子上,
        # 拔盘就走, 白装) ⇒ 给一句人话。
        "fix_command": "" if usb_mode else "sudo bash scripts/install_deps.sh",
        # ⚠️ **2026-11-10**: 这里原来**另抄了一份** U 盘版那句话(还多个
        #    句号)⇒ 全仓同一句有**两份拷贝** ⇒ 改一处忘一处, 页面和
        #    报错说的就不是同一句话了 ⇒ 现在直接用**同一个函数**。
        "fix_hint": deps_fix_hint() if usb_mode else "",
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
        # §2.2 / §7.2: SN 常驻显示在页面顶部。跟着 /api/config 一起下发而不是
        # 单开一个接口 —— 页面本来就要拉它, 多一次请求没有收益。
        "sn": identity.get_sn(),
        # §8.2 本轮测试人员姓名的**默认值** —— 前端拿它预填输入框。
        # 工人可以每轮改; 不改就直接用这个值(多半就是当班的人)。
        "report": {
            "operator": (Config.get("report") or {}).get("operator") or "",
        },
        # 历史库的状态 —— 坏库被**改名隔离**过没有(见 server/db.py)。
        # ⚠️ **只作机器可查的记录, 界面上一个字不提**(2026-11-10 用户定):
        #    挂横幅会让工人以为「这根盘坏了不能用」—— 而盘其实好好的,
        #    只是**之前那批历史记录**读不出来了(旧文件还在 reports/ 里没删)。
        #    ⇒ 这件事只留在 logs/server.log 和文档里; 要查就 curl 这个接口。
        #    (前端不许再拿它渲染东西 —— 有 tests/test_frontend_history_db_silent.js 盯着)
        "history_db": db.history_db_status(),
    }


# ==================== 测试运行 ====================
class StartRequest(BaseModel):
    tests: dict                     # {"cpu": {"enabled": true, "duration_sec": 60}, ...}
    order: Optional[List[str]] = None  # §7.6 用户拖动后的执行顺序 (空则用推荐序)
    # §8.2 本轮测试人员**姓名**。每轮手输 —— 同一工位轮班时, 报告上得写当班
    # 的人, 不能是 config.yaml 里写死的那一个。留空则回落到 config.yaml 的
    # report.operator (前端会用它预填输入框, 所以多数时候其实不用手输)。
    tester_name: str = ""


@app.post("/api/test/start")
async def test_start(req: StartRequest):
    config = {
        "tests": req.tests,
        "order": req.order,
        "project_root": str(PROJECT_ROOT),
    }
    # 也把整个 yaml config 灌进去
    config.update(Config.all())
    # §8.2 本轮测试人员: **前端手填的优先**, 没填才回落到 config.yaml 的
    # report.operator。
    #
    # ⚠️ 必须放在 config.update() **之后**: update() 会用 yaml 里的整个 report
    # 段盖掉前面拼的 config, 提前写会被**静默冲掉** —— 表现是「填了姓名,
    # 报告里还是未填写」, 而且日志上一点异常都没有。
    rep = dict(config.get("report") or {})
    name = (req.tester_name or "").strip()
    if name:
        rep["operator"] = name
    config["report"] = rep
    return await runner.start(config)


@app.post("/api/test/stop")
async def test_stop():
    """「停止」: 中止**整轮** —— 当前项和后面所有项都记「跳过」, 直接跳结果页。"""
    return await runner.stop()


@app.post("/api/test/next")
async def test_next():
    """「下一项」: 只中止**当前这一项**(记「跳过」), 接着测下一项。"""
    return await runner.skip_current()


@app.get("/api/test/status")
async def test_status():
    return runner.snapshot()


# ── §7.3 等操作员: 卡槽空 / 网线没插时的「已插好重测」/「跳过」 ──
class OperatorAnswer(BaseModel):
    decision: str    # "retest" (已插好, 原地再跑一遍这一项) | "skip" (进下一项)


@app.post("/api/test/operator")
async def test_operator(req: OperatorAnswer):
    """唤醒正卡在 waiting_operator 的那一轮。

    **这个接口只做一件事: 通知后端停止等**。下一步是重测当前项还是直接
    进下一项, 由 runner 的主循环按 decision 决定 —— 前端不去猜「下一项是
    几号」, 也就不会再出现「屏幕跳到 USB、日志却在写 HDMI」这种错位。
    """
    return runner.answer_operator(req.decision)


# ==================== 关闭服务 (UI 按钮) ====================
# 工人 / 非专业用户不会敲 `bash start.sh stop` —— 界面上必须有个能关服务的
# 按钮, 否则服务会一直留着占住 8000 端口, 下次点桌面图标就打不开。
#: PID / 日志 —— 落**数据根**下 (打包后 /var/lib/aging-test/logs/)。
#: 服务是普通用户起的, 代码根(root 所有)写不进去。
_PID_FILE = Config.data_root() / "logs" / "server.pid"
_LOG_FILE = Config.data_root() / "logs" / "server.log"


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
async def history(limit: int = 50, include_mini: bool = False):
    """历史列表 —— **默认只列归档记录**(不含重测轮)。

    重测轮是**过程**, 不是归档对象(§7.7.4)。列出来的话, 工人重测两次
    就会看见**四条**带「重测」字样的行 —— 实际只重测过两条。带
    `?include_mini=1` 可以看原始行(排障用), 界面不用。
    """
    return await db.Database.list_runs(limit, include_mini=include_mini)


@app.get("/api/history/{run_id}")
async def history_detail(run_id: int):
    data = await db.Database.get_run(run_id)
    if not data:
        raise HTTPException(404, "记录不存在")
    return data


@app.get("/api/history/{run_id}/pdf")
async def history_pdf(run_id: int):
    """下载/预览已导出的 PDF。"""
    data = await db.Database.get_run(run_id)
    if not data:
        raise HTTPException(404, "记录不存在")
    path = data.get("pdf_path")
    if not path:
        raise HTTPException(404, "该轮次还没导出 PDF, 请点「重新导出」")
    # ⚠️ **还原成「现在真的存在」的那个文件** —— 库里可能存着**已经失效**的
    #    绝对路径(盘换了挂载点), 那样工人点「打开报告」就打不开。
    #    见 report.resolve_pdf_path()。
    p = report.resolve_pdf_path(path)
    if p is None:
        raise HTTPException(404, "PDF 文件已不在磁盘上, 请重新导出")
    # 库里的路径理论上只会是本工具写的, 但万一被手改过 —— 别让它变成一个
    # 能读 /etc/passwd 的任意文件下载口。
    #
    # 边界取 **report.output_dir()** —— 只有报告目录及其子目录可下载:
    #   · 默认 output_dir="reports" → 解析后是 <数据根>/reports/pdf, 落在数据根
    #     之内, 所以行为不变, 但比"项目根之内"**更严**(仓库里其他文件不会被
    #     这个端点读出去);
    #   · 产线把 output_dir 指到 U 盘/网络盘(绝对路径)归档时, 那个目录本身
    #     就是合法落盘位置, 用项目根当边界反而会把刚导出的 PDF 全 403 掉。
    # commonpath 两边都 resolve 过, 所以 <root>/../etc 那种绕不过去。
    #
    # 用 os.path.commonpath 而不是 Path.is_relative_to: 后者是 3.9+ 才有的,
    # 本板是 Python 3.8.10。
    root = str(report.output_dir().resolve())
    try:
        inside = os.path.commonpath([str(p.resolve()), root]) == root
    except ValueError:      # 跨不动路径时 commonpath 直接抛
        inside = False
    if not inside:
        raise HTTPException(403, "PDF 路径不在报告目录内")
    return FileResponse(str(p), media_type="application/pdf",
                        filename=p.name)


@app.post("/api/history/{run_id}/pdf")
async def history_reexport_pdf(run_id: int):
    """§8.2 手动重导 PDF。

    自动导出失败时(缺 chromium / 字体装晚了 / 当时磁盘满), 工人装完依赖就能
    在这一轮上补导, 不用重跑整轮老化。

    **会覆盖同轮次的旧 pdf_path** —— 但文件名叫的是同一台机器 + 收尾时间戳,
    同一轮重导出来的是同名文件, 内容一致, 不会在 reports/pdf/ 里堆一串同轮
    的重复件。
    """
    data = await db.Database.get_run(run_id)
    if not data:
        raise HTTPException(404, "记录不存在")
    # 老库里没有 sn 列(迁移前的记录) → 回读当前设备 SN, 至少别写成 null
    if not data.get("sn"):
        data["sn"] = identity.get_sn()
    results = data.pop("results", [])
    # §7.7 手动重导也要带上「重测日志附录」那一节 —— 不然手动补导出来的
    # 报告会**比**自动导的那份**少一节**, 读者会以为重测日志没了。
    retest_runs = await db.Database.get_retest_runs(run_id)
    try:
        path = await report.export_pdf(data, results, retest_runs=retest_runs)
    except report.ReportError as e:
        raise HTTPException(500, f"导出失败: {e}")
    await db.Database.set_pdf_path(run_id, path)
    # §8.2.3 手动补导的**也**要同步到 U 盘 —— 自动导出失败(缺 chromium /
    # 当时磁盘满)时, 工人装完依赖在这轮上补导, 报告同样该落到 U 盘上。
    # 同一轮重导是同一份报告的另一个版本 ⇒ INDEX.csv 里那按「SN+开始时间」
    # **替换**掉, 不会多出一行。
    usb_info = {"ok": False, "path": "", "message": ""}
    try:
        res = await usb_export.export_report_to_usb(path, data)
        usb_info = {"ok": res.ok, "path": res.directory, "message": res.message}
    except Exception as e:
        usb_info = {"ok": False, "path": "", "message": f"存 U 盘失败: {e!r}"}
    return {"ok": True, "path": path, "usb": usb_info}


# ==================== WebSocket ====================
@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    _touch(connected=True)   # 空闲自退的「有人在线」起点
    q = runner.subscribe()
    try:
        # 先发一帧当前快照
        await ws.send_json({"type": "snapshot", **runner.snapshot()})
        # ── 一个**常驻**的发送任务 ──
        #
        # ⚠️⚠️ **为什么不能每轮循环新建一个**: 上一轮那个发送任务在
        # `send_json()` 写到一半时被 cancel 掉, **已经从队列里取出来的那条
        # 事件就丢了** ⇒ 页面上偶尔少一条日志/一次进度。
        # 固定一个常驻任务, 页面断了再一起收。
        send_task = asyncio.create_task(_drain_queue(ws, q))
        try:
            while True:
                # 同时检查 client 发来的消息 (心跳 ping / 停止)
                recv_task = asyncio.create_task(ws.receive_text())
                done, _pending = await asyncio.wait(
                    [send_task, recv_task],
                    return_when=asyncio.FIRST_COMPLETED
                )
                if recv_task in done:
                    # ⚠️⚠️⚠️ **页面断了, 这里必须让异常出去** ——
                    #
                    # 页面一断, `ws.receive_text()` 立刻抛
                    # `WebSocketDisconnect`(starlette); 再下一轮它抛
                    # `RuntimeError`("disconnect 之后再 receive")。
                    # **两个都绝不能咽掉** —— 一旦咽掉, 这个 while 每一圈都
                    # 「秒完」⇒ **空转** ⇒ 一个 handler 吃满一整核
                    # (2026-11-02 实测: uvicorn 进程 99% CPU,
                    #  `/proc/<pid>/syscall` 一直是 `running`),
                    # 而且下面的 `finally: runner.unsubscribe(q)`
                    # **永远也走不到** —— 订阅队列跟着一起漏,
                    # 每断一次连接就多一个吃核的空转 handler。
                    #
                    # ⇒ 这就是「终端好好的, 一进应用就不行」的差别:
                    #    终端没有服务在吃核; 应用已经占住 1~2 个核,
                    #    CPU 项再加压时**整板满载** ⇒ 电流台阶 ⇒ 板子被拉死。
                    raw = recv_task.result()
                    try:
                        msg = json.loads(raw)
                    except (ValueError, TypeError):
                        continue        # 页面发来一帧看不懂的 ⇒ 忽略, 别断连接
                    # 收到什么帧都算「页面还活着」, ping 只是为了在没有别的
                    # 消息流的时候 (比如盯着总结页发呆) 维持心跳。
                    _touch()
                    if isinstance(msg, dict) and msg.get("type") == "stop":
                        try:
                            await runner.stop()
                        except Exception:
                            pass
                else:
                    # 发送任务先结束 ⇒ 页面已经没了(推不出去)⇒ 收工。
                    recv_task.cancel()
                    await _quiet(recv_task)
                    break
        finally:
            send_task.cancel()
            await _quiet(send_task)
    except WebSocketDisconnect:
        pass
    except Exception:
        # 页面断开时 starlette 抛的别的花样(RuntimeError / ClientDisconnected…)
        # 一律当成「页面没了」—— **关键是别咽了还继续转圈**。
        pass
    finally:
        runner.unsubscribe(q)


async def _quiet(task):
    """把一个任务收干净: 取消它, 并等它真的结束。

    **为什么**: 取消掉但不等它 ⇒ asyncio 会打印
    「Task was destroyed but it is pending!」, 日志里全是这种噪音,
    真正有用的报错反而被淹掉。
    """
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass


async def _drain_queue(ws: WebSocket, q: asyncio.Queue):
    try:
        while True:
            event = await q.get()
            await ws.send_json(event)
    except Exception:
        return
