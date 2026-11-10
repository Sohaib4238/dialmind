import type { CallResult, CallStatus } from "./types"

export const E164 = /^\+[1-9]\d{1,14}$/

/** +17865550100 -> +1 786 555 0100 (US numbers); other numbers unchanged. */
export function formatPhone(n: string): string {
  const m = /^\+1(\d{3})(\d{3})(\d{4})$/.exec(n)
  return m ? `+1 ${m[1]} ${m[2]} ${m[3]}` : n
}

/** 65.2 -> "1:05"; 3700 -> "1:01:40" */
export function formatDuration(seconds: number | null | undefined): string {
  if (seconds == null || !isFinite(seconds) || seconds < 0) return "–"
  const s = Math.floor(seconds)
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const sec = String(s % 60).padStart(2, "0")
  return h ? `${h}:${String(m).padStart(2, "0")}:${sec}` : `${m}:${sec}`
}

export function formatClock(iso: string): string {
  // 24h, compact: fits the transcript's time column on one line.
  return new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23" })
}

/** 02:52:10.597 — for the call debug table. */
export function formatClockMs(iso: string): string {
  const d = new Date(iso)
  return `${formatClock(iso)}.${String(d.getMilliseconds()).padStart(3, "0")}`
}

export function formatDateTime(iso: string): string {
  const d = new Date(iso)
  const today = new Date()
  const yesterday = new Date(today)
  yesterday.setDate(today.getDate() - 1)
  const time = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
  if (d.toDateString() === today.toDateString()) return `Today, ${time}`
  if (d.toDateString() === yesterday.toDateString()) return `Yesterday, ${time}`
  return `${d.toLocaleDateString([], { month: "short", day: "numeric" })}, ${time}`
}

/** 0.512 -> "512ms", 1.48 -> "1.48s" */
export function formatSeconds(v: unknown): string | null {
  if (typeof v !== "number" || !isFinite(v) || v < 0) return null
  return v < 1 ? `${Math.round(v * 1000)}ms` : `${v.toFixed(2)}s`
}

export const STATUS_LABEL: Record<CallStatus, string> = {
  dialing: "Dialing",
  ringing: "Ringing",
  answered: "Live",
  ended: "Ended",
  failed: "Failed",
}

export const RESULT_LABEL: Record<Exclude<CallResult, null>, string> = {
  answered: "Answered",
  voicemail: "Voicemail",
  no_answer: "No answer",
  busy: "Busy",
  bad_line: "Bad line",
  failed: "Failed",
  trunk_rejected: "Refused (not rung)",
}

export const isActive = (s: CallStatus) => s === "dialing" || s === "ringing" || s === "answered"

/** "just now", "5 min ago", "3 h ago", "yesterday", "4 days ago" */
export function timeAgo(iso: string): string {
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000)
  if (s < 60) return "just now"
  if (s < 3600) return `${Math.floor(s / 60)} min ago`
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`
  const d = Math.floor(s / 86400)
  return d === 1 ? "yesterday" : `${d} days ago`
}
