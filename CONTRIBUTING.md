# Contributing

How to develop, test and release the plugin. User documentation is in the [README](README.md).

## Setup

```bash
git clone https://github.com/Juzekkk/netbox-plugin-config-officer-2.git
cd netbox-plugin-config-officer-2
poetry install                    # plugin + dev tools in .venv
poetry run pre-commit install     # checks on every commit (see below)
```

On every commit, pre-commit formats and lints the code (ruff), runs basic file checks and the unit tests, and checks the commit message. Run all checks by hand with `poetry run pre-commit run --all-files`.

## Tests

| Tests | Needs | Command |
|---|---|---|
| Unit | nothing | `poetry run pytest tests/unit` |
| All (unit + integration) | NetBox checkout, PostgreSQL, Redis | `NETBOX_PATH=.netbox poetry run pytest --reuse-db` |

One-time preparation for the integration tests:

```bash
docker compose -f tests/services.yml up -d    # PostgreSQL + Redis
poetry run scripts/setup-netbox.sh            # NetBox (version from .netbox-version) into .netbox/
```

The BGP sync tests also need the netbox-bgp plugin (`poetry run pip install "netbox-bgp>=0.20,<0.21"` for NetBox 4.7); without it they are skipped.

The first run builds NetBox's test database (about 5 minutes); `--reuse-db` keeps it, so later runs take under a minute. Leave `--reuse-db` out once after adding a migration.

| Directory | Contents |
|---|---|
| `tests/unit/` | Pure Python: settings, parsers, redaction, Git storage, config diff, webhook |
| `tests/integration/` | Real NetBox: collection runs against a fake network, NetBox sync, compliance, schedules, views and permissions, REST API |
| `tests/fixtures/<platform>/` | Captured CLI output. Add a file here when a parser has to understand new output |

GitHub Actions runs the same checks on every push and pull request (`.github/workflows/ci.yml`): pre-commit, commit messages, and all tests against the pinned NetBox version and the latest NetBox release. The run against the latest release does not block merging; it is also repeated every Monday, so a NetBox release that breaks the plugin shows up before you upgrade.

## Commit messages

[Conventional Commits](https://www.conventionalcommits.org/), checked by pre-commit and CI:

```
feat: collect VLANs from switches
fix(parsers): read NX-OS speeds given in Gb/s
docs: describe the webhook payload
```

Other types: `test`, `ci`, `build`, `refactor`, `chore`. A breaking change gets a `!` (`feat!: ...`) or a `BREAKING CHANGE:` line in the body. The commit types decide the next version number and become the changelog.

## Releasing

`main` only takes changes through pull requests, so the version bump goes through one too, and the tag is pushed after it is merged:

```bash
git checkout main && git pull
git checkout -b release
poetry run cz bump --dry-run    # preview the new version and changelog entry
poetry run cz bump              # update pyproject.toml + CHANGELOG.md, commit, tag vX.Y.Z (locally)
git push -u origin release      # the branch only - not the tag yet
```

Merge the pull request with **Create a merge commit** (squash or rebase would replace the tagged commit), then:

```bash
git checkout main && git pull
git push origin vX.Y.Z          # GitHub Actions: build, publish to PyPI, create the GitHub release
```

The release workflow refuses a tag whose commit is not on `main`. If the pull request was squashed or rebased anyway, move the tag onto the merged commit first: `git tag -f vX.Y.Z main`.

The version is derived from the commits since the last release (while the plugin is 0.x):

| Commits since the last release | Example |
|---|---|
| only `fix:` | 0.2.0 → 0.2.1 |
| at least one `feat:` or a breaking change | 0.2.0 → 0.3.0 |

Override it with `poetry run cz bump --increment PATCH` (or `MINOR`, `MAJOR`). Check the new CHANGELOG.md entry before pushing; to reword it, edit the file, `git commit --amend --no-edit` and move the tag with `git tag -f vX.Y.Z`.

The version exists only in `pyproject.toml`; the plugin reads it from the installed package.

**One-time PyPI setup.** Publishing uses PyPI trusted publishing, so no token is stored in GitHub. In the PyPI project's *Publishing* settings, add a GitHub publisher with owner `Juzekkk`, repository `netbox-plugin-config-officer-2`, workflow `release.yml` and environment `pypi`.

**Manual fallback**, if GitHub Actions cannot publish: `poetry build && poetry publish` (after `poetry config pypi-token.pypi <token>` once).

## Supporting a new NetBox version

1. Put the version in `.netbox-version` (used by CI and `scripts/setup-netbox.sh`).
2. `poetry run scripts/setup-netbox.sh` and run all tests.
3. Update `min_version` in `config_officer/__init__.py` and the compatibility table in the README if needed.
4. Update the netbox-bgp version in `.github/workflows/ci.yml` to the release for that NetBox version.
