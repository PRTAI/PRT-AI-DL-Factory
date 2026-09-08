/**
 * 平台文档（独立页面，从侧栏 Document 入口新标签页打开）。
 * 内容是后端 backend/app/resources/docs/ 下的 Markdown，改完刷新即可生效。
 */
import * as React from 'react'
import { Link, useParams, useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { ArrowLeft, BookOpen, ExternalLink, Menu, Search } from 'lucide-react'
import { api, getToken } from '@/lib/api'
import { cn } from '@/lib/utils'
import { Input, Skeleton } from '@/components/ui'

/** 文档插图走的是需要鉴权的接口，普通 <img> 带不上 token，所以取回 blob 再显示。 */
function AuthedImage({ src, alt }: { src?: string; alt?: string }) {
  const [url, setUrl] = React.useState<string | null>(null)
  const [failed, setFailed] = React.useState(false)

  React.useEffect(() => {
    if (!src) return
    if (!src.startsWith('/api/')) {
      setUrl(src)
      return
    }
    let objectUrl: string | null = null
    let cancelled = false
    fetch(src, { headers: { Authorization: `Bearer ${getToken() ?? ''}` } })
      .then((response) => (response.ok ? response.blob() : Promise.reject(new Error(String(response.status)))))
      .then((blob) => {
        if (cancelled) return
        objectUrl = URL.createObjectURL(blob)
        setUrl(objectUrl)
      })
      .catch(() => !cancelled && setFailed(true))
    return () => {
      cancelled = true
      if (objectUrl) URL.revokeObjectURL(objectUrl)
    }
  }, [src])

  if (failed) return <span className="text-xs text-fg-subtle">（插图加载失败：{alt}）</span>
  if (!url) return <span className="block h-40 animate-pulse rounded-md bg-muted" />
  return (
    <img
      src={url}
      alt={alt ?? ''}
      className="my-4 w-full rounded-md border border-border"
      loading="lazy"
    />
  )
}

function makeComponents(navigate: (to: string) => void) {
  return {
  h1: (props: React.HTMLAttributes<HTMLHeadingElement>) => (
    <h1 className="mb-4 mt-2 text-2xl font-semibold text-fg" {...props} />
  ),
  h2: (props: React.HTMLAttributes<HTMLHeadingElement>) => (
    <h2 className="mb-3 mt-8 border-b border-border pb-2 text-lg font-semibold text-fg" {...props} />
  ),
  h3: (props: React.HTMLAttributes<HTMLHeadingElement>) => (
    <h3 className="mb-2 mt-6 text-[15px] font-semibold text-fg" {...props} />
  ),
  p: (props: React.HTMLAttributes<HTMLParagraphElement>) => (
    <p className="my-3 text-sm leading-7 text-fg-muted" {...props} />
  ),
  ul: (props: React.HTMLAttributes<HTMLUListElement>) => (
    <ul className="my-3 list-disc space-y-1 pl-5 text-sm leading-7 text-fg-muted" {...props} />
  ),
  ol: (props: React.HTMLAttributes<HTMLOListElement>) => (
    <ol className="my-3 list-decimal space-y-1 pl-5 text-sm leading-7 text-fg-muted" {...props} />
  ),
  li: (props: React.HTMLAttributes<HTMLLIElement>) => <li className="pl-1" {...props} />,
  a: ({ href, ...props }: React.AnchorHTMLAttributes<HTMLAnchorElement>) => (
    <a
      href={href}
      className="link"
      target={href?.startsWith('http') ? '_blank' : undefined}
      rel="noreferrer"
      onClick={(event) => {
        // 站内文档跳转走前端路由，避免整页刷新
        if (href?.startsWith('/docs/')) {
          event.preventDefault()
          navigate(href)
        }
      }}
      {...props}
    />
  ),
  strong: (props: React.HTMLAttributes<HTMLElement>) => <strong className="font-semibold text-fg" {...props} />,
  blockquote: (props: React.HTMLAttributes<HTMLQuoteElement>) => (
    <blockquote
      className="my-4 rounded-r border-l-[3px] border-primary bg-primary-soft px-4 py-2.5 text-sm leading-7 text-fg-muted"
      {...props}
    />
  ),
  table: (props: React.TableHTMLAttributes<HTMLTableElement>) => (
    <div className="my-4 overflow-x-auto rounded-md border border-border">
      <table className="w-full border-collapse text-sm" {...props} />
    </div>
  ),
  th: (props: React.ThHTMLAttributes<HTMLTableCellElement>) => (
    <th className="border-b border-border bg-muted px-3 py-2 text-left text-xs font-medium text-fg-muted" {...props} />
  ),
  td: (props: React.TdHTMLAttributes<HTMLTableCellElement>) => (
    <td className="border-b border-border px-3 py-2 align-top text-[13px] leading-6 text-fg-muted" {...props} />
  ),
  code: ({ className, children, ...props }: React.HTMLAttributes<HTMLElement>) => {
    const block = /language-/.test(className ?? '')
    if (block) {
      return (
        <code className="num block text-[12.5px] leading-6 text-fg" {...props}>
          {children}
        </code>
      )
    }
    return (
      <code className="num rounded bg-muted px-1.5 py-0.5 text-[12px] text-fg" {...props}>
        {children}
      </code>
    )
  },
  pre: (props: React.HTMLAttributes<HTMLPreElement>) => (
    <pre className="my-4 overflow-x-auto rounded-md border border-border bg-muted p-3.5" {...props} />
  ),
  hr: () => <hr className="my-8 border-border" />,
    img: (props: React.ImgHTMLAttributes<HTMLImageElement>) => <AuthedImage src={props.src} alt={props.alt} />,
  }
}

export default function Docs() {
  const { slug } = useParams<{ slug?: string }>()
  const navigate = useNavigate()
  const [keyword, setKeyword] = React.useState('')
  const [navOpen, setNavOpen] = React.useState(false)

  const list = useQuery({ queryKey: ['docs'], queryFn: () => api.docs() })
  const links = useQuery({ queryKey: ['doc-links'], queryFn: () => api.docLinks() })
  const current = slug ?? list.data?.[0]?.slug

  const doc = useQuery({
    queryKey: ['doc', current],
    queryFn: () => api.doc(current!),
    enabled: Boolean(current),
  })

  const needle = keyword.trim().toLowerCase()
  const items = (list.data ?? []).filter(
    (item) => !needle || `${item.title} ${item.summary} ${item.category}`.toLowerCase().includes(needle),
  )
  const grouped = items.reduce<Record<string, typeof items>>((acc, item) => {
    ;(acc[item.category] ??= []).push(item)
    return acc
  }, {})

  React.useEffect(() => {
    document.title = doc.data ? `${doc.data.title} · PRT 平台文档` : 'PRT 平台文档'
  }, [doc.data])

  const components = React.useMemo(() => makeComponents(navigate), [navigate])

  return (
    <div className="flex min-h-screen flex-col bg-bg text-fg">
      <header className="sticky top-0 z-40 flex h-16 shrink-0 items-center gap-3 border-b border-border bg-bg-elevated pl-7 pr-4">
        <button
          type="button"
          className="rounded p-1.5 text-fg-muted hover:bg-muted lg:hidden"
          onClick={() => setNavOpen((open) => !open)}
          aria-label="文档目录"
        >
          <Menu className="h-4 w-4" aria-hidden />
        </button>
        <img src="/logo.png" alt="PRT" className="logo-img h-9 w-auto" />
        <span className="h-7 w-px bg-border" aria-hidden />
        <span className="wordmark text-[22px] leading-none">AI DL Factory</span>
        <span className="ml-2 flex items-center gap-1.5 rounded bg-muted px-2 py-1 text-xs text-fg-muted">
          <BookOpen className="h-3.5 w-3.5" aria-hidden />
          平台文档
        </span>
        <div className="flex-1" />
        <Link to="/" className="flex items-center gap-1.5 text-sm text-fg-muted transition-colors hover:text-primary">
          <ArrowLeft className="h-4 w-4" aria-hidden />
          返回控制台
        </Link>
      </header>

      <div className="flex min-h-0 flex-1">
        <aside
          className={cn(
            'sticky top-16 h-[calc(100vh-4rem)] w-[236px] shrink-0 overflow-y-auto border-r border-border bg-bg px-3 py-4',
            navOpen ? 'block' : 'hidden lg:block',
          )}
        >
          <div className="relative mb-3">
            <Search
              className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-fg-subtle"
              aria-hidden
            />
            <Input
              value={keyword}
              onChange={(event) => setKeyword(event.target.value)}
              placeholder="搜索文档"
              className="h-8 pl-8 text-xs"
              aria-label="搜索文档"
            />
          </div>

          {list.isLoading && <Skeleton className="h-40 w-full" />}

          {Object.entries(grouped).map(([category, docs]) => (
            <div key={category} className="mb-5">
              {/* 分类是这份目录的骨架，字号要压得住下面的条目 */}
              <h2 className="mb-1.5 px-2.5 text-[15px] font-semibold tracking-tight text-fg">
                {category}
              </h2>
              <ul>
                {docs.map((item) => (
                  <li key={item.slug}>
                    <button
                      type="button"
                      onClick={() => {
                        navigate(`/docs/${item.slug}`)
                        setNavOpen(false)
                      }}
                      className={cn(
                        // 条目只给标题：摘要在正文第一段里都有，堆在目录里只会把层级压平
                        'relative flex w-full items-center rounded px-2.5 py-1.5 text-left text-[13px] transition-colors',
                        item.slug === current
                          ? 'bg-primary-soft font-medium text-primary'
                          : 'text-fg-muted hover:bg-card-hover hover:text-fg',
                      )}
                    >
                      {item.slug === current && (
                        <span className="absolute inset-y-1 left-0 w-[3px] rounded-r bg-primary" aria-hidden />
                      )}
                      <span className="truncate">{item.title}</span>
                    </button>
                  </li>
                ))}
              </ul>
            </div>
          ))}

          {!list.isLoading && !items.length && (
            <p className="px-2.5 py-6 text-center text-xs text-fg-subtle">没有匹配的文档</p>
          )}

          {/* ------------------------------------------------ 相关项目 -- */}
          {links.data?.length ? (
            <div className="mt-6 border-t border-border pt-4">
              <h2 className="mb-1.5 px-2.5 text-[15px] font-semibold tracking-tight text-fg">
                相关项目链接
              </h2>
              <ul>
                {links.data.map((item) => (
                  <li key={item.key}>
                    <a
                      href={item.url}
                      target="_blank"
                      rel="noreferrer"
                      title={`${item.note} — ${item.url}`}
                      className="group flex items-center gap-1.5 rounded px-2.5 py-1.5 text-[13px] text-fg-muted transition-colors hover:bg-card-hover hover:text-primary"
                    >
                      <span className="truncate">{item.label}</span>
                      {item.version && (
                        <span className="num shrink-0 text-[10px] text-fg-subtle">{item.version}</span>
                      )}
                      <ExternalLink
                        className="ml-auto h-3 w-3 shrink-0 text-fg-subtle opacity-0 transition-opacity group-hover:opacity-100"
                        aria-hidden
                      />
                    </a>
                  </li>
                ))}
              </ul>
              <p className="mt-1.5 px-2.5 text-[10px] leading-relaxed text-fg-subtle">
                版本号是本机实际装的那份检出。
              </p>
            </div>
          ) : null}
        </aside>

        <main className="min-w-0 flex-1 px-6 py-6 lg:px-10">
          <div className="mx-auto max-w-3xl">
            {doc.isLoading ? (
              <div className="space-y-3">
                <Skeleton className="h-8 w-64" />
                <Skeleton className="h-40 w-full" />
              </div>
            ) : doc.data ? (
              <article>
                <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
                  {doc.data.body}
                </ReactMarkdown>
                <p className="mt-10 border-t border-border pt-4 text-[11px] text-fg-subtle">
                  文档源文件：<code className="num">backend/app/resources/docs/</code>
                  ，修改后刷新本页即可生效。
                </p>
              </article>
            ) : (
              <p className="text-sm text-fg-muted">没有可显示的文档。</p>
            )}
          </div>
        </main>
      </div>
    </div>
  )
}
