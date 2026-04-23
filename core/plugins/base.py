"""Plugin base classes and metadata."""

from typing import Literal

from pydantic import BaseModel


class PluginMeta(BaseModel):
    """Plugin metadata for capability declaration and Skill orchestration."""

    name: str
    category: Literal["search", "generation", "analysis", "code", "data", "mcp"]
    capabilities: list[str] = []
    input_types: list[str] = []
    output_types: list[str] = []
    description: str = ""


# Registry mapping plugin name -> PluginMeta
PLUGIN_REGISTRY: dict[str, PluginMeta] = {}


def register_plugin(meta: PluginMeta):
    """Decorator to register a plugin with its metadata."""

    def decorator(cls):
        PLUGIN_REGISTRY[meta.name] = meta
        cls._plugin_meta = meta
        return cls

    return decorator
