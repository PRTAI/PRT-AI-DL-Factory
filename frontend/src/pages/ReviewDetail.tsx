/**
 * 样例审核详情：阶段进度、拆出的质检项（含被丢弃的项和原因）、逐题结论。
 *
 * 逐题结论是「一个质检项对一道题」的粒度，可能上万条，所以后端只存在磁盘上，
 * 这里拉一次全量再在前端按题聚合——比开一堆分页接口简单。
 */
import * as React from 'react'
import { Link, useParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowLeft, FileDown, Package, Square } from 'lucide-react'
import { toast } from 'sonner'
import { api, ApiError } from '@/lib/api'
import { useAuth } from '@/lib/store'
import { cn, formatBytes, formatDateTime, formatPercent } from '@/lib/utils'
import { Mono, PageHeader, InlineHint } from '@/components/common'
import { ProjectTag } from '@/components/ProjectTag'
import {
  Badge,
  Button,
  Card,
  CardBody,
  CardHeader,
  EmptyState,
  ErrorBox,
  KeyValue,
  Skeleton,
  Stat,
  StatusDot,
  Table,
  Td,
  Th,
  Tr,
} from '@/components/ui'
import type { ReviewCheck, ReviewFinding, ReviewStage, ReviewVerdict } from '@/lib/types'
import { REVIEW_AGENTS, REVIEW_STAGE, REVIEW_STATUS } from './Review'

const STAGES: ReviewStage[] = ['collecting', 'planning', 'checking', 'reporting', 'done']

const VERDICT: Record<ReviewVerdict, { label: string; tone: 'success' | 'danger' | 'warning' }> = {
  pass: { label: '通过', tone: 'success' },
  fail: { label: '不合格', tone: 'danger' },
  needs_review: { label: '需人工复核', tone: 'warning' },
}

const CHECK_STATUS: Record<ReviewCheck['status'], { label: string; tone: 'neutral' | 'info' | 'success' | 'danger' | 'warning' }> = {
  pending: { label: '待判定', tone: 'neutral' },
  running: { label: '判定中', tone: 'info' },
  done: { label: '已完成', tone: 'success' },
  failed: { label: '出错', tone: 'danger' },
  dropped: { label: '已丢弃', tone: 'warning' },
}

/** 逐题聚合：任一项不合格 → 不合格；否则有待复核 → 待复核；都没有才是通过。 */
interface TaskRow {
  task_id: string
  rel_path: string
  verdict: ReviewVerdict
  failed: ReviewFinding[]
  pending: ReviewFinding[]
}

function rollup(findings: ReviewFinding[]): TaskRow[] {
  const rows = new Map<string, TaskRow>()
  for (const finding of findings) {
    let row = rows.get(finding.task_id)
    if (!row) {
      row = { task_id: finding.task_id, rel_path: finding.rel_path, verdict: 'pass', failed: [], pending: [] }
      rows.set(finding.task_id, row)
    }
    if (!row.rel_path) row.rel_path = finding.rel_path
    if (finding.verdict === 'fail') row.failed.push(finding)
    else if (finding.verdict === 'needs_review') row.pending.push(finding)
  }
  for (const row of rows.values()) {
    row.verdict = row.failed.length > 0 ? 'fail' : row.pending.length > 0 ? 'needs_review' : 'pass'
  }
  return [...rows.values()].sort((a, b) => a.task_id.localeCompare(b.task_id))
}

export default function ReviewDetail() {
  const { reviewId = '' } = useParams()
  const { user } = useAuth()
  const queryClient = useQueryClient()
  const [filter, setFilter] = React.useState<ReviewVerdict | 'all'>('all')

  const review = useQuery({
    queryKey: ['review', reviewId],
    queryFn: () => api.review(reviewId),
    refetchInterval: (query) => (query.state.data && !isTerminal(query.state.data.status) ? 3000 : false),
  })
  const item = review.data
  const done = item ? isTerminal(item.status) : false
  const agentLog = useQuery({
    queryKey: ['review-agent-log', reviewId, done],
    queryFn: () => api.reviewAgentLog(reviewId),
    enabled: Boolean(item?.agent),
    refetchInterval: done ? false : 3000,
  })

  const findings = useQuery({
    queryKey: ['review-findings', reviewId],
    queryFn: () => api.reviewFindings(reviewId),
    enabled: Boolean(item),
    // 判定中也拉，能看到已完成质检项的结论慢慢累积
    refetchInterval: done ? false : 5000,
  })

  const cancel = useMutation({
    mutationFn: () => api.cancelReview(reviewId),
    onSuccess: () => {
      toast.success('已取消')
      void queryClient.invalidateQueries({ queryKey: ['review', reviewId] })
    },
    onError: (exception) => toast.error(exception instanceof ApiError ? exception.message : '取消失败'),
  })

  if (review.isLoading) {
    return (
      <div className="space-y-3">
        <Skeleton className="h-8 w-60" />
        <Skeleton className="h-24" />
      </div>
    )
  }
  if (review.isError || !item) {
    return <ErrorBox message={review.error instanceof ApiError ? review.error.message : '审核记录不存在'} />
  }

  const status = REVIEW_STATUS[item.status]
  const stageIndex = STAGES.indexOf(item.stage)
  const tasks = rollup(findings.data ?? [])
  const shown = filter === 'all' ? tasks : tasks.filter((row) => row.verdict === filter)
  const canControl = user?.role === 'admin' || user?.id === item.owner_id

  return (
    <div className="space-y-4">
      <PageHeader
        title={item.name}
        description={
          <>
            {item.supplier && <>供应商 {item.supplier} · </>}
            依据《{item.spec_title || '未填'}》 · 提交于 {formatDateTime(item.created_at)}
          </>
        }
        actions={
          <>
            <Link to={`/review?project=${item.project_id}`}>
              <Button variant="ghost">
                <ArrowLeft className="h-3.5 w-3.5" />
                返回列表
              </Button>
            </Link>
            <Button
              variant="ghost"
              onClick={() => void api.downloadReviewArchive(item.id).catch(() => toast.error('下载失败'))}
            >
              <Package className="h-3.5 w-3.5" />
              原始包
            </Button>
            {canControl && !done && (
              <Button variant="ghost" loading={cancel.isPending} onClick={() => cancel.mutate()}>
                <Square className="h-3.5 w-3.5" />
                取消
              </Button>
            )}
            <Button
              variant="primary"
              disabled={!item.report_path}
              onClick={() => void api.downloadReviewReport(item.id).catch(() => toast.error('下载失败'))}
            >
              <FileDown className="h-3.5 w-3.5" />
              下载返修报告
            </Button>
          </>
        }
      >
        <div className="mt-1.5 flex flex-wrap items-center gap-2">
          <StatusDot tone={status.tone} pulse={!done} label={done ? status.label : REVIEW_STAGE[item.stage]} />
          <Mono className="text-[11px] text-fg-subtle">{item.workspace}</Mono>
          <Link to={`/review?project=${item.project_id}`} title="查看该项目的质检记录">
            <ProjectTag projectId={item.project_id} linked={false} />
          </Link>
          {item.model_label && <Badge tone="primary">{item.model_label}</Badge>}
          <Badge>{item.agent ? REVIEW_AGENTS[item.agent] : '直接 LLM（历史）'}</Badge>
        </div>
      </PageHeader>

      {item.error && <ErrorBox message={item.error} />}

      {/* 阶段进度 */}
      <Card>
        <CardBody className="flex flex-wrap items-center gap-x-2 gap-y-2">
          {STAGES.map((stage, index) => {
            const reached = stageIndex >= index || item.status === 'succeeded'
            const active = item.stage === stage && !done
            return (
              <React.Fragment key={stage}>
                {index > 0 && <span className="h-px w-6 bg-border" aria-hidden />}
                <span
                  className={cn(
                    'rounded-md border px-2 py-1 text-xs',
                    active
                      ? 'border-primary bg-primary-soft text-primary'
                      : reached
                        ? 'border-transparent bg-success-soft text-success'
                        : 'border-border bg-muted text-fg-subtle',
                  )}
                >
                  {REVIEW_STAGE[stage]}
                </span>
              </React.Fragment>
            )
          })}
        </CardBody>
      </Card>

      {/* 统计 */}
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
        <Stat label="受检题数" value={item.stats.n_tasks} />
        <Stat label="通过" value={item.stats.n_pass} tone="success" />
        <Stat label="不合格" value={item.stats.n_fail} tone="danger" />
        <Stat label="需人工复核" value={item.stats.n_needs_review} tone="warning" />
        <Stat
          label="通过率"
          value={item.stats.n_tasks ? formatPercent((item.stats.n_pass / item.stats.n_tasks) * 100) : '—'}
          hint="通过 / 受检，需人工复核不算通过"
        />
      </div>

      {item.notes.length > 0 && (
        <Card>
          <CardHeader
            title="审核备注"
            description="平台替你做的取舍：定位不到原文的质检项、被忽略的模型返回、截断说明等，报告里也会原样写上。"
          />
          <CardBody>
            <ul className="space-y-1 text-xs leading-relaxed text-fg-muted">
              {item.notes.map((note, index) => (
                <li key={index}>· {note}</li>
              ))}
            </ul>
          </CardBody>
        </Card>
      )}

      {item.agent && (
        <Card>
          <CardHeader title="Agent 执行日志" description="每次拆项与核查都由独立的 Agent 会话执行，原生工具轨迹保存在审核工作目录的 agents/ 下。" />
          <CardBody>
            {agentLog.isError ? <ErrorBox message="Agent 日志加载失败" /> : (
              <pre className="max-h-80 overflow-auto whitespace-pre-wrap break-all rounded-lg bg-bg-elevated p-3 font-mono text-[11px] text-fg-muted">
                {agentLog.data?.text || 'Agent 尚未产生执行日志。'}
              </pre>
            )}
          </CardBody>
        </Card>
      )}
      {/* 质检项 */}
      <Card>
        <CardHeader
          title="质检项"
          description="按需求文档拆出来的检查项，每项一个 subagent。每项都必须带规范原文摘录，定位不到的会被丢弃。"
          actions={
            <span className="text-xs text-fg-muted">
              {item.stats.n_checks_done} / {item.stats.n_checks} 已完成
            </span>
          }
        />
        {item.checks.length === 0 ? (
          <EmptyState title="还没拆出质检项" description="平台正在读需求文档。" />
        ) : (
          <Table>
            <thead>
              <Tr>
                <Th className="w-20">编号</Th>
                <Th>检查项</Th>
                <Th className="w-24">状态</Th>
                <Th className="w-16 text-right">不合格</Th>
                <Th className="w-16 text-right">待复核</Th>
                <Th className="w-16 text-right">通过</Th>
              </Tr>
            </thead>
            <tbody>
              {item.checks.map((check) => (
                <Tr key={check.code}>
                  <Td>
                    <Mono className="text-xs">{check.code}</Mono>
                  </Td>
                  <Td>
                    <div className="text-[13px] text-fg">{check.title}</div>
                    <div className="mt-0.5 text-[11px] leading-relaxed text-fg-subtle">
                      {check.source && <>出处 {check.source} · </>}
                      {check.requirement}
                    </div>
                    {check.drop_reason && (
                      <div className="mt-0.5 text-[11px] text-warning">已丢弃：{check.drop_reason}</div>
                    )}
                    {check.error && <div className="mt-0.5 text-[11px] text-danger">{check.error}</div>}
                  </Td>
                  <Td>
                    <Badge tone={CHECK_STATUS[check.status].tone}>{CHECK_STATUS[check.status].label}</Badge>
                  </Td>
                  <Td className="num text-right">{check.n_fail}</Td>
                  <Td className="num text-right">{check.n_needs_review}</Td>
                  <Td className="num text-right">{check.n_pass}</Td>
                </Tr>
              ))}
            </tbody>
          </Table>
        )}
      </Card>

      {/* 逐题结论 */}
      <Card>
        <CardHeader
          title="逐题结论"
          description="同一题可能出现在多个质检项里，这里已按题聚合：任一项不合格即判不合格。"
          actions={
            <div className="flex items-center gap-1">
              {(['all', 'fail', 'needs_review', 'pass'] as const).map((value) => (
                <button
                  key={value}
                  type="button"
                  onClick={() => setFilter(value)}
                  className={cn(
                    'rounded-md px-2 py-1 text-xs transition-colors',
                    filter === value ? 'bg-primary-soft text-primary' : 'text-fg-muted hover:bg-card-hover',
                  )}
                >
                  {value === 'all' ? `全部 ${tasks.length}` : `${VERDICT[value].label} ${countOf(tasks, value)}`}
                </button>
              ))}
            </div>
          }
        />
        {shown.length === 0 ? (
          <EmptyState
            title={tasks.length === 0 ? '还没有结论' : '这个筛选下没有题目'}
            description={tasks.length === 0 ? '质检项判定完成后逐题结论会出现在这里。' : undefined}
          />
        ) : (
          <Table>
            <thead>
              <Tr>
                <Th className="w-40">题号</Th>
                <Th>源数据相对位置</Th>
                <Th className="w-28">结论</Th>
                <Th>问题</Th>
              </Tr>
            </thead>
            <tbody>
              {shown.map((row) => (
                <Tr key={row.task_id}>
                  <Td>
                    <Mono className="text-xs">{row.task_id}</Mono>
                  </Td>
                  <Td>
                    <Mono className="text-[11px] break-all text-fg-muted">{row.rel_path}</Mono>
                  </Td>
                  <Td>
                    <Badge tone={VERDICT[row.verdict].tone}>{VERDICT[row.verdict].label}</Badge>
                  </Td>
                  <Td>
                    <ul className="space-y-0.5 text-[11px] leading-relaxed">
                      {row.failed.map((finding, index) => (
                        <li key={`f-${index}`} className="text-fg-muted">
                          <Mono className="mr-1 text-[10px] text-danger">{finding.check_code}</Mono>
                          {finding.problems.join('；')}
                        </li>
                      ))}
                      {row.pending.map((finding, index) => (
                        <li key={`p-${index}`} className="text-fg-muted">
                          <Mono className="mr-1 text-[10px] text-warning">{finding.check_code}</Mono>
                          {finding.problems.join('；') || '需人工确认'}
                        </li>
                      ))}
                      {row.failed.length === 0 && row.pending.length === 0 && (
                        <li className="text-fg-subtle">—</li>
                      )}
                    </ul>
                  </Td>
                </Tr>
              ))}
            </tbody>
          </Table>
        )}
      </Card>

      {/* 留存信息 */}
      <Card>
        <CardHeader title="留存数据" description="原始上传件与全部中间结论都留在工作目录里，可随时回看下载。" />
        <CardBody className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <KeyValue label="工作目录">
            <Mono className="text-[11px] break-all">{item.work_dir}</Mono>
          </KeyValue>
          <KeyValue label="样例压缩包">
            <span className="text-xs">
              {item.archive_name} · {formatBytes(item.archive_bytes)}
            </span>
          </KeyValue>
          <KeyValue label="需求文档">
            <span className="text-xs">{item.doc_names.join('、') || '—'}</span>
          </KeyValue>
          <KeyValue label="模型用量">
            <span className="num text-xs">
              {Object.entries(item.usage).length === 0
                ? '—'
                : Object.entries(item.usage)
                    .map(([key, value]) => `${key} ${value}`)
                    .join(' · ')}
            </span>
          </KeyValue>
        </CardBody>
      </Card>

      {item.prompt && (
        <Card>
          <CardHeader title="自定义提示词" description="叠加在需求文档之上，不替代文档。" />
          <CardBody>
            <pre className="whitespace-pre-wrap text-xs leading-relaxed text-fg-muted">{item.prompt}</pre>
          </CardBody>
        </Card>
      )}

      {done && item.status === 'succeeded' && !item.report_path && (
        <InlineHint>报告文件不在了（可能被清理过），结论仍然保留在这一页。</InlineHint>
      )}
    </div>
  )
}

function countOf(tasks: TaskRow[], verdict: ReviewVerdict): number {
  return tasks.filter((row) => row.verdict === verdict).length
}

function isTerminal(status: string): boolean {
  return status === 'succeeded' || status === 'failed' || status === 'cancelled'
}
