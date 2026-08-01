"""Incremental ingest: the complement to scripts/load_data.sh's bulk file
drop. Some data (e.g. the Day-2 unseen-incident slice) may not arrive as a
file at all - it could be pushed event-by-event or in small batches from
wherever it's coming from. This gives that path a plain HTTP insert instead
of assuming a shell/file-system step is always available.

Writes straight into ad_events via the admin client, the same insert shape
detect.py/investigate.py already use for their own tables. The existing
mv_hourly_segment_metrics materialized view fires on any insert into
ad_events regardless of path, so hourly_segment_metrics - and everything
downstream: scan, investigate, timeline, thresholds - stays correct with no
other code change, whether a row arrived via bulk load or this endpoint.
"""
from . import db, schemas

_COLUMNS = [
    "event_time",
    "app_id",
    "geo_device_id",
    "advertiser_id",
    "ad_format",
    "is_filled",
    "is_impression",
    "is_click",
    "revenue",
]


def insert_events(events: list[schemas.EventIn]) -> dict:
    admin = db.get_admin_client()
    rows = [
        [
            e.event_time,
            e.app_id,
            e.geo_device_id,
            e.advertiser_id,
            e.ad_format,
            e.is_filled,
            e.is_impression,
            e.is_click,
            e.revenue,
        ]
        for e in events
    ]
    admin.insert("inmobi_rca.ad_events", rows, column_names=_COLUMNS)
    return {"inserted": len(rows)}
