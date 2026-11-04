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
* the local address - the IP of the ``update-source`` interface, or a device IP
  in the peer's subnet;
* the remote address - created as a /32 (/128) in the session's VRF when missing.

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


def _local_address(device, session: ParsedBgpSession, vrf) -> IPAddress:
    device_ips = IPAddress.objects.filter(interface__device=device)
    if session.update_source:
        ip = device_ips.filter(interface__name__iexact=session.update_source).first()
        if ip is not None:
            return ip
    peer = netaddr.IPAddress(session.peer)
    for ip in device_ips.filter(vrf=vrf):
        if ip.address.version == peer.version and peer in ip.address:
            return ip
    raise _Skip("local address unknown (no update-source IP and no device IP in the peer's subnet)")


def _remote_address(device, session: ParsedBgpSession, vrf) -> IPAddress:
    ip = IPAddress.objects.filter(address__net_host=session.peer, vrf=vrf).first()
    if ip is None:
        length = 32 if netaddr.IPAddress(session.peer).version == 4 else 128
        ip = IPAddress(
            address=netaddr.IPNetwork(f"{session.peer}/{length}"),
            vrf=vrf,
            tenant=device.tenant,
            description=f"BGP peer of {device.name} (Config Officer)",
        )
        ip.save()
    return ip


def _sync_session(
    device, session: ParsedBgpSession, local_as_number, settings
) -> tuple[str, list[str]]:
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
        created = BGPSession(
            name=session.description or session.peer,
            device=device,
            site=device.site,
            tenant=device.tenant,
            local_address=_local_address(device, session, vrf),
            remote_address=_remote_address(device, session, vrf),
            local_as=local_as,
            remote_as=remote_as,
            status=status,
            description=description,
        )
        created.full_clean()
        created.save()
        return "created", []

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
    return ("updated" if changes else "unchanged"), changes


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
                result, changes = _sync_session(device, session, bgp.local_as, settings)
        except _Skip as exc:
            report.warnings.append(f"BGP session {label} not synced: {exc}")
        except ValidationError as exc:
            report.warnings.append(f"BGP session {label} not synced: {'; '.join(exc.messages)}")
        else:
            if result == "created":
                report.bgp_sessions_created.append(label)
            elif result == "updated":
                report.bgp_sessions_updated.append(f"{label}: {', '.join(changes)}")
