-- Empirical justification for the detection thresholds in backend/app/config.py
-- (MIN_VOLUME_FLOOR, PCT_DEVIATION_THRESHOLD, Z_SCORE_THRESHOLD).
--
-- These numbers are NOT specified anywhere in the InMobi problem statement - it
-- only prescribes the baseline *method* (trailing same-weekday average). The
-- sensitivity is our own calibration, and this file is the evidence for it:
-- instead of asserting "30% deviation is a good cutoff," we measure what
-- "normal" day-to-day noise actually looks like in this exact dataset and set
-- the threshold above that empirical noise floor. Re-run this any time the
-- loaded data changes (e.g. after the Day-2 unseen-incident slice lands) to
-- confirm the thresholds are still appropriately calibrated for the new data.
--
-- Run: docker compose exec -T clickhouse clickhouse-client --user ro --password <pw> --multiquery < scripts/validate_thresholds.sql

-- ============================================================================
-- PART 1: What does "normal" day-to-day deviation actually look like?
-- Unions the same trailing-same-weekday-baseline computation detect.py uses,
-- across all 9 dimensions, for revenue (the highest-weight metric) - no
-- threshold filter applied, so this is the RAW distribution of deviations a
-- well-populated segment shows on an ordinary day. This is what "noise" is
-- made of in this dataset.
-- ============================================================================
WITH all_devs AS (
    SELECT (revenue - baseline_avg) / baseline_avg AS pct_dev, requests
    FROM (
        WITH daily AS (
            SELECT toDate(hour) AS day, ad_format AS segment_value,
                   countMerge(requests) AS requests, sumMerge(revenue) AS revenue
            FROM inmobi_rca.hourly_segment_metrics GROUP BY day, segment_value
        )
        SELECT requests, revenue,
               avg(revenue) OVER (PARTITION BY segment_value, toDayOfWeek(day) ORDER BY day ROWS BETWEEN 4 PRECEDING AND 1 PRECEDING) AS baseline_avg
        FROM daily
    ) WHERE baseline_avg > 0
    UNION ALL
    SELECT (revenue - baseline_avg) / baseline_avg, requests
    FROM (
        WITH daily AS (
            SELECT toDate(hour) AS day, category AS segment_value,
                   countMerge(requests) AS requests, sumMerge(revenue) AS revenue
            FROM inmobi_rca.hourly_segment_metrics GROUP BY day, segment_value
        )
        SELECT requests, revenue,
               avg(revenue) OVER (PARTITION BY segment_value, toDayOfWeek(day) ORDER BY day ROWS BETWEEN 4 PRECEDING AND 1 PRECEDING) AS baseline_avg
        FROM daily
    ) WHERE baseline_avg > 0
    UNION ALL
    SELECT (revenue - baseline_avg) / baseline_avg, requests
    FROM (
        WITH daily AS (
            SELECT toDate(hour) AS day, publisher_tier AS segment_value,
                   countMerge(requests) AS requests, sumMerge(revenue) AS revenue
            FROM inmobi_rca.hourly_segment_metrics GROUP BY day, segment_value
        )
        SELECT requests, revenue,
               avg(revenue) OVER (PARTITION BY segment_value, toDayOfWeek(day) ORDER BY day ROWS BETWEEN 4 PRECEDING AND 1 PRECEDING) AS baseline_avg
        FROM daily
    ) WHERE baseline_avg > 0
    UNION ALL
    SELECT (revenue - baseline_avg) / baseline_avg, requests
    FROM (
        WITH daily AS (
            SELECT toDate(hour) AS day, vertical AS segment_value,
                   countMerge(requests) AS requests, sumMerge(revenue) AS revenue
            FROM inmobi_rca.hourly_segment_metrics GROUP BY day, segment_value
        )
        SELECT requests, revenue,
               avg(revenue) OVER (PARTITION BY segment_value, toDayOfWeek(day) ORDER BY day ROWS BETWEEN 4 PRECEDING AND 1 PRECEDING) AS baseline_avg
        FROM daily
    ) WHERE baseline_avg > 0
    UNION ALL
    SELECT (revenue - baseline_avg) / baseline_avg, requests
    FROM (
        WITH daily AS (
            SELECT toDate(hour) AS day, campaign_type AS segment_value,
                   countMerge(requests) AS requests, sumMerge(revenue) AS revenue
            FROM inmobi_rca.hourly_segment_metrics GROUP BY day, segment_value
        )
        SELECT requests, revenue,
               avg(revenue) OVER (PARTITION BY segment_value, toDayOfWeek(day) ORDER BY day ROWS BETWEEN 4 PRECEDING AND 1 PRECEDING) AS baseline_avg
        FROM daily
    ) WHERE baseline_avg > 0
    UNION ALL
    SELECT (revenue - baseline_avg) / baseline_avg, requests
    FROM (
        WITH daily AS (
            SELECT toDate(hour) AS day, region AS segment_value,
                   countMerge(requests) AS requests, sumMerge(revenue) AS revenue
            FROM inmobi_rca.hourly_segment_metrics GROUP BY day, segment_value
        )
        SELECT requests, revenue,
               avg(revenue) OVER (PARTITION BY segment_value, toDayOfWeek(day) ORDER BY day ROWS BETWEEN 4 PRECEDING AND 1 PRECEDING) AS baseline_avg
        FROM daily
    ) WHERE baseline_avg > 0
    UNION ALL
    SELECT (revenue - baseline_avg) / baseline_avg, requests
    FROM (
        WITH daily AS (
            SELECT toDate(hour) AS day, country AS segment_value,
                   countMerge(requests) AS requests, sumMerge(revenue) AS revenue
            FROM inmobi_rca.hourly_segment_metrics GROUP BY day, segment_value
        )
        SELECT requests, revenue,
               avg(revenue) OVER (PARTITION BY segment_value, toDayOfWeek(day) ORDER BY day ROWS BETWEEN 4 PRECEDING AND 1 PRECEDING) AS baseline_avg
        FROM daily
    ) WHERE baseline_avg > 0
    UNION ALL
    SELECT (revenue - baseline_avg) / baseline_avg, requests
    FROM (
        WITH daily AS (
            SELECT toDate(hour) AS day, device_model AS segment_value,
                   countMerge(requests) AS requests, sumMerge(revenue) AS revenue
            FROM inmobi_rca.hourly_segment_metrics GROUP BY day, segment_value
        )
        SELECT requests, revenue,
               avg(revenue) OVER (PARTITION BY segment_value, toDayOfWeek(day) ORDER BY day ROWS BETWEEN 4 PRECEDING AND 1 PRECEDING) AS baseline_avg
        FROM daily
    ) WHERE baseline_avg > 0
    UNION ALL
    SELECT (revenue - baseline_avg) / baseline_avg, requests
    FROM (
        WITH daily AS (
            SELECT toDate(hour) AS day, os_version AS segment_value,
                   countMerge(requests) AS requests, sumMerge(revenue) AS revenue
            FROM inmobi_rca.hourly_segment_metrics GROUP BY day, segment_value
        )
        SELECT requests, revenue,
               avg(revenue) OVER (PARTITION BY segment_value, toDayOfWeek(day) ORDER BY day ROWS BETWEEN 4 PRECEDING AND 1 PRECEDING) AS baseline_avg
        FROM daily
    ) WHERE baseline_avg > 0
)
SELECT
    'PART 1: normal deviation distribution (revenue, all dims, volume >= 1000)' AS check_name,
    count(*) AS n_samples,
    round(quantile(0.50)(abs(pct_dev)), 4) AS p50_abs_dev,
    round(quantile(0.90)(abs(pct_dev)), 4) AS p90_abs_dev,
    round(quantile(0.95)(abs(pct_dev)), 4) AS p95_abs_dev,
    round(quantile(0.99)(abs(pct_dev)), 4) AS p99_abs_dev,
    round(stddevPop(pct_dev), 4) AS stddev_dev
FROM all_devs
WHERE requests >= 1000
FORMAT PrettyCompact;

-- ============================================================================
-- PART 2: Does low volume make ratio metrics noisier? (justifies MIN_VOLUME_FLOOR)
-- Buckets the SAME deviations above by request volume and shows stddev per
-- bucket. If low-volume buckets show materially higher stddev, that's the
-- empirical justification for excluding them below the floor - they're not
-- "differently behaved," they're statistically noisier from small-N alone.
-- ============================================================================
WITH daily AS (
    SELECT toDate(hour) AS day, country AS segment_value,
           countMerge(requests) AS requests, sumMerge(revenue) AS revenue
    FROM inmobi_rca.hourly_segment_metrics GROUP BY day, segment_value
),
devs AS (
    SELECT requests, revenue,
           avg(revenue) OVER (PARTITION BY segment_value, toDayOfWeek(day) ORDER BY day ROWS BETWEEN 4 PRECEDING AND 1 PRECEDING) AS baseline_avg
    FROM daily
)
SELECT
    'PART 2: deviation stddev by volume bucket (country x revenue)' AS check_name,
    multiIf(requests < 200, '1: <200', requests < 1000, '2: 200-999', requests < 5000, '3: 1000-4999', '4: 5000+') AS volume_bucket,
    count(*) AS n,
    round(stddevPop((revenue - baseline_avg) / baseline_avg), 4) AS stddev_of_deviation
FROM devs
WHERE baseline_avg > 0
GROUP BY volume_bucket
ORDER BY volume_bucket
FORMAT PrettyCompact;

-- ============================================================================
-- PART 3: Sanity check - does the same-weekday baseline correctly NOT flag
-- pure weekly seasonality? The glossary explicitly warns "a flat global
-- average makes every weekend look like an anomaly" and that at least one
-- planted movement is pure seasonality that should be ruled out. This
-- confirms our baseline choice (same-weekday trailing average, not a flat
-- average) handles that trap by construction - every weekend gets compared
-- to trailing weekends, not to weekdays.
-- ============================================================================
SELECT
    'PART 3: overall revenue by day-of-week (raw, no baseline)' AS check_name,
    toDayOfWeek(day) AS day_of_week,
    round(avg(daily_revenue), 2) AS avg_revenue_this_weekday
FROM (
    SELECT toDate(hour) AS day, sumMerge(revenue) AS daily_revenue
    FROM inmobi_rca.hourly_segment_metrics
    GROUP BY day
)
GROUP BY day_of_week
ORDER BY day_of_week
FORMAT PrettyCompact;

-- ============================================================================
-- PART 4: Current threshold's actual flag rate on the known batch (sanity
-- number to compare against future reruns, e.g. after the unseen data lands).
-- ============================================================================
SELECT
    'PART 4: current open anomaly_candidates count' AS check_name,
    count(*) AS open_candidates,
    (SELECT count(*) FROM inmobi_rca.hourly_segment_metrics) AS rollup_rows
FROM inmobi_rca.anomaly_candidates
WHERE status = 'open'
FORMAT PrettyCompact;
