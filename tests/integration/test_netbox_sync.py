"""Edge cases of writing device data into NetBox (collection/netbox_sync.py)."""

import pytest
from core.models import ObjectType
from dcim.models import Device, Interface, MACAddress
from extras.models import CustomField
from ipam.models import VRF, IPAddress

from config_officer.collection.netbox_sync import sync_device
from config_officer.collection.parsers import ParsedDevice, ParsedInterface
from config_officer.configuration import get_settings


def parsed(*interfaces, serial="FOC1", version="17.9") -> ParsedDevice:
    return ParsedDevice(serial=serial, version=version, interfaces={i.name: i for i in interfaces})


@pytest.mark.django_db
def test_existing_interface_type_is_not_overwritten(make_device):
    device = make_device("sw1")
    Interface.objects.create(
        device=device, name="GigabitEthernet1/0/1", type="1000base-x-sfp", label="uplink"
    )
    report = sync_device(
        device, parsed(ParsedInterface("GigabitEthernet1/0/1", mtu=9000)), "ssh", get_settings()
    )
    interface = Interface.objects.get(device=device, name="GigabitEthernet1/0/1")
    assert (interface.type, interface.mtu) == ("1000base-x-sfp", 9000)  # matched despite the label
    assert report.interfaces_updated == {"GigabitEthernet1/0/1": ["mtu"]}
    assert report.interfaces_created == []


@pytest.mark.django_db
def test_mac_is_assigned_and_legacy_records_are_repaired(make_device):
    device = make_device("sw1")
    interface = Interface.objects.create(device=device, name="Gi1", type="1000base-t")
    legacy = MACAddress.objects.create(mac_address="00:AA:BB:CC:DD:01")  # 0.1.x: never assigned
    interface.primary_mac_address = legacy
    interface.save()

    sync_device(device, parsed(ParsedInterface("Gi1", mac="00aa.bbcc.dd01")), "ssh", get_settings())
    legacy.refresh_from_db()
    assert legacy.assigned_object == interface

    sync_device(device, parsed(ParsedInterface("Gi1", mac="00aa.bbcc.dd02")), "ssh", get_settings())
    interface.refresh_from_db()
    assert str(interface.primary_mac_address.mac_address) == "00:AA:BB:CC:DD:02"
    assert interface.primary_mac_address.assigned_object == interface


@pytest.mark.django_db
def test_ip_addresses(make_device):
    device = make_device("sw1", ip="10.0.0.1/24")  # global VRF in NetBox
    unrelated = IPAddress.objects.create(
        address="10.9.9.9/24"
    )  # same host elsewhere, other VRF below
    mgmt = ParsedInterface("GigabitEthernet0/0", ip="10.0.0.1/24", vrf="Mgmt-vrf")
    loopback = ParsedInterface("Loopback0", ip="192.0.2.1/32", secondary=["192.0.2.2/32"])
    other = ParsedInterface("Gi2", ip="10.9.9.9/24", vrf="Other")

    sync_device(device, parsed(mgmt, loopback, other), "ssh", get_settings())

    moved = IPAddress.objects.get(address__net_host="10.0.0.1")  # no duplicate created
    assert moved.vrf.name == "Mgmt-vrf"
    assert IPAddress.objects.get(address="192.0.2.1/32").role == "loopback"
    assert IPAddress.objects.get(address="192.0.2.2/32").role == "secondary"
    unrelated.refresh_from_db()
    assert unrelated.assigned_object is None  # not taken over
    assert IPAddress.objects.filter(address__net_host="10.9.9.9", vrf__name="Other").exists()
    assert VRF.objects.filter(name__in=["Mgmt-vrf", "Other"]).count() == 2


@pytest.mark.django_db
def test_lags(make_device, plugin_config):
    device = make_device("sw1")
    members = (
        ParsedInterface("Ethernet1/1", lag="port-channel10"),
        ParsedInterface("Ethernet1/2", lag="port-channel10"),
    )
    report = sync_device(device, parsed(*members), "ssh", get_settings())
    lag = Interface.objects.get(device=device, name="port-channel10")
    assert lag.type == "lag"
    assert set(lag.member_interfaces.values_list("name", flat=True)) == {
        "Ethernet1/1",
        "Ethernet1/2",
    }
    assert len(report.lag_members) == 2

    other = make_device("sw2")
    plugin_config(COLLECT_PORT_CHANNEL_DATA=False)
    sync_device(other, parsed(*members), "ssh", get_settings())
    assert not Interface.objects.filter(device=other, name="port-channel10").exists()


@pytest.mark.django_db
def test_only_existing_custom_fields_are_written(make_device):
    field = CustomField.objects.create(name="version", type="text")
    field.object_types.set([ObjectType.objects.get_for_model(Device)])
    device = make_device("sw1")
    report = sync_device(device, parsed(), "ssh", get_settings())
    device.refresh_from_db()
    assert device.custom_field_data == {"version": "17.9"}
    assert report.custom_fields == {"version": "17.9"}
