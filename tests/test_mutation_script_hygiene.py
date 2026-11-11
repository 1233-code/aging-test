"""回归测试: 变异脚本**自己**别坏 —— 证据链不能是空的(2026-11-11 查出来的)。

起因:
    2026-11-11 的一轮审查里, 对**全部 45 个**变异脚本做了「恒等变异探针」
    (把变异改成「原样替换成原样」= 逻辑零变化), 结果有两个脚本报了
    「咬得动」⇒ 它们的判据**根本不成立**:

      * `tests/_mutate_merge.py`: 副本只拷了 `server/` + `config.yaml` +
        `tests/test_history_retest_merge.py`, **没拷 `tests/_tmp.py`** ——
        而那个测试第 51 行 `from tests._tmp import mkdtemp` ⇒ 副本**一启动就
        ModuleNotFoundError** ⇒ **基线本来就是红的** ⇒ 26 条变异不管改对改错
        都报「咬得动」⇒ 声称「全部被抓住」的证据**一条都不成立**。
      * `tests/_mutate_hdmi_replug.py`: 副本**没拷 `config.yaml`** —— 测试
        第 546 行要读它 ⇒ 基线在第 20 节就崩 ⇒ 同上, 判据被一个**无关崩溃**
        满足。

    两处的病根是同一个: **副本少拷了测试要用的东西**, 而判据又只看
    「退出码非 0」⇒ 基线一红, 证据全空。

这个测试防的是**同一类**毛病再犯(静态查, 不跑变异, 所以很快):

  ① **依赖齐不齐**: 副本里被复制的测试文件, 它 `from tests.X import` 的
     东西 ⇒ 变异脚本也得复制 `X.py`; 它要读 `config.yaml` ⇒ 变异脚本也得
     复制 `config.yaml`。
  ② **判据硬不硬**: 脚本要数「✗」的 ⇒ 必须带「红了但一个 ✗ 都没有 ⇒
     那是**崩了**, 不算数」这道守卫(JS 侧一直有, Python 侧以前漏抄)。

跑法: .venv/bin/python -m tests.test_mutation_script_hygiene
"""
import ast
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS = os.path.join(REPO, "tests")

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


def mutation_scripts():
    out = []
    for fn in sorted(os.listdir(TESTS)):
        if fn.startswith("_mutate_") and fn.endswith(".py"):
            out.append(os.path.join(TESTS, fn))
    return out


def copied_test_files(src):
    """脚本源码里提到的测试文件名(它复制到副本里的那些)。"""
    return sorted(set(re.findall(r"test_[A-Za-z0-9_]+\.py", src)))


def runs_in_copy(src):
    """这个脚本是不是**在副本里**跑测试(cwd 不是仓库本身)。

    只在副本里跑的脚本才需要把测试的依赖一起拷过去 —— 在仓库里跑的
    (cwd=REPO) 依赖自然在, 不用管。
    """
    for m in re.finditer(r"cwd\s*=\s*([A-Za-z_][A-Za-z0-9_]*)", src):
        if m.group(1) != "REPO":
            return True
    return False


def reads_repo_config_yaml(path):
    """这个测试是不是**从自己所在目录**(仓库根)读 config.yaml。

    只认「读仓库那份」—— 测试自己在临时目录里**造**一份 config.yaml
    (造镜像目录那种)不算。
    """
    with open(path, encoding="utf-8") as f:
        src = f.read()
    for m in re.finditer(r"open\(\s*os\.path\.join\([^)]*\"config\.yaml\""
                         r"[^)]*\)", src):
        tail = src[m.end():m.end() + 12]
        if ", \"w\"" in tail or ", 'w'" in tail:
            continue        # 这是**写**, 不是读
        return True
    return False


def tests_modules_imported(path):
    """这个测试文件 `from tests.X import ...` 的 X(去掉 .py 的模块名)。"""
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=path)
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module and node.module.startswith("tests."):
                mods.add(node.module.split(".", 1)[1])
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.name.startswith("tests."):
                    mods.add(a.name.split(".", 1)[1])
    return mods


def main():
    scripts = mutation_scripts()
    section("【1】变异脚本的判据: 数 ✗ 的必须带「崩了不算数」这道守卫")
    for p in scripts:
        name = os.path.basename(p)
        with open(p, encoding="utf-8") as f:
            src = f.read()
        counts_fails = ("fails" in src)
        has_guard = ("fails == 0" in src) or ("崩了" in src)
        if counts_fails:
            check(has_guard,
                  "%s: 数 ✗ 的脚本带「崩了不算数」守卫" % name,
                  "少了这道守卫 ⇒ 基线一崩, 证据就空了")
        else:
            check(True, "%s: 不数 ✗ 的脚本(另说)" % name)

    section("【2】副本要带的依赖: 测试 import 的模块 + 它要读的 config.yaml")
    for p in scripts:
        name = os.path.basename(p)
        with open(p, encoding="utf-8") as f:
            src = f.read()
        if not runs_in_copy(src):
            continue      # 在仓库里跑 ⇒ 依赖本来就在
        for tf in copied_test_files(src):
            tp = os.path.join(TESTS, tf)
            if not os.path.exists(tp):
                continue
            for mod in sorted(tests_modules_imported(tp)):
                need = "%s.py" % mod
                check(need in src,
                      "%s: 副本带了 %s(测试 %s 要它)" % (name, need, tf),
                      "少了 ⇒ 副本一启动就 ImportError ⇒ 基线本来就是红的")
            if reads_repo_config_yaml(tp):
                check("config.yaml" in src,
                      "%s: 副本带了 config.yaml(测试 %s 要读它)" % (name, tf),
                      "少了 ⇒ 基线读文件时就崩 ⇒ 判据被无关崩溃满足")

    print("\n" + "─" * 60)
    if FAILS:
        print("✗ %d 项失败 / 共 %d 项" % (len(FAILS), PASSES + len(FAILS)))
        for f in FAILS:
            print("   · " + f)
        sys.exit(1)
    print("✓ 全部通过 (%d 项)" % PASSES)


if __name__ == "__main__":
    main()
