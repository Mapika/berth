import type { Section } from '../nav'

export default function SubTabs({ section, activeTab, onSelect }: {
  section: Section
  activeTab: string
  onSelect: (tabId: string) => void
}) {
  if (section.tabs.length <= 1) return null
  return (
    <div className="border-b border-rule-soft">
      <div className="max-w-[1280px] mx-auto px-8 flex items-center gap-6">
        {section.tabs.map(t => {
          const on = activeTab === t.id
          return (
            <button
              key={t.id}
              onClick={() => onSelect(t.id)}
              className={
                'relative text-[11px] tracking-wider transition-colors py-3 ' +
                (on ? 'text-ink' : 'text-mute hover:text-dim')
              }
            >
              {t.label}
              {on && <span className="absolute left-0 right-0 bottom-0 h-px bg-accent" />}
            </button>
          )
        })}
      </div>
    </div>
  )
}
