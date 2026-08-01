"""Revenue-specific detectors for incident shapes the day-grain threshold
scan is structurally incapable of seeing.

detect.py asks one question per segment-day: "is today far from this
segment's trailing same-weekday baseline?" That catches a cliff. It cannot
catch three real and common revenue incident shapes, and the gap is a
property of the test, not of the tuning - lowering the threshold would not
find them, it would only add noise:

1. SUSTAINED DRIFT. A segment down 8% every day for a week is a larger
   revenue loss than a one-day -30% blip, but no single day clears a ~24%
   cutoff, so nothing is ever flagged. Worse, the trailing baseline slowly
   follows the decline, so the deviation shrinks the longer the problem
   lasts - a slow bleed actively hides itself from a same-weekday comparison.

2. DISAPPEARED SEGMENT. A segment that earned steadily and then goes to
   exactly zero is the single clearest revenue incident there is, and it is
   the one case the existing scan provably cannot report: with revenue 0 the
   deviation is -100%, but if the segment stops producing rows entirely
   there is no row to evaluate, and where a baseline is 0 the
   `baseline_avg == 0` guard drops it. Absence has to be searched for
   explicitly against a day x segment grid; it never arrives on its own.

3. MIX SHIFT. Total revenue flat, but a segment's SHARE of it moved sharply -
   one segment collapsing while another absorbs the demand. The absolute
   deviation can sit under threshold on both sides while the composition of
   the business has changed materially. Found on the real data: os_version
   'Android 15' lost 4.2 percentage points of total revenue share across
   2026-06-23..25, and category 'finance' lost ~2.3pp across 06-19..22.

All three run entirely in ClickHouse, on the same rollup, using the same
robust trailing-same-weekday baseline as everything else (baseline.py). The
LLM is not involved in detection here any more than it is in detect.py.
"""
from typing import Optional

from . import baseline as baseline_module
from . import config, coverage as coverage_module, metrics

METRIC = "revenue"

# A sustained drift is flagged on a LOWER per-day bar than a single-day
# spike, because the evidence is the consistency rather than the magnitude:
# every day in the window must move the same way. Requiring both a
# consistent direction across N days and a meaningful average displacement
# is a much stronger joint condition than either alone, which is why the
# per-day component can safely sit below the single-day cutoff.
DRIFT_MIN_DAYS = int(getattr(config, "DRIFT_MIN_DAYS", 3))
DRIFT_MIN_AVG_DEVIATION = float(getattr(config, "DRIFT_MIN_AVG_DEVIATION", 0.05))
# Share-of-total movement is in percentage POINTS, not percent - a segment
# going from 9.6% to 5.4% of revenue is a 4.2 point shift.
SHARE_SHIFT_MIN_POINTS = float(getattr(config, "SHARE_SHIFT_MIN_POINTS", 0.02))
# A segment must have been earning at least this fraction of total revenue
# for its disappearance to be worth reporting rather than noise vanishing.
COLLAPSE_MIN_BASELINE_SHARE = float(getattr(config, "COLLAPSE_MIN_BASELINE_SHARE", 0.005))


def _dim_union(select_template: str, dimensions) -> str:
    return " UNION ALL ".join(
        select_template.format(dim_col=d, dim_name=f"'{d}'") for d in dimensions
    )


# --- 1. Sustained drift ----------------------------------------------------
# Per segment-day deviation first (same robust baseline as everywhere else),
# then a second window over the trailing DRIFT_MIN_DAYS *calendar* days
# asking two things at once: is the average displacement material, and did
# every day in that window move in the same direction.
#
# EXCESS drift, not raw drift. The first version of this measured each
# segment's drift against its own baseline only, and returned 336 hits on the
# known batch - because 2026-06-21 is a genuine dataset-wide -44.8% day, and
# a whole-business movement makes every segment in every dimension drift
# together. 336 "findings" for one root cause is the same crying-wolf failure
# this project already fixed once in the threshold scan.
#
# The question worth asking is not "did this segment move" but "did this
# segment move MORE than the business as a whole." So the whole dataset is
# unioned in as a synthetic '__overall__' dimension, gets its run statistics
# computed by the identical window, and every real segment is then scored on
# the gap between its own drift and the overall drift on the same day. A
# segment merely carried along by a global dip nets out to roughly zero and
# is correctly not reported.
_DRIFT_QUERY = """
    SELECT
        seg.dim, seg.segment_value, seg.day,
        seg.avg_deviation - overall.avg_deviation AS excess_deviation,
        seg.avg_deviation, overall.avg_deviation AS overall_deviation,
        seg.days_in_run, seg.requests
    FROM (
        SELECT
            dim, segment_value, day, requests,
            avg(pct_dev) OVER run AS avg_deviation,
            sum(sign(pct_dev)) OVER run AS direction_sum,
            count() OVER run AS days_in_run
        FROM (
            SELECT dim, segment_value, day, requests,
                   (actual_value - baseline_avg) / baseline_avg AS pct_dev
            FROM (
                SELECT dim, segment_value, day, requests, actual_value, baseline_avg, baseline_n
                FROM ( {unioned} )
            )
            WHERE isFinite(baseline_avg) AND baseline_avg > 0 AND baseline_n >= {min_baseline_samples}
        )
        WINDOW run AS (
            PARTITION BY dim, segment_value ORDER BY day
            ROWS BETWEEN {drift_days_minus_1} PRECEDING AND CURRENT ROW
        )
    ) AS seg
    INNER JOIN (
        SELECT day, avg(pct_dev) OVER run AS avg_deviation
        FROM (
            SELECT day, (revenue - baseline_avg) / baseline_avg AS pct_dev
            FROM (
                SELECT day, revenue, {overall_baseline_cols}
                FROM (
                    SELECT toDate(hour) AS day, sumMerge(revenue) AS revenue
                    FROM inmobi_rca.hourly_segment_metrics {where_clause}
                    GROUP BY day
                )
            )
            WHERE isFinite(baseline_avg) AND baseline_avg > 0 AND baseline_n >= {min_baseline_samples}
        )
        WINDOW run AS (ORDER BY day ROWS BETWEEN {drift_days_minus_1} PRECEDING AND CURRENT ROW)
    ) AS overall ON seg.day = overall.day
    WHERE seg.days_in_run = {drift_days}
      AND abs(seg.direction_sum) = {drift_days}
      AND abs(seg.avg_deviation - overall.avg_deviation) >= {min_avg_deviation}
      AND seg.requests >= {volume_floor}
    ORDER BY abs(seg.avg_deviation - overall.avg_deviation) DESC
"""

_DRIFT_DIM_SELECT = """
    SELECT {dim_name} AS dim, segment_value, day, requests, actual_value, baseline_avg, baseline_n
    FROM (
        WITH daily AS (
            SELECT toDate(hour) AS day, {dim_col} AS segment_value,
                   countMerge(requests) AS requests, sumMerge(revenue) AS revenue
            FROM inmobi_rca.hourly_segment_metrics
            {where_clause}
            GROUP BY day, segment_value
        )
        SELECT day, segment_value, requests, revenue AS actual_value, {baseline_cols}
        FROM daily
    )
    WHERE segment_value != ''
"""


# --- 2. Disappeared / collapsed segment ------------------------------------
# Built against an explicit day x segment grid so a segment that stops
# producing rows entirely is still evaluated. `revenue` comes back 0 for a
# missing combination rather than the row simply not existing.
_COLLAPSE_QUERY = """
    SELECT scored.dim, scored.segment_value, scored.day, scored.revenue, scored.baseline_revenue
    FROM (
        SELECT
            dim, segment_value, day, revenue,
            quantileExact(0.5)(revenue) OVER w AS baseline_revenue,
            count() OVER w AS baseline_n
        FROM ( {unioned} )
        WINDOW w AS (
            PARTITION BY dim, segment_value, toDayOfWeek(day) ORDER BY day
            ROWS BETWEEN {trailing} PRECEDING AND 1 PRECEDING
        )
    ) AS scored
    INNER JOIN (
        SELECT toDate(hour) AS day, sumMerge(revenue) AS total_revenue
        FROM inmobi_rca.hourly_segment_metrics {where_clause} GROUP BY day
    ) AS totals ON scored.day = totals.day
    WHERE scored.baseline_n >= {min_baseline_samples}
      AND scored.baseline_revenue > 0
      AND scored.revenue <= scored.baseline_revenue * {collapse_ratio}
      AND scored.baseline_revenue / totals.total_revenue >= {min_baseline_share}
    ORDER BY scored.baseline_revenue - scored.revenue DESC
"""

# The day x segment grid is the whole point: a LEFT JOIN from every
# (day, segment) combination onto the actual rollup rows means a segment
# that stopped producing rows entirely still appears, with revenue 0,
# instead of silently not existing.
_COLLAPSE_DIM_SELECT = """
    SELECT {dim_name} AS dim, grid.segment_value AS segment_value, grid.day AS day,
           coalesce(actuals.revenue, 0) AS revenue
    FROM (
        SELECT days.day AS day, vals.segment_value AS segment_value
        FROM (SELECT DISTINCT toDate(hour) AS day FROM inmobi_rca.hourly_segment_metrics {where_clause}) AS days
        CROSS JOIN (
            SELECT DISTINCT {dim_col} AS segment_value
            FROM inmobi_rca.hourly_segment_metrics WHERE {dim_col} != ''
        ) AS vals
    ) AS grid
    LEFT JOIN (
        SELECT toDate(hour) AS day, {dim_col} AS segment_value, sumMerge(revenue) AS revenue
        FROM inmobi_rca.hourly_segment_metrics {where_clause}
        GROUP BY day, segment_value
    ) AS actuals ON grid.day = actuals.day AND grid.segment_value = actuals.segment_value
"""


# --- 3. Mix / share shift --------------------------------------------------
_SHARE_QUERY = """
    SELECT dim, segment_value, day, share, baseline_share, share - baseline_share AS share_delta
    FROM (
        SELECT dim, segment_value, day, share,
               quantileExact(0.5)(share) OVER w AS baseline_share,
               count() OVER w AS baseline_n
        FROM (
            SELECT dim, segment_value, day,
                   revenue / nullIf(sum(revenue) OVER (PARTITION BY dim, day), 0) AS share
            FROM ( {unioned} )
        )
        WINDOW w AS (
            PARTITION BY dim, segment_value, toDayOfWeek(day) ORDER BY day
            ROWS BETWEEN {trailing} PRECEDING AND 1 PRECEDING
        )
    )
    WHERE baseline_n >= {min_baseline_samples}
      AND isFinite(share) AND isFinite(baseline_share)
      AND abs(share - baseline_share) >= {min_points}
    ORDER BY abs(share - baseline_share) DESC
"""

_SHARE_DIM_SELECT = """
    SELECT {dim_name} AS dim, {dim_col} AS segment_value, toDate(hour) AS day,
           sumMerge(revenue) AS revenue
    FROM inmobi_rca.hourly_segment_metrics
    {where_clause}
    GROUP BY day, segment_value
    HAVING segment_value != ''
"""


def _where(hour_cutoff: Optional[int]) -> str:
    f = coverage_module.hour_filter_sql(hour_cutoff)
    return f"WHERE {f}" if f else ""


def detect_sustained_drift(client, hour_cutoff=None, volume_floor=None) -> list:
    where_clause = _where(hour_cutoff)
    baseline_cols = baseline_module.baseline_select(
        "revenue", "segment_value, toDayOfWeek(day)", config.TRAILING_WEEKS
    )
    unioned = " UNION ALL ".join(
        _DRIFT_DIM_SELECT.format(
            dim_col=d, dim_name=f"'{d}'", where_clause=where_clause, baseline_cols=baseline_cols
        )
        for d in metrics.scannable_dimensions(METRIC)
    )
    query = _DRIFT_QUERY.format(
        unioned=unioned,
        where_clause=where_clause,
        overall_baseline_cols=baseline_module.baseline_select(
            "revenue", "toDayOfWeek(day)", config.TRAILING_WEEKS
        ),
        min_baseline_samples=config.MIN_BASELINE_SAMPLES,
        drift_days=DRIFT_MIN_DAYS,
        drift_days_minus_1=DRIFT_MIN_DAYS - 1,
        min_avg_deviation=DRIFT_MIN_AVG_DEVIATION,
        volume_floor=int(volume_floor or config.MIN_VOLUME_FLOOR_ABSOLUTE),
    )
    out = []
    for row in client.query(query).result_rows:
        dim, value, day, excess_dev, avg_dev, overall_dev, days_in_run, requests = row
        out.append(
            {
                "detector": "sustained_drift",
                "metric": METRIC,
                "dimension": dim,
                "value": value,
                "day": day,
                "excess_deviation": float(excess_dev),
                "avg_deviation": float(avg_dev),
                "overall_deviation": float(overall_dev),
                "days_in_run": int(days_in_run),
                "requests": int(requests),
                "description": (
                    f"revenue for {dim}={value} moved {avg_dev:+.1%} on average for "
                    f"{int(days_in_run)} consecutive days, every day in the same direction, "
                    f"against {overall_dev:+.1%} for the business overall - {excess_dev:+.1%} of "
                    "segment-specific drift that no single day's deviation would have flagged"
                ),
            }
        )
    return out


def detect_collapsed_segments(client, hour_cutoff=None, collapse_ratio: float = 0.1) -> list:
    where_clause = _where(hour_cutoff)
    unioned = " UNION ALL ".join(
        _COLLAPSE_DIM_SELECT.format(dim_col=d, dim_name=f"'{d}'", where_clause=where_clause)
        for d in metrics.scannable_dimensions(METRIC)
    )
    query = _COLLAPSE_QUERY.format(
        unioned=unioned,
        where_clause=where_clause,
        trailing=config.TRAILING_WEEKS,
        min_baseline_samples=config.MIN_BASELINE_SAMPLES,
        collapse_ratio=collapse_ratio,
        min_baseline_share=COLLAPSE_MIN_BASELINE_SHARE,
    )
    out = []
    for dim, value, day, revenue, baseline_revenue in client.query(query).result_rows:
        lost = float(baseline_revenue) - float(revenue)
        out.append(
            {
                "detector": "collapsed_segment",
                "metric": METRIC,
                "dimension": dim,
                "value": value,
                "day": day,
                "actual": float(revenue),
                "baseline": float(baseline_revenue),
                "revenue_lost": lost,
                "description": (
                    f"revenue for {dim}={value} fell to {revenue:.2f} against a typical "
                    f"{baseline_revenue:.2f} ({lost:.2f} lost) - a near-total stop, which a "
                    "percentage-deviation test cannot report once the baseline itself reaches zero"
                ),
            }
        )
    return out


def detect_share_shifts(client, hour_cutoff=None) -> list:
    where_clause = _where(hour_cutoff)
    unioned = " UNION ALL ".join(
        _SHARE_DIM_SELECT.format(dim_col=d, dim_name=f"'{d}'", where_clause=where_clause)
        for d in metrics.scannable_dimensions(METRIC)
    )
    query = _SHARE_QUERY.format(
        unioned=unioned,
        trailing=config.TRAILING_WEEKS,
        min_baseline_samples=config.MIN_BASELINE_SAMPLES,
        min_points=SHARE_SHIFT_MIN_POINTS,
    )
    out = []
    for dim, value, day, share, baseline_share, delta in client.query(query).result_rows:
        out.append(
            {
                "detector": "share_shift",
                "metric": METRIC,
                "dimension": dim,
                "value": value,
                "day": day,
                "share": float(share),
                "baseline_share": float(baseline_share),
                "share_delta_points": float(delta) * 100,
                "description": (
                    f"{dim}={value} moved from {baseline_share:.1%} to {share:.1%} of total revenue "
                    f"({delta * 100:+.1f} percentage points) - a change in revenue composition that "
                    "an absolute per-segment deviation test can miss entirely"
                ),
            }
        )
    return out


def all_signals(client, hour_cutoff=None, volume_floor=None) -> dict:
    """Every revenue-specific detector, run together.

    Returned as its own result rather than merged into anomaly_candidates:
    these answer a different question ("what shape of revenue problem is
    present") than the threshold scan ("did this segment-day move"), carry
    different evidence fields, and conflating them would make the flag count
    meaningless. The UI lists them as a separate, labelled panel.
    """
    drift = detect_sustained_drift(client, hour_cutoff, volume_floor)
    collapsed = detect_collapsed_segments(client, hour_cutoff)
    shifts = detect_share_shifts(client, hour_cutoff)
    return {
        "sustained_drift": drift,
        "collapsed_segment": collapsed,
        "share_shift": shifts,
        "total": len(drift) + len(collapsed) + len(shifts),
    }
