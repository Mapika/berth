import { useQuery } from '@tanstack/react-query'
import { api, queryKeys } from '../../api'
import { GpuCard, gpuGridCols } from '../../components/GpuCard'

export default function HealthStrip() {
  const deps = useQuery({ queryKey: queryKeys.deployments, queryFn: api.listDeployments, refetchInterval: 2000 })
  const gpus = useQuery({ queryKey: queryKeys.gpus, queryFn: api.listGpus, refetchInterval: 2000 })
  const models = useQuery({ queryKey: queryKeys.models, queryFn: api.listModels, refetchInterval: 5000 })

  const gpuList = gpus.data ?? []
  const all = deps.data ?? []

  return (
    <section className="space-y-6">
      <div className="label">gpus</div>
      {gpuList.length === 0 ? (
        <div className="text-mute text-[12px]">no gpus reported</div>
      ) : (
        <div className={'grid gap-12 ' + gpuGridCols(gpuList.length)}>
          {gpuList.map(g => (
            <GpuCard key={g.index} g={g} deployments={all} models={models.data ?? []} />
          ))}
        </div>
      )}
    </section>
  )
}
