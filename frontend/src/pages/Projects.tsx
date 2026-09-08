/**
 * 项目管理：列表 / 搜索 / 创建 / 改名 / 归档 / 删除。
 * 列表是全团队共享的（谁在跑什么应该透明），只有负责人和管理员能改。
 */
import * as React from 'react'
import { useNavigate } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Archive,
  ArchiveRestore,
  FolderKanban,
  Pencil,
  Plus,
  Search,
  Trash2,
  Users,
} from 'lucide-react'
import { toast } from 'sonner'
import { api, ApiError } from '@/lib/api'
import { useAuth, useProject } from '@/lib/store'
import { formatRelative } from '@/lib/utils'
import { PageHeader } from '@/components/common'
import {
  Badge,
  Button,
  Card,
  Dialog,
  EmptyState,
  Field,
  Input,
  LinkButton,
  Skeleton,
  Table,
  Td,
  Th,
  Tr,
  Textarea,
} from '@/components/ui'
import type { Project } from '@/lib/types'

export default function Projects() {
  const { user } = useAuth()
  const { projectId, setProject } = useProject()
  const [keyword, setKeyword] = React.useState('')
  const [showArchived, setShowArchived] = React.useState(false)
  const [creating, setCreating] = React.useState(false)
  const [editing, setEditing] = React.useState<Project | null>(null)
  const navigate = useNavigate()
  const queryClient = useQueryClient()

  const projects = useQuery({
    queryKey: ['projects', keyword, showArchived],
    queryFn: () => api.projects({ q: keyword, include_archived: showArchived }),
  })

  const invalidate = () => queryClient.invalidateQueries({ queryKey: ['projects'] })

  const archive = useMutation({
    mutationFn: ({ id, archived }: { id: string; archived: boolean }) =>
      api.updateProject(id, { archived }),
    onSuccess: (project) => {
      toast.success(project.archived ? `已归档「${project.name}」` : `已恢复「${project.name}」`)
      invalidate()
    },
    onError: (error: ApiError) => toast.error(error.message),
  })

  const remove = useMutation({
    mutationFn: (id: string) => api.deleteProject(id),
    onSuccess: (_, id) => {
      toast.success('项目已删除')
      if (id === projectId) setProject('')
      invalidate()
    },
    onError: (error: ApiError) => toast.error(error.message),
  })

  const rows = projects.data ?? []
  const canManage = (project: Project) => user?.role === 'admin' || project.owner_id === user?.id

  return (
    <div>
      <PageHeader
        title="项目"
        description="训练与评测任务都归属于项目。项目列表对全团队可见，只有负责人和管理员能修改。"
        actions={
          <Button variant="primary" onClick={() => setCreating(true)}>
            <Plus className="h-4 w-4" aria-hidden />
            新建项目
          </Button>
        }
      />

      <div className="mb-3 flex flex-wrap items-center gap-2">
        <div className="relative w-72">
          <Search className="pointer-events-none absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-fg-subtle" aria-hidden />
          <Input
            value={keyword}
            onChange={(event) => setKeyword(event.target.value)}
            placeholder="按名称 / 描述 / 标签 / 负责人搜索"
            className="pl-9"
            aria-label="搜索项目"
          />
        </div>
        <Button
          variant={showArchived ? 'primary' : 'secondary'}
          size="sm"
          onClick={() => setShowArchived((value) => !value)}
        >
          <Archive className="h-3.5 w-3.5" aria-hidden />
          含已归档
        </Button>
      </div>

      <Card>
        {projects.isLoading ? (
          <div className="space-y-2 p-4">
            {[0, 1, 2].map((index) => (
              <Skeleton key={index} className="h-12 w-full" />
            ))}
          </div>
        ) : rows.length === 0 ? (
          <EmptyState
            icon={<FolderKanban className="h-8 w-8" aria-hidden />}
            title={keyword ? '没有匹配的项目' : '还没有项目'}
            description="项目是任务的归属单位，先创建一个项目才能提交训练或评测。"
            action={
              <Button variant="primary" onClick={() => setCreating(true)}>
                <Plus className="h-4 w-4" aria-hidden />
                新建项目
              </Button>
            }
          />
        ) : (
          <Table>
            <thead>
              <tr>
                <Th className="w-[280px]">项目</Th>
                <Th className="w-[180px]">负责人</Th>
                <Th className="w-[110px] text-right">任务数</Th>
                <Th className="w-[150px]">运行 / 待审</Th>
                <Th className="w-[130px]">最近活动</Th>
                <Th>操作</Th>
              </tr>
            </thead>
            <tbody>
              {rows.map(({ project, stats }) => (
                <Tr key={project.id}>
                  <Td>
                    <button
                      type="button"
                      className="block max-w-full text-left"
                      onClick={() => {
                        setProject(project.id)
                        navigate('/jobs')
                      }}
                    >
                      <span className="flex items-center gap-1.5">
                        <span className="truncate text-[13px] font-medium text-primary hover:underline">
                          {project.name}
                        </span>
                        {project.id === projectId && <Badge tone="primary">当前</Badge>}
                        {project.archived && <Badge tone="neutral">已归档</Badge>}
                      </span>
                      {project.description && (
                        <span className="mt-0.5 block truncate text-[11px] text-fg-subtle">
                          {project.description}
                        </span>
                      )}
                    </button>
                  </Td>
                  <Td>
                    <span className="truncate text-xs text-fg-muted">{project.owner_email}</span>
                    {stats.members.length > 1 && (
                      <span className="mt-0.5 flex items-center gap-1 text-[11px] text-fg-subtle">
                        <Users className="h-3 w-3" aria-hidden />
                        {stats.members.length} 人参与
                      </span>
                    )}
                  </Td>
                  <Td className="num text-right text-xs">{stats.total_jobs}</Td>
                  <Td className="text-xs text-fg-muted">
                    {stats.running > 0 ? <span className="text-success">{stats.running} 运行中</span> : '—'}
                    {stats.pending_review > 0 && (
                      <span className="ml-1.5 text-warning">{stats.pending_review} 待审</span>
                    )}
                  </Td>
                  <Td className="text-xs text-fg-subtle">
                    {stats.last_activity_at ? formatRelative(stats.last_activity_at) : '—'}
                  </Td>
                  <Td>
                    <div className="flex items-center gap-3">
                      <LinkButton
                        onClick={() => {
                          setProject(project.id)
                          toast.success(`已切换到项目「${project.name}」`)
                        }}
                        disabled={project.id === projectId || project.archived}
                      >
                        进入
                      </LinkButton>
                      <LinkButton onClick={() => setEditing(project)} disabled={!canManage(project)}>
                        <Pencil className="mr-0.5 inline h-3 w-3" aria-hidden />
                        编辑
                      </LinkButton>
                      <LinkButton
                        onClick={() => archive.mutate({ id: project.id, archived: !project.archived })}
                        disabled={!canManage(project)}
                      >
                        {project.archived ? (
                          <>
                            <ArchiveRestore className="mr-0.5 inline h-3 w-3" aria-hidden />
                            恢复
                          </>
                        ) : (
                          <>
                            <Archive className="mr-0.5 inline h-3 w-3" aria-hidden />
                            归档
                          </>
                        )}
                      </LinkButton>
                      <LinkButton
                        tone="danger"
                        disabled={!canManage(project) || stats.total_jobs > 0}
                        title={stats.total_jobs > 0 ? '项目下还有任务，只能归档' : undefined}
                        onClick={() => {
                          if (window.confirm(`确定删除项目「${project.name}」？`)) remove.mutate(project.id)
                        }}
                      >
                        <Trash2 className="mr-0.5 inline h-3 w-3" aria-hidden />
                        删除
                      </LinkButton>
                    </div>
                  </Td>
                </Tr>
              ))}
            </tbody>
          </Table>
        )}
      </Card>

      <CreateDialog open={creating} onOpenChange={setCreating} onCreated={(id) => setProject(id)} />
      <EditDialog project={editing} onClose={() => setEditing(null)} />
    </div>
  )
}

function CreateDialog({
  open,
  onOpenChange,
  onCreated,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  onCreated: (id: string) => void
}) {
  const [name, setName] = React.useState('')
  const [description, setDescription] = React.useState('')
  const [tags, setTags] = React.useState('')
  const queryClient = useQueryClient()

  const create = useMutation({
    mutationFn: () =>
      api.createProject({
        name: name.trim(),
        description: description.trim(),
        tags: tags
          .split(/[,，\s]+/)
          .map((tag) => tag.trim())
          .filter(Boolean),
      }),
    onSuccess: (project) => {
      toast.success(`已创建项目「${project.name}」，已切换为当前项目`)
      onCreated(project.id)
      setName('')
      setDescription('')
      setTags('')
      onOpenChange(false)
      queryClient.invalidateQueries({ queryKey: ['projects'] })
    },
    onError: (error: ApiError) => toast.error(error.message),
  })

  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      title="新建项目"
      description="项目是任务的归属单位；创建后会自动切换为当前项目。"
      footer={
        <>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            取消
          </Button>
          <Button
            variant="primary"
            loading={create.isPending}
            disabled={!name.trim()}
            onClick={() => create.mutate()}
          >
            创建
          </Button>
        </>
      }
    >
      <div className="space-y-3">
        <Field label="项目名称" required hint="团队内唯一，例如「Qwen3-4B 对齐」">
          <Input value={name} onChange={(event) => setName(event.target.value)} placeholder="项目名称" />
        </Field>
        <Field label="描述" hint="目标、约束、负责的模型等，方便别人理解这个项目在做什么">
          <Textarea
            value={description}
            onChange={(event) => setDescription(event.target.value)}
            placeholder="选填"
          />
        </Field>
        <Field label="标签" hint="逗号或空格分隔，用于搜索">
          <Input value={tags} onChange={(event) => setTags(event.target.value)} placeholder="grpo, qwen3" />
        </Field>
      </div>
    </Dialog>
  )
}

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

  const users = useQuery({
    queryKey: ['users'],
    queryFn: () => api.users(),
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
        <Field label="描述">
          <Textarea value={description} onChange={(event) => setDescription(event.target.value)} />
        </Field>
        <Field label="标签" hint="逗号或空格分隔">
          <Input value={tags} onChange={(event) => setTags(event.target.value)} />
        </Field>
        {user?.role === 'admin' && (
          <Field label="负责人" hint="管理员可以把项目转交给其他成员">
            <select
              value={ownerId}
              onChange={(event) => setOwnerId(event.target.value)}
              className="h-9 w-full rounded-md border border-border bg-card px-3 text-sm text-fg"
            >
              {(users.data ?? []).map((item) => (
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
