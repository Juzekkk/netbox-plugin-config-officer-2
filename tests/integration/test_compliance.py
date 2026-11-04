"""Compliance engine: which templates apply, and the verdict."""

from datetime import timedelta

import pytest
from django.utils import timezone

from config_officer.compliance.engine import evaluate_device, merge_templates, templates_for_device
from config_officer.configuration import get_settings
from config_officer.models import Service, ServiceMapping, ServiceRule, Template


@pytest.fixture
def setup(make_device):
    device = make_device("sw1")
    service = Service.objects.create(name="base")
    ntp = Template.objects.create(name="ntp", configuration="ntp server {{ ip }}")
    snmp = Template.objects.create(name="snmp", configuration="snmp-server location {{ x }}")
    ServiceRule.objects.create(service=service, template=ntp)  # any role / type
    other_role_rule = ServiceRule.objects.create(service=service, template=snmp)
    other_role_rule.device_roles.create(name="Core", slug="core")  # does not match "access"
    ServiceMapping.objects.create(device=device, service=service)
    return device


@pytest.mark.django_db
def test_rules_filter_by_role(setup):
    assert [t.name for t in templates_for_device(setup)] == ["ntp"]


def test_merge_combines_sections():
    a = Template(configuration="interface Lo0\n description a\nntp server 1.1.1.1")
    b = Template(configuration="interface Lo0\n ip address 1.1.1.1 255.255.255.255\n description a")
    assert (
        merge_templates([a, b])
        == "interface Lo0\n description a\n ip address 1.1.1.1 255.255.255.255\nntp server 1.1.1.1"
    )


@pytest.mark.django_db
def test_verdicts(setup):
    settings, now = get_settings(), timezone.now()
    compliant = evaluate_device(setup, "hostname sw1\nntp server 1.1.1.1", now, settings)
    assert (compliant.status, compliant.diff) == ("compliance", "")

    missing = evaluate_device(setup, "hostname sw1", now, settings)
    assert (missing.status, missing.diff) == ("non-compliance", "ntp server {{ ip }}")

    stale = evaluate_device(setup, "ntp server 1.1.1.1", now - timedelta(days=30), settings)
    assert stale.status == "non-compliance" and "stale" in stale.notes

    absent = evaluate_device(setup, None, None, settings)
    assert "not found" in absent.notes


@pytest.mark.django_db
def test_devices_without_services_are_skipped(make_device):
    assert evaluate_device(make_device("sw9"), "x", timezone.now(), get_settings()) is None
