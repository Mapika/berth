import { useState } from 'react'
import { getToken, setToken } from '../api'

export default function TokenGate({ children }: { children: React.ReactNode }) {
  const [token, setLocalToken] = useState<string | null>(getToken())
  const [input, setInput] = useState('')

  if (!token) {
    return (
      <div className="min-h-screen flex items-center justify-center px-6">
        <div className="w-full max-w-md enter">
          <div className="flex items-center gap-2 mb-12">
            <div className="text-base">
              berth
            </div>
            <span className="caret" />
          </div>
          <div className="space-y-8">
            <div className="space-y-2">
              <div className="label">authenticate</div>
              <p className="text-dim text-[12px] leading-relaxed">
                Paste an admin-tier API key. If you don't have one,
                run this on the host:
              </p>
              <pre className="text-[12px] bg-elev border border-rule px-3 py-2 text-ink overflow-x-auto">
                <span className="text-mute select-none">$ </span>
                berth key create web --tier admin
              </pre>
            </div>
            <div className="space-y-3">
              <div className="label">api key</div>
              <input
                className="field w-full font-mono"
                placeholder="sk-..."
                value={input}
                onChange={e => setInput(e.target.value)}
                onKeyDown={e => {
                  if (e.key === 'Enter' && input.trim()) {
                    setToken(input.trim())
                    setLocalToken(input.trim())
                  }
                }}
                autoFocus
              />
              <button
                className="btn-primary w-full"
                disabled={!input.trim()}
                onClick={() => {
                  setToken(input.trim())
                  setLocalToken(input.trim())
                }}
              >
                Continue
              </button>
            </div>
          </div>
        </div>
      </div>
    )
  }
  return <>{children}</>
}
