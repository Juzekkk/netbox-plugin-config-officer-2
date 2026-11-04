"""CDP/LLDP neighbors compared with NetBox, and cables created from them.

The neighbors of a device are stored with its latest collection result
(``Collection.details["neighbors"]``). :func:`match_neighbors` resolves them
against NetBox *at the time it is called*, so the device's "Neighbors" tab always
shows the current cabling state:

=================  ===================================================================
Status             Meaning
=================  ===================================================================
connected          NetBox has a cable (or cable path) between exactly these two ports
no cable           both ports exist in NetBox and neither is cabled: a cable can be added
different cable    NetBox cables at least one of the ports to something else
not in NetBox      the neighbor device or one of the two ports is unknown to NetBox
=================  ===================================================================

Cables are only ever created for "no cable" links: automatically during a
collection when ``CREATE_CABLES`` is enabled, or one at a time from the tab.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass

from dcim.choices import LinkStatusChoices
from dcim.models import Cable, Device, Interface
from django.db.models import Q

from .parsers import expand_interface_name

CONNECTED = "connected"
NO_CABLE = "no cable"
DIFFERENT_CABLE = "different cable"
NOT_IN_NETBOX = "not in NetBox"

STATUS_COLORS = {
    CONNECTED: "green",
    NO_CABLE: "yellow",
    DIFFERENT_CABLE: "red",
    NOT_IN_NETBOX: "gray",
}


@dataclass
class NeighborMatch:
    neighbor: dict  # as stored: local_interface, name, interface, protocol, platform, ip
    local_interface: Interface | None
    device: Device | None
    interface: Interface | None
    status: str
    note: str = ""

    @property
    def color(self) -> str:
        return STATUS_COLORS[self.status]


def find_device(name: str, ip: str = "") -> tuple[Device | None, str]:
    """NetBox device of an advertised neighbor, and a note on how it was found.

    1. the advertised name, then the name without its domain (case-insensitive);
    2. the only NetBox device named "<hostname>.<domain>" (NetBox stores the FQDN);
    3. the advertised IP address: the only device that has it as primary IP or on
       one of its interfaces (names in NetBox often differ from hostnames).
    """
    hostname = name.split(".", maxsplit=1)[0]
    for candidate in dict.fromkeys((name, hostname)):
        device = Device.objects.filter(name__iexact=candidate).first()
        if device is not None:
            return device, ""
    with_domain = list(Device.objects.filter(name__istartswith=f"{hostname}.")[:2])
    if len(with_domain) == 1:
        return with_domain[0], f"matched by name {with_domain[0].name}"
    if ip and _is_ip(ip):
        owners = list(
            Device.objects.filter(
                Q(primary_ip4__address__net_host=ip)
                | Q(primary_ip6__address__net_host=ip)
                | Q(interfaces__ip_addresses__address__net_host=ip)
            ).distinct()[:2]
        )
        if len(owners) == 1:
            return owners[0], f"matched by IP {ip}"
    return None, ""


def _is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


def _find_interface(device: Device, advertised: str) -> Interface | None:
    names = {advertised, expand_interface_name(advertised)}
    pattern = "^(" + "|".join(re.escape(name) for name in names) + ")$"
    return Interface.objects.filter(device=device, name__iregex=pattern).first()


def _status(local: Interface, remote: Interface) -> str:
    if remote in (local.connected_endpoints or []):
        return CONNECTED
    if local.cable_id is None and remote.cable_id is None:
        return NO_CABLE
    return DIFFERENT_CABLE


def match_neighbors(device: Device, neighbors: list[dict]) -> list[NeighborMatch]:
    local_interfaces = {i.name.lower(): i for i in Interface.objects.filter(device=device)}
    matches = []
    for neighbor in neighbors:
        local = local_interfaces.get(neighbor["local_interface"].lower())
        remote_device, how = find_device(neighbor["name"], neighbor.get("ip", ""))
        remote = _find_interface(remote_device, neighbor["interface"]) if remote_device else None
        if local is None:
            match = NeighborMatch(
                neighbor, None, remote_device, remote, NOT_IN_NETBOX, "local port unknown"
            )
        elif remote_device is None:
            note = "device unknown (neither its name nor its IP is in NetBox)"
            match = NeighborMatch(neighbor, local, None, None, NOT_IN_NETBOX, note)
        elif remote is None:
            note = f"port {neighbor['interface']} not on {remote_device}" + (
                f"; {how}" if how else ""
            )
            match = NeighborMatch(neighbor, local, remote_device, None, NOT_IN_NETBOX, note)
        else:
            match = NeighborMatch(
                neighbor, local, remote_device, remote, _status(local, remote), how
            )
        matches.append(match)
    return matches


def create_cable(local: Interface, remote: Interface, protocol: str) -> Cable:
    """Cable between two uncabled ports; raises ValidationError if NetBox refuses it."""
    cable = Cable(
        a_terminations=[local],
        b_terminations=[remote],
        status=LinkStatusChoices.STATUS_CONNECTED,
        description=f"Discovered by Config Officer ({protocol.upper()})",
    )
    cable.full_clean()
    cable.save()
    return cable
