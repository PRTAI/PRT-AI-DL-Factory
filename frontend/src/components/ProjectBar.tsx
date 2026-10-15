/**
 * 提交页顶部的「提交到哪个项目」横条。
 *
 * 提交训练/评测时，项目是和任务名一样重要的归属信息，不该只在右侧摘要里
 * 出现一行小字——尤其是同时推几条实验线的时候，很容易提交到上一次选的项目里。
 */
import { useQuery } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { FolderKanban, Repeat2 } from 'lucide-react'
import { api } from '@/lib/api'
import { useProject } from '@/lib/store'
import { Button } from './ui'

export function ProjectBar({ hint }: { hint?: React.ReactNode }) {
  const { projectId, clearProject } = useProject()
  const navigate = useNavigate()
  const project = useQuery({
    queryKey: ['project', projectId],
    queryFn: () => api.project(projectId),
    enabled: Boolean(projectId),
    retry: false,
  })

  const name = project.data?.project.name
  const stats = project.data?.stats

  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-lg border border-primary/30 bg-primary-soft px-3 py-1.5">
      <FolderKanban className="h-4 w-4 shrink-0 text-primary" aria-hidden />
      <p className="min-w-0 truncate text-sm">
        <span className="text-xs text-primary/80">提交到项目</span>
        <span className="ml-2 font-semibold text-fg">{name ?? projectId ?? '未选择'}</span>
      </p>
      {stats && (
        <p className="hidden text-[11px] text-fg-muted sm:block">
          已有 {stats.total_jobs} 个任务
          {stats.training_jobs > 0 && ` · 训练 ${stats.training_jobs}`}
          {stats.verify_jobs > 0 && ` · 评测 ${stats.verify_jobs}`}
          {stats.running > 0 && ` · ${stats.running} 运行中`}
        </p>
      )}
      {hint && <p className="text-[11px] text-fg-muted">{hint}</p>}
      <div className="ml-auto flex shrink-0 items-center gap-1">
        <Button variant="ghost" size="sm" className="h-7" onClick={() => navigate('/projects')}>
          项目详情
        </Button>
        <Button variant="secondary" size="sm" className="h-7" onClick={clearProject}>
          <Repeat2 className="h-3.5 w-3.5" aria-hidden />
          换项目
        </Button>
      </div>
    </div>
  )
}
