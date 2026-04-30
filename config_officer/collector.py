"""
Device configuration collector for config_officer plugin.
"""

from __future__ import annotations

import logging
import os
import re
import socket
from datetime import datetime
from zoneinfo import ZoneInfo

from scrapli.driver.core import IOSXEDriver, IOSXRDriver, NXOSDriver

from .choices import CollectFailChoices
from .config import (
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
    "nxos": NXOSDriver,
    "iosxr": IOSXRDriver,
}


def _resolve_credentials(hostname: str) -> tuple[str, str, int]:
    """Return (username, password, port) for *hostname*, preferring per-device overrides."""
    device_conf = DEVICE_SPECIFIC_CONF.get(hostname, {})
    username = device_conf.get("DEVICE_USERNAME", DEVICE_USERNAME)
    password = device_conf.get("DEVICE_PASSWORD", DEVICE_PASSWORD)
    port = int(device_conf.get("DEVICE_SSH_PORT", DEVICE_SSH_PORT))

    if device_conf:
        logger.info(
            "[CREDS] Per-device override for %r: user=%r port=%d",
            hostname,
            username,
            port,
        )
    else:
        logger.info(
            "[CREDS] Global credentials for %r: user=%r port=%d (password set: %s)",
            hostname,
            username,
            port,
            bool(password),
        )

    return username, password, port


def sanitize_config(config_text: str) -> str:
    """Strip lines that begin with sensitive keywords (passwords, keys, …)."""
    pattern = re.compile(
        r"^\s*(?:{})\b".format("|".join(re.escape(p) for p in SENSITIVE_PREFIXES)),
        re.IGNORECASE,
    )
    return "\n".join(line for line in config_text.splitlines() if not pattern.match(line))


def _collect_iosxe(conn, host_ip: str) -> tuple[ParsedDevice, dict[str, ParsedInterface]]:
    device = IOSXEParser.parse_show_version(_send(conn, "show version"))
    ifaces: dict[str, ParsedInterface] = {}

    if COLLECT_INTERFACES_DATA:
        ifaces = IOSXEParser.parse_show_interfaces(_send(conn, "show interfaces"), host_ip)
        IOSXEParser.parse_show_ip_interface(_send(conn, "show ip interface"), ifaces)
        logger.info("[COLLECT][IOSXE] Parsed %d interfaces", len(ifaces))

    return device, ifaces


def _collect_nxos(conn, host_ip: str) -> tuple[ParsedDevice, dict[str, ParsedInterface]]:
    device = NXOSParser.parse_show_version(_send(conn, "show version"))
    ifaces: dict[str, ParsedInterface] = {}

    if COLLECT_INTERFACES_DATA:
        ifaces = NXOSParser.parse_show_interfaces(_send(conn, "show interface"), host_ip)
        logger.info("[COLLECT][NXOS] Parsed %d interfaces", len(ifaces))

    return device, ifaces


def _collect_iosxr(conn, host_ip: str) -> tuple[ParsedDevice, dict[str, ParsedInterface]]:
    device = IOSXEParser.parse_show_version(_send(conn, "show version"))
    ifaces: dict[str, ParsedInterface] = {}

    if COLLECT_INTERFACES_DATA:
        ifaces = IOSXEParser.parse_show_interfaces(_send(conn, "show interfaces"), host_ip)
        logger.info("[COLLECT][IOSXR] Parsed %d interfaces", len(ifaces))

    return device, ifaces


_PLATFORM_COLLECTORS = {
    "iosxe": _collect_iosxe,
    "nxos": _collect_nxos,
    "iosxr": _collect_iosxr,
}


def _send(conn, cmd: str) -> str:
    logger.debug("[CMD] -> %r", cmd)
    result = conn.send_command(cmd).result
    logger.debug("[CMD] <- %d chars", len(result))
    return result


# ---------------------------------------------------------------------------
# Main collector
# ---------------------------------------------------------------------------


class CollectDeviceData:
    """
    Connect to a device, collect its state, and persist results to NetBox.
    """

    def __init__(self, collect_task, ip: str = "", hostname_ipam: str = "", platform: str = ""):
        self.task = collect_task
        self.hostname_ipam = hostname_ipam.strip()
        self.platform = platform if platform in PLATFORMS else "iosxe"

        username, password, port = _resolve_credentials(self.hostname_ipam)

        self._base_kwargs: dict = {
            "host": ip,
            "auth_username": username,
            "auth_password": password,
            "auth_strict_key": False,
            "port": port,
            "timeout_socket": 20,
            "timeout_ops": 60,
        }

        self._device: ParsedDevice = ParsedDevice()
        self._interfaces: dict[str, ParsedInterface] = {}
        self._used_kwargs: dict = {}

        logger.info(
            "[COLLECT] Initialized: %r host=%s platform=%s port=%d",
            self.hostname_ipam,
            ip,
            self.platform,
            port,
        )

    def _check_reachability(self) -> None:
        """Try SSH, fall back to Telnet. Raises CollectionException on total failure."""
        host = self._base_kwargs["host"]
        timeout = self._base_kwargs["timeout_socket"]

        logger.info("[REACH] Checking reachability of %s (timeout=%ds)", host, timeout)

        last_error: Exception | None = None
        for port in (22, 23):
            try:
                with socket.create_connection((host, port), timeout=timeout):
                    logger.info("[REACH] Port %d is OPEN on %s - proceeding", port, host)
                    return
            except TimeoutError:
                logger.info("[REACH] Port %d on %s: TIMEOUT after %ds", port, host, timeout)
                last_error = TimeoutError(f"timeout after {timeout}s")
            except ConnectionRefusedError:
                logger.info("[REACH] Port %d on %s: CONNECTION REFUSED", port, host)
                last_error = ConnectionRefusedError("connection refused")
            except OSError as e:
                logger.info("[REACH] Port %d on %s: OSError: %s", port, host, e)
                last_error = e

        logger.error(
            "[REACH] %s is UNREACHABLE on both ports 22 and 23. Last error: %s",
            host,
            last_error,
        )
        raise CollectionException(
            reason=CollectFailChoices.FAIL_CONNECT,
            message="Device unreachable on ports 22 and 23",
        )

    def _connect_and_collect(self) -> None:
        """Connect to device and collect the data. Raises CollectionException on total failure."""
        host = self._base_kwargs["host"]
        port = self._base_kwargs["port"]
        driver = PLATFORMS[self.platform]
        collector = _PLATFORM_COLLECTORS[self.platform]

        # SSH
        logger.info(
            "[CONNECT] Attempting SSH to %s:%d as %r (platform=%s)",
            host,
            port,
            self._base_kwargs["auth_username"],
            self.platform,
        )
        try:
            with driver(**self._base_kwargs) as conn:
                logger.info("[CONNECT] SSH connection established to %s:%d", host, port)
                self._device, self._interfaces = collector(conn, host)
                self._used_kwargs = self._base_kwargs
                logger.info("[CONNECT] Data collection via SSH complete for %s", host)
                return
        except Exception as e:
            logger.warning(
                "[CONNECT] SSH failed for %s:%d - %s: %s",
                host,
                port,
                type(e).__name__,
                e,
            )

        # Telnet fallback
        logger.info("[CONNECT] Attempting Telnet fallback to %s:23", host)
        telnet_kwargs = {**self._base_kwargs, "port": 23, "transport": "telnet"}
        try:
            with driver(**telnet_kwargs) as conn:
                logger.info("[CONNECT] Telnet connection established to %s:23", host)
                self._device, self._interfaces = collector(conn, host)
                self._used_kwargs = telnet_kwargs
                logger.info("[CONNECT] Data collection via Telnet complete for %s", host)
        except Exception as e:
            logger.error(
                "[CONNECT] Telnet also failed for %s:23 - %s: %s",
                host,
                type(e).__name__,
                e,
            )
            raise CollectionException from e(
                reason=CollectFailChoices.FAIL_LOGIN,
                message="Cannot login via SSH or Telnet",
            )

    def _check_serial_match(self, device_netbox) -> None:
        """
        Raise CollectionException if the collected serial doesn't match NetBox.
        If serial is not defined in NetBox but collected from device, saves it automatically.
        """
        nb_sn = device_netbox.serial
        dev_sn = self._device.serial
        logger.info("[SERIAL] NetBox serial=%r collected serial=%r", nb_sn, dev_sn)

        if nb_sn and dev_sn and nb_sn != dev_sn:
            logger.error(
                "[SERIAL] Mismatch for %r: NetBox=%r Device=%r",
                self.hostname_ipam,
                nb_sn,
                dev_sn,
            )
            raise CollectionException(
                reason=CollectFailChoices.FAIL_UPDATE,
                message=f"Serial mismatch: NetBox={nb_sn!r} Device={dev_sn!r}",
            )

        elif not nb_sn and dev_sn:
            logger.info(
                "[SERIAL] No serial in NetBox for %r, saving collected serial=%r",
                self.hostname_ipam,
                dev_sn,
            )
            device_netbox.serial = dev_sn
            device_netbox.save(update_fields=["serial"])

        logger.info("[SERIAL] Serial check passed for %r", self.hostname_ipam)

    def _update_custom_fields(self, device_netbox) -> None:
        """Update optional, custom fileds."""
        tz = ZoneInfo(TIME_ZONE)
        now = datetime.now(tz)
        port = self._used_kwargs.get("port", self._base_kwargs["port"])

        fields = {
            CF_SSH: port == 22,
            CF_SW_VERSION: self._device.version.upper() if self._device.version else "",
            CF_LAST_COLLECT_DATE: now.date(),
            CF_LAST_COLLECT_TIME: now.strftime("%H:%M:%S"),
        }
        logger.info("[CF] Updating custom fields for %r: %r", self.hostname_ipam, fields)
        for cf_name, cf_value in fields.items():
            if not cf_name:
                continue
            device_netbox.custom_field_data[cf_name] = cf_value

        device_netbox.save()
        logger.info("[CF] Custom fields saved for %r", self.hostname_ipam)

    def _save_running_config(self) -> None:
        """
        Collects running-config from device.
        Saves running-config to file in configs path.
        """
        os.makedirs(CONFIGS_PATH, exist_ok=True)
        filename = os.path.join(CONFIGS_PATH, f"{self.hostname_ipam}_running.txt")

        logger.info(
            "[CONFIG] Fetching running-config from %r -> %s",
            self.hostname_ipam,
            filename,
        )
        driver = PLATFORMS[self.platform]
        with driver(**self._used_kwargs) as conn:
            conn.send_command("terminal length 0")
            raw = conn.send_command("show running-config").result
            logger.info(
                "[CONFIG] Received %d chars of running-config from %r",
                len(raw),
                self.hostname_ipam,
            )
            clean = sanitize_config(raw)
            logger.info(
                "[CONFIG] After sanitization: %d chars (%d lines stripped)",
                len(clean),
                len(raw.splitlines()) - len(clean.splitlines()),
            )

        with open(filename, "w") as fh:
            fh.write(clean)

        logger.info("[CONFIG] Running config saved -> %s", filename)

    # Main entry point
    def collect_information(self) -> None:
        logger.info(
            "[COLLECT] ===== START %r host=%s platform=%s =====",
            self.hostname_ipam,
            self._base_kwargs["host"],
            self.platform,
        )

        self._check_reachability()
        self._connect_and_collect()

        device_netbox = self.task.device
        self._check_serial_match(device_netbox)
        self._update_custom_fields(device_netbox)

        if COLLECT_INTERFACES_DATA and self._interfaces:
            logger.info(
                "[COLLECT] Syncing %d interfaces to NetBox for %r",
                len(self._interfaces),
                self.hostname_ipam,
            )
            sync_interfaces_to_netbox(device_netbox, self._interfaces)

        self._save_running_config()

        logger.info("[COLLECT] ===== COMPLETE %r =====", self.hostname_ipam)
