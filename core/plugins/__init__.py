"""Plugin auto-discovery module.

Automatically discovers all BaseTool subclasses in core/plugins/ and
registers them in the plugin registry.
"""

from core.plugins.base import PLUGIN_REGISTRY, PluginMeta
from core.plugins.execute_code import ExecuteCodeParams, ExecuteCodeTool
from core.plugins.fetch_url import FetchUrlParams, FetchUrlTool
from core.plugins.image_editing import ImageEditingParams, ImageEditingTool
from core.plugins.image_generation import ImageGenerationParams, ImageGenerationTool
from core.plugins.image_replication import ImageReplicationParams, ImageReplicationTool

# from core.plugins.html_render import HtmlRenderTool, HtmlRenderParams
from core.plugins.image_understanding import ImageUnderstandingParams, ImageUnderstandingTool
from core.plugins.reddit_scraper import RedditScraperParams, RedditScraperTool

# Import all plugin modules to trigger registration
from core.plugins.web_search import WebSearchParams, WebSearchTool
from utils.logger import logger

# All available plugin classes.
# Keys use snake_case to match PluginMeta.name and Tool.name. Lookup in
# `initialize_plugins` is tolerant (normalizes separators + case), so legacy
# forms like "WebSearchTool", "web-search", or "websearch" still resolve.
PLUGIN_CLASSES: dict[str, type] = {
    "web_search": WebSearchTool,
    "fetch_url": FetchUrlTool,
    "image_generation": ImageGenerationTool,
    "image_editing": ImageEditingTool,
    "reddit_scraper": RedditScraperTool,
    # "html_render": HtmlRenderTool,
    "image_understanding": ImageUnderstandingTool,
    "image_replication": ImageReplicationTool,
    "execute_code": ExecuteCodeTool,
}


def discover_plugins() -> dict[str, type]:
    """Return all available plugin classes.

    Uses the pre-registered PLUGIN_CLASSES dict. New plugins are automatically
    available by importing them above and adding to the dict.
    """
    return PLUGIN_CLASSES.copy()


def get_plugin_meta(name: str) -> PluginMeta | None:
    """Get metadata for a specific plugin."""
    return PLUGIN_REGISTRY.get(name)


def list_plugin_metas() -> list[PluginMeta]:
    """List metadata for all registered plugins."""
    return list(PLUGIN_REGISTRY.values())


def _normalize_plugin_name(s: str) -> str:
    """Collapse a plugin identifier to a comparable canonical form.

    Strips the ``Tool`` suffix, lowercases, and removes ``_`` / ``-`` so that
    ``web_search`` / ``web-search`` / ``websearch`` / ``WebSearchTool`` all
    map to the same key. Both PLUGIN_CLASSES keys and incoming names are
    collapsed through this helper.
    """
    return s.lower().removesuffix("tool").replace("_", "").replace("-", "")


def initialize_plugins(
    plugin_names: list[str],
    **kwargs,
) -> list:
    """Initialize a list of plugins by name.

    Name matching is tolerant: ``web_search`` / ``web-search`` / ``websearch``
    / ``WebSearchTool`` all resolve to the same plugin entry.

    Args:
        plugin_names: List of plugin names to initialize.
        **kwargs: Extra keyword arguments passed to plugin constructors
                  (e.g., user_id, session_id).

    Returns:
        List of initialized plugin instances.
    """
    plugins = discover_plugins()
    normalized_index = {_normalize_plugin_name(k): k for k in plugins}
    instances = []
    for name in plugin_names:
        key = normalized_index.get(_normalize_plugin_name(name))
        if key is not None:
            try:
                instance = plugins[key](**kwargs)
                instances.append(instance)
                logger.debug(f"Plugin initialized: {key}")
            except Exception as e:
                logger.error(f"Failed to initialize plugin '{key}': {e}")
        else:
            logger.warning(f"Plugin not found: {name} (normalized: {_normalize_plugin_name(name)})")
    return instances
