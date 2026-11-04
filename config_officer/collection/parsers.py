"""CLI output parsers: raw ``show`` text in, plain dataclasses out.

Pure functions without Django or network access, so they are easy to unit test
with captured device output (see tests/fixtures/). The parsed result is turned
into NetBox objects by netbox_sync.py.

Supported commands:

* IOS-XE (also used for IOS-XR): ``show version``, ``show interfaces``,
  ``show ip interface``
* NX-OS: ``show version``, ``show interface``
* both: ``show vlan brief``, ``show interface(s) switchport``
* all platforms: ``show inventory``, ``show cdp neighbors detail``, ``show lldp neighbors detail``
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

#: IPv4 address with prefix length, e.g. 10.0.0.1/24
REGEX_IPP = r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}/\d{1,2}"


@dataclass
class ParsedInterface:
    """Everything we know about a single interface after parsing CLI output."""

    name: str
    ip: str | None = None  # primary IP/prefix, e.g. "10.0.0.1/24"
    secondary: list[str] = field(default_factory=list)  # secondary IPs/prefix
    mac: str | None = None  # dotted-hex, e.g. "aabb.ccdd.eeff"
    description: str | None = None
    mtu: int | None = None
    vrf: str | None = None
    dhcp: bool = False
    speed: str | None = None  # e.g. "1000Mbps"
    duplex: str | None = None  # "full" | "half"
    admin_up: bool | None = None
    link_up: bool | None = None
    is_mgmt: bool = False
    lag: str | None = None  # normalised LAG name, e.g. "port-channel1"

    def __str__(self) -> str:
        return self.name


@dataclass
class ParsedVlan:
    vid: int
    name: str
    status: str = ""


@dataclass
class ParsedSwitchport:
    """802.1Q settings of one port from ``show interface(s) switchport``."""

    name: str  # full interface name (abbreviations expanded)
    enabled: bool = True  # False = routed port ("Switchport: Disabled")
    admin_mode: str = ""
    oper_mode: str = ""
    access_vlan: int | None = None
    native_vlan: int | None = None
    voice_vlan: int | None = None
    trunk_vlans_text: str = ""  # e.g. "ALL", "1-4094", "10,20,30-32"

    @property
    def mode(self) -> str | None:
        """'access', 'trunk' or None (routed, down or not recognised)."""
        if not self.enabled:
            return None
        # The configured mode wins; "dynamic ..." ports fall back to what was negotiated.
        for candidate in (self.admin_mode, self.oper_mode):
            if "trunk" in candidate:
                return "trunk"
            if "access" in candidate:
                return "access"
        return None

    @property
    def trunk_vlans(self) -> list[int] | None:
        """Allowed VLAN IDs on a trunk; None means all VLANs."""
        return parse_vlan_list(self.trunk_vlans_text)


@dataclass
class ParsedInventoryItem:
    """One entry of ``show inventory`` (chassis, line card, power supply, fan, transceiver...)."""

    name: str
    description: str = ""
    pid: str = ""
    vid: str = ""
    serial: str = ""


@dataclass
class ParsedNeighbor:
    """A directly connected device seen through CDP or LLDP."""

    local_interface: str  # full name of the port on this device
    name: str  # neighbor hostname as advertised (may include a domain)
    interface: str  # neighbor's port exactly as advertised ("Te1/0/10", "eth0", ...)
    protocol: str  # "cdp" | "lldp"
    platform: str = ""
    ip: str = ""


@dataclass
class ParsedDevice:
    """Everything parsed from one device session."""

    hostname: str = ""
    version: str = ""
    pid: str = ""  # product ID / hardware model
    serial: str = ""
    interfaces: dict[str, ParsedInterface] = field(default_factory=dict)
    vlans: dict[int, ParsedVlan] = field(default_factory=dict)
    switchports: dict[str, ParsedSwitchport] = field(default_factory=dict)
    inventory: list[ParsedInventoryItem] = field(default_factory=list)
    neighbors: list[ParsedNeighbor] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def normalize_lag_name(raw: str) -> str:
    """Convert NX-OS shorthand (e.g. 'Po103') to NetBox canonical form."""
    number = raw.lower().replace("po", "").strip()
    return f"port-channel{number}"


# ---------------------------------------------------------------------------
# IOS-XE / IOS-XR parser
# ---------------------------------------------------------------------------


class IOSXEParser:
    """
    Parses plain 'show' output from IOS-XE (and most IOS / IOS-XR) devices.
    Raw output is parsed without pipe-filtering so nothing is silently dropped.
    """

    @staticmethod
    def parse_show_version(output: str) -> ParsedDevice:
        logger.debug("[PARSER][IOSXE] parse_show_version: %d chars", len(output))
        result = ParsedDevice()

        for line in output.splitlines():
            if not result.hostname:
                m = re.search(r"^(\S+)\s+uptime is", line)
                if m:
                    result.hostname = m.group(1)
                    logger.debug("[PARSER][IOSXE] hostname=%r", result.hostname)

            if not result.version:
                m = re.search(r"(?:Cisco IOS.*?|)Version\s+([\w.\(\):]+)", line, re.IGNORECASE)
                if m:
                    result.version = m.group(1)
                    logger.debug("[PARSER][IOSXE] version=%r", result.version)

            if not result.pid:
                m = re.search(r"[Cc]isco\s+([\w\-]+).*?(?:[Pp]rocessor|bytes of memory)", line)
                if m:
                    result.pid = m.group(1)
                    logger.debug("[PARSER][IOSXE] pid=%r", result.pid)

            if not result.serial:
                m = re.search(r"[Pp]rocessor [Bb]oard ID\s+(\S+)", line)
                if m:
                    result.serial = m.group(1)
                    logger.debug("[PARSER][IOSXE] serial=%r", result.serial)

        return result

    @staticmethod
    def parse_show_interfaces(output: str, mgmt_ip: str) -> dict[str, ParsedInterface]:  # noqa: PLR0915
        """
        Parse 'show interfaces' full output.
        Returns a mapping of interface-name -> ParsedInterface.
        """
        logger.debug("[PARSER][IOSXE] parse_show_interfaces: %d chars", len(output))
        ifaces: dict[str, ParsedInterface] = {}
        current: ParsedInterface | None = None

        for line in output.splitlines():
            # Every new interface block starts at column 0 with the name
            m = re.match(
                r"^(\S+.*?)\s+is\s+(administratively\s+)?(up|down),\s+line protocol is\s+(up|down)",
                line,
                re.IGNORECASE,
            )
            if m:
                name = m.group(1).strip()
                current = ParsedInterface(name=name)
                current.admin_up = ("administratively" not in line.lower()) and m.group(3) == "up"
                current.link_up = m.group(4) == "up"
                ifaces[name] = current
                logger.debug(
                    "[PARSER][IOSXE] Interface=%r admin_up=%s link_up=%s",
                    name,
                    current.admin_up,
                    current.link_up,
                )
                continue

            if current is None:
                continue

            # Description
            m = re.match(r"^\s+Description:\s+(.*)", line)
            if m:
                current.description = m.group(1).strip()
                continue

            # Hardware / MAC  (IOS prints "address is" or "BIA")
            m = re.match(
                r"^\s+Hardware is.*?(?:address is|BIA)\s+([\da-f]{4}\.[\da-f]{4}\.[\da-f]{4})",
                line,
                re.IGNORECASE,
            )
            if m and not current.mac:
                current.mac = m.group(1)
                continue

            # Duplex / speed, as printed by IOS / IOS-XE:
            #   "  Full-duplex, 1000Mb/s, media type is ..."  or  "  Full Duplex, 1000Mbps, link type ..."
            m = re.match(
                r"^\s+(Full|Half|Auto)[- ]duplex,\s+(\d+)\s*(Kb|Mb|Gb)(?:/s|ps)",
                line,
                re.IGNORECASE,
            )
            if m:
                current.duplex = m.group(1).lower()
                current.speed = f"{m.group(2)}{m.group(3).capitalize()}ps"
                continue
            # Older format: "  ... 1000Mbps, full-duplex"
            m = re.match(r"^\s+.*?(\d+(?:Mb|Gb|Kb)?ps),?\s+([\w-]+)-duplex", line, re.IGNORECASE)
            if m:
                current.speed = m.group(1)
                current.duplex = m.group(2).lower()
                continue

            # MTU
            m = re.search(r"MTU\s+(\d+)\s+bytes.*?BW\s+(\d+)\s+Kbit", line)
            if m:
                current.mtu = int(m.group(1))
                continue

            # Primary IP
            m = re.match(rf"^\s+Internet address is\s+({REGEX_IPP})", line)
            if m:
                current.ip = m.group(1)
                current.is_mgmt = m.group(1).split("/")[0] == mgmt_ip
                continue

            # Secondary IP
            m = re.match(rf"^\s+Secondary address\s+({REGEX_IPP})", line)
            if m:
                current.secondary.append(m.group(1))
                continue

            # DHCP
            if re.search(r"address determined by (DHCP|IPCP)", line, re.IGNORECASE):
                current.dhcp = True
                continue

            # VRF
            m = re.match(r'^\s+VPN Routing.*?"(\S+)"', line)
            if m:
                current.vrf = m.group(1)
                continue

        logger.debug("[PARSER][IOSXE] Parsed %d interfaces", len(ifaces))
        return ifaces

    @staticmethod
    def parse_show_ip_interface(output: str, ifaces: dict[str, ParsedInterface]) -> None:
        """
        Supplement interface data from 'show ip interface'.
        Adds VRF / DHCP info that may be absent from 'show interfaces'.
        Mutates *ifaces* in place.
        """
        logger.debug("[PARSER][IOSXE] parse_show_ip_interface: %d chars", len(output))
        current_name: str | None = None

        for line in output.splitlines():
            m = re.match(r"^(\S+.*?)\s+is\s+(?:up|down|administratively)", line)
            if m:
                name = m.group(1).strip()
                current_name = name if name in ifaces else None
                continue

            if current_name is None:
                continue

            iface = ifaces[current_name]

            m = re.match(rf"^\s+.*?Internet address is\s+({REGEX_IPP})", line)
            if m and not iface.ip:
                iface.ip = m.group(1)
                continue

            m = re.match(r'^\s+VPN Routing.*?"(\S+)"', line)
            if m:
                iface.vrf = m.group(1)
                continue

            if re.search(r"address determined by (DHCP|IPCP)", line, re.IGNORECASE):
                iface.dhcp = True
                continue


# ---------------------------------------------------------------------------
# NX-OS parser
# ---------------------------------------------------------------------------


class NXOSParser:
    """
    Parses 'show' output from NX-OS (Nexus) devices.
    NX-OS output format differs significantly from IOS-XE.
    """

    @staticmethod
    def parse_show_version(output: str) -> ParsedDevice:
        logger.debug("[PARSER][NXOS] parse_show_version: %d chars", len(output))
        result = ParsedDevice()

        for line in output.splitlines():
            if not result.hostname:
                m = re.search(r"^\s*Device name:\s+(\S+)", line, re.IGNORECASE)
                if m:
                    result.hostname = m.group(1)
                    logger.debug("[PARSER][NXOS] hostname=%r", result.hostname)

            if not result.version:
                m = re.search(r"(?:NXOS|system):\s+version\s+(\S+)", line, re.IGNORECASE)
                if m:
                    result.version = m.group(1)
                    logger.debug("[PARSER][NXOS] version=%r", result.version)

            if not result.pid:
                # e.g. "cisco Nexus9000 C93180YC-EX chassis" (model names contain hyphens)
                m = re.search(
                    r"cisco\s+(Nexus[\w\s-]+?)\s+(?:Chassis|processor)",
                    line,
                    re.IGNORECASE,
                )
                if m:
                    result.pid = m.group(1).strip()
                    logger.debug("[PARSER][NXOS] pid=%r", result.pid)

            if not result.serial:
                m = re.search(r"Processor Board ID\s+(\S+)", line, re.IGNORECASE)
                if m:
                    result.serial = m.group(1)
                    logger.debug("[PARSER][NXOS] serial=%r", result.serial)

        return result

    @staticmethod
    def parse_show_interfaces(output: str, mgmt_ip: str) -> dict[str, ParsedInterface]:
        """
        Parse 'show interface' (NX-OS) full output.
        Returns a mapping of interface-name -> ParsedInterface.
        """
        logger.debug("[PARSER][NXOS] parse_show_interfaces: %d chars", len(output))
        ifaces: dict[str, ParsedInterface] = {}
        current: ParsedInterface | None = None

        for line in output.splitlines():
            # NX-OS header: "Ethernet1/3 is up"  (no "line protocol" clause)
            m = re.match(r"^(\S+)\s+is\s+(up|down)", line, re.IGNORECASE)
            if m:
                name = m.group(1)
                current = ParsedInterface(name=name)
                current.link_up = m.group(2).lower() == "up"
                ifaces[name] = current
                logger.debug("[PARSER][NXOS] Interface=%s link_up=%s", name, current.link_up)
                continue

            if current is None:
                continue

            # Admin state
            m = re.match(r"^\s*admin state is (up|down)", line, re.IGNORECASE)
            if m:
                current.admin_up = m.group(1).lower() == "up"
                continue

            # Description
            m = re.match(r"^\s+Description:\s+(.*)", line)
            if m:
                current.description = m.group(1).strip()
                continue

            # Port-channel membership
            m = re.match(r"^\s+Belongs to (Po\d+)", line)
            if m:
                current.lag = normalize_lag_name(m.group(1))
                continue

            # MAC
            m = re.search(r"address:\s+([\da-f]{4}\.[\da-f]{4}\.[\da-f]{4})", line, re.IGNORECASE)
            if m:
                current.mac = m.group(1)
                continue

            # MTU + bandwidth
            m = re.match(r"^\s+MTU\s+(\d+)\s+bytes,\s+BW\s+(\d+)\s+Kbit", line)
            if m:
                current.mtu = int(m.group(1))
                current.speed = f"{int(m.group(2)) // 1000}Mbps"
                continue

            # Duplex / speed (separate NX-OS line): "full-duplex, 10 Gb/s" or "full-duplex, 1000 Mb/s"
            m = re.match(r"^\s+(full|half|auto)-duplex,\s+(\d+)\s+(Mb|Gb)/s", line, re.IGNORECASE)
            if m:
                current.duplex = m.group(1).lower()
                mbps = int(m.group(2)) * (1000 if m.group(3).lower() == "gb" else 1)
                current.speed = f"{mbps}Mbps"
                continue

            # IP
            m = re.match(rf"^\s+Internet Address is\s+({REGEX_IPP})", line, re.IGNORECASE)
            if m:
                current.ip = m.group(1)
                current.is_mgmt = m.group(1).split("/")[0] == mgmt_ip
                continue

        logger.debug("[PARSER][NXOS] Parsed %d interfaces", len(ifaces))
        return ifaces


# ---------------------------------------------------------------------------
# Output formats shared by the platforms
# ---------------------------------------------------------------------------

# Abbreviations used by "show vlan", "show interfaces switchport", CDP and LLDP.
_ABBREVIATIONS = {
    "fa": "FastEthernet",
    "gi": "GigabitEthernet",
    "tw": "TwoGigabitEthernet",
    "fi": "FiveGigabitEthernet",
    "te": "TenGigabitEthernet",
    "twe": "TwentyFiveGigE",
    "fo": "FortyGigabitEthernet",
    "hu": "HundredGigE",
    "ap": "AppGigabitEthernet",
    "eth": "Ethernet",
    "et": "Ethernet",
    "po": "Port-channel",
    "lo": "Loopback",
    "vl": "Vlan",
    "tu": "Tunnel",
}


def expand_interface_name(name: str) -> str:
    """'Gi1/0/1' -> 'GigabitEthernet1/0/1', 'Eth1/1' -> 'Ethernet1/1'; full names are kept."""
    name = name.strip()
    m = re.match(r"^([A-Za-z-]+?)(\d.*)$", name)
    if not m:
        return name
    full = _ABBREVIATIONS.get(m.group(1).lower())
    return f"{full}{m.group(2)}" if full else name


def parse_vlan_list(text: str) -> list[int] | None:
    """'10,20,30-32' -> [10, 20, 30, 31, 32]; 'ALL' / '1-4094' / '' -> None (all); 'NONE' -> []."""
    text = text.strip().upper().rstrip(",")
    if not text or text == "ALL":
        return None
    if text == "NONE":
        return []
    vids: set[int] = set()
    for part in text.replace(" ", "").split(","):
        if "-" in part:
            first, last = part.split("-", 1)
            vids.update(range(int(first), int(last) + 1))
        elif part:
            vids.add(int(part))
    return None if vids >= set(range(1, 4095)) else sorted(vids)


def parse_vlan_brief(output: str) -> dict[int, ParsedVlan]:
    """``show vlan brief`` (IOS-XE and NX-OS)."""
    vlans = {}
    for line in output.splitlines():
        m = re.match(r"^(\d{1,4})\s+(\S+)\s+(\S+)", line)
        if m and 1 <= int(m.group(1)) <= 4094:
            vid = int(m.group(1))
            vlans[vid] = ParsedVlan(vid=vid, name=m.group(2), status=m.group(3))
    return vlans


def _vlan_id(value: str) -> int | None:
    """'10 (USERS)' -> 10; 'none' -> None."""
    m = re.match(r"^(\d+)", value.strip())
    return int(m.group(1)) if m else None


def parse_switchport(output: str) -> dict[str, ParsedSwitchport]:
    """``show interfaces switchport`` (IOS-XE) / ``show interface switchport`` (NX-OS)."""
    ports: dict[str, ParsedSwitchport] = {}
    current: ParsedSwitchport | None = None
    in_trunk_list = False
    for raw in output.splitlines():
        line = raw.strip()
        if line.startswith("Name:"):
            current = ParsedSwitchport(name=expand_interface_name(line.split(":", 1)[1]))
            ports[current.name] = current
            in_trunk_list = False
            continue
        if current is None:
            continue
        if ":" not in line:
            # Long VLAN lists continue on the next, indented line.
            if in_trunk_list and re.fullmatch(r"[\d,\-]+", line):
                current.trunk_vlans_text += line
            continue
        key, value = (part.strip() for part in line.split(":", 1))
        key = key.lower()
        in_trunk_list = key in ("trunking vlans enabled", "trunking vlans allowed")
        if key == "switchport":
            current.enabled = value.lower().startswith("enabled")
        elif key == "administrative mode":
            current.admin_mode = value.lower()
        elif key == "operational mode":
            current.oper_mode = value.lower()
        elif key == "access mode vlan":
            current.access_vlan = _vlan_id(value)
        elif key == "trunking native mode vlan":
            current.native_vlan = _vlan_id(value)
        elif key == "voice vlan":
            current.voice_vlan = _vlan_id(value)
        elif in_trunk_list:
            current.trunk_vlans_text = value
    return ports


_INVENTORY_ENTRY = re.compile(
    r'NAME:\s*"(?P<name>[^"]*)"\s*,\s*DESCR:\s*"(?P<description>[^"]*)"\s*\n'
    r"\s*PID:\s*(?P<pid>[^,]*?)\s*,\s*VID:\s*(?P<vid>[^,]*?)\s*,\s*SN:\s*(?P<serial>\S*)"
)


def parse_inventory(output: str) -> list[ParsedInventoryItem]:
    """``show inventory`` (IOS-XE, NX-OS and IOS-XR print the same NAME/DESCR/PID/VID/SN pairs)."""
    items = []
    for m in _INVENTORY_ENTRY.finditer(output):
        serial = m["serial"].strip()
        items.append(
            ParsedInventoryItem(
                name=m["name"].strip(),
                description=m["description"].strip(),
                pid=m["pid"].strip(),
                vid=m["vid"].strip(),
                serial="" if serial.upper() in ("N/A", "NA") else serial,
            )
        )
    return items


def _first(pattern: str, text: str) -> str:
    m = re.search(pattern, text, re.MULTILINE)
    return m.group(1).strip() if m else ""


def parse_cdp_neighbors(output: str) -> list[ParsedNeighbor]:
    """``show cdp neighbors detail`` (IOS-XE, NX-OS, IOS-XR); records are separated by dashes."""
    neighbors = []
    for record in re.split(r"(?m)^-{5,}\s*$", output):
        name = _first(r"^Device ID:\s*(\S+)", record)
        local = _first(r"^Interface:\s*([^,]+),", record)
        remote = _first(r"Port ID \(outgoing port\):\s*(.+?)\s*$", record)
        if not (name and local and remote):
            continue
        neighbors.append(
            ParsedNeighbor(
                local_interface=expand_interface_name(local),
                name=re.sub(r"\(.*\)$", "", name),  # NX-OS appends "(serial)"
                interface=remote,
                protocol="cdp",
                platform=_first(r"^Platform:\s*([^,]+)", record),
                ip=_first(r"(?:IP address|IPv4 Address):\s*(\S+)", record),
            )
        )
    return neighbors


def parse_lldp_neighbors(output: str) -> list[ParsedNeighbor]:
    """``show lldp neighbors detail``.

    IOS-XE starts every record with "Local Intf:", NX-OS with "Chassis id:" (and
    calls the local port "Local Port id").
    """
    start = "Local Intf:" if re.search(r"(?m)^Local Intf:", output) else "Chassis id:"
    neighbors = []
    for record in re.split(rf"(?m)^(?={start})", output):
        local = _first(r"^Local (?:Intf|Port id):\s*(\S+)", record)
        port = _first(r"^Port id:\s*(\S+)", record)
        if not (local and port):
            continue
        description = re.search(r"(?m)^System Description:[ \t]*(.*)\n(?:[ \t]*(.+))?", record)
        platform = ""
        if description:
            platform = (description.group(1).strip() or (description.group(2) or "").strip())[:80]
        neighbors.append(
            ParsedNeighbor(
                local_interface=expand_interface_name(local),
                name=_first(r"^System Name:\s*(\S+)", record)
                or _first(r"^Chassis id:\s*(\S+)", record),
                interface=port,
                protocol="lldp",
                platform=platform,
                ip=_first(r"^\s*(?:IP|Management Address):\s*(\d+\.\d+\.\d+\.\d+)", record),
            )
        )
    return neighbors


def merge_neighbors(*sources: list[ParsedNeighbor]) -> list[ParsedNeighbor]:
    """One entry per link; the first source wins (CDP before LLDP: it reports the platform)."""
    merged, seen = [], set()
    for source in sources:
        for neighbor in source:
            local = neighbor.local_interface.lower()
            remote = expand_interface_name(neighbor.interface).lower()
            key = (local, neighbor.name.split(".")[0].lower(), remote)
            if key not in seen:
                seen.add(key)
                merged.append(neighbor)
    return merged


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def parse_device_output(driver: str, output, mgmt_ip: str) -> ParsedDevice:
    """Parse the raw output collected by connection.collect_output for ``driver``."""
    if driver == "nxos":
        device = NXOSParser.parse_show_version(output.show_version)
        if output.show_interfaces:
            device.interfaces = NXOSParser.parse_show_interfaces(output.show_interfaces, mgmt_ip)
    else:
        # IOS-XE and IOS-XR share the IOS-style output format closely enough.
        device = IOSXEParser.parse_show_version(output.show_version)
        if output.show_interfaces:
            device.interfaces = IOSXEParser.parse_show_interfaces(output.show_interfaces, mgmt_ip)
            if output.show_ip_interface:
                IOSXEParser.parse_show_ip_interface(output.show_ip_interface, device.interfaces)

    # Optional commands; missing attributes / empty output simply mean "not collected".
    device.vlans = parse_vlan_brief(getattr(output, "show_vlan", "") or "")
    device.switchports = parse_switchport(getattr(output, "show_switchport", "") or "")
    device.inventory = parse_inventory(getattr(output, "show_inventory", "") or "")
    device.neighbors = merge_neighbors(
        parse_cdp_neighbors(getattr(output, "show_cdp", "") or ""),
        parse_lldp_neighbors(getattr(output, "show_lldp", "") or ""),
    )
    return device
