import { useEffect, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api, queryKeys } from '../api'
import { openAdminEventSource } from '../eventSource'

type LifecycleEvent = { kind: string; payload: unknown; ts: string }

export default function Logs() {
  const deps = useQuery({
    queryKey: queryKeys.deployments,
    queryFn: api.listDeployments,
    refetchInterval: 5000,
  })
  const models = useQuery({ queryKey: queryKeys.models, queryFn: api.listModels })
  const [selected, setSelected] = useState<number | null>(null)
  const [lines, setLines] = useState<string[]>([])
  const [streaming, setStreaming] = useState(false)
  const [streamError, setStreamError] = useState('')
  const [events, setEvents] = useState<LifecycleEvent[]>([])
  const logRef = useRef<HTMLDivElement | null>(null)
  const stickToBottom = useRef(true)

  useEffect(() => {
    if (selected !== null) return
    const list = deps.data ?? []
    // Prefer the most recently active deployment; fall back to the newest row
    // (highest id) so we don't open onto a long-dead container by default.
    const active = list.find(d => d.status === 'ready' || d.status === 'loading')
    if (active) {
      setSelected(active.id)
      return
    }
    const sorted = [...list].sort((a, b) => b.id - a.id)
    if (sorted.length > 0) setSelected(sorted[0].id)
  }, [deps.data, selected])

  useEffect(() => {
    if (selected === null) return
    setLines([])
    setStreamError('')
    setStreaming(true)
    const close = openAdminEventSource(
      `/admin/deployments/${selected}/logs/stream`,
      {
        onMessage: e => {
          setLines(prev => {
            const next = [...prev, e.data]
            return next.length > 2000 ? next.slice(-2000) : next
          })
        },
        onError: (_event, source) => {
          setStreaming(false)
          setStreamError('stream closed (container stopped or auth failed)')
          source.close()
        },
        onOpenError: e => {
          setStreaming(false)
          setStreamError(e.message)
        },
      },
    )
    return () => {
      setStreaming(false)
      close()
    }
  }, [selected])

  useEffect(() => {
    const close = openAdminEventSource('/admin/events', {
      onMessage: e => {
        try {
          const obj = JSON.parse(e.data) as LifecycleEvent
          setEvents(prev => [obj, ...prev].slice(0, 50))
        } catch { /* ignore */ }
      },
      onError: (_event, source) => source.close(),
    })
    return close
  }, [])

  useEffect(() => {
    if (!logRef.current || !stickToBottom.current) return
    logRef.current.scrollTop = logRef.current.scrollHeight
  }, [lines])

  function onLogScroll(e: React.UIEvent<HTMLDivElement>) {
    const el = e.currentTarget
    const dist = el.scrollHeight - el.scrollTop - el.clientHeight
    stickToBottom.current = dist < 60
  }

  // Active deployments first (so the dropdown opens on something useful),
  // then everything else by descending id (newest first). Rows without a
  // container_id are excluded because their SSE attach always 404s.
  const visibleDeps = (deps.data ?? [])
    .filter(d => d.container_id !== null && d.container_id !== undefined)
    .sort((a, b) => {
      const aLive = a.status === 'ready' || a.status === 'loading' ? 0 : 1
      const bLive = b.status === 'ready' || b.status === 'loading' ? 0 : 1
      if (aLive !== bLive) return aLive - bLive
      return b.id - a.id
    })

  const selectedDep = visibleDeps.find(d => d.id === selected)

  return (
    <div className="space-y-10">
      <header className="flex items-baseline justify-between">
        <h2 className="text-2xl font-light tracking-tightish caret">logs</h2>
        <div className="flex items-center gap-3">
          {streaming && (
            <span className="flex items-center text-[11px] tracking-wider text-accent">
              <span className="dot dot-loading" />live
            </span>
          )}
          {streamError && (
            <span className="text-err text-[11px] tracking-wider">{streamError}</span>
          )}
        </div>
      </header>

      <section className="space-y-3">
        <div className="label">container</div>
        <select
          className="field font-mono text-[13px] min-w-[420px]"
          value={selected ?? ''}
          onChange={e => {
            setSelected(e.target.value ? Number(e.target.value) : null)
          }}
        >
          <option value="">select deployment</option>
          {visibleDeps.map(d => {
            const m = (models.data ?? []).find(m => m.id === d.model_id)
            const live = d.status === 'ready' || d.status === 'loading'
            const marker = live ? '●' : '·'
            return (
              <option key={d.id} value={d.id}>
                {marker} #{d.id} · {m?.name ?? `model ${d.model_id}`} · {d.status}
              </option>
            )
          })}
        </select>
      </section>

      <section className="space-y-3">
        <div className="flex items-center justify-between">
          <div className="label">stdout</div>
          <div className="text-mute text-[10px] tracking-wider tnum">{lines.length} lines</div>
        </div>
        <div
          ref={logRef}
          onScroll={onLogScroll}
          className="border border-rule font-mono text-[11.5px] leading-relaxed text-ink/80 overflow-y-auto h-[32rem] p-4 bg-[#08080a]"
        >
          {selected === null && (
            <div className="text-mute">select a deployment above to tail its container logs</div>
          )}
          {selected !== null && lines.length === 0 && streaming && (
            <div className="text-mute">waiting for output<span className="caret"></span></div>
          )}
          {selected !== null && lines.length === 0 && !streaming && (
            <div className="text-mute">
              no log output. the container for #{selected}
              {selectedDep ? ` (${selectedDep.status})` : ''} may have been removed.
            </div>
          )}
          {lines.map((line, i) => {
            const meta = line.startsWith('[serve-engine]')
            return (
              <div
                key={i}
                className={
                  'whitespace-pre-wrap break-all ' +
                  (meta ? 'text-accent/70' : '')
                }
              >
                {line}
              </div>
            )
          })}
        </div>
      </section>

      <details className="border border-rule">
        <summary className="cursor-pointer px-4 py-3 text-dim text-[11px] tracking-wider hover:text-ink transition-colors select-none flex items-center justify-between">
          <span>lifecycle events</span>
          <span className="text-mute tnum">{events.length}</span>
        </summary>
        <div className="px-4 pb-4 text-[11px] font-mono space-y-1 max-h-64 overflow-y-auto">
          {events.length === 0 && <div className="text-mute">no events yet</div>}
          {events.map((e, i) => (
            <div key={i} className="flex gap-3">
              <span className="text-mute tnum w-16 shrink-0">{e.ts.slice(11, 19)}</span>
              <span className="text-accent shrink-0">{e.kind}</span>
              <span className="text-dim break-all">{JSON.stringify(e.payload)}</span>
            </div>
          ))}
        </div>
      </details>
    </div>
  )
}
