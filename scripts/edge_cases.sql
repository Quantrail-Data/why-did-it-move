-- Reusable edge-case probe. Re-run after ANY data change - especially after
-- the Day-2 unseen-incident slice lands - the same way
-- scripts/validate_thresholds.sql is re-run. Findings and their consequences
-- are written up in EDGE_CASES.md; this file is the executable form.
--
--   docker compose exec -T clickhouse clickhouse-client --user ro \
--     --password <pw> --multiquery --format PrettyCompactMonoBlock \
--     < scripts/edge_cases.sql
--
-- Every check is designed so that 0 is the expected/clean answer, except
-- where noted, so an eyeball scan of the output is enough.

-- ============================================================
-- PART 1 - Funnel integrity on the raw fact table
-- Expected: all zero. A non-zero here means the metric formulas in
-- metrics_glossary.md cannot be applied as written.
-- ============================================================
SELECT 'A1 clicks without impression'      AS check, count() AS n FROM inmobi_rca.ad_events WHERE is_click = 1 AND is_impression = 0;
SELECT 'A2 impressions without fill'       AS check, count() AS n FROM inmobi_rca.ad_events WHERE is_impression = 1 AND is_filled = 0;
SELECT 'A3 revenue on unfilled request'    AS check, count() AS n FROM inmobi_rca.ad_events WHERE is_filled = 0 AND revenue != 0;
SELECT 'A4 revenue without impression'     AS check, count() AS n FROM inmobi_rca.ad_events WHERE is_impression = 0 AND revenue != 0;
SELECT 'A5 negative revenue'               AS check, count() AS n FROM inmobi_rca.ad_events WHERE revenue < 0;
SELECT 'A6 filled but no advertiser'       AS check, count() AS n FROM inmobi_rca.ad_events WHERE is_filled = 1 AND advertiser_id = '';
SELECT 'A7 unfilled but has advertiser'    AS check, count() AS n FROM inmobi_rca.ad_events WHERE is_filled = 0 AND advertiser_id != '';
SELECT 'A8 flags outside {0,1}'            AS check, count() AS n FROM inmobi_rca.ad_events WHERE is_filled > 1 OR is_impression > 1 OR is_click > 1;

-- ============================================================
-- PART 2 - Referential integrity. Expected: all zero.
-- A non-zero means the rollup's LEFT JOINs are resolving that dimension to
-- '' and inventing a phantom segment.
-- ============================================================
SELECT 'B1 orphan app_id' AS check, count() AS n
FROM inmobi_rca.ad_events e LEFT ANTI JOIN inmobi_rca.apps a ON e.app_id = a.app_id;
SELECT 'B2 orphan geo_device_id' AS check, count() AS n
FROM inmobi_rca.ad_events e LEFT ANTI JOIN inmobi_rca.geo_device g ON e.geo_device_id = g.geo_device_id;
SELECT 'B3 orphan advertiser_id (non-blank)' AS check, count() AS n
FROM (SELECT advertiser_id FROM inmobi_rca.ad_events WHERE advertiser_id != '') e
LEFT ANTI JOIN inmobi_rca.advertisers d ON e.advertiser_id = d.advertiser_id;

-- ============================================================
-- PART 3 - Ratio bounds at the grain the pipeline actually evaluates.
-- Expected: all zero. Non-zero means a metric formula is producing an
-- impossible value (>100% fill/render/click-through).
-- ============================================================
SELECT 'C1 ratio bound violations (day x country)' AS check,
       countIf(fills > reqs) AS fill_rate_gt_1,
       countIf(imps > fills) AS render_rate_gt_1,
       countIf(clicks > imps) AS ctr_gt_1
FROM (SELECT toDate(hour) AS d, country AS sv, countMerge(requests) AS reqs, sumMerge(fills) AS fills,
             sumMerge(impressions) AS imps, sumMerge(clicks) AS clicks
      FROM inmobi_rca.hourly_segment_metrics GROUP BY d, sv);

-- ============================================================
-- PART 4 - DAY COMPLETENESS. Not expected to be empty on Day 2.
-- Any day with hours_present < 24 is partial. backend/app/coverage.py
-- restricts that day AND its baselines to the same hour window; confirm the
-- day listed here matches what /api/scan's coverage block reports.
-- ============================================================
SELECT 'D1 partial days' AS check, toDate(hour) AS day,
       uniqExact(toHour(hour)) AS hours_present, max(toHour(hour)) AS max_hour
FROM inmobi_rca.hourly_segment_metrics
GROUP BY day HAVING hours_present < 24 ORDER BY day;

-- ============================================================
-- PART 5 - BASELINE COVERAGE. Expected: the first ~14 days have < 2 prior
-- same-weekday observations and are correctly NOT evaluated (see
-- config.MIN_BASELINE_SAMPLES). Confirm the count of such days matches what
-- the dashboard shows as "Not evaluated" rather than green/normal.
-- ============================================================
SELECT 'E1 prior same-weekday samples per day' AS check, day, prior_samples,
       if(prior_samples < 2, 'NOT EVALUATED', 'evaluated') AS status
FROM (
  SELECT toDate(hour) AS day,
         count() OVER (PARTITION BY toDayOfWeek(toDate(hour)) ORDER BY toDate(hour)
                       ROWS BETWEEN 4 PRECEDING AND 1 PRECEDING) AS prior_samples
  FROM (SELECT DISTINCT toStartOfDay(hour) AS hour FROM inmobi_rca.hourly_segment_metrics)
) ORDER BY day;

-- ============================================================
-- PART 6 - BASELINE CONTAMINATION. A real incident poisons the trailing
-- baseline of the following same-weekdays and manufactures a phantom anomaly
-- in the opposite direction. `overstatement` is how much the mean baseline
-- exaggerates vs the robust median one the pipeline now uses.
-- Large values here confirm the median baseline is doing real work.
-- ============================================================
SELECT 'F1 mean vs median baseline (revenue)' AS check, day, round(revenue,2) AS revenue,
       round(base_mean,2) AS baseline_mean, round((revenue-base_mean)/base_mean,4) AS pct_dev_mean,
       round(base_median,2) AS baseline_median, round((revenue-base_median)/base_median,4) AS pct_dev_median,
       round(abs((revenue-base_mean)/base_mean) - abs((revenue-base_median)/base_median), 4) AS overstatement
FROM (
  SELECT day, revenue, avg(revenue) OVER w AS base_mean, quantileExact(0.5)(revenue) OVER w AS base_median
  FROM (SELECT toDate(hour) AS day, sumMerge(revenue) AS revenue
        FROM inmobi_rca.hourly_segment_metrics GROUP BY day)
  WINDOW w AS (PARTITION BY toDayOfWeek(day) ORDER BY day ROWS BETWEEN 4 PRECEDING AND 1 PRECEDING)
)
WHERE isFinite(base_mean) AND base_mean > 0 AND base_median > 0
ORDER BY overstatement DESC LIMIT 10;

-- ============================================================
-- PART 7 - DEGENERATE CUTS. Expected: fill_rate is exactly 1 for every
-- non-blank vertical/campaign_type and 0 for the blank one. That is why
-- metrics.DEGENERATE_METRIC_DIMENSIONS skips those cuts instead of running
-- them and reporting "checked, normal". If this ever shows variance, the
-- exclusion should be revisited.
-- ============================================================
SELECT 'G1 fill_rate by vertical' AS check, vertical,
       round(sumMerge(fills)/countMerge(requests), 6) AS fill_rate
FROM inmobi_rca.hourly_segment_metrics GROUP BY vertical ORDER BY vertical;

SELECT 'G2 fill_rate by campaign_type' AS check, campaign_type,
       round(sumMerge(fills)/countMerge(requests), 6) AS fill_rate
FROM inmobi_rca.hourly_segment_metrics GROUP BY campaign_type ORDER BY campaign_type;

-- ============================================================
-- PART 8 - THE BLANK PSEUDO-SEGMENT. Expected: fills/impressions/clicks/
-- revenue all exactly 0. This is unfilled traffic, not a segment, and is
-- excluded by name (metrics.BLANK_SEGMENT_VALUE) rather than by accident.
-- ============================================================
SELECT 'H1 blank vertical is all-zero' AS check,
       countMerge(requests) AS requests, sumMerge(fills) AS fills,
       sumMerge(impressions) AS impressions, sumMerge(clicks) AS clicks, sumMerge(revenue) AS revenue
FROM inmobi_rca.hourly_segment_metrics WHERE vertical = '';

SELECT 'H2 blank share of rollup rows' AS check,
       countIf(vertical = '') AS blank_rows, count() AS total_rows,
       round(countIf(vertical = '') / count(), 4) AS share
FROM inmobi_rca.hourly_segment_metrics;

-- ============================================================
-- PART 9 - ROLLUP vs RAW cross-check. THE check that caught the ORDER BY
-- corruption bug. Expected: every diff exactly 0. Run this after EVERY load;
-- a rollup that runs without error is not a rollup that is correct.
--
-- H0 MUST BE CLEAN BEFORE I1 MEANS ANYTHING. The dimension tables are
-- ReplacingMergeTree, which deduplicates only on merge. If a load inserted a
-- second copy of a dimension row and no merge has happened yet, the raw side's
-- JOIN fans out and every country reports exactly 2x - which looks identical
-- to catastrophic rollup corruption and is not. This was hit for real: a
-- refused load left duplicate dimension rows behind and I1 reported "16
-- mismatched countries, max diff 5050.7" against a perfectly correct rollup.
-- Check H0 first; if it is non-zero, run
--   OPTIMIZE TABLE inmobi_rca.{apps,advertisers,geo_device} FINAL
-- and re-run, before concluding anything about the rollup.
-- ============================================================
SELECT 'H0 duplicate dimension rows (must be 0 before trusting I1)' AS check,
       (SELECT count() - uniqExact(app_id) FROM inmobi_rca.apps) AS dup_apps,
       (SELECT count() - uniqExact(advertiser_id) FROM inmobi_rca.advertisers) AS dup_advertisers,
       (SELECT count() - uniqExact(geo_device_id) FROM inmobi_rca.geo_device) AS dup_geo_device;

-- FINAL on the dimension side makes this check correct even when a merge is
-- still pending, so it measures the rollup rather than the merge schedule.
SELECT 'I1 rollup vs raw by country (whole dataset)' AS check,
       countIf(abs(rollup_rev - raw_rev) > 0.000001) AS mismatched_countries,
       max(abs(rollup_rev - raw_rev)) AS max_abs_diff
FROM (
  WITH
    rollup AS (SELECT country, sumMerge(revenue) AS rev FROM inmobi_rca.hourly_segment_metrics GROUP BY country),
    raw AS (SELECT gd.country AS country, sum(e.revenue) AS rev
            FROM inmobi_rca.ad_events e
            INNER JOIN (SELECT geo_device_id, country FROM inmobi_rca.geo_device FINAL) AS gd
                    ON e.geo_device_id = gd.geo_device_id
            GROUP BY country)
  SELECT rollup.country AS country, rollup.rev AS rollup_rev, raw.rev AS raw_rev
  FROM rollup INNER JOIN raw ON rollup.country = raw.country
);

-- ============================================================
-- PART 10 - Scale/cardinality snapshot. Not pass/fail - these are the
-- numbers SCALABILITY.md's projections are built from. Re-read them after
-- new data lands, since the rollup's usefulness is governed by the ratio
-- between events per hour and populated combinations per hour.
-- ============================================================
SELECT 'J1 cardinality snapshot' AS check,
       (SELECT count() FROM inmobi_rca.ad_events) AS raw_events,
       count() AS rollup_rows,
       uniqExact(hour) AS hours,
       round(count() / uniqExact(hour), 0) AS avg_rollup_rows_per_hour,
       uniqExact((ad_format,category,publisher_tier,vertical,campaign_type,region,country,device_model,os_version)) AS distinct_combinations
FROM inmobi_rca.hourly_segment_metrics;
