"""BGP: sessions stored with the result, the device tab, and the optional netbox-bgp sync."""

from types import SimpleNamespace

import pytest
from dcim.models import Interface
from django.apps import apps
from ipam.models import ASN, RIR, VRF, IPAddress

from config_officer.collection.netbox_sync import sync_device
from config_officer.collection.parsers import parse_device_output
from config_officer.configuration import get_settings
from config_officer.models import Collection
from tests.conftest import FIXTURES
from tests.integration.helpers import run_job

needs_netbox_bgp = pytest.mark.skipif(
    not apps.is_installed("netbox_bgp"), reason="netbox-bgp is not installed"
)


def parsed_iosxe():
    read = lambda name: (FIXTURES / "iosxe" / name).read_text()  # noqa: E731
    output = SimpleNamespace(
        show_version=read("show_version.txt"),
        show_interfaces="",
        show_ip_interface="",
        running_config=read("running_config.txt"),
        show_bgp=read("show_bgp_all_summary.txt"),
    )
    return parse_device_output("iosxe", output, "10.0.0.1")


@pytest.mark.django_db
def test_sessions_are_stored_and_shown(admin_user, admin_client, make_device, fake_network):
    device = make_device("sw1", ip="10.0.0.1/24")
    job = run_job(admin_user, device_ids=(device.pk,))
    stored = Collection.objects.get(job=job).details["bgp"]
    assert (stored["local_as"], len(stored["sessions"])) == (65001, 5)

    tab = admin_client.get(f"/dcim/devices/{device.pk}/config-officer/bgp/").content.decode()
    assert "core1 uplink" in tab and "shutdown" in tab and "CUST" in tab
    from config_officer.storage import ConfigRepository

    assert "password 7" not in ConfigRepository.from_settings().read_config(
        "sw1"
    )  # BGP neighbor password redacted


@pytest.fixture
def router(make_device):
    """sw1 with the addresses BGP needs: a link to core1, a loopback and a VRF link."""
    device = make_device("sw1")
    cust = VRF.objects.create(name="CUST")
    for name, address, vrf in (
        ("GigabitEthernet1/0/1", "10.0.0.253/24", None),
        ("Loopback0", "10.255.0.1/32", None),
        ("GigabitEthernet1/0/2", "10.1.1.1/30", cust),
    ):
        interface = Interface.objects.create(device=device, name=name, type="virtual")
        IPAddress.objects.create(address=address, vrf=vrf, assigned_object=interface)
    RIR.objects.create(name="Private", slug="private", is_private=True)
    return device


@needs_netbox_bgp
@pytest.mark.django_db
def test_sync_creates_sessions(router, plugin_config):
    from netbox_bgp.models import BGPSession

    plugin_config(SYNC_BGP_SESSIONS=True, BGP_ASN_RIR="private")
    report = sync_device(router, parsed_iosxe(), "ssh", get_settings())

    assert sorted(report.bgp_sessions_created) == ["10.0.0.254", "10.1.1.2 (VRF CUST)", "192.0.2.2"]
    assert any(
        "10.9.9.9" in w and "local address unknown" in w for w in report.warnings
    )  # no IP in its subnet
    uplink = BGPSession.objects.get(remote_address__address__net_host="10.0.0.254")
    assert (str(uplink.local_address.address), uplink.local_as.asn, uplink.remote_as.asn) == (
        "10.0.0.253/24",
        65001,
        65000,
    )
    rr = BGPSession.objects.get(remote_address__address__net_host="192.0.2.2")
    assert (
        str(rr.local_address.address) == "10.255.0.1/32"
    )  # update-source Loopback0 (from the peer group)
    assert str(rr.remote_address.address) == "192.0.2.2/32"  # created for the peer
    customer = BGPSession.objects.get(remote_address__address__net_host="10.1.1.2")
    assert customer.remote_address.vrf.name == "CUST"

    again = sync_device(router, parsed_iosxe(), "ssh", get_settings())
    assert (again.bgp_sessions_created, again.bgp_sessions_updated) == ([], [])


@needs_netbox_bgp
@pytest.mark.django_db
def test_sync_needs_asns_or_a_rir(router, plugin_config):
    from netbox_bgp.models import BGPSession

    plugin_config(SYNC_BGP_SESSIONS=True)  # no BGP_ASN_RIR and no ASNs in NetBox
    report = sync_device(router, parsed_iosxe(), "ssh", get_settings())
    assert not BGPSession.objects.exists()
    assert any("AS65000 is not in NetBox" in w for w in report.warnings)
    assert not ASN.objects.exists()  # nothing half-created


@needs_netbox_bgp
@pytest.mark.django_db
def test_sync_updates_existing_sessions(router, plugin_config):
    from netbox_bgp.models import BGPSession

    rir = RIR.objects.get(slug="private")
    BGPSession.objects.create(
        device=router,
        local_address=IPAddress.objects.get(address="10.0.0.253/24"),
        remote_address=IPAddress.objects.create(address="10.0.0.254/24"),
        local_as=ASN.objects.create(asn=65001, rir=rir),
        remote_as=ASN.objects.create(asn=64999, rir=rir),  # wrong in NetBox
        status="planned",
    )
    plugin_config(SYNC_BGP_SESSIONS=True, BGP_ASN_RIR="private")
    report = sync_device(router, parsed_iosxe(), "ssh", get_settings())
    assert "10.0.0.254: remote AS, status, description" in report.bgp_sessions_updated
    session = BGPSession.objects.get(remote_address__address__net_host="10.0.0.254")
    assert (session.remote_as.asn, session.status, session.description) == (
        65000,
        "active",
        "core1 uplink",
    )
