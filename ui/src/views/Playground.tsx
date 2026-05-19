import { useMemo, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api, getToken, queryKeys, type Model, type ServiceRoute } from '../api'

type Stats = {
  ttftMs: number | null
  totalMs: number | null
  tokens: number
  tps: number | null
}

type PaneState = {
  model: string
  reasoning: string
  answer: string
  error: string
  stats: Stats
  pending: boolean
}

const EMPTY_STATS: Stats = { ttftMs: null, totalMs: null, tokens: 0, tps: null }
const FRESH_PANE: Omit<PaneState, 'model'> = {
  reasoning: '', answer: '', error: '', stats: EMPTY_STATS, pending: false,
}

async function streamOne(
  model: string,
  prompt: string,
  maxTokens: number,
  signal: AbortSignal,
  setPane: (updater: (p: PaneState) => PaneState) => void,
): Promise<void> {
  const t0 = performance.now()
  let firstTokenAt: number | null = null
  let tokenCount = 0
  try {
    const headers: Record<string, string> = { 'Content-Type': 'application/json' }
    const token = getToken()
    if (token) headers['Authorization'] = `Bearer ${token}`
    const r = await fetch('/v1/chat/completions', {
      method: 'POST',
      headers,
      signal,
      body: JSON.stringify({
        model,
        messages: [{ role: 'user', content: prompt }],
        stream: true,
        max_tokens: maxTokens,
      }),
    })
    if (!r.ok) {
      const body = await r.text()
      setPane(p => ({ ...p, error: `${r.status}: ${body.slice(0, 500)}` }))
      return
    }
    if (!r.body) return
    const reader = r.body.getReader()
    const decoder = new TextDecoder()
    let buf = ''
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      buf += decoder.decode(value, { stream: true })
      const lines = buf.split('\n')
      buf = lines.pop()!
      for (const line of lines) {
        if (!line.startsWith('data:')) continue
        const payload = line.slice(5).trim()
        if (payload === '[DONE]') continue
        try {
          const obj = JSON.parse(payload) as {
            choices?: { delta?: {
              reasoning?: string
              reasoning_content?: string
              content?: string
            } }[]
          }
          const delta = obj.choices?.[0]?.delta ?? {}
          const rd: string = delta.reasoning ?? delta.reasoning_content ?? ''
          const cd: string = delta.content ?? ''
          if ((rd || cd) && firstTokenAt === null) {
            firstTokenAt = performance.now()
            const ttft = Math.round(firstTokenAt - t0)
            setPane(p => ({ ...p, stats: { ...p.stats, ttftMs: ttft } }))
          }
          if (rd) {
            tokenCount += 1
            setPane(p => ({ ...p, reasoning: p.reasoning + rd }))
          }
          if (cd) {
            tokenCount += 1
            setPane(p => ({ ...p, answer: p.answer + cd }))
          }
        } catch { /* skip malformed lines */ }
      }
    }
  } catch (e: unknown) {
    if (!(e instanceof DOMException && e.name === 'AbortError')) {
      const message = e instanceof Error ? e.message : String(e)
      setPane(p => ({ ...p, error: `error: ${message}` }))
    }
  } finally {
    const t1 = performance.now()
    setPane(p => ({
      ...p,
      stats: {
        ...p.stats,
        totalMs: Math.round(t1 - t0),
        tokens: tokenCount,
        tps: tokenCount && t1 > t0 ? Math.round((tokenCount / (t1 - t0)) * 10000) / 10 : null,
      },
    }))
  }
}

function ModelSelect({
  value, onChange, models, routes,
}: {
  value: string
  onChange: (v: string) => void
  models: Model[]
  routes: ServiceRoute[]
}) {
  return (
    <select
      className="field w-full font-mono"
      value={value}
      onChange={e => onChange(e.target.value)}
    >
      <option value="">choose model or route</option>
      {routes.length > 0 && (
        <optgroup label="routes">
          {routes.map(r => (
            <option key={`r-${r.id}`} value={r.match_model}>
              {r.match_model} → {r.profile_name}
            </option>
          ))}
        </optgroup>
      )}
      {models.length > 0 && (
        <optgroup label="models">
          {models.map(m => (
            <option key={`m-${m.id}`} value={m.name}>{m.name}</option>
          ))}
        </optgroup>
      )}
    </select>
  )
}

function PaneStats({ stats }: { stats: Stats }) {
  if (stats.ttftMs === null && stats.totalMs === null) return null
  return (
    <div className="flex gap-5 text-mute text-[11px] tnum">
      {stats.ttftMs !== null && (
        <div><span className="text-dim">ttft</span> {stats.ttftMs}ms</div>
      )}
      {stats.totalMs !== null && (
        <div><span className="text-dim">total</span> {stats.totalMs}ms</div>
      )}
      {stats.tps !== null && (
        <div><span className="text-dim">tok/s</span> {stats.tps}</div>
      )}
      {stats.tokens > 0 && (
        <div><span className="text-dim">tokens</span> {stats.tokens}</div>
      )}
    </div>
  )
}

function PaneOutput({
  pane, showThinking, onToggleThinking,
}: {
  pane: PaneState
  showThinking: boolean
  onToggleThinking: (v: boolean) => void
}) {
  if (pane.error) {
    return (
      <pre className="text-err text-[12px] whitespace-pre-wrap font-mono border border-err/30 px-3 py-2">
        {pane.error}
      </pre>
    )
  }
  return (
    <div className="space-y-3">
      {pane.reasoning && (
        <details
          open={showThinking}
          onToggle={e => onToggleThinking((e.target as HTMLDetailsElement).open)}
          className="border border-rule"
        >
          <summary className="cursor-pointer px-4 py-3 text-dim text-[11px] tracking-wider hover:text-ink transition-colors select-none flex items-center gap-2">
            <span className="dot dot-loading" style={{ width: 5, height: 5 }} />
            thinking
            <span className="text-mute tnum">{pane.reasoning.length.toLocaleString()}c</span>
          </summary>
          <pre className="px-4 pb-4 text-[12px] text-dim whitespace-pre-wrap font-mono leading-relaxed">
            {pane.reasoning}
          </pre>
        </details>
      )}
      {(pane.answer || (pane.pending && !pane.reasoning)) && (
        <pre className="text-[13px] whitespace-pre-wrap font-mono leading-relaxed border-l border-accent pl-4 min-h-[2.5rem]">
          {pane.answer || (pane.pending ? <span className="text-mute">waiting for tokens…</span> : '')}
        </pre>
      )}
      {!pane.answer && !pane.reasoning && !pane.pending && (
        <div className="text-mute text-[11px] tracking-wider">no output yet</div>
      )}
    </div>
  )
}

export default function Playground() {
  const models = useQuery({ queryKey: queryKeys.models, queryFn: api.listModels })
  const routes = useQuery({ queryKey: queryKeys.routes, queryFn: api.listRoutes })

  const [compareMode, setCompareMode] = useState(false)
  const [paneA, setPaneA] = useState<PaneState>({ model: '', ...FRESH_PANE })
  const [paneB, setPaneB] = useState<PaneState>({ model: '', ...FRESH_PANE })
  const [prompt, setPrompt] = useState('')
  const [maxTokens, setMaxTokens] = useState(4096)
  const [showThinkingA, setShowThinkingA] = useState(true)
  const [showThinkingB, setShowThinkingB] = useState(true)
  const abortRef = useRef<AbortController | null>(null)

  const modelList = useMemo(() => models.data ?? [], [models.data])
  const routeList = useMemo(
    () => (routes.data ?? []).filter(r => r.enabled),
    [routes.data],
  )

  const canSendA = !!paneA.model && !!prompt.trim()
  const anyPending = paneA.pending || paneB.pending

  async function send() {
    if (!canSendA) return
    if (compareMode && !paneB.model) return
    abortRef.current?.abort()
    const ctrl = new AbortController()
    abortRef.current = ctrl

    setPaneA(p => ({ ...p, ...FRESH_PANE, model: p.model, pending: true }))
    if (compareMode) {
      setPaneB(p => ({ ...p, ...FRESH_PANE, model: p.model, pending: true }))
    }

    const tasks: Promise<void>[] = [
      streamOne(paneA.model, prompt, maxTokens, ctrl.signal, setPaneA)
        .finally(() => setPaneA(p => ({ ...p, pending: false }))),
    ]
    if (compareMode) {
      tasks.push(
        streamOne(paneB.model, prompt, maxTokens, ctrl.signal, setPaneB)
          .finally(() => setPaneB(p => ({ ...p, pending: false }))),
      )
    }
    await Promise.allSettled(tasks)
  }

  function stop() { abortRef.current?.abort() }

  return (
    <div className="space-y-10">
      <header className="flex items-baseline justify-between">
        <h2 className="text-2xl font-light tracking-tightish caret">playground</h2>
        <div className="flex items-center gap-6">
          <div className="flex items-center gap-2 text-[11px] tracking-wider">
            <button
              className={
                'transition-colors ' +
                (!compareMode ? 'text-ink' : 'text-mute hover:text-dim')
              }
              onClick={() => setCompareMode(false)}
            >
              single
            </button>
            <span className="text-mute">/</span>
            <button
              className={
                'transition-colors ' +
                (compareMode ? 'text-ink' : 'text-mute hover:text-dim')
              }
              onClick={() => setCompareMode(true)}
            >
              compare
            </button>
          </div>
          <div className="label">openai-compatible</div>
        </div>
      </header>

      <section className="space-y-6">
        <div className={
          'grid gap-6 ' +
          (compareMode ? 'grid-cols-1 md:grid-cols-2' : 'grid-cols-1')
        }>
          <div className="space-y-2">
            <div className="flex items-center justify-between">
              <div className="label">{compareMode ? 'a' : 'model'}</div>
              <PaneStats stats={paneA.stats} />
            </div>
            <ModelSelect
              value={paneA.model}
              onChange={v => setPaneA(p => ({ ...p, model: v }))}
              models={modelList}
              routes={routeList}
            />
          </div>
          {compareMode && (
            <div className="space-y-2">
              <div className="flex items-center justify-between">
                <div className="label">b</div>
                <PaneStats stats={paneB.stats} />
              </div>
              <ModelSelect
                value={paneB.model}
                onChange={v => setPaneB(p => ({ ...p, model: v }))}
                models={modelList}
                routes={routeList}
              />
            </div>
          )}
        </div>

        <div className="grid grid-cols-[1fr_180px] gap-4">
          <div className="space-y-2">
            <div className="label">prompt</div>
            <textarea
              className="field w-full font-mono text-[13px]"
              style={{ minHeight: '8rem' }}
              placeholder="ask something"
              value={prompt}
              onChange={e => setPrompt(e.target.value)}
              onKeyDown={e => {
                if (e.key === 'Enter' && (e.metaKey || e.ctrlKey) && !anyPending) send()
              }}
            />
          </div>
          <div className="space-y-2">
            <div className="label">max tokens</div>
            <input
              type="number"
              className="field w-full font-mono tnum"
              value={maxTokens}
              min={32}
              max={32768}
              step={256}
              onChange={e => setMaxTokens(Number(e.target.value) || 4096)}
            />
          </div>
        </div>

        <div className="flex items-center gap-3">
          <button
            className="btn-primary"
            disabled={
              !canSendA ||
              (compareMode && !paneB.model) ||
              anyPending
            }
            onClick={send}
          >
            {anyPending ? 'streaming…' : (compareMode ? 'send both' : 'send')}
          </button>
          {anyPending && (
            <button className="btn" onClick={stop}>stop</button>
          )}
          <span className="label">Ctrl+Enter</span>
        </div>

        <div className={
          'grid gap-8 ' +
          (compareMode ? 'grid-cols-1 md:grid-cols-2' : 'grid-cols-1')
        }>
          <div className="space-y-3">
            {compareMode && (
              <div className="label flex items-center justify-between">
                <span>a — {paneA.model || '—'}</span>
                {paneA.pending && <span className="text-accent tracking-wider">streaming</span>}
              </div>
            )}
            <PaneOutput
              pane={paneA}
              showThinking={showThinkingA}
              onToggleThinking={setShowThinkingA}
            />
          </div>
          {compareMode && (
            <div className="space-y-3">
              <div className="label flex items-center justify-between">
                <span>b — {paneB.model || '—'}</span>
                {paneB.pending && <span className="text-accent tracking-wider">streaming</span>}
              </div>
              <PaneOutput
                pane={paneB}
                showThinking={showThinkingB}
                onToggleThinking={setShowThinkingB}
              />
            </div>
          )}
        </div>
      </section>
    </div>
  )
}
