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
import re

logger = logging.getLogger(__name__)

PLUGIN_SETTINGS = settings.PLUGINS_CONFIG.get("config_officer", dict())
CF_NAME_COLLECTION_STATUS = PLUGIN_SETTINGS.get("CF_NAME_COLLECTION_STATUS", "collection_status")
NETBOX_DEVICES_CONFIGS_DIR = PLUGIN_SETTINGS.get("NETBOX_DEVICES_CONFIGS_DIR", "/device_configs")
GLOBAL_TASK_INIT_MESSAGE = 'global_collection_task'
DEFAULT_PLATFORM = 'nxos'

# Lines matching these patterns change on every 'show run' even when the
# actual config is identical. Strip them before comparing for real changes.
VOLATILE_LINE_PATTERNS = [
    re.compile(r'^!Time:'),
    re.compile(r'^!Running configuration last done at:'),
    re.compile(r'^!NVRAM config last updated'),
    re.compile(r'^! Last configuration change'),
    re.compile(r'^ntp clock-period'),
]


def _strip_volatile_lines(text: str) -> str:
    """Remove timestamp/volatile lines before diffing two config versions."""
    return "\n".join(
        line for line in text.splitlines()
        if not any(p.match(line) for p in VOLATILE_LINE_PATTERNS)
    )


def get_active_collect_task_count():
    """Get count of pending/running global collection tasks."""
    return Collection.objects.filter(
        (
            Q(status__iexact=CollectStatusChoices.STATUS_PENDING)
            | Q(status__iexact=CollectStatusChoices.STATUS_RUNNING)
        )
        & Q(message__iexact=GLOBAL_TASK_INIT_MESSAGE)
    ).count()


# Collect by hostname (UI entry point)

@job("default")
def collect_device_config_hostname(hostname):
    """Collect device configuration by name."""
    logger.info(f"[WORKER] collect_device_config_hostname: hostname={hostname!r}")

    device = Device.objects.get(name__iexact=hostname)
    collect_task = Collection.objects.create(device=device, message="device collection task")
    collect_task.save()
    logger.debug(f"[WORKER] Created Collection id={collect_task.pk}")

    now = datetime.now().strftime("%Y_%m_%d_%H_%M_%S")
    commit_msg = f"device_{hostname}_{now}"
    get_queue("default").enqueue(
        "config_officer.worker.collect_device_config_task",
        collect_task.pk,
        commit_msg,
    )
    logger.debug(f"[WORKER] Enqueued task id={collect_task.pk} commit_msg={commit_msg!r}")


# Main collection task

@job("default")
def collect_device_config_task(task_id, commit_msg=""):
    logger.info(f"[WORKER] collect_device_config_task: task_id={task_id} commit_msg={commit_msg!r}")

    time.sleep(1)
    try:
        collect_task = Collection.objects.get(id=task_id)
        logger.debug(f"[WORKER] Loaded Collection id={task_id} status={collect_task.status!r}")
    except Collection.DoesNotExist:
        logger.warning(f"[WORKER] Collection id={task_id} not found, retrying in 5s")
        time.sleep(5)
        collect_task = Collection.objects.get(id=task_id)

    collect_task.status = CollectStatusChoices.STATUS_RUNNING
    collect_task.save()
    logger.debug("[WORKER] Status -> RUNNING")

    if not commit_msg:
        commit_msg = datetime.now().strftime("%Y_%m_%d_%H_%M_%S")
        logger.debug(f"[WORKER] Generated commit_msg={commit_msg!r}")

    ip = "unknown"
    try:
        device_netbox = collect_task.device
        logger.debug(f"[WORKER] Device: {device_netbox.name}")

        device_netbox.custom_field_data[CF_NAME_COLLECTION_STATUS] = False

        platform = (
            device_netbox.platform.name
            if device_netbox.platform is not None
            else DEFAULT_PLATFORM
        )
        logger.debug(f"[WORKER] Platform: {platform!r}")

        if not device_netbox.primary_ip4:
            raise CollectionException(
                reason=CollectFailChoices.FAIL_CONNECT,
                message=f"Device {device_netbox.name} has no Primary IPv4 set in NetBox.",
            )

        device_netbox.save()
        ip = str(ipaddress.ip_interface(device_netbox.primary_ip4).ip)
        logger.debug(f"[WORKER] Primary IP: {ip}")

        device_collect = CollectDeviceData(
            collect_task,
            ip=ip,
            hostname_ipam=str(device_netbox.name),
            platform=platform,
        )

        logger.debug("[WORKER] Calling collect_information()")
        device_collect.collect_information()
        logger.info(f"[WORKER] collect_information() OK for {device_netbox.name}")

    except CollectionException as exc:
        logger.error(
            f"[WORKER] CollectionException: reason={exc.reason!r} message={exc.message!r}"
        )
        collect_task.status = CollectStatusChoices.STATUS_FAILED
        collect_task.failed_reason = exc.reason
        collect_task.message = exc.message
        collect_task.save()
        if get_active_collect_task_count() < 11:
            get_queue("default").enqueue(
                "config_officer.worker.git_commit_configs_changes", commit_msg
            )
        raise

    except Exception as exc:
        logger.exception(f"[WORKER] Unexpected exception: {exc}")
        collect_task.status = CollectStatusChoices.STATUS_FAILED
        collect_task.failed_reason = CollectFailChoices.FAIL_GENERAL
        collect_task.message = f"Unknown error {exc}"
        collect_task.save()
        if get_active_collect_task_count() < 11:
            get_queue("default").enqueue(
                "config_officer.worker.git_commit_configs_changes", commit_msg
            )
        raise

    collect_task.status = CollectStatusChoices.STATUS_SUCCEEDED
    device_netbox.custom_field_data[CF_NAME_COLLECTION_STATUS] = True
    collect_task.save()
    logger.info(f"[WORKER] Task SUCCESS: {collect_task.device.name} ({ip})")

    try:
        get_queue("default").enqueue(
            "config_officer.worker.check_device_config_compliance",
            device=collect_task.device,
        )
        logger.debug("[WORKER] Enqueued compliance check")
    except Exception:
        logger.exception("[WORKER] Failed to enqueue compliance check")

    if get_active_collect_task_count() < 11:
        get_queue("default").enqueue(
            "config_officer.worker.git_commit_configs_changes", commit_msg
        )
        logger.debug(f"[WORKER] Enqueued git commit: {commit_msg!r}")

    return f"{collect_task.device.name} {ip} running config was collected."


# Smart git commit – skips commit when only volatile lines changed

@job("default")
def git_commit_configs_changes(msg):
    """
    Commit device config changes, but only when the config actually changed.

    Volatile lines (timestamps, 'show run' metadata) are stripped before
    comparing the staged file with HEAD. If the only differences are in those
    lines, the file is restored from HEAD and no commit is made.
    """
    logger.info(f"[GIT] git_commit_configs_changes: msg={msg!r}")

    if get_active_collect_task_count() > 0:
        logger.debug("[GIT] Active collect tasks running – skipping commit")
        return "Skipped: active collect tasks"

    try:
        try:
            repo = Repo(NETBOX_DEVICES_CONFIGS_DIR)
            logger.debug("[GIT] Existing repo loaded")
        except (InvalidGitRepositoryError, NoSuchPathError):
            logger.debug("[GIT] Repo not found – initializing new repository")
            repo = Repo.init(NETBOX_DEVICES_CONFIGS_DIR)
        
        if not repo.head.is_valid():
            logger.debug("[GIT] No HEAD yet – creating initial commit")
            repo.index.commit(msg)
            return "Initial commit created"

        repo.git.add("*")
        logger.debug("[GIT] git add * done")

        staged = repo.index.diff("HEAD")
        logger.debug(f"[GIT] Files staged vs HEAD: {len(staged)}")

        if not staged:
            logger.debug("[GIT] Nothing staged – no commit needed")
            return "No changes for commit"

        real_changes = []
        timestamp_only = []

        for diff_item in staged:
            path = diff_item.b_path or diff_item.a_path
            logger.debug(f"[GIT] Evaluating: {path}")

            try:
                # Read current file from disk
                file_path = f"{NETBOX_DEVICES_CONFIGS_DIR}/{path.split('/')[-1]}"
                try:
                    with open(file_path, "r", errors="replace") as f:
                        new_text = f.read()
                except FileNotFoundError:
                    # Deleted file – always a real change
                    logger.debug(f"[GIT] {path} deleted -> real change")
                    real_changes.append(path)
                    continue

                # Read HEAD version
                try:
                    old_text = repo.git.show(f"HEAD:{path}")
                except Exception:
                    # New file – always a real change
                    logger.debug(f"[GIT] {path} is new -> real change")
                    real_changes.append(path)
                    continue

                new_stripped = _strip_volatile_lines(new_text)
                old_stripped = _strip_volatile_lines(old_text)

                if new_stripped == old_stripped:
                    logger.debug(
                        f"[GIT] {path} -> only timestamps differ, restoring from HEAD"
                    )
                    timestamp_only.append(path)
                    repo.git.checkout("HEAD", "--", path)
                else:
                    logger.debug(f"[GIT] {path} -> real config change")
                    real_changes.append(path)

            except Exception:
                logger.exception(f"[GIT] Error comparing {path}, treating as real change")
                real_changes.append(path)

        logger.info(
            f"[GIT] Result: {len(real_changes)} real change(s), "
            f"{len(timestamp_only)} timestamp-only (skipped)"
        )
        if real_changes:
            logger.info(f"[GIT] Changed: {real_changes}")
        if timestamp_only:
            logger.debug(f"[GIT] Skipped: {timestamp_only}")

        # Re-check staged after restoring timestamp-only files
        staged_after = repo.index.diff("HEAD")
        if not staged_after:
            logger.info("[GIT] No real changes after filtering – commit skipped")
            return "No real config changes (only timestamps differed)"

        commit_hash = repo.git.commit(
            "-m", msg,
            author="Netbox Netbox <netbox@example.com>",
        )
        logger.info(f"[GIT] Committed: {commit_hash}")
        return f"Committed: {commit_hash}"

    except Exception:
        logger.exception("[GIT] Commit failed")
        return "Error during commit"


# Compliance check

@job("default")
def check_device_config_compliance(device):
    """Check configuration template compliance for a particular device."""
    logger.info(f"[COMPLIANCE] Checking: {device.name}")

    compliance = Compliance.objects.get_or_create(device=device)[0]
    compliance.status = ServiceComplianceChoices.STATUS_NON_COMPLIANCE
    compliance.notes = "not checked yet"
    compliance.generated_config = "None"
    compliance.diff = "None"
    compliance.save()
    compliance.services = [
        m.service.name
        for m in ServiceMapping.objects.filter(device=compliance.device)
    ]

    templates = compliance.get_device_templates()
    if not templates:
        logger.debug(f"[COMPLIANCE] No matched templates for {device.name}")
        compliance.notes = 'No matched templates'
        compliance.save()
        return {device: compliance.notes}

    logger.debug(f"[COMPLIANCE] Templates: {[t.name for t in templates]}")

    device_config = get_device_config(NETBOX_DEVICES_CONFIGS_DIR, device.name, "running")
    if not device_config:
        logger.warning(f"[COMPLIANCE] Config file missing for {device.name}")
        compliance.notes = 'running config not found in git'
        compliance.save()
        return {device: compliance.notes}

    device_config_age = get_days_after_update(NETBOX_DEVICES_CONFIGS_DIR, device.name, "running")
    logger.debug(f"[COMPLIANCE] Config age: {device_config_age} day(s)")

    if device_config_age > 7:
        compliance.notes = f"device config is staled ({device_config_age} days)"
        compliance.save()
        return {device: compliance.notes}
    elif device_config_age < 0:
        compliance.notes = 'unknown error during calculating config age'
        compliance.save()
        return {device: compliance.notes}

    generated_config = compliance.get_generated_config().splitlines()
    logger.debug(f"[COMPLIANCE] Generated config: {len(generated_config)} lines")

    compliance.diff = get_config_diff(generated_config, device_config.splitlines())

    if len(compliance.diff) == 0:
        logger.info(f"[COMPLIANCE] {device.name} -> COMPLIANT ✓")
        compliance.diff = ""
        compliance.notes = None
        compliance.status = ServiceComplianceChoices.STATUS_COMPLIANCE
    else:
        logger.info(f"[COMPLIANCE] {device.name} -> NON-COMPLIANT ({len(compliance.diff)} diff lines)")
        compliance.diff = "\n".join("\n".join(line) for line in compliance.diff)
        compliance.notes = None
        compliance.status = ServiceComplianceChoices.STATUS_NON_COMPLIANCE

    compliance.save()
    return {device: compliance.status}


# Global collection

@job("default")
def collect_all_devices_configs():
    """Worker – collect show-run configs from all devices."""
    logger.info("[WORKER] collect_all_devices_configs: starting global collection")

    Collection.objects.all().delete()
    devices = Device.objects.all()
    count = devices.count()
    logger.info(f"[WORKER] Devices to collect: {count}")

    now = datetime.now().strftime("%Y_%m_%d_%H_%M_%S")
    commit_msg = f"global_{now}"

    for device in devices:
        collect_task = Collection.objects.create(device=device, message=GLOBAL_TASK_INIT_MESSAGE)
        collect_task.save()
        get_queue("default").enqueue(
            "config_officer.worker.collect_device_config_task",
            collect_task.pk,
            commit_msg,
        )
        logger.debug(f"[WORKER] Enqueued task for {device.name} (id={collect_task.pk})")

    logger.info(f"[WORKER] Enqueued {count} collection tasks with commit_msg={commit_msg!r}")