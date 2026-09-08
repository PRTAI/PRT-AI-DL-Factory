/**
 * 「先选项目」闸门：提交训练/评测这类必须归属项目的页面套在它里面。
 *
 * 和 wandb / swanlab 一样，没选项目就先让用户选，而不是提交时才报错。
 * 项目被删/被归档之后本地缓存的 id 会失效，所以这里也负责把它清掉。
 */
import * as React from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { useProject } from '@/lib/store'
import { ProjectPicker } from './ProjectPicker'
import { Spinner } from './ui'

export function ProjectGate({ children }: { children: React.ReactNode }) {
  const { projectId, setProject } = useProject()
  const projects = useQuery({ queryKey: ['projects', '', '', 'all'], queryFn: () => api.projects() })

  const rows = projects.data
  const current = rows?.find((row) => row.project.id === projectId)

  // 选中的项目已经不存在或被归档 —— 清掉，避免一直提交失败
  React.useEffect(() => {
    if (!projectId || !rows) return
    if (!current || current.project.archived) setProject('')
  }, [projectId, rows, current, setProject])

  if (projects.isLoading) {
    return (
      <div className="flex justify-center py-24">
        <Spinner className="h-6 w-6" />
      </div>
    )
  }

  if (!current || current.project.archived) {
    return (
      <ProjectPicker
        centered
        title="先选择一个项目"
        description="训练与评测任务都归属于项目。项目和任务在团队内共享——可以直接进入别人的项目，看他们跑了什么。"
      />
    )
  }

  return <>{children}</>
}
