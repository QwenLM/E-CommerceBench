from abc import ABC
from typing import Any, Dict, Type

TOOL_REGISTRY: Dict[str, Type] = {}


def register_tool(name: str):
    def decorator(cls):
        if name in TOOL_REGISTRY:
            raise ValueError(f"Tool '{name}' already registered.")
        cls.tool_name = name
        TOOL_REGISTRY[name] = cls
        return cls

    return decorator


class EcommerceBaseTool(ABC):
    @staticmethod
    def invoke(*args, **kwargs):
        raise NotImplementedError

    @staticmethod
    def get_info() -> Dict[str, Any]:
        raise NotImplementedError
