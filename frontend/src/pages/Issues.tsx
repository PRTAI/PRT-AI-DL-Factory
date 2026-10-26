/**
 * 问题反馈：成员提交、查看自己的反馈；管理员可交给本机 Claude Code 托管修复并合并到 main。
 */
import * as React from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { MessageSquareWarning, Plus } from 'lucide-react'
import { toast } from 'sonner'
import { api, ApiError } from '@/lib/api'
import { useAuth } from '@/lib/store'
import { formatRelative } from '@/lib/utils'
import { PageHeader } from '@/components/common'
import {
  Badge,
  Button,
  Card,
  Dialog,
  EmptyState,
  ErrorBox,
  Field,
  Input,
  Select,
  Skeleton,
  Table,
  Td,
  Textarea,
  Th,
  Tr,
} from '@/components/ui'
import type { IssueStatus } from '@/lib/types'

type Tone = 'neutral' | 'primary' | 'success' | 'warning' | 'danger' | 'info'

export const ISSUE_STATUS: Record<IssueStatus, { label: string; tone: Tone }> = {
  open: { label: '待处理', tone: 'neutral' },
  fixing: { label: 'Agent 修复中', tone: 'info' },
  pending_merge: { label: '申请合并到 main', tone: 'warning' },
  merging: { label: '合并中', tone: 'info' },
  merged: { label: '已合并', tone: 'success' },
  failed: { label: '修复失败', tone: 'danger' },
  closed: { label: '已关闭', tone: 'neutral' },
}

export const ISSUE_ACTIVE: IssueStatus[] = ['fixing', 'merging']

export function IssueStatusBadge({ status }: { status: IssueStatus }) {
  const meta = ISSUE_STATUS[status]
  return <Badge tone={meta.tone}>{meta.label}</Badge>
}

export function NewIssueDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [title, setTitle] = React.useState('')
  const [description, setDescription] = React.useState('')
  const [page, setPage] = React.useState('')
  React.useEffect(() => {
    if (open) setPage(window.location.pathname === '/issues' ? '' : window.location.pathname)
  }, [open])
  const create = useMutation({
    mutationFn: () => api.createIssue({ title: title.trim(), description: description.trim(), page: page.trim() }),
    onSuccess: (issue) => {
      toast.success('已提交问题反馈')
      setTitle('')
      setDescription('')
      onOpenChange(false)
      void queryClient.invalidateQueries({ queryKey: ['issues'] })
      navigate(`/issues/${issue.id}`)
    },
    onError: (error) => toast.error(error instanceof ApiError ? error.message : '提交失败'),
  })
  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      title="反馈问题"
      description="描述越具体（页面、操作步骤、报错原文），Agent 越容易定位"
      footer={
        <>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>取消</Button>
          <Button variant="primary" loading={create.isPending} disabled={!title.trim()} onClick={() => create.mutate()}>
            提交
          </Button>
        </>
      }
    >
      <div className="space-y-3">
        <Field label="标题" required htmlFor="issue-title">
          <Input id="issue-title" value={title} maxLength={200} onChange={(e) => setTitle(e.target.value)} placeholder="例如：单条 trial 重新评分报错" />
        </Field>
        <Field label="所在页面" htmlFor="issue-page" hint="可选，例如 /jobs/job_xxx">
          <Input id="issue-page" value={page} maxLength={500} onChange={(e) => setPage(e.target.value)} />
        </Field>
        <Field label="问题描述" htmlFor="issue-desc">
          <Textarea
            id="issue-desc"
            rows={8}
            value={description}
            maxLength={20000}
            onChange={(e) => setDescription(e.target.value)}
            placeholder={'操作步骤：\n期望结果：\n实际结果 / 报错原文：'}
          />
        </Field>
      </div>
    </Dialog>
  )
}

export default function Issues() {
  const { user } = useAuth()
  const [status, setStatus] = React.useState('all')
  const [open, setOpen] = React.useState(false)
  const issues = useQuery({
    queryKey: ['issues', status],
    queryFn: () => api.issues(status === 'all' ? '' : status),
    refetchInterval: (query) => (query.state.data?.some((i) => ISSUE_ACTIVE.includes(i.status)) ? 3000 : 15000),
  })
  const options = [
    { value: 'all', label: '全部状态' },
    ...Object.entries(ISSUE_STATUS).map(([value, meta]) => ({ value, label: meta.label })),
  ]
  return (
    <div>
      <PageHeader
        title="问题反馈"
        description={user?.role === 'admin' ? '管理员可托管 Agent 在隔离副本中修复，确认后合并到 main 并重启平台' : '提交平台使用中遇到的问题，管理员处理后会通知你'}
        actions={
          <>
            <Select value={status} onValueChange={setStatus} options={options} className="w-40" />
            <Button variant="primary" onClick={() => setOpen(true)}>
              <Plus className="h-4 w-4" aria-hidden />
              反馈问题
            </Button>
          </>
        }
      />
      <Card>
        {issues.isLoading ? (
          <div className="space-y-2 p-4">
            <Skeleton className="h-8 w-full" />
            <Skeleton className="h-8 w-full" />
          </div>
        ) : issues.error ? (
          <ErrorBox className="m-4" message={(issues.error as Error).message} />
        ) : !issues.data?.length ? (
          <EmptyState icon={<MessageSquareWarning className="h-8 w-8" aria-hidden />} title="还没有问题反馈" />
        ) : (
          <Table>
            <thead>
              <tr>
                <Th>标题</Th>
                <Th>状态</Th>
                <Th>反馈人</Th>
                <Th>分支</Th>
                <Th>更新</Th>
              </tr>
            </thead>
            <tbody>
              {issues.data.map((issue) => (
                <Tr key={issue.id}>
                  <Td>
                    <Link to={`/issues/${issue.id}`} className="font-medium text-fg hover:text-primary">{issue.title}</Link>
                  </Td>
                  <Td><IssueStatusBadge status={issue.status} /></Td>
                  <Td className="text-xs text-fg-muted">{issue.reporter_email}</Td>
                  <Td className="num text-xs text-fg-muted">{issue.branch || '—'}</Td>
                  <Td className="text-xs text-fg-muted">{formatRelative(issue.updated_at)}</Td>
                </Tr>
              ))}
            </tbody>
          </Table>
        )}
      </Card>
      <NewIssueDialog open={open} onOpenChange={setOpen} />
    </div>
  )
}
