import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, queryKeys, type Deployment, type GpuSnapshot, type Model } from '../api'

function fmtMb(mb: number | null | undefined): string {
  if (!mb) return '-'
  if (mb >= 1024) return `${(mb / 1024).toFixed(1)} GB`
  return `${mb} MB`
}

function VramCell({ used, reserved, status }: { used: number | null; reserved: number; status: string }) {
  if (status === 'stopped' || status === 'failed') {
    return <span>-</span>
  }
  if (used && used > 0) {
    return (
      <div className="flex flex-col items-end leading-tight">
        <span>{fmtMb(used)}</span>
        <span className="text-mute text-[10px]">est {fmtMb(reserved)}</span>
      </div>
    )
  }
  return (
    <div className="flex flex-col items-end leading-tight">
      <span className="text-dim">{fmtMb(reserved)}</span>
      <span className="text-mute text-[10px]">est</span>
    </div>
  )
}

function fmtGpus(ids: number[] | undefined): string {
  if (!ids || ids.length === 0) return '-'
  return ids.length === 1 ? `gpu ${ids[0]}` : `gpu ${ids.join(',')}`
}

function gpuGridCols(count: number): string {
  if (count <= 1) return 'grid-cols-1'
  if (count === 2) return 'grid-cols-1 md:grid-cols-2'
  if (count === 3) return 'grid-cols-1 md:grid-cols-3'
  return 'grid-cols-1 md:grid-cols-2 lg:grid-cols-4'
}

function idleCountdown(d: Deployment): string | null {
  if (d.pinned) return null
  if (!d.idle_timeout_s || !d.last_request_at) return null
  // sqlite CURRENT_TIMESTAMP is naive UTC ("YYYY-MM-DD HH:MM:SS"); coerce to ISO
  // with explicit Z so Date.parse doesn't treat it as local time.
  const iso = String(d.last_request_at).replace(' ', 'T') + 'Z'
  const last = Date.parse(iso)
  if (Number.isNaN(last)) return null
  const remaining = d.idle_timeout_s - (Date.now() - last) / 1000
  if (remaining <= 0) return 'evicting'
  if (remaining < 60) return `${Math.round(remaining)}s`
  return `${Math.round(remaining / 60)}m`
}

function DeploymentChip({ d, modelName }: { d: Deployment; modelName: string }) {
  const idle = idleCountdown(d)
  const vram = d.vram_used_mb && d.vram_used_mb > 0 ? d.vram_used_mb : d.vram_reserved_mb
  return (
    <div
      className="flex items-center gap-3 text-[12px] py-1.5"
      title={d.last_error || `deployment #${d.id} on ${d.backend}`}
    >
      <span className={`dot dot-${d.status}`} />
      <span className="text-ink truncate flex-1 min-w-0">{modelName}</span>
      <span className="text-mute text-[10px] tracking-wider hidden lg:inline">{d.backend}</span>
      <span className="text-dim tnum">{fmtMb(vram)}</span>
      {d.pinned ? (
        <span className="text-accent text-[10px] tracking-wider">pin</span>
      ) : idle ? (
        <span className="text-mute text-[10px] tracking-wider" title="idle countdown">
          {idle}
        </span>
      ) : (
        <span className="text-mute text-[10px]">—</span>
      )}
    </div>
  )
}

function GpuCard({
  g, deployments, models,
}: {
  g: GpuSnapshot
  deployments: Deployment[]
  models: Model[]
}) {
  const pct = (g.memory_used_mb / g.memory_total_mb) * 100
  const onCard = deployments.filter(d =>
    (d.gpu_ids ?? []).includes(g.index) &&
    (d.status === 'ready' || d.status === 'loading'),
  )
  return (
    <div className="space-y-4">
      <div className="flex items-baseline justify-between">
        <div className="label">gpu {g.index}</div>
        <div className="text-mute text-[11px] tnum">{pct.toFixed(0)}%</div>
      </div>
      <div className="flex items-baseline gap-2 tnum">
        <div className="text-3xl font-light tracking-tightish">
          {(g.memory_used_mb / 1024).toFixed(1)}
        </div>
        <div className="text-mute text-[12px]">/ {(g.memory_total_mb / 1024).toFixed(0)} GB</div>
      </div>
      <div className="h-px bg-rule relative overflow-hidden">
        <div
          className="absolute inset-y-0 left-0 bg-accent transition-[width] duration-500"
          style={{ width: `${pct}%` }}
        />
      </div>
      <div className="flex items-center gap-6 text-mute text-[11px] tnum">
        <span>util {g.gpu_util_pct}%</span>
        <span>{g.power_w} w</span>
        <span className="ml-auto">
          {onCard.length === 0
            ? <span className="text-mute">idle</span>
            : <span className="text-dim">{onCard.length} loaded</span>}
        </span>
      </div>
      {onCard.length > 0 && (
        <div className="pt-2 border-t border-rule-soft space-y-0.5">
          {onCard.map(d => {
            const m = models.find(m => m.id === d.model_id)
            return (
              <DeploymentChip key={d.id} d={d} modelName={m?.name ?? `#${d.id}`} />
            )
          })}
        </div>
      )}
    </div>
  )
}

export default function Dashboard() {
  const qc = useQueryClient()
  const deps = useQuery({ queryKey: queryKeys.deployments, queryFn: api.listDeployments, refetchInterval: 2000 })
  const gpus = useQuery({ queryKey: queryKeys.gpus, queryFn: api.listGpus, refetchInterval: 2000 })
  const models = useQuery({ queryKey: queryKeys.models, queryFn: api.listModels, refetchInterval: 5000 })
  const nodes = useQuery({ queryKey: queryKeys.nodes, queryFn: api.listNodes, refetchInterval: 5000 })
  const [showAll, setShowAll] = useState(false)
  const [pendingId, setPendingId] = useState<number | null>(null)
  const [actionError, setActionError] = useState('')

  const stopMut = useMutation({
    mutationFn: (id: number) => api.stopDeployment(id),
    onMutate: id => { setPendingId(id); setActionError('') },
    onError: (e: Error) => setActionError(e.message),
    onSettled: () => { setPendingId(null); qc.invalidateQueries({ queryKey: queryKeys.deployments }) },
  })
  const pinMut = useMutation({
    mutationFn: ({ id, pinned }: { id: number; pinned: boolean }) =>
      pinned ? api.unpinDeployment(id) : api.pinDeployment(id),
    onMutate: ({ id }) => { setPendingId(id); setActionError('') },
    onError: (e: Error) => setActionError(e.message),
    onSettled: () => { setPendingId(null); qc.invalidateQueries({ queryKey: queryKeys.deployments }) },
  })

  const all = deps.data ?? []
  const active = all.filter(d => d.status === 'ready' || d.status === 'loading')
  const visible = showAll ? all : active
  const hiddenCount = all.length - visible.length

  return (
    <div className="space-y-14">
      <header className="flex items-baseline justify-between">
        <h2 className="text-2xl font-light tracking-tightish caret">dashboard</h2>
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
                  <>
                    cluster <span className="text-dim">{ready}/{ns.length}</span>
                  </>
                )}
              </div>
            )
          })()}
          <div className="label">{(gpus.data ?? []).length} gpu / {active.length} active</div>
        </div>
      </header>

      <section className="space-y-6">
        <div className="label">gpus</div>
        <div className={'grid gap-12 ' + gpuGridCols((gpus.data ?? []).length)}>
          {(gpus.data ?? []).map(g => (
            <GpuCard
              key={g.index}
              g={g}
              deployments={all}
              models={models.data ?? []}
            />
          ))}
        </div>
      </section>

      <section className="space-y-4">
        <div className="flex items-center justify-between">
          <div className="label">deployments</div>
          <label className="text-mute text-[11px] tracking-wider select-none cursor-pointer hover:text-dim transition-colors">
            <input
              type="checkbox"
              className="mr-2 accent-accent align-middle"
              checked={showAll}
              onChange={e => setShowAll(e.target.checked)}
            />
            show stopped {hiddenCount > 0 && !showAll && (
              <span className="text-accent">({hiddenCount})</span>
            )}
          </label>
        </div>
        {actionError && (
          <div className="text-err text-[11px] tracking-wider">{actionError}</div>
        )}
        <table className="ditable">
          <thead>
            <tr>
              <th>#</th>
              <th>model</th>
              <th>backend</th>
              <th>node</th>
              <th>status</th>
              <th className="text-right">vram</th>
              <th className="text-right">gpu</th>
              <th className="text-right">actions</th>
            </tr>
          </thead>
          <tbody>
            {visible.length === 0 && (
              <tr>
                <td colSpan={8} className="!py-12 text-center text-mute">
                  no active deployments. load one from <span className="text-dim">models</span>
                </td>
              </tr>
            )}
            {visible.map(d => {
              const m = (models.data ?? []).find(m => m.id === d.model_id)
              const node = (nodes.data?.nodes ?? []).find(n => n.id === d.node_id)
              const nodeLabel = node?.label ?? (d.node_id ? `#${d.node_id}` : 'local')
              const live = d.status === 'ready' || d.status === 'loading'
              const busy = pendingId === d.id
              return (
                <tr key={d.id} title={d.last_error || undefined}>
                  <td className="text-mute tnum">{d.id}</td>
                  <td>{m?.name ?? '-'}</td>
                  <td className="text-dim">{d.backend}</td>
                  <td>
                    <span className={nodeLabel === 'local' ? 'text-mute' : 'text-accent'}>
                      {nodeLabel}
                    </span>
                  </td>
                  <td>
                    <span className={`dot dot-${d.status}`} />
                    <span className="text-dim">{d.status}</span>
                  </td>
                  <td className="text-right tnum">
                    <VramCell
                      used={d.vram_used_mb ?? null}
                      reserved={d.vram_reserved_mb}
                      status={d.status}
                    />
                  </td>
                  <td className="text-right text-dim tnum">{fmtGpus(d.gpu_ids)}</td>
                  <td className="text-right space-x-5 whitespace-nowrap">
                    <button
                      className={
                        'transition-opacity hover:opacity-70 disabled:opacity-40 ' +
                        (d.pinned ? 'text-accent' : 'text-dim')
                      }
                      disabled={busy}
                      onClick={() => pinMut.mutate({ id: d.id, pinned: !!d.pinned })}
                      title={d.pinned
                        ? 'pinned: idle reaper will not stop this deployment'
                        : 'pin to keep alive through idle timeout'}
                    >
                      {d.pinned ? 'unpin' : 'pin'}
                    </button>
                    {live ? (
                      <button
                        className="btn-link-danger disabled:opacity-40"
                        disabled={busy}
                        onClick={() => {
                          if (confirm(`stop deployment #${d.id}?`)) stopMut.mutate(d.id)
                        }}
                      >
                        {busy ? 'stopping...' : 'stop'}
                      </button>
                    ) : (
                      <span className="text-mute">—</span>
                    )}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </section>
    </div>
  )
}
