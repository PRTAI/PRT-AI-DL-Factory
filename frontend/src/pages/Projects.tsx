/**
 * 项目管理页。
 *
 * 浏览 / 搜索 / 选择用的是和提交页闸门同一个 `ProjectPicker`——
 * 同一份东西不该有两种长相。这里只多给负责人与管理员一组卡内操作，
 * 以及改名 / 转交负责人的弹窗。
 */
import * as React from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Archive, ArchiveRestore } from 'lucide-react'
import { toast } from 'sonner'
import { api, ApiError } from '@/lib/api'
import { useAuth } from '@/lib/store'
import { formatRelative } from '@/lib/utils'
import { ProjectPicker } from '@/components/ProjectPicker'
import { Button, Dialog, Field, Input, Table, Td, Textarea, Th, Tr } from '@/components/ui'
import type { Project } from '@/lib/types'

export default function Projects() {
  const [editing, setEditing] = React.useState<Project | null>(null)
  const [showArchived, setShowArchived] = React.useState(false)

  return (
    <div>
      <ProjectPicker
        manage
        title="项目"
        description="任务的归属单位。列表与搜索对全团队可见——搜索会同时匹配项目名和项目里的任务名、负责人；只有负责人和管理员可以修改。"
        onEdit={setEditing}
      />

      <div className="mt-6">
        <button
          type="button"
          className="link text-xs"
          onClick={() => setShowArchived((value) => !value)}
        >
          {showArchived ? '收起已归档项目' : '查看已归档项目'}
        </button>
        {showArchived && <ArchivedTable />}
      </div>

      <EditDialog project={editing} onClose={() => setEditing(null)} />
    </div>
  )
}

/* -------------------------------------------------------------------------- */
/* 已归档：不占卡片位，用一张紧凑的表列出来就够了                                */
/* -------------------------------------------------------------------------- */
function ArchivedTable() {
  const { user } = useAuth()
  const queryClient = useQueryClient()
  const projects = useQuery({
    queryKey: ['projects', 'archived'],
    queryFn: () => api.projects({ include_archived: true }),
    select: (rows) => rows.filter((row) => row.project.archived),
  })

  const restore = useMutation({
    mutationFn: (id: string) => api.updateProject(id, { archived: false }),
    onSuccess: (project) => {
      toast.success(`已恢复「${project.name}」`)
      queryClient.invalidateQueries({ queryKey: ['projects'] })
    },
    onError: (error: ApiError) => toast.error(error.message),
  })

  const rows = projects.data ?? []
  if (!rows.length) {
    return <p className="mt-2 text-xs text-fg-subtle">没有已归档的项目。</p>
  }

  return (
    <div className="mt-2 overflow-hidden rounded-xl border border-border bg-card">
      <Table>
        <thead>
          <tr>
            <Th>
              <span className="flex items-center gap-1.5">
                <Archive className="h-3.5 w-3.5" aria-hidden />
                已归档项目
              </span>
            </Th>
            <Th className="w-[180px]">负责人</Th>
            <Th className="w-[100px] text-right">任务数</Th>
            <Th className="w-[130px]">最近活动</Th>
            <Th className="w-[90px]">操作</Th>
          </tr>
        </thead>
        <tbody>
          {rows.map(({ project, stats }) => (
            <Tr key={project.id}>
              <Td className="text-[13px]">{project.name}</Td>
              <Td className="truncate text-xs text-fg-muted">{project.owner_email}</Td>
              <Td className="num text-right text-xs">{stats.total_jobs}</Td>
              <Td className="text-xs text-fg-subtle">
                {stats.last_activity_at ? formatRelative(stats.last_activity_at) : '—'}
              </Td>
              <Td>
                <Button
                  variant="ghost"
                  size="sm"
                  disabled={user?.role !== 'admin' && project.owner_id !== user?.id}
                  onClick={() => restore.mutate(project.id)}
                >
                  <ArchiveRestore className="h-3.5 w-3.5" aria-hidden />
                  恢复
                </Button>
              </Td>
            </Tr>
          ))}
        </tbody>
      </Table>
    </div>
  )
}

/* -------------------------------------------------------------------------- */
function EditDialog({ project, onClose }: { project: Project | null; onClose: () => void }) {
  const [name, setName] = React.useState('')
  const [description, setDescription] = React.useState('')
  const [tags, setTags] = React.useState('')
  const [ownerId, setOwnerId] = React.useState('')
  const { user } = useAuth()
  const queryClient = useQueryClient()

  React.useEffect(() => {
    if (!project) return
    setName(project.name)
    setDescription(project.description)
    setTags(project.tags.join(', '))
    setOwnerId(project.owner_id)
  }, [project])

  const members = useQuery({
    queryKey: ['team-members'],
    queryFn: () => api.teamMembers(),
    enabled: Boolean(project) && user?.role === 'admin',
  })

  const save = useMutation({
    mutationFn: () =>
      api.updateProject(project!.id, {
        name: name.trim(),
        description: description.trim(),
        tags: tags
          .split(/[,，\s]+/)
          .map((tag) => tag.trim())
          .filter(Boolean),
        ...(ownerId && ownerId !== project!.owner_id ? { owner_id: ownerId } : {}),
      }),
    onSuccess: () => {
      toast.success('项目已更新')
      onClose()
      queryClient.invalidateQueries({ queryKey: ['projects'] })
    },
    onError: (error: ApiError) => toast.error(error.message),
  })

  return (
    <Dialog
      open={Boolean(project)}
      onOpenChange={(open) => !open && onClose()}
      title="编辑项目"
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            取消
          </Button>
          <Button variant="primary" loading={save.isPending} disabled={!name.trim()} onClick={() => save.mutate()}>
            保存
          </Button>
        </>
      }
    >
      <div className="space-y-3">
        <Field label="项目名称" required>
          <Input value={name} onChange={(event) => setName(event.target.value)} />
        </Field>
        <Field label="描述" hint="目标、约束、负责的模型——方便别人理解这个项目在做什么">
          <Textarea value={description} onChange={(event) => setDescription(event.target.value)} />
        </Field>
        <Field label="标签" hint="逗号或空格分隔，用于搜索">
          <Input value={tags} onChange={(event) => setTags(event.target.value)} />
        </Field>
        {user?.role === 'admin' && (
          <Field label="负责人" hint="管理员可以把项目转交给其他成员">
            <select
              value={ownerId}
              onChange={(event) => setOwnerId(event.target.value)}
              className="h-9 w-full rounded-md border border-border bg-card px-3 text-sm text-fg"
            >
              {(members.data ?? []).map((item) => (
                <option key={item.id} value={item.id}>
                  {item.display_name}（{item.email}）
                </option>
              ))}
            </select>
          </Field>
        )}
      </div>
    </Dialog>
  )
}
