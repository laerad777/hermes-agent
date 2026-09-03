"""Standalone Hermes continuity observer plugin."""

from typing import Any

from .lifecycle import register as register_lifecycle
from .skills import register_assets


def register(ctx: Any) -> None:
    """Register generic lifecycle observers and packaged read-only skills."""
    register_lifecycle(ctx)
    register_assets(ctx)

__all__ = ["register"]
