-- Runs after 00-init-db.sh (alphabetical order in /docker-entrypoint-initdb.d/,
-- and the ro user it creates must exist before this file's GRANT at the
-- bottom). Statements in one file run sequentially, so DB/tables exist
-- before that GRANT executes.

CREATE DATABASE IF NOT EXISTS inmobi_rca;

-- Fact table: one row per ad request (~9M rows / 5 weeks in the known batch,
-- growing when the unseen-incident slice lands Day 2 - same schema, new
-- days, no table change needed). ORDER BY leads with event_time because
-- every investigation query starts by filtering to a day/window before
-- slicing by dimension. ad_format is second because it's the only
-- low-cardinality dimension that lives directly on the fact table (everyone
-- else comes from the joined dimension tables via the rollup below).
-- Partitioned by day: 35 partitions for the known batch, one more per
-- unseen day - keeps "scan just this day" queries cheap.
CREATE TABLE IF NOT EXISTS inmobi_rca.ad_events
(
    event_time      DateTime CODEC(DoubleDelta, ZSTD),
    app_id          LowCardinality(String),
    geo_device_id   String,
    advertiser_id   String,
    ad_format       LowCardinality(String),
    is_filled       UInt8,
    is_impression   UInt8,
    is_click        UInt8,
    revenue         Float64
)
ENGINE = MergeTree
PARTITION BY toDate(event_time)
ORDER BY (event_time, ad_format, app_id);

-- Dimension tables: small (500-5000 rows). ReplacingMergeTree so a future
-- reload/correction is idempotent instead of needing manual dedup.
CREATE TABLE IF NOT EXISTS inmobi_rca.apps
(
    app_id          String,
    category        LowCardinality(String),
    publisher_tier  LowCardinality(String)
)
ENGINE = ReplacingMergeTree
ORDER BY app_id;

CREATE TABLE IF NOT EXISTS inmobi_rca.advertisers
(
    advertiser_id   String,
    vertical        LowCardinality(String),
    campaign_type   LowCardinality(String)
)
ENGINE = ReplacingMergeTree
ORDER BY advertiser_id;

CREATE TABLE IF NOT EXISTS inmobi_rca.geo_device
(
    geo_device_id   String,
    region          LowCardinality(String),
    country         LowCardinality(String),
    device_model    LowCardinality(String),
    os_version      LowCardinality(String)
)
ENGINE = ReplacingMergeTree
ORDER BY geo_device_id;

-- Serving-layer rollup: pre-joins ad_events against all 3 dimension tables
-- and pre-aggregates to hourly grain per full dimension combo. This is what
-- both the background scan and on-demand drill-down actually query - never
-- the raw fact table - avoiding a 3-way join at query time. AggregatingMergeTree
-- + *State because fill rate/eCPM/CTR are sum/sum ratios (the glossary is
-- explicit these must never be averaged per-row) - storing sumState lets us
-- correctly re-aggregate to any hour/day/week grain later.
--
-- ORDER BY MUST list every one of the 10 group-by columns, not a subset.
-- Learned this the hard way: an earlier version used
-- ORDER BY (hour, ad_format, category, region) - 4 of the 10 - reasoning it
-- was just a sort/performance choice. It is not. For AggregatingMergeTree,
-- ORDER BY is a row's *merge identity*. Any grouping column left out of it
-- (country, publisher_tier, vertical, campaign_type, device_model, os_version
-- in that earlier version) gets silently collapsed to an arbitrary value
-- whenever ClickHouse background-merges two parts that share the same
-- ORDER BY tuple but differ in the excluded columns - no error, just quietly
-- wrong numbers once merges happen (which they did, within hours of load).
-- Caught by cross-checking a query result against a from-scratch computation
-- on raw ad_events: every excluded column mismatched the raw data (country,
-- vertical), every included column matched exactly (category, region) -
-- that pattern only has one explanation. Rebuilt with the full key below;
-- re-verified every dimension against raw ad_events afterward, whole-dataset
-- and single-day, all exact matches. See INMOBI_CONTEXT.md for the full
-- incident writeup and scripts/validate_thresholds.sql for reusable checks.
--
-- Trade-off of the fix: the full 10-column key means far less aggregation
-- (~7.9M rollup rows vs 9M raw, not the "orders of magnitude" a 4-column key
-- would give you) - correctness required it. The rollup still earns its keep
-- by pre-computing the 3-way join once instead of on every query, and by
-- storing pre-summed aggregate state rather than raw per-event rows.
CREATE TABLE IF NOT EXISTS inmobi_rca.hourly_segment_metrics
(
    hour            DateTime,
    ad_format       LowCardinality(String),
    category        LowCardinality(String),
    publisher_tier  LowCardinality(String),
    vertical        LowCardinality(String),
    campaign_type   LowCardinality(String),
    region          LowCardinality(String),
    country         LowCardinality(String),
    device_model    LowCardinality(String),
    os_version      LowCardinality(String),
    requests        AggregateFunction(count),
    fills           AggregateFunction(sum, UInt8),
    impressions     AggregateFunction(sum, UInt8),
    clicks          AggregateFunction(sum, UInt8),
    revenue         AggregateFunction(sum, Float64)
)
ENGINE = AggregatingMergeTree
PARTITION BY toDate(hour)
ORDER BY (hour, ad_format, category, publisher_tier, vertical, campaign_type, region, country, device_model, os_version);

-- Fires on every INSERT into ad_events (including our one-time bulk loads),
-- processed block-wise - works the same for a 9M-row batch insert as it
-- would for a trickle of individual inserts. LEFT JOINs so an event with a
-- dimension id not yet in the lookup tables doesn't get silently dropped.
CREATE MATERIALIZED VIEW IF NOT EXISTS inmobi_rca.mv_hourly_segment_metrics
TO inmobi_rca.hourly_segment_metrics
AS
SELECT
    toStartOfHour(e.event_time)     AS hour,
    e.ad_format                     AS ad_format,
    a.category                      AS category,
    a.publisher_tier                AS publisher_tier,
    -- advertiser_id is '' on unfilled requests, so vertical/campaign_type
    -- resolve to '' via the LEFT JOIN too - matches the glossary's note that
    -- these columns only exist for filled events.
    coalesce(adv.vertical, '')      AS vertical,
    coalesce(adv.campaign_type, '') AS campaign_type,
    g.region                        AS region,
    g.country                       AS country,
    g.device_model                  AS device_model,
    g.os_version                    AS os_version,
    countState()                    AS requests,
    sumState(e.is_filled)           AS fills,
    sumState(e.is_impression)       AS impressions,
    sumState(e.is_click)            AS clicks,
    sumState(e.revenue)             AS revenue
FROM inmobi_rca.ad_events AS e
LEFT JOIN inmobi_rca.apps AS a ON e.app_id = a.app_id
LEFT JOIN inmobi_rca.advertisers AS adv ON e.advertiser_id = adv.advertiser_id
LEFT JOIN inmobi_rca.geo_device AS g ON e.geo_device_id = g.geo_device_id
GROUP BY hour, ad_format, category, publisher_tier, vertical, campaign_type, region, country, device_model, os_version;

-- Background-scan output: candidates the system found on its own, before any
-- human or on-demand query pointed at them. ORDER BY leads with day since
-- that's how the UI lists/filters and how the unseen-incident rescan queries
-- "what's new since the last scan."
CREATE TABLE IF NOT EXISTS inmobi_rca.anomaly_candidates
(
    id             UUID DEFAULT generateUUIDv4(),
    detected_at    DateTime DEFAULT now(),
    day            Date,
    metric         LowCardinality(String),
    segment_dims   Map(String, String),
    baseline_value Float64,
    actual_value   Float64,
    pct_deviation  Float64,
    z_score        Float64,
    status         Enum8('open' = 1, 'investigated' = 2, 'dismissed' = 3) DEFAULT 'open',
    -- How many prior same-weekday observations backed the baseline this
    -- candidate was measured against. A -45% move off 4 prior weeks and the
    -- same -45% off 1 are not equally strong evidence, and the UI must not
    -- present them as if they were. See config.MIN_BASELINE_SAMPLES.
    baseline_n     UInt8 DEFAULT 0,
    -- Non-empty only when the day was partially loaded and therefore
    -- evaluated over a restricted hour window (e.g. '00:00-13:59') against
    -- the same hours of its baselines - see backend/app/coverage.py.
    evaluated_hours String DEFAULT ''
)
ENGINE = MergeTree
ORDER BY (day, metric);

-- Final agent output. langfuse_trace_id is the join key back to the full
-- reasoning chain in Langfuse - ClickHouse stores the judged artifact (the
-- diagnosis + the numbers), Langfuse stores the "how we got there," so a
-- judge clicks from one to the other instead of us duplicating trace data
-- in both places.
CREATE TABLE IF NOT EXISTS inmobi_rca.investigations
(
    id                     UUID DEFAULT generateUUIDv4(),
    created_at             DateTime DEFAULT now(),
    anomaly_candidate_id   Nullable(UUID),
    metric                 LowCardinality(String),
    day                    Date,
    diagnosis_text         String,
    responsible_segment    Map(String, String),
    checked_and_ruled_out  Array(String),
    cited_numbers          String,   -- JSON {label: value}, every value reproducible from hourly_segment_metrics
    confidence             Float32,
    langfuse_trace_id      String
)
ENGINE = MergeTree
ORDER BY (day, metric, created_at);

-- Chat stretch feature (/api/ask): free-form questions and the structured
-- answer the pipeline computed for them. Kept separate from `investigations`
-- because these are ad-hoc lookups, not full detect->drill-down
-- investigations - different shape, different lifecycle.
CREATE TABLE IF NOT EXISTS inmobi_rca.chat_queries
(
    id                UUID DEFAULT generateUUIDv4(),
    created_at        DateTime DEFAULT now(),
    question          String,
    answer_text       String,
    cited_numbers     String,   -- JSON {label: value}
    langfuse_trace_id String
)
ENGINE = MergeTree
ORDER BY created_at;

-- Per-call latency log, one row per /api/investigate run - what p50/p95/p99
-- latency is computed FROM. A single run's timing (backend/app/timing.py,
-- shown on every diagnosis) tells you how long THAT diagnosis took; it
-- cannot tell you whether that run was typical or a lucky outlier. p95 needs
-- a distribution across many calls, which needs the calls persisted
-- somewhere queryable - this table is that, and ClickHouse's quantile()
-- computes the percentiles, same "ClickHouse does the analysis" pattern as
-- everything else in this schema, not a number computed in Python.
-- ORDER BY endpoint first since every query filters to one endpoint
-- (currently just 'investigate') before computing its percentiles.
CREATE TABLE IF NOT EXISTS inmobi_rca.request_latencies
(
    created_at   DateTime DEFAULT now(),
    endpoint     LowCardinality(String),
    total_ms     Float64,
    clickhouse_ms Float64,
    llm_ms       Float64
)
ENGINE = MergeTree
ORDER BY (endpoint, created_at);

GRANT SELECT ON inmobi_rca.* TO ro;
