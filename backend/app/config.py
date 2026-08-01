import os
from urllib.parse import urlparse

CLICKHOUSE_HTTP_URL = os.environ.get("CLICKHOUSE_HTTP_URL", "http://clickhouse:8123")
_parsed = urlparse(CLICKHOUSE_HTTP_URL)
CLICKHOUSE_HOST = _parsed.hostname or "clickhouse"
CLICKHOUSE_PORT = _parsed.port or 8123
CLICKHOUSE_DATABASE = "inmobi_rca"

# Read-only user - every analytical SELECT in the pipeline goes through this.
CLICKHOUSE_READONLY_USER = os.environ["CLICKHOUSE_READONLY_USER"]
CLICKHOUSE_READONLY_PASSWORD = os.environ["CLICKHOUSE_READONLY_PASSWORD"]

# Admin user - used ONLY for the handful of deterministic, backend-authored
# INSERTs into anomaly_candidates/investigations/chat_queries. Never passed
# to, or reachable from, any LLM-facing code path.
CLICKHOUSE_ADMIN_USER = os.environ["CLICKHOUSE_ADMIN_USER"]
CLICKHOUSE_ADMIN_PASSWORD = os.environ["CLICKHOUSE_ADMIN_PASSWORD"]

# LLM provider - swap by changing this one var + restarting the container.
ACTIVE_LLM_PROVIDER = os.environ.get("ACTIVE_LLM_PROVIDER", "openai").lower()
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")

LANGFUSE_PUBLIC_KEY = os.environ.get("LANGFUSE_PUBLIC_KEY", "")
LANGFUSE_SECRET_KEY = os.environ.get("LANGFUSE_SECRET_KEY", "")
LANGFUSE_HOST = os.environ.get("LANGFUSE_HOST", "http://langfuse-web:3000")
LANGFUSE_WEB_PUBLIC_URL = os.environ.get("LANGFUSE_WEB_PUBLIC_URL", "http://localhost:3000")

# Detection tuning - kept as env-overridable constants, not buried in query
# code. NOT specified anywhere in the problem statement - InMobi's glossary
# only prescribes the baseline *method* (trailing same-weekday average), not
# a sensitivity. These are our own calibration, revised once (200/15%/OR ->
# 1000/30%/AND) after the first pass flagged ~26% of all scanned
# combinations as anomalous on the known 5-week batch - nowhere close to
# "avoid crying wolf on noise."
MIN_VOLUME_FLOOR = int(os.environ.get("MIN_VOLUME_FLOOR", "1000"))
PCT_DEVIATION_THRESHOLD = float(os.environ.get("PCT_DEVIATION_THRESHOLD", "0.30"))
Z_SCORE_THRESHOLD = float(os.environ.get("Z_SCORE_THRESHOLD", "2.5"))
TRAILING_WEEKS = int(os.environ.get("TRAILING_WEEKS", "4"))

# thresholds.py computes these live from whatever data is currently loaded
# (see compute_metric_thresholds) instead of trusting the flat constants
# above at scan/investigate time. The constants above become the fallback
# for a metric that doesn't yet have enough history to trust a percentile
# from - e.g. right after the Day-2 unseen-incident slice starts landing
# with only a few hours loaded, or a small streamed batch. Below
# MIN_THRESHOLD_SAMPLES observations, we don't have a reliable empirical
# noise distribution yet, so fall back rather than compute a percentile off
# a handful of points.
MIN_THRESHOLD_SAMPLES = int(os.environ.get("MIN_THRESHOLD_SAMPLES", "30"))
# Floor under the *dynamic* pct threshold: if a metric happens to be
# extremely flat in the current data (p95 near 0%), a near-zero cutoff
# would flag almost every tiny fluctuation. This isn't a return to a fixed
# threshold - it's a sanity floor under an otherwise-computed one.
MIN_PCT_DEVIATION_THRESHOLD = float(os.environ.get("MIN_PCT_DEVIATION_THRESHOLD", "0.05"))
# Floor under the *dynamic* volume floor, same reasoning. 200 is the exact
# bucket boundary scripts/validate_thresholds.sql Part 2 empirically showed
# has materially higher deviation variance below it on the known dataset.
MIN_VOLUME_FLOOR_ABSOLUTE = int(os.environ.get("MIN_VOLUME_FLOOR_ABSOLUTE", "200"))

# Minimum number of prior same-weekday observations required before a
# deviation is allowed to raise an alert. Not arbitrary: measured on the
# loaded 5-week batch, days 1-7 have ZERO prior same-weekday history (their
# baseline is NaN and they were already being silently dropped), and days
# 8-14 have exactly one - a single-sample baseline with zero variance, which
# makes the z-score condition undefined and collapses detection to the
# deviation threshold alone. Requiring 2 means a baseline is at least a
# comparison between two prior weeks rather than an echo of one. The days
# this excludes are now reported as "not evaluated" instead of being
# rendered the same as "evaluated and clean" - see scan()'s coverage block.
MIN_BASELINE_SAMPLES = int(os.environ.get("MIN_BASELINE_SAMPLES", "2"))
