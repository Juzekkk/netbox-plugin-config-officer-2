"""A whole collection run: devices -> config files, git, NetBox objects, results and job log."""

import pytest
from core.choices import JobStatusChoices
from core.models import ObjectChange
from dcim.models import Interface

from config_officer.models import Collection
from config_officer.storage import ConfigRepository
from tests.integration.helpers import run_job


def log_text(job) -> str:
    return "\n".join(entry["message"] for entry in job.log_entries)


@pytest.mark.django_db
def test_first_run_collects_and_syncs(admin_user, make_device, fake_network):
    device = make_device("sw1", ip="10.0.0.1/24")
    job = run_job(admin_user, device_ids=(device.pk,))

    assert job.status == JobStatusChoices.STATUS_COMPLETED
    assert job.data["summary"] == {
        "total": 1,
        "succeeded": 1,
        "failed": 0,
        "skipped": 0,
        "changed": 1,
    }
    assert "[sw1] OK - Collected via SSH" in log_text(job)
    assert "Git: committed" in log_text(job)

    result = Collection.objects.get(job=job)
    assert (result.status, result.transport, result.config_changed) == ("succeeded", "ssh", True)
    assert "GigabitEthernet1/0/1" in result.details["netbox"]["interfaces_created"]

    device.refresh_from_db()
    assert device.serial == "FOC12345ABC"
    assert set(Interface.objects.filter(device=device).values_list("name", flat=True)) == {
        "GigabitEthernet0/0",
        "GigabitEthernet1/0/1",
        "GigabitEthernet1/0/2",
        "Loopback0",
    }  # Vlan10 (SVI) is skipped

    stored = ConfigRepository.from_settings().read_config("sw1")
    assert "ntp server 1.1.1.1" in stored and "secret 9" not in stored

    changes = ObjectChange.objects.filter(request_id=job.job_id)
    assert changes.exists() and set(changes.values_list("user_name", flat=True)) == {
        admin_user.username
    }


@pytest.mark.django_db
def test_second_run_changes_nothing(admin_user, make_device, fake_network):
    device = make_device("sw1", ip="10.0.0.1/24")
    run_job(admin_user, device_ids=(device.pk,))
    job = run_job(admin_user, device_ids=(device.pk,))

    result = Collection.objects.get(job=job)
    assert result.config_changed is False
    assert "no NetBox changes" in result.message
    assert "Git: no configuration changes to commit" in log_text(job)
    assert not ObjectChange.objects.filter(request_id=job.job_id).exists()


@pytest.mark.django_db
def test_changed_config_is_committed(admin_user, make_device, fake_network):
    device = make_device("sw1", ip="10.0.0.1/24")
    run_job(admin_user, device_ids=(device.pk,))
    config = (
        fake_network.open_connection(None, "ssh", 22, None)
        .send_command("show running-config")
        .result
    )
    fake_network.overrides["show running-config"] = config.replace(
        "ntp server 1.1.1.1", "ntp server 9.9.9.9"
    )

    job = run_job(admin_user, device_ids=(device.pk,))
    assert job.data["summary"]["changed"] == 1
    history = ConfigRepository.from_settings().history("sw1")
    assert history.count == 2
    assert "+ntp server 9.9.9.9" in history.commits[0].diff


@pytest.mark.django_db
def test_failures_are_reported_per_device(admin_user, make_device, fake_network):
    make_device("ok", ip="10.0.0.1/24")
    make_device("unreachable", ip="10.0.0.2/24")
    make_device("wrong-serial", ip="10.0.0.3/24", serial="OTHER")
    make_device("no-ip")
    fake_network.unreachable.add("10.0.0.2")

    job = run_job(admin_user, all_devices=True)
    assert job.data["summary"] == {
        "total": 4,
        "succeeded": 1,
        "failed": 2,
        "skipped": 1,
        "changed": 1,
    }
    results = {r.device.name: r for r in Collection.objects.filter(job=job)}
    assert results["unreachable"].failed_reason == "fail-connect"
    assert "ssh 10.0.0.2:22: connection refused" in results["unreachable"].message
    assert results["wrong-serial"].failed_reason == "fail-update"
    assert results["no-ip"].status == "skipped"
    # A device with the wrong serial must not overwrite anything.
    assert ConfigRepository.from_settings().read_config("wrong-serial") is None


@pytest.mark.django_db
def test_job_fails_when_no_device_succeeds(admin_user, make_device, fake_network):
    make_device("sw1", ip="10.0.0.1/24")
    fake_network.unreachable.add("10.0.0.1")
    assert run_job(admin_user, all_devices=True).status == JobStatusChoices.STATUS_FAILED


@pytest.mark.django_db
def test_selection_by_tag(admin_user, make_device, fake_network):
    from extras.models import Tag

    core = Tag.objects.create(name="core", slug="core")
    tagged = make_device("tagged", ip="10.0.0.1/24")
    tagged.tags.add(core)
    make_device("other", ip="10.0.0.2/24")

    job = run_job(admin_user, tag_slugs=("core",))
    assert list(Collection.objects.filter(job=job).values_list("device__name", flat=True)) == [
        "tagged"
    ]
