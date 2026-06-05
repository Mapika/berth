import type { ComponentType } from 'react'
// Phase 1: Overview and Deployments are temporarily aliased to the existing
// Dashboard so the shell compiles before Phase 2 splits Dashboard apart.
import Dashboard from './views/Dashboard'
import Models from './views/Models'
import Adapters from './views/Adapters'
import Routes from './views/serving/Routes'
import Profiles from './views/serving/Profiles'
import Predictor from './views/Predictor'
import Playground from './views/Playground'
import Keys from './views/Keys'
import Logs from './views/Logs'
import Requests from './views/Requests'
import Cluster from './views/Cluster'
import Settings from './views/Settings'

const Overview = Dashboard
const Deployments = Dashboard

export type Tab = { id: string; label: string; component: ComponentType }
export type Section = { id: string; label: string; tabs: Tab[] }

export const NAV: Section[] = [
  { id: 'overview', label: 'overview', tabs: [
    { id: 'overview', label: 'overview', component: Overview },
  ] },
  { id: 'serving', label: 'serving', tabs: [
    { id: 'deployments', label: 'deployments', component: Deployments },
    { id: 'models', label: 'models', component: Models },
    { id: 'adapters', label: 'adapters', component: Adapters },
    { id: 'routes', label: 'routes', component: Routes },
    { id: 'profiles', label: 'profiles', component: Profiles },
  ] },
  { id: 'observe', label: 'observe', tabs: [
    { id: 'requests', label: 'requests', component: Requests },
    { id: 'logs', label: 'logs', component: Logs },
    { id: 'cluster', label: 'cluster', component: Cluster },
    { id: 'predictor', label: 'predictor', component: Predictor },
  ] },
  { id: 'admin', label: 'admin', tabs: [
    { id: 'keys', label: 'keys', component: Keys },
    { id: 'settings', label: 'settings', component: Settings },
  ] },
  { id: 'playground', label: 'playground', tabs: [
    { id: 'playground', label: 'playground', component: Playground },
  ] },
]

export function findSection(id: string): Section {
  return NAV.find(s => s.id === id) ?? NAV[0]
}

export function findTab(section: Section, tabId: string): Tab {
  return section.tabs.find(t => t.id === tabId) ?? section.tabs[0]
}
