"""CDP/LLDP neighbors: matching against NetBox, automatic and one-click cables."""

from types import SimpleNamespace

import pytest
from dcim.models import Cable, Interface
from django.contrib.auth import get_user_model

from config_officer.collection.neighbors import (
    CONNECTED,
    DIFFERENT_CABLE,
    NO_CABLE,
    NOT_IN_NETBOX,
    match_neighbors,
)
from config_officer.collection.netbox_sync import sync_device
from config_officer.collection.parsers import parse_device_output
from config_officer.configuration import get_settings
from tests.conftest import FIXTURES
from tests.integration.helpers import grant, run_job


def parsed_iosxe():
    read = lambda name: (FIXTURES / "iosxe" / name).read_text()  # noqa: E731
    output = SimpleNamespace(
        show_version=read("show_version.txt"),
        show_interfaces="",
        show_ip_interface="",
        show_cdp=read("show_cdp_neighbors_detail.txt"),
        show_lldp=read("show_lldp_neighbors_detail.txt"),
    )
    return parse_device_output("iosxe", output, "10.0.0.1")


@pytest.fixture
def topology(make_device):
    """sw1 Gi1/0/1-3 and core1 Te1/0/10-11 in NetBox; the phone and the AP are not."""
    sw1, core1 = make_device("sw1"), make_device("core1")
    for n in (1, 2, 3):
        Interface.objects.create(device=sw1, name=f"GigabitEthernet1/0/{n}", type="1000base-t")
    for n in (10, 11):
        Interface.objects.create(
            device=core1, name=f"TenGigabitEthernet1/0/{n}", type="10gbase-x-sfpp"
        )
    return sw1, core1


def statuses(device):
    from dataclasses import asdict

    return {
        m.neighbor["name"]: m.status
        for m in match_neighbors(device, [asdict(n) for n in parsed_iosxe().neighbors])
    }


@pytest.mark.django_db
def test_matching(topology):
    sw1, _ = topology
    assert statuses(sw1) == {
        "core1.example.com": NO_CABLE,  # found without the domain; both ports free
        "SEP001122334455": NOT_IN_NETBOX,
        "ap-01": NOT_IN_NETBOX,
    }


@pytest.mark.django_db
def test_cables_are_created_only_when_enabled(topology, plugin_config):
    sw1, core1 = topology
    assert sync_device(sw1, parsed_iosxe(), "ssh", get_settings()).cables_created == []
    assert not Cable.objects.exists()

    plugin_config(CREATE_CABLES=True)
    report = sync_device(sw1, parsed_iosxe(), "ssh", get_settings())
    assert report.cables_created == ["GigabitEthernet1/0/1 <-> core1 TenGigabitEthernet1/0/10"]
    assert statuses(sw1)["core1.example.com"] == CONNECTED
    assert (
        sync_device(sw1, parsed_iosxe(), "ssh", get_settings()).cables_created == []
    )  # idempotent


@pytest.mark.django_db
def test_existing_different_cable_is_left_alone(topology, plugin_config):
    sw1, core1 = topology
    wrong_peer = Interface.objects.get(device=core1, name="TenGigabitEthernet1/0/11")
    Cable(
        a_terminations=[Interface.objects.get(device=sw1, name="GigabitEthernet1/0/1")],
        b_terminations=[wrong_peer],
    ).save()
    plugin_config(CREATE_CABLES=True)
    assert statuses(sw1)["core1.example.com"] == DIFFERENT_CABLE
    assert sync_device(sw1, parsed_iosxe(), "ssh", get_settings()).cables_created == []
    assert Cable.objects.count() == 1


@pytest.mark.django_db
def test_neighbors_tab_and_add_cable_button(
    admin_user, admin_client, client, topology, fake_network
):
    sw1, core1 = topology
    from ipam.models import IPAddress

    mgmt = Interface.objects.create(device=sw1, name="GigabitEthernet0/0", type="1000base-t")
    sw1.primary_ip4 = IPAddress.objects.create(address="10.0.0.1/24", assigned_object=mgmt)
    sw1.save()
    run_job(admin_user, device_ids=(sw1.pk,))

    tab = admin_client.get(f"/dcim/devices/{sw1.pk}/config-officer/neighbors/").content.decode()
    assert "core1" in tab and "no cable" in tab and "Add cable" in tab

    local = Interface.objects.get(device=sw1, name="GigabitEthernet1/0/1")
    remote = Interface.objects.get(device=core1, name="TenGigabitEthernet1/0/10")
    url = f"/plugins/config_officer/devices/{sw1.pk}/neighbors/cable/"
    data = {"local_interface": local.pk, "remote_interface": remote.pk}

    user = get_user_model().objects.create_user("viewer")
    grant(user, "dcim.device", ["view"])
    grant(user, "config_officer.collection", ["view"])
    client.force_login(user)
    assert client.post(url, data).status_code == 403  # needs dcim.add_cable

    response = admin_client.post(url, data)
    assert response.status_code == 302
    local.refresh_from_db()
    assert local.cable is not None and remote in local.connected_endpoints
    assert admin_client.post(url, data).status_code == 302  # second click: refused politely
    assert Cable.objects.count() == 1
