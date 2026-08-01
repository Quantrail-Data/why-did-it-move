"""Drill-down pipeline: given a metric + day (flagged by detect.py, or
supplied on demand), find WHICH factor moved and WHICH segment is
responsible, explicitly recording what was checked and ruled out along the
way. Every number here comes from a ClickHouse query; the LLM (llm.narrate)
is called exactly once at the end, on the finished structured result.
"""
import json
from datetime import date
from typing import Optional

from . import baseline as baseline_module
from . import config, coverage as coverage_module, db, llm, metrics, thresholds as thresholds_module, timing, tracing

# Every metric investigate() might need a threshold for: the requested
# metric itself (could be any of metrics.METRIC_EXPRESSIONS via manual
# investigate/chat, not just a headline one), plus the revenue-decomposition
# factors, plus every headline metric (metric-tree/scan reuse this too).
_DECOMPOSITION_FACTORS = ("requests", "fill_rate", "render_rate", "ecpm")

_OVERALL_QUERY = """
    WITH daily AS (
        SELECT
            toDate(hour) AS day,
            countMerge(requests) AS requests,
            sumMerge(fills) AS fills,
            sumMerge(impressions) AS impressions,
            sumMerge(clicks) AS clicks,
            sumMerge(revenue) AS revenue
        FROM inmobi_rca.hourly_segment_metrics
        {where_clause}
        GROUP BY day
    )
    SELECT
        day,
        {metric_expr} AS actual_value,
        {baseline_cols}
    FROM daily
    ORDER BY day
"""

_SEGMENT_QUERY = """
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
    ORDER BY segment_value, day
"""


def _build_overall_query(metric_expr: str, hour_cutoff=None) -> str:
    hour_filter = coverage_module.hour_filter_sql(hour_cutoff)
    return _OVERALL_QUERY.format(
        metric_expr=metric_expr,
        where_clause=f"WHERE {hour_filter}" if hour_filter else "",
        baseline_cols=baseline_module.baseline_select(
            metric_expr, "toDayOfWeek(day)", config.TRAILING_WEEKS
        ),
    )


def _build_segment_query(dim_col: str, metric_expr: str, hour_cutoff=None) -> str:
    hour_filter = coverage_module.hour_filter_sql(hour_cutoff)
    return _SEGMENT_QUERY.format(
        dim_col=dim_col,
        metric_expr=metric_expr,
        where_clause=f"WHERE {hour_filter}" if hour_filter else "",
        baseline_cols=baseline_module.baseline_select(
            metric_expr, "segment_value, toDayOfWeek(day)", config.TRAILING_WEEKS
        ),
    )
# NOTE: deliberately no `WHERE day = ...` here. SQL's logical order runs
# WHERE before window functions, so filtering to one day in the SQL itself
# would leave the trailing-baseline window with zero prior rows to look at
# (confirmed against a real run: every baseline came back NaN, window frame
# size 0, even on the dataset's last day with a month of history behind it).
# Fetch the full range, filter to the target day in Python instead - same
# pattern _OVERALL_QUERY and detect.py's scan already use correctly.


def daily_deviation_series(client, metric_name: str) -> list:
    """Every day's actual vs trailing-same-weekday-baseline for one metric,
    across the whole loaded range (no segment filter, no day filter) - powers
    the metric-history timeline (frontend/src/components/MetricHistoryTimeline.jsx),
    which replaced a manual metric/day picker with a clickable per-day chart:
    click any day, flagged or not, to investigate it. Also the source of
    truth for compute_daily_deviation below, so both stay exactly consistent.

    Each point also carries baseline_n (how many prior same-weekday
    observations backed it) and evaluated_hours (non-empty when the day was
    only partly loaded and was therefore compared over a restricted hour
    window). Days with too little history to judge are returned with
    pct_deviation=None and an explicit reason, so the UI can render "not
    evaluated" differently from "evaluated and clean" - see coverage.py.
    """
    coverage = coverage_module.day_coverage(client)
    metric_expr = metrics.METRIC_EXPRESSIONS[metric_name]

    # One unrestricted pass for the complete days, plus one hour-restricted
    # pass per partial day. Realistically there is at most one partial day
    # (the last), so this is 1-2 queries, not N.
    rows_by_day = {}
    passes = [(None, {d for d, i in coverage.items() if i["complete"]})]
    for partial_day in coverage_module.partial_days(coverage):
        passes.append((coverage_module.hour_cutoff_for(coverage, partial_day), {partial_day}))

    for hour_cutoff, days_in_pass in passes:
        if not days_in_pass:
            continue
        query = _build_overall_query(metric_expr, hour_cutoff)
        for row in client.query(query).result_rows:
            row_day, actual, baseline, baseline_mean, _stddev, baseline_n = row
            if row_day in days_in_pass:
                rows_by_day[row_day] = (actual, baseline, baseline_mean, baseline_n, hour_cutoff)

    series = []
    for row_day in sorted(rows_by_day):
        actual, baseline, baseline_mean, baseline_n, hour_cutoff = rows_by_day[row_day]
        point = {
            "day": row_day.isoformat(),
            "actual": None if metrics.is_invalid_number(actual) else float(actual),
            "baseline": None,
            "pct_deviation": None,
            "baseline_n": int(baseline_n or 0),
            "evaluated_hours": "" if hour_cutoff is None else f"00:00-{hour_cutoff:02d}:59",
            "not_evaluated_reason": None,
        }
        if point["actual"] is None:
            point["not_evaluated_reason"] = "no data for this day"
        elif metrics.is_invalid_number(baseline) or baseline == 0:
            point["not_evaluated_reason"] = "no trailing same-weekday history to compare against"
        elif (baseline_n or 0) < config.MIN_BASELINE_SAMPLES:
            point["baseline"] = float(baseline)
            point["not_evaluated_reason"] = (
                f"only {int(baseline_n or 0)} prior same-weekday observation(s); "
                f"{config.MIN_BASELINE_SAMPLES} required before a deviation is treated as evidence"
            )
        else:
            point["baseline"] = float(baseline)
            point["pct_deviation"] = float((actual - baseline) / baseline)
            if not metrics.is_invalid_number(baseline_mean):
                point["baseline_mean"] = float(baseline_mean)
        series.append(point)
    return series


def compute_daily_deviation(client, day: date, metric_name: str) -> Optional[dict]:
    """Actual vs trailing-same-weekday-baseline for one metric, across the
    whole dataset (no segment filter) - the "did the top-line number move"
    check, reused for both factor decomposition and the dashboard metric tree.
    """
    day_str = day.isoformat()
    for point in daily_deviation_series(client, metric_name):
        if point["day"] == day_str:
            return {
                "metric": metric_name,
                "actual": point["actual"],
                "baseline": point["baseline"],
                "pct_deviation": point["pct_deviation"],
                "baseline_n": point["baseline_n"],
                "evaluated_hours": point["evaluated_hours"],
                "not_evaluated_reason": point["not_evaluated_reason"],
            }
    return None


# Sentinel so callers can pass hour_cutoff=None ("this day is complete, do
# not restrict") distinctly from omitting it ("work it out yourself"). The
# latter costs an extra coverage query, so callers that already know the
# cutoff (investigate()) pass it explicitly.
_UNSET = object()


def segment_ranking(
    client,
    day: date,
    metric_name: str,
    dim_col: str,
    volume_floor: Optional[int] = None,
    hour_cutoff=_UNSET,
) -> list:
    """Every segment value of one dimension, for one metric, on one day -
    ranked by |deviation| descending. dim_col must come from
    metrics.DIMENSIONS (a fixed whitelist), never from raw user input.

    Fetches the full date range (see the note above _SEGMENT_QUERY) and
    filters to `day` here in Python - `day` is a Pydantic-validated `date`
    object either way, safe to compare directly against ClickHouse's
    returned `datetime.date` values.

    volume_floor defaults to the static config value when the caller hasn't
    already computed a dynamic one (thresholds.compute_metric_thresholds) -
    kept optional so this function still works standalone (e.g. a future
    script/test) without forcing every caller through the dynamic path.
    """
    if volume_floor is None:
        volume_floor = config.MIN_VOLUME_FLOOR
    if hour_cutoff is _UNSET:
        coverage = coverage_module.day_coverage(client)
        hour_cutoff = coverage_module.hour_cutoff_for(coverage, day)
    query = _build_segment_query(dim_col, metrics.METRIC_EXPRESSIONS[metric_name], hour_cutoff)
    rows = client.query(query).result_rows

    ranked = []
    for row_day, segment_value, requests, actual, baseline, baseline_mean, _stddev, baseline_n in rows:
        if row_day != day or requests < volume_floor:
            continue
        # Unfilled traffic is not a segment - see metrics.BLANK_SEGMENT_VALUE.
        if str(segment_value) == metrics.BLANK_SEGMENT_VALUE:
            continue
        if metrics.is_invalid_number(actual) or metrics.is_invalid_number(baseline) or baseline == 0:
            continue
        if (baseline_n or 0) < config.MIN_BASELINE_SAMPLES:
            continue
        pct_dev = (actual - baseline) / baseline
        ranked.append(
            {
                "dimension": dim_col,
                "value": segment_value,
                "requests": requests,
                "actual": float(actual),
                "baseline": float(baseline),
                "pct_deviation": float(pct_dev),
                "baseline_n": int(baseline_n or 0),
                "baseline_mean": (
                    None if metrics.is_invalid_number(baseline_mean) else float(baseline_mean)
                ),
            }
        )
    ranked.sort(key=lambda r: abs(r["pct_deviation"]), reverse=True)
    return ranked


def segment_value_lookup(client, day: date, metric_name: str, dim_col: str, value: str) -> dict:
    """Used by /api/ask: look up one specific segment value instead of ranking all of them."""
    for r in segment_ranking(client, day, metric_name, dim_col):
        if str(r["value"]) == str(value):
            return r
    return {"dimension": dim_col, "value": value, "metric": metric_name, "note": "no data for this segment/day/metric combination"}


# Multi-dimension (combo) drill-down: hierarchical second pass. Given the
# winning single-dimension segment (e.g. country=IN), checks every OTHER
# dimension for a combo (country=IN AND device_model=iPhone) that deviates
# even more sharply. hourly_segment_metrics already stores one row per hour
# x the FULL 10-dimension combination (see the ORDER BY in
# configs/clickhouse/01-schema.sql -- that's exactly what the corruption fix
# covers), so this is just _SEGMENT_QUERY with a WHERE on the outer
# dimension and GROUP BY on a second one -- no schema change needed.
_COMBO_SEGMENT_QUERY = """
    WITH daily AS (
        SELECT
            toDate(hour) AS day,
            {inner_dim} AS segment_value,
            countMerge(requests) AS requests,
            sumMerge(fills) AS fills,
            sumMerge(impressions) AS impressions,
            sumMerge(clicks) AS clicks,
            sumMerge(revenue) AS revenue
        FROM inmobi_rca.hourly_segment_metrics
        WHERE {outer_dim} = {{outer_value:String}} {extra_filter}
        GROUP BY day, segment_value
    )
    SELECT
        day,
        segment_value,
        requests,
        {metric_expr} AS actual_value,
        {baseline_cols}
    FROM daily
    ORDER BY segment_value, day
"""


def _build_combo_query(inner_dim: str, outer_dim: str, metric_expr: str, hour_cutoff=None) -> str:
    hour_filter = coverage_module.hour_filter_sql(hour_cutoff)
    return _COMBO_SEGMENT_QUERY.format(
        inner_dim=inner_dim,
        outer_dim=outer_dim,
        metric_expr=metric_expr,
        extra_filter=f"AND {hour_filter}" if hour_filter else "",
        baseline_cols=baseline_module.baseline_select(
            metric_expr, "segment_value, toDayOfWeek(day)", config.TRAILING_WEEKS
        ),
    )


def refine_segment(
    client,
    day: date,
    metric_name: str,
    outer_dim: str,
    outer_value,
    single_pct_dev: float,
    thresholds: dict,
    hour_cutoff=_UNSET,
) -> Optional[dict]:
    """Within the winning single-dimension segment, check every other
    dimension for an intersection that deviates even more sharply than the
    single dimension alone -- e.g. country=IN alone is +25%, but within it
    device_model=iPhone is +52%, a much tighter localization. Returns the
    single best combo only if it independently clears this metric's own
    (dynamic) significance threshold AND is strictly more extreme than the
    marginal single-dimension finding -- a floor against reporting a combo
    that's just noise on a smaller slice of the same segment. Returns None
    if no combo qualifies (the single dimension is already the best answer).

    Scope note: this catches a combo anomaly that also shows some signal in
    the marginal dimension -- the realistic case for most localized
    incidents. A combo invisible in every single dimension alone would need
    full pairwise scanning across all dimension pairs, not done here (see
    PROGRESS.md for the accepted scope/time tradeoff).
    """
    metric_thresholds = thresholds.get(
        metric_name, {"pct_threshold": config.PCT_DEVIATION_THRESHOLD, "volume_floor": config.MIN_VOLUME_FLOOR}
    )
    pct_threshold = metric_thresholds["pct_threshold"]
    volume_floor = metric_thresholds["volume_floor"]
    if hour_cutoff is _UNSET:
        hour_cutoff = coverage_module.hour_cutoff_for(coverage_module.day_coverage(client), day)

    best_combo = None
    for inner_dim in metrics.scannable_dimensions(metric_name):
        if inner_dim == outer_dim:
            continue
        query = _build_combo_query(
            inner_dim, outer_dim, metrics.METRIC_EXPRESSIONS[metric_name], hour_cutoff
        )
        rows = client.query(query, parameters={"outer_value": str(outer_value)}).result_rows
        for row_day, segment_value, requests, actual, baseline, _mean, _stddev, baseline_n in rows:
            if row_day != day or requests < volume_floor:
                continue
            if str(segment_value) == metrics.BLANK_SEGMENT_VALUE:
                continue
            if metrics.is_invalid_number(actual) or metrics.is_invalid_number(baseline) or baseline == 0:
                continue
            if (baseline_n or 0) < config.MIN_BASELINE_SAMPLES:
                continue
            pct_dev = (actual - baseline) / baseline
            if best_combo is None or abs(pct_dev) > abs(best_combo["pct_deviation"]):
                best_combo = {
                    "dimension": inner_dim,
                    "value": segment_value,
                    "requests": requests,
                    "actual": float(actual),
                    "baseline": float(baseline),
                    "pct_deviation": float(pct_dev),
                }

    if (
        best_combo is not None
        and abs(best_combo["pct_deviation"]) >= pct_threshold
        and abs(best_combo["pct_deviation"]) > abs(single_pct_dev)
    ):
        return best_combo
    return None


def investigate(metric_name: str, day: date, anomaly_candidate_id: Optional[str] = None) -> dict:
    client = db.get_ro_client()
    admin = db.get_admin_client()
    timings = timing.Timings()
    trace = tracing.start_trace(
        name="investigate",
        input={"metric": metric_name, "day": str(day), "anomaly_candidate_id": anomaly_candidate_id},
        metadata={"metric": metric_name, "day": str(day)},
    )

    def timed_span(name, func, input=None):
        """Every stage is both a Langfuse span and a timing bucket, under one
        name - so the trace a judge opens and the latency breakdown they see
        in the UI describe the same stages, not two different decompositions."""
        return trace.run_span(name, lambda: timings.measure(name, func), input=input)

    # Is the target day fully loaded? Resolved once here and threaded through
    # every query below, so a partial day is compared against the same hours
    # of its baselines rather than against their full 24 - see coverage.py.
    day_coverage = timed_span("day_coverage", lambda: coverage_module.day_coverage(client))
    hour_cutoff = coverage_module.hour_cutoff_for(day_coverage, day)
    coverage_note = coverage_module.describe(day_coverage, day)

    # One pass computing this metric's own empirical noise band (plus every
    # metric the decomposition/scan below might touch) - see thresholds.py.
    # Reused for factor significance, segment ranking's volume floor, the
    # ruled-out cutoff, confidence, and combo refinement below, so every
    # judgment call in this investigation uses the same, data-derived bar.
    metric_set = set(metrics.HEADLINE_METRICS) | set(_DECOMPOSITION_FACTORS) | {metric_name}
    computed_thresholds = timed_span(
        "compute_thresholds",
        lambda: thresholds_module.compute_metric_thresholds(client, metric_set),
        input={"metrics": sorted(metric_set)},
    )

    overall = timed_span(
        "overall_deviation",
        lambda: compute_daily_deviation(client, day, metric_name),
        input={"metric": metric_name, "day": str(day)},
    )

    ruled_out = []
    driving_factors = [dict(overall, metric=metric_name)] if overall else []

    if metric_name == "revenue":
        factor_breakdown = timed_span(
            "factor_decomposition",
            lambda: [compute_daily_deviation(client, day, f) for f in _DECOMPOSITION_FACTORS],
            input={"factors": list(_DECOMPOSITION_FACTORS), "day": str(day)},
        )
        significant = [
            f for f in factor_breakdown
            if f and f.get("pct_deviation") is not None
            and abs(f["pct_deviation"]) >= computed_thresholds[f["metric"]]["pct_threshold"]
        ]
        for f in factor_breakdown:
            if not f:
                continue
            if f in significant:
                continue
            if f.get("pct_deviation") is None:
                reason = f.get("not_evaluated_reason") or "insufficient baseline history to compare"
                ruled_out.append(f"{f['metric']}: not evaluated - {reason}")
            else:
                ruled_out.append(f"{f['metric']}: normal ({f['pct_deviation']:+.1%} vs baseline)")
        if significant:
            driving_factors = sorted(significant, key=lambda f: abs(f["pct_deviation"]), reverse=True)

    scan_metric = driving_factors[0]["metric"] if driving_factors else metric_name
    scan_thresholds = computed_thresholds.get(
        scan_metric, {"pct_threshold": config.PCT_DEVIATION_THRESHOLD, "volume_floor": config.MIN_VOLUME_FLOOR}
    )
    # Cuts that are undefined for the metric actually being sliced are stated
    # as not-applicable with the reason, never run and then reported as
    # "checked, came back normal" - claiming to have checked something that
    # cannot vary is a false claim of diligence. Keyed off scan_metric, not
    # the requested metric: a revenue investigation that decomposes down to
    # fill_rate slices by fill_rate, so it is fill_rate's degenerate cuts
    # that get skipped and therefore fill_rate's that must be explained.
    # See metrics.DEGENERATE_METRIC_DIMENSIONS.
    scan_dimensions = metrics.scannable_dimensions(scan_metric)
    ruled_out.extend(metrics.degenerate_notes(scan_metric))

    segment_candidates = timed_span(
        "segment_ranking",
        lambda: {
            dim: segment_ranking(
                client, day, scan_metric, dim,
                volume_floor=scan_thresholds["volume_floor"], hour_cutoff=hour_cutoff,
            )
            for dim in scan_dimensions
        },
        input={"metric": scan_metric, "day": str(day), "dimensions": scan_dimensions},
    )

    best = None
    for dim, ranked in segment_candidates.items():
        if not ranked:
            continue
        top = ranked[0]
        if best is None or abs(top["pct_deviation"]) > abs(best["pct_deviation"]):
            best = top

    for dim, ranked in segment_candidates.items():
        if not ranked:
            ruled_out.append(f"{dim}: no segment met the minimum volume floor on this day")
            continue
        top = ranked[0]
        if best is not None and top is best:
            continue
        if abs(top["pct_deviation"]) < scan_thresholds["pct_threshold"]:
            ruled_out.append(f"{dim}: no segment stands out (closest: {top['value']} at {top['pct_deviation']:+.1%})")

    # Multi-dimension drill-down: within the winning segment, check every
    # other dimension for a sharper intersection (see refine_segment above).
    if best is not None:
        combo = timed_span(
            "refine_segment",
            lambda: refine_segment(
                client, day, scan_metric, best["dimension"], best["value"], best["pct_deviation"],
                computed_thresholds, hour_cutoff=hour_cutoff,
            ),
            input={"metric": scan_metric, "day": str(day), "outer_dimension": best["dimension"], "outer_value": str(best["value"])},
        )
        if combo:
            best = dict(best, refined_by=combo)
            ruled_out.append(
                f"{best['dimension']}={best['value']}: further localized to {combo['dimension']}={combo['value']} "
                f"({combo['pct_deviation']:+.1%}, sharper than the segment alone)"
            )
        else:
            ruled_out.append(f"{best['dimension']}={best['value']}: no intersection with another dimension deviated more sharply than the segment alone")

    findings = {
        "metric": metric_name,
        "day": str(day),
        "overall": overall,
        "driving_factors": driving_factors,
        "responsible_segment": best,
        "checked_and_ruled_out": ruled_out,
    }
    # Only disclosed when the day really is partial, so the LLM has nothing
    # to editorialise about on a normal complete day.
    if coverage_note:
        findings["data_coverage_note"] = coverage_note

    diagnosis_text = timed_span("narrate", lambda: llm.narrate(findings), input=findings)

    # Confidence: how far past our detection threshold the responsible
    # segment's deviation sits, normalized so 2x the threshold maps to 1.0.
    # Deliberately NOT a fixed constant - a hardcoded number here would be
    # exactly the kind of unearned-precision figure the problem statement
    # warns against ("no hallucinated figures"), just committed by us
    # instead of the LLM. No segment found -> low flat confidence, since
    # the diagnosis is then just "nothing stood out," not a located cause.
    # Uses the combo's deviation when one was found - it's the more precise
    # (and by construction, more extreme) number.
    #
    # Now additionally discounted by how much history actually backs the
    # baseline. The same -45% deviation is much weaker evidence off 2 prior
    # same-weekdays than off 4, and reporting both at the same confidence
    # would be exactly the unearned precision this score exists to avoid.
    # Linear in baseline_n up to TRAILING_WEEKS, floored at 0.5 so a
    # thin-history finding is still reported, just visibly less certain.
    if best is not None:
        driver_pct_dev = best["refined_by"]["pct_deviation"] if best.get("refined_by") else best["pct_deviation"]
        magnitude = min(1.0, abs(driver_pct_dev) / (2 * scan_thresholds["pct_threshold"]))
        baseline_n = best.get("baseline_n") or config.TRAILING_WEEKS
        history_factor = max(0.5, min(1.0, baseline_n / config.TRAILING_WEEKS))
        confidence = round(magnitude * history_factor, 2)
    else:
        confidence = 0.3

    trace_id = trace.finish(output={"diagnosis_text": diagnosis_text, "responsible_segment": best, "confidence": confidence})

    cited_numbers = json.dumps(
        {
            "overall": overall,
            "driving_factors": driving_factors,
            "responsible_segment": best,
        },
        default=str,
    )

    # Map(String, String) already supports more than one pair - when a
    # combo was found, persist BOTH the outer segment and the refining
    # dimension, a genuine multi-dimension attribution, not just the
    # single-dimension finding.
    if best is not None:
        responsible_segment_map = {best["dimension"]: str(best["value"])}
        if best.get("refined_by"):
            responsible_segment_map[best["refined_by"]["dimension"]] = str(best["refined_by"]["value"])
    else:
        responsible_segment_map = {}

    admin.insert(
        "inmobi_rca.investigations",
        [
            [
                anomaly_candidate_id,
                metric_name,
                day,
                diagnosis_text,
                responsible_segment_map,
                ruled_out,
                cited_numbers,
                confidence,
                trace_id or "",
            ]
        ],
        column_names=[
            "anomaly_candidate_id",
            "metric",
            "day",
            "diagnosis_text",
            "responsible_segment",
            "checked_and_ruled_out",
            "cited_numbers",
            "confidence",
            "langfuse_trace_id",
        ],
    )

    if anomaly_candidate_id:
        admin.command(
            "ALTER TABLE inmobi_rca.anomaly_candidates UPDATE status = 'investigated' WHERE id = {id:String}",
            parameters={"id": anomaly_candidate_id},
        )

    timings_dict = timings.as_dict(
        clickhouse_stages=(
            "day_coverage", "compute_thresholds", "overall_deviation",
            "factor_decomposition", "segment_ranking", "refine_segment",
        ),
        llm_stages=("narrate",),
    )
    # Logged as its own row so it becomes one sample in the p95 distribution
    # /api/latency-stats reports - a single run's timing (above) says nothing
    # about whether this run was typical or lucky.
    timing.log(admin, "investigate", timings_dict)

    return {
        "metric": metric_name,
        "day": str(day),
        "diagnosis_text": diagnosis_text,
        "overall": overall,
        "driving_factors": driving_factors,
        "responsible_segment": best,
        "checked_and_ruled_out": ruled_out,
        "confidence": confidence,
        "data_coverage_note": coverage_note,
        # Per-stage wall clock, same stage names as the Langfuse spans. The
        # clickhouse_ms / llm_ms split is what makes "ClickHouse analyses,
        # the LLM only narrates" a measured claim instead of an assertion.
        "timings": timings_dict,
        "langfuse_trace_id": trace_id,
    }
