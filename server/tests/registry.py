"""注册所有测试用例 - 在 main 中导入以填充 TEST_REGISTRY"""
from typing import List, Dict

# ⚠️ **导入顺序 = 注册顺序 = `get_all_tests()` 返回的顺序**(2026-11-04)。
#   这个顺序是**给人看的清单**(前端展示用的是后端下发的 `recommended_order`,
#   那边才是真正生效的执行顺序 —— 见 `server/runner.py` 的 `RECOMMENDED_ORDER`),
#   但两份清单**摆成一样的顺序**, 免得人对着 API 响应和文档各看各的。
#   ⇒ 这里是: 轻量 → 接口 → 射频 → 重载(WiFi 在 GPU 之前, 2026-11-04)。
#   ⇒ 蓝牙项紧跟在 wifi 后面(§6.4.1, 2026-11-04 实现)—— 它是**只读**的,
#     排在 WiFi 后面才看得到「WiFi 动完射频之后蓝牙还是不是关着的」这个
#     **最终状态**(§6.4.1 的位置理由)。
from . import (                     # noqa: F401
    memory, emmc, tfcard,           # ① 轻量
    hdmi, usb, ethernet, fan,       # ② 接口与外设
    wifi, bluetooth,                # ③ 射频(WiFi 会动全局状态, 蓝牙只读)
    gpu, cpu,                       # ④ 重载
)

from .base import TEST_REGISTRY


def get_all_tests() -> List[Dict]:
    """返回所有测试项的元信息,供前端用"""
    return [
        {
            "name": cls.name,
            "display_name": cls.display_name,
            "description": cls.description,
        }
        for cls in TEST_REGISTRY.values()
    ]
