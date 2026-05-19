import { useState } from 'react'

export function FingerprintBlock({ fp, copyable = true }: { fp: string; copyable?: boolean }) {
  const [copied, setCopied] = useState(false)
  const body = fp.replace(/^sha256:/, '')
  const groups: string[] = []
  for (let i = 0; i < body.length; i += 4) groups.push(body.slice(i, i + 4))
  return (
    <div className="flex items-center gap-3">
      <span className="text-mute text-[10px] tracking-wider">sha256</span>
      <code className="text-[11px] text-dim tracking-wider tnum break-all">
        {groups.join(' ')}
      </code>
      {copyable && (
        <button
          className="text-mute hover:text-accent text-[10px] tracking-wider transition-colors"
          onClick={() => {
            navigator.clipboard.writeText(fp)
            setCopied(true)
            setTimeout(() => setCopied(false), 1200)
          }}
        >
          {copied ? 'copied' : 'copy'}
        </button>
      )}
    </div>
  )
}
