"""Single source of truth for the trailing-same-weekday baseline window.

Six separate queries (detect.py, investigate.py x3, thresholds.py,
timeline.py) all compute "compare this period against the same weekday in
the trailing N weeks." They used to each carry their own hand-copied copy of
the window clause. That is exactly the shape of the bug that corrupted the
rollup earlier in this build (one place said one thing, another said
something slightly different, nothing errored) - so the clause lives here
once and every caller formats it from these helpers.

Two substantive changes are encoded here, both found by auditing the real
dataset rather than by reading code:

1. ROBUST (median) BASELINE, not a mean. A real incident sits inside the
   trailing window of the next N same-weekdays and drags their baseline
   toward itself, manufacturing a phantom anomaly in the incident's wake.
   Measured on the loaded data: 2026-06-21 is a genuine -44.8% revenue
   incident. It then poisons the following two Sundays -
   2026-06-28 reads +22.7% against a mean baseline but only +5.5% against a
   median one, and 2026-07-05 reads +19.6% vs +7.5%. The +32.5% "finance
   category anomaly" on 2026-06-28 is entirely this artifact and disappears
   under a median baseline. quantileExact(0.5) is a true order statistic
   (no interpolation, no sampling), so a single contaminated sample in a
   4-sample window cannot move it.

2. HOUR-RESTRICTED comparison for partial days. See coverage.py - a day that
   is only partly loaded must be compared against the same hours of the
   trailing weeks, never against their full 24 hours.

The mean is still computed and returned alongside the median, so the gap
between them is inspectable evidence rather than a hidden modelling choice.
"""

# ROWS (not RANGE) BETWEEN N PRECEDING AND 1 PRECEDING, partitioned by the
# caller's segment key plus day-of-week: for each day, the trailing
# same-weekday occurrences of that same segment. ROWS is deliberate - it
# counts prior *observations* of this segment, so a segment that doesn't
# appear every day still gets the N most recent comparable days it actually
# has, rather than an empty window.
_WINDOW = "PARTITION BY {partition} ORDER BY day ROWS BETWEEN {trailing} PRECEDING AND 1 PRECEDING"


def window_clause(partition: str, trailing: int) -> str:
    return _WINDOW.format(partition=partition, trailing=trailing)


def baseline_select(metric_expr: str, partition: str, trailing: int) -> str:
    """The four baseline columns every caller needs, as a SELECT fragment.

    baseline_avg     - robust (median) baseline; this is what deviation and
                       the anomaly decision are computed from.
    baseline_mean    - the arithmetic mean, kept purely so a reader can see
                       when the two disagree (i.e. when the trailing window
                       contains an outlier). Never used for a decision.
    baseline_stddev  - population stddev, for the z-score condition.
    baseline_n       - how many prior same-weekday observations actually
                       backed this baseline. A deviation measured against 1
                       prior sample is much weaker evidence than the same
                       deviation against 4, and callers surface this rather
                       than silently treating them as equivalent.
    """
    w = window_clause(partition, trailing)
    return f"""
        quantileExact(0.5)({metric_expr}) OVER ({w}) AS baseline_avg,
        avg({metric_expr}) OVER ({w}) AS baseline_mean,
        stddevPop({metric_expr}) OVER ({w}) AS baseline_stddev,
        count({metric_expr}) OVER ({w}) AS baseline_n
    """.strip()
