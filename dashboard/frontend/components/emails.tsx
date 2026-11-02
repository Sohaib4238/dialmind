"use client"

import { ChevronDown, Mail, RotateCw } from "lucide-react"
import { AnimatePresence, motion } from "motion/react"
import { useEffect, useState } from "react"
import { toast } from "sonner"

import { Tag } from "@/components/common"
import { Button } from "@/components/ui/button"
import { useIsAdmin } from "@/components/login-gate"
import { api, ApiError } from "@/lib/api"
import { formatDateTime } from "@/lib/format"
import type { Email, EmailState } from "@/lib/types"
import { cn } from "@/lib/utils"

export const TEMPLATE_LABEL: Record<Email["template"], string> = {
  interested: "Interested",
  callback: "Not now / callback",
  details: "Details",
  voicemail_followup: "After voicemail",
}

export function EmailStatusTag({ status }: { status: Email["status"] }) {
  if (status === "sent") return <Tag tone="signal">Sent</Tag>
  if (status === "failed") return <Tag tone="danger">Failed</Tag>
  return <Tag>Not sent</Tag>
}

/** Emails for a lead or a call: time, template, status; click a row for the subject and text. */
export function EmailList({ emails, onChanged }: { emails: Email[]; onChanged?: () => void }) {
  const admin = useIsAdmin()
  const [open, setOpen] = useState<number | null>(null)
  const [busy, setBusy] = useState<number | null>(null)

  const retry = async (e: Email) => {
    setBusy(e.id)
    try {
      const r = await api.retryEmail(e.id)
      if (r.status === "sent") toast.success(`Email sent to ${r.to_email}`)
      else toast.warning(r.status === "failed" ? "Sending failed again" : "Still not sent", { description: r.detail })
      onChanged?.()
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : "Couldn't send the email")
    } finally {
      setBusy(null)
    }
  }

  return (
    <ul className="divide-y divide-border">
      {emails.map((e) => (
        <li key={e.id}>
          <button
            onClick={() => setOpen((x) => (x === e.id ? null : e.id))}
            className="grid w-full grid-cols-[150px_130px_76px_1fr_16px] items-center gap-4 px-4 py-2.5 text-left text-[13px] transition-colors duration-[120ms] hover:bg-surface-2"
          >
            <span className="text-muted-foreground tnum">{formatDateTime(e.sent_at ?? e.created_at ?? "")}</span>
            <span className="truncate">{TEMPLATE_LABEL[e.template] ?? e.template}</span>
            <span><EmailStatusTag status={e.status} /></span>
            <span className="truncate text-muted-foreground" title={e.detail || e.to_email}>
              {e.status === "sent" ? `to ${e.to_email}${e.test_mode ? " (test mode)" : ""}` : e.detail.replace(/^not sent:?\s*/, "")}
            </span>
            <ChevronDown className={cn("size-4 text-faint transition-transform duration-[120ms]", open === e.id && "rotate-180")} strokeWidth={1.5} />
          </button>
          <AnimatePresence initial={false}>
            {open === e.id && (
              <motion.div initial={{ height: 0, opacity: 0 }} animate={{ height: "auto", opacity: 1 }} exit={{ height: 0, opacity: 0 }}
                transition={{ duration: 0.18, ease: [0.22, 1, 0.36, 1] }} className="overflow-hidden">
                <div className="space-y-2 px-4 pb-4 text-[13px]">
                  <div className="text-faint">
                    Lead's address: <span className="text-muted-foreground">{e.lead_email || "–"}</span>
                    {e.call_id && <> · Call #{e.call_id}</>}
                  </div>
                  <div className="font-medium">{e.subject}</div>
                  <pre className="rounded-md border border-border bg-background/40 p-3 font-sans text-[12px] leading-5 whitespace-pre-wrap text-muted-foreground">{e.body}</pre>
                  {e.status !== "sent" && admin && (
                    <Button variant="secondary" size="sm" disabled={busy === e.id} onClick={() => retry(e)}>
                      <RotateCw strokeWidth={1.5} />
                      {busy === e.id ? "Sending…" : "Send again"}
                    </Button>
                  )}
                </div>
              </motion.div>
            )}
          </AnimatePresence>
        </li>
      ))}
    </ul>
  )
}

/** Health page: can we send, where emails go, today's count, the STOP-reply check. */
export function EmailHealthPanel() {
  const admin = useIsAdmin()
  const [st, setSt] = useState<EmailState | null>(null)
  const [checking, setChecking] = useState(false)
  const load = () => api.emailStatus().then(setSt).catch(() => setSt(null))
  useEffect(() => {
    load()
  }, [])

  const checkStop = async () => {
    setChecking(true)
    try {
      const r = await api.checkUnsubscribes()
      toast.success(r.enabled ? "Inbox checked for STOP replies" : "STOP-reply check isn't set up", { description: r.detail })
      load()
    } catch (e) {
      toast.error(e instanceof ApiError ? e.message : "Couldn't check the inbox")
    } finally {
      setChecking(false)
    }
  }

  const [testing, setTesting] = useState(false)
  const sendTest = async () => {
    setTesting(true)
    try {
      const r = await api.sendTestEmail()
      if (r.ok) toast.success("Test email sent", { description: r.detail })
      else toast.error("Test email not sent", { description: r.detail, duration: 10000 })
    } catch (e) {
      toast.error(e instanceof ApiError ? e.message : "Couldn't send the test email")
    } finally {
      setTesting(false)
    }
  }

  const rows: [string, string, boolean][] = st
    ? [
        ["Sending", st.disabled ? "Turned off (EMAIL_SENDING_DISABLED=1)" : st.detail, st.configured && !st.disabled],
        ["Follow-up emails", st.enabled ? "On" : "Switched off on the Instructions page", st.enabled],
        ["Mode", st.test_mode ? `Test mode: every email goes to ${st.test_address || "(EMAIL_TEST_ADDRESS not set)"}` : "Live: emails go to the leads", !st.test_mode || !!st.test_address],
        ["Last 24 hours", `${st.sent_24h} sent (limit ${st.daily_limit})`, st.sent_24h < st.daily_limit],
        ["STOP replies", st.unsubscribe.detail + (st.unsubscribe.unsubscribed ? ` · ${st.unsubscribe.unsubscribed} unsubscribed` : ""), st.unsubscribe.enabled],
      ]
    : []
  return (
    <section className="rounded-lg border border-border bg-surface">
      <div className="flex h-11 items-center justify-between border-b border-border px-4">
        <div className="text-[13px] font-medium">Email</div>
        {admin && (
          <div className="flex gap-1">
            <Button variant="ghost" size="sm" onClick={sendTest} disabled={testing}
              title="Sends one sample email to EMAIL_TEST_ADDRESS only (no lead, no call)">
              {testing ? "Sending…" : "Send test email"}
            </Button>
            <Button variant="ghost" size="sm" onClick={checkStop} disabled={checking}>
              {checking ? "Checking…" : "Check STOP replies"}
            </Button>
          </div>
        )}
      </div>
      <ul className="divide-y divide-border">
        {rows.map(([k, v, ok]) => (
          <li key={k} className="grid min-h-11 grid-cols-[160px_1fr] items-center gap-4 px-4 py-2 text-[13px]">
            <span className="font-medium">{k}</span>
            <span className={cn("break-words", ok ? "text-muted-foreground" : "text-warning")}>{v}</span>
          </li>
        ))}
      </ul>
    </section>
  )
}

/** One line on where emails go right now (test mode, not configured, limit). */
export function EmailSetupNote({ className }: { className?: string }) {
  const [st, setSt] = useState<EmailState | null>(null)
  useEffect(() => {
    api.emailStatus().then(setSt).catch(() => setSt(null))
  }, [])
  if (!st) return null
  const text = !st.enabled
    ? "Follow-up emails are switched off (Instructions → Follow-up emails)."
    : st.disabled
      ? "Email sending is turned off (EMAIL_SENDING_DISABLED=1)."
      : !st.configured
        ? `Not sending yet: ${st.detail}. Emails are saved as "not sent".`
        : st.test_mode
          ? `Test mode: every email goes to ${st.test_address || "(EMAIL_TEST_ADDRESS not set)"}. ${st.sent_24h}/${st.daily_limit} today.`
          : `Sending to leads from ${st.from_address}. ${st.sent_24h}/${st.daily_limit} in the last 24 hours.`
  return (
    <p className={cn("flex items-center gap-2 text-[12px]", st.configured && st.enabled && !st.disabled ? "text-faint" : "text-warning", className)}>
      <Mail className="size-3.5 shrink-0" strokeWidth={1.5} />
      {text}
    </p>
  )
}
