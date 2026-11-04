"""VLANs and 802.1Q interface modes from "show vlan brief" + "show interfaces switchport"."""

from types import SimpleNamespace

import pytest
from dcim.models import Interface
from ipam.models import VLAN, VLANGroup

from config_officer.collection.netbox_sync import sync_device
from config_officer.collection.parsers import parse_device_output
from config_officer.configuration import get_settings
from tests.conftest import FIXTURES


def parsed_iosxe():
    read = lambda name: (FIXTURES / "iosxe" / name).read_text()  # noqa: E731
    output = SimpleNamespace(
        show_version=read("show_version.txt"),
        show_interfaces="",
        show_ip_interface="",
        show_vlan=read("show_vlan_brief.txt"),
        show_switchport=read("show_interfaces_switchport.txt"),
    )
    return parse_device_output("iosxe", output, "10.0.0.1")


@pytest.fixture
def switch(make_device):
    device = make_device("sw1")
    for n in range(1, 5):
        Interface.objects.create(device=device, name=f"GigabitEthernet1/0/{n}", type="1000base-t")
    return device


def vids(vlans):
    return sorted(vlan.vid for vlan in vlans)


@pytest.mark.django_db
def test_vlans_and_modes(switch):
    report = sync_device(switch, parsed_iosxe(), "ssh", get_settings())
    assert vids(VLAN.objects.filter(site=switch.site)) == [1, 10, 20, 30]  # 1002-1005 skipped
    assert len(report.vlans_created) == 4

    ports = {i.name: i for i in Interface.objects.filter(device=switch)}
    trunk = ports["GigabitEthernet1/0/1"]
    assert (trunk.mode, trunk.untagged_vlan.vid, vids(trunk.tagged_vlans.all())) == (
        "tagged",
        1,
        [10, 20, 30],
    )
    voice = ports["GigabitEthernet1/0/2"]
    assert (voice.mode, voice.untagged_vlan.vid, vids(voice.tagged_vlans.all())) == (
        "tagged",
        10,
        [20],
    )
    trunk_all = ports["GigabitEthernet1/0/3"]
    assert (trunk_all.mode, trunk_all.untagged_vlan.vid) == ("tagged-all", 30)
    assert ports["GigabitEthernet1/0/4"].mode is None  # routed port

    again = sync_device(switch, parsed_iosxe(), "ssh", get_settings())
    assert (again.vlans_created, again.vlan_assignments) == ([], [])


@pytest.mark.django_db
def test_existing_vlans_are_reused(switch):
    group = VLANGroup.objects.create(name="Campus", slug="campus", scope=switch.site)
    users = VLAN.objects.create(group=group, vid=10, name="Users (curated)")
    sync_device(switch, parsed_iosxe(), "ssh", get_settings())
    assert not VLAN.objects.filter(site=switch.site, vid=10).exists()
    assert Interface.objects.get(device=switch, name="GigabitEthernet1/0/2").untagged_vlan == users
    users.refresh_from_db()
    assert users.name == "Users (curated)"  # never renamed


@pytest.mark.django_db
def test_can_be_disabled(switch, plugin_config):
    plugin_config(COLLECT_VLANS=False)
    sync_device(switch, parsed_iosxe(), "ssh", get_settings())
    assert not VLAN.objects.exists()
