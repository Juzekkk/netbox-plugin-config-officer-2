"""Manage data in local git repository."""

from __future__ import annotations

import os
import time
from datetime import datetime

from git import NULL_TREE, GitCommandError, InvalidGitRepositoryError, Repo
from git.objects.commit import Commit
from .config import (
    CONFIGS_REPO_DIR,
)

import logging

logger = logging.getLogger(__name__)

def _ensure_safe_directory(repo: Repo) -> None:
    """
    Mark the repo path as safe.directory in the global git config for the
    current process user.  Required when the directory is owned by a different
    uid (e.g. written by the worker container, read by the netbox web container).
    """
    repo_path = os.path.abspath(repo.working_tree_dir)
    try:
        existing = repo.git.config(
            "--get-all", "safe.directory"
        ).splitlines()
    except GitCommandError:
        existing = []

    if repo_path not in existing:
        try:
            repo.git.config("--add", "safe.directory", repo_path)
            logger.debug("[GIT] Marked safe.directory: %s", repo_path)
        except Exception:
            logger.exception("[GIT] Failed to set safe.directory for %s", repo_path)


# ---------------------------------------------------------------------------
# File-system helpers (no git required)
# ---------------------------------------------------------------------------


def get_device_config(
    directory: str, hostname: str, config_type: str = "running"
) -> str | None:
    """Return the text of a saved device config file, or None if absent."""
    path = os.path.join(directory, f"{hostname}_{config_type}.txt")
    try:
        with open(path) as fh:
            return fh.read()
    except FileNotFoundError:
        return None


def get_days_after_update(
    directory: str, hostname: str, config_type: str = "running"
) -> int:
    """
    Return how many days ago the config file was last written.
    Returns -1 on any error (file missing, permission denied, …).
    """
    path = os.path.join(directory, f"{hostname}_{config_type}.txt")
    try:
        mtime = os.stat(path).st_mtime
        return round((time.time() - mtime) / 86400)
    except OSError:
        return -1


def get_config_update_date(
    directory: str, hostname: str, config_type: str = "running"
) -> str:
    """Return a human-readable last-modified date for the config file."""
    path = os.path.join(directory, f"{hostname}_{config_type}.txt")
    try:
        mtime = os.stat(path).st_mtime
        return datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
    except OSError:
        return "unknown"


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------


def _diff_for_commit(commit: Commit, filename: str) -> str:
    """
    Return the unified-diff text for *filename* in *commit*.

    For root commits (no parent) the diff is computed against NULL_TREE.
    For ordinary commits it is computed against the first parent.
    """
    parent = commit.parents[0] if commit.parents else None
    base = parent if parent is not None else NULL_TREE

    diff_index = base.diff(commit, create_patch=True)

    for diff in diff_index:
        a_path = diff.a_path or ""
        b_path = diff.b_path or ""
        if a_path.endswith(filename) or b_path.endswith(filename):
            if not diff.diff:
                logger.warning(
                    "[GIT] Empty patch for %s in %s", filename, commit.hexsha[:8]
                )
                return ""
            return diff.diff.decode("utf-8", errors="ignore")

    logger.warning("[GIT] File %s not found in diff of %s", filename, commit.hexsha[:8])
    return ""


def get_file_repo_state(repository_path: str, filename: str) -> dict:
    """
    Return the full commit history and per-commit diffs for *filename*
    inside the git repository rooted at *repository_path*.

    Return value schema::

        {
            "commits_count": int,
            "commits": [
                {
                    "hash": str,
                    "msg":  str,
                    "diff": str,
                    "date": datetime,
                },
                ...
            ],
            # present only on success and when commits exist:
            "first_commit_date": str,
            "last_commit_date":  str,
            # present only on error:
            "error":   str,
            "comment": str,
        }
    """
    os.environ["GIT_CONFIG_COUNT"] = "1"
    os.environ["GIT_CONFIG_KEY_0"] = "safe.directory"
    os.environ["GIT_CONFIG_VALUE_0"] = CONFIGS_REPO_DIR
    repo_state: dict = {"commits_count": 0, "commits": []}

    try:
        try:
            repo = Repo(repository_path)
        except InvalidGitRepositoryError:
            logger.error("[GIT] Not a git repository: %s", repository_path)
            repo_state["error"] = f"not a git repository: {repository_path}"
            return repo_state

        # Ensure this process trusts the repo regardless of directory ownership.
        # The worker container sets safe.directory for itself, but the netbox
        # web container is a separate process (different uid) and needs it too.
        _ensure_safe_directory(repo)

        head_sha = repo.head.commit.hexsha[:8] if repo.head.is_valid() else "none"
        branch = repo.active_branch.name if not repo.head.is_detached else "detached"
        logger.debug(
            "[GIT] Repo loaded: %s  HEAD=%s  branch=%s",
            repository_path,
            head_sha,
            branch,
        )

        if not repo.head.is_valid():
            logger.info("[GIT] Repo has no commits yet")
            repo_state["comment"] = "repository has no commits yet"
            return repo_state

        commits = list(repo.iter_commits(paths=filename))
        commits.reverse()

        repo_state["commits_count"] = len(commits)
        logger.debug("[GIT] %d commit(s) found for %s", len(commits), filename)

        if not commits:
            repo_state["comment"] = f"no commits for {filename}"
            return repo_state

        repo_state["first_commit_date"] = commits[0].committed_datetime.strftime(
            "%d %b %Y %H:%M"
        )
        repo_state["last_commit_date"] = commits[-1].committed_datetime.strftime(
            "%d %b %Y %H:%M"
        )

        for i, commit in enumerate(commits):
            logger.debug(
                "[GIT] Processing commit [%d/%d] %s",
                i + 1,
                len(commits),
                commit.hexsha[:8],
            )
            try:
                diff_text = _diff_for_commit(commit, filename)
            except Exception:
                logger.exception("[GIT] Diff error for commit %s", commit.hexsha[:8])
                diff_text = f"diff error: see logs"

            repo_state["commits"].append(
                {
                    "hash": commit.hexsha,
                    "msg": commit.message.strip(),
                    "diff": diff_text,
                    "date": commit.committed_datetime,
                }
            )

        logger.debug("[GIT] Repo state build complete (%d commits)", len(commits))

    except Exception:
        logger.exception("[GIT] Fatal error in get_file_repo_state")
        repo_state["error"] = "unexpected error: see logs"

    return repo_state


def get_device_file_repo_state(
    repo_dir: str,
    configs_subpath: str,
    hostname: str,
    config_type: str = "running",
) -> dict:
    """
    Convenience wrapper around :func:`get_file_repo_state` for the standard
    config_officer file layout::

        <repo_dir>/<configs_subpath>/<hostname>_<config_type>.txt
    """
    relative_path = os.path.join(configs_subpath, f"{hostname}_{config_type}.txt")
    return get_file_repo_state(repo_dir, relative_path)
