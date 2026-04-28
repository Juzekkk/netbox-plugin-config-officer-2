"""Shared git utilities for config_officer."""

import logging
import os
import tempfile

logger = logging.getLogger(__name__)


def configure_safe_directory(repo_dir: str) -> None:
    """
    Configure git safe.directory via a temp gitconfig file and env variables.
    Must be called before any Repo() instantiation.
    Avoids writing to ~/.gitconfig which may be read-only in the container.
    """
    cfg_path = os.path.join(tempfile.gettempdir(), "gitconfig_netbox")
    with open(cfg_path, "w") as f:
        f.write(f"[safe]\n\tdirectory = {repo_dir}\n")

    os.environ["GIT_CONFIG_GLOBAL"] = cfg_path
    os.environ["GIT_CONFIG_COUNT"] = "1"
    os.environ["GIT_CONFIG_KEY_0"] = "safe.directory"
    os.environ["GIT_CONFIG_VALUE_0"] = repo_dir

    logger.info("[GIT] safe.directory configured for %r via %r", repo_dir, cfg_path)
