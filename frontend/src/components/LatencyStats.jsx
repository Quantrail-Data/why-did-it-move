import { useEffect, useState, useCallback } from "react"
import { Gauge, RefreshCw } from "lucide-react"

import { Button } from "@/components/ui/button"
import { getLatencyStats } from "@/api/client"

function ms(v) {
  if (v == null) return "-"
  return v >= 1000 ? `${(v / 1000).toFixed(2)}s` : `${Math.round(v)}ms`
}

// p95, not a single run's timing - that lives on every diagnosis already
// (InvestigationDetail's LatencyBar) and answers "how long did THIS
// diagnosis take." This answers a different question: "how long does a
// diagnosis reliably take," across every /api/investigate call the system
// has actually logged (backend/app/timing.py -> request_latencies table).
// One lucky fast run proves nothing about the tail; p95 does.
export default function LatencyStats({ refreshKey }) {
  const [stats, setStats] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  const load = useCallback(() => {
    setLoading(true)
    getLatencyStats({ endpoint: "investigate" })
      .then(setStats)
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false))
  }, [])

  // Reloads whenever a new investigation finishes (parent bumps refreshKey),
  // so the sample count and p95 stay current without a manual click every
  // time - but the button stays for "I just want to check again."
  useEffect(() => {
    load()
  }, [load, refreshKey])

  if (error) return null // silent - this is a supplementary stat, not core functionality
  if (!loading && (!stats || stats.n === 0)) {
    return (
      <div className="mb-4 flex items-center gap-1.5 text-xs text-muted-foreground">
        <Gauge className="h-3.5 w-3.5" />
        No investigations logged yet - p95 latency appears after the first one runs.
      </div>
    )
  }

  return (
    <div className="mb-4 flex flex-wrap items-center gap-x-5 gap-y-1 rounded-md border bg-muted/30 px-3 py-2 text-xs">
      <span className="flex items-center gap-1.5 font-semibold uppercase text-muted-foreground">
        <Gauge className="h-3.5 w-3.5" />
        Investigate latency
      </span>
      {loading ? (
        <span className="text-muted-foreground">Loading…</span>
      ) : (
        <>
          <span>
            p50 <span className="font-medium tabular-nums">{ms(stats.p50_ms)}</span>
          </span>
          <span>
            {/* The headline number - the one that answers "is this fast." */}
            p95 <span className="font-semibold tabular-nums text-foreground">{ms(stats.p95_ms)}</span>
          </span>
          <span>
            p99 <span className="font-medium tabular-nums">{ms(stats.p99_ms)}</span>
          </span>
          <span className="text-muted-foreground">
            (ClickHouse p95 <span className="tabular-nums">{ms(stats.p95_clickhouse_ms)}</span> · LLM p95{" "}
            <span className="tabular-nums">{ms(stats.p95_llm_ms)}</span>)
          </span>
          {/* n is not decoration - a p95 off 3 samples is close to just the
              max, and shouldn't be read with the same confidence as one off
              300. Same honesty this project already applies to detection
              thresholds' n_samples/dynamic fields. */}
          <span className="text-muted-foreground">
            across <span className="font-medium">{stats.n}</span> run{stats.n === 1 ? "" : "s"}
          </span>
        </>
      )}
      <Button variant="ghost" size="icon" className="ml-auto h-6 w-6" onClick={load} title="Refresh">
        <RefreshCw className="h-3 w-3" />
      </Button>
    </div>
  )
}
