"""Helpers shared by the integration tests."""

from core.models import ObjectType
from users.models import ObjectPermission

from config_officer.collection.targets import DeviceSelection
from config_officer.jobs import CollectDevicesJob


def run_job(user, **selection):
    """Run a collection synchronously (as the worker would) and return the finished Job."""
    job = CollectDevicesJob.enqueue(
        immediate=True, user=user, selection=DeviceSelection(**selection).as_dict()
    )
    job.refresh_from_db()
    return job


def grant(user, model: str, actions: list[str], constraints=None):
    """Give ``user`` a NetBox ObjectPermission, e.g. grant(u, "dcim.device", ["view"])."""
    app_label, model_name = model.split(".")
    permission = ObjectPermission.objects.create(
        name=f"{user}-{model}-{'-'.join(actions)}", actions=actions, constraints=constraints
    )
    permission.object_types.set([ObjectType.objects.get(app_label=app_label, model=model_name)])
    permission.users.add(user)
