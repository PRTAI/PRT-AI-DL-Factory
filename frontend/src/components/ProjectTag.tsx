/**
 * 任务归属项目的统一显示。
 *
 * 每个任务都属于一个项目，所以「任务出现的地方就该看到它的项目」——
 * 任务列表、任务详情、数据大屏、部署列表都用这一个组件，
 * 免得各处自己拼一遍导致有的地方显示、有的地方不显示。
 *
 * 名字从 react-query 里已缓存的项目列表解析（顶栏切换器一直挂着，所以基本总有缓存），
 * 不把 project_name 冗余进 Job —— 否则改个项目名，历史任务上显示的就都是旧名字。
 */
import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { FolderKanban } from 'lucide-react'
import { api } from '@/lib/api'
import { cn } from '@/lib/utils'

/** 项目 id -> 名称。所有调用共用同一份缓存。 */
export function useProjectNames(): Map<string, string> {
  const projects = useQuery({
    queryKey: ['projects', '', '', 'all'],
    queryFn: () => api.projects({ include_archived: true }),
    staleTime: 30_000,
  })
  return new Map((projects.data ?? []).map((row) => [row.project.id, row.project.name]))
}

export function ProjectTag({
  projectId,
  className,
  showIcon = true,
  /** 点进去看这个项目下的全部任务；纯展示的场合可以关掉 */
  linked = true,
}: {
  projectId: string
  className?: string
  showIcon?: boolean
  linked?: boolean
}) {
  const names = useProjectNames()
  const name = names.get(projectId)

  if (!projectId) {
    // 「项目」上线前的历史任务理论上已经被迁移进默认项目了；
    // 真出现空值时明说，而不是渲染一个空白单元格。
    return <span className={cn('text-[11px] text-fg-subtle', className)}>未归属项目</span>
  }

  const body = (
    <>
      {showIcon && <FolderKanban className="h-3 w-3 shrink-0" aria-hidden />}
      <span className="truncate">{name ?? projectId}</span>
    </>
  )

  if (!linked) {
    return (
      <span className={cn('inline-flex min-w-0 items-center gap-1 text-[11px] text-fg-muted', className)}>
        {body}
      </span>
    )
  }

  return (
    <Link
      to={`/jobs?project=${projectId}`}
      title={`查看项目「${name ?? projectId}」下的全部任务`}
      className={cn(
        'inline-flex min-w-0 items-center gap-1 text-[11px] text-fg-muted transition-colors hover:text-primary',
        className,
      )}
    >
      {body}
    </Link>
  )
}
