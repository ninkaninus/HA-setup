#!/usr/bin/env bash
# unRAID User Scripts — schedule: Custom cron   23 6 * * 1   (Mondays 06:23)
#
# Rebuilds the min_stock_amount suggestions on todo.grocy_forslag from one year
# of consumption plus observed shelf life. Suggestions are whole numbers.
#
# It writes to Grocy ONLY for suggestions that were ticked (approved) on that
# list; deriving a suggestion never changes Grocy by itself.
#
# Longer timeout than sync: a year of stock_log is several paged requests.

exec env TIMEOUT=600 "$(dirname "$(readlink -f "$0")")/../../deploy/run-unit.sh" \
    grocy-lists analyse
