"""测试用的临时目录 —— **跑完自动删**。

`tempfile.mkdtemp()` 只管创建不管收: 每次回归跑一遍就在 `/tmp` 里留一个目录,
跑几十遍就是几十个 —— 实测 `tf_fake_*` 积了 95 个、`aging_skipnext_*` 52 个、
`aging-durations-*` / `aging_reconcile_*` / `aging_merge_*` 各 19 个, 加起来
两百多个目录躺在 `/tmp` 里。

这里统一包一层: 建目录的同时注册 `atexit` 收尾 ⇒ **进程退出时目录就没了**,
不留残骸, 也不用谁去手工清。

    from tests._tmp import mkdtemp
    tmp = mkdtemp(prefix="aging-xxx-")     # 用法跟 tempfile.mkdtemp 一模一样

⚠️ `atexit` 是**进程退出时**才收 —— 同一进程里跑完的用例, 中途还能去翻那个
目录 (调试时有用)。要立刻清就用 `tempfile.TemporaryDirectory` 的 `with`。
"""
import atexit
import shutil
import tempfile


def mkdtemp(prefix="aging-test-", parent=None):
    """建个临时目录, 进程退出时自动删掉。返回路径字符串。"""
    path = tempfile.mkdtemp(prefix=prefix, dir=parent)
    # rmtree(path, ignore_errors=True) —— 目录里可能还有别的进程写的文件,
    # 删不掉就别把整个进程带崩(测试失败的原因不该是"清垃圾失败")
    atexit.register(shutil.rmtree, path, True)
    return path
