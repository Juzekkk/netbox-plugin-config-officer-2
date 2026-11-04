"""NetBox configuration for the integration tests (selected by tests/conftest.py).

Database and Redis are read from environment variables so the same file works
locally and in GitHub Actions. The defaults match tests/services.yml.
"""

import importlib.util
import os
import tempfile

ALLOWED_HOSTS = ["*"]
DATABASES = {
    "default": {
        "NAME": os.environ.get("DB_NAME", "netbox"),
        "USER": os.environ.get("DB_USER", "netbox"),
        "PASSWORD": os.environ.get("DB_PASSWORD", "netbox"),
        "HOST": os.environ.get("DB_HOST", "localhost"),
        "PORT": os.environ.get("DB_PORT", "5432"),
    }
}
_redis = {
    "HOST": os.environ.get("REDIS_HOST", "localhost"),
    "PORT": int(os.environ.get("REDIS_PORT", "6379")),
}
REDIS = {
    "tasks": {**_redis, "DATABASE": 0, "SSL": False},
    "caching": {**_redis, "DATABASE": 1, "SSL": False},
}
SECRET_KEY = "config-officer-tests-0123456789-0123456789-0123456789"
API_TOKEN_PEPPERS = {1: "config-officer-tests-pepper-0123456789-0123456789-0123456789"}
DEFAULT_PERMISSIONS = {}
PLUGINS = ["config_officer"]
# Optional: the BGP sync tests run only when the netbox-bgp plugin is installed.
if importlib.util.find_spec("netbox_bgp"):
    PLUGINS.append("netbox_bgp")
# Each test gets its own repository directory (tests/integration/conftest.py).
PLUGINS_CONFIG = {
    "config_officer": {
        "CONFIGS_REPO_DIR": os.path.join(tempfile.gettempdir(), "config_officer_tests")
    }
}
