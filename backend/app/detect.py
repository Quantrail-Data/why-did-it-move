"""Background scan: sweeps every headline metric x every dimension for
deviations from a trailing same-weekday baseline, entirely in one ClickHouse
query per (metric, dimension) pair using window functions over the
hourly_segment_metrics rollup - never the raw ad_events table, and never one
round-trip per segment value. This is the "Detect" step: it runs on its own,
nobody has to already know where to look.
"""
from datetime import date
from typing import Optional

from . import config, db, investigate as investigate_module, metrics, thresholds as thresholds_module

# ROWS BETWEEN {trailing} PRECEDING AND 1 PRECEDING, partitioned by
# (segment_value, day-of-week): for each day, compares against the trailing
# same-weekday occurrences of that same segment - the like-for-like baseline
# metrics_glossary.md calls for, computed for every segment value in one pass.
_DAILY_SEGMENT_QUERY = """
    WITH daily AS (
        SELECT
            toDate(hour) AS day,
            {dim_col} AS segment_value,
            countMerge(requests) AS requests,
            sumMerge(fills) AS fills,
            sumMerge(impressions) AS impressions,
            sumMerge(clicks) AS clicks,
            sumMerge(revenue) AS revenue
        FROM inmobi_rca.hourly_segment_metrics
        GROUP BY day, segment_value
    )
    SELECT
        day,
        segment_value,
        requests,
        {metric_expr} AS actual_value,
        avg({metric_expr}) OVER (
            PARTITION BY segment_value, toDayOfWeek(day)
            ORDER BY day
            ROWS BETWEEN {trailing} PRECEDING AND 1 PRECEDING
        ) AS baseline_avg,
        stddevPop({metric_expr}) OVER (
            PARTITION BY segment_value, toDayOfWeek(day)
            ORDER BY day
            ROWS BETWEEN {trailing} PRECEDING AND 1 PRECEDING
        ) AS baseline_stddev
    FROM daily
    WHERE requests > 0
    ORDER BY segment_value, day
"""


def _existing_open_keys(client) -> set:
    """(day, metric, dimension, value) already flagged 'open' - scan() must
    be safe to re-run (a second click of "Re-scan", or a re-run after
    loading more data) without re-inserting the same candidate. Each
    candidate's segment_dims map always has exactly one key/value pair.
    """
    rows = client.query(
        "SELECT day, metric, segment_dims FROM inmobi_rca.anomaly_candidates WHERE status = 'open'"
    ).result_rows
    keys = set()
    for day, metric_name, segment_dims in rows:
        for dim_col, value in segment_dims.items():
            keys.add((day, metric_name, dim_col, value))
    return keys


def scan(since_day: Optional[date] = None) -> dict:
    client = db.get_ro_client()
    admin = db.get_admin_client()
    scanned = 0
    new_candidates = []
    existing_keys = _existing_open_keys(client)

    # Computed once per scan from whatever data is currently loaded - see
    # thresholds.py. Replaces the flat PCT_DEVIATION_THRESHOLD/
    # MIN_VOLUME_FLOOR constants with a per-metric empirical cutoff, so a
    # metric's own volatility (not one shared 30% for all four) decides
    # whether a deviation is real.
    computed_thresholds = thresholds_module.compute_metric_thresholds(client, metrics.HEADLINE_METRICS)

    for metric_name in metrics.HEADLINE_METRICS:
        metric_expr = metrics.METRIC_EXPRESSIONS[metric_name]
        pct_threshold = computed_thresholds[metric_name]["pct_threshold"]
        volume_floor = computed_thresholds[metric_name]["volume_floor"]
        for dim_col in metrics.DIMENSIONS:
            query = _DAILY_SEGMENT_QUERY.format(
                dim_col=dim_col, metric_expr=metric_expr, trailing=config.TRAILING_WEEKS
            )
            for row in client.query(query).result_rows:
                day, segment_value, requests, actual, baseline_avg, baseline_stddev = row
                scanned += 1

                if since_day is not None and day < since_day:
                    continue
                if metrics.is_invalid_number(baseline_avg) or baseline_avg == 0 or requests < volume_floor:
                    continue

                pct_dev = (actual - baseline_avg) / baseline_avg
                z = (
                    (actual - baseline_avg) / baseline_stddev
                    if baseline_stddev and baseline_stddev > 0
                    else None
                )
                # AND, not OR - requiring both a large deviation and a
                # statistically real one is what actually cuts noise; OR let
                # either condition alone flag it, which is how we ended up
                # flagging ~26% of everything. Exception: when the trailing
                # baseline has zero variance (a perfectly flat history), z is
                # undefined - that's not a reason to skip a real deviation,
                # so fall back to the deviation threshold alone in that case.
                if z is not None:
                    is_anomaly = (
                        abs(pct_dev) >= pct_threshold
                        and abs(z) >= config.Z_SCORE_THRESHOLD
                    )
                else:
                    is_anomaly = abs(pct_dev) >= pct_threshold
                if not is_anomaly:
                    continue

                key = (day, metric_name, dim_col, str(segment_value))
                if key in existing_keys:
                    continue
                existing_keys.add(key)

                new_candidates.append(
                    {
                        "day": day,
                        "metric": metric_name,
                        "segment_dims": {dim_col: str(segment_value)},
                        "baseline_value": float(baseline_avg),
                        "actual_value": float(actual),
                        "pct_deviation": float(pct_dev),
                        "z_score": float(z) if z is not None else 0.0,
                    }
                )

    # Multi-dimension drill-down for this run's newly-flagged candidates
    # only (not every already-open one on every re-scan) - keeps scan cost
    # bounded to the delta each run, not an 8x blowup of the full sweep.
    # See investigate.py::refine_segment.
    for c in new_candidates:
        outer_dim, outer_value = next(iter(c["segment_dims"].items()))
        combo = investigate_module.refine_segment(
            client, c["day"], c["metric"], outer_dim, outer_value, c["pct_deviation"], computed_thresholds
        )
        if combo:
            c["segment_dims"][combo["dimension"]] = str(combo["value"])

    if new_candidates:
        admin.insert(
            "inmobi_rca.anomaly_candidates",
            [
                [
                    c["day"],
                    c["metric"],
                    c["segment_dims"],
                    c["baseline_value"],
                    c["actual_value"],
                    c["pct_deviation"],
                    c["z_score"],
                    "open",
                ]
                for c in new_candidates
            ],
            column_names=[
                "day",
                "metric",
                "segment_dims",
                "baseline_value",
                "actual_value",
                "pct_deviation",
                "z_score",
                "status",
            ],
        )

    return {"scanned": scanned, "new_candidates": len(new_candidates), "thresholds": computed_thresholds}
