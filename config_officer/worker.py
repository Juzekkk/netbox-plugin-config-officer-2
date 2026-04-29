"""
config_officer worker tasks.

RQ job entry points:
  collect_device_config_hostname   - trigger single-device collection by name
  collect_device_config_task       - actual collect worker (called by the above)
  git_commit_configs_changes       - stage / commit / push collected configs
  check_device_config_compliance   - diff generated vs running config
  collect_all_devices_configs      - enqueue collection for every device
"""

from __future__ import annotations

import ipaddress
import logging
import os
import tempfile
import time
from datetime import datetime

from dcim.models import Device
from django.db.models import Q
from django_rq import get_queue, job
from git import GitCommandError, InvalidGitRepositoryError, NoSuchPathError, Repo
from git.exc import GitCommandNotFound

from .choices import CollectFailChoices, CollectStatusChoices, ServiceComplianceChoices
from .collector import CollectDeviceData
from .config import (
    CF_COLLECTION_STATUS,
    CONFIGS_PATH,
    CONFIGS_REPO_DIR,
    CONFIGS_SUBPATH,
    DEFAULT_PLATFORM,
    GIT_AUTHOR,
    GIT_REMOTE_BRANCH,
    GIT_REMOTE_ENABLED,
    GIT_REMOTE_KEY,
    GIT_REMOTE_NAME,
    GIT_REMOTE_URL,
    VOLATILE_LINE_PATTERNS_COMPILED,
)
from .config_manager import get_config_diff
from .custom_exceptions import CollectionException
from .git_manager import get_days_after_update, get_device_config, get_device_file_repo_state
from .git_utils import configure_safe_directory
from .models import Collection, Compliance, ServiceMapping

GLOBAL_TASK_INIT_MESSAGE: str = "global_collection_task"

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------


def _strip_volatile_lines(text: str) -> str:
    """Remove timestamp / metadata lines before comparing two config versions."""
    return "\n".join(
        line.strip()
        for line in text.splitlines()
        if not any(p.search(line) for p in VOLATILE_LINE_PATTERNS_COMPILED)
    )


def _prepare_ssh_key(key_path: str) -> str:
    """
    Copy SSH key to a temp file with correct permissions and trailing newline.
    OpenSSH requires a newline at the end of the key file.
    The key mounted from Kubernetes secret may lack it due to AVP stripping
    trailing newlines from Vault values.
    """
    with open(key_path, "rb") as f:
        data = f.read()
    if not data.endswith(b"\n"):
        logger.info("[GIT] Adding trailing newline to SSH key")
        data += b"\n"
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pem", mode="wb")  # noqa: SIM115
    tmp.write(data)
    tmp.close()
    os.chmod(tmp.name, 0o600)
    return tmp.name


def _get_ssh_env(key_path: str | None) -> dict[str, str]:
    """
    Build GIT_SSH_COMMAND that uses the specified key and disables interactive prompts.
    Returns an empty dict when no key is configured.
    """
    if not key_path:
        return {}
    prepared_key = _prepare_ssh_key(key_path)
    cmd = (
        f"ssh -i {prepared_key}"
        " -o IdentitiesOnly=yes"
        " -o StrictHostKeyChecking=accept-new"
        " -o UserKnownHostsFile=/dev/null"
        " -o BatchMode=yes"
        " -o ConnectTimeout=15"
    )
    logger.info("[GIT] GIT_SSH_COMMAND: %s", cmd)
    return {"GIT_SSH_COMMAND": cmd}


def _apply_ssh_env(key_path: str | None) -> None:
    os.environ.update(_get_ssh_env(key_path))


def get_active_collect_task_count() -> int:
    """Return the number of pending/running global collection tasks."""
    return Collection.objects.filter(
        Q(status__iexact=CollectStatusChoices.STATUS_PENDING)
        | Q(status__iexact=CollectStatusChoices.STATUS_RUNNING),
        message__iexact=GLOBAL_TASK_INIT_MESSAGE,
    ).count()


def _set_collection_status(device_nb, value: bool) -> None:
    """Persist the collection-status custom field for *device_nb*."""
    device_nb.custom_field_data[CF_COLLECTION_STATUS] = value
    device_nb.save()


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------


def _open_or_init_repo() -> tuple[Repo, bool]:
    """
    Open an existing repo or initialise a new one.
    safe.directory MUST be configured before calling this.
    Returns (repo, is_new).
    """
    logger.info("[GIT] Opening repo at %r", CONFIGS_REPO_DIR)
    try:
        repo = Repo(CONFIGS_REPO_DIR)
        is_new = not repo.head.is_valid()
        sha = repo.head.commit.hexsha[:8] if repo.head.is_valid() else "none"
        logger.info(
            "[GIT] Opened existing repo at %r (HEAD=%s, is_new=%s)", CONFIGS_REPO_DIR, sha, is_new
        )
        return repo, is_new
    except InvalidGitRepositoryError:
        logger.info("[GIT] No valid repo - initialising")
        os.makedirs(CONFIGS_PATH, exist_ok=True)
        repo = Repo.init(CONFIGS_REPO_DIR)
        return repo, True
    except NoSuchPathError:
        logger.info("[GIT] Path does not exist - creating and initialising")
        os.makedirs(CONFIGS_PATH, exist_ok=True)
        repo = Repo.init(CONFIGS_REPO_DIR)
        return repo, True


def _ensure_branch(repo: Repo) -> None:
    """Ensure GIT_REMOTE_BRANCH is checked out, creating it if needed."""
    try:
        current = repo.active_branch.name
    except TypeError:
        current = None  # detached HEAD

    if current == GIT_REMOTE_BRANCH:
        return

    if GIT_REMOTE_BRANCH in repo.heads:
        logger.info("[GIT] Checking out branch '%s'", GIT_REMOTE_BRANCH)
        repo.git.checkout(GIT_REMOTE_BRANCH)
        return

    try:
        repo.git.checkout("-b", GIT_REMOTE_BRANCH, f"origin/{GIT_REMOTE_BRANCH}")
        logger.info("[GIT] Checked out branch '%s' from origin", GIT_REMOTE_BRANCH)
    except Exception:
        logger.warning("[GIT] Remote branch '%s' not found - creating locally", GIT_REMOTE_BRANCH)
        repo.git.checkout("-b", GIT_REMOTE_BRANCH)


def _ensure_remote(repo: Repo) -> bool:
    """
    Make sure the configured remote exists with the right URL.
    Returns True when a remote is present and push is enabled.
    """
    if not GIT_REMOTE_ENABLED or not GIT_REMOTE_URL:
        return False
    remote_names = [r.name for r in repo.remotes]
    if GIT_REMOTE_NAME not in remote_names:
        logger.info("[GIT] Adding remote %r -> %s", GIT_REMOTE_NAME, GIT_REMOTE_URL)
        repo.create_remote(GIT_REMOTE_NAME, GIT_REMOTE_URL)
    return True


def _initial_pull(repo: Repo) -> None:
    """
    On first repo init: fetch remote history and reset local branch to match.
    Uses fetch + reset instead of pull to cleanly handle untracked local files.
    """
    if not GIT_REMOTE_ENABLED or not GIT_REMOTE_URL:
        return
    _apply_ssh_env(GIT_REMOTE_KEY)
    try:
        logger.info("[GIT] Fetching from remote %r", GIT_REMOTE_NAME)
        repo.remotes[GIT_REMOTE_NAME].fetch()
        logger.info("[GIT] Fetch complete")
        remote_refs = [r.name for r in repo.remotes[GIT_REMOTE_NAME].refs]
        logger.info("[GIT] Remote refs: %s", remote_refs)
        if f"{GIT_REMOTE_NAME}/{GIT_REMOTE_BRANCH}" in remote_refs:
            logger.info("[GIT] Checking out remote branch %r", GIT_REMOTE_BRANCH)
            repo.git.checkout("-B", GIT_REMOTE_BRANCH, f"{GIT_REMOTE_NAME}/{GIT_REMOTE_BRANCH}")
            logger.info("[GIT] Reset to remote branch complete")
        else:
            logger.info(
                "[GIT] Remote branch %r not found - will create on first push", GIT_REMOTE_BRANCH
            )
    except GitCommandError as exc:
        logger.warning("[GIT] Initial fetch failed: %s", exc)
    except Exception:
        logger.exception("[GIT] Unexpected error during initial fetch")


def _push_to_remote(repo: Repo) -> str:
    """Push committed changes. Returns a short status string."""
    _apply_ssh_env(GIT_REMOTE_KEY)
    try:
        results = repo.remotes[GIT_REMOTE_NAME].push(GIT_REMOTE_BRANCH)
        for info in results:
            logger.info(
                "[GIT] Push result: flags=%s summary=%r",
                info.flags,
                info.summary.strip(),
            )
        return "pushed"
    except GitCommandError as exc:
        logger.error("[GIT] Push failed: %s", exc)
        return f"push_failed:{exc}"
    except Exception:
        logger.exception("[GIT] Push failed (unexpected)")
        return "push_failed:unexpected"


def _evaluate_staged_files(repo: Repo) -> tuple[list[str], list[str]]:
    """
    For each staged file decide whether it contains real config changes or
    only volatile-line (timestamp) changes.

    Timestamp-only files are restored from HEAD so they are not committed.
    Returns (real_changes, timestamp_only).
    """
    real_changes: list[str] = []
    timestamp_only: list[str] = []

    for diff_item in repo.index.diff("HEAD"):
        path = diff_item.b_path or diff_item.a_path
        logger.debug("[GIT] Evaluating: %s", path)

        # Full path preserving subdirectory structure
        abs_path = os.path.join(CONFIGS_REPO_DIR, path)

        try:
            with open(abs_path, errors="replace") as fh:
                new_text = fh.read()
        except FileNotFoundError:
            logger.debug("[GIT] %s deleted -> real change", path)
            real_changes.append(path)
            continue

        try:
            old_text = repo.git.show(f"HEAD:{path}")
        except GitCommandError:
            logger.debug("[GIT] %s is new (no HEAD) -> real change", path)
            real_changes.append(path)
            continue

        if _strip_volatile_lines(new_text) == _strip_volatile_lines(old_text):
            logger.debug("[GIT] %s - only timestamps changed, restoring from HEAD", path)
            try:
                repo.git.checkout("HEAD", "--", path)
                timestamp_only.append(path)
            except GitCommandError:
                logger.warning("[GIT] Could not restore %s - keeping as staged", path)
                real_changes.append(path)
        else:
            logger.debug("[GIT] %s - real change detected", path)
            real_changes.append(path)

    return real_changes, timestamp_only


def _make_initial_commit(repo: Repo, msg: str, has_remote: bool) -> str:
    """Commit everything in a brand-new repo and optionally push."""
    repo.git.add("--all")
    staged = repo.git.diff("--cached", "--name-only")
    logger.info("[GIT] Initial commit staged files: %r", staged)
    if not staged:
        logger.info("[GIT] Nothing staged")
        return "initial: nothing to commit"
    repo.git.commit("-m", msg, author=GIT_AUTHOR)
    logger.info("[GIT] Initial commit done")
    if has_remote:
        _apply_ssh_env(GIT_REMOTE_KEY)
        results = repo.remotes[GIT_REMOTE_NAME].push(GIT_REMOTE_BRANCH)
        for info in results:
            logger.info("[GIT] Push: flags=%s summary=%r", info.flags, info.summary.strip())
        return "initial commit+pushed"
    return "initial commit"


def _ensure_repo_ready() -> None:
    """
    Ensure the local git repo exists, is on the correct branch,
    and is up to date with remote. Called before each collect task.
    """
    configure_safe_directory(CONFIGS_REPO_DIR, GIT_AUTHOR)

    logger.info("[GIT] Ensuring repo is ready at %r", CONFIGS_REPO_DIR)

    try:
        try:
            repo = Repo(CONFIGS_REPO_DIR)
            logger.info("[GIT] Repo exists at %r", CONFIGS_REPO_DIR)
        except (InvalidGitRepositoryError, NoSuchPathError):
            logger.info("[GIT] Initialising new repo at %r", CONFIGS_REPO_DIR)
            os.makedirs(CONFIGS_PATH, exist_ok=True)
            repo = Repo.init(CONFIGS_REPO_DIR)

        _ensure_remote(repo)

        if not GIT_REMOTE_ENABLED or not GIT_REMOTE_URL:
            logger.info("[GIT] Remote disabled - skipping fetch")
            if not repo.head.is_valid():
                _ensure_branch(repo)
            return

        _apply_ssh_env(GIT_REMOTE_KEY)

        logger.info("[GIT] Fetching from remote %r", GIT_REMOTE_NAME)
        repo.remotes[GIT_REMOTE_NAME].fetch()
        logger.info("[GIT] Fetch complete")

        remote_refs = [r.name for r in repo.remotes[GIT_REMOTE_NAME].refs]
        target = f"{GIT_REMOTE_NAME}/{GIT_REMOTE_BRANCH}"

        if target in remote_refs:
            logger.info("[GIT] Checking out %r from remote", GIT_REMOTE_BRANCH)
            repo.git.checkout("-B", GIT_REMOTE_BRANCH, target)
            logger.info(
                "[GIT] Now on branch %r at HEAD=%s",
                GIT_REMOTE_BRANCH,
                repo.head.commit.hexsha[:8],
            )
        else:
            logger.info(
                "[GIT] Remote branch %r not found - ensuring local branch", GIT_REMOTE_BRANCH
            )
            _ensure_branch(repo)

    except GitCommandError as exc:
        logger.warning("[GIT] _ensure_repo_ready failed: %s - continuing anyway", exc)
    except Exception:
        logger.exception("[GIT] Unexpected error in _ensure_repo_ready - continuing anyway")


# ---------------------------------------------------------------------------
# RQ jobs
# ---------------------------------------------------------------------------


@job("default")
def collect_device_config_hostname(hostname: str) -> None:
    """Trigger collection for a single device by hostname."""
    logger.info("[COLLECT] collect_device_config_hostname: %r", hostname)
    device = Device.objects.get(name__iexact=hostname)
    collect_task = Collection.objects.create(device=device, message="device collection task")
    commit_msg = f"device_{hostname}_{datetime.now().strftime('%Y_%m_%d_%H_%M_%S')}"

    get_queue("default").enqueue(
        "config_officer.worker.collect_device_config_task",
        collect_task.pk,
        commit_msg,
    )
    logger.info("[COLLECT] Enqueued task id=%d commit=%r", collect_task.pk, commit_msg)


@job("default")
def collect_device_config_task(task_id: int, commit_msg: str = "") -> str:  # noqa: PLR0915
    """Collect running config from a single device and persist results to NetBox."""
    logger.info("[COLLECT] Task start: task_id=%d commit=%r", task_id, commit_msg)

    # Give the DB a moment to flush the Collection record written by the caller
    time.sleep(1)
    try:
        collect_task = Collection.objects.get(id=task_id)
    except Collection.DoesNotExist:
        logger.warning("[COLLECT] Collection id=%d not found, retrying in 5s", task_id)
        time.sleep(5)
        collect_task = Collection.objects.get(id=task_id)  # propagates if still missing

    collect_task.status = CollectStatusChoices.STATUS_RUNNING
    collect_task.save()

    if not commit_msg:
        commit_msg = datetime.now().strftime("%Y_%m_%d_%H_%M_%S")

    def _maybe_enqueue_commit() -> None:
        if get_active_collect_task_count() < 11:
            get_queue("default").enqueue(
                "config_officer.worker.git_commit_configs_changes", commit_msg
            )
            logger.debug("[COLLECT] Enqueued git commit: %r", commit_msg)

    device_nb = collect_task.device
    ip = "unknown"

    try:
        platform = device_nb.platform.name if device_nb.platform is not None else DEFAULT_PLATFORM
        logger.debug("[COLLECT] Device=%s platform=%r", device_nb.name, platform)

        if not device_nb.primary_ip4:
            raise CollectionException(
                reason=CollectFailChoices.FAIL_CONNECT,
                message=f"Device {device_nb.name!r} has no Primary IPv4 set in NetBox.",
            )

        ip = str(ipaddress.ip_interface(device_nb.primary_ip4).ip)
        logger.debug("[COLLECT] Primary IP: %s", ip)

        # Ensure git repo is ready and up to date before collecting
        _ensure_repo_ready()

        # Mark in-progress *before* the potentially-long SSH session
        _set_collection_status(device_nb, False)

        CollectDeviceData(
            collect_task,
            ip=ip,
            hostname_ipam=str(device_nb.name),
            platform=platform,
        ).collect_information()

        logger.info("[COLLECT] collect_information() OK for %s", device_nb.name)

    except CollectionException as exc:
        logger.error(
            "[COLLECT] CollectionException: reason=%r message=%r",
            exc.reason,
            exc.message,
        )
        collect_task.status = CollectStatusChoices.STATUS_FAILED
        collect_task.failed_reason = exc.reason
        collect_task.message = exc.message
        collect_task.save()
        _maybe_enqueue_commit()
        raise

    except Exception as exc:
        logger.exception("[COLLECT] Unexpected error: %s", exc)
        collect_task.status = CollectStatusChoices.STATUS_FAILED
        collect_task.failed_reason = CollectFailChoices.FAIL_GENERAL
        collect_task.message = f"Unknown error: {exc}"
        collect_task.save()
        _maybe_enqueue_commit()
        raise

    # Success path
    collect_task.status = CollectStatusChoices.STATUS_SUCCEEDED
    collect_task.save()
    _set_collection_status(device_nb, True)
    logger.info("[COLLECT] SUCCESS: %s (%s)", device_nb.name, ip)

    try:
        get_queue("default").enqueue(
            "config_officer.worker.check_device_config_compliance",
            device=device_nb,
        )
    except Exception:
        logger.exception("[COLLECT] Failed to enqueue compliance check for %s", device_nb.name)

    _maybe_enqueue_commit()
    return f"{device_nb.name} {ip} collected"


@job("default")
def git_commit_configs_changes(msg: str) -> str:  # noqa: PLR0911
    """
    Stage all changed configs and commit only if real config lines changed.
    Volatile timestamp-only changes are filtered out before committing.

    Flow:
      1. Open or init the local git repo
      2. Configure remote and do initial pull on first run
      3. Stage all files
      4. Per-file: compare stripped content vs HEAD
         - only timestamps changed  →  restore from HEAD, skip
         - real change              →  keep staged
      5. Commit if anything remains staged
      6. Push to remote (if enabled)
    """
    logger.info("[GIT] git_commit_configs_changes: msg=%r", msg)

    if get_active_collect_task_count() > 0:
        logger.info("[GIT] Active collect tasks running - deferring commit")
        return "deferred: active collect tasks"

    try:
        configure_safe_directory(CONFIGS_REPO_DIR, GIT_AUTHOR)

        repo, is_new = _open_or_init_repo()
        has_remote = _ensure_remote(repo)

        _ensure_branch(repo)

        # Truly empty repo - do an initial commit and exit
        if not repo.head.is_valid():
            return _make_initial_commit(repo, msg, has_remote)

        # Stage everything
        repo.git.add("--all")
        staged = repo.index.diff("HEAD")
        logger.debug("[GIT] Files staged vs HEAD: %d", len(staged))

        if not staged:
            logger.info("[GIT] Nothing staged - no commit needed")
            return "no changes"

        real_changes, timestamp_only = _evaluate_staged_files(repo)
        logger.info(
            "[GIT] Real: %d file(s) | Timestamp-only (skipped): %d file(s)",
            len(real_changes),
            len(timestamp_only),
        )

        if not repo.index.diff("HEAD"):
            logger.info("[GIT] No real changes remain - commit skipped")
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

    compliance, _ = Compliance.objects.get_or_create(device=device)
    compliance.status = ServiceComplianceChoices.STATUS_NON_COMPLIANCE
    compliance.notes = "not checked yet"
    compliance.generated_config = "None"
    compliance.diff = "None"
    compliance.save()
    compliance.services = [
        m.service.name for m in ServiceMapping.objects.filter(device=compliance.device)
    ]

    templates = compliance.get_device_templates()
    if not templates:
        logger.info("[COMPLIANCE] %s - no matched templates", device.name)
        compliance.notes = "No matched templates"
        compliance.save()
        return {device: compliance.notes}

    logger.debug("[COMPLIANCE] %s - matched: %s", device.name, [t.name for t in templates])

    device_config = get_device_config(CONFIGS_PATH, device.name, "running")
    if not device_config:
        logger.warning("[COMPLIANCE] %s - running config file not found", device.name)
        compliance.notes = "running config not found in git"
        compliance.save()
        return {device: compliance.notes}

    config_age = get_days_after_update(CONFIGS_PATH, device.name, "running")
    logger.debug("[COMPLIANCE] %s - config age: %d day(s)", device.name, config_age)

    if config_age < 0:
        compliance.notes = "unknown error calculating config age"
        compliance.save()
        logger.warning("[COMPLIANCE] %s - could not determine config age", device.name)
        return {device: compliance.notes}

    if config_age > 7:
        msg = f"config is stale ({config_age} days)"
        compliance.notes = msg
        compliance.save()
        logger.warning("[COMPLIANCE] %s - %s", device.name, msg)
        return {device: compliance.notes}

    generated = compliance.get_generated_config().splitlines()
    logger.debug("[COMPLIANCE] %s - generated: %d lines", device.name, len(generated))

    diff = get_config_diff(generated, device_config.splitlines())

    if not diff:
        logger.info("[COMPLIANCE] %s -> COMPLIANT", device.name)
        compliance.status = ServiceComplianceChoices.STATUS_COMPLIANCE
        compliance.diff = ""
        compliance.notes = None
    else:
        logger.info(
            "[COMPLIANCE] %s -> NON-COMPLIANT (%d missing lines)",
            device.name,
            len(diff),
        )
        compliance.status = ServiceComplianceChoices.STATUS_NON_COMPLIANCE
        compliance.diff = "\n".join("\n".join(line) for line in diff)
        compliance.notes = None

    compliance.save()
    return {device: compliance.status}


@job("default")
def collect_all_devices_configs() -> str:
    """Enqueue config collection for every device in NetBox."""
    logger.info("[COLLECT] collect_all_devices_configs: starting global run")

    # Only remove completed/failed records - don't discard tasks still in flight
    Collection.objects.filter(
        status__in=[
            CollectStatusChoices.STATUS_SUCCEEDED,
            CollectStatusChoices.STATUS_FAILED,
        ]
    ).delete()
    logger.debug("[COLLECT] Cleared finished Collection records")

    devices = list(Device.objects.all())
    commit_msg = f"global_{datetime.now().strftime('%Y_%m_%d_%H_%M_%S')}"

    for device in devices:
        collect_task = Collection.objects.create(device=device, message=GLOBAL_TASK_INIT_MESSAGE)
        get_queue("default").enqueue(
            "config_officer.worker.collect_device_config_task",
            collect_task.pk,
            commit_msg,
        )
        logger.debug("[COLLECT] Enqueued %s (task_id=%d)", device.name, collect_task.pk)

    logger.info("[COLLECT] Enqueued %d tasks, commit=%r", len(devices), commit_msg)
    return f"queued {len(devices)} devices"


@job("default")
def get_device_running_config(hostname: str) -> str | None:
    """Return running config text for hostname, read by worker from its local fs."""
    configure_safe_directory(CONFIGS_REPO_DIR, GIT_AUTHOR)
    return get_device_config(CONFIGS_PATH, hostname, "running")


@job("default")
def get_device_repo_state(hostname: str) -> dict:
    """Return git repo state for hostname, read by worker from its local repo."""
    configure_safe_directory(CONFIGS_REPO_DIR, GIT_AUTHOR)
    return get_device_file_repo_state(CONFIGS_REPO_DIR, CONFIGS_SUBPATH, hostname, "running")
