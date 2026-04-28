"""
tests/conftest.py
-----------------
Pytest configuration for unit tests that run without a full NetBox installation.

The problem: importing anything from the `config_officer` package triggers
`config_officer/__init__.py`, which calls `from netbox.plugins import PluginConfig`.
NetBox is not installed in the test environment, so the import fails immediately.

The fix: register a minimal stub for the `netbox` namespace *before* any test
module is collected. This satisfies the `PluginConfig` import without requiring
NetBox itself to be present.
"""

from __future__ import annotations

import sys
import types


def _make_netbox_stub() -> None:
    """Inject a lightweight netbox.plugins stub into sys.modules."""
    if "netbox" in sys.modules:
        return  # already present (real NetBox or a previous stub)

    # netbox
    netbox = types.ModuleType("netbox")

    # netbox.plugins
    plugins = types.ModuleType("netbox.plugins")

    class PluginConfig:
        """Minimal stand-in for netbox.plugins.PluginConfig."""

        name: str = ""

        def __init_subclass__(cls, **kwargs: object) -> None:
            super().__init_subclass__(**kwargs)

    plugins.PluginConfig = PluginConfig  # type: ignore[attr-defined]
    netbox.plugins = plugins  # type: ignore[attr-defined]

    sys.modules["netbox"] = netbox
    sys.modules["netbox.plugins"] = plugins


# Run immediately when conftest.py is loaded - before any test collection
_make_netbox_stub()
