/*
 * U 盘版应用的一键启动器 —— 产线工人双击它, 就把同目录下的 start.sh 跑起来。
 *
 * 为什么必须是「原生二进制」而不是 start.sh 本身:
 *   文件管理器(Nautilus)对**可执行的文本文件**默认是「显示」而不是「运行」
 *   (org.gnome.nautilus.preferences executable-text-activation 默认 'display';
 *   这板子还没给 application/x-shellscript 注册默认程序 ⇒ 回退 text/plain
 *   ⇒ 双击只会用文本编辑器把源码显示出来)。
 *   想让它跑, 要么改**每台设备每个用户**的 dconf 设置, 要么每次右键
 *   「以程序方式运行」—— 产线上都不现实(工人不碰终端、也不该碰设置)。
 *   而 Nautilus 对**原生可执行文件**默认就是「运行」⇒ 这个小二进制
 *   双击直接执行, **不用改任何设备设置, 换台设备插上还是双击就行**。
 *
 * 它只做一件事: 找到自己所在目录, 用 bash 跑同目录的 start.sh。
 * 应用逻辑一行没动 —— 还在 start.sh 里(再双击一次 = start.sh 幂等,
 * 服务已在跑就只重开浏览器)。
 *
 * 编译(板子上, aarch64):
 *   gcc -O2 -o 启动老化测试 scripts/usb_launcher.c
 */
#include <errno.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <unistd.h>

#ifndef PATH_MAX
#define PATH_MAX 4096
#endif

/* 出错了: 桌面里双击时 stderr 没人看 ⇒ 落一份到盘上, 顺带弹个通知 */
static void fail(const char *dir, const char *what)
{
    char msg[PATH_MAX + 512];
    snprintf(msg, sizeof(msg), "启动老化测试失败: %s (%s)\n", what,
             strerror(errno));
    fprintf(stderr, "%s", msg);

    if (dir && *dir) {
        char p[PATH_MAX];
        snprintf(p, sizeof(p), "%s/logs", dir);
        mkdir(p, 0777);
        snprintf(p, sizeof(p), "%s/logs/启动失败.txt", dir);
        FILE *f = fopen(p, "a");
        if (f) {
            fputs(msg, f);
            fclose(f);
        }
    }
    /* 有通知工具就弹一下; 没有就算了(别把启动搞得更脆) */
    int rc = system("command -v notify-send >/dev/null 2>&1 && "
                    "notify-send '老化测试' '启动失败, 详见 logs/启动失败.txt' "
                    ">/dev/null 2>&1");
    if (rc != 0) {
        rc = system("command -v zenity >/dev/null 2>&1 && "
                    "zenity --error --text='启动老化测试失败, 详见 logs/启动失败.txt' "
                    ">/dev/null 2>&1");
        (void)rc;
    }
}

int main(int argc, char **argv)
{
    char self[PATH_MAX];
    ssize_t n = readlink("/proc/self/exe", self, sizeof(self) - 1);
    if (n <= 0) {
        fail("", "读不到自己的路径(/proc/self/exe)");
        return 1;
    }
    self[n] = '\0';

    char *slash = strrchr(self, '/');
    if (!slash) {
        fail("", "自己的路径里没有 '/'");
        return 1;
    }
    *slash = '\0';
    const char *dir = self;              /* 现在 self = 所在目录 */

    char script[PATH_MAX + 16];
    snprintf(script, sizeof(script), "%s/start.sh", dir);
    if (access(script, R_OK) != 0) {
        char msg[PATH_MAX + 64];
        snprintf(msg, sizeof(msg), "同目录下没有 start.sh (%s)", script);
        fail(dir, msg);
        return 1;
    }

    /* bash <dir>/start.sh [原参数...] */
    char **newargv = calloc((size_t)argc + 2, sizeof(char *));
    if (!newargv) {
        fail(dir, "内存不够");
        return 1;
    }
    newargv[0] = (char *)"bash";
    newargv[1] = script;
    for (int i = 1; i < argc; i++)
        newargv[i + 1] = argv[i];
    newargv[argc + 1] = NULL;

    /* ⚠️ **不能直接 execv** —— exec 之后启动器自己就没了, start.sh 万一失败
     *   (端口被占 / venv 解释器路径不对 / 权限不够) 就**没有任何人报**,
     *   工人看到的只是「双击没反应」(2026-11-05 实测: 盘上的 venv 解释器
     *   路径写死了构建目录 ⇒ 换台设备就起不来, 而且是**静默**的)。
     *   ⇒ fork 出来跑, 等它退出; 退出码非 0 = 启动失败 ⇒ 弹通知 + 落盘。
     */
    pid_t pid = fork();
    if (pid < 0) {
        fail(dir, "fork 失败");
        return 1;
    }
    if (pid == 0) {
        execv("/bin/bash", newargv);
        _exit(127);              /* 子进程 exec 失败 ⇒ 父进程看到 127 */
    }

    int status = 0;
    while (waitpid(pid, &status, 0) < 0) {
        if (errno != EINTR) {
            fail(dir, "等不到 start.sh 结束");
            return 1;
        }
    }
    if (WIFEXITED(status) && WEXITSTATUS(status) != 0) {
        char msg[PATH_MAX + 64];
        snprintf(msg, sizeof(msg), "start.sh 退出码 %d (启动失败)",
                 WEXITSTATUS(status));
        fail(dir, msg);
        return 1;
    }
    return 0;
}
