#!/bin/sh
# 自动生成 —— 别手改 (scripts/build_usb_image.sh 用 scripts/usb_wrapper.sh.tpl 打的)。
#
# 作用: 那 10 个运行时共享库在 U 盘上, 不在系统路径里 ⇒ 链接器找不到它们
# ⇒ 「error while loading shared libraries」。这个 wrapper 在 exec 真二进制
# **之前**把 LD_LIBRARY_PATH 指好。
#
# ⚠️ **搜索路径要带上子目录**(2026-11-06 修): 有的库不在顶层 ——
#    `libceph-common.so.2` 住在 `usr/lib/aarch64-linux-gnu/ceph/` 里,
#    少列这一层 ⇒ 在没装 ceph 的板子上 fio rc=127「起不来」⇒
#    存储项 error ⇒ 好板子被当成坏板子返修。
#
# ⚠️ **必须在 sudo 之后才设 LD_LIBRARY_PATH** —— sudo 默认 env_reset 会清掉它。
#
# ⚠️ **这份模板是「构建」和「给现成的盘打补丁」共用的唯一来源** ——
#    两边各写一份 ⇒ 迟早对不上 ⇒ 盘上的 wrapper 悄悄少一层 ⇒
#    同样的毛病再犯一次(2026-11-06 的教训)。
#
# 占位符: @REAL@ = 真二进制名(比如 fio.bin)
_self="$0"
while [ -L "$_self" ]; do
    _l="$(readlink "$_self")"
    case "$_l" in
        /*) _self="$_l" ;;
        *)  _self="$(dirname "$_self")/$_l" ;;
    esac
done
_here="$(cd "$(dirname "$_self")" && pwd)"
_root="$(cd "$_here/.." && pwd)"
_sysroot="$_root/root"
_libs=""
# ⚠️ **子目录也得列上**(2026-11-06 实测, 别删这两项):
#    Debian 的库不都放在顶层 —— `librados2` 把 `libceph-common.so.2` 放在
#    `usr/lib/aarch64-linux-gnu/ceph/` 里(不是顶层), 而 `librados.so.2`
#    的 RUNPATH 写的是**目标机**的绝对路径 `/usr/lib/aarch64-linux-gnu/ceph`
#    ⇒ 在**没装 ceph** 的板子上, 链接器哪儿都找不到那个库 ⇒ fio rc=127
#    ⇒ 存储项 error ⇒ **好板子被当成坏板子返修**。
#    ⇒ 搜索路径必须把**再深一层**的子目录也带上。
#
# ⚠️ **这份 glob 清单必须跟 scripts/check_usb_selfcontained.py 里的
#    LIB_GLOBS 一模一样** —— 那个检查脚本就是按这条路径验「换台板子还能
#    不能起来」的, 两边不一致 ⇒ 验的是另一条路径 ⇒ 白验。
#    `tests/test_usb_stick_selfcontained.py` 会静态比对这两份。
for _d in "$_sysroot"/usr/lib/*/ "$_sysroot"/usr/lib/ "$_sysroot"/lib/*/ "$_sysroot"/lib/ \
         "$_sysroot"/usr/lib/*/*/ "$_sysroot"/lib/*/*/; do
    [ -d "$_d" ] || continue
    _libs="$_libs${_libs:+:}$_d"
done
if [ -n "$_libs" ]; then
    LD_LIBRARY_PATH="$_libs${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
    export LD_LIBRARY_PATH
fi

# glmark2 的场景数据(models/shaders/textures)在 U 盘上 ⇒ 必须显式指过去
# (编译进去的默认路径是 /usr/share/glmark2 —— 设备上根本没这个目录)。
# ⚠️ 调用方自己给了 --data-path 就不许再加一遍(加了 glmark2 会报错)。
_name="$(basename "$_self")"
case "$_name" in
    glmark2-es2|glmark2-es2-wayland)
        _has_data=0
        for _a in "$@"; do
            case "$_a" in
                --data-path|-d) _has_data=1; break ;;
            esac
        done
        if [ "$_has_data" = "0" ] && [ -d "$_root/share/glmark2" ]; then
            set -- --data-path "$_root/share/glmark2" "$@"
        fi
        ;;
esac

exec "$_root/libexec/@REAL@" "$@"
