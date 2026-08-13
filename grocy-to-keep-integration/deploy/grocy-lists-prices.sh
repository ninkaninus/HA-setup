#!/usr/bin/env bash
# unRAID User Scripts — schedule: Custom cron   0 7 * * *   (daily 07:00)
#
# Fills product_barcodes.last_price where it is empty, so scanning a barcode
# at purchase pre-fills a price. Two exact-keyed sources: the price you last
# typed for that product, and Salling's Anti Food Waste feed, which publishes
# an EAN alongside the ORIGINAL shelf price.
#
# DAILY, not monthly, and only the Salling half needs it: clearances rotate
# within a day, so a monthly poll would see one arbitrary day of markdowns and
# miss the other 29. The purchase-history half converges after the first run
# and then finds nothing to do, so the extra runs cost one Grocy read.
#
# Safe to run repeatedly: it only ever fills EMPTY prices, so it cannot
# overwrite anything the household typed.

exec "$(dirname "$(readlink -f "$0")")/../../deploy/run-unit.sh" \
    grocy-lists prices
