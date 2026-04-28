# Config Officer - NetBox plugin

NetBox plugin that deals with Cisco device configuration (collects running config from Cisco devices, indicates config changes, and checks templates compliance).

A plugin for [NetBox](https://github.com/netbox-community/netbox) to work with running-configuration of Cisco devices.
> Compatible with NetBox 2.9 and higher versions only.

- Collect actual information from Cisco devices (running_config, version, IP addresses, etc.) and shows it on a dedicated NetBox page.
- Save Cisco running configuration in a local directory and display all changes with git-like diffs.
- Set up configuration templates for distinct device roles, types.
- Audit whether devices are configured according to appropriate template.
- Export template compliance detailed information to Excel.

Preview.
> Collect devices data:
> ![collect devices data](static/collection.gif)

> Templates compliance
> ![templates compliance](static/templates.gif)

---

## Table of Contents

- [Development Setup](#development-setup)
- [Installation and Configuration](#installation-and-configuration)
- [Usage](#usage)

---

## Development Setup

This section describes how to set up a local development environment so you can work on the plugin, run tests, and have pre-commit checks run automatically on every commit.

### Step 1 - Clone the repository

```shell
git clone https://github.com/Juzekkk/netbox-plugin-config-officer-2
cd netbox-plugin-config-officer-2
```

### Step 2 - Install dependencies

Poetry creates an isolated virtual environment and installs both runtime and development dependencies declared in `pyproject.toml`.

```shell
poetry install
```

Activate the environment for the current shell session (optional - all subsequent commands work with `poetry run <cmd>` too):

```shell
poetry shell
```

### Step 3 - Install pre-commit hooks

This registers the hooks defined in `.pre-commit-config.yaml` into your local `.git` directory. They will run automatically before every `git commit`.

```shell
# Register the pre-commit hook (runs before staging is finalised)
pre-commit install

# Register the commit-msg hook (validates the commit message format)
pre-commit install --hook-type commit-msg
```

### Step 4 - Verify the setup

Run all hooks against every file in the repository to confirm everything is working before you make your first commit:

```shell
pre-commit run --all-files
```

All checks should pass on a clean checkout. If anything fails, fix it before proceeding.

### Step 5 - Run the test suite

```shell
pytest
```

Unit tests live in `tests/` and do not require a running NetBox instance - a lightweight stub in `tests/conftest.py` satisfies the `netbox.plugins` import at collection time.

---

### Commit message format

This project follows the [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/) specification, enforced by the `commitizen` hook on every commit.

The required format is:

```
<type>(<scope>): <subject>
```

Valid types: `feat`, `fix`, `docs`, `style`, `refactor`, `perf`, `test`, `build`, `ci`, `chore`, `revert`

Commits that do not follow this format will be rejected by the `commit-msg` hook.

---

### Selective test execution

The `pytest-changed` pre-commit hook automatically determines which test files are relevant to the files you are committing and runs only those.

The mapping logic is:

- A changed test file (e.g. `tests/test_cisco_diff.py`) -> runs that file directly.
- A changed production module (e.g. `config_officer/cisco_diff.py`) -> looks for `tests/**/test_cisco_diff.py`.
- Django migration files and non-Python files are skipped entirely.

To run the full test suite manually at any time:

```shell
pytest
```

To run tests for a specific module only:

```shell
pytest tests/test_cisco_diff.py -v
```

---

### Project structure

```
.
├── config_officer/               # Plugin source code
│   └── ...
├── tests/
│   ├── conftest.py               # Injects a netbox.plugins stub so tests run without NetBox
│   └── ...
├── scripts/
│   ├── run_tests_for_changed.py  # Pre-commit helper: maps changed files to test files
│   └── ...
├── .pre-commit-config.yaml
└── pyproject.toml                # Dependencies, Ruff, pytest, and Commitizen configuration
```

---

## Installation and Configuration

> Watch the [YouTube](https://www.youtube.com/watch?v=O5kayrkuC1E) video about installation and usage of the plugin.

This instruction only describes how to install this plugin into a [Docker Compose](https://github.com/netbox-community/netbox-docker) instance of NetBox.

> General installation steps and considerations follow the [official guidelines](https://netbox.readthedocs.io/en/stable/plugins/).
> The plugin is available as a Python package from [PyPi](https://pypi.org/project/netbox-plugin-config-officer/) or from [GitHub](https://github.com/artyomovs/netbox-plugin-config-officer).

### 0. Pull NetBox docker-compose version from GitHub

```shell
mkdir ~/netbox && cd "$_"
git clone https://github.com/netbox-community/netbox-docker
```

### 1. Create new docker container based on latest netbox image

```shell
cd ~/netbox
git clone https://github.com/artyomovs/netbox-plugin-config-officer
cd netbox-plugin-config-officer
sudo docker build -t netbox-myplugins .
```

> What's in the Dockerfile:
>
> ```dockerfile
> FROM netboxcommunity/netbox:latest
> RUN apk add iputils bind-tools openssh-client git
> COPY ./requirements.txt /
> COPY . /netbox-plugin-config-officer/
> RUN /opt/netbox/venv/bin/pip install install -r /requirements.txt
> RUN /opt/netbox/venv/bin/pip install  --no-warn-script-location /netbox-plugin-config-officer/
> ```

### 2. Create local git repository and perform first commit

```shell
mkdir ~/netbox/netbox-docker/device_configs && cd "$_"
git init
echo hello > hello.txt
git add .
git commit -m "Initial"
chmod 777 -R ../device_configs
```

### 3. Change **netbox** service in docker-compose.yml (do not delete, just add new lines and change image name)

```yaml
version: '3.4'
services:
  netbox: &netbox
    # Change image name to netbox-myplugins (old name is netboxcommunity/netbox:${VERSION-latest})
    image: netbox-myplugins
    ...
    # Add environment variables for git:
    environment:
      - GIT_PYTHON_GIT_EXECUTABLE=/usr/bin/git
      - GIT_COMMITTER_NAME=netbox
      - GIT_COMMITTER_EMAIL=netbox@example.com
    # user: '101' <--- Comment this out. SSH does not work with this line set.
    volumes:
    # Add this volume:
      - ./device_configs:/device_configs:z
    ports:
      - 8080:8080
```

### 4. Update the *PLUGINS* parameter in the global NetBox **configuration.py** config file in *netbox-docker/configuration* directory

```python
PLUGINS = [
    "config_officer"
]
```

Update the `PLUGINS_CONFIG` parameter in **configuration.py** to configure the plugin:

```python
PLUGINS_CONFIG = {
    "config_officer": {
        # Credentials to Cisco devices:
        "DEVICE_USERNAME": "cisco",
        "DEVICE_PASSWORD": "cisco",
        # "DEVICE_SSH_PORT": 1234  # default: 22

        # Mount this directory to NetBox in docker-compose.yml
        "NETBOX_DEVICES_CONFIGS_DIR": "/device_configs",

        # Add these custom fields to NetBox in advance:
        "CF_NAME_SW_VERSION": "version",
        "CF_NAME_SSH": "ssh",
        "CF_NAME_LAST_COLLECT_DATE": "last_collect_date",
        "CF_NAME_LAST_COLLECT_TIME": "last_collect_time",
        "CF_NAME_COLLECTION_STATUS": "collection_status"
    }
}
```

### 5. Start Docker Compose

```shell
cd ~/netbox/netbox-docker/
sudo docker-compose up -d
```

### 6. When NetBox is started - open the web interface `http://NETBOX_IP:8080`, open the Admin panel, and create the following elements

#### Custom Links

| Name | Content type | URL |
|---|---|---|
| collect_device_data | dcim > device | `http://NETBOX_IP:8080/plugins/config_officer/collect_device_config/{{ obj }}` |
| show_running_config | dcim > device | `http://NETBOX_IP:8080/plugins/config_officer/running_config/{{ obj.name }}` |

#### Custom Fields (optional)

| Name | Label | Object(s) |
|---|---|---|
| collection_status | Last collection status | dcim > device |
| last_collect_date | Date of last collection | dcim > device |
| last_collect_time | Time of last collection | dcim > device |
| ssh | SSH enabled | dcim > device |
| version | Software version | dcim > device |

---

## Usage

Follow the [YouTube](https://www.youtube.com/watch?v=O5kayrkuC1E) link to see the full installation and usage instructions.

### Collection

Add all needed Custom Links and Custom Fields (optionally) and have fun.

### Templates compliance

After the plugin is installed, an additional "Plugin" menu will appear in the top navigation panel.
For the templates compliance feature, follow this three-step scenario:

1. **Add a template** - e.g. for a particular configuration section.
2. **Add a service** - inside the service, add service rules that match the template to particular device roles and device types.
3. **Attach the service to devices.**

![compliance_list](static/compliance_list.png)

All matched templates will be merged into one combined template, which is then compared against the actual running config.

### Schedule config collection

To schedule a global collection from all devices (e.g. every night at 3 a.m.) use the API. Add this line to cron:

```shell
curl --location --request POST 'http://NETBOX_IP:8080/api/plugins/config_officer/collection/' \
  --header 'Authorization: Token YOUR_TOKEN' \
  --form 'task="global_collection"'
```
