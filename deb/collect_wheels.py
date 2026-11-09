#!/usr/bin/env python3
"""从**本机 pip 缓存**里把 python 依赖的 wheel 捞出来, 组成离线仓。

为什么要这么做:
    §11.7 的硬规矩是**装机时全程不联网** —— 产线跑测时再去 PyPI 拉包,
    拉不动就等于把产线挂在公网上, 拉失败就等于产线停线。所以 python 依赖
    必须在**打包时**备齐, 随包带走, 装机时离线装。

    "打包时备齐"有两种做法:
      A. `pip download -r requirements.txt -d wheels/` —— 要联网;
      B. **从本机已有的 pip 缓存里把 wheel 捞出来** —— 完全离线。

    B 还有个附带好处: 缓存里的 wheel 就是这台开发机**实际装过、实际跑过**
    的那一份, 版本天然跟 requirements.txt 对得上, 不会出现"下到一个新版本
    装上就炸"。

⚠️ 本脚本**不执行任何 wheel 里的代码**:
    只做三件事 —— 读 zip 里的 METADATA、读 .dist-info 里的 METADATA、用
    PEP 508 的 marker 算依赖闭包。全程是读文件 + 算式子。闭包算不出来
    就**报错退出**, 绝不"装一下试试看"。

闭包从哪儿来:
    ① 根: requirements.txt;
    ② 事实依据: 开发机的 .venv —— 那里装的就是跑这套工具用的那套依赖,
       每个包的 METADATA 里写着它要什么 (Requires-Dist);
    ③ 闭包 = 从根出发、按 METADATA 里的 Requires-Dist 递归展开 (marker
       按**目标机**的环境算: python 3.8 / linux / aarch64)。

wheel 文件名怎么还原:
    pip 的 HTTP 缓存只存响应体, 不存原始 URL —— 光有 zip 内容拼不出文件名。
    但 wheel 是标准 zip, 里面有:
      · `<name>-<version>.dist-info/`        —— 文件名的主体部分
      · `<name>-<version>.dist-info/WHEEL`   —— 一行一个 `Tag: <py>-<abi>-<plat>`
    PEP 425 的**压缩 tag 组**规则: 多个平台用 `.` 连成一个平台串, 于是
    `cp38-cp38-manylinux_2_17_aarch64` + `cp38-cp38-manylinux2014_aarch64`
    → `cp38-cp38-manylinux_2_17_aarch64.manylinux2014_aarch64`。
    照这个规则还原 —— 装机时 pip 会再验一遍文件名与包内容对不对得上。

用法:
    python3 packaging/collect_wheels.py --out build/wheels
    python3 packaging/collect_wheels.py --out build/wheels --cache-dir /path/to/pip/cache
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
import zipfile
from pathlib import Path

try:                                    # 独立的 packaging 包
    from packaging.requirements import Requirement
except ImportError:                     # 退到 pip 自带的那份 (系统里必有 pip)
    try:
        from pip._vendor.packaging.requirements import Requirement
    except ImportError:
        raise SystemExit("✗ 找不到 packaging —— 装 python3-pip 就有")

#: 目标机的运行环境 —— marker 按这个算 (不是按打包机!)
#: 打包机也是 arm64/focal/python3.8, 但显式写死是为了将来换机器打包时
#: 还能算对 (python 3.10 上打包出来的闭包, 装到 3.8 的机器上就是错的)。
TARGET_ENV = {
    "python_version": "3.8",
    "python_full_version": "3.8.10",
    "sys_platform": "linux",
    "platform_system": "Linux",
    "platform_machine": "aarch64",
    "platform_python_implementation": "CPython",
    "implementation_name": "cpython",
    "implementation_version": "3.8.10",
    "os_name": "posix",
    "extra": "",
}

_TAG_RE = re.compile(r"^Tag:\s*(\S+)\s*$", re.M)


def canon(name: str) -> str:
    """PEP 503 的名字归一化"""
    return re.sub(r"[-_.]+", "-", name).lower()


# ────────────────────────── 读元数据 ──────────────────────────

def read_meta_from_wheel(path: Path) -> tuple[str, str, list[str]] | None:
    """从 wheel (zip) 里读出 (名字, 版本, Requires-Dist 列表)。"""
    try:
        with zipfile.ZipFile(path) as z:
            meta_files = [n for n in z.namelist() if n.endswith(".dist-info/METADATA")]
            if not meta_files:
                return None
            raw = z.read(meta_files[0]).decode("utf-8", "replace")
    except Exception:
        return None
    return _parse_meta(raw)


def read_meta_from_distinfo(d: Path) -> tuple[str, str, list[str]] | None:
    """从已安装的 .dist-info 目录里读出 (名字, 版本, Requires-Dist 列表)。"""
    meta = d / "METADATA"
    if not meta.is_file():
        return None
    try:
        raw = meta.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None
    return _parse_meta(raw)


def _parse_meta(raw: str) -> tuple[str, str, list[str]] | None:
    name = re.search(r"^Name:\s*(.+)$", raw, re.M)
    ver = re.search(r"^Version:\s*(.+)$", raw, re.M)
    if not name or not ver:
        return None
    requires = re.findall(r"^Requires-Dist:\s*(.+)$", raw, re.M)
    return name.group(1).strip(), ver.group(1).strip(), requires


# ────────────────────────── 静态闭包 ──────────────────────────

def _marker_ok(marker, ctx: frozenset) -> bool:
    """marker 在给定的 extra 上下文里成不成立。

    ctx 里装着「父包请求的 extra 名」。比如 uvicorn[standard] 的依赖写的是
    `httptools; extra == "standard"` —— 这个 `standard` 是**uvicorn 的
    extra**, 不是 httptools 的, 所以算 marker 时必须带上父包的上下文,
    拿空 extra 去算会把标准版的依赖全判成"不需要"。
    """
    if marker is None:
        return True
    for e in (sorted(ctx) or [""]):
        env = dict(TARGET_ENV)
        env["extra"] = e
        if marker.evaluate(env):
            return True
    return False


def resolve_closure(roots: list[Requirement],
                     universe: dict[str, tuple[str, str, list[str]]]
                     ) -> tuple[dict[str, str], list[str], list[str]]:
    """从 roots 出发静态展开依赖闭包。

    universe: {归一化名: (原名, 版本, Requires-Dist 列表)}
    返回 (闭包名→版本, 缺失的包, 版本对不上的包)
    """
    resolved: dict[str, str] = {}
    missing: list[str] = []
    mismatched: list[str] = []
    # 队列项 = (需求, 该需求所处的 extra 上下文)
    #   上下文 = 「父包请求的 extra 名」集合, 算 marker 时要用
    queue: list[tuple[Requirement, frozenset]] = [(r, frozenset({""})) for r in roots]
    seen: set[tuple[str, frozenset]] = set()

    while queue:
        req, ctx = queue.pop()
        key = (canon(req.name), ctx)
        if key in seen:
            continue
        seen.add(key)

        if not _marker_ok(req.marker, ctx):
            continue

        cname = canon(req.name)
        info = universe.get(cname)
        if info is None:
            missing.append(req.name)
            continue
        _orig, version, requires = info

        if req.specifier and not req.specifier.contains(version, prereleases=True):
            mismatched.append(f"{req.name} 要求 {req.specifier}, 实际 {version}")
            continue
        resolved[cname] = version

        # 这个包被要的 extras —— 它的依赖里 `extra == "xxx"` 就是指这些
        want_extras = set(req.extras)
        for raw in requires:
            try:
                sub = Requirement(raw)
            except Exception:
                continue
            sub_ctx = frozenset({""} | want_extras)
            if not _marker_ok(sub.marker, sub_ctx):
                continue
            queue.append((sub, sub_ctx))

    return resolved, missing, mismatched


# ────────────────────────── 主流程 ──────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="从 pip 缓存离线收集 wheel")
    ap.add_argument("--out", required=True, type=Path, help="输出目录 (wheels/)")
    ap.add_argument("--requirements", type=Path, default=None,
                    help="requirements.txt (默认: 仓库根下的)")
    ap.add_argument("--venv", type=Path, default=None,
                    help="开发机 venv (默认: 仓库根下的 .venv)")
    ap.add_argument("--cache-dir", type=Path, default=None,
                    help="pip 缓存目录 (默认: PIP_CACHE_DIR 或 ~/.cache/pip)")
    args = ap.parse_args()

    root = Path(__file__).resolve().parent.parent
    reqs_file = args.requirements or (root / "requirements.txt")
    venv = args.venv or (root / ".venv")
    cache = args.cache_dir or Path(os.environ.get("PIP_CACHE_DIR") or
                                   Path.home() / ".cache" / "pip")
    if not reqs_file.is_file():
        raise SystemExit(f"✗ 找不到 requirements.txt: {reqs_file}")

    # ① 读 requirements.txt 的根
    roots: list[Requirement] = []
    for line in reqs_file.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        roots.append(Requirement(line))

    # ② 读 .venv 的已装发行包 —— 闭包的事实依据
    sp = venv / "lib" / "python3.8" / "site-packages"
    if not sp.is_dir():
        raise SystemExit(f"✗ 找不到 {sp} —— 先建好开发 venv")
    universe: dict[str, tuple[str, str, list[str]]] = {}
    for d in sorted(sp.glob("*.dist-info")):
        info = read_meta_from_distinfo(d)
        if info:
            universe[canon(info[0])] = info
    print(f"==> .venv 里的发行包: {len(universe)} 个")

    # ③ 静态解闭包
    resolved, missing, mismatched = resolve_closure(roots, universe)
    if missing:
        raise SystemExit("✗ 闭包缺件 (venv 里没有): " + ", ".join(sorted(set(missing))))
    if mismatched:
        raise SystemExit("✗ 版本对不上:\n    " + "\n    ".join(mismatched))
    print(f"==> 依赖闭包: {len(resolved)} 个包")

    # ④ 扫 pip 缓存里的 wheel
    if not cache.is_dir():
        raise SystemExit(f"✗ 找不到 pip 缓存 {cache}")
    wheels: dict[tuple[str, str], tuple[str, Path]] = {}
    for dirpath, _dirs, files in os.walk(cache):
        for fn in files:
            p = Path(dirpath) / fn
            try:
                with open(p, "rb") as f:
                    if f.read(2) != b"PK":
                        continue
            except Exception:
                continue
            info = read_meta_from_wheel(p)
            if not info:
                continue
            name, ver, _req = info
            wheels[(canon(name), ver)] = (name, p)
    print(f"==> pip 缓存里的 wheel: {len(wheels)} 个")

    # ⑤ 拷闭包里的那些出来
    out = args.out
    out.mkdir(parents=True, exist_ok=True)
    for f in out.glob("*.whl"):
        f.unlink()

    not_found = []
    total = 0
    for cname, ver in sorted(resolved.items()):
        hit = wheels.get((cname, ver))
        if not hit:
            not_found.append(f"{cname}=={ver}")
            continue
        orig, src = hit
        # 还原标准 wheel 文件名
        with zipfile.ZipFile(src) as z:
            wf = [n for n in z.namelist() if n.endswith(".dist-info/WHEEL")]
            if not wf:
                not_found.append(f"{cname}=={ver} (缺 WHEEL)")
                continue
            stem = os.path.dirname(wf[0])[: -len(".dist-info")]
            tags = _TAG_RE.findall(z.read(wf[0]).decode("utf-8", "replace"))
        fname = _wheel_filename(stem, tags)
        if not fname:
            not_found.append(f"{cname}=={ver} (tag 认不出: {tags})")
            continue
        shutil.copy2(src, out / fname)
        total += 1

    if not_found:
        raise SystemExit(
            "✗ 缓存里缺这些 wheel (装机会失败):\n    " + "\n    ".join(not_found) +
            "\n    在联网机器上跑: pip download -r requirements.txt -d <目录>, "
            "再把目录拷过来。")

    size = sum(f.stat().st_size for f in out.glob("*.whl"))
    print(f"==> 输出 {total} 个 wheel 到 {out}  ({size // 1024} KB)")
    for f in sorted(out.glob("*.whl")):
        print(f"    {f.name:<80} {f.stat().st_size // 1024}K")
    return 0


def _wheel_filename(stem: str, tags: list[str]) -> str | None:
    """由 dist-info 名 + WHEEL 的 Tag 行还原 wheel 文件名 (PEP 425 压缩 tag 组)。"""
    if not tags:
        return None
    parsed = []
    for t in tags:
        parts = t.split("-")
        if len(parts) != 3:
            return None
        parsed.append(tuple(parts))
    pytags = sorted({p[0] for p in parsed})
    abitags = sorted({p[1] for p in parsed})
    plats: list[str] = []
    for p in parsed:
        if p[2] not in plats:
            plats.append(p[2])
    if len(pytags) != 1 or len(abitags) != 1:
        return None
    tag = f"{pytags[0]}-{abitags[0]}-{'.'.join(plats)}"
    return f"{stem}-{tag}.whl"


if __name__ == "__main__":
    sys.exit(main())
