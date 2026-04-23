"""
Central configuration for config_officer plugin.
"""

import os
import re
from django.conf import settings

_PS = settings.PLUGINS_CONFIG.get("config_officer", {})


def _get(key: str, default):
    return _PS.get(key, default)


# SSH / auth
DEVICE_USERNAME: str       = _get("DEVICE_USERNAME", "cisco")
DEVICE_PASSWORD: str       = _get("DEVICE_PASSWORD", "cisco")
DEVICE_SSH_PORT: int       = int(_get("DEVICE_SSH_PORT", 22))
DEVICE_SPECIFIC_CONF: dict = _get("DEVICE_SPECIFIC_CONF", {})

# Custom-field names
CF_SW_VERSION:        str = _get("CF_NAME_SW_VERSION",    "version")
CF_SSH:               str = _get("CF_NAME_SSH",           "ssh")
CF_LAST_COLLECT_DATE: str = _get("CF_NAME_LAST_COLLECT_DATE", "last_collect_date")
CF_LAST_COLLECT_TIME: str = _get("CF_NAME_LAST_COLLECT_TIME", "last_collect_time")
CF_COLLECTION_STATUS: str = _get("CF_NAME_COLLECTION_STATUS", "collection_status")

# Storage
CONFIGS_REPO_DIR: str = _get("NETBOX_DEVICES_CONFIGS_REPO_DIR", "/device_configs")
CONFIGS_SUBPATH:  str = _get("NETBOX_DEVICES_CONFIGS_SUBPATH", "netbox")
CONFIGS_PATH:     str = os.path.join(CONFIGS_REPO_DIR, CONFIGS_SUBPATH)

# Remote
_GIT_REMOTE_CFG    = _PS.get("GIT_REMOTE", {})
GIT_REMOTE_ENABLED: bool      = _GIT_REMOTE_CFG.get("ENABLED", True)
GIT_REMOTE_URL:     str | None = _GIT_REMOTE_CFG.get("URL")
GIT_REMOTE_NAME:    str        = _GIT_REMOTE_CFG.get("NAME", "origin")
GIT_REMOTE_BRANCH:  str        = _GIT_REMOTE_CFG.get("BRANCH", "netbox")
GIT_REMOTE_KEY:     str | None = _GIT_REMOTE_CFG.get("SSH_KEY_PATH")
GIT_AUTHOR:         str        = _GIT_REMOTE_CFG.get("AUTHOR", "Netbox <netbox@example.com>")

# Feature flags
COLLECT_INTERFACES_DATA:  bool = _get("COLLECT_INTERFACES_DATA", True)

# Timezone
TIME_ZONE: str = os.environ.get("TIME_ZONE", "UTC")

# Default platform
DEFAULT_PLATFORM:        str = _PS.get("DEFAULT_PLATFORM", "nxos")

# Config sanitisation
SENSITIVE_PREFIXES_DEFAULT: tuple[str, ...] = (
    "username",
    "ssh",
    "snmp-server user",
    "crypto",
    "key",
    "password",
)
SENSITIVE_PREFIXES: tuple[str, ...] = tuple(
    _get("SENSITIVE_PREFIXES", SENSITIVE_PREFIXES_DEFAULT)
)

VOLATILE_LINE_PATTERNS_DEFAULT: list[re.Pattern] = [
    re.compile(r"^!Time:"),
    re.compile(r"^!Running configuration last done at:"),
    re.compile(r"^!NVRAM config last updated"),
    re.compile(r"^! Last configuration change"),
    re.compile(r"^ntp clock-period"),
]
VOLATILE_LINE_PATTERNS: tuple[str, ...] = tuple(
    _get("VOLATILE_LINE_PATTERNS", VOLATILE_LINE_PATTERNS_DEFAULT)
)

# Common regexes
REGEX_IP:  str = r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}"
REGEX_IPP: str = r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}/\d{1,2}"  # IP/prefix-length
