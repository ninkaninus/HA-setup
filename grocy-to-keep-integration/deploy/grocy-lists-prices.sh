#!/usr/bin/env bash
# unRAID User Scripts — schedule: Custom cron   0 7 * * 2   (Tuesdays 07:00)
#
# Fills product_barcodes.last_price where it is empty, so scanning a barcode
# at purchase pre-fills a price. Two exact-keyed sources: the price you last
# typed for that product, and Salling's Products EAN API — barcode in, shelf
# price out.
#
# WEEKLY, and the quota is what decides that. Salling allows 100 requests per
# day and each barcode costs one, so a run covers 90 and the ~128 unpriced
# barcodes take two passes. Monthly would take two months to converge for no
# gain; daily would re-ask about products Salling does not sell, every day,
# forever. Weekly converges in a fortnight and then costs almost nothing.
#
# Tuesday keeps it clear of Monday's analyse run.
#
# Safe to run repeatedly: it only ever fills EMPTY prices, so it cannot
# overwrite anything the household typed.

exec "$(dirname "$(readlink -f "$0")")/../../deploy/run-unit.sh" \
    grocy-lists prices
