"""
Device configuration collector for config_officer plugin.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import re
import socket
from datetime import datetime

import pytz
from django.db import transaction

from scrapli.driver.core import IOSXEDriver, IOSXRDriver, NXOSDriver

from .choices import CollectFailChoices
from .config import (
    CF_COLLECTION_STATUS,
    CF_LAST_COLLECT_DATE,
    CF_LAST_COLLECT_TIME,
    CF_SSH,
    CF_SW_VERSION,
    COLLECT_INTERFACES_DATA,
    CONFIGS_PATH,
    DEVICE_PASSWORD,
    DEVICE_SPECIFIC_CONF,
    DEVICE_SSH_PORT,
    DEVICE_USERNAME,
    SENSITIVE_PREFIXES,
    TIME_ZONE,
)
from .custom_exceptions import CollectionException
from .models import ParsedDevice, ParsedInterface
from .netbox_sync import sync_interfaces_to_netbox
from .parsers import IOSXEParser, NXOSParser

logger = logging.getLogger(__name__)

PLATFORMS: dict[str, type] = {
    "iosxe": IOSXEDriver,
    "nxos":  NXOSDriver,
    "iosxr": IOSXRDriver,
}


def _resolve_credentials(hostname: str) -> tuple[str, str, int]:
    """Return (username, password, port) for *hostname*, preferring per-device overrides."""
    device_conf = DEVICE_SPECIFIC_CONF.get(hostname, {})
    username    = device_conf.get("DEVICE_USERNAME", DEVICE_USERNAME)
    password    = device_conf.get("DEVICE_PASSWORD", DEVICE_PASSWORD)
    port        = int(device_conf.get("DEVICE_SSH_PORT", DEVICE_SSH_PORT))

    if device_conf:
        logger.info("[CREDS] Per-device config for %r: user=%r port=%d", hostname, username, port)
    else:
        logger.debug("[CREDS] Global config for %r: user=%r port=%d", hostname, username, port)

    return username, password, port


def sanitize_config(config_text: str) -> str:
    """Strip lines that begin with sensitive keywords (passwords, keys, …)."""
    pattern = re.compile(
        r"^\s*(?:{})\b".format("|".join(re.escape(p) for p in SENSITIVE_PREFIXES)),
        re.IGNORECASE,
    )
    return "\n".join(line for line in config_text.splitlines() if not pattern.match(line))


def _collect_iosxe(conn, host_ip: str) -> tuple[ParsedDevice, dict[str, ParsedInterface]]:
    device  = IOSXEParser.parse_show_version(_send(conn, "show version"))
    ifaces: dict[str, ParsedInterface] = {}

    if COLLECT_INTERFACES_DATA:
        ifaces = IOSXEParser.parse_show_interfaces(_send(conn, "show interfaces"), host_ip)
        IOSXEParser.parse_show_ip_interface(_send(conn, "show ip interface"), ifaces)
        logger.info("[COLLECT][IOSXE] Parsed %d interfaces", len(ifaces))

    return device, ifaces


def _collect_nxos(conn, host_ip: str) -> tuple[ParsedDevice, dict[str, ParsedInterface]]:
    device  = NXOSParser.parse_show_version(_send(conn, "show version"))
    ifaces: dict[str, ParsedInterface] = {}

    if COLLECT_INTERFACES_DATA:
        ifaces = NXOSParser.parse_show_interfaces(_send(conn, "show interface"), host_ip)
        logger.info("[COLLECT][NXOS] Parsed %d interfaces", len(ifaces))

    return device, ifaces


def _collect_iosxr(conn, host_ip: str) -> tuple[ParsedDevice, dict[str, ParsedInterface]]:
    # IOS-XR 'show version' is close enough to IOS-XE for our purposes
    device  = IOSXEParser.parse_show_version(_send(conn, "show version"))
    ifaces: dict[str, ParsedInterface] = {}

    if COLLECT_INTERFACES_DATA:
        ifaces = IOSXEParser.parse_show_interfaces(_send(conn, "show interfaces"), host_ip)
        logger.info("[COLLECT][IOSXR] Parsed %d interfaces", len(ifaces))

    return device, ifaces


_PLATFORM_COLLECTORS = {
    "iosxe": _collect_iosxe,
    "nxos":  _collect_nxos,
    "iosxr": _collect_iosxr,
}


def _send(conn, cmd: str) -> str:
    logger.debug("[CMD] -> %r", cmd)
    result = conn.send_command(cmd).result
    logger.debug("[CMD] <- %d chars", len(result))
    return result


def _ssh_config_path() -> str:
    pkg_origin = importlib.util.find_spec("config_officer").origin
    return os.path.join(os.path.dirname(pkg_origin), "ssh_config")


# ---------------------------------------------------------------------------
# Main collector
# ---------------------------------------------------------------------------

class CollectDeviceData:
    """
    Connect to a device, collect its state, and persist results to NetBox.
    """

    def __init__(self, collect_task, ip: str = "", hostname_ipam: str = "", platform: str = ""):
        self.task          = collect_task
        self.hostname_ipam = hostname_ipam.strip()
        self.platform      = platform if platform in PLATFORMS else "iosxe"

        username, password, port = _resolve_credentials(self.hostname_ipam)

        self._base_kwargs: dict = {
            "host":            ip,
            "auth_username":   username,
            "auth_password":   password,
            "auth_strict_key": False,
            "port":            port,
            "timeout_socket":  20,
            "timeout_ops":     60,
            "ssh_config_file": _ssh_config_path(),
        }

        # Populated during collection
        self._device:     ParsedDevice            = ParsedDevice()
        self._interfaces: dict[str, ParsedInterface] = {}
        self._used_kwargs: dict                   = {}

        logger.info(
            "[COLLECT] Initialized: %r host=%s platform=%s port=%d",
            self.hostname_ipam, ip, self.platform, port,
        )


    def _check_reachability(self) -> None:
        host    = self._base_kwargs["host"]
        timeout = self._base_kwargs["timeout_socket"]

        for port in (22, 23):
            try:
                with socket.create_connection((host, port), timeout=timeout):
                    logger.debug("[REACH] Port %d open on %s", port, host)
                    return
            except OSError as e:
                logger.debug("[REACH] Port %d closed on %s: %s", port, host, e)

        raise CollectionException(
            reason=CollectFailChoices.FAIL_CONNECT,
            message="Device unreachable on ports 22 and 23",
        )


    def _connect_and_collect(self) -> None:
        """Try SSH, fall back to Telnet. Raises CollectionException on total failure."""
        host      = self._base_kwargs["host"]
        driver    = PLATFORMS[self.platform]
        collector = _PLATFORM_COLLECTORS[self.platform]

        # --- SSH ---
        logger.debug("[CONNECT] Attempting SSH to %s", host)
        try:
            with driver(**self._base_kwargs) as conn:
                logger.info("[CONNECT] SSH connected to %s", host)
                self._device, self._interfaces = collector(conn, host)
                self._used_kwargs = self._base_kwargs
                return
        except Exception as e:
            logger.warning("[CONNECT] SSH failed: %s: %s", type(e).__name__, e)

        # --- Telnet fallback ---
        logger.debug("[CONNECT] Falling back to Telnet on %s", host)
        telnet_kwargs = {**self._base_kwargs, "port": 23, "transport": "telnet"}
        try:
            with driver(**telnet_kwargs) as conn:
                logger.info("[CONNECT] Telnet connected to %s", host)
                self._device, self._interfaces = collector(conn, host)
                self._used_kwargs = telnet_kwargs
        except Exception as e:
            logger.error("[CONNECT] Telnet also failed: %s: %s", type(e).__name__, e)
            raise CollectionException(
                reason=CollectFailChoices.FAIL_LOGIN,
                message="Cannot login via SSH or Telnet",
            )


    def _check_serial_match(self, device_netbox) -> None:
        """Raise CollectionException if the collected serial doesn't match NetBox."""
        nb_sn  = device_netbox.serial
        dev_sn = self._device.serial
        if nb_sn and dev_sn and nb_sn != dev_sn:
            raise CollectionException(
                reason=CollectFailChoices.FAIL_UPDATE,
                message=f"Serial mismatch: NetBox={nb_sn!r} Device={dev_sn!r}",
            )


    def _update_custom_fields(self, device_netbox) -> None:
        tz   = pytz.timezone(TIME_ZONE)
        now  = datetime.now(tz)
        port = self._used_kwargs.get("port", self._base_kwargs["port"])

        fields = {
            CF_COLLECTION_STATUS: "temporary value",
            CF_SSH:               port == 22,
            CF_SW_VERSION:        self._device.version.upper() if self._device.version else "",
            CF_LAST_COLLECT_DATE: now.date(),
            CF_LAST_COLLECT_TIME: now.strftime("%H:%M:%S"),
        }
        for cf_name, cf_value in fields.items():
            if not cf_name:
                continue
            logger.debug("[CF] %r = %r", cf_name, cf_value)
            device_netbox.custom_field_data[cf_name] = cf_value

        device_netbox.save()


    def _save_running_config(self) -> None:
        os.makedirs(CONFIGS_PATH, exist_ok=True)
        filename = os.path.join(CONFIGS_PATH, f"{self.hostname_ipam}_running.txt")

        driver = PLATFORMS[self.platform]
        with driver(**self._used_kwargs) as conn:
            conn.send_command("terminal length 0")
            raw    = conn.send_command("show running-config").result
            clean  = sanitize_config(raw)

        with open(filename, "w") as fh:
            fh.write(clean)

        logger.info("[COLLECT] Running config saved -> %s", filename)


    # Main entry point
    def collect_information(self) -> None:
        logger.info(
            "[COLLECT] ===== START %r host=%s platform=%s =====",
            self.hostname_ipam, self._base_kwargs["host"], self.platform,
        )

        self._check_reachability()
        self._connect_and_collect()

        device_netbox = self.task.device
        self._check_serial_match(device_netbox)
        self._update_custom_fields(device_netbox)

        if COLLECT_INTERFACES_DATA and self._interfaces:
            sync_interfaces_to_netbox(device_netbox, self._interfaces)

        self._save_running_config()

        logger.info("[COLLECT] ===== COMPLETE %r =====", self.hostname_ipam)
