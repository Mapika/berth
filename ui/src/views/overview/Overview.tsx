import { useQuery } from '@tanstack/react-query'
import { api, queryKeys } from '../../api'
import HealthStrip from './HealthStrip'
import StatTile from './StatTile'
import TrafficChart from './TrafficChart'
import LatencyChart from './LatencyChart'
import TopModels from './TopModels'
import ModelBreakdown from './ModelBreakdown'
import DeploymentsGlance from './DeploymentsGlance'
import { aggregateSnapshot, recentRates } from './useOverviewStats'

function unit(value: string, u: string) {
  return <>{value}<span className="text-mute text-[13px]"> {u}</span></>
}

export default function Overview() {
  const deps = useQuery({ queryKey: queryKeys.deployments, queryFn: api.listDeployments, refetchInterval: 2000 })
  const gpus = useQuery({ queryKey: queryKeys.gpus, queryFn: api.listGpus, refetchInterval: 2000 })
  const nodes = useQuery({ queryKey: queryKeys.nodes, queryFn: api.listNodes, refetchInterval: 5000 })
  const snap = useQuery({ queryKey: queryKeys.metricsSnapshot, queryFn: api.getMetricsSnapshot, refetchInterval: 2000 })

  // Live volume/throughput: last hour in 60s buckets, refreshed often.
  const live = useQuery({
    queryKey: queryKeys.usageSeries(3600, 60, 'none'),
    queryFn: () => api.getUsageSeries(3600, 60),
    refetchInterval: 5000,
  })
  // History: last 24h in 1h buckets.
  const history = useQuery({
    queryKey: queryKeys.usageSeries(86400, 3600, 'none'),
    queryFn: () => api.getUsageSeries(86400, 3600),
    refetchInterval: 30000,
  })
  const byModel = useQuery({
    queryKey: queryKeys.usageSeries(86400, 3600, 'model'),
    queryFn: () => api.getUsageByModel(86400, 3600),
    refetchInterval: 30000,
  })
  // Latency/error history (persisted): 24h summary for the tiles + breakdown,
  // 24h hourly buckets for the chart.
  const metricsSummary = useQuery({
    queryKey: queryKeys.metricsSummary(86400, 'none'),
    queryFn: () => api.getMetricsSummary(86400),
    refetchInterval: 15000,
  })
  const metricsHistory = useQuery({
    queryKey: queryKeys.metricsHistory(86400, 3600),
    queryFn: () => api.getMetricsHistory(86400, 3600),
    refetchInterval: 30000,
  })
  const metricsByModel = useQuery({
    queryKey: queryKeys.metricsSummary(86400, 'model'),
    queryFn: () => api.getMetricsByModel(86400),
    refetchInterval: 30000,
  })

  const all = deps.data ?? []
  const active = all.filter(d => d.status === 'ready' || d.status === 'loading')
  const stats = aggregateSnapshot(snap.data)
  const liveBuckets = live.data?.buckets ?? []
  const rates = recentRates(liveBuckets, 60)
  const volSpark = liveBuckets.slice(-30).map(b => b.count)
  const tokSpark = liveBuckets.slice(-30).map(b => b.tokens_out)
  const ms = metricsSummary.data?.summary
  const snapGpus = (snap.data?.nodes ?? []).reduce((n, x) => n + x.gpus.length, 0)
  const gpuCount = snapGpus || (gpus.data ?? []).length

  return (
    <div className="space-y-14">
      <header className="flex items-baseline justify-between">
        <h2 className="text-2xl font-light tracking-tightish caret">overview</h2>
        <div className="flex items-baseline gap-6">
          {(() => {
            const ns = nodes.data?.nodes ?? []
            const ready = ns.filter(n => n.status === 'ready').length
            const remote = ns.filter(n => n.label !== 'local').length
            if (ns.length === 0) return null
            return (
              <div className="label" title="cluster nodes">
                {remote === 0 ? (
                  <>single-node</>
                ) : (
                  <>cluster <span className="text-dim">{ready}/{ns.length}</span></>
                )}
              </div>
            )
          })()}
          <div className="label">{gpuCount} gpu / {active.length} active</div>
        </div>
      </header>

      <HealthStrip />

      <section className="space-y-6">
        <div className="label">request stats</div>
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-y-8 gap-x-6">
          <StatTile
            title="volume"
            value={unit(rates.reqPerMin.toFixed(rates.reqPerMin < 10 ? 1 : 0), 'req/min')}
            sub={`${stats.inFlight} in flight`}
            spark={volSpark}
            badge="live"
          />
          <StatTile
            title="latency"
            value={unit(ms?.latency_p50_ms != null ? String(ms.latency_p50_ms) : '—', 'ms p50')}
            sub={`p95 ${ms?.latency_p95_ms ?? '—'} ms`}
            badge="24h"
          />
          <StatTile
            title="errors"
            value={unit(ms ? (ms.error_rate * 100).toFixed(1) : '—', '%')}
            sub={
              <span className="text-accent">{ms?.error_count ?? 0} in 24h → see traffic</span>
            }
            badge="24h"
            onClick={() => { location.hash = '#/observe/requests' }}
          />
          <StatTile
            title="throughput"
            value={unit(rates.tokPerSec.toFixed(0), 'tok/s')}
            sub={`${rates.totalOut.toLocaleString()} out tok · 1h`}
            spark={tokSpark}
            badge="live"
          />
        </div>
      </section>

      <section className="space-y-4">
        <div className="flex items-baseline justify-between">
          <div className="label">traffic over time</div>
          <div className="text-mute text-[10px] tracking-wider uppercase">
            last 24h · bounded by retention
          </div>
        </div>
        <TrafficChart buckets={history.data?.buckets ?? []} />
      </section>

      <section className="space-y-4">
        <div className="flex items-baseline justify-between">
          <div className="label">latency &amp; errors over time</div>
          <div className="text-mute text-[10px] tracking-wider uppercase">last 24h</div>
        </div>
        <LatencyChart buckets={metricsHistory.data?.buckets ?? []} />
      </section>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-x-12 gap-y-10">
        <section className="space-y-4">
          <div className="label">top models · 24h</div>
          <TopModels groups={byModel.data?.groups ?? []} />
        </section>
        <section className="space-y-4">
          <div className="label">latency &amp; errors by model · 24h</div>
          <ModelBreakdown groups={metricsByModel.data?.groups ?? []} />
        </section>
      </div>

      <section>
        <DeploymentsGlance />
      </section>
    </div>
  )
}
