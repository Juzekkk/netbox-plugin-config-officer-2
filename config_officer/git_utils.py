"""Shared git utilities for config_officer."""

import logging
import os
import re
import tempfile

logger = logging.getLogger(__name__)


def configure_safe_directory(repo_dir: str, author: str = "") -> None:
    """Configure git safe directory and set necessary env vars"""
    match = re.match(r"^(.+?)\s+<(.+?)>$", author) if author else None
    name = match.group(1) if match else "Netbox"
    email = match.group(2) if match else "netbox@example.com"

    cfg_path = os.path.join(tempfile.gettempdir(), "gitconfig_netbox")
    with open(cfg_path, "w") as f:
        f.write(f"[safe]\n\tdirectory = {repo_dir}\n")
        f.write(f"[user]\n\tname = {name}\n\temail = {email}\n")

    os.environ["GIT_CONFIG_GLOBAL"] = cfg_path
    os.environ["GIT_CONFIG_COUNT"] = "1"
    os.environ["GIT_CONFIG_KEY_0"] = "safe.directory"
    os.environ["GIT_CONFIG_VALUE_0"] = repo_dir

    logger.info(
        "[GIT] Git configured: safe.directory=%r user=%r <%r> via %r",
        repo_dir,
        name,
        email,
        cfg_path,
    )
