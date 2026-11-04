#!/usr/bin/env bash
# Prepare a NetBox source checkout for the integration tests.
#
#   poetry run scripts/setup-netbox.sh            # NetBox version from .netbox-version
#   poetry run scripts/setup-netbox.sh v4.7.3     # another version
#
# Clones NetBox into .netbox/ and installs its requirements into the current
# (Poetry) environment. Then: NETBOX_PATH=.netbox poetry run pytest
set -euo pipefail
cd "$(dirname "$0")/.."

VERSION="${1:-$(cat .netbox-version)}"
TARGET=".netbox"

if [ -d "$TARGET/.git" ]; then
    git -C "$TARGET" fetch --quiet --depth 1 origin tag "$VERSION"
    git -C "$TARGET" checkout --quiet "$VERSION"
else
    git clone --quiet --depth 1 --branch "$VERSION" https://github.com/netbox-community/netbox.git "$TARGET"
fi

# psycopg[c] needs a C compiler and libpq headers; the binary build is fine for tests.
sed 's/psycopg\[c,pool\]/psycopg[binary,pool]/' "$TARGET/requirements.txt" > "$TARGET/requirements-tests.txt"
python -m pip install --quiet -r "$TARGET/requirements-tests.txt"
python -m pip install --quiet -e .

echo "NetBox $VERSION is ready in $TARGET/"
echo "Start PostgreSQL + Redis:  docker compose -f tests/services.yml up -d"
echo "Run all tests:             NETBOX_PATH=$TARGET poetry run pytest"
