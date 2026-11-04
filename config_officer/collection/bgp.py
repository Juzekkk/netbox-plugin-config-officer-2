"""BGP sessions compared with (and optionally written to) the netbox-bgp plugin.

The sessions of a device (``parsers.parse_bgp``: running config + summary) are
stored with its collection result (``Collection.details["bgp"]``). This module:

* :func:`match_sessions` pairs them with the device's netbox-bgp sessions for the
  device's "BGP" tab and lists NetBox sessions the device does not have;
* :func:`sync_sessions` (setting ``SYNC_BGP_SESSIONS``, off by default) creates
  missing netbox-bgp sessions and updates the remote/local AS, the status
  (``shutdown`` -> offline, otherwise active) and the description of existing
  ones. Sessions are never deleted.

Creating a session needs, in NetBox:

* both ASNs - missing ones are only created when ``BGP_ASN_RIR`` names the RIR to use;
* the local address, in this order: the IP of the ``update-source`` interface;
  the "Local host" the device reports (created as /32 or /128 when NetBox does
  not have it yet); a device IP in the peer's subnet;
* the remote address - created as /32 (/128) in the session's VRF when missing.

When a session still cannot be written (e.g. it is down, so the device reports no
local address), the peer's IP is created anyway, with a description naming the
device, AS and neighbor description, so NetBox keeps a trace of it.

netbox-bgp is imported lazily: without it the tab still shows the sessions.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

import netaddr
from django.apps import apps
from django.core.exceptions import ValidationError
from django.db import transaction
from ipam.models import ASN, RIR, VRF, IPAddress

from config_officer.configuration import PluginSettings

from .parsers import ParsedBgp, ParsedBgpSession

ESTABLISHED = "Established"


def netbox_bgp_installed() -> bool:
    return apps.is_installed("netbox_bgp")


@dataclass
class SessionMatch:
    session: dict  # as stored: asdict(ParsedBgpSession)
    netbox: object | None = None  # netbox_bgp BGPSession
    differences: list[str] = field(default_factory=list)

    @property
    def state_color(self) -> str:
        if self.session["shutdown"] or not self.session["state"]:
            return "gray"
        return "green" if self.session["state"] == ESTABLISHED else "red"


def _expected_status(session: dict) -> str:
    from netbox_bgp.choices import SessionStatusChoices

    return (
        SessionStatusChoices.STATUS_OFFLINE
        if session["shutdown"]
        else SessionStatusChoices.STATUS_ACTIVE
    )


def find_netbox_session(device, session: dict):
    """The device's netbox-bgp session to this peer (same VRF preferred), or None."""
    from netbox_bgp.models import BGPSession

    candidates = list(
        BGPSession.objects.filter(
            device=device, remote_address__address__net_host=session["peer"]
        ).select_related("remote_address__vrf", "remote_as", "local_as")
    )
    for candidate in candidates:
        vrf = candidate.remote_address.vrf
        if (vrf.name if vrf else "").lower() == session["vrf"].lower():
            return candidate
    return candidates[0] if len(candidates) == 1 else None


def _differences(session: dict, netbox) -> list[str]:
    differences = []
    if session["remote_as"] and netbox.remote_as.asn != session["remote_as"]:
        differences.append(f"remote AS in NetBox: {netbox.remote_as.asn}")
    if netbox.status != _expected_status(session):
        differences.append(f"status in NetBox: {netbox.get_status_display()}")
    return differences


def match_sessions(device, sessions: list[dict]) -> tuple[list[SessionMatch], list]:
    """(one match per device session, netbox-bgp sessions of the device that it does not have)."""
    if not netbox_bgp_installed():
        return [SessionMatch(session) for session in sessions], []
    from netbox_bgp.models import BGPSession

    matches, matched = [], set()
    for session in sessions:
        netbox = find_netbox_session(device, session)
        if netbox is None:
            matches.append(SessionMatch(session))
        else:
            matched.add(netbox.pk)
            matches.append(SessionMatch(session, netbox, _differences(session, netbox)))
    only_in_netbox = BGPSession.objects.filter(device=device).exclude(pk__in=matched)
    return matches, list(only_in_netbox.select_related("remote_address", "remote_as"))


# ---------------------------------------------------------------------------
# Writing sessions
# ---------------------------------------------------------------------------


class _Skip(Exception):  # noqa: N818 - not an error: the session is reported and skipped
    """The session cannot be written; the message says why."""


def _get_asn(number: int, settings: PluginSettings) -> ASN:
    asn = ASN.objects.filter(asn=number).first()
    if asn is not None:
        return asn
    if not settings.bgp_asn_rir:
        raise _Skip(f"AS{number} is not in NetBox (set BGP_ASN_RIR to create missing ASNs)")
    rir = RIR.objects.filter(slug=settings.bgp_asn_rir).first()
    if rir is None:
        raise _Skip(f"RIR {settings.bgp_asn_rir!r} (BGP_ASN_RIR) does not exist")
    asn = ASN(asn=number, rir=rir, description="Discovered by Config Officer")
    asn.full_clean()
    asn.save()
    return asn


def _get_vrf(name: str):
    if not name:
        return None
    vrf = VRF.objects.filter(name__iexact=name).first()
    if vrf is None:
        raise _Skip(f"VRF {name} is not in NetBox")
    return vrf


def _create_ip(host: str, vrf, device, description: str) -> IPAddress:
    """A host address (/32 or /128) NetBox does not know yet."""
    length = 32 if netaddr.IPAddress(host).version == 4 else 128
    ip = IPAddress(
        address=netaddr.IPNetwork(f"{host}/{length}"),
        vrf=vrf,
        tenant=device.tenant,
        description=description[:200],
    )
    ip.save()
    return ip


def _local_address(device, session: ParsedBgpSession, vrf) -> tuple[IPAddress, bool]:
    """(local address, created?) - see the module docstring for the order."""
    device_ips = IPAddress.objects.filter(interface__device=device)
    if session.update_source:
        ip = device_ips.filter(interface__name__iexact=session.update_source).first()
        if ip is not None:
            return ip, False
    if session.local_address:
        ip = (
            device_ips.filter(address__net_host=session.local_address).first()
            or IPAddress.objects.filter(address__net_host=session.local_address, vrf=vrf).first()
        )
        if ip is not None:
            return ip, False
        return _create_ip(
            session.local_address,
            vrf,
            device,
            f"BGP local address of {device.name} (Config Officer)",
        ), True
    peer = netaddr.IPAddress(session.peer)
    for candidates in (device_ips.filter(vrf=vrf), device_ips):  # same VRF first
        for ip in candidates:
            if ip.address.version == peer.version and peer in ip.address:
                return ip, False
    raise _Skip(
        "local address unknown - the device reports none (session down?) and no device IP is in the peer's subnet"
    )


def _peer_description(device, session: ParsedBgpSession) -> str:
    text = f"BGP peer of {device.name}"
    if session.remote_as:
        text += f" (AS{session.remote_as})"
    if session.description:
        text += f": {session.description}"
    return f"{text} - Config Officer"


def _remote_address(device, session: ParsedBgpSession, vrf) -> tuple[IPAddress, bool]:
    ip = IPAddress.objects.filter(address__net_host=session.peer, vrf=vrf).first()
    if ip is not None:
        return ip, False
    return _create_ip(session.peer, vrf, device, _peer_description(device, session)), True


def _sync_session(
    device, session: ParsedBgpSession, local_as_number, settings
) -> tuple[str, list[str], list[str]]:
    """Returns (created | updated | unchanged, changed fields, IP addresses created)."""
    from netbox_bgp.models import BGPSession

    if session.remote_as is None:
        raise _Skip("remote AS unknown")
    if not (session.local_as or local_as_number):
        raise _Skip("local AS unknown")
    vrf = _get_vrf(session.vrf)
    remote_as = _get_asn(session.remote_as, settings)
    local_as = _get_asn(session.local_as or local_as_number, settings)
    status = _expected_status(asdict(session))
    description = session.description[:200]

    existing = find_netbox_session(device, asdict(session))
    if existing is None:
        local_ip, local_created = _local_address(device, session, vrf)
        remote_ip, remote_created = _remote_address(device, session, vrf)
        created = BGPSession(
            name=session.description or session.peer,
            device=device,
            site=device.site,
            tenant=device.tenant,
            local_address=local_ip,
            remote_address=remote_ip,
            local_as=local_as,
            remote_as=remote_as,
            status=status,
            description=description,
        )
        created.full_clean()
        created.save()
        new_ips = [
            f"{ip.address} (BGP {role})"
            for ip, new, role in (
                (local_ip, local_created, "local"),
                (remote_ip, remote_created, "peer"),
            )
            if new
        ]
        return "created", [], new_ips

    existing.snapshot()
    changes = []
    for attr, value, label in (
        ("remote_as", remote_as, "remote AS"),
        ("local_as", local_as, "local AS"),
        ("status", status, "status"),
    ):
        if getattr(existing, attr) != value:
            setattr(existing, attr, value)
            changes.append(label)
    if description and existing.description != description:
        existing.description = description
        changes.append("description")
    if changes:
        existing.full_clean()
        existing.save()
    return ("updated" if changes else "unchanged"), changes, []


def _record_peer(device, session: ParsedBgpSession) -> str:
    """Create the peer's IP (if missing) so NetBox keeps a trace of a session it does not have."""
    try:
        with transaction.atomic():
            ip, created = _remote_address(device, session, _get_vrf(session.vrf))
    except (_Skip, ValidationError):
        return ""
    return str(ip.address) if created else ""


def sync_sessions(device, bgp: ParsedBgp, settings: PluginSettings, report) -> None:
    """Create / update the device's netbox-bgp sessions; problems become report warnings."""
    if not netbox_bgp_installed():
        report.warnings.append(
            "SYNC_BGP_SESSIONS is enabled, but the netbox-bgp plugin is not installed"
        )
        return
    for session in bgp.sessions:
        label = session.peer + (f" (VRF {session.vrf})" if session.vrf else "")
        try:
            with transaction.atomic():  # a skipped session leaves no half-created ASN or IP behind
                result, changes, new_ips = _sync_session(device, session, bgp.local_as, settings)
        except (_Skip, ValidationError) as exc:
            reason = "; ".join(exc.messages) if isinstance(exc, ValidationError) else str(exc)
            recorded = _record_peer(device, session)
            if recorded:
                report.ip_addresses_created.append(f"{recorded} (BGP peer, no session)")
                reason += f"; peer IP {recorded} recorded in NetBox"
            report.warnings.append(f"BGP session {label} not created: {reason}")
            continue
        report.ip_addresses_created.extend(new_ips)
        if result == "created":
            report.bgp_sessions_created.append(label)
        elif result == "updated":
            report.bgp_sessions_updated.append(f"{label}: {', '.join(changes)}")
