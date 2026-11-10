import { cn } from "@/lib/utils"
import type { CallResult } from "@/lib/types"
import { RESULT_LABEL } from "@/lib/format"

export function PageHeader({
  title,
  description,
  actions,
}: {
  title: string
  description?: string
  actions?: React.ReactNode
}) {
  return (
    <div className="mb-8 flex items-end justify-between gap-6">
      <div>
        <h1 className="text-[20px] leading-7">{title}</h1>
        {description && <p className="mt-1 text-[13px] text-muted-foreground">{description}</p>}
      </div>
      {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
    </div>
  )
}

export function Panel({ className, ...props }: React.ComponentProps<"section">) {
  return <section className={cn("rounded-lg border border-border bg-surface", className)} {...props} />
}

export function PanelHeader({ title, right }: { title: React.ReactNode; right?: React.ReactNode }) {
  return (
    <div className="flex h-11 items-center justify-between border-b border-border px-4">
      <div className="text-[13px] font-medium">{title}</div>
      {right}
    </div>
  )
}

export function Kbd({ children }: { children: React.ReactNode }) {
  return (
    <kbd className="inline-flex h-[18px] min-w-[18px] items-center justify-center rounded border border-line-strong bg-surface-2 px-1 font-mono text-[10px] text-muted-foreground">
      {children}
    </kbd>
  )
}

export function Tag({ children, tone = "neutral" }: { children: React.ReactNode; tone?: "neutral" | "signal" | "danger" }) {
  return (
    <span
      className={cn(
        "inline-flex h-5 items-center rounded-full border px-2 text-[11px] font-medium",
        tone === "neutral" && "border-border text-muted-foreground",
        tone === "signal" && "border-transparent bg-signal-soft text-signal",
        tone === "danger" && "border-transparent bg-danger-soft text-danger",
      )}
    >
      {children}
    </span>
  )
}

export function ResultTag({ result, active }: { result: CallResult; active?: boolean }) {
  if (active) return <Tag tone="signal">In progress</Tag>
  if (!result) return <Tag>–</Tag>
  return <Tag tone={result === "answered" ? "neutral" : result === "failed" || result === "trunk_rejected" ? "danger" : result === "voicemail" ? "signal" : "neutral"}>{RESULT_LABEL[result]}</Tag>
}

export function EmptyState({ children, action }: { children: React.ReactNode; action?: React.ReactNode }) {
  return (
    <div className="flex flex-col items-start gap-3 px-4 py-10 text-[13px] text-muted-foreground">
      <p>{children}</p>
      {action}
    </div>
  )
}

export function SkeletonLine({ className }: { className?: string }) {
  return <div className={cn("skeleton-shimmer h-3 rounded", className)} />
}
