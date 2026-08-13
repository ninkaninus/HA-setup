#!/usr/bin/env bash
# unRAID User Scripts — schedule: Custom cron   */15 * * * *
#
# On the quarter-hour, which means this shares :00/:15/:30/:45 with the deploy
# agent and with every other cron on the box. Fine at this workload. It does
# mean a run can coincide with the agent replacing the image pin, so that
# write is an atomic rename — see promote() in deploy/autodeploy.sh. Offset
# schedules that avoid the overlap entirely are in deploy/UNRAID.md, for when
# there is enough here to be worth spreading out.
#
# Reconciles Grocy below-min-stock products onto the shared todo.indkob list,
# and applies any suggestion that was ticked on todo.grocy_forslag.
# Safe to run repeatedly: it converges and then no-ops.
#
# The image version comes from the deploy agent, not from this file — see
# deploy/run-unit.sh and deploy/UNRAID.md.

exec "$(dirname "$(readlink -f "$0")")/../../deploy/run-unit.sh" \
    grocy-lists sync
