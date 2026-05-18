import { useState } from 'react'
import TokenGate from './components/TokenGate'
import { clearToken } from './api'
import Dashboard from './views/Dashboard'
import Models from './views/Models'
import Adapters from './views/Adapters'
import Predictor from './views/Predictor'
import Playground from './views/Playground'
import Keys from './views/Keys'
import Logs from './views/Logs'
import Services from './views/Services'
import Requests from './views/Requests'
import Cluster from './views/Cluster'
import Settings from './views/Settings'

type View =
  | 'dashboard'
  | 'cluster'
  | 'models'
  | 'adapters'
  | 'services'
  | 'predictor'
  | 'playground'
  | 'requests'
  | 'keys'
  | 'logs'
  | 'settings'

const VIEWS: { id: View; label: string }[] = [
  { id: 'dashboard', label: 'dashboard' },
  { id: 'cluster', label: 'cluster' },
  { id: 'models', label: 'models' },
  { id: 'adapters', label: 'adapters' },
  { id: 'services', label: 'services' },
  { id: 'predictor', label: 'predictor' },
  { id: 'playground', label: 'playground' },
  { id: 'requests', label: 'requests' },
  { id: 'keys', label: 'keys' },
  { id: 'logs', label: 'logs' },
  { id: 'settings', label: 'settings' },
]

export default function App() {
  const [view, setView] = useState<View>('dashboard')

  return (
    <TokenGate>
      <div className="min-h-screen flex flex-col">
        <header className="sticky top-0 z-10 backdrop-blur-sm bg-bg/80 border-b border-rule">
          <div className="max-w-[1280px] mx-auto px-8 h-14 flex items-center justify-between gap-8">
            <div className="flex items-center gap-10">
              <div className="text-[13px] tracking-tightish select-none">
                serve<span className="text-accent">-</span>engine
              </div>
              <nav className="flex items-center gap-6">
                {VIEWS.map(v => {
                  const active = view === v.id
                  return (
                    <button
                      key={v.id}
                      onClick={() => setView(v.id)}
                      className={
                        'relative text-[12px] tracking-wider transition-colors py-4 ' +
                        (active
                          ? 'text-ink'
                          : 'text-mute hover:text-dim')
                      }
                    >
                      {v.label}
                      {active && (
                        <span className="absolute left-0 right-0 bottom-0 h-px bg-accent" />
                      )}
                    </button>
                  )
                })}
              </nav>
            </div>
            <button
              onClick={() => { clearToken(); location.reload() }}
              className="label hover:text-dim transition-colors"
            >
              sign out
            </button>
          </div>
        </header>
        <main className="flex-1 overflow-y-auto">
          <div key={view} className="max-w-[1280px] mx-auto px-8 py-12 enter">
            {view === 'dashboard' && <Dashboard />}
            {view === 'cluster' && <Cluster />}
            {view === 'models' && <Models />}
            {view === 'adapters' && <Adapters />}
            {view === 'services' && <Services />}
            {view === 'predictor' && <Predictor />}
            {view === 'playground' && <Playground />}
            {view === 'requests' && <Requests />}
            {view === 'keys' && <Keys />}
            {view === 'logs' && <Logs />}
            {view === 'settings' && <Settings />}
          </div>
        </main>
      </div>
    </TokenGate>
  )
}
