import TokenGate from './components/TokenGate'
import TopNav from './components/TopNav'
import SubTabs from './components/SubTabs'
import { clearToken } from './api'
import { findSection, findTab } from './nav'
import { useHashRoute } from './hashRoute'

export default function App() {
  const [route, navigate] = useHashRoute()
  const section = findSection(route.section)
  const tab = findTab(section, route.tab ?? section.tabs[0].id)
  const View = tab.component

  return (
    <TokenGate>
      <div className="min-h-screen flex flex-col">
        <TopNav
          active={section.id}
          onSelect={id => { const s = findSection(id); navigate(s.id, s.tabs[0].id) }}
          onSignOut={() => { clearToken(); location.reload() }}
        />
        <SubTabs section={section} activeTab={tab.id} onSelect={t => navigate(section.id, t)} />
        <main className="flex-1 overflow-y-auto">
          <div key={`${section.id}/${tab.id}`} className="max-w-[1280px] mx-auto px-8 py-12 enter">
            <View />
          </div>
        </main>
      </div>
    </TokenGate>
  )
}
