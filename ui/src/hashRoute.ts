import { useEffect, useState } from 'react'

export type Route = { section: string; tab: string | null }

// '#/serving/models' -> { section:'serving', tab:'models' }; '' -> overview
export function parseHash(hash: string): Route {
  const clean = hash.replace(/^#\/?/, '')
  if (!clean) return { section: 'overview', tab: null }
  const [section, tab] = clean.split('/')
  return { section, tab: tab || null }
}

export function formatHash(section: string, tab: string | null): string {
  return tab ? `#/${section}/${tab}` : `#/${section}`
}

export function useHashRoute(): [Route, (s: string, t: string | null) => void] {
  const [route, setRoute] = useState<Route>(() => parseHash(location.hash))
  useEffect(() => {
    const on = () => setRoute(parseHash(location.hash))
    window.addEventListener('hashchange', on)
    return () => window.removeEventListener('hashchange', on)
  }, [])
  const nav = (s: string, t: string | null) => { location.hash = formatHash(s, t) }
  return [route, nav]
}
