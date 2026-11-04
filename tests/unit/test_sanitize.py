from config_officer.collection.sanitize import redact_config
from config_officer.configuration import DEFAULT_SENSITIVE_PREFIXES
from tests.conftest import FIXTURES


def test_redacts_lines_and_whole_blocks():
    config = (FIXTURES / "iosxe" / "running_config.txt").read_text()
    redacted = redact_config(config, DEFAULT_SENSITIVE_PREFIXES)
    assert "secret 9" not in redacted
    assert "crypto pki" not in redacted
    assert "3082022B" not in redacted  # certificate body inside the crypto block
    assert "password 7" not in redacted  # indented line under "line vty"
    assert "line vty 0 4\n transport input ssh" in redacted
    assert "ntp server 1.1.1.1" in redacted


def test_no_prefixes_keeps_everything_else():
    assert redact_config("username a\n b", []) == "username a\n b"


def test_bgp_neighbor_passwords_are_always_removed():
    config = "router bgp 65001\n neighbor 10.0.0.2 remote-as 65002\n neighbor 10.0.0.2 password 7 104D000A0618"
    assert redact_config(config, []) == "router bgp 65001\n neighbor 10.0.0.2 remote-as 65002"
