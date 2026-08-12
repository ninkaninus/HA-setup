#!/usr/bin/env bash
# Run one deployed unit at the image version the agent pinned.
#
#   run-unit.sh <unit> <job> [args...]
#
# The image is NEVER named literally here. autodeploy.sh writes the exact
# ghcr.io/...:sha-<commit> ref into $STATE_DIR/<unit>.image once that build
# has passed its smoke test, and this reads it back. That indirection is what
# makes the deploy atomic: the cron jobs always run a version that was
# verified against the live Grocy and Home Assistant, and they keep running
# the previous one for as long as a newer build is failing.
#
# Nothing here needs the repo to be up to date beyond its own file, so a
# deploy landing mid-run cannot affect a job already in flight.

set -uo pipefail

BASE_DIR="${BASE_DIR:-/mnt/user/appdata/ha-setup}"
SECRETS_DIR="${SECRETS_DIR:-$BASE_DIR/secrets}"
STATE_DIR="${STATE_DIR:-$BASE_DIR/state}"
TIMEOUT="${TIMEOUT:-300}"

unit=${1:?usage: run-unit.sh <unit> <job> [args...]}
shift

image_file="$STATE_DIR/$unit.image"
if [ ! -s "$image_file" ]; then
    echo "no pinned image for $unit — run deploy/autodeploy.sh once to deploy" >&2
    exit 1
fi
image=$(cat "$image_file")

# Same resolution order as the agent: appdata first, then the old USB-flash
# location so an existing install keeps working.
if [ -f "$SECRETS_DIR/$unit.env" ]; then
    env_file="$SECRETS_DIR/$unit.env"
elif [ -f "/boot/config/plugins/user.scripts/$unit.env" ]; then
    env_file="/boot/config/plugins/user.scripts/$unit.env"
else
    echo "missing env file: expected $SECRETS_DIR/$unit.env" >&2
    exit 1
fi

# --rm so runs never pile up: at a 15-minute cadence a hung run would
# otherwise stack containers until the box runs out of room.
timeout "$TIMEOUT" docker run --rm --env-file "$env_file" "$image" "$@"
rc=$?

case $rc in
    0)   ;;
    124) echo "$unit $*: exceeded ${TIMEOUT}s and was killed" >&2 ;;
    *)   echo "$unit $*: failed with exit $rc (image $image)" >&2 ;;
esac
exit $rc
