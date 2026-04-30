from datetime import datetime

from core.choices import JobStatusChoices
from core.models import Job
from django.contrib.contenttypes.models import ContentType
from django_rq import get_queue
from netbox.jobs import JobRunner

from .models import Collection, CollectSchedule
from .worker import collect_device_config_task


class CollectScheduleJob(JobRunner):
    class Meta:
        name = "Config Officer - Collect Schedule"

    def run(self, *args, **kwargs):
        try:
            schedule = CollectSchedule.objects.get(pk=self.job.object_id)
        except CollectSchedule.DoesNotExist:
            self.logger.warning(
                f"CollectSchedule (pk={self.job.object_id}) no longer exists - skipping"
            )
            return

        if not schedule.enabled:
            self.logger.info(f"Schedule '{schedule.name}' is disabled, skipping")
            return

        devices = list(schedule.devices.all())
        self.logger.info(f"Schedule '{schedule.name}': enqueuing {len(devices)} device(s)")

        queue = get_queue("default")
        commit_msg = f"schedule_{schedule.name}_{datetime.now().strftime('%Y_%m_%d_%H_%M_%S')}"

        ct = ContentType.objects.get_for_model(CollectSchedule)
        previous_job = (
            Job.objects.filter(
                object_type=ct,
                object_id=schedule.pk,
                status=JobStatusChoices.STATUS_COMPLETED,
            )
            .exclude(pk=self.job.pk)
            .order_by("-completed")
            .first()
        )
        since = previous_job.completed if previous_job else None

        collect_job_ids = []
        for device in devices:
            collect_task = Collection.objects.create(
                device=device,
                message=f"schedule:{schedule.name}",
            )
            job = queue.enqueue(collect_device_config_task, collect_task.pk, commit_msg)
            collect_job_ids.append(job.id)
            self.logger.debug(
                f"Enqueued {device.name} (task_id={collect_task.pk}, job_id={job.id})"
            )

        if schedule.webhook_url and collect_job_ids:
            queue.enqueue(
                "config_officer.webhook.send_schedule_webhook_task",
                schedule.pk,
                commit_msg,
                collect_job_ids,
                since,
            )

        self.logger.info(
            f"Schedule '{schedule.name}': done, enqueued {len(devices)} device(s) "
            f"with commit '{commit_msg}'"
        )
