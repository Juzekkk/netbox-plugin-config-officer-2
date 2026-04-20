"""Manage data in local git repository."""

import os
import time
from datetime import datetime
from git import Repo, NULL_TREE
import logging

logger = logging.getLogger(__name__)


def get_device_config(directory, hostname, config_type="running"):
    try:
        with open(f"{directory}/{hostname}_{config_type}.txt", "r") as file:
            return file.read()
    except FileNotFoundError:
        return None


def get_days_after_update(directory, hostname, config_type="running"):
    try:
        create_time = os.stat(f"{directory}/{hostname}_{config_type}.txt").st_ctime
        return round((time.time() - create_time) / 86400)
    except:
        return -1


def get_config_update_date(directory, hostname, config_type="running"):
    try:
        create_time = os.stat(f"{directory}/{hostname}_{config_type}.txt").st_ctime
        return datetime.fromtimestamp(create_time).strftime("%Y-%m-%d %H:%M")
    except:
        return "unknown"


def get_file_repo_state(repository_path, filename):
    repo_state = {
        "commits_count": 0,
        "commits": []
    }

    try:
        repo = Repo(repository_path)

        logger.debug(f"[GIT] Repo loaded: {repository_path}")
        logger.debug(f"[GIT] HEAD: {repo.head.commit.hexsha}")
        logger.debug(f"[GIT] Active branch: {repo.active_branch.name}")

        commits = list(repo.iter_commits(paths=filename))
        commits.reverse()

        repo_state["commits_count"] = len(commits)

        logger.debug(f"[GIT] Found commits: {len(commits)} for {filename}")

        if not commits:
            repo_state["comment"] = f"no commits changes for {filename}"
            return repo_state

        repo_state["first_commit_date"] = commits[0].committed_datetime.strftime("%d %b %Y %H:%M")
        repo_state["last_commit_date"] = commits[-1].committed_datetime.strftime("%d %b %Y %H:%M")

        for i, commit in enumerate(commits):
            parent = commit.parents[0] if commit.parents else None

            logger.debug("====================================")
            logger.debug(f"[GIT] COMMIT [{i+1}/{len(commits)}]: {commit.hexsha}")
            logger.debug(f"[GIT] PARENT: {parent.hexsha if parent else 'NONE (ROOT)'}")

            if parent:
                logger.debug(f"[GIT] PARENT TREE: {parent.tree.hexsha}")
            logger.debug(f"[GIT] COMMIT TREE: {commit.tree.hexsha}")

            diff_text = ""

            try:
                if parent:
                    diff_index = parent.diff(commit, create_patch=True)
                    logger.debug(f"[GIT] DIFF MODE: parent -> commit ({commit.hexsha})")
                else:
                    diff_index = parent.diff(commit, create_patch=True)
                    logger.debug(f"[GIT] DIFF MODE: NULL_TREE -> commit ({commit.hexsha})")

                logger.debug(f"[GIT] RAW DIFF COUNT: {len(diff_index)}")

                for idx, diff in enumerate(diff_index):
                    a_path = diff.a_path or ""
                    b_path = diff.b_path or ""

                    logger.debug(
                        f"[GIT] DIFF[{idx}] "
                        f"a_path={a_path} | b_path={b_path} | "
                        f"change_type={diff.change_type}"
                    )

                    logger.debug(f"[GIT] filename match check: {filename}")

                    if (
                        a_path.endswith(filename)
                        or b_path.endswith(filename)
                    ):
                        if diff.diff:
                            try:
                                decoded = diff.diff.decode("utf-8", errors="ignore")
                                diff_text += decoded

                                logger.debug(
                                    f"[GIT] DIFF[{idx}] MATCHED FILE, SIZE={len(decoded)} chars"
                                )
                            except Exception as e:
                                logger.exception(f"[GIT] decode error in commit {commit.hexsha}")
                                diff_text += str(diff.diff)
                        else:
                            logger.warning(f"[GIT] DIFF[{idx}] EMPTY diff.diff")

                if not diff_text:
                    logger.warning(f"[GIT] Empty FINAL diff for commit {commit.hexsha}")

            except Exception as e:
                logger.exception(f"[GIT] Diff error for commit {commit.hexsha}")
                diff_text = f"diff error: {e}"

            repo_state["commits"].append({
                "hash": commit.hexsha,
                "msg": commit.message.strip(),
                "diff": diff_text,
                "date": commit.committed_datetime,
            })

        logger.debug("[GIT] Repo state build complete")

    except Exception as e:
        logger.exception("[GIT] Fatal error in get_file_repo_state")
        repo_state["error"] = str(e)

    return repo_state