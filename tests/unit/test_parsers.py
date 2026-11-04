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
