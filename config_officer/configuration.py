"""
Central configuration for config_officer plugin.
"""

import os
import re

from django.conf import settings

_PS = settings.PLUGINS_CONFIG.get("config_officer", {})


def _get(key: str, default, env_var: str | None = None):
    """
    Resolution order:
      1. Environment variable (env_var, or CO_<key> by default)
      2. PLUGINS_CONFIG value
      3. default
    """
    env_key = env_var or f"CO_{key}"
    env_val = os.environ.get(env_key)
    if env_val is not None:
        return env_val
    return _PS.get(key, default)


def _get_bool(key: str, default: bool, env_var: str | None = None) -> bool:
    val = _get(key, default, env_var)
    if isinstance(val, bool):
        return val
    return str(val).lower() in ("1", "true", "yes")


def _get_int(key: str, default: int, env_var: str | None = None) -> int:
    return int(_get(key, default, env_var))


# SSH / auth
DEVICE_USERNAME: str = _get("DEVICE_USERNAME", "cisco")
DEVICE_PASSWORD: str = _get("DEVICE_PASSWORD", "cisco")
DEVICE_SSH_PORT: int = _get_int("DEVICE_SSH_PORT", 22)
DEVICE_SPECIFIC_CONF: dict = _get("DEVICE_SPECIFIC_CONF", {})

# Custom-field names
CF_SW_VERSION: str = _get("CF_NAME_SW_VERSION", "version")
CF_SSH: str = _get("CF_NAME_SSH", "ssh")
CF_LAST_COLLECT_DATE: str = _get("CF_NAME_LAST_COLLECT_DATE", "last_collect_date")
CF_LAST_COLLECT_TIME: str = _get("CF_NAME_LAST_COLLECT_TIME", "last_collect_time")

# Storage
CONFIGS_REPO_DIR: str = _get("CONFIGS_REPO_DIR", "/device_configs")
CONFIGS_SUBPATH: str = _get("CONFIGS_SUBPATH", "netbox")
CONFIGS_PATH: str = os.path.join(CONFIGS_REPO_DIR, CONFIGS_SUBPATH)

# Remote
_GIT_REMOTE_CFG = _PS.get("GIT_REMOTE", {})


def _get_remote(key: str, default, env_var: str | None = None):
    env_key = env_var or f"CO_GIT_REMOTE_{key}"
    env_val = os.environ.get(env_key)
    if env_val is not None:
        return env_val
    return _GIT_REMOTE_CFG.get(key, default)


GIT_REMOTE_ENABLED: bool = _get_bool("ENABLED", True, "CO_GIT_REMOTE_ENABLED")
GIT_REMOTE_URL: str | None = _get_remote("URL", None)
GIT_REMOTE_NAME: str = _get_remote("NAME", "origin")
GIT_REMOTE_BRANCH: str = _get_remote("BRANCH", "netbox")
GIT_REMOTE_KEY: str | None = _get_remote("SSH_KEY_PATH", None)
GIT_AUTHOR: str = _get_remote("AUTHOR", "Netbox <netbox@example.com>")

# Feature flags
COLLECT_INTERFACES_DATA: bool = _get_bool("COLLECT_INTERFACES_DATA", True)
COLLECT_PORT_CHANNEL_DATA: bool = _get_bool("COLLECT_PORT_CHANNEL_DATA", True)

# Timezone
TIME_ZONE: str = os.environ.get("TIME_ZONE", "UTC")

# Default platform
DEFAULT_PLATFORM: str = _get("DEFAULT_PLATFORM", "nxos")

# Config sanitisation
SENSITIVE_PREFIXES_DEFAULT: tuple[str, ...] = (
    "username",
    "ssh",
    "snmp-server user",
    "crypto",
    "key",
    "password",
)
SENSITIVE_PREFIXES: tuple[str, ...] = tuple(_get("SENSITIVE_PREFIXES", SENSITIVE_PREFIXES_DEFAULT))

VOLATILE_LINE_PATTERNS_DEFAULT = [
    r"^!Time:",
    r"^!Running configuration last done at:",
    r"^!NVRAM config last updated",
    r"^! Last configuration change",
    r"^ntp clock-period",
]
VOLATILE_LINE_PATTERNS: tuple[str, ...] = tuple(
    _get("VOLATILE_LINE_PATTERNS", VOLATILE_LINE_PATTERNS_DEFAULT)
)
VOLATILE_LINE_PATTERNS_COMPILED = [re.compile(p) for p in VOLATILE_LINE_PATTERNS]

# Common regexes
REGEX_IP: str = r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}"
REGEX_IPP: str = r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}/\d{1,2}"
