"""Detection thresholds computed live from whatever data is currently
loaded, instead of a single hardcoded percentage shared across every
metric. This is the automated version of what scripts/validate_thresholds.sql
already does by hand: measure the empirical distribution of "normal"
day-to-day deviation in the actual dataset, and set the cutoff just above
that noise floor. The difference is this runs inside the pipeline itself
(detect.py's scan, investigate.py's drill-down) every time, so it stays
correct whether the loaded data is the known 5-week batch, the Day-2
unseen-incident slice, or rows arriving one at a time via /api/ingest/events
- none of which necessarily share the known batch's exact noise profile.
"""
import threading
import time

from . import config, metrics

# Short TTL cache: compute_metric_thresholds does up to 5 heavy UNION-ALL
# queries across all 9 dimensions, and was being called fresh on every
# /api/scan, /api/investigate, AND /api/metric-tree request - including
# every page load/reload, which stacks up fast (confirmed directly: 3
# concurrent /api/metric-tree calls took ~143s each, ClickHouse itself
# became the bottleneck under that load). The empirical noise distribution
# this measures does not meaningfully change between two page loads a few
# seconds apart, so caching it for a short window is a straightforward,
# correctness-preserving win - worst case, a threshold lags freshly-loaded
# data by up to CACHE_TTL_SECONDS, an explicit and small tradeoff, not
# silently stale forever. Keyed by the exact metric set requested (scan /
# metric-tree ask for headline metrics; investigate asks for a larger set
# including decomposition factors), so different callers don't collide.
CACHE_TTL_SECONDS = 120
_cache_lock = threading.Lock()
_cache: dict = {}

# Same trailing-same-weekday-baseline computation detect.py's
# _DAILY_SEGMENT_QUERY uses, reduced to just (pct_dev, requests) for one
# dimension. Unioned across every dimension in the caller below - this is
# scripts/validate_thresholds.sql Part 1, generalized from 9 hand-copied
# blocks (one per dimension) into one Python-built query.
_DIM_DEVIATION_SUBQUERY = """
    SELECT requests, (actual_value - baseline_avg) / baseline_avg AS pct_dev
    FROM (
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
            requests,
            {metric_expr} AS actual_value,
            quantileExact(0.5)({metric_expr}) OVER (
                PARTITION BY segment_value, toDayOfWeek(day)
                ORDER BY day
                ROWS BETWEEN {trailing} PRECEDING AND 1 PRECEDING
            ) AS baseline_avg,
            count({metric_expr}) OVER (
                PARTITION BY segment_value, toDayOfWeek(day)
                ORDER BY day
                ROWS BETWEEN {trailing} PRECEDING AND 1 PRECEDING
            ) AS baseline_n
        FROM daily
    )
    -- Same robust (median) baseline detect.py/investigate.py now use - see
    -- baseline.py. Measuring the noise distribution with a mean while
    -- flagging against a median would calibrate the threshold against a
    -- different statistic than the one being thresholded.
    WHERE baseline_avg > 0 AND baseline_n >= {min_baseline_samples}
"""

_METRIC_THRESHOLD_QUERY = """
    SELECT
        count() AS n,
        quantile(0.95)(abs(pct_dev)) AS pct_p95,
        quantile(0.10)(requests) AS vol_p10
    FROM ( {unioned_subqueries} )
    WHERE isFinite(pct_dev) AND requests > 0
"""


def compute_metric_thresholds(client, metric_names) -> dict:
    """Returns {metric_name: {pct_threshold, volume_floor, n_samples, dynamic}}.

    pct_threshold: this metric's own empirical p95 |deviation| across every
    dimension's day-to-day trailing-baseline comparison - the anomaly cutoff.
    volume_floor: p10 of request volume among the same population - segments
    below this are the ones validate_thresholds.sql Part 2 showed are
    statistically noisier from small-N alone, not "differently behaved."

    Falls back to the static config constants when there isn't enough
    history yet to trust a percentile (cold start on a small/new batch).
    """
    cache_key = frozenset(metric_names)
    with _cache_lock:
        cached = _cache.get(cache_key)
        if cached is not None and (time.monotonic() - cached[0]) < CACHE_TTL_SECONDS:
            return cached[1]

    result = {}
    for metric_name in metric_names:
        metric_expr = metrics.METRIC_EXPRESSIONS[metric_name]
        # Degenerate cuts excluded: fill_rate is 1.0 by construction inside
        # every vertical/campaign_type (see metrics.DEGENERATE_METRIC_DIMENSIONS),
        # so including them would stuff the noise distribution with thousands
        # of exactly-zero deviations and drag the p95 cutoff artificially low.
        subqueries = [
            _DIM_DEVIATION_SUBQUERY.format(
                dim_col=dim_col,
                metric_expr=metric_expr,
                trailing=config.TRAILING_WEEKS,
                min_baseline_samples=config.MIN_BASELINE_SAMPLES,
            )
            for dim_col in metrics.scannable_dimensions(metric_name)
        ]
        query = _METRIC_THRESHOLD_QUERY.format(unioned_subqueries=" UNION ALL ".join(subqueries))
        row = client.query(query).result_rows[0]
        n, pct_p95, vol_p10 = row

        if not n or n < config.MIN_THRESHOLD_SAMPLES:
            result[metric_name] = {
                "pct_threshold": config.PCT_DEVIATION_THRESHOLD,
                "volume_floor": config.MIN_VOLUME_FLOOR,
                "n_samples": int(n or 0),
                "dynamic": False,
            }
        else:
            result[metric_name] = {
                "pct_threshold": max(float(pct_p95), config.MIN_PCT_DEVIATION_THRESHOLD),
                "volume_floor": max(int(vol_p10), config.MIN_VOLUME_FLOOR_ABSOLUTE),
                "n_samples": int(n),
                "dynamic": True,
            }

    with _cache_lock:
        _cache[cache_key] = (time.monotonic(), result)
    return result
