# Why Did It Move

**Automated root-cause analysis for ad-metrics, on ClickHouse.**
Built by CH-Minds for Click-a-thon 2026 - InMobi problem statement, *"From alert to answer: the automated root-cause analyst."*

A metric moves → the system detects it on its own, drills down through ClickHouse to find the exact segment responsible, and explains it in plain English - every cited number is real, reproducible, and traced end to end.

## The problem

InMobi's ad business runs at global scale - every app open, scroll, and ad slot is an **ad request**, and each one flows through a funnel: `Request → Fill → Impression → Click`, with revenue earned on impressions. Across thousands of app/device/geo/advertiser combinations, a metric like revenue or fill rate can move for any number of reasons - traffic changed, an ad format broke, a specific device/OS combo stopped rendering, one advertiser's campaign paused. Today, answering *"why did it move?"* means a human manually slicing dashboards by dimension after dimension. That doesn't scale, and it's slow even when it works.

The ask: given a stream of ad-event data, **detect** when a headline metric deviates from its normal baseline, **automatically drill down** to the exact responsible segment, and **produce a plain-language diagnosis** where every claim is backed by a real, reproducible number - stating what was checked and ruled out, not just what was found. A private, unseen slice of new data (with new planted anomalies) drops in the final hours; the system has to work on data it has never seen, not just the known training batch.

Full spec: the original package's `PROBLEM_STATEMENT.md` and `metrics_glossary.md` (not included in this public repo - see `INMOBI_CONTEXT.md` for the condensed, load-bearing facts).

## Our solution

Three stages, and a hard architectural rule that shapes all of them: **ClickHouse does every bit of the analysis; the LLM only narrates the finished result.** The LLM never queries ClickHouse, never sees a raw event row, and never invents a number - it receives a pre-computed JSON object and turns it into 2–4 sentences of plain English.

1. **Detect** - a background scan sweeps every headline metric (revenue, fill rate, render rate, eCPM, CTR) × every dimension (ad format, app category, publisher tier, advertiser vertical/campaign type, region, country, device model, OS version), comparing each day against a **trailing same-weekday baseline** - never a flat average, which would falsely flag every weekend given this data's real weekly seasonality. Deviations are flagged into `anomaly_candidates`.
2. **Investigate** - for a flagged (or manually chosen) metric/day: decompose the metric into its revenue-identity factors (`Requests × Fill rate × Render rate × eCPM`), rank every dimension's segments by deviation to find the responsible one, then drill **one level deeper** within that segment across every other dimension to check for a sharper intersection (e.g. `country=IN` alone vs. `country=IN AND device_model=iPhone` together) - and explicitly record every factor and segment that was checked and came back normal.
3. **Narrate + trace** - the structured findings go to the LLM exactly once, purely to phrase them in plain language. Every query run, in order, plus the narration call, is captured as a Langfuse trace with real input/output at every step - a judge (or you) can open the trace and see precisely what the system checked and why, independent of the prose.

### Key differentiators

- **Nothing about detection is a hardcoded guess.** Both the baseline (trailing same-weekday average) and the anomaly threshold are computed live from whatever data is currently loaded
- each metric earns its own sensitivity from its own empirical noise distribution (`backend/app/thresholds.py`), instead of one fixed percentage applied to every metric. This keeps working correctly if the unseen-incident data has a different scale or volatility than the known batch, with a safe static fallback for the cold-start case (not enough history yet to trust a percentile).
- **Multi-dimension drill-down.** Most systems (and an earlier version of this one) only ever check one dimension at a time. This one checks intersections too, so a planted anomaly localized to *"iPhones in India"* specifically - not `country=IN` alone, not `device_model=iPhone` alone - still gets found and named precisely.
- **Ruled-out is a first-class output**, not an afterthought - every factor and dimension that came back normal is recorded and shown, directly answering the bonus criterion.
- **Ingestion doesn't assume one delivery mechanism.** The known batch arrived as files; the system also accepts incremental pushes (`POST /api/ingest/events`) so however the unseen
  data actually shows up, it lands in the same table and flows through the same pipeline with
  no code change.
- **Every number in the UI is one click from its raw JSON evidence** - the "Raw evidence" panel on every diagnosis is the literal object handed to the LLM, and the downloadable PDF report includes it verbatim as an appendix, so a reader can check every sentence against a specific field instead of trusting the prose.

## Architecture

```
ad_events (raw fact table, ~9M rows/5 weeks)
        │  MATERIALIZED VIEW (joins in app/advertiser/geo_device dimensions)
        ▼
hourly_segment_metrics (AggregatingMergeTree rollup - everything below reads only this)
        │
   ┌────┴─────────────────────────────────┐
   ▼                                       ▼
detect.py (background scan)      investigate.py (on-demand drill-down)
   │  flags → anomaly_candidates      │  → factor decomposition, segment ranking,
   │                                  │    combo drill-down (refine_segment)
   └──────────────┬───────────────────┘
                   ▼
            llm.py (narrate the finished JSON - never queries ClickHouse itself)
                   │
                   ▼
      investigations (ClickHouse) + full trace (Langfuse)
                   │
                   ▼
              React dashboard: metric tree, anomaly list, diagnosis,
              hour-by-hour replay, PDF report, chat follow-up
```

- **`ad_events`** - raw fact table, one row per ad request.
- **`hourly_segment_metrics`** - the serving-layer rollup every query actually reads. `AggregatingMergeTree` + `*State`/`*Merge` because fill rate/eCPM/CTR/render rate are **sum/sum ratios** (never averaged per-row - that breaks rollups), and its `ORDER BY` deliberately covers all 10 grouping columns (a real ClickHouse gotcha: for `AggregatingMergeTree`, `ORDER BY` is a row's *merge identity* - leaving a column out silently corrupts it during background merges; see `INMOBI_CONTEXT.md`'s "Incident" section for the full story of finding and fixing this).
- **`anomaly_candidates` / `investigations` / `chat_queries`** - the pipeline's own output, written via a least-privilege `ch_admin` path never reachable from LLM-facing code.
- Full schema reasoning lives as comments directly above each `CREATE TABLE` in [`configs/clickhouse/01-schema.sql`](configs/clickhouse/01-schema.sql).

## Stack

| Component | Role |
|---|---|
| **ClickHouse** | Primary datastore *and* the engine doing the actual analysis - every decomposition, ranking, and combo drill-down is a ClickHouse query, never Python/LLM logic. |
| **FastAPI** (`backend/`) | Orchestrates detect → investigate → narrate; the only code path with write access to ClickHouse. |
| **Langfuse** | Full tracing of every investigation - what was checked, in what order, with real input/output at every step. The one optional OSS component this build integrates, chosen because it's the direct mechanism for the "no trace, no credit" unseen-incident requirement. |
| **Vite + React + Tailwind + shadcn/ui** (`frontend/`) | Dashboard: metric tree (green/amber/red), flagged-anomaly list, diagnosis view, hour-by-hour replay, PDF report, chat follow-up. |

## Repository layout

```
docker-compose.yaml                   All infra (ClickHouse always on; Langfuse optional profile)
.env.example                          Copy to .env before running
configs/clickhouse/01-schema.sql      Schema + reasoning comments (read this to understand the data model)
configs/clickhouse/init-db.sh         Creates the read-only ClickHouse user on first boot
backend/app/
  detect.py                           Background scan (Detect)
  investigate.py                      Drill-down + factor decomposition + combo refinement (Investigate)
  thresholds.py                       Dynamic, data-derived detection thresholds
  llm.py                              Provider-agnostic narration (OpenAI/Anthropic/Gemini)
  tracing.py                          Langfuse wrapper
  ingest.py                           Incremental event ingest (POST /api/ingest/events)
  timeline.py                         Hour-by-hour replay data
  ask.py                              Chat follow-up endpoint
frontend/src/                         Dashboard (see components/ for metric tree, diagnosis, chat, playback)
scripts/load_data.sh                  One-time (and re-runnable) bulk data load
scripts/validate_thresholds.sql       Independent empirical check of the detection thresholds
PROGRESS.md / INMOBI_CONTEXT.md       Full build history, known risks, and problem-statement reference
```

## Running it

```bash
cp .env.example .env
```

Edit `.env` and set at least one LLM key (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, or `GEMINI_API_KEY`) plus a matching `ACTIVE_LLM_PROVIDER`. Everything else in `.env.example` already has working defaults for local development.

```bash
docker compose up -d
```

This brings up ClickHouse (always on) and, per `COMPOSE_PROFILES` in `.env`, Langfuse - plus the backend and frontend. First boot takes a minute while ClickHouse initializes and Langfuse runs its own migrations; `docker compose ps` should show every container `healthy`.

### Load the data

The InMobi ad-events dataset (`ad_events.parquet`, `apps.csv`, `advertisers.csv`,
`geo_device.csv`) isn't included in this repo - `ad_events.parquet` alone is over GitHub's
100MB limit. Place the four files in `data/inmobi/`, then:

```bash
./scripts/load_data.sh
```

Dimension tables load first, `ad_events` second (the rollup's materialized view needs the dimension lookups populated before it can resolve joins correctly on the incoming events). The script prints row counts for all 5 tables when it's done - compare against what the data package's own README says it contains as a sanity check.

### Use it

| | |
|---|---|
| Dashboard | http://localhost:5173 |
| Backend API | http://localhost:8001 (interactive docs at `/docs`) |
| Langfuse UI | http://localhost:3000 |

From the dashboard: pick a day, hit **Re-scan** to run detection, click any flagged anomaly (or use **Investigate manually** for any metric/day) to get a diagnosis, **Replay this incident** for the hour-by-hour view, the download icon for a PDF report, and **Ask a follow-up** to query the same data conversationally.

### Verifying the results are real, not hallucinated

- Every diagnosis has a **"Raw evidence (JSON)"** panel - the exact object handed to the LLM, so any sentence in the diagnosis can be checked against a specific field.
- Every diagnosis links to its **full Langfuse trace** - every ClickHouse query the pipeline ran, in order, with real input/output.
- `scripts/validate_thresholds.sql` independently re-derives the detection thresholds straight from the data, as a cross-check against what `thresholds.py` computes live.
- The downloadable PDF report bundles the diagnosis, every number behind it, the raw JSON appendix, and the trace link into one artifact - this is the actual submission artifact for the unseen-incident requirement.

### The unseen incident / new data

`scripts/load_data.sh` is safe to re-run against a new slice of files (dimension tables upsert, `ad_events` just gets new date partitions); `POST /api/ingest/events` accepts incrementally-pushed data if it doesn't arrive as files at all. Either way it lands in the same `ad_events` table and needs zero schema/code changes downstream. See `INMOBI_CONTEXT.md`'s "Ingesting and verifying new data" section for the exact steps,including the raw-vs-rollup spot check that already caught one real corruption bug during this build.

## Further reading

- [`INMOBI_CONTEXT.md`](INMOBI_CONTEXT.md) - condensed problem-statement reference, the rollup-corruption incident writeup, and the exact query inventory showing where ClickHouse does the real work.
- [`PROGRESS.md`](PROGRESS.md) - full build log: what's implemented, what's been independently verified against live data (not just read), and the ranked list of known risks.

## License

MIT - see [LICENSE](LICENSE).
