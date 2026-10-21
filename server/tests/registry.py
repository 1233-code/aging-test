"""注册所有测试用例 - 在 main 中导入以填充 TEST_REGISTRY"""
from typing import List, Dict
from . import cpu, gpu, wifi, usb, ethernet, hdmi, fan, tfcard, memory, emmc  # noqa: F401

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
