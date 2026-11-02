import type { Call, Outcome } from "@/lib/types"
import { cn } from "@/lib/utils"

export const OUTCOME_LABEL: Record<Outcome, string> = {
  interested: "Interested",
  not_now: "Not now",
  gatekeeper: "Gatekeeper",
  not_interested: "Not interested",
  do_not_contact: "Do not contact",
  wrong_number: "Wrong number",
  voicemail: "Voicemail",
  bad_line: "Bad line",
  no_answer: "No answer",
  failed: "Failed",
  needs_review: "Needs review",
  transferred: "Transferred",
  transfer_declined: "Supervisor busy",
  transfer_failed: "Transfer failed",
}

/** Outcomes that can be set by hand on the lead page (the AI's, plus transferred and needs review). */
export const MANUAL_OUTCOMES: Outcome[] = ["interested", "not_now", "gatekeeper", "not_interested", "do_not_contact", "wrong_number", "transferred", "needs_review"]

export const SOURCE_LABEL: Record<string, string> = {
  ai: "AI, during the call",
  transcript: "from the transcript",
  call: "from how the call ended",
  manual: "set by hand",
  transfer: "from the transfer to the supervisor",
}

const TONE: Partial<Record<Outcome, string>> = {
  interested: "border-transparent bg-signal text-signal-fg",
  not_now: "border-transparent bg-warning/10 text-warning",
  gatekeeper: "border-transparent bg-warning/10 text-warning",
  do_not_contact: "border-transparent bg-danger-soft text-danger",
  failed: "border-transparent bg-danger-soft text-danger",
  needs_review: "border-warning/40 text-warning",
  voicemail: "border-border text-foreground",
  transferred: "border-transparent bg-signal text-signal-fg",
  transfer_declined: "border-transparent bg-warning/10 text-warning",
  transfer_failed: "border-transparent bg-danger-soft text-danger",
}

export function OutcomeTag({ outcome, messageLeft, className }: { outcome: Outcome; messageLeft?: boolean; className?: string }) {
  return (
    <span className={cn("inline-flex h-5 items-center rounded-full border px-2 text-[11px] font-medium whitespace-nowrap",
      TONE[outcome] ?? "border-border text-muted-foreground", className)}>
      {OUTCOME_LABEL[outcome] ?? outcome}
      {outcome === "voicemail" && messageLeft && <span className="ml-1 font-normal text-muted-foreground">· message left</span>}
    </span>
  )
}

/** "Thu Oct 29, 3:00 PM" in the given IANA zone (the lead's), or the browser's. */
export function formatInZone(iso: string, tz?: string | null, withZone = false): string {
  const opts: Intl.DateTimeFormatOptions = { weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }
  try {
    return new Intl.DateTimeFormat("en-US", { ...opts, timeZone: tz || undefined, timeZoneName: withZone ? "short" : undefined }).format(new Date(iso))
  } catch {
    return new Date(iso).toLocaleString()
  }
}

export function hasOutcome(c: Pick<Call, "outcome">): c is Call & { outcome: Outcome } {
  return !!c.outcome
}
