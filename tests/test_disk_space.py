"""回归测试: **盘上空间不够时才提醒**, 够的时候**一个字都不显示**。

**起因 (2026-11-13 工人提的)**
    U 盘既是**启动盘**又是**数据根** —— 报告 / 历史库 / 日志全落在它上面。
    盘写满时的失败是**静默丢数据**那一种:
      · 报告 PDF 导出失败 —— 页面会报错, 工人**看得见**;
      · **`history.db` 写不进去** ⇒ 那一轮的结果**记不进库**, 极端情况把库
        写坏(库坏了有自愈, 但那一轮数据还是没了)。
    ⇒ 板子明明测了, **记录却没了** ⇒ 在**开跑之前**提醒一句。

**用户定的规矩 (一条都不许违反)**
    · **只在空间不足的时候显示** —— 空间充足时**任何地方都不显示提醒**
      (不许挂「剩余 54 GB」那种仪表盘: 工人不看, 看了也只会麻木);
    · **只管 U 盘版** —— 本地/设备版部署**不要**这条逻辑 ⇒ 设备版
      **一条提醒都不出**(哪怕设备自己的盘快满了);
    · **提醒, 不拦截** —— 空间不够也照样能开测(盘真满了还拦着不让测,
      那是另一种事故: 板子白烧一轮);
    · **读不到空间就当作够** —— 绝不能因为读不到就把测试搞挂;
    · **第一句先讲「现在还能不能测」** —— 界面提示别吓人:
      工人读到「磁盘不足」会以为这根盘废了。

**这个测试不碰真库、不碰真硬件**: 数据目录换成临时目录, 空间用假的
`disk_usage` 造出来; 「是不是 U 盘版」也是假的(直接换掉
`server/tests/_tools.py::usb_mode` ⇒ 判据那条路本身照旧走)。

跑法: .venv/bin/python -m tests.test_disk_space
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests._tmp import mkdtemp                         # noqa: E402

DATA_DIR = mkdtemp(prefix="aging-disk-space-")
os.environ["AGING_DATA_DIR"] = DATA_DIR

from server import disk_space                          # noqa: E402
from server.tests import _tools as _tools_mod          # noqa: E402

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

FAILS = []
PASSES = 0


def check(cond, msg, extra=""):
    global PASSES
    if cond:
        PASSES += 1
        print("    ✓ " + msg)
    else:
        FAILS.append(msg)
        print("    ✗ " + msg + ("  → " + str(extra) if extra else ""))


def section(title):
    print("\n" + title)


def with_free(fake_mb, fn, usb=True):
    """把可用空间换成假的, 并把「是不是 U 盘版」也换成假的, 跑完还原。

    ⚠️ 换的是 `server/tests/_tools.py::usb_mode` —— 也就是**判据那条路
       本身**(环境变量 / 盘根标记文件)照旧走, 只是它的**结论**被顶掉了。
       ⇒ 「设备版不提醒」这条规矩验的是真逻辑, 不是验一个假开关。
    """
    orig = disk_space.free_space_mb
    orig_mode = _tools_mod.usb_mode
    disk_space.free_space_mb = lambda path=None: fake_mb
    _tools_mod.usb_mode = lambda: usb
    try:
        return fn()
    finally:
        disk_space.free_space_mb = orig
        _tools_mod.usb_mode = orig_mode


# ══════════════════════════════════════════════════════════════════════
section("【1】读空间: 读得到有数, 读不到就是 None(不当成 0)")
# ══════════════════════════════════════════════════════════════════════
free = disk_space.free_space_mb()
check(isinstance(free, float) and free > 0, "读得到可用空间", free)
check(disk_space.free_space_mb("/no/such/path/xyz") is None,
      "路径不存在 ⇒ None(不是 0 —— 0 会被当成「满了」)")
# 0 是个危险的假答案: 「读不到」跟「一点空间都没有」必须分得开
check(disk_space.free_space_mb("/no/such/path/xyz") != 0,
      "「读不到」跟「没空间」是两回事")

# ══════════════════════════════════════════════════════════════════════
section("【2】空间够 ⇒ **一个字都不显示**")
# ══════════════════════════════════════════════════════════════════════
for free_mb in (54 * 1024, 50000, 2000, disk_space.WARN_FREE_MB):
    got = with_free(free_mb, lambda: disk_space.disk_warning())
    check(got is None, f"可用 {free_mb} MB ⇒ 不提醒", got)

# 差一兆也要算「够」(边界在 WARN 下面)
got = with_free(disk_space.WARN_FREE_MB - 0.1, lambda: disk_space.disk_warning())
check(got is not None, "差一点点到阈值 ⇒ 开始提醒", got)

# ══════════════════════════════════════════════════════════════════════
section("【3】读不到空间 ⇒ 当作够(绝不能因为读不到就乱喊)")
# ══════════════════════════════════════════════════════════════════════
got = with_free(None, lambda: disk_space.disk_warning())
check(got is None, "读不到 ⇒ 不提醒(不吓工人)", got)

# ══════════════════════════════════════════════════════════════════════
section("【4】空间不够 ⇒ 提醒, 而且说清「还能测」")
# ══════════════════════════════════════════════════════════════════════
got = with_free(500, lambda: disk_space.disk_warning())
check(got is not None, "500 MB ⇒ 提醒")
check(got["level"] == "warn", "500 MB ⇒ 一般的提醒", got)
check("还能正常测" in got["message"], "第一句讲「现在还能正常测」", got["message"])
check("还能存" in got["message"], "说清「还能存几台」", got["message"])
check(got["devices_left"] == 294,
      "还能存几台算对了(500 MB ÷ 1.7 MB/台 = 294 台)", got["devices_left"])
check(got["free_mb"] == 500, "可用空间带上了", got["free_mb"])
# ⚠️ 每台设备占多少是**实测**出来的(2026-11-12, 64G 盘): 报告 PDF 两份
#   ≈ 1.56 MB + 历史库 ≈ 0.03 MB + 日志 ≈ 0.03 MB ⇒ 约 1.7 MB/台。
#   这个数是给工人看的「还能存几台」的分母 —— 改了它, 工人看到的数就错了。
check(disk_space.PER_DEVICE_MB == 1.7,
      "每台占 1.7 MB(实测值, 改了工人看到的数就错了)",
      disk_space.PER_DEVICE_MB)

got = with_free(100, lambda: disk_space.disk_warning())
check(got["level"] == "critical", "100 MB ⇒ 升级措辞", got)
check("记不下来" in got["message"], "升级后说清「结果可能记不下来」", got["message"])
check("还能正常测" in got["message"], "升级后**仍然**先讲「还能正常测」", got["message"])

# ══════════════════════════════════════════════════════════════════════
section("【5】**只管 U 盘版** —— 设备版/本地部署一条提醒都不出")
# ══════════════════════════════════════════════════════════════════════
# 用户原话: 「主要就是要在 U 盘加上这个, 本地部署不需要这条逻辑」
# ⇒ 设备版的数据在设备自己的系统盘上, 不归这条提醒管 ⇒ 设备版**一个字都不出**,
#    哪怕设备那个盘已经快满了。
for free_mb in (500, 100, 5, 0.5):
    got = with_free(free_mb, lambda: disk_space.disk_warning(), usb=False)
    check(got is None, f"设备版: 可用 {free_mb} MB ⇒ 也不提醒", got)

# U 盘版同一组数 ⇒ 提醒(别把这条规矩做过头了)
for free_mb in (500, 100):
    got = with_free(free_mb, lambda: disk_space.disk_warning(), usb=True)
    check(got is not None, f"U 盘版: 可用 {free_mb} MB ⇒ 提醒", got)
    check(got is not None and "U 盘" in got["message"],
          f"U 盘版的文案里说的是「U 盘」(不是「数据盘」)", got["message"] if got else None)

# 判据本身: 环境变量 / 盘根标记文件 ⇒ 取或(跟 shell 脚本同一个判据)
import importlib                                             # noqa: E402
_tools_mod2 = importlib.reload(_tools_mod)
_olds = os.environ.get("AGING_TOOL_DIR")
try:
    os.environ["AGING_TOOL_DIR"] = "/tmp/fake-tool-dir"
    check(_tools_mod2.usb_mode() is True,
          "判据之一: 环境变量 AGING_TOOL_DIR 在 ⇒ 认成 U 盘版")
    os.environ.pop("AGING_TOOL_DIR", None)
    check(_tools_mod2.usb_mode() is False,
          "仓库里没有盘根标记 ⇒ 本地跑不算 U 盘版(所以设备版不提醒)")
finally:
    if _olds is not None:
        os.environ["AGING_TOOL_DIR"] = _olds
    else:
        os.environ.pop("AGING_TOOL_DIR", None)

# ══════════════════════════════════════════════════════════════════════
section("【6】措辞不许吓人 —— 不许出现「不能测 / 已满 / 废了」")
# ══════════════════════════════════════════════════════════════════════
for free_mb in (500, 100, 5):
    msg = with_free(free_mb, lambda: disk_space.disk_warning())["message"]
    for bad in ("不能测", "已满", "废了", "报废", "坏盘", "禁止测试"):
        check(bad not in msg, f"{free_mb} MB 的文案里没有「{bad}」", msg)

# ══════════════════════════════════════════════════════════════════════
section("【7】**提醒, 不拦截** —— 空间不够也照样能开测")
# ══════════════════════════════════════════════════════════════════════
main_src = open(os.path.join(HERE, "server", "main.py"), encoding="utf-8").read()
check("res = await runner.start(config)" in main_src,
      "开跑**先**开跑, 再查空间(不拿空间当门槛)")
check('res["disk"] = disk_space.disk_warning()' in main_src,
      "开跑这一刻再查一次空间")
check('"ok": False' not in main_src.split('res = await runner.start(config)')[1][:400],
      "查空间**不会**把开跑改成失败")

# ══════════════════════════════════════════════════════════════════════
section("【8】接线: 页面加载时查一次 + 开跑时再查一次")
# ══════════════════════════════════════════════════════════════════════
check('"disk": disk_space.disk_warning(),' in main_src,
      "/api/config 里带着 disk 字段(够的时候是 null)")
check(main_src.count("disk_space.disk_warning()") == 2,
      "一共查两处: /api/config + 开跑那一刻",
      main_src.count("disk_space.disk_warning()"))

app_js = open(os.path.join(HERE, "static", "app.js"), encoding="utf-8").read()
check("diskWarning" in app_js, "前端有 diskWarning 这个状态")
check(app_js.count("diskWarning.value = data.disk || null;") == 2,
      "前端两处都接了后端的 disk 字段",
      app_js.count("diskWarning.value = data.disk || null;"))

index_html = open(os.path.join(HERE, "static", "index.html"), encoding="utf-8").read()
check("v-if=\"diskWarning\"" in index_html,
      "模板里**只在有值时**才渲染(v-if ⇒ 没有值就整块不出现)")
check("diskWarning.message" in index_html, "模板渲染的是后端出的那句话")

print("\n" + ("全部通过" if not FAILS else f"有失败项 ({len(FAILS)})"))
sys.exit(0 if not FAILS else 1)
