from datetime import datetime
from netbox.jobs import JobRunner

from .worker import collect_device_config_task

class CollectScheduleJob(JobRunner):
    class Meta:
        name = "Config Officer - Collect Schedule"

    def run(self, *args, **kwargs):
        from django_rq import get_queue
        from .models import Collection
        from .worker import collect_device_config_task
        from datetime import datetime

        schedule = self.job.object
        if not schedule.enabled:
            self.logger.info("Schedule %r is disabled, skipping", schedule.name)
            return

        devices = list(schedule.devices.all())
        self.logger.info("Schedule %r: enqueuing %d device(s)", schedule.name, len(devices))

        queue = get_queue("default")
        commit_msg = (
            f"schedule_{schedule.name}_{datetime.now().strftime('%Y_%m_%d_%H_%M_%S')}"
        )

        for device in devices:
            collect_task = Collection.objects.create(
                device=device,
                message=f"schedule:{schedule.name}",
            )
            queue.enqueue(
                collect_device_config_task,  # referencja zamiast stringa
                collect_task.pk,
                commit_msg,
            )
            self.logger.debug("Enqueued %s (task_id=%d)", device.name, collect_task.pk)