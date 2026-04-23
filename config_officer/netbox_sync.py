"""
NetBox synchronisation helpers.

Responsible for creating / updating Interface, IPAddress, MACAddress and VRF
objects based on data collected from live devices.
"""

from __future__ import annotations

import logging
import re

from django.db import transaction

from dcim.choices import InterfaceTypeChoices
from dcim.fields import mac_unix_expanded_uppercase
from dcim.models import Interface, MACAddress
from ipam.choices import IPAddressRoleChoices, IPAddressStatusChoices
from ipam.models import IPAddress, VRF
from netaddr import EUI

from .models import ParsedInterface

logger = logging.getLogger(__name__)


# Interface type inference

_TYPE_RULES: list[tuple[str, str]] = [
    (r"^(eth|ethernet).*100g",          InterfaceTypeChoices.TYPE_100GE_CFP),
    (r"^(eth|ethernet).*40g",           InterfaceTypeChoices.TYPE_40GE_QSFP_PLUS),
    (r"^(eth|ethernet).*10g",           InterfaceTypeChoices.TYPE_10GE_SFP_PLUS),
    (r"^(eth|ethernet)",                InterfaceTypeChoices.TYPE_1GE_FIXED),
    (r"^(gigabit|gi)\d",               InterfaceTypeChoices.TYPE_1GE_FIXED),
    (r"^(tengig|te\d|ten)",            InterfaceTypeChoices.TYPE_10GE_SFP_PLUS),
    (r"^(fortygig|fo\d)",              InterfaceTypeChoices.TYPE_40GE_QSFP_PLUS),
    (r"^(hundredgig|hu\d)",            InterfaceTypeChoices.TYPE_100GE_CFP),
    (r"^(fastethernet|fa\d)",          InterfaceTypeChoices.TYPE_100ME_FIXED),
    (r"^(vlan|svi|bvi|loopback|lo\d|tunnel|tun)", InterfaceTypeChoices.TYPE_VIRTUAL),
    (r"^(port.channel|po\d)",          InterfaceTypeChoices.TYPE_LAG),
    (r"^mgmt",                         InterfaceTypeChoices.TYPE_1GE_FIXED),
]


def infer_interface_type(name: str) -> str:
    n = name.lower()
    for pattern, if_type in _TYPE_RULES:
        if re.match(pattern, n):
            return if_type
    return InterfaceTypeChoices.TYPE_OTHER


# LAG helpers

def _ensure_lags(device_netbox, parsed: dict[str, ParsedInterface]) -> None:
    """Create any LAG interfaces referenced by member interfaces that don't yet exist."""
    required_lags = {pif.lag for pif in parsed.values() if pif.lag}
    if not required_lags:
        return

    existing_lags = set(
        Interface.objects.filter(
            device=device_netbox,
            type=InterfaceTypeChoices.TYPE_LAG,
        ).values_list("name", flat=True)
    )

    for lag_name in required_lags - existing_lags:
        logger.debug("[LAG] Creating %s on %s", lag_name, device_netbox.name)
        Interface.objects.create(
            device=device_netbox,
            name=lag_name,
            type=InterfaceTypeChoices.TYPE_LAG,
            enabled=True,
        )


def _attach_lags(device_netbox, parsed: dict[str, ParsedInterface]) -> None:
    """Assign member interfaces to their LAG parent."""
    iface_map: dict[str, Interface] = {
        i.name.lower(): i
        for i in Interface.objects.filter(device=device_netbox)
    }

    for name, pif in parsed.items():
        if not pif.lag:
            continue

        member = iface_map.get(name.lower())
        lag    = iface_map.get(pif.lag.lower())

        if not member:
            logger.warning("[LAG] Member interface %r not found in NetBox", name)
            continue
        if not lag:
            logger.warning("[LAG] LAG interface %r not found in NetBox (member=%s)", pif.lag, name)
            continue

        if member.lag_id != lag.pk:
            member.lag = lag
            member.save(update_fields=["lag"])
            logger.debug("[LAG] %s -> %s", name, pif.lag)


# MAC address helper

def _assign_mac(iface: Interface, mac_str: str) -> None:
    """
    Create (or fetch) a MACAddress object and assign it as the primary MAC
    of *iface*, unless it is already in use by a different interface.
    """
    with transaction.atomic():
        mac_obj, _ = MACAddress.objects.get_or_create(
            mac_address=EUI(mac_str, version=48, dialect=mac_unix_expanded_uppercase)
        )
        conflict = (
            Interface.objects.filter(primary_mac_address=mac_obj)
            .exclude(pk=iface.pk)
            .exists()
        )
        if conflict:
            logger.warning(
                "[MAC] %s already assigned as primary MAC elsewhere - skipping %s",
                mac_str, iface.name,
            )
            return

        iface.primary_mac_address = mac_obj
        iface.save(update_fields=["primary_mac_address"])


# IP address sync

def _sync_ips(device_netbox, iface: Interface, pif: ParsedInterface) -> None:
    """Create / update IPAddress objects and attach them to *iface*."""
    candidates: list[tuple[str, bool]] = []  # (address/prefix, is_secondary)
    if pif.ip:
        candidates.append((pif.ip, False))
    candidates.extend((addr, True) for addr in pif.secondary)

    for addr, is_secondary in candidates:
        logger.debug("[IP] %s: syncing %s (secondary=%s)", iface.name, addr, is_secondary)
        try:
            ip_obj, created = IPAddress.objects.get_or_create(
                address=addr,
                defaults={"tenant": device_netbox.tenant},
            )
            if created:
                logger.debug("[IP] Created %s", addr)

            if ip_obj not in iface.ip_addresses.all():
                iface.ip_addresses.add(ip_obj)
                logger.debug("[IP] Assigned %s -> %s", addr, iface.name)

            # Role
            if is_secondary:
                ip_obj.role = IPAddressRoleChoices.ROLE_SECONDARY
            elif re.match(r"loopback|lo\d", iface.name.lower()):
                ip_obj.role = IPAddressRoleChoices.ROLE_LOOPBACK

            if pif.dhcp:
                ip_obj.status = IPAddressStatusChoices.STATUS_DHCP

            # VRF
            if pif.vrf:
                ip_obj.vrf = _get_or_create_vrf(pif.vrf)

            ip_obj.save()

        except Exception:
            logger.exception("[IP] Error syncing %s on %s", addr, iface.name)


def _get_or_create_vrf(name: str) -> VRF:
    try:
        return VRF.objects.get(name__iexact=name)
    except VRF.DoesNotExist:
        logger.debug("[VRF] Creating %r", name)
        return VRF.objects.create(name=name, enforce_unique=False)


# Interface field update helpers

def _apply_speed(iface: Interface, speed_str: str) -> bool:
    """Convert e.g. '1000Mbps' -> 1_000_000 kbps and apply to iface. Returns True if changed."""
    try:
        mbps      = int(speed_str.replace("Mbps", ""))
        speed_val = mbps * 1000  # NetBox stores speed in Kbps
        if iface.speed != speed_val:
            iface.speed = speed_val
            return True
    except ValueError:
        logger.warning("[SPEED] Could not parse speed %r for %s", speed_str, iface.name)
    return False


def _update_existing_interface(iface: Interface, pif: ParsedInterface) -> bool:
    """
    Apply changed fields from *pif* to an existing NetBox *iface*.
    Returns True if any scalar field changed (MAC is handled separately).
    """
    changed = False
    if_type = infer_interface_type(iface.name)

    if iface.type != if_type:
        iface.type = if_type
        changed = True

    if pif.description and iface.description != pif.description:
        iface.description = pif.description
        changed = True

    if pif.mtu and iface.mtu != pif.mtu:
        iface.mtu = pif.mtu
        changed = True

    if pif.speed:
        changed |= _apply_speed(iface, pif.speed)

    if pif.duplex and iface.duplex != pif.duplex:
        iface.duplex = pif.duplex
        changed = True

    if pif.admin_up is not None and iface.enabled != pif.admin_up:
        iface.enabled = pif.admin_up
        changed = True

    return changed


# Public API

def sync_interfaces_to_netbox(device_netbox, parsed: dict[str, ParsedInterface]) -> None:
    """
    Create or update Interface objects in NetBox from *parsed* device data.

    Ordering:
      1. Ensure LAG interfaces exist before touching members.
      2. Iterate parsed interfaces - skip VLAN SVIs.
      3. Update or create each interface.
      4. Assign MACs and sync IPs.
      5. Attach LAG memberships after all interfaces are persisted.
    """
    logger.info("[SYNC] Syncing %d interfaces for %s", len(parsed), device_netbox.name)

    _ensure_lags(device_netbox, parsed)

    existing: dict[str, Interface] = {
        str(i): i for i in Interface.objects.filter(device=device_netbox)
    }

    for name, pif in parsed.items():
        if name.lower().startswith("vlan"):
            continue

        logger.debug(
            "[SYNC] %s mac=%s mtu=%s speed=%s desc=%r",
            name, pif.mac, pif.mtu, pif.speed, pif.description,
        )

        if name in existing:
            iface   = existing[name]
            changed = _update_existing_interface(iface, pif)
            if changed:
                iface.save()
                logger.debug("[SYNC] %s: saved updated fields", name)
        else:
            if_type = infer_interface_type(name)
            logger.debug("[SYNC] Creating %r (type=%r)", name, if_type)
            iface = Interface(
                device=device_netbox,
                name=name,
                type=if_type,
                description=pif.description or "",
                enabled=pif.admin_up if pif.admin_up is not None else True,
                mtu=pif.mtu,
            )
            if pif.speed:
                _apply_speed(iface, pif.speed)
            if pif.duplex:
                iface.duplex = pif.duplex
            iface.save()

        # MAC
        if pif.mac:
            _assign_mac(iface, pif.mac)

        # IPs
        _sync_ips(device_netbox, iface, pif)

    # LAG attachments need all interfaces to exist first
    _attach_lags(device_netbox, parsed)

    logger.info("[SYNC] Finished syncing interfaces for %s", device_netbox.name)
