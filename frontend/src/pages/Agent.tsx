/**
 * 平台内置 Agent：左侧是这个账号自己的历史对话，右侧是对话区。
 * 对话由后端启动本机 Claude Code，模型由管理员在「系统设置」里统一配置，
 * 附件（输入框旁的按钮或直接拖进对话区）、改名、删除都在这里完成；对话之间按账号隔离。
 */
import * as React from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Bot,
  Check,
  FileText,
  Loader2,
  Paperclip,
  Pencil,
  Plus,
  Send,
  Sparkles,
  Square,
  Trash2,
  Upload,
  Wrench,
  X,
} from 'lucide-react'
import { toast } from 'sonner'
import { api, ApiError } from '@/lib/api'
import { cn, formatRelative } from '@/lib/utils'
import {
  Badge,
  Button,
  Dialog,
  EmptyState,
  ErrorBox,
  Input,
  Spinner,
  Textarea,
  Tooltip,
  TooltipProvider,
} from '@/components/ui'
import type { AgentConversation, AgentMessage } from '@/lib/types'
import { isFileDrag } from '@/lib/agentModel'

const MD_CLASS =
  'space-y-2 text-sm leading-6 text-fg [&_a]:text-primary [&_a]:underline [&_code]:rounded [&_code]:bg-muted [&_code]:px-1 [&_code]:py-0.5 [&_code]:text-[12px] [&_h1]:text-base [&_h1]:font-semibold [&_h2]:text-sm [&_h2]:font-semibold [&_h3]:text-sm [&_h3]:font-semibold [&_li]:ml-5 [&_li]:list-disc [&_ol_li]:list-decimal [&_pre]:overflow-auto [&_pre]:rounded-md [&_pre]:bg-muted [&_pre]:p-2 [&_pre]:text-xs [&_table]:w-full [&_table]:text-xs [&_td]:border [&_td]:border-border [&_td]:px-2 [&_td]:py-1 [&_th]:border [&_th]:border-border [&_th]:px-2 [&_th]:py-1'

/** 加载失败时把后端给的原因带上，否则「接口不存在」和「服务挂了」看起来一模一样。 */
function loadError(prefix: string, error: unknown): string {
  return error instanceof ApiError && error.message ? `${prefix}：${error.message}` : prefix
}

function MessageView({ message }: { message: AgentMessage }) {
  if (message.kind === 'tool_call') {
    let pretty = message.content
    try {
      pretty = JSON.stringify(JSON.parse(message.content), null, 2)
    } catch {
      /* 保留原文 */
    }
    return (
      <details className="group rounded-md border border-primary/30 bg-primary-soft/40 px-3 py-2 text-xs">
        <summary className="flex cursor-pointer items-center gap-1.5 text-primary">
          <Wrench className="h-3.5 w-3.5" aria-hidden />
          <span className="font-medium">调用工具</span>
          <span className="font-mono">{message.name}</span>
        </summary>
        <pre className="mt-2 max-h-72 overflow-auto whitespace-pre-wrap break-words text-[11px] text-fg-muted">{pretty}</pre>
      </details>
    )
  }
  if (message.kind === 'tool_result') {
    return (
      <details className="rounded-md border border-border bg-card-hover px-3 py-2 text-xs">
        <summary className="cursor-pointer text-fg-muted">
          工具返回{message.name ? ` · ${message.name}` : ''}
        </summary>
        <pre className="mt-2 max-h-72 overflow-auto whitespace-pre-wrap break-words text-[11px] text-fg-muted">{message.content}</pre>
      </details>
    )
  }
  if (message.kind === 'error' || message.role === 'assistant' && !message.content.trim()) {
    return <ErrorBox message={message.content || 'Agent 没有返回内容'} />
  }
  if (message.role === 'user') {
    return (
      <div className="flex justify-end">
        <div className="max-w-[85%] whitespace-pre-wrap break-words rounded-2xl rounded-br-sm bg-primary px-4 py-2.5 text-sm text-primary-fg">
          {message.content}
        </div>
      </div>
    )
  }
  return (
    <div className="flex gap-2.5">
      <div className="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-primary-soft text-primary">
        <Bot className="h-4 w-4" aria-hidden />
      </div>
      <div className="min-w-0 flex-1 rounded-2xl rounded-tl-sm border border-border bg-card px-4 py-2.5">
        <div className={MD_CLASS}>
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{message.content}</ReactMarkdown>
        </div>
      </div>
    </div>
  )
}

function ConversationRow({
  item,
  active,
  onSelect,
  onRename,
  onDelete,
}: {
  item: AgentConversation
  active: boolean
  onSelect: () => void
  onRename: (title: string) => void
  onDelete: () => void
}) {
  const [editing, setEditing] = React.useState(false)
  const [draft, setDraft] = React.useState(item.title)
  const input = React.useRef<HTMLInputElement>(null)
  React.useEffect(() => {
    if (editing) input.current?.focus()
  }, [editing])

  const save = () => {
    const title = draft.trim()
    if (title && title !== item.title) onRename(title)
    setEditing(false)
  }

  return (
    <div
      className={cn(
        'group relative flex items-center gap-2 rounded-md px-2.5 py-2 text-[13px] transition-colors',
        active ? 'bg-primary-soft text-primary' : 'text-fg-muted hover:bg-card-hover hover:text-fg',
      )}
    >
      {editing ? (
        <Input
          ref={input}
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          onBlur={save}
          onKeyDown={(event) => {
            if (event.key === 'Enter') save()
            if (event.key === 'Escape') setEditing(false)
          }}
          className="h-7 min-w-0 flex-1 px-1.5 py-0 text-xs"
          aria-label="对话名称"
        />
      ) : (
        <button
          type="button"
          onClick={onSelect}
          className="flex min-w-0 flex-1 items-center gap-2 text-left"
          title={item.title}
        >
          {item.status === 'running' ? (
            <Loader2 className="h-3.5 w-3.5 shrink-0 animate-spin text-primary" aria-hidden />
          ) : (
            <span
              className={cn(
                'h-1.5 w-1.5 shrink-0 rounded-full',
                item.status === 'failed' ? 'bg-danger' : active ? 'bg-primary' : 'bg-fg-subtle',
              )}
              aria-hidden
            />
          )}
          <span className="truncate">{item.title}</span>
        </button>
      )}
      {!editing && (
        <span className="hidden shrink-0 items-center gap-0.5 group-hover:flex">
          <button
            type="button"
            className="rounded p-1 text-fg-subtle hover:bg-muted hover:text-fg"
            onClick={() => {
              setDraft(item.title)
              setEditing(true)
            }}
            aria-label={`重命名「${item.title}」`}
          >
            <Pencil className="h-3.5 w-3.5" aria-hidden />
          </button>
          <button
            type="button"
            className="rounded p-1 text-fg-subtle hover:bg-danger-soft hover:text-danger"
            onClick={onDelete}
            aria-label={`删除「${item.title}」`}
          >
            <Trash2 className="h-3.5 w-3.5" aria-hidden />
          </button>
        </span>
      )}
    </div>
  )
}

export default function Agent() {
  const queryClient = useQueryClient()
  const [activeId, setActiveId] = React.useState('')
  const [draft, setDraft] = React.useState('')
  const [pendingDelete, setPendingDelete] = React.useState<AgentConversation | null>(null)
  const [showCapabilities, setShowCapabilities] = React.useState(false)
  const [dragging, setDragging] = React.useState(false)
  // dragenter/dragleave 会在子元素之间反复触发，按计数判断是否真的离开了对话区
  const dragDepth = React.useRef(0)
  const fileInput = React.useRef<HTMLInputElement>(null)
  const bottom = React.useRef<HTMLDivElement>(null)

  const conversations = useQuery({
    queryKey: ['agent-conversations'],
    queryFn: () => api.agentConversations(),
    refetchInterval: 3000,
  })
  const systemSettings = useQuery({ queryKey: ['settings'], queryFn: () => api.settings() })
  const skills = useQuery({ queryKey: ['agent-skills'], queryFn: () => api.agentSkills() })
  const tools = useQuery({ queryKey: ['agent-tools'], queryFn: () => api.agentTools() })

  const rows = conversations.data ?? []
  React.useEffect(() => {
    if (!activeId && rows.length) setActiveId(rows[0].id)
    if (activeId && rows.length && !rows.some((row) => row.id === activeId)) setActiveId(rows[0].id)
  }, [activeId, rows])

  const active = rows.find((row) => row.id === activeId)
  const messages = useQuery({
    queryKey: ['agent-messages', activeId],
    queryFn: () => api.agentMessages(activeId),
    enabled: Boolean(activeId),
    refetchInterval: active?.status === 'running' ? 1000 : false,
  })

  React.useEffect(() => {
    bottom.current?.scrollIntoView({ behavior: 'smooth', block: 'end' })
  }, [messages.data?.length, active?.status])

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ['agent-conversations'] })
    void queryClient.invalidateQueries({ queryKey: ['agent-messages', activeId] })
  }
  const fail = (error: unknown) =>
    toast.error(error instanceof ApiError ? error.message : '操作失败，请稍后重试')

  const create = useMutation({
    mutationFn: () => api.createAgentConversation(),
    onSuccess: (conversation) => {
      setActiveId(conversation.id)
      void queryClient.invalidateQueries({ queryKey: ['agent-conversations'] })
    },
    onError: fail,
  })
  const rename = useMutation({
    mutationFn: ({ id, title }: { id: string; title: string }) =>
      api.updateAgentConversation(id, { title }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['agent-conversations'] }),
    onError: fail,
  })
  const remove = useMutation({
    mutationFn: (id: string) => api.deleteAgentConversation(id),
    onSuccess: () => {
      setPendingDelete(null)
      void queryClient.invalidateQueries({ queryKey: ['agent-conversations'] })
    },
    onError: fail,
  })
  const send = useMutation({
    mutationFn: ({ id, content }: { id: string; content: string }) =>
      api.sendAgentMessage(id, content),
    onSuccess: () => {
      setDraft('')
      invalidate()
    },
    onError: fail,
  })
  const stop = useMutation({
    mutationFn: (id: string) => api.stopAgentConversation(id),
    onSuccess: invalidate,
    onError: fail,
  })
  const upload = useMutation({
    mutationFn: async ({ id, files }: { id: string; files: File[] }) => {
      for (const file of files) await api.uploadAgentAttachment(id, file)
      return files.length
    },
    onSuccess: (count) => {
      toast.success(`已上传 ${count} 个附件，发送消息时 Agent 可以读取`)
      invalidate()
    },
    onError: (error) => {
      invalidate()
      fail(error)
    },
  })
  const uploadFiles = (files: FileList | null | undefined) => {
    const list = Array.from(files ?? [])
    if (!active || !list.length) return
    if (upload.isPending) {
      toast.error('上一批附件还在上传，请稍候')
      return
    }
    upload.mutate({ id: active.id, files: list })
  }
  const dropProps = {
    onDragEnter: (event: React.DragEvent) => {
      if (!active || !isFileDrag(event.dataTransfer.types)) return
      event.preventDefault()
      dragDepth.current += 1
      setDragging(true)
    },
    onDragOver: (event: React.DragEvent) => {
      if (!active || !isFileDrag(event.dataTransfer.types)) return
      event.preventDefault()
      event.dataTransfer.dropEffect = 'copy'
    },
    onDragLeave: () => {
      dragDepth.current = Math.max(0, dragDepth.current - 1)
      if (!dragDepth.current) setDragging(false)
    },
    onDrop: (event: React.DragEvent) => {
      if (!isFileDrag(event.dataTransfer.types)) return
      event.preventDefault()
      dragDepth.current = 0
      setDragging(false)
      uploadFiles(event.dataTransfer.files)
    },
  }
  const removeAttachment = useMutation({
    mutationFn: ({ id, attachmentId }: { id: string; attachmentId: string }) =>
      api.deleteAgentAttachment(id, attachmentId),
    onSuccess: invalidate,
    onError: fail,
  })

  const sendNow = () => {
    if (!active || !draft.trim() || send.isPending) return
    if (active.status === 'running') {
      toast.error('上一轮还在运行，请稍候或先停止')
      return
    }
    send.mutate({ id: active.id, content: draft.trim() })
  }

  const modelLabel = active?.model_name || systemSettings.data?.agent_model || '自动选择'

  return (
    <TooltipProvider>
      <div className="-m-5 flex h-[calc(100vh-4rem)] min-h-0 bg-bg text-fg">
        {/* ------------------------------------------------ 历史对话 --- */}
        <aside className="flex w-[260px] shrink-0 flex-col border-r border-border bg-bg-elevated">
          <div className="flex items-center justify-between gap-2 px-3 py-3">
            <div className="flex items-center gap-2">
              <Sparkles className="h-4 w-4 text-primary" aria-hidden />
              <span className="text-sm font-semibold">Agent</span>
            </div>
            <Tooltip label="新建对话">
              <Button
                variant="primary"
                size="icon"
                className="h-7 w-7"
                onClick={() => create.mutate()}
                loading={create.isPending}
                aria-label="新建对话"
              >
                <Plus className="h-4 w-4" aria-hidden />
              </Button>
            </Tooltip>
          </div>
          <div className="flex-1 overflow-y-auto px-2 pb-3">
            {conversations.isLoading ? (
              <div className="flex justify-center py-8"><Spinner /></div>
            ) : conversations.isError ? (
              <ErrorBox message={loadError('历史对话加载失败', conversations.error)} />
            ) : rows.length ? (
              <div className="space-y-0.5">
                {rows.map((item) => (
                  <ConversationRow
                    key={item.id}
                    item={item}
                    active={item.id === activeId}
                    onSelect={() => setActiveId(item.id)}
                    onRename={(title) => rename.mutate({ id: item.id, title })}
                    onDelete={() => setPendingDelete(item)}
                  />
                ))}
              </div>
            ) : (
              <p className="px-3 py-8 text-center text-xs text-fg-subtle">还没有对话，点右上角「+」开始。</p>
            )}
          </div>
          <div className="border-t border-border p-2">
            <button
              type="button"
              onClick={() => setShowCapabilities(true)}
              className="flex w-full items-center gap-2 rounded px-2.5 py-2 text-[13px] text-fg-muted hover:bg-card-hover hover:text-fg"
            >
              <Wrench className="h-4 w-4" aria-hidden />
              能力与 Skill
              <span className="ml-auto text-[10px] text-fg-subtle">
                {skills.data?.length ?? 0} Skill · {tools.data?.length ?? 0} 工具
              </span>
            </button>
          </div>
        </aside>

        {/* ---------------------------------------------------- 对话区 --- */}
        <section className="relative flex min-w-0 flex-1 flex-col" {...dropProps}>
          {dragging && (
            <div className="pointer-events-none absolute inset-2 z-20 flex flex-col items-center justify-center gap-2 rounded-xl border-2 border-dashed border-primary bg-primary-soft/80 text-primary">
              <Upload className="h-8 w-8" aria-hidden />
              <p className="text-sm font-medium">松开即可上传到当前对话</p>
              <p className="text-xs text-fg-muted">上传后发送消息时 Agent 可以读取这些文件</p>
            </div>
          )}
          <header className="flex h-14 shrink-0 items-center gap-3 border-b border-border px-4">
            <div className="min-w-0 flex-1">
              <h1 className="truncate text-sm font-semibold">{active?.title ?? 'Agent 助手'}</h1>
              <p className="truncate text-[11px] text-fg-subtle">
                {active
                  ? `${modelLabel} · ${active.message_count} 条消息 · ${formatRelative(active.updated_at)}`
                  : '模型由管理员统一配置，可查询数据、提交训练与评测、上传附件'}
              </p>
            </div>
            {active?.status === 'running' && (
              <Button variant="outline" size="sm" onClick={() => stop.mutate(active.id)}>
                <Square className="mr-1 h-3.5 w-3.5" aria-hidden />
                停止
              </Button>
            )}
          </header>

          <div className="min-h-0 flex-1 overflow-y-auto px-4 py-5 sm:px-8">
            {!active ? (
              <div className="mx-auto max-w-lg py-16">
                <EmptyState
                  icon={<Sparkles className="h-6 w-6" />}
                  title="开始和平台 Agent 对话"
                  description="Agent 会用管理员配置的模型理解需求，并通过平台 Skill 查询项目、数据集、任务与费用；上传的数据集压缩包也可以交给它处理。"
                  action={
                    <Button variant="primary" onClick={() => create.mutate()} loading={create.isPending}>
                      <Plus className="mr-1.5 h-4 w-4" aria-hidden />
                      新建对话
                    </Button>
                  }
                />
              </div>
            ) : (
              <div className="mx-auto flex max-w-3xl flex-col gap-4">
                {messages.isLoading && <div className="flex justify-center py-8"><Spinner /></div>}
                {messages.isError && <ErrorBox message={loadError('消息加载失败', messages.error)} />}
                {!messages.isLoading && !(messages.data ?? []).length && (
                  <div className="rounded-lg border border-dashed border-border p-6">
                    <p className="text-sm font-medium">可以试试：</p>
                    <div className="mt-3 flex flex-wrap gap-2">
                      {[
                        '列出我所在的项目和最近的任务',
                        '帮我检查最近失败的评测任务',
                        '这个月的费用花在哪些模型上？',
                        '我想评测一个数据集，需要准备什么？',
                      ].map((text) => (
                        <button
                          key={text}
                          type="button"
                          className="rounded-full border border-border bg-card px-3 py-1.5 text-xs text-fg-muted hover:border-primary hover:text-primary"
                          onClick={() => setDraft(text)}
                        >
                          {text}
                        </button>
                      ))}
                    </div>
                    <p className="mt-4 text-[11px] text-fg-subtle">
                      附件可以点输入框左侧的回形针，或直接把文件拖进对话区；Agent 使用你的账号权限操作平台，只显示你自己的数据。
                    </p>
                  </div>
                )}
                {(messages.data ?? []).map((message) => (
                  <MessageView key={message.id} message={message} />
                ))}
                {active.status === 'running' && (
                  <div className="flex items-center gap-2 text-xs text-fg-muted">
                    <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden />
                    Agent 正在思考并使用工具…
                  </div>
                )}
                <div ref={bottom} />
              </div>
            )}
          </div>

          {active && (
            <div className="shrink-0 border-t border-border bg-bg-elevated px-4 py-3 sm:px-8">
              <div className="mx-auto max-w-3xl">
                {active.attachments.length > 0 && (
                  <div className="mb-2 flex flex-wrap gap-1.5">
                    {active.attachments.map((attachment) => (
                      <span
                        key={attachment.id}
                        className="inline-flex items-center gap-1 rounded-full border border-border bg-card px-2 py-0.5 text-[11px] text-fg-muted"
                      >
                        <FileText className="h-3 w-3" aria-hidden />
                        <span className="max-w-48 truncate" title={attachment.name}>{attachment.name}</span>
                        <button
                          type="button"
                          className="text-fg-subtle hover:text-danger"
                          onClick={() => removeAttachment.mutate({ id: active.id, attachmentId: attachment.id })}
                          aria-label={`移除附件 ${attachment.name}`}
                        >
                          <X className="h-3 w-3" aria-hidden />
                        </button>
                      </span>
                    ))}
                  </div>
                )}
                <div className="flex items-end gap-2 rounded-xl border border-border bg-card p-2 focus-within:border-primary">
                  <input
                    ref={fileInput}
                    type="file"
                    multiple
                    className="hidden"
                    onChange={(event) => {
                      uploadFiles(event.target.files)
                      event.target.value = ''
                    }}
                  />
                  <Tooltip label="上传附件（也可以把文件拖进对话区）">
                    <Button
                      variant="ghost"
                      size="icon"
                      onClick={() => fileInput.current?.click()}
                      loading={upload.isPending}
                      aria-label="上传附件"
                    >
                      <Paperclip className="h-4 w-4" aria-hidden />
                    </Button>
                  </Tooltip>
                  <Textarea
                    value={draft}
                    onChange={(event) => setDraft(event.target.value)}
                    onKeyDown={(event) => {
                      if (event.key === 'Enter' && !event.shiftKey) {
                        event.preventDefault()
                        sendNow()
                      }
                    }}
                    rows={2}
                    placeholder="描述你想让 Agent 做什么…（Enter 发送，Shift+Enter 换行）"
                    className="min-h-0 resize-none border-0 bg-transparent shadow-none focus-visible:outline-none"
                  />
                  <Button
                    variant="primary"
                    size="icon"
                    onClick={sendNow}
                    loading={send.isPending}
                    disabled={!draft.trim() || active.status === 'running'}
                    aria-label="发送"
                  >
                    <Send className="h-4 w-4" aria-hidden />
                  </Button>
                </div>
                <p className="mt-1.5 text-[10px] text-fg-subtle">
                  Agent 以本机 Claude Code 为内核，运行在独立工作目录；不能修改平台源码。
                </p>
              </div>
            </div>
          )}
        </section>

        <Dialog
          open={showCapabilities}
          onOpenChange={setShowCapabilities}
          title="Agent 能力与 Skill"
          description="Skill 由平台仓库同步到 Agent 工作目录，每次发消息前自动刷新"
          width="max-w-2xl"
        >
          <div className="space-y-4">
            <div>
              <h3 className="mb-1.5 text-xs font-semibold text-fg">Skill</h3>
              <div className="space-y-1.5">
                {(skills.data ?? []).map((skill) => (
                  <div key={skill.name} className="rounded-md border border-border bg-card px-3 py-2">
                    <div className="flex items-center gap-2">
                      <Badge tone="primary">{skill.name}</Badge>
                      {skill.updated_at && (
                        <span className="text-[10px] text-fg-subtle">更新于 {formatRelative(skill.updated_at)}</span>
                      )}
                    </div>
                    <p className="mt-1 text-xs text-fg-muted">{skill.description}</p>
                  </div>
                ))}
                {!skills.data?.length && <p className="text-xs text-fg-subtle">没有可用 Skill。</p>}
              </div>
            </div>
            <div>
              <h3 className="mb-1.5 text-xs font-semibold text-fg">工具</h3>
              <div className="grid gap-1.5 sm:grid-cols-2">
                {(tools.data ?? []).map((tool) => (
                  <div key={tool.name} className="rounded-md border border-border bg-card px-3 py-2">
                    <div className="flex items-center gap-1.5 text-xs font-medium">
                      <Check className="h-3.5 w-3.5 text-success" aria-hidden />
                      {tool.name}
                    </div>
                    <p className="mt-0.5 text-[11px] text-fg-muted">{tool.description}</p>
                  </div>
                ))}
              </div>
            </div>
          </div>
        </Dialog>

        <Dialog
          open={Boolean(pendingDelete)}
          onOpenChange={(open) => !open && setPendingDelete(null)}
          title="删除对话"
          description={pendingDelete ? `「${pendingDelete.title}」及其消息、附件都会被删除，无法恢复。` : ''}
          footer={
            <>
              <Button variant="ghost" onClick={() => setPendingDelete(null)}>取消</Button>
              <Button
                variant="danger"
                loading={remove.isPending}
                onClick={() => pendingDelete && remove.mutate(pendingDelete.id)}
              >
                删除
              </Button>
            </>
          }
        >
          <p className="text-xs text-fg-muted">该操作只影响你自己的历史对话。</p>
        </Dialog>
      </div>
    </TooltipProvider>
  )
}
