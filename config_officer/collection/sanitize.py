"""Removing secrets from a running configuration before it is stored."""

from __future__ import annotations

import re
from collections.abc import Iterable

#: Always removed: "neighbor 10.0.0.2 password 7 ..." (IOS-XE BGP; type 7 passwords are reversible).
_BGP_NEIGHBOR_PASSWORD = r"neighbor\s+\S+\s+password"


def redact_config(text: str, sensitive_prefixes: Iterable[str]) -> str:
    """Drop every line that starts with one of ``sensitive_prefixes`` (case-insensitive).

    A dropped top-level line takes its indented children with it, so e.g. a
    ``crypto pki certificate chain`` block disappears as a whole instead of
    leaving orphaned certificate lines that would be attached to the previous
    section. An indented line (``  password 7 ...`` under ``line vty``) is
    dropped on its own. BGP neighbor passwords are always removed.
    """
    alternatives = [re.escape(p) for p in sensitive_prefixes if p] + [_BGP_NEIGHBOR_PASSWORD]
    pattern = re.compile(r"^\s*(?:{})\b".format("|".join(alternatives)), re.IGNORECASE)

    kept: list[str] = []
    skipping_block = False
    for line in text.splitlines():
        indented = line[:1] in (" ", "\t")
        if skipping_block:
            if indented:
                continue
            skipping_block = False
        if pattern.match(line):
            skipping_block = not indented
            continue
        kept.append(line)
    return "\n".join(kept)
