import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { Button, Card, CardBody, CardHeader } from '@/components/ui'

export function HarborImagePanel() {
  const client = useQueryClient()
  const image = useQuery({ queryKey: ['harbor-image'], queryFn: api.harborImage, refetchInterval: (q) => q.state.data?.status === 'running' ? 2000 : false })
  const recipe = useQuery({ queryKey: ['harbor-image-recipe'], queryFn: api.harborImageRecipe })
  const build = useMutation({ mutationFn: api.buildHarborImage, onSuccess: () => client.invalidateQueries({ queryKey: ['harbor-image'] }) })
  const data = image.data
  return <Card className="min-w-0">
    <CardHeader title="平台 Harbor 评测镜像" description="预装公共依赖：Claude Code 2.1.114、Codex 0.155.1、rewardkit 0.1.7。题目文件在评测时单独加入。" />
    <CardBody className="min-w-0 space-y-4">
      <div className="break-all font-mono text-sm">{data?.image ?? 'prt-ai-factory-harbor-eval-base-image:latest'}</div>
      <p>{data?.status === 'running' ? '构建中' : data?.ready ? '镜像已就绪' : '镜像未就绪'}{data?.status === 'failed' ? '（最近构建失败）' : ''}</p>
      {data?.image_id && <p className="break-all text-xs text-fg-muted">{data.image_id}</p>}
      <div className="flex gap-2">
        <Button onClick={() => image.refetch()}>刷新状态</Button>
        <Button variant="primary" disabled={data?.status === 'running' || build.isPending} onClick={() => build.mutate()}>{data?.ready ? '重新构建' : '构建镜像'}</Button>
      </div>
      {(image.error || build.error || data?.error) && <p className="break-all text-sm text-danger">{image.error?.message || build.error?.message || data?.error}</p>}
      <details><summary className="cursor-pointer text-sm">查看构建 Dockerfile</summary><pre className="mt-2 max-h-96 w-full min-w-0 overflow-auto whitespace-pre-wrap break-all text-xs">{recipe.data?.dockerfile ?? recipe.error?.message}</pre></details>
      {data?.log && <pre className="max-h-96 w-full min-w-0 overflow-auto whitespace-pre-wrap break-all rounded bg-muted p-3 text-xs">{data.log}</pre>}
    </CardBody>
  </Card>
}
