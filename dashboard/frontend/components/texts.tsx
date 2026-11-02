"use client"

import { ChevronDown, MessageSquare, RotateCw } from "lucide-react"
import { AnimatePresence, motion } from "motion/react"
import { useEffect, useState } from "react"
import { toast } from "sonner"

import { Tag } from "@/components/common"
import { Button } from "@/components/ui/button"
import { useIsAdmin } from "@/components/login-gate"
import { api, ApiError } from "@/lib/api"
import { formatDateTime, formatPhone } from "@/lib/format"
import type { Sms, SmsState } from "@/lib/types"
import { cn } from "@/lib/utils"

export const SMS_TEMPLATE_LABEL: Record<Sms["template"], string> = {
  not_answered: "Not answered / voicemail",
  not_interested: "Not interested",
  not_now: "Not now / callback",
  interested: "Interested",
}

export function SmsStatusTag({ status }: { status: Sms["status"] }) {
  if (status === "delivered") return <Tag tone="signal">Delivered</Tag>
  if (status === "sent") return <Tag tone="signal">Sent</Tag>
  if (status === "failed") return <Tag tone="danger">Failed</Tag>
  if (status === "queued") return <Tag>Queued</Tag>
  return <Tag>Not sent</Tag>
}

function rowNote(t: Sms): string {
  if (t.status === "sent" || t.status === "delivered") return `to ${formatPhone(t.to_phone)}${t.test_mode ? " (test mode)" : ""}`
  return t.detail.replace(/^not sent:?\s*/, "")
}

/** Texts for a lead or a call: time, template, status; click a row for the text itself. */
export function SmsList({ texts, onChanged }: { texts: Sms[]; onChanged?: () => void }) {
  const admin = useIsAdmin()
  const [open, setOpen] = useState<number | null>(null)
  const [busy, setBusy] = useState<number | null>(null)

  const retry = async (t: Sms) => {
    setBusy(t.id)
    try {
      const r = await api.retrySms(t.id)
      if (r.status === "sent") toast.success(`Text sent to ${formatPhone(r.to_phone)}`)
      else toast.warning(r.status === "queued" ? "Text queued" : r.status === "failed" ? "Sending failed again" : "Still not sent", { description: r.detail })
      onChanged?.()
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : "Couldn't send the text")
    } finally {
      setBusy(null)
    }
  }

  return (
    <ul className="divide-y divide-border">
      {texts.map((t) => (
        <li key={t.id}>
          <button
            onClick={() => setOpen((x) => (x === t.id ? null : t.id))}
            className="grid w-full grid-cols-[150px_130px_76px_1fr_16px] items-center gap-4 px-4 py-2.5 text-left text-[13px] transition-colors duration-[120ms] hover:bg-surface-2"
          >
            <span className="text-muted-foreground tnum">{formatDateTime(t.delivered_at ?? t.sent_at ?? t.send_after ?? t.created_at ?? "")}</span>
            <span className="truncate">{SMS_TEMPLATE_LABEL[t.template] ?? t.template}</span>
            <span><SmsStatusTag status={t.status} /></span>
            <span className="truncate text-muted-foreground" title={t.detail || t.to_phone}>{rowNote(t)}</span>
            <ChevronDown className={cn("size-4 text-faint transition-transform duration-[120ms]", open === t.id && "rotate-180")} strokeWidth={1.5} />
          </button>
          <AnimatePresence initial={false}>
            {open === t.id && (
              <motion.div initial={{ height: 0, opacity: 0 }} animate={{ height: "auto", opacity: 1 }} exit={{ height: 0, opacity: 0 }}
                transition={{ duration: 0.18, ease: [0.22, 1, 0.36, 1] }} className="overflow-hidden">
                <div className="space-y-2 px-4 pb-4 text-[13px]">
                  <div className="text-faint">
                    Lead's number: <span className="text-muted-foreground">{formatPhone(t.lead_phone) || "–"}</span>
                    {t.call_id && <> · Call #{t.call_id}</>}
                    {t.detail && (t.status === "sent" || t.status === "delivered") && <> · {t.detail}</>}
                  </div>
                  <pre className="rounded-md border border-border bg-background/40 p-3 font-sans text-[12px] leading-5 whitespace-pre-wrap text-muted-foreground">{t.body}</pre>
                  {(t.status === "failed" || t.status === "not_sent") && admin && (
                    <Button variant="secondary" size="sm" disabled={busy === t.id} onClick={() => retry(t)}>
                      <RotateCw strokeWidth={1.5} />
                      {busy === t.id ? "Sending…" : "Send again"}
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

/** Health page: can we text, where texts go, today's count, replies (STOP), and one test text. */
export function SmsHealthPanel() {
  const admin = useIsAdmin()
  const [st, setSt] = useState<SmsState | null>(null)
  const [testing, setTesting] = useState(false)
  const load = () => api.smsStatus().then(setSt).catch(() => setSt(null))
  useEffect(() => {
    load()
  }, [])

  const sendTest = async () => {
    setTesting(true)
    try {
      const r = await api.sendTestSms()
      if (r.ok) toast.success("Test text sent", { description: r.detail })
      else toast.error("Test text not sent", { description: r.detail, duration: 10000 })
      load()
    } catch (e) {
      toast.error(e instanceof ApiError ? e.message : "Couldn't send the test text")
    } finally {
      setTesting(false)
    }
  }

  const rows: [string, string, boolean][] = st
    ? [
        ["Sending", st.disabled ? "Turned off (SMS_SENDING_DISABLED=1)" : st.detail, st.configured && !st.disabled],
        ["Follow-up texts", st.enabled ? "On" : "Switched off on the Instructions page", st.enabled],
        ["Mode", st.test_mode ? `Test mode: every text goes to ${formatPhone(st.test_number) || "(TEST_CALL_NUMBER not set)"}` : "Live: texts go to the leads", !st.test_mode || !!st.test_number],
        ["Last 24 hours", `${st.sent_24h} sent (limit ${st.daily_limit}; 1 per lead, ever)${st.queued ? ` · ${st.queued} queued for calling hours` : ""}`, st.sent_24h < st.daily_limit],
        ["Replies (STOP)", st.webhook, st.webhook_ok],
        ["Signature check", st.signature, st.signature.startsWith("On")],
      ]
    : []
  return (
    <section className="rounded-lg border border-border bg-surface">
      <div className="flex h-11 items-center justify-between border-b border-border px-4">
        <div className="text-[13px] font-medium">Texts (SMS)</div>
        {admin && (
          <Button variant="ghost" size="sm" onClick={sendTest} disabled={testing}
            title={`Sends ONE sample text to the test number ${st?.test_number ?? ""} only (no lead, no call)`}>
            {testing ? "Sending…" : "Send test SMS"}
          </Button>
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

/** One line on where texts go right now (test mode, not configured). */
export function SmsSetupNote({ className }: { className?: string }) {
  const [st, setSt] = useState<SmsState | null>(null)
  useEffect(() => {
    api.smsStatus().then(setSt).catch(() => setSt(null))
  }, [])
  if (!st) return null
  const text = !st.enabled
    ? "Follow-up texts are switched off (Instructions → Follow-up texts)."
    : st.disabled
      ? "Text sending is turned off (SMS_SENDING_DISABLED=1)."
      : !st.configured
        ? `Not sending yet: ${st.detail}. Texts are saved as "not sent".`
        : st.test_mode
          ? `Test mode: every text goes to ${formatPhone(st.test_number)}. ${st.sent_24h}/${st.daily_limit} today.`
          : `Texting leads from ${formatPhone(st.from_number)}. ${st.sent_24h}/${st.daily_limit} in the last 24 hours.`
  return (
    <p className={cn("flex items-center gap-2 text-[12px]", st.configured && st.enabled && !st.disabled ? "text-faint" : "text-warning", className)}>
      <MessageSquare className="size-3.5 shrink-0" strokeWidth={1.5} />
      {text}
    </p>
  )
}
