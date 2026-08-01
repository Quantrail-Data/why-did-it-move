import { useEffect, useMemo, useRef, useState } from "react"
import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ReferenceDot,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts"

import { Button } from "@/components/ui/button"
import { getTimeline } from "@/api/client"

function formatHour(hod) {
  return `${String(hod).padStart(2, "0")}:00`
}

// ClickHouse returns hours as ISO strings in server-local (UTC) time - parse
// the hour-of-day directly from the string instead of via `new Date(...)`,
// which would silently shift it into the browser's local timezone.
function hodFromIso(iso) {
  return Number(iso.slice(11, 13))
}

export default function PlaybackTimeline({ metric, day, segment }) {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)
  const [hourIndex, setHourIndex] = useState(0)
  const [playing, setPlaying] = useState(false)
  const timerRef = useRef(null)

  useEffect(() => {
    setLoading(true)
    setError(null)
    setPlaying(false)
    getTimeline({
      metric,
      day,
      dimension: segment?.dimension,
      value: segment?.value,
      dimension2: segment?.refined_by?.dimension,
      value2: segment?.refined_by?.value,
    })
      .then((res) => {
        setData(res)
        setHourIndex(0)
      })
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false))
  }, [metric, day, segment])

  useEffect(() => {
    if (!playing || !data) return
    timerRef.current = setInterval(() => {
      setHourIndex((i) => {
        if (i >= 23) {
          setPlaying(false)
          return i
        }
        return i + 1
      })
    }, 600)
    return () => clearInterval(timerRef.current)
  }, [playing, data])

  const chartData = useMemo(() => {
    if (!data) return []
    return data.overall.map((pt, i) => ({
      hod: pt.hod,
      label: formatHour(pt.hod),
      overall: pt.actual,
      segment: data.segment ? data.segment[i]?.actual : undefined,
      segmentBaseline: data.segment ? data.segment[i]?.baseline : undefined,
    }))
  }, [data])

  if (loading) return <p className="text-sm text-muted-foreground">Loading playback…</p>
  if (error) return <p className="text-sm text-destructive">{error}</p>
  if (!data || chartData.length === 0) return <p className="text-sm text-muted-foreground">No hourly data for this day.</p>

  const current = chartData[hourIndex]
  const anomalyHod = data.anomaly_hour ? hodFromIso(data.anomaly_hour) : null
  const currentSeriesValue = data.segment ? current?.segment : current?.overall
  const overallLabel = `Overall ${metric}`

  return (
    <div className="space-y-3">
      <div className="h-[26rem] w-full">
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={chartData} margin={{ top: 8, right: 16, left: 0, bottom: 0 }}>
            <CartesianGrid strokeDasharray="3 3" opacity={0.2} />
            <XAxis dataKey="label" tick={{ fontSize: 12 }} interval={1} />
            {/* Two axes, not one - overall is a whole-dataset sum, the
                segment is one slice of it, so they live on very different
                scales. Sharing an axis flattens the segment line to a
                barely-visible ripple near zero (confirmed visually). */}
            <YAxis yAxisId="overall" tick={{ fontSize: 12 }} width={52} stroke="hsl(var(--primary))" />
            {data.segment && (
              <YAxis yAxisId="segment" orientation="right" tick={{ fontSize: 12 }} width={52} stroke="#ef4444" />
            )}
            <Tooltip />
            <Legend wrapperStyle={{ fontSize: 12 }} />
            <Line
              yAxisId="overall"
              type="monotone"
              dataKey="overall"
              stroke="hsl(var(--primary))"
              dot={false}
              strokeWidth={2}
              name={overallLabel}
            />
            {data.segment && (
              <>
                <Line
                  yAxisId="segment"
                  type="monotone"
                  dataKey="segment"
                  stroke="#ef4444"
                  dot={false}
                  strokeWidth={2}
                  name={data.segment_label}
                />
                <Line
                  yAxisId="segment"
                  type="monotone"
                  dataKey="segmentBaseline"
                  stroke="#64748b"
                  strokeDasharray="4 3"
                  dot={false}
                  strokeWidth={1.5}
                  name={`${data.segment_label} baseline`}
                />
              </>
            )}
            {anomalyHod != null && (
              <ReferenceLine
                x={formatHour(anomalyHod)}
                stroke="#ef4444"
                strokeDasharray="2 2"
                label={{ value: "detected here", fontSize: 11, fill: "#ef4444", position: "top" }}
              />
            )}
            {current && currentSeriesValue != null && (
              <ReferenceDot
                yAxisId={data.segment ? "segment" : "overall"}
                x={current.label}
                y={currentSeriesValue}
                r={6}
                fill="#ef4444"
                stroke="none"
              />
            )}
          </LineChart>
        </ResponsiveContainer>
      </div>

      <div className="flex items-center gap-2">
        <Button size="sm" variant="outline" onClick={() => setPlaying((p) => !p)}>
          {playing ? "Pause" : "Play"}
        </Button>
        <input
          type="range"
          min={0}
          max={23}
          value={hourIndex}
          onChange={(e) => {
            setPlaying(false)
            setHourIndex(Number(e.target.value))
          }}
          className="flex-1 accent-primary"
        />
        <span className="w-12 text-right text-xs text-muted-foreground">{current?.label}</span>
      </div>

      {current && (
        <p className="text-xs text-muted-foreground">
          {data.segment_label || overallLabel} at {current.label}: {currentSeriesValue?.toFixed(4)}
          {data.segment && current.segmentBaseline != null && ` vs baseline ${current.segmentBaseline.toFixed(4)}`}
        </p>
      )}
    </div>
  )
}
