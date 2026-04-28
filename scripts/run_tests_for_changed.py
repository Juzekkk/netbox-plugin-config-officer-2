#!/usr/bin/env python3
"""
scripts/run_tests_for_changed.py
---------------------------------
Called by pre-commit with a list of staged Python files.

Strategy:
  1. If the changed file is itself a test module  -> run it directly.
  2. If the changed file is a production module   -> look for a matching test_<module>.py.
  3. If no test files are found                   -> exit 0 (do not block the commit).
  4. If test files are found                      -> run pytest for those files only.

Manual usage (for debugging):
    python scripts/run_tests_for_changed.py config_officer/cisco_diff.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def find_tests_for(changed_file: Path, tests_root: Path) -> list[Path]:
    """Return test files associated with the given changed file."""
    found: list[Path] = []

    # Case 1: the file is already a test module
    if changed_file.name.startswith("test_"):
        if changed_file.exists():
            found.append(changed_file)
        return found

    # Case 2: search for test_<module_name>.py anywhere under tests/
    stem = changed_file.stem  # e.g. "cisco_diff"
    pattern = f"test_{stem}.py"
    found.extend(tests_root.rglob(pattern))

    # Also check a mirrored sub-path: config_officer/utils/helpers.py
    # -> tests/utils/test_helpers.py
    parts = changed_file.parts
    if len(parts) >= 2:
        sub = Path(*parts[1:-1]) if len(parts) > 2 else Path()
        candidate = tests_root / sub / pattern
        if candidate.exists() and candidate not in found:
            found.append(candidate)

    return found


def main(changed_files: list[str]) -> int:
    repo_root = Path(__file__).resolve().parent.parent
    tests_root = repo_root / "tests"

    test_files: set[Path] = set()

    for f in changed_files:
        path = Path(f)
        # Skip non-Python files (configs, templates, etc.)
        if path.suffix != ".py":
            continue
        # Skip Django migration files - generated code, not worth testing here
        if "migrations" in path.parts:
            continue

        hits = find_tests_for(path, tests_root)
        test_files.update(hits)

    if not test_files:
        print("pre-commit [pytest-changed]: no tests found for changed files - skipping.")
        return 0

    sorted_tests = sorted(str(p) for p in test_files)
    print(f"pre-commit [pytest-changed]: running tests: {', '.join(sorted_tests)}")

    result = subprocess.run(
        [sys.executable, "-m", "pytest", *sorted_tests, "--tb=short", "-q"],
        cwd=repo_root,
        check=False,
    )
    return result.returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
