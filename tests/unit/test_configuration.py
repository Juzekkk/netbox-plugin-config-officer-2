"""Settings parsing: defaults, PLUGINS_CONFIG, CO_* environment overrides, warnings."""

from config_officer.configuration import load_settings


def test_defaults():
    settings = load_settings({})
    assert settings.configs_repo_dir == "/device_configs"
    assert settings.device_ssh_port == 22
    assert settings.default_platform == "nxos"
    assert settings.git_remote.branch == "netbox"
    assert not settings.git_remote.is_configured
    assert settings.warnings == ()


def test_environment_overrides_plugins_config():
    settings = load_settings(
        {"DEVICE_SSH_PORT": 2222, "TELNET_FALLBACK": True},
        {
            "CO_DEVICE_SSH_PORT": "2200",
            "CO_TELNET_FALLBACK": "off",
            "CO_SENSITIVE_PREFIXES": "username, snmp",
        },
    )
    assert settings.device_ssh_port == 2200
    assert settings.telnet_fallback is False
    assert settings.sensitive_prefixes == ("username", "snmp")


def test_git_remote_enabled_is_read_from_the_dict():
    remote = {"URL": "git@example.com:configs.git", "ENABLED": False}
    assert not load_settings({"GIT_REMOTE": remote}).git_remote.is_configured
    assert load_settings({"GIT_REMOTE": {**remote, "ENABLED": True}}).git_remote.is_configured
    env = {"CO_GIT_REMOTE_ENABLED": "true"}
    assert load_settings({"GIT_REMOTE": remote}, env).git_remote.is_configured


def test_invalid_values_fall_back_to_defaults_with_a_warning():
    settings = load_settings(
        {"DEVICE_SSH_PORT": "abc", "COLLECTION_CONCURRENCY": 500, "DEFAULT_PLATFORM": "junos"}
    )
    assert settings.device_ssh_port == 22
    assert settings.collection_concurrency == 8
    assert settings.default_platform == "nxos"
    assert len(settings.warnings) == 3


def test_unknown_keys_are_reported():
    settings = load_settings({"NETBOX_DEVICES_CONFIGS_REPO_DIR": "/opt/x", "DEVICE_USERNAM": "x"})
    assert any(
        "NETBOX_DEVICES_CONFIGS_REPO_DIR" in w and "CONFIGS_REPO_DIR" in w
        for w in settings.warnings
    )
    assert any("DEVICE_USERNAM" in w for w in settings.warnings)


def test_invalid_regex_and_platform_map_entries_are_dropped():
    settings = load_settings(
        {
            "VOLATILE_LINE_PATTERNS": ["^ok", "(broken"],
            "PLATFORM_MAP": {"C9K": "IOSXE", "x": "junos"},
        }
    )
    assert settings.volatile_line_patterns == ("^ok",)
    assert settings.platform_map == {"c9k": "iosxe"}
    assert len(settings.warnings) == 2


def test_json_values_from_the_environment():
    env = {
        "CO_DEVICE_SPECIFIC_CONF": '{"sw1": {"DEVICE_USERNAME": "admin", "DEVICE_SSH_PORT": 2222}}'
    }
    settings = load_settings({}, env)
    credentials = settings.credentials_for("sw1")
    assert (credentials.username, credentials.port) == ("admin", 2222)
    assert settings.credentials_for("sw2").username == "cisco"
