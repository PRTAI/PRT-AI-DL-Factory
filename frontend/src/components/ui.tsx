/**
 * Minimal shadcn-flavoured primitive set (Radix under the hood, Tailwind on top).
 * Kept in one module so feature code has a single import surface.
 */
import * as React from 'react'
import * as SelectPrimitive from '@radix-ui/react-select'
import * as DialogPrimitive from '@radix-ui/react-dialog'
import * as TabsPrimitive from '@radix-ui/react-tabs'
import * as TooltipPrimitive from '@radix-ui/react-tooltip'
import * as SwitchPrimitive from '@radix-ui/react-switch'
import * as CheckboxPrimitive from '@radix-ui/react-checkbox'
import * as SeparatorPrimitive from '@radix-ui/react-separator'
import { Check, ChevronDown, Loader2, X } from 'lucide-react'
import { cn } from '@/lib/utils'

/* -------------------------------------------------------------------------- */
/* Button                                                                      */
/* -------------------------------------------------------------------------- */
type ButtonVariant = 'primary' | 'secondary' | 'ghost' | 'danger' | 'success' | 'outline'
type ButtonSize = 'sm' | 'md' | 'lg' | 'icon'

const BUTTON_VARIANTS: Record<ButtonVariant, string> = {
  primary: 'bg-primary text-primary-fg hover:brightness-115',
  secondary: 'border border-border bg-card text-fg hover:border-primary hover:text-primary',
  outline: 'border border-border bg-card text-fg hover:border-primary hover:text-primary',
  ghost: 'bg-transparent text-fg-muted hover:bg-muted hover:text-fg',
  danger: 'bg-danger text-white hover:brightness-110',
  success: 'bg-success text-white hover:brightness-110',
}

const BUTTON_SIZES: Record<ButtonSize, string> = {
  sm: 'h-8 px-3 text-xs gap-1.5',
  md: 'h-9 px-4 text-sm gap-2',
  lg: 'h-11 px-6 text-sm gap-2',
  icon: 'h-9 w-9 justify-center',
}

export interface ButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant
  size?: ButtonSize
  loading?: boolean
}

export const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { className, variant = 'secondary', size = 'md', loading, disabled, children, ...props },
  ref,
) {
  return (
    <button
      ref={ref}
      disabled={disabled || loading}
      className={cn(
        'inline-flex items-center rounded-md font-medium transition-[background-color,color,border-color,filter] duration-150',
        'disabled:pointer-events-none disabled:opacity-50 whitespace-nowrap',
        BUTTON_VARIANTS[variant],
        BUTTON_SIZES[size],
        className,
      )}
      {...props}
    >
      {loading && <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden />}
      {children}
    </button>
  )
})

/* -------------------------------------------------------------------------- */
/* Card                                                                        */
/* -------------------------------------------------------------------------- */
export function Card({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return <div className={cn('card', className)} {...props} />
}

export function CardHeader({
  title,
  description,
  actions,
  icon,
  className,
}: {
  title: React.ReactNode
  description?: React.ReactNode
  actions?: React.ReactNode
  icon?: React.ReactNode
  className?: string
}) {
  return (
    <div className={cn('flex items-start justify-between gap-3 border-b border-border px-4 py-3', className)}>
      <div className="flex min-w-0 items-start gap-2.5">
        {icon && <div className="mt-0.5 text-fg-muted">{icon}</div>}
        <div className="min-w-0">
          <h3 className="truncate text-sm font-semibold text-fg">{title}</h3>
          {description && <p className="mt-0.5 text-xs text-fg-muted">{description}</p>}
        </div>
      </div>
      {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
    </div>
  )
}

export function CardBody({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return <div className={cn('p-4', className)} {...props} />
}

/* -------------------------------------------------------------------------- */
/* Badge                                                                       */
/* -------------------------------------------------------------------------- */
type BadgeTone = 'neutral' | 'primary' | 'success' | 'warning' | 'danger' | 'info'

const BADGE_TONES: Record<BadgeTone, string> = {
  neutral: 'bg-muted text-fg-muted border-transparent',
  primary: 'bg-primary-soft text-primary border-transparent',
  success: 'bg-success-soft text-success border-transparent',
  warning: 'bg-warning-soft text-warning border-transparent',
  danger: 'bg-danger-soft text-danger border-transparent',
  info: 'bg-info-soft text-info border-transparent',
}

export function Badge({
  tone = 'neutral',
  className,
  children,
  ...props
}: React.HTMLAttributes<HTMLSpanElement> & { tone?: BadgeTone }) {
  return (
    <span
      className={cn(
        'inline-flex items-center gap-1 whitespace-nowrap rounded-md border px-1.5 py-0.5 text-[11px] font-medium leading-4',
        BADGE_TONES[tone],
        className,
      )}
      {...props}
    >
      {children}
    </span>
  )
}

/* -------------------------------------------------------------------------- */
/* Inputs                                                                      */
/* -------------------------------------------------------------------------- */
const FIELD_BASE =
  'w-full rounded-md border border-border bg-card px-3 text-sm text-fg placeholder:text-fg-subtle ' +
  'transition-colors hover:border-border-strong focus:border-primary focus:outline-none disabled:opacity-60'

export const Input = React.forwardRef<HTMLInputElement, React.InputHTMLAttributes<HTMLInputElement>>(
  function Input({ className, ...props }, ref) {
    return <input ref={ref} className={cn(FIELD_BASE, 'h-9', className)} {...props} />
  },
)

export const Textarea = React.forwardRef<
  HTMLTextAreaElement,
  React.TextareaHTMLAttributes<HTMLTextAreaElement>
>(function Textarea({ className, ...props }, ref) {
  return <textarea ref={ref} className={cn(FIELD_BASE, 'min-h-20 py-2 leading-relaxed', className)} {...props} />
})

export function NumberInput({
  value,
  onChange,
  className,
  step,
  ...props
}: Omit<React.InputHTMLAttributes<HTMLInputElement>, 'value' | 'onChange'> & {
  value: number | null | undefined
  onChange: (value: number | null) => void
}) {
  return (
    <input
      type="number"
      step={step ?? 'any'}
      value={value ?? ''}
      onChange={(event) => {
        const raw = event.target.value
        onChange(raw === '' ? null : Number(raw))
      }}
      className={cn(FIELD_BASE, 'num h-9', className)}
      {...props}
    />
  )
}

/**
 * 能直接输 `1e-6` 的数字框，给学习率这种量级很小的参数用。
 *
 * 原生 <input type="number"> 会把 1e-6 渲染成 0.000001，光标一进去随便敲一下就变成
 * 1.000001 —— 真机上一次 GRPO 就是这么把学习率提交成 1.0 的。
 */
export function ScientificInput({
  value,
  onChange,
  className,
  ...props
}: Omit<React.InputHTMLAttributes<HTMLInputElement>, 'value' | 'onChange'> & {
  value: number | null | undefined
  onChange: (value: number | null) => void
}) {
  const format = (v: number | null | undefined) =>
    v === null || v === undefined ? '' : Math.abs(v) < 1e-3 && v !== 0 ? v.toExponential().replace('e-', 'e-').replace('+', '') : String(v)
  const [text, setText] = React.useState(() => format(value))
  const [invalid, setInvalid] = React.useState(false)
  // 外部值变了（比如切换算法重置默认值）就同步显示；自己正在输入时不打断
  React.useEffect(() => {
    if (!invalid && Number(text) !== value) setText(format(value))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value])
  return (
    <input
      type="text"
      inputMode="decimal"
      value={text}
      onChange={(event) => {
        const raw = event.target.value.trim()
        setText(event.target.value)
        if (raw === '') {
          setInvalid(false)
          onChange(null)
          return
        }
        const parsed = Number(raw)
        const ok = Number.isFinite(parsed)
        setInvalid(!ok)
        if (ok) onChange(parsed)
      }}
      aria-invalid={invalid || undefined}
      className={cn(FIELD_BASE, 'num h-9', invalid && 'border-danger focus:border-danger', className)}
      {...props}
    />
  )
}

export function Field({
  label,
  hint,
  error,
  required,
  htmlFor,
  children,
  className,
}: {
  label: React.ReactNode
  hint?: React.ReactNode
  error?: string | null
  required?: boolean
  htmlFor?: string
  children: React.ReactNode
  className?: string
}) {
  return (
    <div className={cn('space-y-1.5', className)}>
      <label htmlFor={htmlFor} className="flex items-center gap-1 text-xs font-medium text-fg-muted">
        {label}
        {required && <span className="text-danger">*</span>}
      </label>
      {children}
      {error ? (
        <p className="text-xs text-danger">{error}</p>
      ) : (
        hint && <p className="text-xs leading-relaxed text-fg-subtle">{hint}</p>
      )}
    </div>
  )
}

/* -------------------------------------------------------------------------- */
/* Select                                                                      */
/* -------------------------------------------------------------------------- */
export interface SelectOption {
  value: string
  label: string
  description?: string
  disabled?: boolean
}

export function Select({
  value,
  onValueChange,
  options,
  placeholder = '请选择',
  className,
  disabled,
  id,
}: {
  value: string
  onValueChange: (value: string) => void
  options: SelectOption[]
  placeholder?: string
  className?: string
  disabled?: boolean
  id?: string
}) {
  return (
    <SelectPrimitive.Root value={value} onValueChange={onValueChange} disabled={disabled}>
      <SelectPrimitive.Trigger
        id={id}
        className={cn(
          FIELD_BASE,
          'flex h-9 items-center justify-between gap-2 data-[placeholder]:text-fg-subtle',
          // 选中项名字长了要截断而不是换行——换行会把 h-9 的高度撑破
          'overflow-hidden [&>span:first-child]:truncate',
          className,
        )}
      >
        <SelectPrimitive.Value placeholder={placeholder} />
        <SelectPrimitive.Icon>
          <ChevronDown className="h-4 w-4 text-fg-subtle" aria-hidden />
        </SelectPrimitive.Icon>
      </SelectPrimitive.Trigger>
      <SelectPrimitive.Portal>
        <SelectPrimitive.Content
          position="popper"
          sideOffset={4}
          className="z-50 max-h-72 min-w-[var(--radix-select-trigger-width)] overflow-hidden rounded-lg border border-border bg-card shadow-xl"
        >
          <SelectPrimitive.Viewport className="p-1">
            {options.map((option) => (
              <SelectPrimitive.Item
                key={option.value}
                value={option.value}
                disabled={option.disabled}
                className="relative flex cursor-pointer select-none flex-col rounded-md px-2 py-1.5 pr-7 text-sm text-fg outline-none data-[highlighted]:bg-muted data-[disabled]:opacity-50"
              >
                <SelectPrimitive.ItemText>{option.label}</SelectPrimitive.ItemText>
                {option.description && (
                  <span className="text-[11px] text-fg-subtle">{option.description}</span>
                )}
                <SelectPrimitive.ItemIndicator className="absolute right-2 top-2">
                  <Check className="h-3.5 w-3.5 text-primary" aria-hidden />
                </SelectPrimitive.ItemIndicator>
              </SelectPrimitive.Item>
            ))}
          </SelectPrimitive.Viewport>
        </SelectPrimitive.Content>
      </SelectPrimitive.Portal>
    </SelectPrimitive.Root>
  )
}

/* -------------------------------------------------------------------------- */
/* Switch / Checkbox                                                           */
/* -------------------------------------------------------------------------- */
export function Switch({
  checked,
  onCheckedChange,
  id,
  disabled,
}: {
  checked: boolean
  onCheckedChange: (checked: boolean) => void
  id?: string
  disabled?: boolean
}) {
  return (
    <SwitchPrimitive.Root
      id={id}
      checked={checked}
      onCheckedChange={onCheckedChange}
      disabled={disabled}
      className={cn(
        'relative h-5 w-9 shrink-0 rounded-full border border-border transition-colors',
        checked ? 'bg-primary' : 'bg-muted',
        disabled && 'opacity-50',
      )}
    >
      <SwitchPrimitive.Thumb className="block h-4 w-4 translate-x-0.5 rounded-full bg-white shadow transition-transform data-[state=checked]:translate-x-4" />
    </SwitchPrimitive.Root>
  )
}

export function ToggleRow({
  label,
  hint,
  checked,
  onCheckedChange,
  disabled,
}: {
  label: string
  hint?: string
  checked: boolean
  onCheckedChange: (checked: boolean) => void
  disabled?: boolean
}) {
  const id = React.useId()
  return (
    <div className="flex items-start justify-between gap-4 rounded-lg border border-border bg-bg-elevated px-3 py-2.5">
      <div className="min-w-0">
        <label htmlFor={id} className="text-sm font-medium text-fg">
          {label}
        </label>
        {hint && <p className="mt-0.5 text-xs leading-relaxed text-fg-subtle">{hint}</p>}
      </div>
      <Switch id={id} checked={checked} onCheckedChange={onCheckedChange} disabled={disabled} />
    </div>
  )
}

export function Checkbox({
  checked,
  onCheckedChange,
  id,
}: {
  checked: boolean
  onCheckedChange: (checked: boolean) => void
  id?: string
}) {
  return (
    <CheckboxPrimitive.Root
      id={id}
      checked={checked}
      onCheckedChange={(state) => onCheckedChange(state === true)}
      className="flex h-4 w-4 items-center justify-center rounded border border-border-strong bg-bg-elevated data-[state=checked]:border-primary data-[state=checked]:bg-primary"
    >
      <CheckboxPrimitive.Indicator>
        <Check className="h-3 w-3 text-white" aria-hidden />
      </CheckboxPrimitive.Indicator>
    </CheckboxPrimitive.Root>
  )
}

/* -------------------------------------------------------------------------- */
/* Dialog / Tabs / Tooltip / Separator                                         */
/* -------------------------------------------------------------------------- */
export function Dialog({
  open,
  onOpenChange,
  title,
  description,
  children,
  footer,
  width = 'max-w-lg',
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  title: React.ReactNode
  description?: React.ReactNode
  children: React.ReactNode
  footer?: React.ReactNode
  width?: string
}) {
  return (
    <DialogPrimitive.Root open={open} onOpenChange={onOpenChange}>
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay className="fixed inset-0 z-50 bg-black/60 backdrop-blur-sm" />
        <DialogPrimitive.Content
          className={cn(
            'fixed left-1/2 top-1/2 z-50 w-[calc(100vw-2rem)] -translate-x-1/2 -translate-y-1/2',
            'max-h-[calc(100vh-4rem)] overflow-y-auto rounded-xl border border-border bg-card shadow-2xl',
            width,
          )}
        >
          <div className="flex items-start justify-between gap-3 border-b border-border px-4 py-3">
            <div>
              <DialogPrimitive.Title className="text-sm font-semibold text-fg">{title}</DialogPrimitive.Title>
              {description && (
                <DialogPrimitive.Description className="mt-0.5 text-xs text-fg-muted">
                  {description}
                </DialogPrimitive.Description>
              )}
            </div>
            <DialogPrimitive.Close asChild>
              <Button variant="ghost" size="icon" aria-label="关闭">
                <X className="h-4 w-4" aria-hidden />
              </Button>
            </DialogPrimitive.Close>
          </div>
          <div className="p-4">{children}</div>
          {footer && <div className="flex justify-end gap-2 border-t border-border px-4 py-3">{footer}</div>}
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  )
}

export const Tabs = TabsPrimitive.Root

export function TabsList({ className, ...props }: React.ComponentProps<typeof TabsPrimitive.List>) {
  return (
    <TabsPrimitive.List
      className={cn('flex items-center gap-5 border-b border-border', className)}
      {...props}
    />
  )
}

export function TabsTrigger({ className, ...props }: React.ComponentProps<typeof TabsPrimitive.Trigger>) {
  return (
    <TabsPrimitive.Trigger
      className={cn(
        'relative -mb-px border-b-2 border-transparent px-0.5 pb-2.5 pt-1 text-[13px] text-fg-muted transition-colors',
        'hover:text-fg data-[state=active]:border-primary data-[state=active]:font-medium data-[state=active]:text-primary',
        className,
      )}
      {...props}
    />
  )
}

export const TabsContent = TabsPrimitive.Content

export function TooltipProvider({ children }: { children: React.ReactNode }) {
  return <TooltipPrimitive.Provider delayDuration={200}>{children}</TooltipPrimitive.Provider>
}

export function Tooltip({ label, children }: { label: React.ReactNode; children: React.ReactNode }) {
  return (
    <TooltipPrimitive.Root>
      <TooltipPrimitive.Trigger asChild>{children}</TooltipPrimitive.Trigger>
      <TooltipPrimitive.Portal>
        <TooltipPrimitive.Content
          sideOffset={6}
          className="z-50 max-w-xs rounded-md border border-border bg-card px-2.5 py-1.5 text-xs leading-relaxed text-fg shadow-xl"
        >
          {label}
          <TooltipPrimitive.Arrow className="fill-[var(--card)]" />
        </TooltipPrimitive.Content>
      </TooltipPrimitive.Portal>
    </TooltipPrimitive.Root>
  )
}

export function Separator({ className, ...props }: React.ComponentProps<typeof SeparatorPrimitive.Root>) {
  return <SeparatorPrimitive.Root className={cn('bg-border', 'h-px w-full', className)} {...props} />
}

/* -------------------------------------------------------------------------- */
/* Feedback                                                                    */
/* -------------------------------------------------------------------------- */
export function Progress({
  value,
  tone = 'primary',
  className,
  showLabel,
}: {
  value: number
  tone?: BadgeTone
  className?: string
  showLabel?: boolean
}) {
  const clamped = Math.max(0, Math.min(100, value || 0))
  const toneClass = {
    neutral: 'bg-fg-subtle',
    primary: 'bg-primary',
    success: 'bg-success',
    warning: 'bg-warning',
    danger: 'bg-danger',
    info: 'bg-info',
  }[tone]
  return (
    <div className={cn('flex items-center gap-2', className)}>
      <div
        className="h-1.5 flex-1 overflow-hidden rounded-full bg-muted"
        role="progressbar"
        aria-valuenow={Math.round(clamped)}
        aria-valuemin={0}
        aria-valuemax={100}
      >
        <div className={cn('h-full rounded-full transition-[width] duration-500', toneClass)} style={{ width: `${clamped}%` }} />
      </div>
      {showLabel && <span className="num w-10 text-right text-[11px] text-fg-muted">{clamped.toFixed(0)}%</span>}
    </div>
  )
}

export function Spinner({ className }: { className?: string }) {
  return <Loader2 className={cn('h-4 w-4 animate-spin text-fg-muted', className)} aria-hidden />
}

export function Skeleton({ className }: { className?: string }) {
  return <div className={cn('animate-pulse rounded-md bg-muted', className)} />
}

export function EmptyState({
  icon,
  title,
  description,
  action,
  className,
}: {
  icon?: React.ReactNode
  title: string
  description?: React.ReactNode
  action?: React.ReactNode
  className?: string
}) {
  return (
    <div className={cn('flex flex-col items-center justify-center gap-2 px-6 py-12 text-center', className)}>
      {icon && <div className="text-fg-subtle">{icon}</div>}
      <p className="text-sm font-medium text-fg">{title}</p>
      {description && <p className="max-w-md text-xs leading-relaxed text-fg-muted">{description}</p>}
      {action && <div className="mt-2">{action}</div>}
    </div>
  )
}

export function ErrorBox({ message, className }: { message: string; className?: string }) {
  return (
    <div
      role="alert"
      className={cn('rounded-lg border border-danger/40 bg-danger-soft px-3 py-2 text-xs text-danger', className)}
    >
      {message}
    </div>
  )
}

/* -------------------------------------------------------------------------- */
/* Table                                                                       */
/* -------------------------------------------------------------------------- */
export function Table({ className, ...props }: React.TableHTMLAttributes<HTMLTableElement>) {
  return (
    <div className="w-full overflow-x-auto">
      <table className={cn('w-full border-collapse text-sm', className)} {...props} />
    </div>
  )
}

export function Th({ className, ...props }: React.ThHTMLAttributes<HTMLTableCellElement>) {
  return (
    <th
      className={cn(
        'border-b border-border bg-muted px-3 py-2.5 text-left text-xs font-medium text-fg-muted',
        className,
      )}
      {...props}
    />
  )
}

export function Td({ className, ...props }: React.TdHTMLAttributes<HTMLTableCellElement>) {
  return <td className={cn('border-b border-border px-3 py-2.5 align-middle text-fg', className)} {...props} />
}

export function Tr({ className, ...props }: React.HTMLAttributes<HTMLTableRowElement>) {
  return <tr className={cn('transition-colors hover:bg-card-hover', className)} {...props} />
}

/** 状态圆点 + 文字：参考控制台用它表达状态，比整块彩色底更克制。
 *  文字本身已说明状态，颜色只是辅助，不违反「不能只靠颜色传达信息」。 */
export function StatusDot({
  tone = 'neutral',
  label,
  pulse,
  className,
}: {
  tone?: BadgeTone
  label: React.ReactNode
  pulse?: boolean
  className?: string
}) {
  const dot = {
    neutral: 'bg-fg-subtle',
    primary: 'bg-primary',
    success: 'bg-success',
    warning: 'bg-warning',
    danger: 'bg-danger',
    info: 'bg-info',
  }[tone]
  return (
    <span className={cn('inline-flex items-center gap-1.5 whitespace-nowrap text-[13px] text-fg', className)}>
      <span className={cn('h-1.5 w-1.5 shrink-0 rounded-full', dot, pulse && 'pulse-live')} aria-hidden />
      {label}
    </span>
  )
}

/** 表格里的操作项：参考图用蓝色文字链接，而不是一排按钮。 */
export function LinkButton({
  className,
  tone = 'primary',
  ...props
}: React.ButtonHTMLAttributes<HTMLButtonElement> & { tone?: 'primary' | 'danger' }) {
  return (
    <button
      type="button"
      className={cn(
        'text-xs transition-colors hover:underline hover:underline-offset-2 disabled:cursor-not-allowed disabled:opacity-40 disabled:no-underline',
        tone === 'danger' ? 'text-danger' : 'text-primary',
        className,
      )}
      {...props}
    />
  )
}

/* -------------------------------------------------------------------------- */
/* Stat tile                                                                   */
/* -------------------------------------------------------------------------- */
export function Stat({
  label,
  value,
  unit,
  hint,
  tone = 'neutral',
  icon,
  className,
}: {
  label: string
  value: React.ReactNode
  unit?: string
  hint?: React.ReactNode
  tone?: BadgeTone
  icon?: React.ReactNode
  className?: string
}) {
  const toneText = {
    neutral: 'text-fg',
    primary: 'text-primary',
    success: 'text-success',
    warning: 'text-warning',
    danger: 'text-danger',
    info: 'text-info',
  }[tone]
  return (
    <div className={cn('card px-3.5 py-3', className)}>
      <div className="flex items-center justify-between gap-2">
        <span className="text-xs font-medium text-fg-muted">{label}</span>
        {icon && <span className="text-fg-subtle">{icon}</span>}
      </div>
      <div className="mt-1.5 flex items-baseline gap-1">
        <span className={cn('num text-2xl font-semibold leading-none', toneText)}>{value}</span>
        {unit && <span className="text-xs text-fg-subtle">{unit}</span>}
      </div>
      {hint && <div className="mt-1.5 text-[11px] leading-relaxed text-fg-subtle">{hint}</div>}
    </div>
  )
}

export function KeyValue({ label, children, className }: { label: string; children: React.ReactNode; className?: string }) {
  return (
    <div className={cn('flex items-start justify-between gap-4 py-1.5 text-sm', className)}>
      <span className="shrink-0 text-xs text-fg-muted">{label}</span>
      <span className="min-w-0 break-all text-right text-xs text-fg">{children}</span>
    </div>
  )
}

export function SectionTitle({ children, className }: { children: React.ReactNode; className?: string }) {
  return (
    <h4 className={cn('mb-2 text-[11px] font-semibold uppercase tracking-wider text-fg-subtle', className)}>
      {children}
    </h4>
  )
}
