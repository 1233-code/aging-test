#!/usr/bin/env python3
"""标定 GPU 压测的场景清单 (server/tests/gpu_benchmarks.py)。

跑一次 glmark2 默认套件, 把它打印的场景行**原样**抄成清单 —— 清单决定了
GPU 压测跑哪些场景、每个跑多久、以及「这一轮该跑几个」这个判定依据。

用法:
    # 1) 采一份标定输出 (离屏渲染, 不抢产线的屏幕)
    glmark2-es2 --off-screen > /tmp/glmark2-calib.txt 2>&1

    # 2) 生成清单并写回 server/tests/gpu_benchmarks.py
    .venv/bin/python scripts/calibrate_gpu_benchmarks.py /tmp/glmark2-calib.txt --write

    # 3) 验证: 拼出来的 -b 参数能还原出同一批场景
    .venv/bin/python scripts/calibrate_gpu_benchmarks.py --verify

⚠️ 换 glmark2 版本 / 换板子 (渲染器不同, 场景集可能变) 都要重新标定。
   清单过时的话, GPU 项会判 error 并提示「需要重新标定」, 不会静默少跑。
"""
import argparse
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from server.tests import gpu_benchmarks as gb  # noqa: E402

# 与 gpu.py 里那条正则一致: [场景] <选项串>: FPS: N FrameTime: X ms
SCENE_RE = re.compile(
    r"^\[(?P<scene>[^\]]+)\]\s*(?P<opts>.*?):\s*FPS:\s*(?P<fps>[\d.]+)"
    r"\s+FrameTime:\s*(?P<ft>[\d.]+)\s*ms"
)
SCORE_RE = re.compile(r"glmark2 Score:\s*([\d.]+)")


def parse(path):
    """从标定输出里解析出 (场景, 选项串) 列表和分数。"""
    out = []
    score = None
    with open(path, "r", errors="replace") as f:
        for line in f:
            line = line.rstrip("\n")
            m = SCENE_RE.match(line)
            if m:
                out.append((m.group("scene"), m.group("opts")))
                continue
            sm = SCORE_RE.search(line)
            if sm:
                score = float(sm.group(1))
    return out, score


def render_block(benchmarks):
    """生成 BENCHMARKS 的源码块。"""
    lines = ["BENCHMARKS: Tuple[Tuple[str, str], ...] = ("]
    for scene, opts in benchmarks:
        lines.append(f'    ({scene!r}, {opts!r}),')
    lines.append(")")
    return "\n".join(lines)


def write_back(benchmarks, path):
    """把新的清单写回 gpu_benchmarks.py (只换 BENCHMARKS 那一段)。"""
    with open(path, "r", encoding="utf-8") as f:
        src = f.read()
    start = src.index("BENCHMARKS: Tuple[Tuple[str, str], ...] = (")
    end = src.index("\n)\n", start) + len("\n)\n")
    new = src[:start] + render_block(benchmarks) + "\n" + src[end:]
    with open(path, "w", encoding="utf-8") as f:
        f.write(new)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("calib", nargs="?", help="标定输出文件 (glmark2 的输出)")
    ap.add_argument("--write", action="store_true", help="写回 gpu_benchmarks.py")
    ap.add_argument("--verify", action="store_true", help="只验证当前清单自洽")
    args = ap.parse_args()

    target = os.path.join(ROOT, "server", "tests", "gpu_benchmarks.py")

    if args.verify or not args.calib:
        # 验证: 从 -b 参数反解出的身份 == 清单
        args_list = gb.build_bench_args(3.0)
        specs = [args_list[i + 1] for i in range(0, len(args_list), 2)]
        back = []
        for s in specs:
            parts = s.split(":")
            opts = ":".join(p for p in parts[1:] if not p.startswith("duration="))
            back.append(gb.identity(parts[0], opts))
        ok = back == gb.expected_identities()
        print(f"清单 {len(gb.BENCHMARKS)} 个场景, -b 参数 {len(specs)} 条, "
              f"反解一致: {ok}")
        return 0 if ok else 1

    benchmarks, score = parse(args.calib)
    if not benchmarks:
        print("标定文件里一个场景行都没有 —— 这不是 glmark2 的输出?", file=sys.stderr)
        return 1
    print(f"解析到 {len(benchmarks)} 个场景, 分数 {score}")
    seen = {}
    for s, o in benchmarks:
        seen[s] = seen.get(s, 0) + 1
    print("按场景统计: " + ", ".join(f"{k}×{v}" for k, v in seen.items()))
    # 同一个 (场景, 选项) 组合重复出现 → 清单里就会有重复项, 提醒一下
    dup = [x for x in set(benchmarks) if benchmarks.count(x) > 1]
    if dup:
        print(f"⚠ 有重复的组合: {dup}")

    if args.write:
        write_back(benchmarks, target)
        print(f"已写回 {target}")
    else:
        print(render_block(benchmarks))
    return 0


if __name__ == "__main__":
    sys.exit(main())
