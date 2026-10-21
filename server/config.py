"""配置加载 - 读取 config.yaml"""
from pathlib import Path
from typing import Any
import yaml


PROJECT_ROOT = Path(__file__).parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"


class Config:
    _data: dict = {}
    _path: Path = DEFAULT_CONFIG_PATH

    @classmethod
    def load(cls, path: Path = None) -> dict:
        cls._path = path or DEFAULT_CONFIG_PATH
        if not cls._path.exists():
            raise FileNotFoundError(f"配置文件不存在: {cls._path}")
        with open(cls._path, "r", encoding="utf-8") as f:
            cls._data = yaml.safe_load(f) or {}
        return cls._data

    @classmethod
    def get(cls, key: str, default: Any = None) -> Any:
        """支持点号路径,如 'server.port'"""
        if not cls._data:
            cls.load()
        keys = key.split(".")
        val = cls._data
        for k in keys:
            if isinstance(val, dict) and k in val:
                val = val[k]
            else:
                return default
        return val

    @classmethod
    def all(cls) -> dict:
        if not cls._data:
            cls.load()
        return cls._data

    @classmethod
    def project_root(cls) -> Path:
        return PROJECT_ROOT
