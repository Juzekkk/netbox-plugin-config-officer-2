"""CLI parsers against captured device output (tests/fixtures)."""

from types import SimpleNamespace

from config_officer.collection.parsers import parse_device_output
from tests.conftest import FIXTURES


def output(platform: str, **files) -> SimpleNamespace:
    def read(name):
        return (FIXTURES / platform / name).read_text() if name else ""

    return SimpleNamespace(**{key: read(name) for key, name in files.items()})


def test_iosxe():
    raw = output(
        "iosxe",
        show_version="show_version.txt",
        show_interfaces="show_interfaces.txt",
        show_ip_interface="show_ip_interface.txt",
    )
    device = parse_device_output("iosxe", raw, mgmt_ip="10.0.0.1")
    assert (device.hostname, device.version, device.pid, device.serial) == (
        "sw1",
        "17.09.04a",
        "C9300-48P",
        "FOC12345ABC",
    )

    mgmt = device.interfaces["GigabitEthernet0/0"]
    assert (mgmt.ip, mgmt.vrf, mgmt.mac, mgmt.mtu) == (
        "10.0.0.1/24",
        "Mgmt-vrf",
        "00aa.bbcc.dd00",
        1500,
    )
    assert (mgmt.speed, mgmt.duplex, mgmt.is_mgmt) == ("1000Mbps", "full", True)

    uplink = device.interfaces["GigabitEthernet1/0/1"]
    assert (uplink.description, uplink.mtu, uplink.speed, uplink.duplex) == (
        "uplink to core",
        9000,
        "1000Mbps",
        "full",
    )
    assert device.interfaces["GigabitEthernet1/0/2"].admin_up is False
    assert device.interfaces["Loopback0"].ip == "192.0.2.1/32"


def test_nxos():
    raw = output("nxos", show_version="show_version.txt", show_interfaces="show_interface.txt")
    device = parse_device_output("nxos", raw, mgmt_ip="10.0.1.1")
    assert (device.hostname, device.version, device.serial) == ("nx1", "9.3(8)", "FDO12345678")
    assert device.pid == "Nexus9000 C93180YC-EX"

    uplink = device.interfaces["Ethernet1/1"]
    assert (uplink.lag, uplink.description, uplink.mtu) == ("port-channel10", "to-spine1", 9216)
    assert (uplink.speed, uplink.duplex, uplink.mac) == ("10000Mbps", "full", "00aa.bbcc.0101")
    assert device.interfaces["Ethernet1/2"].admin_up is False
    assert device.interfaces["mgmt0"].ip == "10.0.1.1/24"
    assert device.interfaces["mgmt0"].is_mgmt


def test_interface_data_is_optional():
    raw = output(
        "iosxe", show_version="show_version.txt", show_interfaces=None, show_ip_interface=None
    )
    device = parse_device_output("iosxe", raw, mgmt_ip="10.0.0.1")
    assert device.serial == "FOC12345ABC"
    assert device.interfaces == {}


# ---------------------------------------------------------------------------
# VLANs and switchports
# ---------------------------------------------------------------------------

from config_officer.collection.parsers import (  # noqa: E402
    expand_interface_name,
    parse_switchport,
    parse_vlan_brief,
    parse_vlan_list,
)


def test_expand_interface_name():
    assert expand_interface_name("Gi1/0/1") == "GigabitEthernet1/0/1"
    assert expand_interface_name("Te1/1/4") == "TenGigabitEthernet1/1/4"
    assert expand_interface_name("Twe1/0/1") == "TwentyFiveGigE1/0/1"
    assert expand_interface_name("Eth1/49") == "Ethernet1/49"
    assert expand_interface_name("Po10") == "Port-channel10"
    assert expand_interface_name("GigabitEthernet1/0/1") == "GigabitEthernet1/0/1"
    assert expand_interface_name("mgmt0") == "mgmt0"


def test_parse_vlan_list():
    assert parse_vlan_list("10,20,30-32") == [10, 20, 30, 31, 32]
    assert parse_vlan_list("ALL") is None
    assert parse_vlan_list("1-4094") is None
    assert parse_vlan_list("NONE") == []


def test_vlans_and_switchports_iosxe():
    vlans = parse_vlan_brief((FIXTURES / "iosxe" / "show_vlan_brief.txt").read_text())
    assert sorted(vlans) == [1, 10, 20, 30, 1002, 1003, 1004, 1005]
    assert vlans[10].name == "USERS"

    ports = parse_switchport((FIXTURES / "iosxe" / "show_interfaces_switchport.txt").read_text())
    trunk = ports["GigabitEthernet1/0/1"]
    assert (trunk.mode, trunk.native_vlan, trunk.trunk_vlans) == (
        "trunk",
        1,
        [1, 10, 20, 30],
    )  # wrapped list
    access = ports["GigabitEthernet1/0/2"]
    assert (access.mode, access.access_vlan, access.voice_vlan) == ("access", 10, 20)
    dynamic = ports["GigabitEthernet1/0/3"]
    assert (dynamic.mode, dynamic.native_vlan, dynamic.trunk_vlans) == ("trunk", 30, None)
    routed = ports["GigabitEthernet1/0/4"]
    assert (routed.enabled, routed.mode) == (False, None)


def test_vlans_and_switchports_nxos():
    assert sorted(parse_vlan_brief((FIXTURES / "nxos" / "show_vlan_brief.txt").read_text())) == [
        1,
        10,
        20,
    ]
    ports = parse_switchport((FIXTURES / "nxos" / "show_interface_switchport.txt").read_text())
    assert (ports["Ethernet1/1"].mode, ports["Ethernet1/1"].trunk_vlans) == (
        "trunk",
        None,
    )  # 1-4094 = all
    assert (ports["Ethernet1/5"].mode, ports["Ethernet1/5"].access_vlan) == ("access", 10)
    assert (ports["Ethernet1/6"].native_vlan, ports["Ethernet1/6"].trunk_vlans) == (10, [10, 20])


# ---------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------

from config_officer.collection.parsers import parse_inventory  # noqa: E402


def test_inventory_iosxe():
    items = parse_inventory((FIXTURES / "iosxe" / "show_inventory.txt").read_text())
    assert [i.name for i in items][:3] == ["c93xx Stack", "Switch 1", "Switch 1 - Power Supply A"]
    psu = items[2]
    assert (psu.pid, psu.vid, psu.serial) == ("PWR-C1-715WAC", "V01", "LIT12345678")
    sfp = items[-1]
    assert (sfp.name, sfp.description, sfp.pid) == (
        "GigabitEthernet1/0/1",
        "1000BaseSX SFP",
        "GLC-SX-MMD",
    )


def test_inventory_nxos():
    items = {
        i.name: i for i in parse_inventory((FIXTURES / "nxos" / "show_inventory.txt").read_text())
    }
    assert len(items) == 5
    assert items["Fan 1"].serial == ""  # "N/A"
    assert items["Ethernet1/49"].pid == "QSFP-40G-SR4"


# ---------------------------------------------------------------------------
# CDP / LLDP neighbors
# ---------------------------------------------------------------------------

from config_officer.collection.parsers import (  # noqa: E402
    merge_neighbors,
    parse_cdp_neighbors,
    parse_lldp_neighbors,
)


def neighbors(platform):
    cdp = parse_cdp_neighbors((FIXTURES / platform / "show_cdp_neighbors_detail.txt").read_text())
    lldp = parse_lldp_neighbors(
        (FIXTURES / platform / "show_lldp_neighbors_detail.txt").read_text()
    )
    return cdp, lldp


def test_neighbors_iosxe():
    cdp, lldp = neighbors("iosxe")
    core = cdp[0]
    assert (core.local_interface, core.name, core.interface) == (
        "GigabitEthernet1/0/1",
        "core1.example.com",
        "TenGigabitEthernet1/0/10",
    )
    assert (core.platform, core.ip, core.protocol) == ("cisco C9500-48Y4C", "10.0.0.254", "cdp")
    assert lldp[0].local_interface == "GigabitEthernet1/0/1"  # "Gi1/0/1" expanded
    assert lldp[1].interface == "eth0"  # non-Cisco port names are kept as advertised

    merged = merge_neighbors(cdp, lldp)  # core1 is seen through CDP and LLDP: one entry, from CDP
    assert [(n.name, n.protocol) for n in merged] == [
        ("core1.example.com", "cdp"),
        ("SEP001122334455", "cdp"),
        ("ap-01", "lldp"),
    ]


def test_neighbors_nxos():
    cdp, lldp = neighbors("nxos")
    assert (cdp[0].name, cdp[0].local_interface, cdp[0].interface) == (
        "spine1",
        "Ethernet1/1",
        "Ethernet1/49",
    )
    assert (lldp[0].name, lldp[0].local_interface, lldp[0].ip) == (
        "spine2",
        "Ethernet1/2",
        "10.0.1.253",
    )
