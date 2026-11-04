"""Writing what was learned from a device into NetBox.

Input: a :class:`~.parsers.ParsedDevice`. Output: a :class:`SyncReport` that
lists every object created or changed, which is shown on the collection result
page and summarised in the job log.

What is synchronised (all inside one database transaction per device):

* ``serial`` - set when empty in NetBox; a *different* serial aborts the
  collection (the IP probably belongs to another box now), see :func:`check_serial`;
* custom fields named by ``CF_NAME_*`` - only the ones that exist on dcim.Device;
* interfaces (when ``COLLECT_INTERFACES_DATA``): created when missing; description,
  MTU, speed, duplex and enabled state updated. The interface *type* is only
  guessed for new interfaces and never overwritten afterwards;
* LAGs and LAG membership (when ``COLLECT_PORT_CHANNEL_DATA``);
* MAC addresses, assigned to the interface and set as its primary MAC;
* IPv4 addresses (primary and secondary) with VRF, role and DHCP status;
* VLANs and 802.1Q interface modes (when ``COLLECT_VLANS``), see :func:`_sync_vlans`;
* inventory items from ``show inventory`` (when ``COLLECT_INVENTORY``), see :func:`_sync_inventory`;
* cables to CDP/LLDP neighbors (when ``CREATE_CABLES``), see neighbors.py.

VLAN interfaces (SVIs) are skipped.
"""

from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime

from dcim.choices import InterfaceModeChoices, InterfaceTypeChoices
from dcim.fields import mac_unix_expanded_uppercase
from dcim.models import Interface, InventoryItem, MACAddress
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from ipam.choices import IPAddressRoleChoices, IPAddressStatusChoices
from ipam.models import VLAN, VRF, IPAddress
from netaddr import EUI, AddrFormatError, IPNetwork

from config_officer.choices import CollectFailChoices, TransportChoices
from config_officer.configuration import PluginSettings

from .connection import CollectionError
from .neighbors import NO_CABLE, create_cable, match_neighbors
from .parsers import ParsedDevice, ParsedInterface, ParsedSwitchport, expand_interface_name

logger = logging.getLogger(__name__)


@dataclass
class SyncReport:
    """Everything one collection changed in NetBox for one device."""

    serial: str = ""
    custom_fields: dict = field(default_factory=dict)
    interfaces_created: list[str] = field(default_factory=list)
    interfaces_updated: dict[str, list[str]] = field(default_factory=dict)
    lag_members: list[str] = field(default_factory=list)
    mac_addresses: list[str] = field(default_factory=list)
    ip_addresses_created: list[str] = field(default_factory=list)
    ip_addresses_updated: list[str] = field(default_factory=list)
    vrfs_created: list[str] = field(default_factory=list)
    vlans_created: list[str] = field(default_factory=list)
    vlan_assignments: list[str] = field(default_factory=list)
    inventory_created: list[str] = field(default_factory=list)
    inventory_updated: list[str] = field(default_factory=list)
    inventory_removed: list[str] = field(default_factory=list)
    cables_created: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    #: (field, label) of every kind of change, in the order shown in summaries.
    CHANGES = (
        ("interfaces_created", "interface(s) created"),
        ("interfaces_updated", "interface(s) updated"),
        ("lag_members", "LAG membership(s) set"),
        ("mac_addresses", "MAC address(es) set"),
        ("ip_addresses_created", "IP address(es) created"),
        ("ip_addresses_updated", "IP address(es) updated"),
        ("vrfs_created", "VRF(s) created"),
        ("vlans_created", "VLAN(s) created"),
        ("vlan_assignments", "interface VLAN assignment(s) updated"),
        ("inventory_created", "inventory item(s) created"),
        ("inventory_updated", "inventory item(s) updated"),
        ("inventory_removed", "inventory item(s) removed"),
        ("cables_created", "cable(s) created"),
    )

    def summary(self) -> str:
        """Short human summary, e.g. '2 interface(s) created, 1 VLAN(s) created'."""
        parts = [f"serial set to {self.serial}"] if self.serial else []
        parts += [
            f"{len(getattr(self, name))} {label}"
            for name, label in self.CHANGES
            if getattr(self, name)
        ]
        return ", ".join(parts) or "no NetBox changes"

    @property
    def changed_objects(self) -> int:
        return bool(self.serial) + sum(len(getattr(self, name)) for name, _ in self.CHANGES)

    def as_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Device-level fields
# ---------------------------------------------------------------------------


def check_serial(device, parsed: ParsedDevice) -> None:
    """Refuse to touch a device whose serial differs from the collected one."""
    if device.serial and parsed.serial and device.serial != parsed.serial:
        raise CollectionError(
            CollectFailChoices.FAIL_UPDATE,
            f"Serial mismatch: NetBox has {device.serial!r}, the device reports {parsed.serial!r}. "
            "Nothing was saved - check that the primary IP points to the right device.",
        )


def _sync_device_fields(
    device, parsed: ParsedDevice, transport: str, settings: PluginSettings, now, report
):
    from extras.models import CustomField

    device.snapshot()  # pre-change state for NetBox's change log
    changed = False
    if not device.serial and parsed.serial:
        device.serial = parsed.serial
        report.serial = parsed.serial
        changed = True

    local_now = timezone.localtime(now)
    wanted = {
        settings.cf_ssh: transport == TransportChoices.TRANSPORT_SSH,
        settings.cf_sw_version: parsed.version.upper() if parsed.version else "",
        settings.cf_last_collect_date: local_now.date().isoformat(),
        settings.cf_last_collect_time: local_now.strftime("%H:%M:%S"),
    }
    existing = {cf.name for cf in CustomField.objects.get_for_model(device)}
    for name, value in wanted.items():
        if name and name in existing and device.custom_field_data.get(name) != value:
            device.custom_field_data[name] = value
            report.custom_fields[name] = value
            changed = True
    if changed:
        device.save()


# ---------------------------------------------------------------------------
# Interfaces
# ---------------------------------------------------------------------------

_TYPE_RULES: list[tuple[str, str]] = [
    (r"^(eth|ethernet).*100g", InterfaceTypeChoices.TYPE_100GE_CFP),
    (r"^(eth|ethernet).*40g", InterfaceTypeChoices.TYPE_40GE_QSFP_PLUS),
    (r"^(eth|ethernet).*10g", InterfaceTypeChoices.TYPE_10GE_SFP_PLUS),
    (r"^(eth|ethernet)", InterfaceTypeChoices.TYPE_1GE_FIXED),
    (r"^(gigabitethernet|gi\d)", InterfaceTypeChoices.TYPE_1GE_FIXED),
    (r"^(tengig|te\d|ten)", InterfaceTypeChoices.TYPE_10GE_SFP_PLUS),
    (r"^(fortygig|fo\d)", InterfaceTypeChoices.TYPE_40GE_QSFP_PLUS),
    (r"^(hundredgig|hu\d)", InterfaceTypeChoices.TYPE_100GE_CFP),
    (r"^(fastethernet|fa\d)", InterfaceTypeChoices.TYPE_100ME_FIXED),
    (r"^(vlan|svi|bvi|loopback|lo\d|tunnel|tun)", InterfaceTypeChoices.TYPE_VIRTUAL),
    (r"^(port.channel|po\d)", InterfaceTypeChoices.TYPE_LAG),
    (r"^mgmt", InterfaceTypeChoices.TYPE_1GE_FIXED),
]


def infer_interface_type(name: str) -> str:
    """Best-effort NetBox interface type from the interface name (used for new interfaces only)."""
    lowered = name.lower()
    for pattern, if_type in _TYPE_RULES:
        if re.match(pattern, lowered):
            return if_type
    return InterfaceTypeChoices.TYPE_OTHER


def _is_lag_name(name: str) -> bool:
    return bool(re.match(r"^(port.channel|po\d)", name.lower()))


def _speed_kbps(speed: str | None) -> int | None:
    """'1000Mbps' -> 1_000_000 (NetBox stores speed in Kbps); unknown formats -> None."""
    if not speed:
        return None
    match = re.match(r"^(\d+)\s*(K|M|G)b/?p?s?$", speed.strip(), re.IGNORECASE)
    if not match:
        return None
    multiplier = {"K": 1, "M": 1000, "G": 1_000_000}[match.group(2).upper()]
    return int(match.group(1)) * multiplier


def _desired_fields(parsed: ParsedInterface) -> dict:
    """Interface attributes the device is authoritative for (None = unknown, leave as is)."""
    return {
        "description": parsed.description,
        "mtu": parsed.mtu,
        "speed": _speed_kbps(parsed.speed),
        "duplex": parsed.duplex,
        "enabled": parsed.admin_up,
    }


def _ensure_lags(device, parsed: dict[str, ParsedInterface], existing: dict, report: SyncReport):
    for lag_name in sorted({p.lag for p in parsed.values() if p.lag}):
        if lag_name.lower() in existing:
            continue
        lag = Interface(
            device=device, name=lag_name, type=InterfaceTypeChoices.TYPE_LAG, enabled=True
        )
        lag.save()
        existing[lag_name.lower()] = lag
        report.interfaces_created.append(lag_name)


def _sync_interface(
    device, parsed: ParsedInterface, existing: dict, report: SyncReport
) -> Interface:
    interface = existing.get(parsed.name.lower())
    desired = {key: value for key, value in _desired_fields(parsed).items() if value is not None}

    if interface is None:
        interface = Interface(
            device=device, name=parsed.name, type=infer_interface_type(parsed.name)
        )
        for key, value in desired.items():
            setattr(interface, key, value)
        interface.save()
        existing[parsed.name.lower()] = interface
        report.interfaces_created.append(parsed.name)
        return interface

    changes = [key for key, value in desired.items() if getattr(interface, key) != value]
    if changes:
        interface.snapshot()
        for key in changes:
            setattr(interface, key, desired[key])
        interface.save()
        report.interfaces_updated[interface.name] = changes
    return interface


def _sync_mac(interface: Interface, value: str, report: SyncReport) -> None:
    try:
        mac = EUI(value, version=48, dialect=mac_unix_expanded_uppercase)
    except (AddrFormatError, TypeError):
        report.warnings.append(f"{interface.name}: invalid MAC address {value!r}")
        return

    current = interface.primary_mac_address
    if current is not None and current.mac_address == mac:
        if current.assigned_object_id is None:
            # Plugin 0.1.x created primary MACs without assigning them; repair that.
            current.snapshot()
            current.assigned_object = interface
            current.save()
            report.mac_addresses.append(f"{interface.name}: {mac} (assigned to interface)")
        return

    record = interface.mac_addresses.filter(mac_address=mac).first()
    if record is None:
        record = MACAddress(mac_address=mac, assigned_object=interface)
        record.save()
    interface.snapshot()
    interface.primary_mac_address = record
    interface.save()
    report.mac_addresses.append(f"{interface.name}: {mac}")


def _get_or_create_vrf(name: str, report: SyncReport):
    vrf = VRF.objects.filter(name__iexact=name).first()
    if vrf is None:
        vrf = VRF.objects.create(name=name, enforce_unique=False)
        report.vrfs_created.append(name)
    return vrf


def _ip_role(interface: Interface, is_secondary: bool) -> str:
    if is_secondary:
        return IPAddressRoleChoices.ROLE_SECONDARY
    if re.match(r"loopback|lo\d", interface.name.lower()):
        return IPAddressRoleChoices.ROLE_LOOPBACK
    return ""


def _find_ip(interface: Interface, network: IPNetwork, vrf):
    """Existing NetBox record for this address, or None.

    1. same host address in the same VRF (any prefix length, so a stale /32 is fixed);
    2. the same host address already on this interface, e.g. in another VRF.
    Unrelated addresses elsewhere in NetBox are never taken over.
    """
    host = str(network.ip)
    return (
        IPAddress.objects.filter(address__net_host=host, vrf=vrf).first()
        or interface.ip_addresses.filter(address__net_host=host).first()
    )


def _update_ip(
    ip: IPAddress, interface: Interface, network: IPNetwork, vrf, role: str, status
) -> list[str]:
    """Apply the device's view to an existing IP; returns the names of changed attributes."""
    ip.snapshot()
    changes: list[str] = []

    def apply(attr: str, value, label: str) -> None:
        if getattr(ip, attr) != value:
            setattr(ip, attr, value)
            changes.append(label)

    apply("address", network, "prefix length")
    apply("vrf", vrf, "VRF")
    apply("assigned_object", interface, "assigned interface")
    if role:
        apply("role", role, "role")
    if status:
        apply("status", status, "status")
    if changes:
        ip.save()
    return changes


def _sync_ip_addresses(
    device, interface: Interface, parsed: ParsedInterface, report: SyncReport
) -> None:
    candidates = ([(parsed.ip, False)] if parsed.ip else []) + [
        (ip, True) for ip in parsed.secondary
    ]
    vrf = _get_or_create_vrf(parsed.vrf, report) if parsed.vrf and candidates else None
    status = IPAddressStatusChoices.STATUS_DHCP if parsed.dhcp else None

    for address, is_secondary in candidates:
        try:
            network = IPNetwork(address)
        except (AddrFormatError, ValueError):
            report.warnings.append(f"{interface.name}: invalid IP address {address!r}")
            continue
        role = _ip_role(interface, is_secondary)
        try:
            with transaction.atomic():  # one bad address must not roll back the others
                ip = _find_ip(interface, network, vrf)
                if ip is None:
                    ip = IPAddress(
                        address=network, vrf=vrf, tenant=device.tenant, assigned_object=interface
                    )
                    ip.role = role
                    if status:
                        ip.status = status
                    ip.save()
                    report.ip_addresses_created.append(f"{network} on {interface.name}")
                elif changes := _update_ip(ip, interface, network, vrf, role, status):
                    report.ip_addresses_updated.append(
                        f"{network} on {interface.name}: {', '.join(changes)}"
                    )
        except Exception as exc:  # noqa: BLE001 - recorded in the report, collection goes on
            logger.exception(
                "[%s] could not sync IP %s on %s", device.name, address, interface.name
            )
            report.warnings.append(f"{interface.name}: IP {address} not synced ({exc})")


def _attach_lag_members(
    parsed: dict[str, ParsedInterface], existing: dict, report: SyncReport
) -> None:
    for name, item in parsed.items():
        if not item.lag:
            continue
        member = existing.get(name.lower())
        lag = existing.get(item.lag.lower())
        if member is None or lag is None:
            report.warnings.append(f"LAG {item.lag} or its member {name} is missing in NetBox")
            continue
        if member.lag_id != lag.pk:
            member.snapshot()
            member.lag = lag
            member.save()
            report.lag_members.append(f"{name} -> {item.lag}")


def _existing_interfaces(device) -> dict[str, Interface]:
    """The device's interfaces in NetBox, keyed by lower-case name."""
    return {
        interface.name.lower(): interface for interface in Interface.objects.filter(device=device)
    }


def _sync_interfaces(
    device,
    parsed: dict[str, ParsedInterface],
    existing: dict,
    settings: PluginSettings,
    report: SyncReport,
):
    sync_lags = settings.collect_port_channel_data
    if sync_lags:
        _ensure_lags(device, parsed, existing, report)

    for name, item in parsed.items():
        if name.lower().startswith("vlan"):
            continue  # SVIs are not modelled yet
        if _is_lag_name(name) and not sync_lags:
            continue
        interface = _sync_interface(device, item, existing, report)
        if item.mac:
            _sync_mac(interface, item.mac, report)
        _sync_ip_addresses(device, interface, item, report)

    if sync_lags:
        _attach_lag_members(parsed, existing, report)


# ---------------------------------------------------------------------------
# VLANs and 802.1Q modes
# ---------------------------------------------------------------------------

#: Legacy FDDI / Token Ring VLANs IOS always lists; never created in NetBox.
RESERVED_VLAN_IDS = frozenset(range(1002, 1006))


def _available_vlans(device) -> dict[int, VLAN]:
    """VLANs NetBox allows on this device's interfaces, by VLAN ID.

    Uses NetBox's own rule (``VLAN.objects.get_for_device``: VLANs of the site,
    of VLAN groups scoped to the device's site/location/region/..., and global
    ones). When a VLAN ID exists several times, the most specific one wins:
    site VLAN, then VLAN group, then global.
    """

    def specificity(vlan):
        if vlan.site_id == device.site_id:
            return 0
        return 1 if vlan.group_id else 2

    vlans: dict[int, VLAN] = {}
    for vlan in sorted(VLAN.objects.get_for_device(device), key=specificity):
        vlans.setdefault(vlan.vid, vlan)
    return vlans


def _sync_vlans(device, parsed: ParsedDevice, report: SyncReport) -> dict[int, VLAN]:
    """Create the device's VLANs that NetBox does not know yet (as VLANs of the device's site).

    Existing VLANs are reused and never renamed - names in NetBox may be curated.
    """
    vlans = _available_vlans(device)
    for vid, item in sorted(parsed.vlans.items()):
        if vid in RESERVED_VLAN_IDS or vid in vlans:
            continue
        vlan = VLAN(
            site=device.site, vid=vid, name=(item.name or f"VLAN{vid}")[:64], tenant=device.tenant
        )
        vlan.save()
        vlans[vid] = vlan
        report.vlans_created.append(f"{vid} {vlan.name}")
    return vlans


def _desired_vlan_mode(port: ParsedSwitchport, vlans: dict[int, VLAN]):
    """(mode, untagged VLAN, tagged VLANs) for a port, or None to leave the interface alone.

    * access port                 -> access, untagged = access VLAN
    * access port with voice VLAN -> tagged, untagged = access VLAN, tagged = [voice VLAN]
    * trunk allowing all VLANs    -> tagged-all, untagged = native VLAN
    * trunk with an allowed list  -> tagged, untagged = native VLAN, tagged = allowed VLANs
    * routed port (no switchport) -> no mode, no VLANs
    """
    if not port.enabled:
        return None, None, []
    if port.mode == "access":
        untagged = vlans.get(port.access_vlan)
        if port.voice_vlan in vlans:
            return InterfaceModeChoices.MODE_TAGGED, untagged, [vlans[port.voice_vlan]]
        return InterfaceModeChoices.MODE_ACCESS, untagged, []
    if port.mode == "trunk":
        native = vlans.get(port.native_vlan)
        allowed = port.trunk_vlans
        if allowed is None:
            return InterfaceModeChoices.MODE_TAGGED_ALL, native, []
        tagged = [vlans[vid] for vid in allowed if vid in vlans and vid != port.native_vlan]
        return InterfaceModeChoices.MODE_TAGGED, native, tagged
    return None  # down / not recognised


def _sync_switchports(
    parsed: ParsedDevice, existing: dict, vlans: dict[int, VLAN], report: SyncReport
):
    for port in parsed.switchports.values():
        interface = existing.get(port.name.lower())
        desired = _desired_vlan_mode(port, vlans)
        if interface is None or desired is None:
            continue
        mode, untagged, tagged = desired
        wanted_tagged = {vlan.pk for vlan in tagged}
        current_tagged = set(interface.tagged_vlans.values_list("pk", flat=True))
        if (
            (interface.mode or None) == mode
            and interface.untagged_vlan_id == (untagged.pk if untagged else None)
            and current_tagged == wanted_tagged
        ):
            continue
        interface.snapshot()
        interface.mode = mode
        interface.untagged_vlan = untagged
        interface.save()  # NetBox clears tagged VLANs unless the mode is "tagged"
        if mode == InterfaceModeChoices.MODE_TAGGED:
            interface.tagged_vlans.set(tagged)
        description = mode or "routed"
        if untagged:
            description += f", untagged {untagged.vid}"
        if tagged:
            description += ", tagged " + ",".join(str(vlan.vid) for vlan in tagged)
        report.vlan_assignments.append(f"{interface.name}: {description}")


# ---------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------


def _sync_inventory(device, parsed: ParsedDevice, existing: dict, report: SyncReport) -> None:
    """Top-level inventory items from ``show inventory``, matched by name.

    * missing items are created and marked ``discovered`` (NetBox's flag for
      automatically found hardware); an item named like an interface (a
      transceiver) is linked to that interface;
    * part ID, serial and description of existing items follow the device;
    * a ``discovered`` item that the device no longer reports is deleted (unless
      someone nested other items under it). Items created by hand are never deleted.
    """
    current = {
        item.name: item for item in InventoryItem.objects.filter(device=device, parent__isnull=True)
    }
    seen: set[str] = set()
    for entry in parsed.inventory:
        name = entry.name[:64]
        if not name or name in seen:
            continue
        seen.add(name)
        wanted = {
            "part_id": entry.pid[:50],
            "serial": entry.serial[:50],
            "description": entry.description[:200],
        }
        item = current.get(name)
        if item is None:
            item = InventoryItem(device=device, name=name, discovered=True, **wanted)
            interface = existing.get(expand_interface_name(name).lower())
            if interface is not None:
                item.component = interface
            item.save()
            report.inventory_created.append(f"{name} ({entry.pid or 'no PID'})")
            continue
        changes = [key for key, value in wanted.items() if getattr(item, key) != value]
        if changes:
            item.snapshot()
            for key in changes:
                setattr(item, key, wanted[key])
            item.save()
            report.inventory_updated.append(f"{name} ({', '.join(changes)})")

    for name, item in current.items():
        if item.discovered and name not in seen and not item.child_items.exists():
            item.delete()
            report.inventory_removed.append(name)


def _sync_cables(device, parsed: ParsedDevice, report: SyncReport) -> None:
    """Cable every CDP/LLDP link whose two ports exist in NetBox and are both uncabled."""
    for match in match_neighbors(device, [asdict(neighbor) for neighbor in parsed.neighbors]):
        if match.status != NO_CABLE:
            continue
        link = f"{match.local_interface.name} <-> {match.device.name} {match.interface.name}"
        try:
            with transaction.atomic():
                create_cable(match.local_interface, match.interface, match.neighbor["protocol"])
        except ValidationError as exc:
            report.warnings.append(f"cable {link} not created: {'; '.join(exc.messages)}")
        else:
            report.cables_created.append(link)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def sync_device(
    device,
    parsed: ParsedDevice,
    transport: str,
    settings: PluginSettings,
    now: datetime | None = None,
) -> SyncReport:
    """Apply ``parsed`` to ``device`` in NetBox and report what changed.

    Raises CollectionError (FAIL_UPDATE) on a serial mismatch; everything else
    is applied atomically, so a crash leaves NetBox untouched for this device.
    """
    check_serial(device, parsed)
    report = SyncReport()
    with transaction.atomic():
        _sync_device_fields(device, parsed, transport, settings, now or timezone.now(), report)
        existing = _existing_interfaces(device)
        if settings.collect_interfaces_data and parsed.interfaces:
            _sync_interfaces(device, parsed.interfaces, existing, settings, report)
        if settings.collect_vlans and (parsed.vlans or parsed.switchports):
            vlans = _sync_vlans(device, parsed, report)
            _sync_switchports(parsed, existing, vlans, report)
        if settings.collect_inventory and parsed.inventory:
            _sync_inventory(device, parsed, existing, report)
        if settings.collect_neighbors and settings.create_cables and parsed.neighbors:
            _sync_cables(device, parsed, report)
    return report
