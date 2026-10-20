"""Optional plugins: extra routes, MCP tools, token identities and call authorization.

Ringabot's MCP server is complete without any plugin. An operator can add one by naming
`module:factory` entries in RINGABOT_PLUGINS (comma-separated). Each factory is called with a
PluginContext and returns a Plugin. Nothing is loaded by default, and plugins run in-process
with the operator's own privileges, so only list code you trust.

A plugin may:
- add HTTP routes (`routes`) and MCP tools (`tools` + `call_tool`);
- recognise extra bearer tokens (`identify`), returning any object with an `id` attribute;
  that id scopes the calls the identity can see and counts against its rate limit;
- veto an MCP action for an identity (`authorize` returns an error message).
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Any


class PluginError(RuntimeError):
    pass


@dataclass(frozen=True)
class PluginContext:
    settings: Any
    store: Any
    manager: Any


class Plugin:
    """Base class: every hook has a do-nothing default."""

    name = "plugin"

    def routes(self) -> list:
        """Extra starlette Route objects."""
        return []

    def tools(self) -> list[dict]:
        """Extra MCP tool definitions (name, description, inputSchema)."""
        return []

    def call_tool(self, name: str, who: Any, args: dict) -> dict | None:
        """Run one of this plugin's tools. Return an MCP result dict, or None if not yours."""
        return None

    def identify(self, token: str) -> Any | None:
        """Return an identity object (with an `id`) for a bearer token you recognise."""
        return None

    def authorize(self, who: Any, action: str) -> str | None:
        """Return an error message to refuse `action` (e.g. "place_call") for `who`."""
        return None


def load_plugins(spec: list[str], ctx: PluginContext) -> list[Plugin]:
    loaded: list[Plugin] = []
    for entry in spec:
        module_name, sep, factory_name = entry.partition(":")
        if not sep or not module_name or not factory_name:
            raise PluginError(f"RINGABOT_PLUGINS entry {entry!r} must look like module:factory")
        try:
            factory = getattr(importlib.import_module(module_name), factory_name)
        except (ImportError, AttributeError) as exc:
            raise PluginError(f"cannot load plugin {entry!r}: {type(exc).__name__}") from exc
        plugin = factory(ctx)
        if not isinstance(plugin, Plugin):
            raise PluginError(f"plugin {entry!r} did not return a ringabot.plugins.Plugin")
        loaded.append(plugin)
    return loaded
