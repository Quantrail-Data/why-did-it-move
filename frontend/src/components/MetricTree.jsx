import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"

const STATUS_STYLES = {
  red: { dot: "bg-red-500", badge: "destructive", label: "Anomalous" },
  amber: { dot: "bg-amber-500", badge: "warning", label: "Watch" },
  green: { dot: "bg-emerald-500", badge: "success", label: "Normal" },
  // "Not evaluated" is deliberately NOT "Normal". A day we could not judge
  // (no trailing same-weekday history, or too little of it to trust) must
  // not be rendered as a clean bill of health - the tooltip carries the
  // specific reason from the backend. See backend/app/coverage.py.
  gray: { dot: "bg-muted-foreground/40", badge: "outline", label: "Not evaluated" },
}

const METRIC_LABELS = {
  revenue: "Revenue",
  fill_rate: "Fill rate",
  render_rate: "Render rate",
  ecpm: "eCPM",
  ctr: "CTR",
}

export default function MetricTree({ tree, loading }) {
  if (loading) {
    return <p className="text-sm text-muted-foreground">Computing metric tree…</p>
  }
  if (!tree?.length) {
    return <p className="text-sm text-muted-foreground">No data for this day yet.</p>
  }

  return (
    <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 md:grid-cols-5">
      {tree.map((node) => {
        const style = STATUS_STYLES[node.status] || STATUS_STYLES.gray
        return (
          <Card key={node.metric} title={node.not_evaluated_reason || undefined}>
            <CardHeader className="space-y-0 p-3 pb-1">
              <div className="flex items-center justify-between gap-1">
                <CardTitle className="truncate text-xs">{METRIC_LABELS[node.metric] || node.metric}</CardTitle>
                <span className={`h-2 w-2 shrink-0 rounded-full ${style.dot}`} />
              </div>
            </CardHeader>
            <CardContent className="p-3 pt-0">
              <div className="truncate text-base font-semibold">
                {node.actual != null ? node.actual.toLocaleString(undefined, { maximumFractionDigits: 4 }) : "-"}
              </div>
              <div className="mt-1 flex items-center justify-between gap-1">
                <span className="truncate text-[11px] text-muted-foreground">
                  {node.pct_deviation != null
                    ? `${node.pct_deviation >= 0 ? "+" : ""}${(node.pct_deviation * 100).toFixed(1)}%`
                    : node.baseline_n === 0
                      ? "no history"
                      : `only ${node.baseline_n} prior`}
                </span>
                <Badge variant={style.badge} className="shrink-0 px-1.5 py-0 text-[10px]">
                  {style.label}
                </Badge>
              </div>
            </CardContent>
          </Card>
        )
      })}
    </div>
  )
}
