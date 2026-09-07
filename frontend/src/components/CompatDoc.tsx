/**
 * "模型 × 框架" 适配速查文档。
 * 数据来自 scripts/gen_framework_support.py 对 LLaMA-Factory / verl / TRL / Harbor
 * 源码的静态解析，因此版本升级后只要重新生成即可，不会与实际能力脱节。
 */
import * as React from 'react'
import { useQuery } from '@tanstack/react-query'
import { BookOpen, Search } from 'lucide-react'
import { api } from '@/lib/api'
import type { Framework } from '@/lib/types'
import { Badge, Button, Dialog, Input, Skeleton, Table, Tabs, TabsContent, TabsList, TabsTrigger, Td, Th, Tr } from './ui'
import { FRAMEWORK_LABEL } from './common'

export function CompatDocButton({
  defaultFramework = 'llamafactory',
  label = '模型适配速查',
  size = 'sm',
}: {
  defaultFramework?: Framework
  label?: string
  size?: 'sm' | 'md'
}) {
  const [open, setOpen] = React.useState(false)
  return (
    <>
      <Button variant="outline" size={size} onClick={() => setOpen(true)} type="button">
        <BookOpen className="h-3.5 w-3.5" aria-hidden />
        {label}
      </Button>
      <CompatDocDialog open={open} onOpenChange={setOpen} defaultFramework={defaultFramework} />
    </>
  )
}

export function CompatDocDialog({
  open,
  onOpenChange,
  defaultFramework = 'llamafactory',
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  defaultFramework?: Framework
}) {
  const compat = useQuery({
    queryKey: ['model-compat'],
    queryFn: () => api.modelCompat(),
    enabled: open,
    staleTime: 5 * 60 * 1000,
  })
  const [tab, setTab] = React.useState<string>(defaultFramework)
  const [query, setQuery] = React.useState('')

  React.useEffect(() => setTab(defaultFramework), [defaultFramework])

  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      width="max-w-4xl"
      title="模型 × 框架 适配速查"
      description={
        compat.data?.generated_at
          ? `数据解析自本机框架源码 · 生成于 ${new Date(compat.data.generated_at).toLocaleString('zh-CN')}`
          : '数据解析自本机框架源码'
      }
    >
      {compat.isLoading ? (
        <Skeleton className="h-72 w-full" />
      ) : (
        <Tabs value={tab} onValueChange={setTab}>
          <TabsList>
            {(compat.data?.sections ?? []).map((section) => (
              <TabsTrigger key={section.framework} value={section.framework}>
                {FRAMEWORK_LABEL[section.framework]}
              </TabsTrigger>
            ))}
          </TabsList>

          {(compat.data?.sections ?? []).map((section) => {
            const rows = section.table?.rows ?? []
            const filtered = query
              ? rows.filter((row) => row.join(' ').toLowerCase().includes(query.toLowerCase()))
              : rows
            return (
              <TabsContent key={section.framework} value={section.framework} className="mt-3 space-y-3">
                <div>
                  <div className="flex flex-wrap items-center gap-2">
                    <h3 className="text-sm font-semibold">{section.title}</h3>
                    <Badge tone="primary">{section.headline}</Badge>
                  </div>
                  <ul className="mt-2 space-y-1.5">
                    {section.bullets.map((bullet) => (
                      <li key={bullet} className="flex gap-2 text-xs leading-relaxed text-fg-muted">
                        <span className="mt-1.5 h-1 w-1 shrink-0 rounded-full bg-primary" aria-hidden />
                        <span className="break-words">{bullet}</span>
                      </li>
                    ))}
                  </ul>
                </div>

                {section.table && (
                  <>
                    <div className="relative">
                      <Search
                        className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-fg-subtle"
                        aria-hidden
                      />
                      <Input
                        value={query}
                        onChange={(event) => setQuery(event.target.value)}
                        placeholder={`在 ${rows.length} 条模型记录中搜索，例如 qwen3 / llama / template`}
                        className="pl-8"
                        aria-label="搜索模型适配表"
                      />
                    </div>
                    <div className="max-h-80 overflow-auto rounded-lg border border-border">
                      <Table>
                        <thead className="sticky top-0 bg-card">
                          <tr>
                            {section.table.columns.map((column) => (
                              <Th key={column}>{column}</Th>
                            ))}
                          </tr>
                        </thead>
                        <tbody>
                          {filtered.slice(0, 400).map((row) => (
                            <Tr key={row[0]}>
                              {row.map((cell, index) => (
                                <Td key={index} className={index === 0 ? 'text-xs font-medium' : 'num text-[11px] text-fg-muted'}>
                                  {cell}
                                </Td>
                              ))}
                            </Tr>
                          ))}
                          {!filtered.length && (
                            <tr>
                              <Td colSpan={section.table.columns.length}>
                                <div className="py-6 text-center text-xs text-fg-subtle">
                                  没有匹配的模型。未收录不代表不能训练，只是需要手动指定 template。
                                </div>
                              </Td>
                            </tr>
                          )}
                        </tbody>
                      </Table>
                    </div>
                    {filtered.length > 400 && (
                      <p className="text-[11px] text-fg-subtle">仅显示前 400 条，请用搜索缩小范围。</p>
                    )}
                  </>
                )}
              </TabsContent>
            )
          })}
        </Tabs>
      )}
    </Dialog>
  )
}
