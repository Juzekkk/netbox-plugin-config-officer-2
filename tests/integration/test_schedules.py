"""Schedules: the recurring NetBox job follows the schedule; runs send the webhook."""

import json
from datetime import timedelta
from unittest import mock

import pytest
from django.utils import timezone

from config_officer.jobs import CollectDevicesJob, CollectScheduleJob
from config_officer.models import CollectSchedule


def waiting_jobs(schedule):
    return list(CollectScheduleJob.get_jobs(schedule).filter(status__in=["pending", "scheduled"]))


@pytest.mark.django_db
def test_job_follows_the_schedule():
    start = timezone.now() + timedelta(hours=2)
    schedule = CollectSchedule.objects.create(
        name="nightly", all_devices=True, interval_hours=24, next_run=start
    )
    (job,) = waiting_jobs(schedule)
    assert (job.scheduled, job.interval) == (start, 24 * 60)

    schedule.interval_hours = 12
    schedule.save()
    (job,) = waiting_jobs(schedule)
    assert job.interval == 12 * 60

    schedule.enabled = False
    schedule.save()
    assert waiting_jobs(schedule) == []

    schedule.enabled = True
    schedule.save()
    schedule.delete()
    assert waiting_jobs(schedule) == []


def test_next_occurrence_keeps_the_cadence():
    start = timezone.now() - timedelta(hours=25)
    schedule = CollectSchedule(interval_hours=24, next_run=start)
    assert schedule.next_occurrence() == start + timedelta(hours=48)


@pytest.mark.django_db
def test_scheduled_run_sends_a_signed_webhook(admin_user, make_device, fake_network):
    make_device("sw1", ip="10.0.0.1/24")
    schedule = CollectSchedule.objects.create(
        name="nightly", all_devices=True, webhook_url="http://hook.example", webhook_secret="s3cret"
    )
    with mock.patch("config_officer.webhook.requests.post") as post:
        post.return_value.status_code = 200
        job = CollectScheduleJob.enqueue(immediate=True, instance=schedule, user=admin_user)
    job.refresh_from_db()
    payload = json.loads(post.call_args.kwargs["data"])
    assert (payload["schedule"], payload["summary"]["succeeded"]) == ("nightly", 1)
    assert payload["commit"].startswith("schedule_nightly_")
    assert any("Webhook: delivered" in entry["message"] for entry in job.log_entries)


@pytest.mark.django_db
def test_disabled_schedule_stops_its_recurring_job(admin_user):
    schedule = CollectSchedule.objects.create(name="paused", all_devices=True)
    (job,) = waiting_jobs(schedule)
    CollectSchedule.objects.filter(pk=schedule.pk).update(
        enabled=False
    )  # e.g. a job left over from 0.1.x

    CollectScheduleJob(job).run()
    assert job.interval is None  # JobRunner.handle() will not schedule the next run


@pytest.mark.django_db
def test_run_now_is_linked_to_the_schedule(admin_client):
    schedule = CollectSchedule.objects.create(name="nightly", all_devices=True)
    response = admin_client.post(f"/plugins/config_officer/schedules/{schedule.pk}/run/")
    assert response.status_code == 302
    job = CollectDevicesJob.get_jobs(schedule).get()
    assert response["Location"] == f"/plugins/config_officer/runs/{job.pk}/"
