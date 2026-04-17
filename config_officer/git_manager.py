"""Manage data in local git repository."""

import os
import time
from datetime import datetime
from git import Repo


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
    """Get commits and diffs for a file using GitPython."""

    repo_state = {
        "commits_count": 0,
        "commits": []
    }

    try:
        repo = Repo(repository_path)

        commits = list(repo.iter_commits(paths=filename))
        commits.reverse()  # oldest → newest

        repo_state["commits_count"] = len(commits)

        if commits:
            repo_state["first_commit_date"] = commits[0].committed_datetime.strftime("%d %b %Y %H:%M")
            repo_state["last_commit_date"] = commits[-1].committed_datetime.strftime("%d %b %Y %H:%M")

            for commit in commits:
                diff_text = None

                # compute diff for this file in this commit
                if commit.parents:
                    diffs = commit.diff(commit.parents[0], paths=filename)
                else:
                    diffs = commit.diff(NULL_TREE := None)

                for diff in diffs:
                    if diff.a_path == filename or diff.b_path == filename:
                        try:
                            diff_text = diff.diff.decode("utf-8", errors="ignore")
                        except:
                            diff_text = str(diff.diff)

                repo_state["commits"].append({
                    "hash": commit.hexsha,
                    "msg": commit.message.strip(),
                    "diff": diff_text,
                    "date": commit.committed_datetime,
                })

        else:
            repo_state["comment"] = f"no commits changes for {filename}"

    except Exception as e:
        repo_state["error"] = str(e)

    return repo_state