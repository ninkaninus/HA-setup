#!/usr/bin/env bash
# unRAID User Scripts — schedule: Custom cron   */15 * * * *
#
# Reconciles Grocy below-min-stock products onto the shared todo.indkob list,
# and applies any suggestion that was ticked on todo.grocy_forslag.
# Safe to run repeatedly: it converges and then no-ops.
#
# The image version comes from the deploy agent, not from this file — see
# deploy/run-unit.sh and deploy/UNRAID.md.

exec "$(dirname "$(readlink -f "$0")")/../../deploy/run-unit.sh" \
    grocy-lists sync
