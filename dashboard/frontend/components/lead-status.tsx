import type { LeadStatus } from "@/lib/types"
import { cn } from "@/lib/utils"

export const LEAD_STATUS_LABEL: Record<LeadStatus, string> = {
  new: "New",
  calling: "Calling",
  interested: "Interested",
  callback: "Callback",
  not_interested: "Not interested",
  needs_review: "Needs review",
  voicemail: "Voicemail",
  no_answer: "No answer",
  bad_line: "Bad line",
  bad_number: "Bad number",
  do_not_contact: "Do not contact",
  not_a_fit: "Not a fit",
  failed: "Failed",
  answered: "Answered",
  transferred: "Transferred",
  texted: "Texted",
}

/** Statuses in the filter (legacy / later-phase ones only when a lead has them). */
export const LEAD_STATUSES = Object.keys(LEAD_STATUS_LABEL) as LeadStatus[]
export const RARE_STATUSES: LeadStatus[] = ["answered", "texted"]

const TONE: Record<LeadStatus, { pill: string; dot: string }> = {
  new: { pill: "border-border text-muted-foreground", dot: "bg-muted-foreground" },
  calling: { pill: "border-transparent bg-signal-soft text-signal", dot: "dot-pulse bg-signal text-signal" },
  interested: { pill: "border-transparent bg-signal text-signal-fg", dot: "bg-signal-fg" },
  callback: { pill: "border-transparent bg-warning/10 text-warning", dot: "bg-warning" },
  not_interested: { pill: "border-border text-muted-foreground", dot: "bg-faint" },
  needs_review: { pill: "border-warning/40 text-warning", dot: "bg-warning" },
  voicemail: { pill: "border-border text-foreground", dot: "bg-signal" },
  no_answer: { pill: "border-border text-muted-foreground", dot: "bg-faint" },
  bad_line: { pill: "border-border text-muted-foreground", dot: "bg-warning" },
  bad_number: { pill: "border-border text-faint", dot: "bg-danger" },
  do_not_contact: { pill: "border-transparent bg-danger-soft text-danger", dot: "bg-danger" },
  not_a_fit: { pill: "border-border text-faint", dot: "bg-faint" },
  failed: { pill: "border-transparent bg-danger-soft text-danger", dot: "bg-danger" },
  answered: { pill: "border-transparent bg-signal-soft text-signal", dot: "bg-signal" },
  transferred: { pill: "border-transparent bg-signal text-signal-fg", dot: "bg-signal-fg" },
  texted: { pill: "border-border text-foreground", dot: "bg-muted-foreground" },
}

export function LeadStatusTag({ status, className }: { status: LeadStatus; className?: string }) {
  const t = TONE[status] ?? TONE.new
  return (
    <span className={cn("inline-flex h-5 items-center gap-1.5 rounded-full border px-2 text-[11px] font-medium whitespace-nowrap", t.pill, className)}>
      <span className={cn("relative size-1.5 rounded-full", t.dot)} />
      {LEAD_STATUS_LABEL[status] ?? status}
    </span>
  )
}
