import { useEffect, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api, queryKeys, type RequestTrace } from '../api'
import { openAdminEventSource } from '../eventSource'

function fmtMs(seconds: number): string {
  const ms = seconds * 1000
  if (ms < 1) return '0ms'
  if (ms < 10) return `${ms.toFixed(1)}ms`
  if (ms < 1000) return `${Math.round(ms)}ms`
  return `${(ms / 1000).toFixed(2)}s`
}

function ageSeconds(t: RequestTrace): number {
  // arrived_at is monotonic-ish; we don't know its absolute relation to now,
  // so age = (completed_at ?? now-equivalent) - arrived_at; for in-flight
  // requests show "—" rather than guessing.
  if (t.completed_at === null) return 0
  return t.completed_at - t.arrived_at
}

function statusKind(t: RequestTrace): 'inflight' | 'ok' | 'err' {
  if (t.completed_at === null) return 'inflight'
  if (t.error || (t.status_code !== null && t.status_code >= 400)) return 'err'
  return 'ok'
}

function dotClass(t: RequestTrace): string {
  switch (statusKind(t)) {
    case 'inflight': return 'dot-loading'
    case 'err': return 'dot-failed'
    default: return 'dot-ready'
  }
}

function Waterfall({ t }: { t: RequestTrace }) {
  const start = t.arrived_at
  const end = t.completed_at ?? Math.max(
    t.first_byte_at ?? 0,
    t.dispatched_at ?? 0,
    t.route_resolved_at ?? 0,
    t.arrived_at,
  )
  const total = Math.max(end - start, 1e-6)
  type Seg = { label: string; from: number; to: number; color: string }
  const segs: Seg[] = []
  if (t.route_resolved_at !== null) {
    segs.push({
      label: 'route',
      from: start, to: t.route_resolved_at,
      color: 'var(--ink-mute)',
    })
  }
  if (t.route_resolved_at !== null && t.dispatched_at !== null) {
    segs.push({
      label: 'placement',
      from: t.route_resolved_at, to: t.dispatched_at,
      color: 'var(--ink-dim)',
    })
  }
  if (t.dispatched_at !== null && t.first_byte_at !== null) {
    segs.push({
      label: 'ttft',
      from: t.dispatched_at, to: t.first_byte_at,
      color: 'var(--accent-dim)',
    })
  }
  if (t.first_byte_at !== null && t.completed_at !== null) {
    segs.push({
      label: 'stream',
      from: t.first_byte_at, to: t.completed_at,
      color: 'var(--accent)',
    })
  }
  return (
    <div className="space-y-2">
      <div className="relative h-3 bg-rule-soft">
        {segs.map((s, i) => {
          const left = ((s.from - start) / total) * 100
          const width = Math.max(((s.to - s.from) / total) * 100, 0.5)
          return (
            <div
              key={i}
              className="absolute inset-y-0"
              style={{ left: `${left}%`, width: `${width}%`, background: s.color }}
              title={`${s.label}: ${fmtMs(s.to - s.from)}`}
            />
          )
        })}
      </div>
      <div className="flex flex-wrap gap-x-6 gap-y-1 text-[11px] text-mute tracking-wider">
        {segs.map((s, i) => (
          <span key={i}>
            <span className="text-dim">{s.label}</span>{' '}
            <span className="tnum">{fmtMs(s.to - s.from)}</span>
          </span>
        ))}
        <span className="ml-auto">
          <span className="text-dim">total</span>{' '}
          <span className="tnum">{fmtMs(total)}</span>
        </span>
      </div>
    </div>
  )
}

function TraceRow({ t, isOpen, onToggle }: {
  t: RequestTrace; isOpen: boolean; onToggle: () => void
}) {
  return (
    <>
      <tr className="cursor-pointer" onClick={onToggle}>
        <td>
          <span className={`dot ${dotClass(t)}`} />
          <span className="text-dim font-mono text-[11px]">{t.request_id}</span>
        </td>
        <td className="text-dim text-[11px]">{t.method}</td>
        <td className="font-mono text-[12px] truncate max-w-[16ch]">
          {t.model_requested ?? '—'}
        </td>
        <td className="text-dim text-[12px]">
          {t.route_name ? (
            <span>
              {t.route_name}
              <span className="text-mute"> → </span>
              <span className="font-mono">{t.target_model}</span>
            </span>
          ) : t.target_model ?? <span className="text-mute">direct</span>}
        </td>
        <td className="text-mute text-[12px]">{t.backend ?? '—'}</td>
        <td className="text-right tnum text-[12px]">
          {t.completed_at !== null
            ? fmtMs(ageSeconds(t))
            : <span className="text-accent">streaming…</span>}
        </td>
        <td className="text-right tnum text-[12px] text-dim">
          {t.tokens_out > 0 ? `${t.tokens_out}` : '—'}
        </td>
        <td className="text-right text-[12px]">
          {t.error ? (
            <span className="text-err">{t.status_code ?? 'err'}</span>
          ) : t.status_code !== null ? (
            <span className={t.status_code >= 400 ? 'text-err' : 'text-ok'}>
              {t.status_code}
            </span>
          ) : <span className="text-mute">—</span>}
        </td>
      </tr>
      {isOpen && (
        <tr>
          <td colSpan={8} className="!pt-2 !pb-6">
            <div className="bg-elev/40 border border-rule p-5 space-y-5">
              <div className="grid grid-cols-1 md:grid-cols-3 gap-x-8 gap-y-2 text-[12px]">
                <div>
                  <div className="label">request</div>
                  <div className="font-mono">{t.method} {t.path}</div>
                </div>
                <div>
                  <div className="label">api key</div>
                  <div>{t.api_key_name ?? <span className="text-mute">none</span>}</div>
                </div>
                <div>
                  <div className="label">deployment</div>
                  <div>
                    {t.deployment_id !== null
                      ? <>#{t.deployment_id} <span className="text-mute">({t.backend})</span></>
                      : <span className="text-mute">—</span>}
                  </div>
                </div>
                <div>
                  <div className="label">route</div>
                  <div>
                    {t.route_name ?? <span className="text-mute">direct</span>}
                  </div>
                </div>
                <div>
                  <div className="label">profile</div>
                  <div>
                    {t.profile_name ?? <span className="text-mute">—</span>}
                  </div>
                </div>
                <div>
                  <div className="label">cold load</div>
                  <div>
                    {t.cold_loaded
                      ? <span className="text-accent">yes</span>
                      : <span className="text-mute">no</span>}
                  </div>
                </div>
              </div>
              <Waterfall t={t} />
              {t.error && (
                <div className="text-err text-[12px] font-mono whitespace-pre-wrap border-l border-err/40 pl-3">
                  {t.error}
                </div>
              )}
            </div>
          </td>
        </tr>
      )}
    </>
  )
}

export default function Requests() {
  // Seed from REST so the table renders something on initial load even if
  // the SSE stream hasn't fired yet.
  const seed = useQuery({ queryKey: queryKeys.requestsSeed, queryFn: api.listRequests })
  const [traces, setTraces] = useState<Map<string, RequestTrace>>(new Map())
  const [openId, setOpenId] = useState<string | null>(null)
  const [paused, setPaused] = useState(false)
  const seedApplied = useRef(false)

  useEffect(() => {
    if (!seed.data || seedApplied.current) return
    seedApplied.current = true
    setTraces(prev => {
      const m = new Map(prev)
      for (const t of seed.data!) m.set(t.request_id, t)
      return m
    })
  }, [seed.data])

  useEffect(() => {
    if (paused) return
    const handler = (e: MessageEvent) => {
      try {
        const data = JSON.parse(e.data)
        if (Array.isArray(data)) {
          setTraces(() => {
            const m = new Map<string, RequestTrace>()
            for (const t of data as RequestTrace[]) m.set(t.request_id, t)
            return m
          })
        } else if (data && typeof data === 'object' && 'request_id' in data) {
          setTraces(prev => {
            const trace = data as RequestTrace
            const m = new Map(prev)
            m.set(trace.request_id, trace)
            if (m.size > 256) {
              const oldest = Array.from(m.keys()).slice(0, m.size - 256)
              for (const k of oldest) m.delete(k)
            }
            return m
          })
        }
      } catch { /* ignore malformed events */ }
    }
    return openAdminEventSource('/admin/requests/stream', {
      listeners: {
        snapshot: handler,
        started: handler,
        updated: handler,
        completed: handler,
      },
      onOpenError: e => console.error('requests stream open failed', e),
    })
  }, [paused])

  const rows = Array.from(traces.values()).sort(
    (a, b) => b.arrived_at - a.arrived_at,
  )
  const inflight = rows.filter(t => t.completed_at === null).length
  const errors = rows.filter(t => statusKind(t) === 'err').length

  return (
    <div className="space-y-10">
      <header className="flex items-baseline justify-between">
        <h2 className="text-2xl font-light tracking-tightish caret">requests</h2>
        <div className="flex items-center gap-6">
          <button
            className="text-[11px] tracking-wider hover:text-dim transition-colors"
            onClick={() => setPaused(p => !p)}
          >
            <span className={paused ? 'text-accent' : 'text-mute'}>
              {paused ? '▶ resume' : '❚❚ pause'}
            </span>
          </button>
          <button
            className="text-mute text-[11px] tracking-wider hover:text-dim transition-colors"
            onClick={() => { setTraces(new Map()); setOpenId(null) }}
          >
            clear
          </button>
          <div className="label">
            {rows.length} traced · {inflight} in flight{errors > 0 ? ` · ${errors} errored` : ''}
          </div>
        </div>
      </header>

      <section className="space-y-4">
        <table className="ditable">
          <thead>
            <tr>
              <th>id</th>
              <th>method</th>
              <th>model</th>
              <th>route → target</th>
              <th>backend</th>
              <th className="text-right">total</th>
              <th className="text-right">out tok</th>
              <th className="text-right">status</th>
            </tr>
          </thead>
          <tbody>
            {rows.length === 0 && (
              <tr>
                <td colSpan={8} className="!py-12 text-center text-mute">
                  no traffic yet. send a request to /v1/chat/completions to see it appear.
                </td>
              </tr>
            )}
            {rows.map(t => (
              <TraceRow
                key={t.request_id}
                t={t}
                isOpen={openId === t.request_id}
                onToggle={() => setOpenId(openId === t.request_id ? null : t.request_id)}
              />
            ))}
          </tbody>
        </table>
      </section>
    </div>
  )
}
