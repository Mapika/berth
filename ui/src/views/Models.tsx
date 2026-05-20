import { Fragment, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, queryKeys, type Deployment, type LoadDeploymentBody, type Model } from '../api'
import { parseGpuIds, parseMaxModelLen, remoteNodeLabel } from '../launchForm'

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

const sleep = (ms: number) => new Promise(resolve => setTimeout(resolve, ms))

function isDroppedFetch(e: unknown): boolean {
  return e instanceof TypeError && /failed to fetch|network/i.test(e.message)
}

async function recoverStartedDeployment(
  modelId: number,
  beforeMaxId: number,
): Promise<Deployment> {
  for (let i = 0; i < 15; i += 1) {
    const rows = await api.listDeployments()
    const dep = rows
      .filter(d => d.id > beforeMaxId && d.model_id === modelId)
      .sort((a, b) => b.id - a.id)[0]
    if (dep) {
      if (dep.status === 'failed') {
        throw new Error(dep.last_error || 'deployment failed after it started')
      }
      return dep
    }
    await sleep(1000)
  }
  throw new Error('deployment request disconnected before a new deployment appeared')
}

export default function Models() {
  const qc = useQueryClient()
  const models = useQuery({ queryKey: queryKeys.models, queryFn: api.listModels })
  const backends = useQuery({ queryKey: queryKeys.backends, queryFn: api.listBackends })
  const gpus = useQuery({ queryKey: queryKeys.gpus, queryFn: api.listGpus })
  const nodes = useQuery({ queryKey: queryKeys.nodes, queryFn: api.listNodes })
  const config = useQuery({ queryKey: queryKeys.config, queryFn: api.getConfig })
  const [repo, setRepo] = useState('')
  const [name, setName] = useState('')
  const [openLauncher, setOpenLauncher] = useState<string | null>(null)
  const [form, setForm] = useState<LauncherForm>(DEFAULT_FORM)
  const [launchError, setLaunchError] = useState('')
  const leaderOnly = config.data?.values.leader_only === true
  const remoteNodes = (nodes.data?.nodes ?? []).filter(n => n.label !== 'local')
  const readyRemoteNodes = remoteNodes.filter(n => n.status === 'ready')
  const selectedNodeLabel = form.nodeLabel || (leaderOnly ? (readyRemoteNodes[0]?.label ?? '') : '')
  const missingDeployTarget = leaderOnly && !selectedNodeLabel

  const addModel = useMutation({
    mutationFn: () => api.createModel({
      name: name || repo.split('/').pop()!.toLowerCase(),
      hf_repo: repo,
    }),
    onSuccess: () => { setRepo(''); setName(''); qc.invalidateQueries({ queryKey: queryKeys.models }) },
  })
  const delModel = useMutation({
    mutationFn: (modelName: string) => api.deleteModel(modelName),
    onSuccess: () => qc.invalidateQueries({ queryKey: queryKeys.models }),
  })
  const launchModel = useMutation({
    mutationFn: async (m: Model) => {
      const gpuIds = parseGpuIds(form.gpuIds)
      const maxLen = parseMaxModelLen(form.maxModelLen)
      const payload: LoadDeploymentBody = {
        model_name: m.name,
        hf_repo: m.hf_repo,
        gpu_ids: gpuIds,
        max_model_len: maxLen,
        pinned: form.pinned,
      }
      if (form.backend) payload.backend = form.backend
      payload.node_label = remoteNodeLabel(selectedNodeLabel)
      const before = await api.listDeployments()
      const beforeMaxId = before.reduce((max, d) => Math.max(max, d.id), 0)
      try {
        return await api.loadModel(payload)
      } catch (e) {
        if (!isDroppedFetch(e)) throw e
        return recoverStartedDeployment(m.id, beforeMaxId)
      }
    },
    onMutate: () => setLaunchError(''),
    onSuccess: () => {
      setOpenLauncher(null)
      setForm(DEFAULT_FORM)
      qc.invalidateQueries({ queryKey: queryKeys.deployments })
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
            {(models.data ?? []).map(m => {
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
                                value={selectedNodeLabel}
                                onChange={e => setForm(f => ({ ...f, nodeLabel: e.target.value }))}
                              >
                                {!leaderOnly && <option value="">leader (local)</option>}
                                {readyRemoteNodes.map(n => (
                                    <option key={n.id} value={n.label}>
                                      {n.label} · {n.gpu_count} gpu
                                    </option>
                                  ))}
                                {leaderOnly && readyRemoteNodes.length === 0 && (
                                  <option value="" disabled>no ready agents</option>
                                )}
                              </select>
                              {remoteNodes.length === 0 && (
                                <div className="text-mute text-[10px] tracking-wider">
                                  no agents enrolled
                                </div>
                              )}
                              {remoteNodes.length > 0 && readyRemoteNodes.length === 0 && (
                                <div className="text-mute text-[10px] tracking-wider">
                                  no ready agents online
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
                                    available: {(gpus.data ?? []).map(g => g.index).join(', ')}
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
                              disabled={pending || missingDeployTarget}
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
