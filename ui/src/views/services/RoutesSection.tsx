import type { Dispatch, SetStateAction } from 'react'
import type { UseMutationResult } from '@tanstack/react-query'
import type { RouteDryRun, ServiceProfile, ServiceRoute } from '../../api'
import { DryRunResult } from './DryRunResult'

export type RouteFormState = {
  name: string
  match_model: string
  profile_name: string
  fallback_profile_name: string
  priority: string
}

type RoutesSectionProps = {
  profiles: ServiceProfile[]
  routes: ServiceRoute[]
  hasProfiles: boolean
  form: RouteFormState
  setForm: Dispatch<SetStateAction<RouteFormState>>
  routeError: string
  createRoute: UseMutationResult<ServiceRoute, Error, void, unknown>
  deleteRoute: UseMutationResult<void, Error, string, unknown>
  dryRunModel: string
  setDryRunModel: Dispatch<SetStateAction<string>>
  dryRunResult: RouteDryRun | null
  setDryRunResult: Dispatch<SetStateAction<RouteDryRun | null>>
  dryRun: UseMutationResult<RouteDryRun, Error, string, unknown>
}

export function RoutesSection({
  profiles,
  routes,
  hasProfiles,
  form,
  setForm,
  routeError,
  createRoute,
  deleteRoute,
  dryRunModel,
  setDryRunModel,
  dryRunResult,
  setDryRunResult,
  dryRun,
}: RoutesSectionProps) {
  return (
    <section className="space-y-4">
      <div className="flex items-baseline justify-between">
        <div className="label">routes</div>
        <div className="text-mute text-[11px] tracking-wider">
          public model name → profile · lower priority wins
        </div>
      </div>

      {!hasProfiles ? (
        <div className="border border-rule bg-elev/40 px-5 py-12 text-center text-mute text-[12px]">
          create a profile above first — routes point at profiles.
        </div>
      ) : (
        <div className="bg-elev/40 border border-rule p-5 space-y-4">
          <div className="grid grid-cols-12 gap-3">
            <div className="space-y-1 col-span-6 md:col-span-2">
              <div className="label">name</div>
              <input
                className="field font-mono w-full text-[12px]"
                placeholder="chat-default"
                value={form.name}
                onChange={e => setForm(f => ({ ...f, name: e.target.value }))}
              />
            </div>
            <div className="space-y-1 col-span-6 md:col-span-3">
              <div className="label">profile</div>
              <select
                className="field font-mono w-full text-[12px]"
                value={form.profile_name}
                onChange={e => setForm(f => ({ ...f, profile_name: e.target.value }))}
              >
                <option value="">choose…</option>
                {profiles.map(p => (
                  <option key={p.id} value={p.name}>{p.name}</option>
                ))}
              </select>
            </div>
            <div className="space-y-1 col-span-6 md:col-span-3">
              <div className="label">fallback (optional)</div>
              <select
                className="field font-mono w-full text-[12px]"
                value={form.fallback_profile_name}
                onChange={e => setForm(f => ({ ...f, fallback_profile_name: e.target.value }))}
              >
                <option value="">none</option>
                {profiles
                  .filter(p => p.name !== form.profile_name)
                  .map(p => (
                    <option key={p.id} value={p.name}>{p.name}</option>
                  ))}
              </select>
            </div>
            <div className="space-y-1 col-span-6 md:col-span-3">
              <div className="label">match model (exact)</div>
              <input
                className="field font-mono w-full text-[12px]"
                placeholder="chat"
                value={form.match_model}
                onChange={e => setForm(f => ({ ...f, match_model: e.target.value }))}
              />
            </div>
            <div className="space-y-1 col-span-12 md:col-span-1">
              <div className="label">pri</div>
              <input
                className="field font-mono w-full text-[12px] tnum text-right"
                value={form.priority}
                onChange={e => setForm(f => ({ ...f, priority: e.target.value }))}
              />
            </div>
          </div>
          {routeError && (
            <div className="text-err text-[11px] tracking-wider">{routeError}</div>
          )}
          <div className="flex items-center gap-3">
            <button
              className="btn-primary"
              disabled={
                !form.name.trim() ||
                !form.match_model.trim() ||
                !form.profile_name ||
                createRoute.isPending
              }
              onClick={() => createRoute.mutate()}
            >
              {createRoute.isPending ? 'creating…' : 'create route'}
            </button>
            <span className="label">
              callable as <span className="text-dim">model: {form.match_model || '<name>'}</span>
            </span>
          </div>
        </div>
      )}

      {hasProfiles && routes.length > 0 && (
        <div className="bg-elev/40 border border-rule px-5 py-4 space-y-3">
          <div className="flex items-center gap-3">
            <div className="label whitespace-nowrap">dry-run</div>
            <input
              className="field font-mono w-full text-[12px]"
              placeholder="model name to test (e.g. chat)"
              value={dryRunModel}
              onChange={e => setDryRunModel(e.target.value)}
              onKeyDown={e => {
                if (e.key === 'Enter' && dryRunModel.trim()) {
                  dryRun.mutate(dryRunModel.trim())
                }
              }}
            />
            <button
              className="btn"
              disabled={!dryRunModel.trim() || dryRun.isPending}
              onClick={() => dryRun.mutate(dryRunModel.trim())}
            >
              {dryRun.isPending ? 'testing…' : 'test'}
            </button>
            {dryRunResult && (
              <button
                className="text-mute text-[11px] tracking-wider hover:text-dim transition-colors whitespace-nowrap"
                onClick={() => { setDryRunResult(null); setDryRunModel('') }}
              >
                clear
              </button>
            )}
          </div>
          {dryRunResult && (
            <DryRunResult result={dryRunResult} />
          )}
        </div>
      )}

      <table className="ditable">
        <thead>
          <tr>
            <th className="w-12">pri</th>
            <th>name</th>
            <th>match model</th>
            <th>profile</th>
            <th>fallback</th>
            <th>enabled</th>
            <th className="text-right">actions</th>
          </tr>
        </thead>
        <tbody>
          {routes.length === 0 && (
            <tr>
              <td colSpan={7} className="!py-12 text-center text-mute">
                {hasProfiles
                  ? 'no routes. create one above to expose a public model name.'
                  : 'no routes — and no profiles to route at yet.'}
              </td>
            </tr>
          )}
          {routes
            .slice()
            .sort((a, b) => a.priority - b.priority)
            .map(r => (
              <tr key={r.id}>
                <td className="text-mute tnum">{r.priority}</td>
                <td>{r.name}</td>
                <td className="font-mono text-[12px]">{r.match_model}</td>
                <td className="text-dim">{r.profile_name}</td>
                <td className="text-mute">{r.fallback_profile_name ?? '—'}</td>
                <td>
                  <span className={`dot ${r.enabled ? 'dot-ready' : 'dot-stopped'}`} />
                  <span className="text-dim">{r.enabled ? 'on' : 'off'}</span>
                </td>
                <td className="text-right">
                  <button
                    className="btn-link-danger disabled:opacity-40"
                    disabled={deleteRoute.isPending}
                    onClick={() => {
                      if (confirm(`delete route ${r.name}?`)) deleteRoute.mutate(r.name)
                    }}
                  >
                    delete
                  </button>
                </td>
              </tr>
            ))}
        </tbody>
      </table>
    </section>
  )
}
