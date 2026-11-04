"""Inventory items from "show inventory"."""

from types import SimpleNamespace

import pytest
from dcim.models import Interface, InventoryItem

from config_officer.collection.netbox_sync import sync_device
from config_officer.collection.parsers import parse_device_output
from config_officer.configuration import get_settings
from tests.conftest import FIXTURES


def parsed(inventory_text):
    output = SimpleNamespace(
        show_version=(FIXTURES / "iosxe" / "show_version.txt").read_text(),
        show_interfaces="",
        show_ip_interface="",
        show_inventory=inventory_text,
    )
    return parse_device_output("iosxe", output, "10.0.0.1")


INVENTORY = (FIXTURES / "iosxe" / "show_inventory.txt").read_text()


@pytest.mark.django_db
def test_inventory_items(make_device):
    device = make_device("sw1")
    sfp_port = Interface.objects.create(
        device=device, name="GigabitEthernet1/0/1", type="1000base-x-sfp"
    )
    manual = InventoryItem.objects.create(device=device, name="Asset sticker")  # created by hand

    report = sync_device(device, parsed(INVENTORY), "ssh", get_settings())
    items = {i.name: i for i in InventoryItem.objects.filter(device=device)}
    assert len(report.inventory_created) == 5
    psu = items["Switch 1 - Power Supply A"]
    assert (psu.part_id, psu.serial, psu.discovered) == ("PWR-C1-715WAC", "LIT12345678", True)
    assert items["GigabitEthernet1/0/1"].component == sfp_port  # transceiver linked to its port

    # The power supply is replaced and the uplink module is removed.
    blocks = INVENTORY.strip().split("\n\n")
    changed = "\n\n".join(b for b in blocks if "Uplink Module" not in b).replace(
        "LIT12345678", "LIT99999999"
    )
    report = sync_device(device, parsed(changed), "ssh", get_settings())
    assert report.inventory_updated == ["Switch 1 - Power Supply A (serial)"]
    assert report.inventory_removed == ["Switch 1 FRU Uplink Module 1"]
    assert InventoryItem.objects.filter(pk=manual.pk).exists()  # created by hand: never deleted

    again = sync_device(device, parsed(changed), "ssh", get_settings())
    assert (again.inventory_created, again.inventory_updated, again.inventory_removed) == (
        [],
        [],
        [],
    )
