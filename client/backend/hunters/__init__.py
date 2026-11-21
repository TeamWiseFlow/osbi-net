"""Platform-specific catalog discovery hunters."""
from .base import CatalogDiscovery, Hunter, HUNTERS, hunter_for, register
from . import wx_mp  # noqa: F401  (registration side effect)

__all__ = ["CatalogDiscovery", "Hunter", "HUNTERS", "hunter_for", "register"]
