import { useCallback, useEffect, useMemo, useState } from "react"

import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Select, SelectTrigger, SelectValue, SelectContent, SelectItem } from "@/components/ui/select"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import MetricTree from "@/components/MetricTree"
import AnomalyList from "@/components/AnomalyList"
import InvestigationDetail from "@/components/InvestigationDetail"
import MetricHistoryTimeline from "@/components/MetricHistoryTimeline"
import { listAnomalyCandidates, triggerScan, investigate, getMetricTree } from "@/api/client"

const METRIC_FILTERS = [
  { value: "all", label: "All metrics" },
  { value: "revenue", label: "Revenue" },
  { value: "fill_rate", label: "Fill rate" },
  { value: "render_rate", label: "Render rate" },
  { value: "ecpm", label: "eCPM" },
  { value: "ctr", label: "CTR" },
]

function todayIso() {
  return new Date().toISOString().slice(0, 10)
}

export default function App() {
  const [day, setDay] = useState(() => localStorage.getItem("wdim_day") || "2026-07-05")
  const [tree, setTree] = useState([])
  const [treeLoading, setTreeLoading] = useState(false)
  const [anomalies, setAnomalies] = useState([])
  const [anomaliesLoading, setAnomaliesLoading] = useState(false)
  const [scanning, setScanning] = useState(false)
  const [investigating, setInvestigating] = useState(false)
  const [investigatingId, setInvestigatingId] = useState(null)
  const [result, setResult] = useState(null)
  const [error, setError] = useState(null)
  const [searchText, setSearchText] = useState("")
  const [metricFilter, setMetricFilter] = useState("all")

  const filteredAnomalies = useMemo(() => {
    const q = searchText.trim().toLowerCase()
    return anomalies.filter((a) => {
      if (metricFilter !== "all" && a.metric !== metricFilter) return false
      if (!q) return true
      const segmentText = Object.entries(a.segment_dims || {})
        .map(([k, v]) => `${k} ${v}`)
        .join(" ")
        .toLowerCase()
      return a.metric.toLowerCase().includes(q) || segmentText.includes(q)
    })
  }, [anomalies, searchText, metricFilter])

  const loadTree = useCallback(async (d) => {
    setTreeLoading(true)
    try {
      setTree(await getMetricTree({ day: d }))
      setError(null)
    } catch (e) {
      setError(e.message)
    } finally {
      setTreeLoading(false)
    }
  }, [])

  const loadAnomalies = useCallback(async (d) => {
    setAnomaliesLoading(true)
    try {
      setAnomalies(await listAnomalyCandidates({ day: d }))
      setError(null)
    } catch (e) {
      setError(e.message)
    } finally {
      setAnomaliesLoading(false)
    }
  }, [])

  useEffect(() => {
    localStorage.setItem("wdim_day", day)
    // Sequenced, not concurrent - some proxies/corporate networks choke on
    // simultaneous same-origin requests fired back-to-back; these are both
    // cheap enough that sequencing costs nothing and removes the risk.
    ;(async () => {
      await loadTree(day)
      await loadAnomalies(day)
    })()
  }, [day, loadTree, loadAnomalies])

  async function handleScan() {
    setScanning(true)
    setError(null)
    try {
      await triggerScan({})
      await loadAnomalies(day)
      await loadTree(day)
    } catch (e) {
      setError(e.message)
    } finally {
      setScanning(false)
    }
  }

  async function runInvestigate({ metric, day: investigateDay, anomalyCandidateId }) {
    setError(null)
    setResult(null)
    if (anomalyCandidateId) setInvestigatingId(anomalyCandidateId)
    else setInvestigating(true)
    try {
      const res = await investigate({ metric, day: investigateDay, anomalyCandidateId })
      setResult(res)
    } catch (e) {
      setError(e.message)
    } finally {
      setInvestigatingId(null)
      setInvestigating(false)
    }
  }

  return (
    <div className="mx-auto max-w-6xl px-4 py-8">
      <header className="mb-6 flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <img src="/favicon.svg" alt="" className="h-9 w-9 rounded-lg" />
          <div>
            <h1 className="text-xl font-semibold">Why Did It Move</h1>
            <p className="text-sm text-muted-foreground">Automated root-cause analysis on ClickHouse - InMobi ad-metrics</p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <Input
            type="date"
            value={day}
            onChange={(e) => setDay(e.target.value)}
            className="h-8 w-32 text-xs"
          />
          <Button variant="outline" size="sm" onClick={() => setDay(todayIso())}>
            Today
          </Button>
          <Button size="sm" onClick={handleScan} disabled={scanning}>
            {scanning ? "Scanning…" : "Re-scan"}
          </Button>
        </div>
      </header>

      {error && (
        <div className="mb-4 rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive">
          {error}
        </div>
      )}

      <section className="mb-6">
        <h2 className="mb-2 text-sm font-semibold uppercase text-muted-foreground">Metric tree - {day}</h2>
        <MetricTree tree={tree} loading={treeLoading} />
      </section>

      {/* Detection-side views, side by side: what the scan flagged on its
          own, and the full history to click into anything it didn't. Both
          are Cards now, same bordered-container treatment on both sides. */}
      <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle>
              Flagged anomalies {anomalies.length > 0 && `(${filteredAnomalies.length}/${anomalies.length})`}
            </CardTitle>
          </CardHeader>
          <CardContent>
            <div className="mb-2 flex gap-2">
              <Input
                value={searchText}
                onChange={(e) => setSearchText(e.target.value)}
                placeholder="Search segment, e.g. gaming or country…"
                className="h-8 flex-1 text-xs"
              />
              <Select value={metricFilter} onValueChange={setMetricFilter}>
                <SelectTrigger className="h-8 w-32 shrink-0 text-xs">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {METRIC_FILTERS.map((m) => (
                    <SelectItem key={m.value} value={m.value}>
                      {m.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="scroll-thin max-h-[28rem] overflow-y-auto pr-1">
              <AnomalyList
                anomalies={filteredAnomalies}
                loading={anomaliesLoading}
                investigatingId={investigatingId}
                onInvestigate={(a) => runInvestigate({ metric: a.metric, day: a.day, anomalyCandidateId: a.id })}
                emptyMessage={
                  anomalies.length > 0
                    ? "No anomalies match this search/filter."
                    : "No flagged anomalies for this day. Try Re-scan, or click a day on the right to investigate it manually."
                }
              />
            </div>
          </CardContent>
        </Card>

        <MetricHistoryTimeline
          onInvestigate={({ metric, day: d }) => runInvestigate({ metric, day: d })}
          investigating={investigating}
        />
      </div>

      {/* Full-width below, not squeezed into a half-width column - once a
          diagnosis exists it gets room for the comparison chart/table
          alongside the prose instead of everything wrapping awkwardly. */}
      <section className="mt-6">
        <h2 className="mb-2 text-sm font-semibold uppercase text-muted-foreground">Investigation</h2>
        <InvestigationDetail result={result} loading={investigating || investigatingId != null} />
      </section>
    </div>
  )
}
