"""回归测试: U 盘版打包链的「**基线闭包**」那道闸(2026-11-11 补上的)。

起因:
    `scripts/build_deb.sh` 那条链**早就有**一道闭包闸(`deb/check_runtime_closure.py`
    + `deb/image-baseline.txt`), 而 **U 盘版打包链没有对等的那道** ⇒
    「这个库在**别的板子**上还找得到吗」这件事, U 盘版只查了
    「**能不能靠写死的绝对路径**找到」(RUNPATH 那一档) ⇒
    开发期 `apt install fio` 拖进来的 ceph/gluster 那一族(`librados2` /
    `librbd1` / `libtirpc3` / `libgfapi0` / `libceph-common` …)
    **根本不在出厂镜像里**, 却在打包机的 `/lib` 里好好地躺着
    ⇒ 判成「出厂镜像提供」⇒ **假绿**
    ⇒ 换台没装 ceph 的板子 `fio rc=127` ⇒ eMMC/TF/USB 三项全废
    ⇒ **好板子被返修**(2026-11-06 实测就是这个坑, SN A10010000000058)。

    修法: 判据改成 **出货镜像基线** —— 提供这个库的**包**在基线里
    才算「出厂镜像提供」; `check_usb_selfcontained.py` 已经按这个改了,
    这道闸**跟着它**就自动成了基线闭包闸。

    ⚠️ 但**判据本身也得有前提**: 基线读不到 / 认不出归属包 ⇒
    **判不了** ⇒ 这一关**没判** ⇒ 停手, 绝不能出一个「没判过」的镜像
    (判据缺一半 = 假绿 = 坏镜像出厂)。

这个测试守四件事(全是**静态**检查):
  ① 构建脚本引用了**出货镜像基线**(`deb/image-baseline.txt`);
  ② 基线**不在** ⇒ `die`(停手), 不是 `warn`;
  ③ 检查脚本的退出码 **3(下不了结论)**跟 **1(真问题)**分开处理
     —— 3 单独给一条「判据不全」的 die, 别把它说成「板子坏了」;
  ④ 检查脚本**压根不在** ⇒ 也 `die`(以前是 warn ⇒ 坏镜像照样出厂)。

跑法: .venv/bin/python -m tests.test_usb_image_baseline_gate
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUILD_REL = os.path.join("scripts", "build_usb_image.sh")
# 变异脚本会把**改坏的那一份**指过来(AGING_BUILD_USB_SH)。
BUILD_PATH = os.environ.get("AGING_BUILD_USB_SH",
                             os.path.join(REPO, BUILD_REL))

FAILS = []
PASSES = 0


def check(cond, msg, extra=""):
    global PASSES
    if cond:
        PASSES += 1
        print("  ✓ " + msg)
    else:
        FAILS.append(msg)
        print("  ✗ " + msg + ("  → " + str(extra) if extra else ""))


def main():
    path = BUILD_PATH
    with open(path, encoding="utf-8") as f:
        src = f.read()

    print("① 构建脚本引用了出货镜像基线")
    check("deb/image-baseline.txt" in src,
          "构建脚本里认得 `deb/image-baseline.txt`(基线在仓库根的 deb/ 下)")
    m = re.search(r'USB_BASELINE="([^"]+)"', src)
    check(m is not None, "基线路径是个变量(后面好几处要用)", "找不到 USB_BASELINE=")
    if m:
        check("deb/image-baseline.txt" in m.group(1),
              "基线路径指向 deb/image-baseline.txt", m.group(1))

    print("② 基线不在 ⇒ die(停手), 不是 warn")
    # 「基线不在」那个分支里必须有 die
    idx = src.find('if [ ! -f "$USB_BASELINE" ]; then')
    check(idx >= 0, "找得到「基线不在」那个判断", src[:200])
    if idx >= 0:
        seg = src[idx:idx + 800]
        check("die " in seg, "那个分支里是 die", seg[:200])
        check("warn " not in seg.split("fi")[0],
              "那个分支里**没有** warn(只警告 = 坏镜像照样出厂)",
              seg.split("fi")[0][:200])

    print("③ 退出码 3(下不了结论)跟 1(真问题)分开处理")
    check('if [ "$_rc" = "3" ]; then' in src,
          "单独判了退出码 3(= 下不了结论)", "没看到对 3 的单独判断")
    seg3 = src[src.find('if [ "$_rc" = "3" ]; then'):][:800]
    check("die " in seg3.split("elif")[0],
          "3 ⇒ die(停手, 不出没判过的镜像)", seg3.split("elif")[0][:200])
    check("下不了结论" in seg3,
          "3 那条消息说的是「下不了结论」, 不是「板子坏了」", seg3[:200])
    check('elif [ "$_rc" != "0" ]; then' in src,
          "真问题(非 0)另有一条 die")
    check(src.find('if [ "$_rc" = "3" ]; then')
          < src.find('elif [ "$_rc" != "0" ]; then'),
          "3 的判断在「真问题」之前(先分清是哪种)")

    print("④ 检查脚本压根不在 ⇒ 也 die")
    idx2 = src.find('if [ -f "$PROJECT_ROOT/scripts/check_usb_selfcontained.py" ]; then')
    check(idx2 >= 0, "找得到「检查脚本在不在」那个判断")
    if idx2 >= 0:
        else_seg = src[src.find("else", idx2):]
        else_seg = else_seg[:else_seg.find("\nfi") + 6]
        check("die " in else_seg,
              "那个 else 分支里是 die(以前是 warn ⇒ 坏镜像照样出厂)",
              else_seg[:200])

    print("\n" + "─" * 60)
    if FAILS:
        print("✗ %d 项失败 / 共 %d 项" % (len(FAILS), PASSES + len(FAILS)))
        for f in FAILS:
            print("   · " + f)
        return 1
    print("✓ 全部通过 (%d 项)" % PASSES)
    return 0


if __name__ == "__main__":
    sys.exit(main())
