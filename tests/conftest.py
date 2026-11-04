"""Shared pytest setup.

The tests come in two groups:

* ``tests/unit``        - pure Python; run anywhere with ``pytest``.
* ``tests/integration`` - need a NetBox source checkout plus PostgreSQL and Redis;
                          run with ``NETBOX_PATH=/path/to/netbox pytest``.

``NETBOX_PATH`` points at the root of a NetBox git checkout. When it is set,
Django is configured with NetBox's settings and tests/netbox_configuration.py.
When it is not set, the integration tests are skipped and a small stub of
``netbox.plugins`` lets ``import config_officer`` work without NetBox.
"""

import os
import sys
import types
from pathlib import Path

import pytest

NETBOX_PATH = os.environ.get("NETBOX_PATH")
FIXTURES = Path(__file__).parent / "fixtures"

if NETBOX_PATH:
    sys.path.insert(0, str(Path(NETBOX_PATH).resolve() / "netbox"))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "netbox.settings")
    os.environ.setdefault("NETBOX_CONFIGURATION", "tests.netbox_configuration")

    import django

    django.setup()  # pytest-django only does this itself when settings were configured earlier
else:
    collect_ignore = ["integration"]

    netbox = types.ModuleType("netbox")
    netbox.plugins = types.ModuleType("netbox.plugins")
    netbox.plugins.PluginConfig = type("PluginConfig", (), {})
    sys.modules.setdefault("netbox", netbox)
    sys.modules.setdefault("netbox.plugins", netbox.plugins)


# Variables git sets for hooks (pre-commit runs the unit tests from one). They would
# point the git commands in the storage tests at this project's repository.
_GIT_HOOK_VARIABLES = (
    "GIT_DIR",
    "GIT_INDEX_FILE",
    "GIT_WORK_TREE",
    "GIT_PREFIX",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
)


@pytest.fixture(autouse=True)
def _isolated_git_environment(monkeypatch):
    for name in _GIT_HOOK_VARIABLES:
        monkeypatch.delenv(name, raising=False)
