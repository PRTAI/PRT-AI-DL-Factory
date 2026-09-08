/**
 * 「先选项目」闸门：提交训练/评测这类必须归属项目的页面套在它里面。
 *
 * 和 wandb / swanlab 一样，没选项目就先让用户选，而不是提交时才报错。
 * 项目被删/被归档之后本地缓存的 id 会失效，所以这里也负责把它清掉。
 */
import * as React from 'react'
import { useQuery } from '@tanstack/react-query'
import { FolderKanban } from 'lucide-react'
import { api } from '@/lib/api'
import { useProject } from '@/lib/store'
import { Button, Card, EmptyState, Spinner } from './ui'
import { ProjectSwitcher } from './ProjectSwitcher'

export function ProjectGate({ children }: { children: React.ReactNode }) {
  const { projectId, setProject } = useProject()
  const projects = useQuery({ queryKey: ['projects'], queryFn: () => api.projects() })

  const rows = projects.data
  const current = rows?.find((row) => row.project.id === projectId)

  // 选中的项目已经不存在或被归档 —— 清掉，避免一直提交失败
  React.useEffect(() => {
    if (!projectId || !rows) return
    if (!current || current.project.archived) setProject('')
  }, [projectId, rows, current, setProject])

  if (projects.isLoading) {
    return (
      <div className="flex justify-center py-16">
        <Spinner className="h-6 w-6" />
      </div>
    )
  }

  if (!current || current.project.archived) {
    return (
      <Card className="mx-auto max-w-xl">
        <EmptyState
          icon={<FolderKanban className="h-8 w-8" aria-hidden />}
          title="请先选择一个项目"
          description={
            rows?.length
              ? '训练与评测任务都归属于项目。选择一个项目后即可继续提交。'
              : '还没有任何项目。项目是任务的归属单位，先创建一个再回来提交任务。'
          }
          action={
            rows?.length ? (
              <ProjectSwitcher />
            ) : (
              <Button variant="primary" onClick={() => window.location.assign('/projects')}>
                去创建项目
              </Button>
            )
          }
        />
      </Card>
    )
  }

  return <>{children}</>
}
