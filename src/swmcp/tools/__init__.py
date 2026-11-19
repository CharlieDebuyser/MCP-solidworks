"""Outils MCP du catalogue (spec §5). L'import des modules remplit le registre `TOOLS`."""

from . import api, features, inspect, session, sketch
from .registry import TOOLS, ToolSpec, exposed

__all__ = ["TOOLS", "ToolSpec", "api", "exposed", "features", "inspect", "session", "sketch"]
