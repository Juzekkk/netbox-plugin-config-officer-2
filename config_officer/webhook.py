"""
Webhook delivery for CollectSchedule completion events.

Public API
----------
send_schedule_webhook_task(schedule_pk, commit_msg, collect_job_ids, since)
    RQ job entry point - waits for all collection jobs, then POSTs a summary.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
from datetime import datetime

import requests
from django_rq import get_connection
from git import NULL_TREE, InvalidGitRepositoryError, Repo
from rq.exceptions import NoSuchJobError
from rq.job import Job as RQJob

from .choices import CollectStatusChoices
from .config import CONFIGS_REPO_DIR, CONFIGS_SUBPATH, GIT_AUTHOR
from .git_utils import configure_safe_directory
from .models import Collection, CollectSchedule

logger = logging.getLogger(__name__)

_POLL_INTERVAL_SECONDS = 15
_POLL_TIMEOUT_SECONDS = 3600  # 1 hour


# ---------------------------------------------------------------------------
# RQ entry point
# ---------------------------------------------------------------------------


def send_schedule_webhook_task(
    schedule_pk: int,
    commit_msg: str,
    collect_job_ids: list[str],
    since: datetime | None,
) -> None:
    """
    RQ job: wait for all collection jobs to finish, then POST a webhook summary.

    Parameters
    ----------
    schedule_pk:
        PK of the CollectSchedule that triggered the run.
    commit_msg:
        Git commit message used for this schedule run (used only for the payload label).
    collect_job_ids:
        RQ job IDs of the individual per-device collect tasks to wait for.
    since:
        Completed timestamp of the previous schedule run.
        Diffs are limited to commits newer than this value.
        Pass None on the very first run to include only the latest commit per device.
    """
    _wait_for_jobs(collect_job_ids)

    try:
        schedule = CollectSchedule.objects.get(pk=schedule_pk)
    except CollectSchedule.DoesNotExist:
        logger.warning("[WEBHOOK] Schedule pk=%d no longer exists - aborting", schedule_pk)
        return

    if not schedule.webhook_url:
        logger.debug(
            "[WEBHOOK] No webhook URL configured for schedule %r - skipping", schedule.name
        )
        return

    tasks = _fetch_tasks(schedule.name, len(collect_job_ids))
    payload = _build_payload(schedule.name, commit_msg, tasks, since)
    _post_webhook(schedule.webhook_url, schedule.webhook_secret, payload)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _wait_for_jobs(collect_job_ids: list[str]) -> None:
    """Poll RQ until every job in *collect_job_ids* has finished or failed."""
    logger.info("[WEBHOOK] Waiting for %d collect job(s) to finish", len(collect_job_ids))

    connection = get_connection("default")
    deadline = time.monotonic() + _POLL_TIMEOUT_SECONDS

    while time.monotonic() < deadline:
        pending = _count_pending_jobs(collect_job_ids, connection)
        if pending == 0:
            logger.info("[WEBHOOK] All collect jobs finished")
            return
        logger.debug(
            "[WEBHOOK] %d job(s) still running - checking again in %ds",
            pending,
            _POLL_INTERVAL_SECONDS,
        )
        time.sleep(_POLL_INTERVAL_SECONDS)

    logger.warning("[WEBHOOK] Timed out after %ds - sending partial summary", _POLL_TIMEOUT_SECONDS)


def _count_pending_jobs(job_ids: list[str], connection) -> int:
    """Return the number of jobs that are still running or queued."""
    pending = 0
    for job_id in job_ids:
        try:
            rq_job = RQJob.fetch(job_id, connection=connection)
            if not (rq_job.is_finished or rq_job.is_failed):
                pending += 1
        except NoSuchJobError:
            pass  # job expired from RQ - treat as finished
    return pending


def _fetch_tasks(schedule_name: str, limit: int):
    """Return the most recent Collection records for this schedule run."""
    return (
        Collection.objects.filter(message=f"schedule:{schedule_name}")
        .order_by("-timestamp")[:limit]
        .select_related("device")
    )


def _build_payload(
    schedule_name: str,
    commit_msg: str,
    tasks,
    since: datetime | None,
) -> str:
    """Assemble and return the JSON payload string."""
    results = [
        {
            "device": t.device.name if t.device else "unknown",
            "status": t.status,
            "failed_reason": t.failed_reason or "",
        }
        for t in tasks
    ]

    succeeded = sum(1 for r in results if r["status"] == CollectStatusChoices.STATUS_SUCCEEDED)
    failed = len(results) - succeeded

    device_names = [t.device.name for t in tasks if t.device]
    diffs = _collect_git_diffs(device_names, since)

    return json.dumps(
        {
            "event": "schedule_complete",
            "schedule": schedule_name,
            "commit": commit_msg,
            "summary": {
                "total": len(results),
                "succeeded": succeeded,
                "failed": failed,
            },
            "results": results,
            "diffs": diffs,
        }
    )


def _post_webhook(url: str, secret: str, payload: str) -> None:
    """Sign and POST *payload* to *url*."""
    headers = {"Content-Type": "application/json"}

    if secret:
        sig = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
        headers["X-Webhook-Signature"] = f"sha256={sig}"

    try:
        resp = requests.post(url, data=payload, headers=headers, timeout=15)
        resp.raise_for_status()
        logger.info("[WEBHOOK] Delivered to %r - HTTP %d", url, resp.status_code)
    except requests.HTTPError as exc:
        logger.error("[WEBHOOK] HTTP error delivering to %r: %s", url, exc)
    except requests.RequestException as exc:
        logger.error("[WEBHOOK] Network error delivering to %r: %s", url, exc)


def _collect_git_diffs(device_names: list[str], since: datetime | None) -> list[dict]:
    """
    Return per-device git diffs.

    For each device the diff covers commits newer than *since*.
    When *since* is None (first run) only the latest commit is included.
    """
    configure_safe_directory(CONFIGS_REPO_DIR, GIT_AUTHOR)

    try:
        repo = Repo(CONFIGS_REPO_DIR)
    except InvalidGitRepositoryError:
        logger.error("[WEBHOOK] Not a git repository: %r", CONFIGS_REPO_DIR)
        return [{"error": f"not a git repository: {CONFIGS_REPO_DIR}"}]

    if not repo.head.is_valid():
        logger.info("[WEBHOOK] Repo has no commits yet")
        return []

    diffs = []
    for hostname in device_names:
        diffs.append(_diff_for_device(repo, hostname, since))
    return diffs


def _diff_for_device(repo: Repo, hostname: str, since: datetime | None) -> dict:
    """Return the diff entry for a single device."""
    filename = os.path.join(CONFIGS_SUBPATH, f"{hostname}_running.txt")
    all_commits = list(repo.iter_commits(paths=filename))

    if since is not None:
        matching = [c for c in all_commits if c.committed_datetime >= since]
    else:
        matching = all_commits[:1]  # first run - just the latest commit

    if not matching:
        logger.info("[WEBHOOK] No new commits for %s since %s", hostname, since)
        return {"device": hostname, "changed": False, "diff": ""}

    # The most recent matching commit represents the current state of this device.
    commit = matching[0]
    parent = commit.parents[0] if commit.parents else None
    base = parent if parent is not None else NULL_TREE

    diff_text = ""
    for diff_item in base.diff(commit, create_patch=True):
        a_path = diff_item.a_path or ""
        b_path = diff_item.b_path or ""
        if a_path.endswith(filename) or b_path.endswith(filename):
            diff_text = diff_item.diff.decode("utf-8", errors="ignore") if diff_item.diff else ""
            break

    return {
        "device": hostname,
        "changed": bool(diff_text),
        "commit_hash": commit.hexsha,
        "commit_date": commit.committed_datetime.strftime("%Y-%m-%d %H:%M"),
        "diff": diff_text,
    }
