#!/bin/bash
# One-time (and re-runnable for the Day-2 unseen incident) data load into
# ClickHouse. Not a Compose service on purpose - this is an explicitly
# triggered step, not something that should re-run on every container
# restart. Run from the repo root after `docker compose up -d clickhouse`
# (or the full stack) is healthy:
#
#   ./scripts/load_data.sh
#
# Dimension tables load BEFORE ad_events on purpose: the mv_hourly_segment_metrics
# materialized view joins against apps/advertisers/geo_device at insert time,
# so if ad_events loaded first, that first (largest) batch would resolve
# every segment column to '' since the lookup tables would still be empty.
#
# For the Day-2 unseen-incident slice: drop the new files into
# data/inmobi/ (same filenames, or point LOAD_DIR at wherever they land) and
# re-run this script. New dimension rows upsert (ReplacingMergeTree);
# ad_events just gets new partitions for the new day(s) - no table changes,
# no re-running past days.

set -euo pipefail
cd "$(dirname "$0")/.."

LOAD_DIR=""
ALLOW_OVERLAP=0
FORCE=0
for arg in "$@"; do
    case "$arg" in
        --allow-overlap) ALLOW_OVERLAP=1 ;;
        --force)         FORCE=1 ;;
        *)               LOAD_DIR="$arg" ;;
    esac
done
LOAD_DIR="${LOAD_DIR:-data/inmobi}"

if [ ! -f "$LOAD_DIR/ad_events.parquet" ]; then
    echo "ERROR: $LOAD_DIR/ad_events.parquet not found." >&2
    echo "Place the InMobi data package (ad_events.parquet, apps.csv, advertisers.csv, geo_device.csv) in $LOAD_DIR first." >&2
    exit 1
fi

if [ -f .env ]; then
    set -a
    # shellcheck disable=SC1091
    source .env
    set +a
fi

CH_USER="${CLICKHOUSE_USER:-ch_admin}"
CH_PASSWORD="${CLICKHOUSE_PASSWORD:-12345678}"

ch_insert() {
    local table="$1" format="$2" file="$3"
    docker compose exec -T clickhouse clickhouse-client \
        --user "$CH_USER" --password "$CH_PASSWORD" \
        --query "INSERT INTO $table FORMAT $format" < "$file"
}

# --- Idempotency guard -------------------------------------------------------
# ad_events is a plain MergeTree and the INSERT below is unguarded, so loading
# a file whose days are already present silently DOUBLES every number for
# those days - no error, no warning, and every downstream metric quietly wrong.
# This matters because both PROGRESS.md and INMOBI_CONTEXT.md tell you to
# re-run this script for the Day-2 slice, which is only safe if that package
# contains just the new days. That is an assumption about a file nobody has
# seen yet, not a guarantee - so check instead of assuming.
ch_query() {
    docker compose exec -T clickhouse clickhouse-client \
        --user "$CH_USER" --password "$CH_PASSWORD" --query "$1"
}

EXISTING_ROWS="$(ch_query "SELECT count() FROM inmobi_rca.ad_events" 2>/dev/null || echo 0)"

# NOTE ON ORDERING: dimension tables are loaded AFTER the overlap check below,
# not before it. They were briefly loaded first, which meant a run that
# correctly refused to commit overlapping events had still already inserted a
# second copy of every dimension row. These are ReplacingMergeTree, so that
# does not corrupt anything permanently - but dedup only happens on merge, and
# until it does, any raw-side verification JOIN (scripts/edge_cases.sql Part 9,
# the exact check that caught the ORDER BY corruption bug) doubles its numbers
# and reports a false mismatch. A guard that leaves a side effect behind when
# it refuses is not a guard. Found by running the check and chasing the
# resulting "16 mismatched countries" alarm back to its cause.

# Events land in a STAGING table first, never straight into ad_events.
#
# ad_events is a plain MergeTree and the insert does not deduplicate, so
# loading a file whose days are already present silently DOUBLES every number
# for those days - no error, no warning, every downstream metric quietly
# wrong. This matters because PROGRESS.md and INMOBI_CONTEXT.md both tell you
# to re-run this script for the Day-2 slice, which is only safe if that
# package contains just the new days. Nobody has seen that package yet, so
# that is an assumption, not a guarantee.
#
# Staging makes the check possible at all: the file is not mounted into the
# container, so ClickHouse cannot inspect it with file() before loading. Once
# it is in a table we can read its real date range and compare. The staging
# table has no materialized view attached, so nothing reaches the rollup until
# the overlap decision has been made.
echo "==> Staging ad_events for an overlap check before committing..."
ch_query "DROP TABLE IF EXISTS inmobi_rca.ad_events_staging"
ch_query "CREATE TABLE inmobi_rca.ad_events_staging AS inmobi_rca.ad_events"
ch_insert inmobi_rca.ad_events_staging Parquet "$LOAD_DIR/ad_events.parquet"

INCOMING_MIN="$(ch_query "SELECT toString(min(toDate(event_time))) FROM inmobi_rca.ad_events_staging")"
INCOMING_MAX="$(ch_query "SELECT toString(max(toDate(event_time))) FROM inmobi_rca.ad_events_staging")"
STAGED_ROWS="$(ch_query "SELECT count() FROM inmobi_rca.ad_events_staging")"
echo "    staged $STAGED_ROWS rows, covering $INCOMING_MIN .. $INCOMING_MAX"

if [ "${EXISTING_ROWS:-0}" -gt 0 ] && [ "$FORCE" -eq 0 ]; then
    OVERLAP_DAYS="$(ch_query "
        SELECT count() FROM (
            SELECT DISTINCT toDate(event_time) AS d FROM inmobi_rca.ad_events
            WHERE d BETWEEN toDate('$INCOMING_MIN') AND toDate('$INCOMING_MAX')
        )")"
    if [ "${OVERLAP_DAYS:-0}" -gt 0 ]; then
        if [ "$ALLOW_OVERLAP" -eq 1 ]; then
            echo "    --allow-overlap: dropping $OVERLAP_DAYS existing day partition(s) before committing."
            for part in $(ch_query "
                SELECT DISTINCT toString(toDate(event_time)) FROM inmobi_rca.ad_events
                WHERE toDate(event_time) BETWEEN toDate('$INCOMING_MIN') AND toDate('$INCOMING_MAX')"); do
                ch_query "ALTER TABLE inmobi_rca.ad_events DROP PARTITION '$part'"
                ch_query "ALTER TABLE inmobi_rca.hourly_segment_metrics DROP PARTITION '$part'"
            done
        else
            ch_query "DROP TABLE IF EXISTS inmobi_rca.ad_events_staging"
            echo "ERROR: $OVERLAP_DAYS day(s) in $INCOMING_MIN .. $INCOMING_MAX are already loaded." >&2
            echo "       Committing would DOUBLE-COUNT them - ad_events does not deduplicate." >&2
            echo "       Nothing was changed; the staging table has been dropped." >&2
            echo "       Re-run with --allow-overlap to drop and reload those day partitions," >&2
            echo "       or with --force to insert anyway (you almost certainly do not want this)." >&2
            exit 1
        fi
    else
        echo "    no overlap with the $EXISTING_ROWS rows already loaded - safe to commit."
    fi
fi

# Dimension tables load BEFORE the events are committed, but only once the
# overlap check has passed - the materialized view joins against them at
# insert time, so if events committed first the rollup would resolve every
# segment column to '' for that batch. OPTIMIZE FINAL forces the
# ReplacingMergeTree dedup immediately rather than waiting for a background
# merge, so a re-run leaves exactly one row per id and raw-side verification
# JOINs stay correct straight away.
echo "==> Loading dimension tables (apps, advertisers, geo_device)..."
ch_insert inmobi_rca.apps CSVWithNames "$LOAD_DIR/apps.csv"
ch_insert inmobi_rca.advertisers CSVWithNames "$LOAD_DIR/advertisers.csv"
ch_insert inmobi_rca.geo_device CSVWithNames "$LOAD_DIR/geo_device.csv"
ch_query "OPTIMIZE TABLE inmobi_rca.apps FINAL"
ch_query "OPTIMIZE TABLE inmobi_rca.advertisers FINAL"
ch_query "OPTIMIZE TABLE inmobi_rca.geo_device FINAL"

echo "==> Committing ad_events (this populates the hourly_segment_metrics rollup via the materialized view)..."
ch_query "INSERT INTO inmobi_rca.ad_events SELECT * FROM inmobi_rca.ad_events_staging"
ch_query "DROP TABLE inmobi_rca.ad_events_staging"

echo "==> Row counts:"
docker compose exec -T clickhouse clickhouse-client \
    --user "$CH_USER" --password "$CH_PASSWORD" \
    --query "
        SELECT 'ad_events' AS table, count() FROM inmobi_rca.ad_events
        UNION ALL SELECT 'apps', count() FROM inmobi_rca.apps
        UNION ALL SELECT 'advertisers', count() FROM inmobi_rca.advertisers
        UNION ALL SELECT 'geo_device', count() FROM inmobi_rca.geo_device
        UNION ALL SELECT 'hourly_segment_metrics', count() FROM inmobi_rca.hourly_segment_metrics
        FORMAT PrettyCompact
    "

echo "==> Done."
