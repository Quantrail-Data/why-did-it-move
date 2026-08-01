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

LOAD_DIR="${1:-data/inmobi}"

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

echo "==> Loading dimension tables (apps, advertisers, geo_device)..."
ch_insert inmobi_rca.apps CSVWithNames "$LOAD_DIR/apps.csv"
ch_insert inmobi_rca.advertisers CSVWithNames "$LOAD_DIR/advertisers.csv"
ch_insert inmobi_rca.geo_device CSVWithNames "$LOAD_DIR/geo_device.csv"

echo "==> Loading ad_events (this populates the hourly_segment_metrics rollup via the materialized view)..."
ch_insert inmobi_rca.ad_events Parquet "$LOAD_DIR/ad_events.parquet"

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
