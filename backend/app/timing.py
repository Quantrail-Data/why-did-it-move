"""Per-stage wall-clock timing, surfaced in the API response and rendered in
the UI.

Two reasons this is a first-class output rather than a debug log:

1. The problem statement grades "Fast - diagnosis in seconds, not minutes."
   Claiming that in a pitch is worth nothing; showing the measured number
   next to the diagnosis, on the judges' own unseen data, is evidence.

2. It makes the central architectural claim of this project falsifiable.
   "ClickHouse does the analysis, the LLM only narrates" is either true or
   it isn't, and the split between clickhouse_ms and llm_ms is exactly the
   measurement that settles it. If the LLM were quietly doing analytical
   work, its share of the time would show it.

Deliberately wall-clock and per-stage, not a profiler: the number a judge
cares about is how long they waited, decomposed into the stages the
architecture claims exist. The same stage names are already the Langfuse
span names, so a trace and a timings block line up one-to-one.
"""
import time
from contextlib import contextmanager

# p50/p95/p99 answer a different question than any single run's timing does:
# not "how long did this diagnosis take" but "how long does this diagnosis
# reliably take, across everything the system has actually done." A single
# fast run proves nothing about the tail; p95 does. Computed in ClickHouse
# via quantile(), not in Python - same division of labor as every other
# number in this pipeline.
_LATENCY_STATS_QUERY = """
    SELECT
        count() AS n,
        quantile(0.50)(total_ms) AS p50_ms,
        quantile(0.95)(total_ms) AS p95_ms,
        quantile(0.99)(total_ms) AS p99_ms,
        max(total_ms) AS max_ms,
        quantile(0.95)(clickhouse_ms) AS p95_clickhouse_ms,
        quantile(0.95)(llm_ms) AS p95_llm_ms
    FROM inmobi_rca.request_latencies
    WHERE endpoint = {endpoint:String}
"""


def log(admin_client, endpoint: str, timings_dict: dict) -> None:
    """Persist one call's timing so it becomes a sample in the p95
    distribution. Best-effort: a logging failure must never break the
    investigation it's trying to measure, so it's swallowed, not raised."""
    try:
        admin_client.insert(
            "inmobi_rca.request_latencies",
            [[endpoint, timings_dict["total_ms"], timings_dict["clickhouse_ms"], timings_dict["llm_ms"]]],
            column_names=["endpoint", "total_ms", "clickhouse_ms", "llm_ms"],
        )
    except Exception:
        pass


def stats(ro_client, endpoint: str) -> dict:
    """p50/p95/p99 for one endpoint, plus n so a caller can tell a
    percentile backed by 3 samples from one backed by 300 - the same
    n-aware honesty this project already applies to detection thresholds
    (thresholds.py's `dynamic`/`n_samples` fields)."""
    row = ro_client.query(_LATENCY_STATS_QUERY, parameters={"endpoint": endpoint}).result_rows[0]
    n, p50, p95, p99, max_ms, p95_ch, p95_llm = row
    if not n:
        return {"endpoint": endpoint, "n": 0}
    return {
        "endpoint": endpoint,
        "n": int(n),
        "p50_ms": round(float(p50), 1),
        "p95_ms": round(float(p95), 1),
        "p99_ms": round(float(p99), 1),
        "max_ms": round(float(max_ms), 1),
        "p95_clickhouse_ms": round(float(p95_ch), 1),
        "p95_llm_ms": round(float(p95_llm), 1),
    }


class Timings:
    """Accumulates {stage: total_ms} across an investigation.

    Stages repeat (segment_ranking runs once per dimension), so durations
    accumulate per stage name rather than overwriting - the reported
    clickhouse_ms is the real total time spent in ClickHouse, not the last
    query's time.
    """

    def __init__(self):
        self._stages: dict = {}
        self._counts: dict = {}
        self._start = time.perf_counter()

    @contextmanager
    def stage(self, name: str):
        started = time.perf_counter()
        try:
            yield
        finally:
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            self._stages[name] = self._stages.get(name, 0.0) + elapsed_ms
            self._counts[name] = self._counts.get(name, 0) + 1

    def measure(self, name: str, func):
        """Run func() inside a stage and return its result."""
        with self.stage(name):
            return func()

    def as_dict(self, clickhouse_stages=(), llm_stages=()) -> dict:
        """Report per-stage totals plus the two rollups that matter.

        total_ms is measured end to end rather than summed from the stages,
        so any time spent outside a named stage is included instead of
        quietly vanishing - the reported total always matches what the user
        actually waited.
        """
        total_ms = (time.perf_counter() - self._start) * 1000.0
        stages = {
            name: {"ms": round(ms, 1), "calls": self._counts[name]}
            for name, ms in self._stages.items()
        }
        clickhouse_ms = sum(self._stages.get(s, 0.0) for s in clickhouse_stages)
        llm_ms = sum(self._stages.get(s, 0.0) for s in llm_stages)
        return {
            "total_ms": round(total_ms, 1),
            "clickhouse_ms": round(clickhouse_ms, 1),
            "llm_ms": round(llm_ms, 1),
            # Whatever is left is this process's own Python work plus the
            # result-table INSERT. Named explicitly so the three numbers add
            # up to the total and nothing is hidden.
            "other_ms": round(max(0.0, total_ms - clickhouse_ms - llm_ms), 1),
            "stages": stages,
        }
