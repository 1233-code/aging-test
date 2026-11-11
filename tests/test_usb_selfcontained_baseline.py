"""回归测试: 「出厂镜像提供」这个判据, 必须按**出货镜像基线**算(2026-11-11 查出来的)。

起因:
    `scripts/check_usb_selfcontained.py` 以前判断「这个库在别的板子上找得到吗」
    时, 「出厂镜像提供」这一档看的是**这台打包机**的 `/lib` 里有没有 ⇒
    **假绿**:

      · 打包机上开发期 `apt install fio` 拖进来的那一族库(librados2 /
        librbd1 / libtirpc3 / libgfapi0 / librdmacm1 / libceph-common …)
        在打包机的 `/lib` 里好好地躺着, 可它们**根本不在出厂镜像里**
        (不在 `deb/image-baseline.txt` 的基线里) ⇒ 判成「出厂镜像提供」
        ⇒ 「每个库在别的板子上都找得到」这句话是**假的**
      ⇒ 2026-11-06 实测就是这个坑: fio 硬链 `librbd.so.1`, 换台没装 ceph
        的板子 fio rc=127 ⇒ 存储项 error ⇒ **好板子被返修**。

    正确判据: 提供这个库的**包**在不在**出货镜像基线**里。

这个测试守三件事:
  ① 基线里的包提供的库 ⇒ 判「出厂镜像提供」;
  ② **不在**基线里的包提供的库 ⇒ 判「镜像里没有」(⇒ 算问题, 退出码非 0);
  ③ 判不出来(这台机器上没有 dpkg 信息)⇒ **说清楚判据偏松**, 不许闷着。

跑法: .venv/bin/python -m tests.test_usb_selfcontained_baseline
"""
import importlib.util
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL_REL = os.path.join("scripts", "check_usb_selfcontained.py")

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


def load_tool():
    # 变异脚本会把**改坏的那一份**指过来(AGING_USB_SELF_CONTAINED_TOOL)。
    path = os.environ.get("AGING_USB_SELF_CONTAINED_TOOL",
                          os.path.join(REPO, TOOL_REL))
    spec = importlib.util.spec_from_file_location("_chk_usb_selfc", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    tool = load_tool()

    print("① 基线读得到, 而且是一份真基线")
    baseline = tool.load_baseline()
    check(baseline is not None, "读得到出货镜像基线(deb/image-baseline.txt)")
    check(bool(baseline) and len(baseline) > 500,
          "基线里有上千个包", len(baseline or []))
    check(any(b.startswith("libc6") for b in (baseline or set())),
          "基线里有 libc6(包名带架构后缀, 形如 libc6:arm64)(自检)",
          "基线像是空的")

    print("② 判据: 看**提供这个库的包**在不在基线里")
    # 盘上没有; 这台机器的「标准目录」里有(假装它是板子上的库)
    board_index = {"libfoo.so.1": "/usr/lib/libfoo.so.1"}
    verdict, why = tool.classify_needed(
        "libfoo.so.1", {}, board_index, [],
        image_owner={"libfoo.so.1": "libfoo1"},
        baseline={"libc6", "libfoo1"})
    check(verdict == "board",
          "包**在**基线里 ⇒ 判「出厂镜像提供」", "%s / %s" % (verdict, why))
    check("libfoo1" in why, "说明里带上是哪个包提供的", why)

    verdict, why = tool.classify_needed(
        "libfoo.so.1", {}, board_index, [],
        image_owner={"libfoo.so.1": "libfoo1"},
        baseline={"libc6"})
    check(verdict == "notimage",
          "包**不在**基线里 ⇒ 判「镜像里没有」(以前会误判成「出厂镜像」)",
          "%s / %s" % (verdict, why))
    check("libfoo1" in why, "说明里点出是哪个包不在基线里", why)

    print("③ 认不出归属包 ⇒ 说清楚判据偏松, 不许闷着")
    verdict, why = tool.classify_needed(
        "libfoo.so.1", {}, board_index, [],
        image_owner={"libfoo.so.1": "libfoo1"},
        baseline=None)
    check(verdict != "board",
          "基线读不到 ⇒ 不许直接说「出厂镜像提供」", "%s / %s" % (verdict, why))
    check(verdict == "unknown",
          "基线读不到 ⇒ 归到「下不了结论」(unknown), **不是**「有问题」",
          "%s / %s" % (verdict, why))
    check("判据不全" in why,
          "说明里写了「判据不全」(不许闷着)", why)
    verdict, why = tool.classify_needed(
        "libfoo.so.1", {}, board_index, [],
        image_owner=None,
        baseline={"libc6"})
    check(verdict == "unknown",
          "认不出归属包 ⇒ 也归到「下不了结论」", "%s / %s" % (verdict, why))

    print("④ 「镜像里没有」这一档**要算问题**(退出码非 0)")
    src = open(os.path.join(REPO, TOOL_REL), encoding="utf-8").read()
    check("bad_runpath or missing or notimage" in src,
          "main() 把 notimage 算进失败条件", "退出码那行没带上 notimage")

    print("④' 判据不全 ⇒ 退出码 3(下不了结论), **不是** 1(有问题)")
    check("    if unknown:\n        return 3" in src,
          "有 unknown 时退出码是 3", "退出码那行不见了")
    check(src.index("    if unknown:\n        return 3")
          > src.index("    if bad_runpath or missing or notimage:\n        return 1"),
          "真问题(1)优先于下不了结论(3)")

    print("⑤ 盘上自带的库, 优先级最高(不受基线影响)")
    verdict, why = tool.classify_needed(
        "libfoo.so.1", {"libfoo.so.1": "/media/x/tools/root/usr/lib/libfoo.so.1"},
        {}, [], image_owner={"libfoo.so.1": "libfoo1"}, baseline=set())
    check(verdict == "stick", "盘上自带 ⇒ 判「stick」", "%s / %s" % (verdict, why))

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
