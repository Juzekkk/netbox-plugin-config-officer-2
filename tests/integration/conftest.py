"""Fixtures for the integration tests (real NetBox, PostgreSQL and Redis; fake network)."""

from contextlib import contextmanager
from types import SimpleNamespace

import pytest
import utilities.testing.api
import utilities.testing.views
from dcim.models import Device, DeviceRole, DeviceType, Interface, Manufacturer, Platform, Site
from ipam.models import IPAddress

from config_officer.configuration import get_settings
from tests.conftest import FIXTURES


@contextmanager
def _skip_query_count_check(test_case, name):
    yield


# NetBox's generic test cases compare SQL query counts with baselines stored in
# <app>/tests/query_counts.json. Those numbers change with every NetBox release,
# so the plugin does not track them.
utilities.testing.api.assert_expected_query_count = _skip_query_count_check
utilities.testing.views.assert_expected_query_count = _skip_query_count_check

IOSXE_COMMANDS = {
    "show version": "show_version.txt",
    "show interfaces": "show_interfaces.txt",
    "show ip interface": "show_ip_interface.txt",
    "show running-config": "running_config.txt",
    "show vlan brief": "show_vlan_brief.txt",
    "show interfaces switchport": "show_interfaces_switchport.txt",
    "show inventory": "show_inventory.txt",
    "show cdp neighbors detail": "show_cdp_neighbors_detail.txt",
    "show lldp neighbors detail": "show_lldp_neighbors_detail.txt",
}


@pytest.fixture(autouse=True)
def plugin_config(settings, tmp_path):
    """Gives every test its own configs repository. Call it to change plugin settings."""

    def configure(**values):
        config = {"CONFIGS_REPO_DIR": str(tmp_path / "configs"), **values}
        settings.PLUGINS_CONFIG = {**settings.PLUGINS_CONFIG, "config_officer": config}
        get_settings.cache_clear()
        return get_settings()

    configure()
    yield configure
    get_settings.cache_clear()


class FakeNetwork:
    """Stands in for the devices: answers CLI commands from tests/fixtures/iosxe."""

    def __init__(self):
        self.unreachable: set[str] = set()
        self.overrides: dict[str, str] = {}  # command -> output, for all hosts

    def port_is_open(self, host, port, timeout):
        return (False, "connection refused") if host in self.unreachable else (True, "port open")

    def open_connection(self, target, transport, port, settings):
        network = self

        class Connection:
            def send_command(self, command):
                if command in network.overrides:
                    return SimpleNamespace(result=network.overrides[command], failed=False)
                if command not in IOSXE_COMMANDS:  # like a real device: unknown command
                    return SimpleNamespace(
                        result="% Invalid input detected at '^' marker.", failed=True
                    )
                text = (FIXTURES / "iosxe" / IOSXE_COMMANDS[command]).read_text()
                return SimpleNamespace(result=text, failed=False)

            def close(self):
                pass

        return Connection()


@pytest.fixture
def fake_network(monkeypatch):
    network = FakeNetwork()
    monkeypatch.setattr("config_officer.collection.connection.port_is_open", network.port_is_open)
    monkeypatch.setattr(
        "config_officer.collection.connection.open_connection", network.open_connection
    )
    return network


@pytest.fixture
def make_device(db):
    """Creates a device; with ``ip`` it also gets GigabitEthernet0/0 and that primary IPv4."""

    def make(name, ip=None, platform="cisco-ios-xe", **attrs):
        site, _ = Site.objects.get_or_create(name="Site 1", slug="site-1")
        manufacturer, _ = Manufacturer.objects.get_or_create(name="Cisco", slug="cisco")
        device_type, _ = DeviceType.objects.get_or_create(
            manufacturer=manufacturer, model="C9300", slug="c9300"
        )
        role, _ = DeviceRole.objects.get_or_create(name="Access", slug="access")
        if platform:
            attrs["platform"], _ = Platform.objects.get_or_create(name=platform, slug=platform)
        device = Device.objects.create(
            name=name, site=site, device_type=device_type, role=role, **attrs
        )
        if ip:
            interface = Interface.objects.create(
                device=device, name="GigabitEthernet0/0", type="1000base-t"
            )
            device.primary_ip4 = IPAddress.objects.create(address=ip, assigned_object=interface)
            device.save()
        return device

    return make
