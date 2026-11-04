"""ConfigRepository against real git repositories in a temporary directory."""

import subprocess

import pytest

from config_officer.configuration import load_settings
from config_officer.storage import ConfigRepository, StorageError

VOLATILE = load_settings({}).volatile_regexes


def git(*args, cwd):
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def remote(tmp_path):
    path = tmp_path / "remote.git"
    git("init", "--bare", "-q", str(path), cwd=tmp_path)
    return path


def make_repo(path, remote_url=None):
    settings = load_settings({"GIT_REMOTE": {"URL": str(remote_url)}} if remote_url else {})
    return ConfigRepository(path, remote=settings.git_remote, volatile_regexes=VOLATILE)


def test_write_commit_and_history(tmp_path):
    repo = make_repo(tmp_path / "configs")
    repo.prepare()
    assert repo.write_config("sw1", "hostname sw1\nntp server 1.1.1.1")
    first = repo.commit("first")
    assert first.committed and first.files == ["netbox/sw1_running.txt"] and first.pushed is None

    assert repo.write_config("sw1", "hostname sw1\nntp server 9.9.9.9")
    repo.commit("second")
    history = repo.history("sw1")
    assert history.count == 2
    assert [c.message for c in history.commits] == ["second", "first"]
    assert "+ntp server 9.9.9.9" in history.commits[0].diff
    assert "+hostname sw1" in history.commits[1].diff  # root commit diff is shown too
    assert git("rev-parse", "--abbrev-ref", "HEAD", cwd=tmp_path / "configs") == "netbox"


def test_volatile_lines_do_not_count_as_changes(tmp_path):
    repo = make_repo(tmp_path / "configs")
    repo.prepare()
    repo.write_config("sw1", "! Last configuration change at 10:00\nhostname sw1")
    repo.commit("first")
    assert not repo.write_config("sw1", "! Last configuration change at 11:11\nhostname sw1   ")
    assert not repo.commit("nothing").committed


def test_push_failure_keeps_the_commit_and_retries(tmp_path, remote):
    repo = make_repo(tmp_path / "configs", remote_url=tmp_path / "missing.git")
    repo.prepare()
    repo.write_config("sw1", "hostname sw1")
    result = repo.commit("first")
    assert result.committed and result.pushed is False and result.push_error

    # The remote becomes reachable: the next run pushes the old commit.
    fixed = make_repo(tmp_path / "configs", remote_url=remote)
    fixed.prepare()
    retry = fixed.commit("nothing new")
    assert not retry.committed and retry.pushed is True
    assert git("log", "--format=%s", "netbox", cwd=remote) == "first"


def test_fast_forward_from_remote(tmp_path, remote):
    a = make_repo(tmp_path / "a", remote_url=remote)
    a.prepare()
    a.write_config("sw1", "v1")
    assert a.commit("from a").pushed

    b = make_repo(tmp_path / "b", remote_url=remote)
    b.prepare()  # new repository adopts the remote history
    assert b.read_config("sw1") == "v1\n"

    a.write_config("sw1", "v2")
    a.commit("from a again")
    notes = b.prepare()
    assert any("fast-forwarded" in note for note in notes)
    assert b.read_config("sw1") == "v2\n"


def test_reading_before_the_first_run(tmp_path):
    repo = make_repo(tmp_path / "configs")
    assert not repo.is_available()
    assert repo.read_config("sw1") is None
    assert repo.history("sw1").count == 0
    with pytest.raises(StorageError):
        repo.commit("x")
