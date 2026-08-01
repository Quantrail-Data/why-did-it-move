const BASE_URL = import.meta.env.VITE_BACKEND_URL || "http://localhost:8001"

async function request(path, options = {}) {
  const res = await fetch(`${BASE_URL}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...options,
  })
  if (!res.ok) {
    const detail = await res.json().catch(() => ({}))
    throw new Error(detail.detail || `${res.status} ${res.statusText}`)
  }
  return res.json()
}

export function listAnomalyCandidates({ day, status = "open" } = {}) {
  const params = new URLSearchParams()
  if (day) params.set("day", day)
  if (status) params.set("status", status)
  return request(`/api/anomalies?${params.toString()}`)
}

// Every anomaly the system has ever detected, across every day and every
// status (open, investigated, dismissed) - the raw material for the
// anomaly-count-by-metric chart. Deliberately no server-side aggregation:
// the whole loaded batch is a few hundred rows at most, so grouping by
// day/metric client-side is cheap and needs no new backend endpoint - same
// call as listAnomalyCandidates, just with the "all" status sentinel and no
// day filter.
export function getAllAnomaliesEver() {
  return request("/api/anomalies?status=all")
}

export function triggerScan({ sinceDay } = {}) {
  return request("/api/scan", {
    method: "POST",
    body: JSON.stringify({ since_day: sinceDay || null }),
  })
}

export function investigate({ metric, day, anomalyCandidateId }) {
  return request("/api/investigate", {
    method: "POST",
    body: JSON.stringify({
      metric,
      day,
      anomaly_candidate_id: anomalyCandidateId || null,
    }),
  })
}

export function getInvestigation(id) {
  return request(`/api/investigations/${id}`)
}

export function getMetricTree({ day }) {
  const params = new URLSearchParams({ day })
  return request(`/api/metric-tree?${params.toString()}`)
}

export function getMetricHistory({ metric }) {
  const params = new URLSearchParams({ metric })
  return request(`/api/metric-history?${params.toString()}`)
}

export function getLatencyStats({ endpoint = "investigate" } = {}) {
  const params = new URLSearchParams({ endpoint })
  return request(`/api/latency-stats?${params.toString()}`)
}

export function getRevenueSignals({ day } = {}) {
  const params = new URLSearchParams()
  if (day) params.set("day", day)
  return request(`/api/revenue-signals?${params.toString()}`)
}

export function ask(question, context) {
  return request("/api/ask", {
    method: "POST",
    body: JSON.stringify({ question, context: context || null }),
  })
}

export function getTimeline({ metric, day, dimension, value, dimension2, value2 }) {
  const params = new URLSearchParams({ metric, day })
  if (dimension && value != null) {
    params.set("dimension", dimension)
    params.set("value", value)
  }
  if (dimension2 && value2 != null) {
    params.set("dimension2", dimension2)
    params.set("value2", value2)
  }
  return request(`/api/timeline?${params.toString()}`)
}
