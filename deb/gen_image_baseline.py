"""生成「出货镜像基线」清单 —— deb/image-baseline.txt。

**为什么要有这个文件**
    工具包是随包解到目标机上的 (`dpkg-deb -x`, 不走 dpkg 依赖解析), 所以
    **谁来保证那些共享库在目标机上存在?**
    靠 apt 依赖是不行的 —— 装机时不联网, 依赖解析跑不了。

    答案: 出货镜像自带一批包, 我们把「镜像自带的」和「必须随包带走的」分开。
    这份基线就是「镜像自带」的契约 —— 打包脚本拿它算出「还差哪些」,
    再断言「差的那个必须已经躺进 deps/ 里了」。

**怎么推导出基线**
    镜像烤制时间 = 2026-10-16 (厂商 anfuhua-full-apt 烤的)。
    dpkg 会给每个包的 files-list (`/var/lib/dpkg/info/<pkg>.list`) 打上安装时间戳,
    所以「安装时间早于镜像烤制日」= 镜像自带, 「晚于」= 开发期才装的
    (本项目开发期只 apt 装了 fio / glmark2-es2 / glmark2-es2-wayland 三个,
     以及它们拖进来的依赖)。

    ⚠️ 所以这个脚本要在**跟出货镜像同款的那台机器**上跑才有意义。

跑法:  python3 deb/gen_image_baseline.py > deb/image-baseline.txt
"""
import glob
import os
import sys
from datetime import datetime

# 镜像烤制日 —— 早于这天装的都是镜像自带
IMG_BAKED = "2026-10-16"


def main():
    cut = datetime.strptime(IMG_BAKED, "%Y-%m-%d").timestamp()
    base, later = [], []
    for lst in glob.glob("/var/lib/dpkg/info/*.list"):
        pkg = os.path.basename(lst)[:-5]
        (base if os.stat(lst).st_mtime < cut else later).append(pkg)

    out = sys.stdout
    out.write("# 出货镜像基线 —— 目标板上「本来就有的」包\n")
    out.write("#\n")
    out.write("# 由 deb/gen_image_baseline.py 生成 (在跟出货镜像同款的板子上跑)。\n")
    out.write("# 判据: dpkg 的 files-list 时间戳早于镜像烤制日 %s = 镜像自带。\n" % IMG_BAKED)
    out.write("#\n")
    out.write("# 用途: 打包脚本算「随包工具的运行时共享库闭包」时, 闭包里属于本基线的\n")
    out.write("#       那些包 = 目标机上已经有了, 不用随包带; 不在基线里的 = 目标机上\n")
    out.write("#       没有, **必须**打进 deps/ 离线仓, 否则工具装上了也跑不起来\n")
    out.write("#       (fio 就是这么翻车的 —— 它硬链 librbd.so.1, 而 librbd1/librados2/\n")
    out.write("#        libtirpc3/... 全是开发期 apt 装 fio 时才拖进来的, 镜像里没有)。\n")
    out.write("#\n")
    out.write("# 共 %d 个包 = 镜像自带; 另有 %d 个是开发期才装的 (不在基线里)。\n"
              % (len(base), len(later)))
    for p in sorted(base):
        out.write(p + "\n")


if __name__ == "__main__":
    main()
