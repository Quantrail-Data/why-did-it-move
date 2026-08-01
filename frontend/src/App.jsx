import { useCallback, useEffect, useMemo, useState } from "react"

import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Select, SelectTrigger, SelectValue, SelectContent, SelectItem } from "@/components/ui/select"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import MetricTree from "@/components/MetricTree"
import AnomalyList from "@/components/AnomalyList"
import InvestigationDetail from "@/components/InvestigationDetail"
import MetricHistoryTimeline from "@/components/MetricHistoryTimeline"
import RevenueSignals from "@/components/RevenueSignals"
import AnomalyCountChart from "@/components/AnomalyCountChart"
import LatencyStats from "@/components/LatencyStats"
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
  const [scanCoverage, setScanCoverage] = useState(null)
  const [latencyRefreshKey, setLatencyRefreshKey] = useState(0)

  // Shared date-range state for the two "over time" panels (Anomaly history
  // + Anomaly counts) - one control drives both instead of each owning its
  // own from/to pair. timelineDays is the canonical full day list, reported
  // up by MetricHistoryTimeline once it fetches (every metric covers the
  // same date range, so whichever one it happens to have loaded is fine as
  // the source of truth) - Anomaly counts needs that same list to draw a
  // continuous timeline instead of only plotting days that have a candidate.
  const [timelineDays, setTimelineDays] = useState([])
  const [timelineFrom, setTimelineFrom] = useState("")
  const [timelineTo, setTimelineTo] = useState("")

  const handleTimelineDaysLoaded = useCallback((days) => {
    const dayStrings = days.map((d) => d.day)
    setTimelineDays(dayStrings)
    // Only default the range once - if the user already narrowed it,
    // switching the metric inside Anomaly History shouldn't reset their zoom.
    setTimelineFrom((prev) => prev || dayStrings[0] || "")
    setTimelineTo((prev) => prev || dayStrings[dayStrings.length - 1] || "")
  }, [])

  const timelineMin = timelineDays[0]
  const timelineMax = timelineDays[timelineDays.length - 1]

  function resetTimelineRange() {
    setTimelineFrom(timelineMin || "")
    setTimelineTo(timelineMax || "")
  }

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
      const res = await triggerScan({})
      setScanCoverage(res?.coverage || null)
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
      // Bumps LatencyStats to reload - this run just added a new sample to
      // the p95 distribution, so the stat shown should include it without
      // needing a manual refresh click.
      setLatencyRefreshKey((k) => k + 1)
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

      <LatencyStats refreshKey={latencyRefreshKey} />

      {/* What the last scan could and could not evaluate. Shown because a
          day the scan was unable to judge must not be presented the same way
          as a day it judged and found clean - see backend/app/coverage.py. */}
      {scanCoverage && (scanCoverage.partial_days?.length > 0 || scanCoverage.skipped_insufficient_history > 0) && (
        <div className="mb-4 rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-xs">
          <div className="font-semibold">Detection coverage</div>
          <ul className="mt-1 list-inside list-disc space-y-0.5">
            {scanCoverage.partial_days?.map((p) => (
              <li key={p.day}>{p.note || `${p.day} is only partially loaded.`}</li>
            ))}
            {scanCoverage.skipped_insufficient_history > 0 && (
              <li>
                {scanCoverage.skipped_insufficient_history.toLocaleString()} segment-days skipped: fewer than{" "}
                {scanCoverage.min_baseline_samples} prior same-weekday observations to compare against. Not evaluated,
                not cleared.
              </li>
            )}
          </ul>
        </div>
      )}

      <section className="mb-6">
        <h2 className="mb-2 text-sm font-semibold uppercase text-muted-foreground">Metric tree - {day}</h2>
        <MetricTree tree={tree} loading={treeLoading} />
      </section>

      {/* Grouped by what they answer, not by build order: the two "over
          time" views (Anomaly history's deviation-per-day, Anomaly counts'
          breadth-per-day) share one row and one date-range control below -
          zooming into a window moves both charts together instead of two
          independent pickers that can drift out of sync. */}
      <section className="mb-6">
        <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
          <h2 className="text-sm font-semibold uppercase text-muted-foreground">Anomaly timelines</h2>
          <div className="flex items-center gap-1">
            <Input
              type="date"
              value={timelineFrom}
              min={timelineMin}
              max={timelineTo || timelineMax}
              onChange={(e) => setTimelineFrom(e.target.value)}
              className="h-8 w-32 text-xs"
            />
            <span className="text-xs text-muted-foreground">to</span>
            <Input
              type="date"
              value={timelineTo}
              min={timelineFrom || timelineMin}
              max={timelineMax}
              onChange={(e) => setTimelineTo(e.target.value)}
              className="h-8 w-32 text-xs"
            />
            {(timelineFrom !== timelineMin || timelineTo !== timelineMax) && (
              <Button variant="ghost" size="sm" className="h-8 text-xs" onClick={resetTimelineRange}>
                Reset range
              </Button>
            )}
          </div>
        </div>
        <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
          <MetricHistoryTimeline
            onInvestigate={({ metric, day: d }) => runInvestigate({ metric, day: d })}
            investigating={investigating}
            fromDay={timelineFrom}
            toDay={timelineTo}
            onDaysLoaded={handleTimelineDaysLoaded}
          />
          <AnomalyCountChart
            onInvestigate={runInvestigate}
            allDays={timelineDays}
            fromDay={timelineFrom}
            toDay={timelineTo}
          />
        </div>
      </section>

      {/* The two "current state" views: what the scan flagged on its own,
          and what shape of revenue problem is present right now
          (drift/collapse/mix-shift) - separate from the threshold scan on
          purpose, since merging either into it would make the flag count
          meaningless. See backend/app/revenue_signals.py. */}
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

        <RevenueSignals day={day} onInvestigate={runInvestigate} />
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
