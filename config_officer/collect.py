"""
Device configuration collector for config_officer plugin.
"""

from .custom_exceptions import CollectionException
from .choices import CollectFailChoices
import pytz
from datetime import datetime
import os
import socket
import time
import re
import importlib
import logging

from scrapli.driver.core import IOSXEDriver, NXOSDriver, IOSXRDriver
from django.conf import settings
from django.db import transaction, IntegrityError
from netaddr import EUI, AddrFormatError

from dcim.choices import InterfaceTypeChoices
from dcim.models import Interface, DeviceType, MACAddress
from ipam.choices import IPAddressRoleChoices, IPAddressStatusChoices
from ipam.models import IPAddress, VRF
from dcim.fields import mac_unix_expanded_uppercase

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

logger = logging.getLogger(__name__)


# Plugin settings

PLUGIN_SETTINGS       = settings.PLUGINS_CONFIG.get("config_officer", {})
DEVICE_USERNAME       = PLUGIN_SETTINGS.get("DEVICE_USERNAME", "cisco")
DEVICE_PASSWORD       = PLUGIN_SETTINGS.get("DEVICE_PASSWORD", "cisco")
DEVICE_SSH_PORT       = PLUGIN_SETTINGS.get("DEVICE_SSH_PORT", 22)
DEVICE_SPECIFIC_CONF  = PLUGIN_SETTINGS.get("DEVICE_SPECIFIC_CONF", {})
CF_NAME_SW_VERSION    = PLUGIN_SETTINGS.get("CF_NAME_SW_VERSION", "version")
CF_NAME_SSH           = PLUGIN_SETTINGS.get("CF_NAME_SSH", "ssh")
CF_NAME_LAST_COLLECT_DATE = PLUGIN_SETTINGS.get("CF_NAME_LAST_COLLECT_DATE", "last_collect_date")
CF_NAME_LAST_COLLECT_TIME = PLUGIN_SETTINGS.get("CF_NAME_LAST_COLLECT_TIME", "last_collect_time")
CF_NAME_COLLECTION_STATUS = PLUGIN_SETTINGS.get("CF_NAME_COLLECTION_STATUS", "collection_status")
CF_NAME_SW_VERSION = PLUGIN_SETTINGS.get('CF_NAME_SW_VERSION', 'version')
CF_NAME_SSH = PLUGIN_SETTINGS.get('CF_NAME_SSH', 'ssh')
NETBOX_DEVICES_CONFIGS_REPO_DIR = PLUGIN_SETTINGS.get("NETBOX_DEVICES_CONFIGS_REPO_DIR", "/device_configs")
NETBOX_DEVICES_CONFIGS_SUBPATH = PLUGIN_SETTINGS.get('NETBOX_DEVICES_CONFIGS_SUBPATH', 'netbox')
NETBOX_DEVICES_CONFIGS_PATH = os.path.join(NETBOX_DEVICES_CONFIGS_REPO_DIR, NETBOX_DEVICES_CONFIGS_SUBPATH)

TIME_ZONE             = os.environ.get("TIME_ZONE", "UTC")
NETBOX_DUAL_SIM_PLATFORM = PLUGIN_SETTINGS.get("NETBOX_DUAL_SIM_PLATFORM", "None")
COLLECT_INTERFACES_DATA  = PLUGIN_SETTINGS.get("COLLECT_INTERFACES_DATA", True)

SENSITIVE_PREFIXES_DEFAULT = (
    "username",
    "ssh",
    "snmp-server user",
    "crypto",
    "key",
    "password"
)

SENSITIVE_PREFIXES = PLUGIN_SETTINGS.get("SENSITIVE_PREFIXES", SENSITIVE_PREFIXES_DEFAULT)

REGEX_IP  = r'\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}'
REGEX_IPP = r'\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}/\d{1,2}'  # IP with prefix

PLATFORMS = {
    "iosxe": IOSXEDriver,
    "nxos":  NXOSDriver,
    "iosxr": IOSXRDriver,
}

# Data classes

class ParsedInterface:
    """Holds everything we know about a single interface after parsing."""

    def __init__(self, name: str):
        self.name        = name
        self.ip          = None    # primary IP with prefix, e.g. "10.0.0.1/24"
        self.secondary   = []      # list of secondary IPs with prefix
        self.mac         = None    # MAC address string
        self.description = None
        self.mtu         = None
        self.vrf         = None
        self.dhcp        = False
        self.speed       = None    # e.g. "1000Mbps"
        self.duplex      = None    # e.g. "full"
        self.admin_up    = None    # True/False
        self.link_up     = None    # True/False
        self.is_mgmt     = False   # True if this is the management interface
        self.lag         = None

    def __str__(self):
        return self.name


# IOS-XE / IOS parser

class IOSXEParser:
    """
    Parses plain 'show' output from IOS-XE (and most IOS) devices.
    All commands are sent without pipe filtering so the raw output is
    fully available for debugging.
    """

    @staticmethod
    def parse_show_version(output: str) -> dict:
        logger.debug("[PARSER][IOS] parse_show_version: %d chars", len(output))
        result = {
            "hostname": "", "version": "", "hardware": [""], "serial": [""],
        }
        for line in output.splitlines():
            m = re.search(r'^(\S+)\s+uptime is', line)
            if m and not result["hostname"]:
                result["hostname"] = m.group(1)
                logger.debug("[PARSER][IOS] hostname=%r", result["hostname"])

            m = re.search(r'(?:Cisco IOS.*?|)Version\s+([\w\.\(\):]+)', line, re.IGNORECASE)
            if m and not result["version"]:
                result["version"] = m.group(1)
                logger.debug("[PARSER][IOS] version=%r", result["version"])

            m = re.search(r'[Cc]isco\s+([\w\-]+).*?(?:[Pp]rocessor|bytes of memory)', line)
            if m and not result["hardware"][0]:
                result["hardware"] = [m.group(1)]
                logger.debug("[PARSER][IOS] hardware=%r", result["hardware"])

            m = re.search(r'[Pp]rocessor [Bb]oard ID\s+(\S+)', line)
            if m and not result["serial"][0]:
                result["serial"] = [m.group(1)]
                logger.debug("[PARSER][IOS] serial=%r", result["serial"])

        return result

    @staticmethod
    def parse_show_interfaces(output: str, mgmt_ip: str) -> dict[str, ParsedInterface]:
        """
        Parse 'show interfaces' full output.
        Returns dict of {if_name: ParsedInterface}.
        """
        logger.debug("[PARSER][IOS] parse_show_interfaces: %d chars", len(output))
        ifaces: dict[str, ParsedInterface] = {}
        current: ParsedInterface | None = None

        for line in output.splitlines():
            # New interface block starts with non-whitespace
            m = re.match(r'^(\S+.*?)\s+is\s+(administratively\s+)?(up|down),\s+line protocol is\s+(up|down)', line, re.IGNORECASE)
            if m:
                name = m.group(1).strip()
                current = ParsedInterface(name)
                current.admin_up = "administratively" not in line.lower() and m.group(3) == "up"
                current.link_up  = m.group(4) == "up"
                ifaces[name] = current
                logger.debug("[PARSER][IOS] Interface: %r admin_up=%s link_up=%s",
                             name, current.admin_up, current.link_up)
                continue

            if current is None:
                continue

            # Description
            m = re.match(r'^\s+Description:\s+(.*)', line)
            if m:
                current.description = m.group(1).strip()
                continue

            # Hardware / MAC
            m = re.match(r'^\s+Hardware is.*?(?:address is|BIA)\s+([\da-f]{4}\.[\da-f]{4}\.[\da-f]{4})', line, re.IGNORECASE)
            if m and not current.mac:
                current.mac = m.group(1)
                continue

            # Speed / duplex (IOS format)
            m = re.match(r'^\s+.*?(\d+(?:Mb|Gb|Kb)?ps),?\s+([\w-]+)-duplex', line, re.IGNORECASE)
            if m:
                current.speed  = m.group(1)
                current.duplex = m.group(2).lower()
                continue

            # MTU
            m = re.search(r'MTU\s+(\d+)\s+bytes.*?BW\s+(\d+)\s+Kbit', line)
            if m:
                current.mtu = int(m.group(1))
                continue

            # Primary IP
            m = re.match(fr'^\s+Internet address is\s+({REGEX_IPP})', line)
            if m:
                current.ip = m.group(1)
                if m.group(1).split('/')[0] == mgmt_ip:
                    current.is_mgmt = True
                continue

            # Secondary IP
            m = re.match(fr'^\s+Secondary address\s+({REGEX_IPP})', line)
            if m:
                current.secondary.append(m.group(1))
                continue

            # DHCP
            if re.search(r'address determined by (DHCP|IPCP)', line, re.IGNORECASE):
                current.dhcp = True
                continue

            # VRF
            m = re.match(r'^\s+VPN Routing.*?"(\S+)"', line)
            if m:
                current.vrf = m.group(1)
                continue

        logger.debug("[PARSER][IOS] Parsed %d interfaces", len(ifaces))
        logger.debug("[PARSER][NXOS] FINAL %s -> %s", name, vars(current))
        return ifaces

    @staticmethod
    def parse_show_ip_interface(output: str, ifaces: dict[str, ParsedInterface]):
        """
        Supplement interface data from 'show ip interface' output.
        Adds VRF and DHCP info that may not be in 'show interfaces'.
        """
        logger.debug("[PARSER][IOS] parse_show_ip_interface: %d chars", len(output))
        current_name = None
        for line in output.splitlines():
            m = re.match(r'^(\S+.*?)\s+is\s+(?:up|down|administratively)', line)
            if m:
                name = m.group(1).strip()
                current_name = name if name in ifaces else None
                continue
            if current_name is None:
                continue

            m = re.match(fr'^\s+.*?Internet address is\s+({REGEX_IPP})', line)
            if m and not ifaces[current_name].ip:
                ifaces[current_name].ip = m.group(1)
                continue

            m = re.match(r'^\s+VPN Routing.*?"(\S+)"', line)
            if m:
                ifaces[current_name].vrf = m.group(1)
                continue

            if re.search(r'address determined by (DHCP|IPCP)', line, re.IGNORECASE):
                ifaces[current_name].dhcp = True
                continue


# NX-OS parser (Nexus 93xx)

class NXOSParser:
    """
    Parses 'show' output from NX-OS (Nexus) devices.
    NX-OS output format differs significantly from IOS-XE.
    """

    @staticmethod
    def parse_show_version(output: str) -> dict:
        logger.debug("[PARSER][NXOS] parse_show_version: %d chars", len(output))
        result = {"hostname": "", "version": "", "hardware": [""], "serial": [""]}

        for line in output.splitlines():
            m = re.search(r'^\s*Device name:\s+(\S+)', line, re.IGNORECASE)
            if m and not result["hostname"]:
                result["hostname"] = m.group(1)
                logger.debug("[PARSER][NXOS] hostname=%r", result["hostname"])

            m = re.search(r'NXOS:\s+version\s+(\S+)', line, re.IGNORECASE)
            if not m:
                m = re.search(r'system:\s+version\s+(\S+)', line, re.IGNORECASE)
            if m and not result["version"]:
                result["version"] = m.group(1)
                logger.debug("[PARSER][NXOS] version=%r", result["version"])

            m = re.search(r'cisco\s+(Nexus[\s\w]+?)\s+(?:Chassis|processor)', line, re.IGNORECASE)
            if m and not result["hardware"][0]:
                result["hardware"] = [m.group(1).strip()]
                logger.debug("[PARSER][NXOS] hardware=%r", result["hardware"])

            m = re.search(r'Processor Board ID\s+(\S+)', line, re.IGNORECASE)
            if m and not result["serial"][0]:
                result["serial"] = [m.group(1)]
                logger.debug("[PARSER][NXOS] serial=%r", result["serial"])

        return result

    @staticmethod
    def parse_show_interfaces(output: str, mgmt_ip: str) -> dict[str, ParsedInterface]:
        """
        Parse 'show interface' (NX-OS) full output.
        NX-OS uses slightly different field names and ordering than IOS-XE.
        """
        logger.debug("[PARSER][NXOS] parse_show_interfaces: %d chars", len(output))

        ifaces: dict[str, ParsedInterface] = {}
        current: ParsedInterface | None = None

        for line in output.splitlines():
            # logger.debug("[PARSER][NXOS] LINE: %r", line)

            # NX-OS header: "Ethernet1/3 is up"
            m = re.match(r'^(\S+)\s+is\s+(up|down)', line, re.IGNORECASE)
            if m:
                name = m.group(1)
                current = ParsedInterface(name)
                current.link_up = m.group(2).lower() == "up"
                ifaces[name] = current
                logger.debug("[PARSER][NXOS] NEW IFACE: %s link_up=%s", name, current.link_up)
                continue

            if current is None:
                continue

            # admin state
            m = re.match(r'^\s*admin state is (up|down)', line, re.IGNORECASE)
            if m:
                current.admin_up = m.group(1).lower() == "up"
                logger.debug("[PARSER][NXOS] admin_up=%s", current.admin_up)
                continue

            # Description
            m = re.match(r'^\s+Description:\s+(.*)', line)
            if m:
                current.description = m.group(1).strip()
                logger.debug("[PARSER][NXOS] description=%r", current.description)
                continue

            # Port-channel membership
            m = re.match(r'^\s+Belongs to (Po\d+)', line)
            if m:
                current.lag = normalize_lag(m.group(1))
                logger.debug("[PARSER][NXOS] LAG=%s", current.lag)
                continue

            # MAC
            m = re.search(r'address:\s+([\da-f]{4}\.[\da-f]{4}\.[\da-f]{4})', line, re.IGNORECASE)
            if m:
                current.mac = m.group(1)
                logger.debug("[PARSER][NXOS] mac=%s", current.mac)
                continue

            # MTU + BW (NX-OS format)
            m = re.match(r'^\s+MTU\s+(\d+)\s+bytes,\s+BW\s+(\d+)\s+Kbit', line)
            if m:
                current.mtu = int(m.group(1))
                bw_kbit = int(m.group(2))
                current.speed = f"{bw_kbit // 1000}Mbps"
                logger.debug("[PARSER][NXOS] mtu=%s speed=%s", current.mtu, current.speed)
                continue

            # duplex/speed
            m = re.match(r'^\s+(full|half)-duplex,\s+(\d+)\s+Mb/s', line, re.IGNORECASE)
            if m:
                current.duplex = m.group(1).lower()
                current.speed = f"{m.group(2)}Mbps"
                logger.debug("[PARSER][NXOS] duplex=%s speed=%s", current.duplex, current.speed)
                continue

            # IP
            m = re.match(fr'^\s+Internet Address is\s+({REGEX_IPP})', line, re.IGNORECASE)
            if m:
                current.ip = m.group(1)
                if m.group(1).split('/')[0] == mgmt_ip:
                    current.is_mgmt = True
                logger.debug("[PARSER][NXOS] ip=%s mgmt=%s", current.ip, current.is_mgmt)
                continue

        logger.debug("[PARSER][NXOS] Parsed %d interfaces", len(ifaces))
        return ifaces


# Helper: infer NetBox interface type from name

def _interface_type(name: str) -> str:
    n = name.lower()
    if re.match(r'eth|ethernet', n):
        if '100' in name or '10g' in name.lower():
            return InterfaceTypeChoices.TYPE_10GE_SFP_PLUS
        if '40' in name or '40g' in name.lower():
            return InterfaceTypeChoices.TYPE_40GE_QSFP_PLUS
        if '100g' in name.lower():
            return InterfaceTypeChoices.TYPE_100GE_CFP
        return InterfaceTypeChoices.TYPE_1GE_FIXED
    if re.match(r'gigabit|gi\d', n):
        return InterfaceTypeChoices.TYPE_1GE_FIXED
    if re.match(r'tengig|te\d|ten', n):
        return InterfaceTypeChoices.TYPE_10GE_SFP_PLUS
    if re.match(r'fortygig|fo\d', n):
        return InterfaceTypeChoices.TYPE_40GE_QSFP_PLUS
    if re.match(r'hundredgig|hu\d', n):
        return InterfaceTypeChoices.TYPE_100GE_CFP
    if re.match(r'fastethernet|fa\d', n):
        return InterfaceTypeChoices.TYPE_100ME_FIXED
    if re.match(r'vlan|svi|bvi', n):
        return InterfaceTypeChoices.TYPE_VIRTUAL
    if re.match(r'loopback|lo\d', n):
        return InterfaceTypeChoices.TYPE_VIRTUAL
    if re.match(r'tunnel|tun', n):
        return InterfaceTypeChoices.TYPE_VIRTUAL
    if re.match(r'port.channel|po\d', n):
        return InterfaceTypeChoices.TYPE_LAG
    if re.match(r'mgmt', n):
        return InterfaceTypeChoices.TYPE_1GE_FIXED
    return InterfaceTypeChoices.TYPE_OTHER

def normalize_lag(name: str) -> str:
    # Po103 -> port-channel103
    n = name.lower().replace("po", "").strip()
    return f"port-channel{n}"

def ensure_lags(device_netbox, parsed):
    lag_names = {
        p.lag
        for p in parsed.values()
        if p.lag
    }

    existing = {
        i.name: i
        for i in Interface.objects.filter(device=device_netbox, type=InterfaceTypeChoices.TYPE_LAG)
    }

    for lag_name in lag_names:
        if lag_name not in existing:
            logger.debug("[LAG] Creating %s", lag_name)
            Interface.objects.create(
                device=device_netbox,
                name=lag_name,
                type=InterfaceTypeChoices.TYPE_LAG,
                enabled=True,
            )

def attach_lags(device_netbox, parsed):
    interfaces = {
        i.name.lower(): i
        for i in Interface.objects.filter(device=device_netbox)
    }

    for name, pif in parsed.items():
        if not pif.lag:
            continue

        iface = interfaces.get(name.lower())
        lag_name = pif.lag

        lag_iface = interfaces.get(lag_name)

        if not iface:
            continue

        if not lag_iface:
            logger.warning("[LAG] Missing %s for %s", lag_name, name)
            continue

        if iface.lag != lag_iface:
            iface.lag = lag_iface
            iface.save()
            logger.debug("[LAG] %s -> %s", name, lag_name)

# NetBox interface sync

def sync_interfaces_to_netbox(device_netbox, parsed: dict[str, ParsedInterface]):
    """
    Create or update Interface objects in NetBox based on parsed device data.
    Does NOT delete interfaces (conservative – manual cleanup preferred).
    """
    logger.info("[NETBOX] Syncing %d interfaces for %s", len(parsed), device_netbox.name)

    ensure_lags(device_netbox, parsed)

    existing = {str(i): i for i in Interface.objects.filter(device=device_netbox)}
    logger.debug("[NETBOX] Existing interfaces in NetBox: %s", list(existing.keys()))

    for name, pif in parsed.items():
        logger.debug(
            "[SYNC INPUT] %s mac=%s mtu=%s speed=%s desc=%s",
            name, pif.mac, pif.mtu, pif.speed, pif.description
        )
        if_type = _interface_type(name)
        description = pif.description or ""

        if name.lower().startswith("vlan"):
            continue

        if name in existing:
            iface = existing[name]
            changed = False

            if iface.type != if_type:
                logger.debug("[NETBOX] %s: updating type %r -> %r", name, iface.type, if_type)
                iface.type = if_type
                changed = True

            if description and iface.description != description:
                logger.debug("[NETBOX] %s: updating description -> %r", name, description)
                iface.description = description
                changed = True

            if pif.mac:
                with transaction.atomic():
                    mac_obj, _ = MACAddress.objects.get_or_create(
                        mac_address=EUI(pif.mac, version=48, dialect=mac_unix_expanded_uppercase)
                    )

                    if Interface.objects.filter(primary_mac_address=mac_obj).exclude(pk=iface.pk).exists():
                        logger.warning("[MAC] already used as primary elsewhere -> skipping %s", iface.name)
                        return

                    iface.primary_mac_address = mac_obj
                    iface.save()

            if pif.mtu and iface.mtu != pif.mtu:
                logger.debug("[NETBOX] %s: updating MTU -> %s", name, pif.mtu)
                iface.mtu = pif.mtu
                changed = True

            if pif.speed and iface.speed != pif.speed:
                try:
                    speed_val = int(pif.speed.replace("Mbps", "")) * 1000
                    if iface.speed != speed_val:
                        logger.debug("[NETBOX] %s: updating speed -> %s", name, speed_val)
                        iface.speed = speed_val
                        changed = True
                except ValueError:
                    logger.warning("[NETBOX] %s: invalid speed %r", name, pif.speed)

            if pif.duplex and iface.duplex != pif.duplex:
                logger.debug("[NETBOX] %s: updating duplex -> %s", name, pif.duplex)
                iface.duplex = pif.duplex
                changed = True

            if pif.admin_up is not None and iface.enabled != pif.admin_up:
                logger.debug("[NETBOX] %s: updating enabled -> %s", name, pif.admin_up)
                iface.enabled = pif.admin_up
                changed = True

            if changed:
                iface.save()
                logger.debug("[NETBOX] %s: saved", name)
        else:
            logger.debug("[NETBOX] Creating interface %r type=%r", name, if_type)
            iface = Interface.objects.create(
                device=device_netbox,
                name=name,
                type=if_type,
                description=description,
                enabled=pif.admin_up if pif.admin_up is not None else True,
                mtu=pif.mtu,
            )
            if pif.speed:
                try:
                    iface.speed = int(pif.speed.replace("Mbps", ""))
                except:
                    pass

            if pif.duplex:
                iface.duplex = pif.duplex

            iface.save()

        # IP addresses 
        _sync_ip(device_netbox, iface, pif)

        attach_lags(device_netbox, parsed)

        logger.debug(
            "[NETBOX FINAL] %s mtu=%s speed=%s duplex=%s enabled=%s mac=%s",
            iface.name,
            iface.mtu,
            iface.speed,
            iface.duplex,
            iface.enabled,
            iface.mac_address,
        )


def _sync_ip(device_netbox, iface: Interface, pif: ParsedInterface):
    """Create/update primary and secondary IPs for an interface."""
    all_ips = []
    if pif.ip:
        all_ips.append((pif.ip, False))  # (address, is_secondary)
    for sec in pif.secondary:
        all_ips.append((sec, True))

    for addr, is_secondary in all_ips:
        logger.debug("[NETBOX] %s: ensuring IP %s (secondary=%s)", iface.name, addr, is_secondary)
        try:
            ip_obj, created = IPAddress.objects.get_or_create(
                address=addr,
                defaults={"tenant": device_netbox.tenant},
            )
            if created:
                logger.debug("[NETBOX] Created IP %s", addr)
            else:
                logger.debug("[NETBOX] IP %s already exists", addr)

            # Assign to interface
            if ip_obj not in iface.ip_addresses.all():
                iface.ip_addresses.add(ip_obj)
                logger.debug("[NETBOX] Assigned %s -> %s", addr, iface.name)

            if is_secondary:
                ip_obj.role = IPAddressRoleChoices.ROLE_SECONDARY
            elif re.match(r'loopback|lo\d', iface.name.lower()):
                ip_obj.role = IPAddressRoleChoices.ROLE_LOOPBACK

            if pif.dhcp:
                ip_obj.status = IPAddressStatusChoices.STATUS_DHCP

            if pif.vrf:
                try:
                    ip_obj.vrf = VRF.objects.get(name__iexact=pif.vrf)
                except VRF.DoesNotExist:
                    logger.debug("[NETBOX] Creating VRF %r", pif.vrf)
                    new_vrf = VRF.objects.create(name=pif.vrf, enforce_unique=False)
                    ip_obj.vrf = new_vrf

            ip_obj.save()

        except Exception:
            logger.exception("[NETBOX] Error syncing IP %s on %s", addr, iface.name)


# Main collector

class CollectDeviceData:
    """Connect to a device and collect its running config + interface data."""

    def __init__(self, collect_task, ip: str = "", hostname_ipam: str = "", platform: str = ""):
        self.task         = collect_task
        self.hostname_ipam = hostname_ipam.strip()
        self.platform     = platform if platform in PLATFORMS else "iosxe"

        # Per-device credentials 
        device_conf    = DEVICE_SPECIFIC_CONF.get(self.hostname_ipam, {})
        device_username = device_conf.get("DEVICE_USERNAME", DEVICE_USERNAME)
        device_password = device_conf.get("DEVICE_PASSWORD", DEVICE_PASSWORD)
        device_port     = int(device_conf.get("DEVICE_SSH_PORT", DEVICE_SSH_PORT))

        if device_conf:
            logger.info("[COLLECT] Per-device config for %r: user=%r port=%d",
                        self.hostname_ipam, device_username, device_port)
        else:
            logger.debug("[COLLECT] Global config for %r: user=%r port=%d",
                         self.hostname_ipam, device_username, device_port)

        ssh_config_path = (
            os.path.dirname(importlib.util.find_spec("config_officer").origin)
            + "/ssh_config"
        )
        logger.debug("[COLLECT] ssh_config=%r", ssh_config_path)

        self.device_kwargs = {
            "host":            ip,
            "auth_username":   device_username,
            "auth_password":   device_password,
            "auth_strict_key": False,
            "port":            device_port,
            "timeout_socket":  20,
            "timeout_ops":     60,
            "ssh_config_file": ssh_config_path,
        }

        # Parsed results (filled during collection)
        self.hostname = ""
        self.pid      = ""
        self.sn       = ""
        self.sw       = ""
        self.interfaces: dict[str, ParsedInterface] = {}

        logger.info("[COLLECT] Initialized: %r host=%s platform=%s port=%d",
                    self.hostname_ipam, ip, self.platform, device_port)

    # Reachability 

    def check_reachability(self):
        host    = self.device_kwargs["host"]
        timeout = self.device_kwargs["timeout_socket"]
        logger.debug("[REACHABILITY] host=%s timeout=%ds", host, timeout)

        for port in (22, 23):
            try:
                with socket.create_connection((host, port), timeout=timeout):
                    logger.debug("[REACHABILITY] Port %d open on %s", port, host)
                    return
            except Exception as e:
                logger.debug("[REACHABILITY] Port %d closed on %s: %s", port, host, e)

        logger.warning("[REACHABILITY] %s unreachable on ports 22 and 23", host)
        raise CollectionException(
            reason=CollectFailChoices.FAIL_CONNECT,
            message="Device unreachable",
        )

    # Command helpers 

    def _send(self, conn, cmd: str) -> str:
        logger.debug("[CMD] -> %r", cmd)
        result = conn.send_command(cmd).result
        logger.debug("[CMD] ← %d chars", len(result))
        return result

    # Sanitize config helper

    def _sanitize_config(self, config_text: str) -> str:
        pattern = re.compile(
            r"^\s*(?:{})\b".format("|".join(re.escape(p) for p in SENSITIVE_PREFIXES)),
            re.IGNORECASE,
        )

        sanitized_lines = []
        
        for line in config_text.splitlines():
            if not pattern.match(line):
                sanitized_lines.append(line)
        
        return "\n".join(sanitized_lines)

    # Device info collection 

    def _collect_iosxe(self, conn):
        logger.debug("[COLLECT][IOSXE] Collecting show version")
        ver_output = self._send(conn, "show version")
        parsed_ver = IOSXEParser.parse_show_version(ver_output)
        self.hostname = parsed_ver.get("hostname", "")
        self.pid      = (parsed_ver.get("hardware") or [""])[0]
        self.sn       = (parsed_ver.get("serial")   or [""])[0]
        self.sw       = parsed_ver.get("version", "")
        logger.info("[COLLECT][IOSXE] hostname=%r pid=%r sn=%r sw=%r",
                    self.hostname, self.pid, self.sn, self.sw)

        if COLLECT_INTERFACES_DATA:
            logger.debug("[COLLECT][IOSXE] Collecting show interfaces")
            if_output = self._send(conn, "show interfaces")
            host_ip = self.device_kwargs["host"]
            self.interfaces = IOSXEParser.parse_show_interfaces(if_output, host_ip)

            logger.debug("[COLLECT][IOSXE] Collecting show ip interface")
            ip_output = self._send(conn, "show ip interface")
            IOSXEParser.parse_show_ip_interface(ip_output, self.interfaces)
            logger.info("[COLLECT][IOSXE] Parsed %d interfaces", len(self.interfaces))

    def _collect_nxos(self, conn):
        logger.debug("[COLLECT][NXOS] Collecting show version")
        ver_output = self._send(conn, "show version")
        parsed_ver = NXOSParser.parse_show_version(ver_output)
        self.hostname = parsed_ver.get("hostname", "")
        self.pid      = (parsed_ver.get("hardware") or [""])[0]
        self.sn       = (parsed_ver.get("serial")   or [""])[0]
        self.sw       = parsed_ver.get("version", "")
        logger.info("[COLLECT][NXOS] hostname=%r pid=%r sn=%r sw=%r",
                    self.hostname, self.pid, self.sn, self.sw)

        if COLLECT_INTERFACES_DATA:
            logger.debug("[COLLECT][NXOS] Collecting show interface")
            if_output = self._send(conn, "show interface")
            host_ip = self.device_kwargs["host"]
            self.interfaces = NXOSParser.parse_show_interfaces(if_output, host_ip)
            logger.info("[COLLECT][NXOS] Parsed %d interfaces", len(self.interfaces))

    def _collect_iosxr(self, conn):
        logger.debug("[COLLECT][IOSXR] Collecting show version")
        ver_output = self._send(conn, "show version")
        # IOS-XR version format is similar enough to IOS-XE for our purposes
        parsed_ver = IOSXEParser.parse_show_version(ver_output)
        self.hostname = parsed_ver.get("hostname", "")
        self.pid      = (parsed_ver.get("hardware") or [""])[0]
        self.sn       = (parsed_ver.get("serial")   or [""])[0]
        self.sw       = parsed_ver.get("version", "")
        logger.info("[COLLECT][IOSXR] hostname=%r pid=%r sn=%r sw=%r",
                    self.hostname, self.pid, self.sn, self.sw)

        if COLLECT_INTERFACES_DATA:
            logger.debug("[COLLECT][IOSXR] Collecting show interfaces")
            if_output = self._send(conn, "show interfaces")
            host_ip = self.device_kwargs["host"]
            self.interfaces = IOSXEParser.parse_show_interfaces(if_output, host_ip)
            logger.info("[COLLECT][IOSXR] Parsed %d interfaces", len(self.interfaces))

    # NetBox update 

    def _update_custom_fields(self, device_netbox, port: int):
        fields = {
            CF_NAME_COLLECTION_STATUS: "temporary value",
            CF_NAME_SSH:               port == 22,
            CF_NAME_SW_VERSION:        self.sw.upper() if self.sw else "",
            CF_NAME_LAST_COLLECT_DATE: datetime.now(pytz.timezone(TIME_ZONE)).date(),
            CF_NAME_LAST_COLLECT_TIME: datetime.now(pytz.timezone(TIME_ZONE)).strftime("%H:%M:%S"),
        }
        for cf_name, cf_value in fields.items():
            if not cf_name:
                continue
            logger.debug("[CF] %r = %r", cf_name, cf_value)
            device_netbox.custom_field_data[cf_name] = cf_value
        device_netbox.save()

    # Main entry point 

    def collect_information(self):
        host = self.device_kwargs["host"]
        logger.info("[COLLECT] ===== %r host=%s platform=%s =====",
                    self.hostname_ipam, host, self.platform)

        self.check_reachability()

        collectors = {
            "iosxe": self._collect_iosxe,
            "nxos":  self._collect_nxos,
            "iosxr": self._collect_iosxr,
        }
        collect_fn = collectors[self.platform]

        # SSH attempt 
        logger.debug("[COLLECT] Trying SSH on port %d", self.device_kwargs["port"])
        connected = False
        try:
            with PLATFORMS[self.platform](**self.device_kwargs) as conn:
                logger.info("[COLLECT] SSH connected to %s", host)
                collect_fn(conn)
                connected = True
        except Exception as e:
            logger.warning("[COLLECT] SSH failed: %s: %s", type(e).__name__, e)

        # Telnet fallback 
        if not connected:
            logger.debug("[COLLECT] Trying telnet fallback (port 23)")
            telnet_kwargs = {**self.device_kwargs, "port": 23, "transport": "telnet"}
            try:
                with PLATFORMS[self.platform](**telnet_kwargs) as conn:
                    logger.info("[COLLECT] Telnet connected to %s", host)
                    collect_fn(conn)
                    connected = True
                    self.device_kwargs = telnet_kwargs  # remember for config save
            except Exception as e:
                logger.error("[COLLECT] Telnet also failed: %s: %s", type(e).__name__, e)
                raise CollectionException(
                    reason=CollectFailChoices.FAIL_LOGIN,
                    message="Can not login",
                )

        # Serial check 
        device_netbox = self.task.device
        if device_netbox.serial and self.sn and device_netbox.serial != self.sn:
            logger.warning("[SYNC] Serial mismatch NetBox=%r Device=%r",
                           device_netbox.serial, self.sn)
            raise CollectionException(
                reason=CollectFailChoices.FAIL_UPDATE,
                message=f"SN mismatch: NetBox={device_netbox.serial} Device={self.sn}",
            )

        # Update NetBox 
        self._update_custom_fields(device_netbox, self.device_kwargs["port"])

        if COLLECT_INTERFACES_DATA and self.interfaces:
            sync_interfaces_to_netbox(device_netbox, self.interfaces)

        # Save running config 
        os.makedirs(NETBOX_DEVICES_CONFIGS_PATH, exist_ok=True)
        filename = os.path.join(NETBOX_DEVICES_CONFIGS_PATH,f"{self.hostname_ipam}_running.txt")
        logger.debug("[COLLECT] Saving running config -> %r", filename)
        with PLATFORMS[self.platform](**self.device_kwargs) as conn:
            conn.send_command("terminal length 0")
            output = conn.send_command("show running-config")
            clean_output = self._sanitize_config(output.result)
            with open(filename, "w") as f:
                f.write(clean_output)
            logger.info("[COLLECT] Running config saved -> %s", filename)

        logger.info("[COLLECT] ===== Complete: %r =====", self.hostname_ipam)