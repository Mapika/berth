import { Fragment, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../api'

type LauncherForm = {
  backend: string  // '' = let the server pick
  maxModelLen: string
  gpuIds: string   // comma-separated, e.g. "0" or "0,1"
  pinned: boolean
  nodeLabel: string  // '' or 'local' = leader; else remote agent label
}

const DEFAULT_FORM: LauncherForm = {
  backend: '',
  maxModelLen: '4096',
  gpuIds: '0',
  pinned: false,
  nodeLabel: '',
}

export default function Models() {
  const qc = useQueryClient()
  const models = useQuery({ queryKey: ['models'], queryFn: api.listModels })
  const backends = useQuery({ queryKey: ['backends'], queryFn: api.listBackends })
  const gpus = useQuery({ queryKey: ['gpus'], queryFn: api.listGpus })
  const nodes = useQuery({ queryKey: ['nodes'], queryFn: api.listNodes })
  const [repo, setRepo] = useState('')
  const [name, setName] = useState('')
  const [openLauncher, setOpenLauncher] = useState<string | null>(null)
  const [form, setForm] = useState<LauncherForm>(DEFAULT_FORM)
  const [launchError, setLaunchError] = useState('')

  const addModel = useMutation({
    mutationFn: () => api.createModel({
      name: name || repo.split('/').pop()!.toLowerCase(),
      hf_repo: repo,
    }),
    onSuccess: () => { setRepo(''); setName(''); qc.invalidateQueries({ queryKey: ['models'] }) },
  })
  const delModel = useMutation({
    mutationFn: (modelName: string) => api.deleteModel(modelName),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['models'] }),
  })
  const launchModel = useMutation({
    mutationFn: (m: any) => {
      const gpuIds = form.gpuIds
        .split(',')
        .map(s => s.trim())
        .filter(s => s.length > 0)
        .map(s => Number(s))
        .filter(n => Number.isInteger(n) && n >= 0)
      if (gpuIds.length === 0) throw new Error('gpu_ids: provide at least one valid GPU index')
      const maxLen = Number(form.maxModelLen)
      if (!Number.isInteger(maxLen) || maxLen < 128) {
        throw new Error('max_model_len: must be an integer >= 128')
      }
      const payload: Record<string, unknown> = {
        model_name: m.name,
        hf_repo: m.hf_repo,
        gpu_ids: gpuIds,
        max_model_len: maxLen,
        pinned: form.pinned,
      }
      if (form.backend) payload.backend = form.backend
      if (form.nodeLabel && form.nodeLabel !== 'local') {
        payload.node_label = form.nodeLabel
      }
      return api.loadModel(payload)
    },
    onMutate: () => setLaunchError(''),
    onSuccess: () => {
      setOpenLauncher(null)
      setForm(DEFAULT_FORM)
      qc.invalidateQueries({ queryKey: ['deps'] })
    },
    onError: (e: Error) => setLaunchError(e.message),
  })

  return (
    <div className="space-y-14">
      <header className="flex items-baseline justify-between">
        <h2 className="text-2xl font-light tracking-tightish caret">models</h2>
        <div className="label">{(models.data ?? []).length} registered</div>
      </header>

      <section className="space-y-5">
        <div className="label">register</div>
        <div className="grid grid-cols-[1fr_220px_auto] gap-3 max-w-3xl">
          <input
            className="field font-mono"
            placeholder="huggingface repo (e.g. Qwen/Qwen3.6-35B-A3B-FP8)"
            value={repo}
            onChange={e => setRepo(e.target.value)}
          />
          <input
            className="field font-mono"
            placeholder="local alias (optional)"
            value={name}
            onChange={e => setName(e.target.value)}
          />
          <button
            className="btn-primary"
            disabled={!repo.trim() || addModel.isPending}
            onClick={() => addModel.mutate()}
          >
            {addModel.isPending ? 'registering...' : 'register'}
          </button>
        </div>
        {addModel.error && (
          <div className="text-err text-[12px]">{(addModel.error as Error).message}</div>
        )}
      </section>

      <section className="space-y-4">
        <div className="label">registry</div>
        <table className="ditable">
          <thead>
            <tr>
              <th>name</th>
              <th>huggingface</th>
              <th>revision</th>
              <th className="text-right"></th>
            </tr>
          </thead>
          <tbody>
            {(models.data ?? []).length === 0 && (
              <tr>
                <td colSpan={4} className="!py-12 text-center text-mute">
                  no models registered yet
                </td>
              </tr>
            )}
            {(models.data ?? []).map((m: any) => {
              const isOpen = openLauncher === m.name
              const pending = launchModel.isPending && openLauncher === m.name
              return (
                <Fragment key={m.id}>
                  <tr>
                    <td>{m.name}</td>
                    <td className="text-dim">{m.hf_repo}</td>
                    <td className="text-mute">{m.revision}</td>
                    <td className="text-right space-x-6">
                      <button
                        className="text-accent hover:opacity-70 transition-opacity"
                        onClick={() => {
                          if (isOpen) {
                            setOpenLauncher(null)
                          } else {
                            setOpenLauncher(m.name)
                            setForm(DEFAULT_FORM)
                            setLaunchError('')
                          }
                        }}
                      >
                        {isOpen ? 'cancel' : 'load'}
                      </button>
                      <button
                        className="btn-link-danger"
                        onClick={() => delModel.mutate(m.name)}
                      >
                        delete
                      </button>
                    </td>
                  </tr>
                  {isOpen && (
                    <tr>
                      <td colSpan={4} className="!pt-2 !pb-6">
                        <div className="bg-elev/40 border border-rule p-5 space-y-4">
                          <div className="grid grid-cols-2 md:grid-cols-5 gap-4">
                            <div className="space-y-1">
                              <div className="label">backend</div>
                              <select
                                className="field font-mono w-full text-[12px]"
                                value={form.backend}
                                onChange={e => setForm(f => ({ ...f, backend: e.target.value }))}
                              >
                                <option value="">auto (server picks)</option>
                                {(backends.data ?? []).map(b => (
                                  <option key={b.name} value={b.name}>{b.name}</option>
                                ))}
                              </select>
                            </div>
                            <div className="space-y-1">
                              <div className="label">node</div>
                              <select
                                className="field font-mono w-full text-[12px]"
                                value={form.nodeLabel}
                                onChange={e => setForm(f => ({ ...f, nodeLabel: e.target.value }))}
                              >
                                <option value="">leader (local)</option>
                                {(nodes.data?.nodes ?? [])
                                  .filter(n => n.label !== 'local' && n.status === 'ready')
                                  .map(n => (
                                    <option key={n.id} value={n.label}>
                                      {n.label} · {n.gpu_count} gpu
                                    </option>
                                  ))}
                              </select>
                              {(nodes.data?.nodes ?? []).filter(n => n.label !== 'local').length === 0 && (
                                <div className="text-mute text-[10px] tracking-wider">
                                  no agents enrolled
                                </div>
                              )}
                            </div>
                            <div className="space-y-1">
                              <div className="label">max model len</div>
                              <input
                                className="field font-mono w-full text-[12px]"
                                value={form.maxModelLen}
                                onChange={e => setForm(f => ({ ...f, maxModelLen: e.target.value }))}
                                placeholder="4096"
                              />
                            </div>
                            <div className="space-y-1">
                              <div className="label">gpu ids</div>
                              <input
                                className="field font-mono w-full text-[12px]"
                                value={form.gpuIds}
                                onChange={e => setForm(f => ({ ...f, gpuIds: e.target.value }))}
                                placeholder="0 or 0,1"
                              />
                              {form.nodeLabel && form.nodeLabel !== 'local' ? (
                                <div className="text-mute text-[10px] tracking-wider">
                                  on agent {form.nodeLabel}
                                </div>
                              ) : (
                                (gpus.data ?? []).length > 0 && (
                                  <div className="text-mute text-[10px] tracking-wider">
                                    available: {(gpus.data ?? []).map((g: any) => g.index).join(', ')}
                                  </div>
                                )
                              )}
                            </div>
                            <div className="space-y-1">
                              <div className="label">options</div>
                              <label className="text-[12px] text-dim flex items-center gap-2 select-none cursor-pointer pt-1">
                                <input
                                  type="checkbox"
                                  className="accent-accent"
                                  checked={form.pinned}
                                  onChange={e => setForm(f => ({ ...f, pinned: e.target.checked }))}
                                />
                                pin (idle reaper skips it)
                              </label>
                            </div>
                          </div>
                          {launchError && (
                            <div className="text-err text-[11px] tracking-wider">{launchError}</div>
                          )}
                          <div className="flex items-center gap-3">
                            <button
                              className="btn-primary"
                              disabled={pending}
                              onClick={() => launchModel.mutate(m)}
                            >
                              {pending ? 'launching...' : 'deploy'}
                            </button>
                            <button
                              className="btn"
                              disabled={pending}
                              onClick={() => setOpenLauncher(null)}
                            >
                              cancel
                            </button>
                          </div>
                        </div>
                      </td>
                    </tr>
                  )}
                </Fragment>
              )
            })}
          </tbody>
        </table>
      </section>
    </div>
  )
}
