from datetime import datetime
from netbox.jobs import JobRunner


class CollectScheduleJob(JobRunner):
    class Meta:
        name = "Config Officer - Collect Schedule"

    def run(self, *args, **kwargs):
        from django_rq import get_queue
        from .models import Collection
        from .worker import collect_device_config_task

        schedule = self.job.object

        if not schedule.enabled:
            self.logger.info(f"Schedule '{schedule.name}' is disabled, skipping")
            return

        devices = list(schedule.devices.all())
        self.logger.info(f"Schedule '{schedule.name}': enqueuing {len(devices)} device(s)")

        queue = get_queue("default")
        commit_msg = f"schedule_{schedule.name}_{datetime.now().strftime('%Y_%m_%d_%H_%M_%S')}"

        for device in devices:
            collect_task = Collection.objects.create(
                device=device,
                message=f"schedule:{schedule.name}",
            )
            queue.enqueue(collect_device_config_task, collect_task.pk, commit_msg)
            self.logger.debug(f"Enqueued {device.name} (task_id={collect_task.pk})")

        self.logger.info(
            f"Schedule '{schedule.name}': done, enqueued {len(devices)} device(s) "
            f"with commit '{commit_msg}'"
        )