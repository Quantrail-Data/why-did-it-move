"""Hour-by-hour playback data for one metric/day, optionally overlaid with
one specific segment - powers the frontend's "replay the incident" view
(the problem statement's own suggested demo language: "Replay an incident
end to end"). Reuses hourly_segment_metrics directly; no new tables, no new
ingestion - the rollup already has hour grain.
"""
from datetime import date
from typing import Optional

from . import config, db, metrics

_TIMELINE_QUERY = """
    WITH hourly AS (
        SELECT
            hour,
            toHour(hour) AS hod,
            toDate(hour) AS day,
            countMerge(requests) AS requests,
            sumMerge(fills) AS fills,
            sumMerge(impressions) AS impressions,
            sumMerge(clicks) AS clicks,
            sumMerge(revenue) AS revenue
        FROM inmobi_rca.hourly_segment_metrics
        {where_clause}
        GROUP BY hour, hod, day
    )
    SELECT
        hour,
        hod,
        {metric_expr} AS actual_value,
        avg({metric_expr}) OVER (
            PARTITION BY hod, toDayOfWeek(day) ORDER BY day
            ROWS BETWEEN {trailing} PRECEDING AND 1 PRECEDING
        ) AS baseline_avg
    FROM hourly
    ORDER BY hour
"""


def _hourly_series(
    client,
    metric_name: str,
    day: date,
    dim_col: Optional[str],
    value: Optional[str],
    dim_col2: Optional[str] = None,
    value2: Optional[str] = None,
) -> list:
    conditions = []
    params = {}
    if dim_col and value is not None:
        conditions.append(f"{dim_col} = {{value:String}}")
        params["value"] = str(value)
    if dim_col2 and value2 is not None:
        conditions.append(f"{dim_col2} = {{value2:String}}")
        params["value2"] = str(value2)
    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    query = _TIMELINE_QUERY.format(
        where_clause=where_clause,
        metric_expr=metrics.METRIC_EXPRESSIONS[metric_name],
        trailing=config.TRAILING_WEEKS,
    )
    rows = client.query(query, parameters=params).result_rows

    points = []
    day_str = day.isoformat()
    for hour, hod, actual, baseline in rows:
        if hour.date().isoformat() != day_str:
            continue
        actual_ok = not metrics.is_invalid_number(actual)
        baseline_ok = not metrics.is_invalid_number(baseline) and baseline != 0
        pct_dev = (actual - baseline) / baseline if actual_ok and baseline_ok else None
        points.append(
            {
                "hour": hour.isoformat(),
                "hod": hod,
                "actual": float(actual) if actual_ok else None,
                "baseline": float(baseline) if baseline_ok else None,
                "pct_deviation": float(pct_dev) if pct_dev is not None else None,
            }
        )
    points.sort(key=lambda p: p["hod"])
    return points


def get_timeline(
    client,
    metric_name: str,
    day: date,
    dim_col: Optional[str] = None,
    value: Optional[str] = None,
    dim_col2: Optional[str] = None,
    value2: Optional[str] = None,
) -> dict:
    overall = _hourly_series(client, metric_name, day, None, None)
    segment = (
        _hourly_series(client, metric_name, day, dim_col, value, dim_col2, value2) if dim_col and value else None
    )

    driver = segment if segment else overall
    anomaly_hour = None
    for point in driver:
        if point["pct_deviation"] is not None and abs(point["pct_deviation"]) >= config.PCT_DEVIATION_THRESHOLD:
            anomaly_hour = point["hour"]
            break

    if dim_col and value and dim_col2 and value2:
        segment_label = f"{dim_col} = {value}, {dim_col2} = {value2}"
    elif dim_col and value:
        segment_label = f"{dim_col} = {value}"
    else:
        segment_label = None

    return {
        "metric": metric_name,
        "day": day.isoformat(),
        "overall": overall,
        "segment": segment,
        "segment_label": segment_label,
        "anomaly_hour": anomaly_hour,
    }
