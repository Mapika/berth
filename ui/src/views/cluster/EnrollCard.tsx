import { useEffect, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { api, enrollmentUri, queryKeys, type EnrollResponse } from '../../api'

export function EnrollCard({ onMinted }: { onMinted: () => void }) {
  const qc = useQueryClient()
  const [label, setLabel] = useState('')
  const [error, setError] = useState('')
  const [minted, setMinted] = useState<EnrollResponse | null>(null)
  const [copiedKey, setCopiedKey] = useState<'uri' | 'cmd' | null>(null)
  const [expiresAt, setExpiresAt] = useState<number | null>(null)
  const [now, setNow] = useState<number>(Date.now())

  useEffect(() => {
    if (!expiresAt) return
    const id = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(id)
  }, [expiresAt])

  const enroll = useMutation({
    mutationFn: (l: string) => api.enrollNode(l),
    onMutate: () => {
      setError('')
      setMinted(null)
    },
    onError: (e: Error) => setError(e.message),
    onSuccess: (data) => {
      setMinted(data)
      setExpiresAt(Date.now() + 10 * 60 * 1000)
      qc.invalidateQueries({ queryKey: queryKeys.nodes })
      onMinted()
    },
  })

  const uri = minted ? enrollmentUri(minted) : ''
  const command = minted ? `berth agent register --uri '${uri}'` : ''
  const remaining = expiresAt ? Math.max(0, expiresAt - now) : 0
  const remainingLabel = remaining > 0
    ? `${Math.floor(remaining / 60000)}m ${Math.floor((remaining % 60000) / 1000).toString().padStart(2, '0')}s`
    : 'expired'

  function copy(text: string, key: 'uri' | 'cmd') {
    navigator.clipboard.writeText(text)
    setCopiedKey(key)
    setTimeout(() => setCopiedKey(null), 1500)
  }

  return (
    <div className="border border-rule p-6 space-y-6">
      <div className="flex items-baseline justify-between">
        <div>
          <div className="label">enroll new node</div>
          <p className="text-mute text-[11px] tracking-wider mt-1">
            mint a single-use uri that pins the cluster ca to its fingerprint
          </p>
        </div>
        {minted && (
          <span className={'text-[10px] tracking-wider ' + (remaining > 60000 ? 'text-mute' : 'text-warn')}>
            expires in {remainingLabel}
          </span>
        )}
      </div>

      <div className="flex items-center gap-3">
        <input
          className="field flex-1 font-mono"
          placeholder="label  (e.g. gpu-rig-2)"
          value={label}
          onChange={e => setLabel(e.target.value.toLowerCase().replace(/\s+/g, '-'))}
          onKeyDown={e => {
            if (e.key === 'Enter' && label.trim()) enroll.mutate(label.trim())
          }}
        />
        <button
          className="btn-primary"
          disabled={!label.trim() || enroll.isPending}
          onClick={() => enroll.mutate(label.trim())}
        >
          {enroll.isPending ? 'minting…' : 'mint uri'}
        </button>
      </div>

      {error && (
        <div className="text-err text-[11px] tracking-wider">{error}</div>
      )}

      {minted && (
        <div className="space-y-5 pt-2 border-t border-rule-soft">
          <div className="space-y-2">
            <div className="flex items-baseline justify-between">
              <div className="label">enrollment uri</div>
              <button
                onClick={() => copy(uri, 'uri')}
                className="text-mute hover:text-accent text-[10px] tracking-wider transition-colors"
              >
                {copiedKey === 'uri' ? 'copied' : 'copy'}
              </button>
            </div>
            <pre className="text-[11px] bg-bg border border-rule px-3 py-3 text-dim overflow-x-auto break-all whitespace-pre-wrap leading-relaxed">
              {uri}
            </pre>
          </div>

          <div className="space-y-2">
            <div className="flex items-baseline justify-between">
              <div className="label">on the agent host</div>
              <button
                onClick={() => copy(command, 'cmd')}
                className="text-mute hover:text-accent text-[10px] tracking-wider transition-colors"
              >
                {copiedKey === 'cmd' ? 'copied' : 'copy'}
              </button>
            </div>
            <pre className="text-[12px] bg-bg border border-rule px-3 py-3 text-ink overflow-x-auto break-all whitespace-pre-wrap">
              <span className="text-mute select-none">$ </span>
              {command}
            </pre>
            <p className="text-mute text-[11px] tracking-wider leading-relaxed">
              the agent fetches the ca, verifies its sha256 against the pin in the uri, then registers.
              re-run <code className="text-dim">berth agent start</code> after.
            </p>
          </div>
        </div>
      )}
    </div>
  )
}
