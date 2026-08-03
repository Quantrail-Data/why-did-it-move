"""Background scan: sweeps every headline metric x every dimension for
deviations from a trailing same-weekday baseline, one ClickHouse query per
(metric, dimension) pair over the hourly_segment_metrics rollup."""
from datetime import date
from typing import Optional

from . import baseline as baseline_module
from . import config, coverage as coverage_module, db, investigate as investigate_module, metrics, thresholds as thresholds_module

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
        {where_clause}
        GROUP BY day, segment_value
    )
    SELECT
        day,
        segment_value,
        requests,
        {metric_expr} AS actual_value,
        {baseline_cols}
    FROM daily
    WHERE requests > 0
    ORDER BY segment_value, day
"""


def _build_daily_segment_query(dim_col: str, metric_expr: str, hour_cutoff=None) -> str:
    hour_filter = coverage_module.hour_filter_sql(hour_cutoff)
    return _DAILY_SEGMENT_QUERY.format(
        dim_col=dim_col,
        metric_expr=metric_expr,
        where_clause=f"WHERE {hour_filter}" if hour_filter else "",
        baseline_cols=baseline_module.baseline_select(
            metric_expr, "segment_value, toDayOfWeek(day)", config.TRAILING_WEEKS
        ),
    )


def _existing_open_candidates(client) -> dict:
    """Maps (day, metric, primary_dim, primary_value) -> candidate id, keyed
    only on the PRIMARY dimension (segment_dims' first entry - the one
    refine_segment() started from), not every dimension in a combo. A combo
    candidate's marginal deviation differs per dimension (country=CA's
    number isn't device_model=Pixel 8's number), so keying on every entry
    would make it ambiguous which fresh value to refresh it with."""
    # 'investigated' counts as already-known too - once a candidate has been
    # clicked into, a later scan must still recognize it (and refresh it),
    # not treat the segment as never-seen and re-insert a fresh duplicate.
    # 'dismissed' is excluded on purpose: a human explicitly said this one's
    # not real, and a rescan shouldn't resurrect it.
    rows = client.query(
        "SELECT id, day, metric, segment_dims FROM inmobi_rca.anomaly_candidates WHERE status IN ('open', 'investigated')"
    ).result_rows
    keys = {}
    for cid, day, metric_name, segment_dims in rows:
        if not segment_dims:
            continue
        primary_dim, primary_value = next(iter(segment_dims.items()))
        keys[(day, metric_name, primary_dim, primary_value)] = str(cid)
    return keys


def scan(since_day: Optional[date] = None) -> dict:
    client = db.get_ro_client()
    admin = db.get_admin_client()
    scanned = 0
    skipped_low_history = 0
    new_candidates = []
    refresh_rows = {}  # candidate id -> fresh (baseline, actual, pct_dev, z, baseline_n)
    existing_candidates = _existing_open_candidates(client)
    existing_keys = set(existing_candidates)

    computed_thresholds = thresholds_module.compute_metric_thresholds(client, metrics.HEADLINE_METRICS)

    # Partial days are compared against the same hour window on their
    # baselines, never full 24h - see coverage.py.
    coverage = coverage_module.day_coverage(client)
    complete_days = {d for d, info in coverage.items() if info["complete"]}
    passes = [(None, complete_days)]
    for partial_day in coverage_module.partial_days(coverage):
        passes.append((coverage_module.hour_cutoff_for(coverage, partial_day), {partial_day}))

    for metric_name in metrics.HEADLINE_METRICS:
        metric_expr = metrics.METRIC_EXPRESSIONS[metric_name]
        pct_threshold = computed_thresholds[metric_name]["pct_threshold"]
        volume_floor = computed_thresholds[metric_name]["volume_floor"]
        for dim_col in metrics.scannable_dimensions(metric_name):
            for hour_cutoff, days_in_pass in passes:
                if not days_in_pass:
                    continue
                query = _build_daily_segment_query(dim_col, metric_expr, hour_cutoff)
                for row in client.query(query).result_rows:
                    (
                        day, segment_value, requests, actual,
                        baseline_avg, baseline_mean, baseline_stddev, baseline_n,
                    ) = row
                    if day not in days_in_pass:
                        continue
                    scanned += 1

                    if since_day is not None and day < since_day:
                        continue
                    if str(segment_value) == metrics.BLANK_SEGMENT_VALUE:
                        continue
                    if metrics.is_invalid_number(baseline_avg) or baseline_avg == 0 or requests < volume_floor:
                        continue
                    if (baseline_n or 0) < config.MIN_BASELINE_SAMPLES:
                        skipped_low_history += 1
                        continue

                    pct_dev = (actual - baseline_avg) / baseline_avg
                    z = (
                        (actual - baseline_avg) / baseline_stddev
                        if baseline_stddev and baseline_stddev > 0
                        else None
                    )
                    z_val = float(z) if z is not None else 0.0
                    # AND, not OR: both a large and statistically real
                    # deviation, or ~26% of everything gets flagged.
                    if z is not None:
                        is_anomaly = (
                            abs(pct_dev) >= pct_threshold
                            and abs(z) >= config.Z_SCORE_THRESHOLD
                        )
                    else:
                        is_anomaly = abs(pct_dev) >= pct_threshold

                    key = (day, metric_name, dim_col, str(segment_value))
                    if key in existing_candidates:
                        # Already an open candidate flagged on this (day,
                        # metric, primary dim) by an earlier scan - refresh
                        # its stored snapshot to today's numbers instead of
                        # leaving it frozen at whatever it was when first
                        # flagged, even if new data means it's no longer
                        # anomalous by today's numbers.
                        refresh_rows[existing_candidates[key]] = (
                            float(baseline_avg), float(actual), float(pct_dev),
                            z_val, int(baseline_n or 0),
                        )
                        continue

                    if not is_anomaly:
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
                            "baseline_n": int(baseline_n or 0),
                            "baseline_mean": (
                                None if metrics.is_invalid_number(baseline_mean) else float(baseline_mean)
                            ),
                            "hour_cutoff": hour_cutoff,
                        }
                    )

    for c in new_candidates:
        outer_dim, outer_value = next(iter(c["segment_dims"].items()))
        combo = investigate_module.refine_segment(
            client, c["day"], c["metric"], outer_dim, outer_value, c["pct_deviation"], computed_thresholds
        )
        if combo:
            c["segment_dims"][combo["dimension"]] = str(combo["value"])

    # Refinement runs after the single-dim dedup check above, so two
    # different starting dimensions (e.g. country and device_model, scanned
    # independently) can converge on the identical combo and both survive
    # that check - dedupe again here on the final, post-refinement key.
    seen_final_keys = set()
    deduped = []
    for c in new_candidates:
        final_key = (c["day"], c["metric"], frozenset(c["segment_dims"].items()))
        if final_key in seen_final_keys:
            continue
        seen_final_keys.add(final_key)
        deduped.append(c)
    new_candidates = deduped

    # existing_keys was snapshotted at the start of this scan - a concurrent
    # scan (e.g. a client retry after a proxy timeout, with the original
    # request still running server-side) can commit in the meantime. Narrow
    # that race by re-checking immediately before the insert, against the
    # full combo key this time (existing_keys only has decomposed single-dim
    # keys, not precise enough for this check).
    if new_candidates:
        already_open = {
            (day, metric_name, frozenset(segment_dims.items()))
            for day, metric_name, segment_dims in admin.query(
                "SELECT day, metric, segment_dims FROM inmobi_rca.anomaly_candidates WHERE status IN ('open', 'investigated')"
            ).result_rows
        }
        new_candidates = [
            c for c in new_candidates
            if (c["day"], c["metric"], frozenset(c["segment_dims"].items())) not in already_open
        ]

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
                    c["baseline_n"],
                    "" if c["hour_cutoff"] is None else f"00:00-{c['hour_cutoff']:02d}:59",
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
                "baseline_n",
                "evaluated_hours",
            ],
        )

    # Refresh every already-open candidate this scan re-touched, so
    # anomaly_candidates never sits stale as new data streams in - each row
    # keeps its original flag/id, but the deviation it reports stays live.
    # Point mutations, not a single batched one: this table is small (low
    # hundreds of rows), and the codebase's own convention (see
    # investigate.py's status update) is a plain ALTER ... UPDATE per id
    # rather than a staging-table join for a table this size.
    for candidate_id, (baseline, actual, pct_dev, z_val, baseline_n) in refresh_rows.items():
        admin.command(
            """ALTER TABLE inmobi_rca.anomaly_candidates UPDATE
                   baseline_value = {baseline:Float64},
                   actual_value = {actual:Float64},
                   pct_deviation = {pct_dev:Float64},
                   z_score = {z:Float64},
                   baseline_n = {baseline_n:UInt8}
               WHERE id = {id:String}""",
            parameters={
                "baseline": baseline, "actual": actual, "pct_dev": pct_dev,
                "z": z_val, "baseline_n": baseline_n, "id": candidate_id,
            },
        )

    return {
        "scanned": scanned,
        "new_candidates": len(new_candidates),
        "refreshed_candidates": len(refresh_rows),
        "thresholds": computed_thresholds,
        "coverage": {
            "days_loaded": len(coverage),
            "partial_days": [
                {"day": str(d), "note": coverage_module.describe(coverage, d)}
                for d in coverage_module.partial_days(coverage)
            ],
            "skipped_insufficient_history": skipped_low_history,
            "min_baseline_samples": config.MIN_BASELINE_SAMPLES,
        },
    }
