import { NAV } from '../nav'

export default function TopNav({ active, onSelect, onSignOut }: {
  active: string
  onSelect: (sectionId: string) => void
  onSignOut: () => void
}) {
  return (
    <header className="sticky top-0 z-10 backdrop-blur-sm bg-bg/80 border-b border-rule">
      <div className="max-w-[1280px] mx-auto px-8 h-14 flex items-center justify-between gap-8">
        <div className="flex items-center gap-10">
          <div className="text-[13px] tracking-tightish select-none">berth</div>
          <nav className="flex items-center gap-6">
            {NAV.map(s => {
              const on = active === s.id
              return (
                <button
                  key={s.id}
                  onClick={() => onSelect(s.id)}
                  className={
                    'relative text-[12px] tracking-wider transition-colors py-4 ' +
                    (on ? 'text-ink' : 'text-mute hover:text-dim')
                  }
                >
                  {s.label}
                  {on && <span className="absolute left-0 right-0 bottom-0 h-px bg-accent" />}
                </button>
              )
            })}
          </nav>
        </div>
        <button onClick={onSignOut} className="label hover:text-dim transition-colors">
          sign out
        </button>
      </div>
    </header>
  )
}
