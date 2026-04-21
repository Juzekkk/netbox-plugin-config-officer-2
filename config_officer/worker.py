"""
config_officer worker tasks.
"""

import ipaddress
import logging
import os
import re
import time
from datetime import datetime

from django.conf import settings
from django.db.models import Q
from django_rq import get_queue, job
from git import GitCommandError, InvalidGitRepositoryError, NoSuchPathError, Repo
from git.exc import GitCommandNotFound

from dcim.models import Device

from .choices import CollectFailChoices, CollectStatusChoices, ServiceComplianceChoices
from .collect import CollectDeviceData
from .config_manager import get_config_diff
from .custom_exceptions import CollectionException
from .git_manager import get_days_after_update, get_device_config
from .models import Collection, Compliance, ServiceMapping

logger = logging.getLogger(__name__)

#  Plugin settings 

PLUGIN_SETTINGS = settings.PLUGINS_CONFIG.get("config_officer", {})

CF_NAME_COLLECTION_STATUS  = PLUGIN_SETTINGS.get("CF_NAME_COLLECTION_STATUS", "collection_status")
NETBOX_DEVICES_CONFIGS_DIR = PLUGIN_SETTINGS.get("NETBOX_DEVICES_CONFIGS_DIR", "/device_configs")
DEFAULT_PLATFORM           = PLUGIN_SETTINGS.get("DEFAULT_PLATFORM", "nxos")
GLOBAL_TASK_INIT_MESSAGE   = "global_collection_task"

_GIT_REMOTE_CFG       = PLUGIN_SETTINGS.get("GIT_REMOTE", {})
GIT_REMOTE_ENABLED    = _GIT_REMOTE_CFG.get("ENABLED", False)
GIT_REMOTE_URL        = _GIT_REMOTE_CFG.get("URL")
GIT_REMOTE_NAME       = _GIT_REMOTE_CFG.get("NAME", "origin")
GIT_REMOTE_BRANCH     = _GIT_REMOTE_CFG.get("BRANCH", "main")
GIT_REMOTE_KEY        = _GIT_REMOTE_CFG.get("SSH_KEY_PATH")
GIT_AUTHOR            = _GIT_REMOTE_CFG.get("AUTHOR", "Netbox <netbox@example.com>")

# Lines that change on every 'show run' even when config is identical
VOLATILE_LINE_PATTERNS: list[re.Pattern] = [
    re.compile(r"^!Time:"),
    re.compile(r"^!Running configuration last done at:"),
    re.compile(r"^!NVRAM config last updated"),
    re.compile(r"^! Last configuration change"),
    re.compile(r"^ntp clock-period"),
]


# Helpers

def _strip_volatile_lines(text: str) -> str:
    """Remove timestamp/metadata lines before comparing two config versions."""
    return "\n".join(
        line for line in text.splitlines()
        if not any(p.match(line) for p in VOLATILE_LINE_PATTERNS)
    )


def _get_ssh_env(key_path: str) -> dict[str, str]:
    """
    Build GIT_SSH_COMMAND that:
      - uses the specified private key
      - disables interactive host-key prompts (StrictHostKeyChecking=accept-new
        on first connect, then verify on subsequent connects)
      - disables agent forwarding to avoid key leakage
    """
    if not key_path:
        return {}
    cmd = (
        f"ssh -i {key_path}"
        " -o IdentitiesOnly=yes"
        " -o StrictHostKeyChecking=accept-new"   # accept unknown host on first connect
        " -o UserKnownHostsFile=/root/.ssh/known_hosts"
        " -o BatchMode=yes"                       # never prompt interactively
        " -o ConnectTimeout=15"
    )
    logger.debug("[GIT] GIT_SSH_COMMAND: %s", cmd)
    return {"GIT_SSH_COMMAND": cmd}


def _apply_ssh_env(key_path: str) -> None:
    """Set GIT_SSH_COMMAND in the current process environment."""
    env = _get_ssh_env(key_path)
    os.environ.update(env)


def get_active_collect_task_count() -> int:
    """Count pending/running global collection tasks."""
    return Collection.objects.filter(
        (
            Q(status__iexact=CollectStatusChoices.STATUS_PENDING)
            | Q(status__iexact=CollectStatusChoices.STATUS_RUNNING)
        )
        & Q(message__iexact=GLOBAL_TASK_INIT_MESSAGE)
    ).count()


# Git repository helpers

def _open_or_init_repo() -> tuple[Repo, bool]:
    """
    Open existing repo or initialise a new one.
    Returns (repo, is_new).
    """
    try:
        repo = Repo(NETBOX_DEVICES_CONFIGS_DIR)
        logger.debug("[GIT] Opened existing repo at %s (HEAD=%s)",
                     NETBOX_DEVICES_CONFIGS_DIR,
                     repo.head.commit.hexsha[:8] if repo.head.is_valid() else "none")
        return repo, False
    except (InvalidGitRepositoryError, NoSuchPathError):
        logger.info("[GIT] No git repo found at %s - initialising", NETBOX_DEVICES_CONFIGS_DIR)
        os.makedirs(NETBOX_DEVICES_CONFIGS_DIR, exist_ok=True)
        repo = Repo.init(NETBOX_DEVICES_CONFIGS_DIR)
        logger.info("[GIT] Initialised new repo at %s", NETBOX_DEVICES_CONFIGS_DIR)
        return repo, True


def _ensure_remote(repo: Repo) -> bool:
    """
    Make sure the configured remote exists.
    Returns True if remote is present/created, False if not configured.
    """
    if not GIT_REMOTE_ENABLED or not GIT_REMOTE_URL:
        logger.debug("[GIT] Remote push disabled or not configured - skipping")
        return False

    if GIT_REMOTE_NAME not in [r.name for r in repo.remotes]:
        logger.info("[GIT] Adding remote %r -> %s", GIT_REMOTE_NAME, GIT_REMOTE_URL)
        repo.create_remote(GIT_REMOTE_NAME, GIT_REMOTE_URL)
    else:
        remote = repo.remotes[GIT_REMOTE_NAME]
        if remote.url != GIT_REMOTE_URL:
            logger.info("[GIT] Updating remote URL: %s -> %s", remote.url, GIT_REMOTE_URL)
            remote.set_url(GIT_REMOTE_URL)

    return True


def _initial_clone_or_pull(repo: Repo, is_new: bool) -> None:
    """
    For a newly initialised repo: pull from remote to get existing history.
    For an existing repo: nothing (we push only).
    """
    if not is_new or not GIT_REMOTE_ENABLED or not GIT_REMOTE_URL:
        return

    _apply_ssh_env(GIT_REMOTE_KEY)

    try:
        remote = repo.remotes[GIT_REMOTE_NAME]
        logger.info("[GIT] New repo - pulling from %s/%s", GIT_REMOTE_NAME, GIT_REMOTE_BRANCH)
        remote.pull(GIT_REMOTE_BRANCH)
        logger.info("[GIT] Pull complete - repo is up to date")
    except GitCommandError as exc:
        # Remote may be empty (first use) - that's OK
        logger.warning("[GIT] Pull failed (remote may be empty): %s", exc)
    except Exception:
        logger.exception("[GIT] Unexpected error during initial pull")


def _push_to_remote(repo: Repo) -> str:
    """
    Push committed changes to remote.
    Returns a status string suitable for the task return value.
    """
    _apply_ssh_env(GIT_REMOTE_KEY)
    try:
        remote = repo.remotes[GIT_REMOTE_NAME]
        logger.info("[GIT] Pushing to %s/%s", GIT_REMOTE_NAME, GIT_REMOTE_BRANCH)
        push_results = remote.push(GIT_REMOTE_BRANCH)
        for info in push_results:
            logger.info("[GIT] Push result: flags=%s summary=%r",
                        info.flags, info.summary.strip())
        return "pushed"
    except GitCommandError as exc:
        logger.error("[GIT] Push failed (GitCommandError): %s", exc)
        return f"push_failed:{exc}"
    except Exception:
        logger.exception("[GIT] Push failed (unexpected)")
        return "push_failed:unexpected"


def _evaluate_staged_files(repo: Repo) -> tuple[list[str], list[str]]:
    """
    For each file staged vs HEAD decide whether it has real config changes
    or only volatile-line changes.

    Returns (real_changes, timestamp_only).
    Timestamp-only files are restored from HEAD (unstaged).
    """
    real_changes: list[str] = []
    timestamp_only: list[str] = []

    for diff_item in repo.index.diff("HEAD"):
        path = diff_item.b_path or diff_item.a_path
        logger.debug("[GIT] Evaluating staged file: %s", path)

        # Read current file from disk
        abs_path = os.path.join(NETBOX_DEVICES_CONFIGS_DIR, os.path.basename(path))
        try:
            with open(abs_path, "r", errors="replace") as fh:
                new_text = fh.read()
        except FileNotFoundError:
            logger.debug("[GIT] %s deleted -> real change", path)
            real_changes.append(path)
            continue

        # Read HEAD version
        try:
            old_text = repo.git.show(f"HEAD:{path}")
        except GitCommandError:
            logger.debug("[GIT] %s is new (no HEAD version) -> real change", path)
            real_changes.append(path)
            continue

        if _strip_volatile_lines(new_text) == _strip_volatile_lines(old_text):
            logger.debug("[GIT] %s - only timestamps changed, restoring from HEAD", path)
            timestamp_only.append(path)
            try:
                repo.git.checkout("HEAD", "--", path)
            except GitCommandError:
                logger.warning("[GIT] Could not restore %s from HEAD, keeping as staged", path)
                real_changes.append(path)
                timestamp_only.pop()
        else:
            logger.debug("[GIT] %s - real config change detected", path)
            real_changes.append(path)

    return real_changes, timestamp_only



# RQ jobs

@job("default")
def collect_device_config_hostname(hostname: str) -> None:
    """Entry point: trigger collection for a single device by name."""
    logger.info("[COLLECT] collect_device_config_hostname: hostname=%r", hostname)

    device = Device.objects.get(name__iexact=hostname)
    collect_task = Collection.objects.create(device=device, message="device collection task")
    logger.debug("[COLLECT] Created Collection id=%d", collect_task.pk)

    commit_msg = f"device_{hostname}_{datetime.now().strftime('%Y_%m_%d_%H_%M_%S')}"
    get_queue("default").enqueue(
        "config_officer.worker.collect_device_config_task",
        collect_task.pk,
        commit_msg,
    )
    logger.info("[COLLECT] Enqueued task id=%d commit_msg=%r", collect_task.pk, commit_msg)


@job("default")
def collect_device_config_task(task_id: int, commit_msg: str = "") -> str:
    """Collect running config from a single device."""
    logger.info("[COLLECT] Task start: task_id=%d commit_msg=%r", task_id, commit_msg)

    # Give the DB a moment to flush the Collection record
    time.sleep(1)
    try:
        collect_task = Collection.objects.get(id=task_id)
        logger.debug("[COLLECT] Loaded Collection id=%d status=%r",
                     task_id, collect_task.status)
    except Collection.DoesNotExist:
        logger.warning("[COLLECT] Collection id=%d not found, retrying in 5s", task_id)
        time.sleep(5)
        collect_task = Collection.objects.get(id=task_id)   # raises if still missing

    collect_task.status = CollectStatusChoices.STATUS_RUNNING
    collect_task.save()
    logger.debug("[COLLECT] Status -> RUNNING")

    if not commit_msg:
        commit_msg = datetime.now().strftime("%Y_%m_%d_%H_%M_%S")
        logger.debug("[COLLECT] Generated commit_msg=%r", commit_msg)

    ip = "unknown"

    def _enqueue_commit() -> None:
        if get_active_collect_task_count() < 11:
            get_queue("default").enqueue(
                "config_officer.worker.git_commit_configs_changes", commit_msg
            )
            logger.debug("[COLLECT] Enqueued git commit job: %r", commit_msg)

    try:
        device_nb = collect_task.device
        logger.debug("[COLLECT] Device: %s", device_nb.name)

        device_nb.custom_field_data[CF_NAME_COLLECTION_STATUS] = False

        platform = (
            device_nb.platform.name
            if device_nb.platform is not None
            else DEFAULT_PLATFORM
        )
        logger.debug("[COLLECT] Platform: %r", platform)

        if not device_nb.primary_ip4:
            raise CollectionException(
                reason=CollectFailChoices.FAIL_CONNECT,
                message=f"Device {device_nb.name} has no Primary IPv4 set in NetBox.",
            )

        device_nb.save()
        ip = str(ipaddress.ip_interface(device_nb.primary_ip4).ip)
        logger.debug("[COLLECT] Primary IP: %s", ip)

        collector = CollectDeviceData(
            collect_task,
            ip=ip,
            hostname_ipam=str(device_nb.name),
            platform=platform,
        )

        logger.debug("[COLLECT] Calling collect_information()")
        collector.collect_information()
        logger.info("[COLLECT] collect_information() OK for %s", device_nb.name)

    except CollectionException as exc:
        logger.error("[COLLECT] CollectionException: reason=%r message=%r",
                     exc.reason, exc.message)
        collect_task.status = CollectStatusChoices.STATUS_FAILED
        collect_task.failed_reason = exc.reason
        collect_task.message = exc.message
        collect_task.save()
        _enqueue_commit()
        raise

    except Exception as exc:
        logger.exception("[COLLECT] Unexpected error: %s", exc)
        collect_task.status = CollectStatusChoices.STATUS_FAILED
        collect_task.failed_reason = CollectFailChoices.FAIL_GENERAL
        collect_task.message = f"Unknown error: {exc}"
        collect_task.save()
        _enqueue_commit()
        raise

    collect_task.status = CollectStatusChoices.STATUS_SUCCEEDED
    device_nb.custom_field_data[CF_NAME_COLLECTION_STATUS] = True
    collect_task.save()
    logger.info("[COLLECT] Task SUCCESS: %s (%s)", collect_task.device.name, ip)

    # Enqueue follow-up jobs
    try:
        get_queue("default").enqueue(
            "config_officer.worker.check_device_config_compliance",
            device=collect_task.device,
        )
        logger.debug("[COLLECT] Enqueued compliance check for %s", collect_task.device.name)
    except Exception:
        logger.exception("[COLLECT] Failed to enqueue compliance check")

    _enqueue_commit()
    return f"{collect_task.device.name} {ip} collected"


@job("default")
def git_commit_configs_changes(msg: str) -> str:
    """
    Stage all changed configs and commit only if real config lines changed
    (volatile timestamp-only changes are filtered out).

    Flow:
      1. Open or initialise the local git repo
      2. Configure remote (if enabled) and do initial pull on first run
      3. Stage all files
      4. For each staged file: compare stripped content vs HEAD
         - Only timestamps changed -> restore from HEAD (skip)
         - Real change -> keep staged
      5. Commit if anything remains staged
      6. Push to remote (if enabled)
    """
    logger.info("[GIT] git_commit_configs_changes: msg=%r", msg)

    if get_active_collect_task_count() > 0:
        logger.info("[GIT] Active collect tasks still running - deferring commit")
        return "deferred: active collect tasks"

    try:
        repo, is_new = _open_or_init_repo()
        has_remote = _ensure_remote(repo)

        # On first-ever run: pull remote history so we can push later
        if is_new and has_remote:
            _initial_clone_or_pull(repo, is_new)

        # Nothing to commit if HEAD doesn't exist yet (truly empty repo)
        if not repo.head.is_valid():
            repo.git.add("*")
            if repo.index.diff(None) or repo.untracked_files:
                commit_hash = repo.index.commit(
                    msg,
                    author=repo.config_reader().get_value("user", "name", "Netbox")
                    + " <" + repo.config_reader().get_value("user", "email", "netbox@example.com") + ">",
                )
                logger.info("[GIT] Initial commit: %s", commit_hash.hexsha[:8])
                if has_remote:
                    _push_to_remote(repo)
            return "initial commit"

        # Stage all files
        repo.git.add("*")
        staged = repo.index.diff("HEAD")
        logger.debug("[GIT] Files staged vs HEAD: %d", len(staged))

        if not staged:
            logger.info("[GIT] Nothing staged - no commit needed")
            return "no changes"

        real_changes, timestamp_only = _evaluate_staged_files(repo)

        logger.info("[GIT] Real changes: %d file(s) | Timestamp-only (skipped): %d file(s)",
                    len(real_changes), len(timestamp_only))
        if real_changes:
            logger.info("[GIT] Files with real changes: %s", real_changes)
        if timestamp_only:
            logger.debug("[GIT] Files with timestamp-only changes: %s", timestamp_only)

        # Re-check after restoring timestamp-only files
        staged_after = repo.index.diff("HEAD")
        if not staged_after:
            logger.info("[GIT] No real changes remain after filtering - commit skipped")
            return "skipped: only timestamps changed"

        commit_result = repo.git.commit("-m", msg, author=GIT_AUTHOR)
        logger.info("[GIT] Committed: %s", commit_result.split("\n")[0])

        if has_remote:
            push_status = _push_to_remote(repo)
            return f"committed+{push_status}"

        return "committed"

    except GitCommandNotFound:
        logger.exception("[GIT] git binary not found - check PATH")
        return "error: git not found"
    except Exception:
        logger.exception("[GIT] Unexpected error in git_commit_configs_changes")
        return "error: see logs"


@job("default")
def check_device_config_compliance(device: Device) -> dict:
    """Check compliance of a device's running config against its service templates."""
    logger.info("[COMPLIANCE] Checking: %s", device.name)

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
        logger.info("[COMPLIANCE] %s - no matched templates", device.name)
        compliance.notes = "No matched templates"
        compliance.save()
        return {device: compliance.notes}

    logger.debug("[COMPLIANCE] %s - matched templates: %s",
                 device.name, [t.name for t in templates])

    device_config = get_device_config(NETBOX_DEVICES_CONFIGS_DIR, device.name, "running")
    if not device_config:
        logger.warning("[COMPLIANCE] %s - running config file not found", device.name)
        compliance.notes = "running config not found in git"
        compliance.save()
        return {device: compliance.notes}

    config_age = get_days_after_update(NETBOX_DEVICES_CONFIGS_DIR, device.name, "running")
    logger.debug("[COMPLIANCE] %s - config age: %d day(s)", device.name, config_age)

    if config_age > 7:
        msg = f"config is stale ({config_age} days)"
        logger.warning("[COMPLIANCE] %s - %s", device.name, msg)
        compliance.notes = msg
        compliance.save()
        return {device: compliance.notes}

    if config_age < 0:
        logger.warning("[COMPLIANCE] %s - could not determine config age", device.name)
        compliance.notes = "unknown error calculating config age"
        compliance.save()
        return {device: compliance.notes}

    generated = compliance.get_generated_config().splitlines()
    logger.debug("[COMPLIANCE] %s - generated config: %d lines", device.name, len(generated))

    diff = get_config_diff(generated, device_config.splitlines())

    if not diff:
        logger.info("[COMPLIANCE] %s -> COMPLIANT ✓", device.name)
        compliance.diff = ""
        compliance.notes = None
        compliance.status = ServiceComplianceChoices.STATUS_COMPLIANCE
    else:
        logger.info("[COMPLIANCE] %s -> NON-COMPLIANT (%d missing lines)",
                    device.name, len(diff))
        compliance.diff = "\n".join("\n".join(line) for line in diff)
        compliance.notes = None
        compliance.status = ServiceComplianceChoices.STATUS_NON_COMPLIANCE

    compliance.save()
    return {device: compliance.status}


@job("default")
def collect_all_devices_configs() -> str:
    """Trigger collection for all devices in NetBox."""
    logger.info("[COLLECT] collect_all_devices_configs: starting global run")

    Collection.objects.all().delete()
    logger.debug("[COLLECT] Cleared existing Collection records")

    devices = list(Device.objects.all())
    count = len(devices)
    logger.info("[COLLECT] Devices to collect: %d", count)

    commit_msg = f"global_{datetime.now().strftime('%Y_%m_%d_%H_%M_%S')}"

    for device in devices:
        collect_task = Collection.objects.create(
            device=device, message=GLOBAL_TASK_INIT_MESSAGE
        )
        get_queue("default").enqueue(
            "config_officer.worker.collect_device_config_task",
            collect_task.pk,
            commit_msg,
        )
        logger.debug("[COLLECT] Enqueued %s (task_id=%d)", device.name, collect_task.pk)

    logger.info("[COLLECT] Enqueued %d tasks, commit_msg=%r", count, commit_msg)
    return f"queued {count} devices"