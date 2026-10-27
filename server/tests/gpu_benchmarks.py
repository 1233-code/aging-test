"""glmark2 场景清单 —— GPU 压测跑哪些场景、每个跑多久 (§6.3 方案A)

## 为什么要有这份清单

§6.3 原来直接跑 `glmark2 --fullscreen` 的**默认套件**: 33 个场景组合, 每个
10 秒 → 一轮 336 秒 (实测 run 57)。两个问题:

  ① **太慢** —— 产线要求一轮控制在两分钟。
  ② **分数不可比, 而且会骗人** —— glmark2 跑到一半被掐断时**照样打印
     `glmark2 Score:`**。历史数据摆在那儿:

        run 50  跑了 26 个场景  出分 142   ← 被掐断了
        run 57  跑了 33 个场景  出分 148   ← 跑全了
        run 124 跑了  2 个场景  出分 198   ← 才跑两个, 分数最高

     **分数高的那个反而是没跑全的那个** —— 因为分数是各场景的加权平均,
     漏掉的都是分数低的场景 (terrain 8 分、bump/effect2d 那些重的场景排在
     后面, 被掐断的往往只剩前面分数高的)。所以光看分数**根本判断不出这一轮
     跑全了没有**, 而 §9.3 明确要求「静默的『通过』比明确的『失败』有害得多」。

所以改成: **场景清单写死在代码里, 用 `-b` 一个一个显式点名**。于是

  · 每个场景的时长可以调 (`duration=N`) → 一轮 = 33 × 3s ≈ 99 秒
    (+ 装载开销, 实测 105.2s);
  · 「这一轮该跑多少个场景」是**我们自己知道的** → 跑少了判得出来
    (掐断了 → failed, 清单过时了 → error, 都不会再记成 passed);
  · 分数**跨轮可比** —— 场景集和时长都固定, 分数才有意义 (多轮看衰减趋势
    才成立)。

## 清单是怎么来的 (标定)

跑一次默认套件, 把 glmark2 打印的场景行**原样抄下来** —— 就是下面
`BENCHMARKS` 里那一串。抄的是 glmark2 自己打印的选项串, 不做任何加工,
这样重新拼回去的 `-b` 参数和标定时跑的是同一批场景。

标定环境 (2026-10-26):
    glmark2 2021.02 / /usr/bin/glmark2-es2 (X11 变体)
    Mali-G57 (Panfrost) / OpenGL ES 3.1 Mesa 22.2.2
    标定方式: `glmark2-es2 --off-screen` (离屏渲染, 不抢产线的屏幕)

**换 glmark2 版本 / 换板子**导致场景集变了的话, 必须重新标定 —— 否则
`-b` 会带上本机不认识的选项, glmark2 直接报错退出 (退出码非 0, 那一轮判
failed, **不会静默少跑**)。重新标定的步骤:

    1. 跑一次默认套件, 抓全部场景行:
           glmark2-es2 --off-screen 2>&1 | tee /tmp/calib.txt
    2. 把每行 `[场景] <选项串>: FPS: ...` 里的「场景 + 选项串」抄进
       下面 BENCHMARKS (选项串照抄, 不要改写、不要省略)。
    3. 核对场景数与 BENCHMARKS 的长度一致, 跑一次短时验证:
           glmark2-es2 --off-screen $(build_bench_args 0.3) | grep -c FPS:

## 判定怎么用这份清单

`gpu.py` 的 `_judge_round()` 拿实际渲染出来的场景身份和这里的清单比:

  · 实际是清单的**前缀** → 中途被打断 → **failed** (这一轮没有结论)
  · 实际和清单**对不上** (数量/顺序/内容) → 清单过时了 → **error**
  · 完全一致 → **passed**
"""
from typing import List, Tuple

# ── 标定信息 ─────────────────────────────────────────────────────
# 换 glmark2 / 换机器导致场景集变了, 这几行也要跟着更新
GLMARK2_VERSION = "2021.02"
CALIBRATED_AT = "2026-10-26"
CALIBRATED_RENDERER = "Mali-G57 (Panfrost) / OpenGL ES 3.1 Mesa 22.2.2"

# ── 场景清单 ─────────────────────────────────────────────────────
# (场景名, 选项串) —— 选项串是 glmark2 自己打印的那一串, **原样照抄**
# 顺序 = glmark2 默认套件的执行顺序, 改顺序会让分数和标定时不可比。
BENCHMARKS: Tuple[Tuple[str, str], ...] = (
    ('build', 'use-vbo=false'),
    ('build', 'use-vbo=true'),
    ('texture', 'texture-filter=nearest'),
    ('texture', 'texture-filter=linear'),
    ('texture', 'texture-filter=mipmap'),
    ('shading', 'shading=gouraud'),
    ('shading', 'shading=blinn-phong-inf'),
    ('shading', 'shading=phong'),
    ('shading', 'shading=cel'),
    ('bump', 'bump-render=high-poly'),
    ('bump', 'bump-render=normals'),
    ('bump', 'bump-render=height'),
    ('effect2d', 'kernel=0,1,0;1,-4,1;0,1,0;'),
    ('effect2d', 'kernel=1,1,1,1,1;1,1,1,1,1;1,1,1,1,1;'),
    ('pulsar', 'light=false:quads=5:texture=false'),
    ('desktop', 'blur-radius=5:effect=blur:passes=1:separable=true:windows=4'),
    ('desktop', 'effect=shadow:windows=4'),
    ('buffer', 'columns=200:interleave=false:update-dispersion=0.9:update-fraction=0.5:update-method=map'),
    ('buffer', 'columns=200:interleave=false:update-dispersion=0.9:update-fraction=0.5:update-method=subdata'),
    ('buffer', 'columns=200:interleave=true:update-dispersion=0.9:update-fraction=0.5:update-method=map'),
    ('ideas', 'speed=duration'),
    ('jellyfish', '<default>'),
    ('terrain', '<default>'),
    ('shadow', '<default>'),
    ('refract', '<default>'),
    ('conditionals', 'fragment-steps=0:vertex-steps=0'),
    ('conditionals', 'fragment-steps=5:vertex-steps=0'),
    ('conditionals', 'fragment-steps=0:vertex-steps=5'),
    ('function', 'fragment-complexity=low:fragment-steps=5'),
    ('function', 'fragment-complexity=medium:fragment-steps=5'),
    ('loop', 'fragment-loop=false:fragment-steps=5:vertex-steps=5'),
    ('loop', 'fragment-steps=5:fragment-uniform=false:vertex-steps=5'),
    ('loop', 'fragment-steps=5:fragment-uniform=true:vertex-steps=5'),
)


def _opt_parts(opts: str) -> List[str]:
    """把 glmark2 打印的选项串拆成一个个 `名=值`。

    · `<default>` = 这个场景没设任何选项 → 空列表
    · `duration=...` 去掉 —— 我们会自己指定它, 带着走会重复
    """
    o = (opts or "").strip()
    if not o or o == "<default>":
        return []
    return [p for p in o.split(":") if p and not p.startswith("duration=")]


def normalize_opts(opts: str) -> str:
    """归一化选项串 —— 判定时用来比对「是不是同一个场景」。"""
    return ":".join(_opt_parts(opts))


def identity(scene: str, opts: str) -> str:
    """场景身份 = 场景名 + 归一化后的选项串。

    比对时**必须**用归一化后的形式: 标定时 glmark2 没显式设 duration, 打印的
    选项串里没有它; 而我们自己跑的时候 duration 是显式给的, 打印出来会多出
    `duration=3` 这一项。不归一化就永远对不上。
    """
    return f"{scene}|{normalize_opts(opts)}"


def expected_identities() -> List[str]:
    """这一轮**应该**渲染出来的场景身份清单 (按顺序)。"""
    return [identity(s, o) for s, o in BENCHMARKS]


def build_bench_args(scene_sec: float) -> List[str]:
    """生成 `-b` 参数: 每个场景组合显式点名 + 统一的时长。

    返回的是 `["-b", "build:use-vbo=false:duration=3", "-b", ...]` 这种可以直接
    拼进命令行的形式 —— 一次给 glmark2 传**多条** `-b` (它支持重复使用),
    顺序就是执行顺序。
    """
    args: List[str] = []
    for scene, opts in BENCHMARKS:
        spec = ":".join([scene] + _opt_parts(opts) + [f"duration={scene_sec:g}"])
        args += ["-b", spec]
    return args
