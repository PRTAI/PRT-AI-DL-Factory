/**
 * Chart primitives.
 *
 * Guidance applied (ui-ux-pro-max `chart` domain, "Real-Time Streaming"):
 * - live series get an explicit pause control and always show the current value
 *   as text, so meaning never depends on colour or motion alone;
 * - each series carries a distinct stroke pattern in addition to its hue;
 * - buffers are bounded (the backend keeps ~30 min at 2 s resolution).
 */
import * as React from 'react'
import {
  Area,
  AreaChart,
  CartesianGrid,
  Cell,
  Legend,
  Line,
  LineChart,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { cn } from '@/lib/utils'

export const CHART_COLORS = [
  'var(--chart-1)',
  'var(--chart-2)',
  'var(--chart-3)',
  'var(--chart-4)',
  'var(--chart-5)',
  'var(--chart-6)',
]

const DASH_PATTERNS = ['', '6 3', '2 3', '10 4', '4 2 1 2', '1 3']

export interface SeriesDef {
  key: string
  label: string
  color?: string
  unit?: string
  /** Right-hand axis (e.g. learning rate against loss). */
  axis?: 'left' | 'right'
}

function axisStyle() {
  return { fill: 'var(--fg-subtle)', fontSize: 11 }
}

function TooltipContent({
  active,
  payload,
  label,
  labelFormatter,
  valueFormatter,
}: {
  active?: boolean
  payload?: { name?: string; value?: number | string; color?: string; dataKey?: string }[]
  label?: string | number
  labelFormatter?: (value: string | number) => string
  valueFormatter?: (value: number, key: string) => string
}) {
  if (!active || !payload?.length) return null
  return (
    <div className="rounded-lg border border-border bg-card px-2.5 py-2 text-xs shadow-xl">
      <div className="mb-1 text-fg-muted">{labelFormatter ? labelFormatter(label ?? '') : label}</div>
      <div className="space-y-0.5">
        {payload.map((entry) => (
          <div key={String(entry.dataKey)} className="flex items-center justify-between gap-4">
            <span className="flex items-center gap-1.5 text-fg-muted">
              <span className="h-2 w-2 rounded-full" style={{ background: entry.color }} aria-hidden />
              {entry.name}
            </span>
            <span className="num text-fg">
              {typeof entry.value === 'number' && valueFormatter
                ? valueFormatter(entry.value, String(entry.dataKey))
                : String(entry.value)}
            </span>
          </div>
        ))}
      </div>
    </div>
  )
}

/** Multi-series line chart with optional dual axis. */
export function LineSeriesChart({
  data,
  series,
  xKey = 'x',
  height = 220,
  xFormatter,
  yFormatter,
  yDomain,
  rightDomain,
  className,
}: {
  data: Record<string, number | string>[]
  series: SeriesDef[]
  xKey?: string
  height?: number
  xFormatter?: (value: string | number) => string
  yFormatter?: (value: number, key: string) => string
  yDomain?: [number | 'auto' | 'dataMin' | 'dataMax', number | 'auto' | 'dataMin' | 'dataMax']
  rightDomain?: [number | 'auto', number | 'auto']
  className?: string
}) {
  const hasRight = series.some((s) => s.axis === 'right')
  return (
    <div className={cn('w-full', className)} style={{ height }}>
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={data} margin={{ top: 8, right: hasRight ? 8 : 12, bottom: 4, left: 0 }}>
          <CartesianGrid stroke="var(--border)" strokeDasharray="3 3" vertical={false} />
          <XAxis
            dataKey={xKey}
            tick={axisStyle()}
            tickLine={false}
            axisLine={{ stroke: 'var(--border)' }}
            tickFormatter={xFormatter as never}
            minTickGap={24}
          />
          <YAxis
            yAxisId="left"
            tick={axisStyle()}
            tickLine={false}
            axisLine={false}
            domain={yDomain ?? ['auto', 'auto']}
            width={52}
          />
          {hasRight && (
            <YAxis
              yAxisId="right"
              orientation="right"
              tick={axisStyle()}
              tickLine={false}
              axisLine={false}
              domain={rightDomain ?? ['auto', 'auto']}
              width={52}
            />
          )}
          <Tooltip
            content={<TooltipContent labelFormatter={xFormatter} valueFormatter={yFormatter} />}
            cursor={{ stroke: 'var(--border-strong)', strokeWidth: 1 }}
          />
          {series.length > 1 && (
            <Legend
              wrapperStyle={{ fontSize: 11, color: 'var(--fg-muted)', paddingTop: 4 }}
              iconType="plainline"
            />
          )}
          {series.map((item, index) => (
            <Line
              key={item.key}
              yAxisId={item.axis === 'right' ? 'right' : 'left'}
              type="monotone"
              dataKey={item.key}
              name={item.label}
              stroke={item.color ?? CHART_COLORS[index % CHART_COLORS.length]}
              strokeWidth={1.8}
              strokeDasharray={DASH_PATTERNS[index % DASH_PATTERNS.length]}
              dot={false}
              isAnimationActive={false}
              connectNulls
            />
          ))}
        </LineChart>
      </ResponsiveContainer>
    </div>
  )
}

/** Filled area chart used for the Task-Manager style utilisation panes. */
export function UtilizationChart({
  data,
  series,
  xKey = 'x',
  height = 120,
  xFormatter,
  className,
}: {
  data: Record<string, number | string>[]
  series: SeriesDef[]
  xKey?: string
  height?: number
  xFormatter?: (value: string | number) => string
  className?: string
}) {
  const gradientId = React.useId().replace(/:/g, '')
  return (
    <div className={cn('w-full', className)} style={{ height }}>
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart data={data} margin={{ top: 4, right: 4, bottom: 0, left: 0 }}>
          <defs>
            {series.map((item, index) => {
              const color = item.color ?? CHART_COLORS[index % CHART_COLORS.length]
              return (
                <linearGradient key={item.key} id={`${gradientId}-${item.key}`} x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor={color} stopOpacity={0.45} />
                  <stop offset="100%" stopColor={color} stopOpacity={0.02} />
                </linearGradient>
              )
            })}
          </defs>
          <CartesianGrid stroke="var(--border)" strokeDasharray="3 3" vertical={false} />
          <XAxis dataKey={xKey} tick={axisStyle()} tickLine={false} axisLine={false} minTickGap={40} tickFormatter={xFormatter as never} />
          <YAxis
            domain={[0, 100]}
            ticks={[0, 50, 100]}
            tick={axisStyle()}
            tickLine={false}
            axisLine={false}
            width={34}
            tickFormatter={(value) => `${value}`}
          />
          <Tooltip
            content={<TooltipContent labelFormatter={xFormatter} valueFormatter={(value) => `${value.toFixed(1)}%`} />}
            cursor={{ stroke: 'var(--border-strong)', strokeWidth: 1 }}
          />
          {series.map((item, index) => {
            const color = item.color ?? CHART_COLORS[index % CHART_COLORS.length]
            return (
              <Area
                key={item.key}
                type="monotone"
                dataKey={item.key}
                name={item.label}
                stroke={color}
                strokeWidth={1.6}
                fill={`url(#${gradientId}-${item.key})`}
                isAnimationActive={false}
                connectNulls
              />
            )
          })}
        </AreaChart>
      </ResponsiveContainer>
    </div>
  )
}

/** Compact inline sparkline for table rows / stat tiles. */
export function Sparkline({
  values,
  color = 'var(--chart-1)',
  height = 32,
}: {
  values: number[]
  color?: string
  height?: number
}) {
  const data = React.useMemo(() => values.map((value, index) => ({ x: index, y: value })), [values])
  if (!values.length) return <div style={{ height }} />
  return (
    <div style={{ height }}>
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart data={data} margin={{ top: 2, right: 0, bottom: 0, left: 0 }}>
          <Area type="monotone" dataKey="y" stroke={color} strokeWidth={1.4} fill={color} fillOpacity={0.15} isAnimationActive={false} />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  )
}

/** Horizontal meter with a text value — never colour-only. */
export function Meter({
  label,
  value,
  max = 100,
  detail,
  tone = 'var(--chart-1)',
}: {
  label: string
  value: number
  max?: number
  detail?: string
  tone?: string
}) {
  const percent = Math.max(0, Math.min(100, (value / max) * 100))
  return (
    <div className="space-y-1">
      <div className="flex items-baseline justify-between gap-2 text-xs">
        <span className="text-fg-muted">{label}</span>
        <span className="num text-fg">{detail ?? `${percent.toFixed(0)}%`}</span>
      </div>
      <div className="h-1.5 overflow-hidden rounded-full bg-muted">
        <div
          className="h-full rounded-full transition-[width] duration-500"
          style={{ width: `${percent}%`, background: tone }}
        />
      </div>
    </div>
  )
}

/* -------------------------------------------------------------------------- */
/* Donut                                                                       */
/* -------------------------------------------------------------------------- */
export interface DonutSlice {
  key: string
  label: string
  value: number
  color: string
}

/**
 * 圆环图 + 右侧图例。中心放总数，图例里每一项都带数字与百分比——
 * 颜色只是辅助，看数字就能读，不依赖分辨色相。
 */
export function DonutChart({
  slices,
  centerLabel,
  centerValue,
  className,
  height = 200,
}: {
  slices: DonutSlice[]
  centerLabel: string
  centerValue: React.ReactNode
  className?: string
  height?: number
}) {
  const total = slices.reduce((sum, s) => sum + s.value, 0)
  const shown = slices.filter((s) => s.value > 0)
  return (
    <div className={cn('flex items-center gap-4', className)}>
      <div className="relative shrink-0" style={{ width: height, height }}>
        <ResponsiveContainer width="100%" height="100%">
          <PieChart>
            <Pie
              data={shown.length ? shown : [{ key: 'empty', label: '无数据', value: 1, color: 'var(--muted)' }]}
              dataKey="value"
              nameKey="label"
              innerRadius="68%"
              outerRadius="92%"
              paddingAngle={shown.length > 1 ? 2 : 0}
              stroke="none"
              isAnimationActive={false}
            >
              {(shown.length ? shown : [{ key: 'empty', color: 'var(--muted)' }]).map((s) => (
                <Cell key={s.key} fill={s.color} />
              ))}
            </Pie>
            {shown.length > 0 && (
              <Tooltip
                formatter={(value: number, name: string) => [`${value}（${total ? ((value / total) * 100).toFixed(1) : 0}%）`, name]}
                contentStyle={{ background: 'var(--card)', border: '1px solid var(--border)', borderRadius: 6, fontSize: 12 }}
              />
            )}
          </PieChart>
        </ResponsiveContainer>
        <div className="pointer-events-none absolute inset-0 flex flex-col items-center justify-center">
          <span className="num text-2xl font-semibold leading-none text-fg">{centerValue}</span>
          <span className="mt-1 text-[11px] text-fg-subtle">{centerLabel}</span>
        </div>
      </div>
      <ul className="min-w-0 flex-1 space-y-1.5">
        {slices.map((s) => (
          <li key={s.key} className="flex items-center gap-2 text-xs">
            <span className="h-2.5 w-2.5 shrink-0 rounded-sm" style={{ background: s.color }} aria-hidden />
            <span className="min-w-0 flex-1 truncate text-fg-muted">{s.label}</span>
            <span className="num text-fg">{s.value}</span>
            <span className="num w-12 text-right text-fg-subtle">{total ? `${((s.value / total) * 100).toFixed(0)}%` : '—'}</span>
          </li>
        ))}
      </ul>
    </div>
  )
}
