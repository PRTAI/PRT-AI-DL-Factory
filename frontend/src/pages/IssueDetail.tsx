/**
 * 问题详情：描述、托管修复（管理员）、Agent 修复报告与轨迹、合并到 main。
 */
import * as React from 'react'
import { Link, useParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { ArrowLeft, Bot, GitMerge, X } from 'lucide-react'
import { toast } from 'sonner'
import { api, ApiError } from '@/lib/api'
import { useAuth } from '@/lib/store'
import { formatDateTime } from '@/lib/utils'
import { InlineHint, Mono, PageHeader, WarnHint } from '@/components/common'
import { AgentTrace } from '@/components/AgentTrace'
import { Button, Card, CardBody, CardHeader, Dialog, ErrorBox, KeyValue, Skeleton } from '@/components/ui'
import { ISSUE_ACTIVE, IssueStatusBadge } from './Issues'

export default function IssueDetail() {
  const { issueId = '' } = useParams()
  const { user } = useAuth()
  const isAdmin = user?.role === 'admin'
  const queryClient = useQueryClient()
  const [confirmMerge, setConfirmMerge] = React.useState(false)

  const issue = useQuery({
    queryKey: ['issue', issueId],
    queryFn: () => api.issue(issueId),
    refetchInterval: (query) => (query.state.data && ISSUE_ACTIVE.includes(query.state.data.status) ? 3000 : false),
  })
  const active = issue.data ? ISSUE_ACTIVE.includes(issue.data.status) : false
  const hasRun = Boolean(issue.data?.fix_started_at)
  const log = useQuery({
    queryKey: ['issue-log', issueId],
    queryFn: () => api.issueAgentLog(issueId),
    enabled: hasRun,
    refetchInterval: active ? 3000 : false,
  })
  const logs = React.useMemo(() => (log.data?.text ? [{ name: 'claude-code', content: log.data.text }] : []), [log.data])

  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: ['issue', issueId] })
    void queryClient.invalidateQueries({ queryKey: ['issue-log', issueId] })
    void queryClient.invalidateQueries({ queryKey: ['issues'] })
  }
  const onError = (error: unknown) => toast.error(error instanceof ApiError ? error.message : '操作失败')
  const fix = useMutation({ mutationFn: () => api.startFix(issueId), onSuccess: () => { toast.success('已交给 Claude Code 修复'); refresh() }, onError })
  const merge = useMutation({
    mutationFn: () => api.mergeIssue(issueId),
    onSuccess: () => { setConfirmMerge(false); toast.success('开始合并，完成后平台会自动重启'); refresh() },
    onError,
  })
  const close = useMutation({ mutationFn: () => api.closeIssue(issueId), onSuccess: refresh, onError })

  if (issue.isLoading) return <Skeleton className="h-64 w-full" />
  if (issue.error || !issue.data) return <ErrorBox message={(issue.error as Error)?.message ?? '问题不存在'} />
  const item = issue.data
  const canFix = isAdmin && ['open', 'failed', 'pending_merge'].includes(item.status)
  const canClose = !['fixing', 'merging', 'merged', 'closed'].includes(item.status)

  return (
    <div className="space-y-4">
      <Link to="/issues" className="inline-flex items-center gap-1 text-xs text-fg-muted hover:text-primary">
        <ArrowLeft className="h-3.5 w-3.5" aria-hidden /> 返回问题列表
      </Link>
      <PageHeader
        title={item.title}
        description={<>{item.reporter_email} · {formatDateTime(item.created_at)} 反馈</>}
        actions={
          <>
            <IssueStatusBadge status={item.status} />
            {canFix && (
              <Button variant={item.status === 'pending_merge' ? 'secondary' : 'primary'} loading={fix.isPending} onClick={() => fix.mutate()}>
                <Bot className="h-4 w-4" aria-hidden />
                {item.status === 'open' ? '托管 Agent 修复' : '重新托管修复'}
              </Button>
            )}
            {isAdmin && item.status === 'pending_merge' && (
              <Button variant="success" onClick={() => setConfirmMerge(true)}>
                <GitMerge className="h-4 w-4" aria-hidden />
                合并到 main
              </Button>
            )}
            {canClose && (
              <Button variant="ghost" loading={close.isPending} onClick={() => close.mutate()}>
                <X className="h-4 w-4" aria-hidden /> 关闭
              </Button>
            )}
          </>
        }
      />

      {item.error && <ErrorBox message={item.error} />}
      {item.status === 'merged' && item.restart_scheduled && (
        <InlineHint>已合并 <Mono>{item.merge_commit.slice(0, 10)}</Mono> 并推送，平台已安排重启；刷新页面即为新版本。</InlineHint>
      )}

      <div className="grid gap-4 lg:grid-cols-3">
        <Card className="lg:col-span-2">
          <CardHeader title="问题描述" />
          <CardBody>
            <p className="whitespace-pre-wrap text-sm leading-relaxed text-fg">{item.description || '（无描述）'}</p>
          </CardBody>
        </Card>
        <Card>
          <CardHeader title="修复信息" />
          <CardBody>
            <KeyValue label="所在页面">{item.page || '—'}</KeyValue>
            <KeyValue label="修复分支">{item.branch ? <Mono>{item.branch}</Mono> : '—'}</KeyValue>
            <KeyValue label="基线提交">{item.base_commit ? <Mono>{item.base_commit.slice(0, 10)}</Mono> : '—'}</KeyValue>
            <KeyValue label="修复提交">{item.head_commit ? <Mono>{item.head_commit.slice(0, 10)}</Mono> : '—'}</KeyValue>
            <KeyValue label="开始修复">{formatDateTime(item.fix_started_at)}</KeyValue>
            <KeyValue label="修复完成">{formatDateTime(item.fix_finished_at)}</KeyValue>
            <KeyValue label="合并时间">{formatDateTime(item.merged_at)}</KeyValue>
            {isAdmin && item.workspace && <KeyValue label="副本目录"><Mono>{item.workspace}</Mono></KeyValue>}
          </CardBody>
        </Card>
      </div>

      {(item.report || active) && (
        <Card>
          <CardHeader title="Agent 修复报告" description={active ? 'Claude Code 正在定位与修复…' : undefined} />
          <CardBody className="space-y-3">
            {item.report ? (
              <div className="prose-sm space-y-2 text-sm leading-relaxed text-fg [&_code]:rounded [&_code]:bg-muted [&_code]:px-1 [&_h2]:mt-3 [&_h2]:font-semibold [&_li]:ml-4 [&_ul]:list-disc">
                <ReactMarkdown remarkPlugins={[remarkGfm]}>{item.report}</ReactMarkdown>
              </div>
            ) : (
              <Skeleton className="h-20 w-full" />
            )}
            {item.diff_stat && (
              <pre className="num overflow-x-auto rounded-md bg-muted p-3 text-[11px] text-fg-muted">{item.diff_stat}</pre>
            )}
          </CardBody>
        </Card>
      )}

      {hasRun && (
        <Card>
          <CardHeader title="Agent 执行轨迹" />
          <CardBody>
            {logs.length ? <AgentTrace logs={logs} live={active} /> : <p className="text-xs text-fg-muted">暂无输出</p>}
          </CardBody>
        </Card>
      )}

      <Dialog
        open={confirmMerge}
        onOpenChange={setConfirmMerge}
        title="合并到 main 并重启平台？"
        footer={
          <>
            <Button variant="ghost" onClick={() => setConfirmMerge(false)}>取消</Button>
            <Button variant="success" loading={merge.isPending} onClick={() => merge.mutate()}>确认合并</Button>
          </>
        }
      >
        <div className="space-y-2 text-sm text-fg">
          <p>将把 <Mono>{item.branch}</Mono> 合并进平台目录的 main，推送到远端，有前端改动时重新构建，然后重启本地平台。</p>
          <WarnHint>推送后其他人会拉到这些改动；重启期间页面会短暂不可用，运行中的训练/评测子进程不受影响。请先确认上面的报告和改动。</WarnHint>
        </div>
      </Dialog>
    </div>
  )
}
