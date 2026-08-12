#!/usr/bin/env bash
# Pull-based deploy agent for unRAID — the server half of auto-deploy.
# See deploy/UNRAID.md for setup and .github/workflows/deploy.yml for the CI
# half.
#
# The principle is Argo CD's, boiled down to one machine: git is the desired
# state, the agent compares and reconciles. Concretely:
#
#   1. Fetch origin/main. New commit? Otherwise stop.
#   2. Does ghcr.io/...:sha-<commit> exist? Otherwise stop — that means CI is
#      not finished, or the tests failed. No green tests, no deploy.
#   3. Smoke-test the new image against the REAL config: `sync --dry-run`
#      reads Grocy and Home Assistant, prints the plan and writes nothing.
#   4. Only if that exits 0: git reset --hard and pin the new image ref.
#
# Step 3 is where this differs from a long-running service. There is no
# container to healthcheck — a cron worker is only alive for a few seconds
# every 15 minutes — so the dry run IS the healthcheck, and it is a better
# one: it exercises the actual credentials, the actual API shapes and the
# actual reconcile logic before a single write is allowed.
#
# Rollback is therefore free and needs no code: if the smoke test fails,
# nothing is promoted, and the box simply keeps running the previous commit's
# scripts against the previous commit's image. There is no half-applied state
# to back out of, because a failed deploy never started.
#
# Run it from the User Scripts plugin on a cron schedule, e.g. every 5
# minutes. It takes a file lock, so overlapping runs are harmless.
#
# Needs only what stock unRAID has: bash, docker, flock. git does NOT ship
# with unRAID — when it is missing, the git commands run in a throwaway
# container (alpine/git) against the same paths, so the result is identical.

set -euo pipefail

BASE_DIR="${BASE_DIR:-/mnt/user/appdata/ha-setup}"
REPO_DIR="${REPO_DIR:-$BASE_DIR/repo}"
SECRETS_DIR="${SECRETS_DIR:-$BASE_DIR/secrets}"
STATE_DIR="${STATE_DIR:-$BASE_DIR/state}"
BRANCH="${BRANCH:-main}"
REGISTRY="${REGISTRY:-ghcr.io/ninkaninus/ha-setup}"
STATE_FILE="$BASE_DIR/.deploy-state"     # last commit that passed its smoke test
SMOKE_TIMEOUT="${SMOKE_TIMEOUT:-300}"

# The deployable units in this repo: "name|smoke arguments".
#
# Adding one means a line here plus a build step in the CI workflow. Each
# unit's env file is $SECRETS_DIR/<name>.env and its pinned image ref is
# written to $STATE_DIR/<name>.image, which is what the cron scripts read.
UNITS=(
    "grocy-lists|sync --dry-run"
)

# Repo files on unRAID are typically owned by root or nobody; without this
# git refuses to touch them ("dubious ownership").
export GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=safe.directory GIT_CONFIG_VALUE_0='*'

log() { printf '%s  %s\n' "$(date '+%F %T')" "$*"; }

run_git() {
    if command -v git >/dev/null 2>&1; then
        git "$@"
        return
    fi
    local extra=()
    [ -d "$SECRETS_DIR" ] && extra+=(-v "$SECRETS_DIR:$SECRETS_DIR:ro")
    [ -n "${GIT_SSH_COMMAND:-}" ] && extra+=(-e "GIT_SSH_COMMAND=$GIT_SSH_COMMAND")
    docker run --rm -v "$REPO_DIR:$REPO_DIR" -w "$REPO_DIR" \
        -e GIT_CONFIG_COUNT -e GIT_CONFIG_KEY_0 -e GIT_CONFIG_VALUE_0 \
        "${extra[@]}" alpine/git "$@"
}

# Credentials live outside the repo and outside git. The old location on the
# USB flash still works so an existing install keeps running, but appdata is
# where they belong: /boot is vfat, where chmod 600 is silently a no-op.
env_file_for() {
    local name=$1
    if [ -f "$SECRETS_DIR/$name.env" ]; then
        printf '%s\n' "$SECRETS_DIR/$name.env"
    elif [ -f "/boot/config/plugins/user.scripts/$name.env" ]; then
        printf '%s\n' "/boot/config/plugins/user.scripts/$name.env"
    else
        return 1
    fi
}

# Exercise the new image against the real Grocy and Home Assistant. --dry-run
# prints the full plan and writes nothing, so this is safe to run on every
# deploy — and a failure here is exactly the class of breakage that would
# otherwise land silently on her shopping list.
smoke() {
    local name=$1 image=$2 env_file=$3
    shift 3
    local out rc attempt
    # Retried once: HA restarts and Grocy hiccups are the expected case, and
    # a transient 502 during the probe is not a reason to withhold a good
    # commit. The worker already retries 5xx internally; this covers the
    # window where the host is flat-out unreachable.
    for attempt in 1 2; do
        set +e
        out=$(timeout "$SMOKE_TIMEOUT" docker run --rm --env-file "$env_file" \
              "$image" "$@" 2>&1)
        rc=$?
        set -e
        [ $rc -eq 0 ] && return 0
        [ $attempt -eq 1 ] && sleep 30
    done
    log "FEJL: smoke test failed for $name (exit $rc) — not promoting"
    printf '%s\n' "$out" | tail -20
    return 1
}

# All units move together, or none do. The repo is one desired state: the
# cron scripts and the image they run come from the same commit, so promoting
# one unit while another failed would leave the tree describing a deployment
# that is not what is running.
promote() {
    local sha=$1
    run_git reset --hard --quiet "$sha"
    mkdir -p "$STATE_DIR"
    local unit name
    for unit in "${UNITS[@]}"; do
        name=${unit%%|*}
        printf '%s\n' "$REGISTRY/$name:sha-$sha" >"$STATE_DIR/$name.image"
    done
    printf '%s\n' "$sha" >"$STATE_FILE"
}

# True when every unit already has its pinned image file. Guards the case
# where the state file says "deployed" but the pin files were wiped.
pins_intact() {
    local unit name
    for unit in "${UNITS[@]}"; do
        name=${unit%%|*}
        [ -s "$STATE_DIR/$name.image" ] || return 1
    done
    return 0
}

main() {
    mkdir -p "$BASE_DIR" "$STATE_DIR"
    exec 9>"$BASE_DIR/.deploy-lock"
    flock -n 9 || exit 0    # another run is in progress

    if [ ! -d "$REPO_DIR/.git" ]; then
        log "FEJL: no git clone at $REPO_DIR — see deploy/UNRAID.md step 1"
        exit 1
    fi
    cd "$REPO_DIR"

    # Optional, and unused while the repo and package are public: a read-only
    # GHCR token. Dropping one in flips this to a private package without any
    # other change.
    if [ -f "$SECRETS_DIR/ghcr_token" ]; then
        docker login ghcr.io -u "${GHCR_USER:-ninkaninus}" \
            --password-stdin <"$SECRETS_DIR/ghcr_token" >/dev/null 2>&1 || true
    fi
    if [ -f "$SECRETS_DIR/deploy_key" ]; then
        export GIT_SSH_COMMAND="ssh -i $SECRETS_DIR/deploy_key -o IdentitiesOnly=yes -o UserKnownHostsFile=$SECRETS_DIR/known_hosts -o StrictHostKeyChecking=accept-new"
    fi

    run_git fetch --quiet origin "$BRANCH"
    local want last_good
    want=$(run_git rev-parse "origin/$BRANCH")
    last_good=$(cat "$STATE_FILE" 2>/dev/null || true)

    if [ "$want" = "$last_good" ] && pins_intact; then
        exit 0    # reconciled — nothing to do, and nothing to say
    fi

    # Does the image for the wanted commit exist? If not, CI is still running
    # (or the tests failed), and we just wait for the next run. Every unit
    # must be present before anything is promoted.
    local unit name image
    for unit in "${UNITS[@]}"; do
        name=${unit%%|*}
        image="$REGISTRY/$name:sha-$want"
        if ! docker pull --quiet "$image" >/dev/null 2>&1; then
            log "image for $want not published yet ($name) — CI still running or failed; retrying later"
            exit 0
        fi
    done

    # Every unit is smoke-tested before any of them is promoted.
    local smoke_args env_file
    for unit in "${UNITS[@]}"; do
        name=${unit%%|*}
        smoke_args=${unit#*|}
        image="$REGISTRY/$name:sha-$want"
        if ! env_file=$(env_file_for "$name"); then
            log "FEJL: no env file for $name — expected $SECRETS_DIR/$name.env"
            exit 1
        fi
        # shellcheck disable=SC2086 — smoke_args is a deliberate word list
        smoke "$name" "$image" "$env_file" $smoke_args || exit 1
    done

    log "deploying $want (from $(run_git rev-parse --short HEAD 2>/dev/null || echo none))"
    promote "$want"
    log "ok — $want smoke-tested and pinned"
}

# main runs last and everything above is a function, on purpose: bash reads a
# script incrementally, and promote() rewrites this very file via git reset.
# Defining everything first means the whole body is parsed before the reset
# happens, so an update mid-run cannot make bash resume at a stale offset.
main "$@"
