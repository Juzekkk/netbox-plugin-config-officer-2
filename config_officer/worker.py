from django_rq import job, get_queue
from dcim.models import Device
from .models import Collection, Compliance, ServiceMapping
from datetime import datetime
import time
from .choices import CollectFailChoices, CollectStatusChoices
import ipaddress
from .collect import CollectDeviceData
from .custom_exceptions import CollectionException
from django.db.models import Q
from git import Repo
from .choices import ServiceComplianceChoices
from .git_manager import get_device_config, get_days_after_update
from .config_manager import get_config_diff
from django.conf import settings
import logging

logger = logging.getLogger(__name__)

PLUGIN_SETTINGS = settings.PLUGINS_CONFIG.get("config_officer", dict())
CF_NAME_COLLECTION_STATUS = PLUGIN_SETTINGS.get("CF_NAME_COLLECTION_STATUS", "collection_status")
NETBOX_DEVICES_CONFIGS_DIR = PLUGIN_SETTINGS.get("NETBOX_DEVICES_CONFIGS_DIR", "/device_configs")
GLOBAL_TASK_INIT_MESSAGE = 'global_collection_task'
DEFAULT_PLATFORM = 'iosxe'

def get_active_collect_task_count():
    """ Get count of pending collection tasks."""
    return  Collection.objects.filter((Q(status__iexact=CollectStatusChoices.STATUS_PENDING)
            | Q(status__iexact=CollectStatusChoices.STATUS_RUNNING)) & Q(message__iexact=GLOBAL_TASK_INIT_MESSAGE)).count()


@job("default")
def collect_device_config_hostname(hostname):
    """Collect device configuration by name. Task started with hostname param."""

    device = Device.objects.get(name__iexact=hostname)
    collect_task = Collection.objects.create(device=device, message="device collection task")
    collect_task.save()

    now = datetime.now().strftime("%Y_%m_%d_%H_%M_%S")
    commit_msg = f"device_{hostname}_{now}"      
    get_queue("default").enqueue("config_officer.worker.collect_device_config_task", collect_task.pk, commit_msg)


@job("default")
def collect_device_config_task(task_id, commit_msg=""):
    logger.debug(f"[COLLECT] Task start: task_id={task_id}, commit_msg='{commit_msg}'")

    time.sleep(1)
    try:
        collect_task = Collection.objects.get(id=task_id)
        logger.debug(f"[COLLECT] Loaded Collection id={task_id}, status={collect_task.status}")
    except Collection.DoesNotExist:
        logger.warning(f"[COLLECT] Collection not found immediately, retrying task_id={task_id}")
        time.sleep(5)
        collect_task = Collection.objects.get(id=task_id)
        logger.exception(f"[COLLECT] Loaded after retry task_id={task_id}")

    collect_task.status = CollectStatusChoices.STATUS_RUNNING
    collect_task.save()
    logger.debug(f"[COLLECT] Status set to RUNNING task_id={task_id}")

    if not commit_msg:
        now = datetime.now().strftime("%Y_%m_%d_%H_%M_%S")
        commit_msg = f"{now}"
        logger.debug(f"[COLLECT] Generated commit_msg={commit_msg}")

    try:
        device_netbox = collect_task.device
        logger.debug(f"[COLLECT] Device loaded: {device_netbox.name}")

        device_netbox.custom_field_data[CF_NAME_COLLECTION_STATUS] = False

        platform = (
            device_netbox.platform.name
            if device_netbox.platform is not None
            else DEFAULT_PLATFORM
        )

        device_netbox.save()
        logger.debug(f"[COLLECT] Device saved, platform={platform}")

        ip = str(ipaddress.ip_interface(device_netbox.primary_ip4).ip)
        logger.debug(f"[COLLECT] Parsed IP={ip}")

        device_collect = CollectDeviceData(
            collect_task,
            ip=ip,
            hostname_ipam=str(device_netbox.name),
            platform=platform
        )

        logger.debug("[COLLECT] Starting collect_information()")
        device_collect.collect_information()
        logger.debug("[COLLECT] collect_information() finished")

    except CollectionException as exc:
        logger.error(f"[COLLECT] CollectionException: {exc.reason} | {exc.message}")

        collect_task.status = CollectStatusChoices.STATUS_FAILED
        collect_task.failed_reason = exc.reason
        collect_task.message = exc.message
        collect_task.save()

        logger.exception("[COLLECT] Task marked FAILED (CollectionException)")

        if get_active_collect_task_count() < 11:
            logger.exception("[COLLECT] Enqueue git commit (after failure)")
            get_queue("default").enqueue(
                "config_officer.worker.git_commit_configs_changes",
                commit_msg
            )

        raise

    except Exception as exc:
        logger.exception("[COLLECT] Unknown exception occurred")

        collect_task.status = CollectStatusChoices.STATUS_FAILED
        collect_task.failed_reason = CollectFailChoices.FAIL_GENERAL
        collect_task.message = f"Unknown error {exc}"
        collect_task.save()

        logger.exception("[COLLECT] Task marked FAILED (general exception)")

        if get_active_collect_task_count() < 11:
            logger.exception("[COLLECT] Enqueue git commit (after failure)")
            get_queue("default").enqueue(
                "config_officer.worker.git_commit_configs_changes",
                commit_msg
            )

        raise

    collect_task.status = CollectStatusChoices.STATUS_SUCCEEDED
    device_netbox.custom_field_data[CF_NAME_COLLECTION_STATUS] = True
    collect_task.save()

    logger.debug("[COLLECT] Task SUCCESS")

    try:
        logger.debug("[COLLECT] Enqueue config compliance check")
        get_queue("default").enqueue(
            "config_officer.worker.check_device_config_compliance",
            device=collect_task.device
        )
    except Exception:
        logger.exception("[COLLECT] Failed to enqueue compliance check")

    if get_active_collect_task_count() < 11:
        logger.exception("[COLLECT] Enqueue git commit (success path)")
        get_queue("default").enqueue(
            "config_officer.worker.git_commit_configs_changes",
            commit_msg
        )

    logger.debug(f"[COLLECT] Task finished: device={collect_task.device.name}, ip={ip}")

    return f"{collect_task.device.name} {ip} running config was collected."


@job("default")
def git_commit_configs_changes(msg):
    logger.debug(f"[GIT] Job started with msg='{msg}'")

    if get_active_collect_task_count() > 0:
        logger.debug("[GIT] Skipped commit - active collect tasks still running")
        return "Skipped due to active tasks"

    try:
        repo = Repo(NETBOX_DEVICES_CONFIGS_DIR)
        repo.git.add("*")
        logger.debug("[GIT] git add executed")

        diff = repo.index.diff("HEAD")
        logger.debug(f"[GIT] Diff size vs HEAD: {len(diff)}")

        if len(diff) > 0:
            logger.debug("[GIT] Changes detected -> committing now")

            commit_hash = repo.git.commit(
                "-m", msg,
                author="Netbox Netbox <netbox@example.com>"
            )

            logger.debug(f"[GIT] COMMIT DONE: {commit_hash}")
            return f"Committed: {commit_hash}"

        else:
            logger.debug("[GIT] No changes -> commit skipped")
            return "No changes for commit"

    except Exception as e:
        logger.exception("[GIT] Commit failed")
        return f"Error: {e}"


@job("default")
def check_device_config_compliance(device):
    
    """Check a configuration template compliance for a particular device.""" 

    # Compliance.objects.get_or_create(device=device).delete()    
    compliance = Compliance.objects.get_or_create(device=device)[0]
    compliance.status = ServiceComplianceChoices.STATUS_NON_COMPLIANCE
    compliance.notes = "not checked yet"
    compliance.generated_config = "None"
    compliance.diff = "None"
    compliance.save()
    compliance.services = [m.service.name for m in ServiceMapping.objects.filter(device=compliance.device)]

    # Check if there are matched templates
    templates = compliance.get_device_templates()
    if not templates:
        compliance.notes = 'No matched templates'
        compliance.save()   
        return {device: compliance.notes}    

    # Check if device config file exists: 
    device_config = get_device_config(NETBOX_DEVICES_CONFIGS_DIR, device.name, "running")        
    if not device_config:
        compliance.notes = 'running config not found in git'
        compliance.save()
        return {device: compliance.notes}

    # If device configuration elder tham 7 days - non_compliance
    device_config_age = get_days_after_update(NETBOX_DEVICES_CONFIGS_DIR, device.name, "running")       
    if device_config_age > 7:    
        compliance.notes = f"device config is staled ({device_config_age} days)"
        compliance.save()
        return {device: compliance.notes}
    elif device_config_age < 0:
        compliance.notes = 'unknown error during calculating config age',
        compliance.save()
        return {device: compliance.notes}

    generated_config = compliance.get_generated_config().splitlines()
    
    compliance.diff = get_config_diff(generated_config, device_config.splitlines())
    
    if len(compliance.diff) == 0:     
        # Running configuration absilutely compliant to template
        compliance.diff = ""   
        compliance.notes = None
        compliance.status = ServiceComplianceChoices.STATUS_COMPLIANCE
    else:
        # There are diffs
        compliance.diff = "\n".join("\n".join(line) for line in compliance.diff)
        compliance.notes = None
        compliance.status = ServiceComplianceChoices.STATUS_NON_COMPLIANCE
    compliance.save()

    return {device: compliance.status}


@job("default")
def collect_all_devices_configs():
    """Worker - collect show-run configs from all devices."""
    # commit changes before the global collection

    Collection.objects.all().delete()
    devices = Device.objects.all()
    now = datetime.now().strftime("%Y_%m_%d_%H_%M_%S")
    commit_msg = f"global_{now}"      
    for device in devices:
        collect_task = Collection.objects.create(device=device, message=GLOBAL_TASK_INIT_MESSAGE)
        collect_task.save()
        get_queue("default").enqueue("config_officer.worker.collect_device_config_task", collect_task.pk, commit_msg)