import type { Dispatch, SetStateAction } from 'react'
import type { UseMutationResult } from '@tanstack/react-query'
import type { BackendInfo, Deployment, Model, Node, ServiceProfile } from '../../api'

export type ProfileFormState = {
  name: string
  model_name: string
  hf_repo: string
  backend: string
  gpu_ids: string
  max_model_len: string
  pinned: boolean
  node_label: string
}

type ProfileSectionProps = {
  profiles: ServiceProfile[]
  models: Model[]
  backends: BackendInfo[]
  nodes: Node[]
  form: ProfileFormState
  setForm: Dispatch<SetStateAction<ProfileFormState>>
  formError: string
  actionError: string
  createProfile: UseMutationResult<ServiceProfile, Error, void, unknown>
  deployProfile: UseMutationResult<Deployment, Error, string, unknown>
  deleteProfile: UseMutationResult<void, Error, string, unknown>
}

export function ProfileSection({
  profiles,
  models,
  backends,
  nodes,
  form,
  setForm,
  formError,
  actionError,
  createProfile,
  deployProfile,
  deleteProfile,
}: ProfileSectionProps) {
  return (
    <section className="space-y-4">
      <div className="flex items-baseline justify-between">
        <div className="label">profiles</div>
        <div className="text-mute text-[11px] tracking-wider">
          reusable launch definition
        </div>
      </div>
      {actionError && (
        <div className="text-err text-[11px] tracking-wider">{actionError}</div>
      )}

      <div className="bg-elev/40 border border-rule p-5 space-y-4">
        <div className="grid grid-cols-12 gap-3">
          <div className="space-y-1 col-span-12 md:col-span-3">
            <div className="label">profile name</div>
            <input
              className="field font-mono w-full text-[12px]"
              placeholder="qwen-vllm"
              value={form.name}
              onChange={e => setForm(f => ({ ...f, name: e.target.value }))}
            />
          </div>
          <div className="space-y-1 col-span-12 md:col-span-3">
            <div className="label">model name</div>
            <input
              className="field font-mono w-full text-[12px]"
              list="profile-model-list"
              placeholder="qwen"
              value={form.model_name}
              onChange={e => {
                const next = e.target.value
                const m = models.find(m => m.name === next)
                setForm(f => ({
                  ...f,
                  model_name: next,
                  hf_repo: m ? m.hf_repo : f.hf_repo,
                }))
              }}
            />
            <datalist id="profile-model-list">
              {models.map(m => (
                <option key={m.id} value={m.name} />
              ))}
            </datalist>
          </div>
          <div className="space-y-1 col-span-12 md:col-span-6">
            <div className="label">hf repo</div>
            <input
              className="field font-mono w-full text-[12px]"
              placeholder="Qwen/Qwen2.5-0.5B-Instruct"
              value={form.hf_repo}
              onChange={e => setForm(f => ({ ...f, hf_repo: e.target.value }))}
            />
          </div>
          <div className="space-y-1 col-span-6 md:col-span-2">
            <div className="label">backend</div>
            <select
              className="field font-mono w-full text-[12px]"
              value={form.backend}
              onChange={e => setForm(f => ({ ...f, backend: e.target.value }))}
            >
              <option value="">auto</option>
              {backends.map(b => (
                <option key={b.name} value={b.name}>{b.name}</option>
              ))}
            </select>
          </div>
          <div className="space-y-1 col-span-6 md:col-span-2">
            <div className="label">node</div>
            <select
              className="field font-mono w-full text-[12px]"
              value={form.node_label}
              onChange={e => setForm(f => ({ ...f, node_label: e.target.value }))}
            >
              <option value="">leader (local)</option>
              {nodes
                .filter(n => n.label !== 'local' && n.status === 'ready')
                .map(n => (
                  <option key={n.id} value={n.label}>
                    {n.label} · {n.gpu_count} gpu
                  </option>
                ))}
            </select>
          </div>
          <div className="space-y-1 col-span-6 md:col-span-2">
            <div className="label">gpu ids</div>
            <input
              className="field font-mono w-full text-[12px] tnum"
              placeholder="0 or 0,1"
              value={form.gpu_ids}
              onChange={e => setForm(f => ({ ...f, gpu_ids: e.target.value }))}
            />
          </div>
          <div className="space-y-1 col-span-6 md:col-span-3">
            <div className="label">max model len</div>
            <input
              className="field font-mono w-full text-[12px] tnum"
              value={form.max_model_len}
              onChange={e => setForm(f => ({ ...f, max_model_len: e.target.value }))}
            />
          </div>
          <div className="space-y-1 col-span-6 md:col-span-3 flex flex-col">
            <div className="label">options</div>
            <label className="text-[12px] text-dim flex items-center gap-2 select-none cursor-pointer pt-2">
              <input
                type="checkbox"
                className="accent-accent"
                checked={form.pinned}
                onChange={e => setForm(f => ({ ...f, pinned: e.target.checked }))}
              />
              pinned (skip idle reaper)
            </label>
          </div>
        </div>
        {formError && (
          <div className="text-err text-[11px] tracking-wider">{formError}</div>
        )}
        <div className="flex items-center gap-3">
          <button
            className="btn-primary"
            disabled={
              !form.name.trim() ||
              !form.model_name.trim() ||
              !form.hf_repo.trim() ||
              createProfile.isPending
            }
            onClick={() => createProfile.mutate()}
          >
            {createProfile.isPending ? 'creating…' : 'create profile'}
          </button>
        </div>
      </div>

      <table className="ditable">
        <thead>
          <tr>
            <th>name</th>
            <th>model</th>
            <th>backend</th>
            <th className="text-right">gpus</th>
            <th className="text-right">ctx</th>
            <th>pinned</th>
            <th className="text-right">actions</th>
          </tr>
        </thead>
        <tbody>
          {profiles.length === 0 && (
            <tr>
              <td colSpan={7} className="!py-12 text-center text-mute">
                no profiles yet. create one above to define how a model is launched.
              </td>
            </tr>
          )}
          {profiles.map(p => {
            const isDeploying = deployProfile.isPending && deployProfile.variables === p.name
            const isDeleting = deleteProfile.isPending && deleteProfile.variables === p.name
            return (
              <tr key={p.id}>
                <td>{p.name}</td>
                <td className="text-dim">{p.model_name}</td>
                <td className="text-dim">{p.backend}</td>
                <td className="text-right text-dim tnum">{p.gpu_ids.join(',') || '—'}</td>
                <td className="text-right tnum">{p.max_model_len}</td>
                <td>
                  {p.pinned
                    ? <span className="text-accent">yes</span>
                    : <span className="text-mute">no</span>}
                </td>
                <td className="text-right space-x-5 whitespace-nowrap">
                  <button
                    className="text-accent hover:opacity-70 transition-opacity disabled:opacity-40"
                    disabled={isDeploying}
                    onClick={() => deployProfile.mutate(p.name)}
                  >
                    {isDeploying ? 'deploying…' : 'deploy'}
                  </button>
                  <button
                    className="btn-link-danger disabled:opacity-40"
                    disabled={isDeleting}
                    onClick={() => {
                      if (confirm(`delete profile ${p.name}?`)) deleteProfile.mutate(p.name)
                    }}
                  >
                    {isDeleting ? 'deleting…' : 'delete'}
                  </button>
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </section>
  )
}
