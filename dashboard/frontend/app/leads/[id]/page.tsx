"use client"

import { ArrowLeft, Ban, ChevronRight, Phone, ShieldOff } from "lucide-react"
import { motion } from "motion/react"
import Link from "next/link"
import { useParams, useRouter } from "next/navigation"
import { useCallback, useEffect, useState } from "react"
import { toast } from "sonner"

import { EmptyState, PageHeader, Panel, PanelHeader, ResultTag, SkeletonLine } from "@/components/common"
import { EmailList, EmailSetupNote } from "@/components/emails"
import { SmsList, SmsSetupNote } from "@/components/texts"
import { LeadStatusTag } from "@/components/lead-status"
import { formatInZone, MANUAL_OUTCOMES, OUTCOME_LABEL, OutcomeTag, SOURCE_LABEL } from "@/components/outcome"
import { TransferInfo } from "@/components/transfer"
import { useIsAdmin } from "@/components/login-gate"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { api, ApiError } from "@/lib/api"
import { formatClock, formatDateTime, formatDuration, formatPhone, isActive, timeAgo } from "@/lib/format"
import { useLiveStream } from "@/lib/live"
import type { Call, Lead, LeadDetail, Outcome } from "@/lib/types"
import { cn } from "@/lib/utils"

const TZ_SOURCE = { zip: "from zip code", state: "from state", area_code: "from area code", unknown: "" } as const

function tzLabel(tz: string | null, source: keyof typeof TZ_SOURCE): string {
  if (!tz) return "Unknown: add a zip or state to the location (not called until then)"
  const local = new Intl.DateTimeFormat("en-US", { timeZone: tz, hour: "numeric", minute: "2-digit", weekday: "short" }).format(new Date())
  return `${tz.split("/").pop()!.replace(/_/g, " ")} · ${local} now · ${TZ_SOURCE[source]}`
}

export default function LeadDetailPage() {
  const admin = useIsAdmin()
  const { id } = useParams<{ id: string }>()
  const leadId = Number(id)
  const router = useRouter()
  const [data, setData] = useState<LeadDetail | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [askReason, setAskReason] = useState(false)
  const [reason, setReason] = useState("")

  const load = useCallback(() => {
    api.lead(leadId).then(setData).catch((e) => setError(e instanceof ApiError ? e.message : "Couldn't load this lead"))
  }, [leadId])

  useEffect(load, [load])

  useLiveStream((msg) => {
    if ((msg.type === "lead" && msg.lead.id === leadId) || (msg.type === "call" && msg.call.lead_id === leadId)
      || (msg.type === "email" && msg.email.lead_id === leadId) || (msg.type === "sms" && msg.sms.lead_id === leadId)) load()
  })

  const lead = data?.lead

  const call = async () => {
    if (!lead) return
    setBusy(true)
    try {
      await api.callLead(lead.id)
      toast.success(`Calling ${lead.name || formatPhone(lead.phone)}`, { action: { label: "Watch live", onClick: () => router.push("/") } })
      load()
    } catch (e) {
      toast.error(e instanceof ApiError ? e.message : "Couldn't start the call")
    } finally {
      setBusy(false)
    }
  }

  const setDnc = async (dnc: boolean) => {
    if (!lead) return
    setBusy(true)
    try {
      await api.setDnc(lead.id, dnc, reason)
      toast.success(dnc ? "Marked Do not contact: this lead won't be called or texted" : "Removed from Do not contact")
      setAskReason(false)
      setReason("")
      load()
    } catch (e) {
      toast.error(e instanceof ApiError ? e.message : "Couldn't update the lead")
    } finally {
      setBusy(false)
    }
  }

  const callAnyway = async () => {
    if (!lead) return
    setBusy(true)
    try {
      await api.callAnyway(lead.id)
      toast.success("Back in the dialer's queue", { description: "The service-fit check is ignored for this lead." })
      load()
    } catch (e) {
      toast.error(e instanceof ApiError ? e.message : "Couldn't update the lead")
    } finally {
      setBusy(false)
    }
  }

  const back = (
    <Link href="/leads" className="inline-flex items-center gap-1.5 text-[12px] text-muted-foreground transition-colors hover:text-foreground">
      <ArrowLeft className="size-3.5" strokeWidth={1.5} /> Leads
    </Link>
  )

  if (error) {
    return (
      <>
        <div className="mb-4">{back}</div>
        <EmptyState>{error}</EmptyState>
      </>
    )
  }

  return (
    <>
      <div className="mb-4">{back}</div>
      {lead ? (
        <PageHeader
          title={lead.name || formatPhone(lead.phone)}
          description={[lead.business, lead.location || lead.city].filter(Boolean).join(" · ") || formatPhone(lead.phone)}
          actions={
            <>
              <LeadStatusTag status={lead.status} className="mr-1" />
              {admin && lead.status === "not_a_fit" && (
                <Button variant="secondary" size="sm" disabled={busy} onClick={callAnyway}
                  title="The service they need isn't something we offer. Put this lead back in the dialer's queue anyway.">
                  Call anyway
                </Button>
              )}
              {!admin ? null : lead.dnc ? (
                <Button variant="secondary" size="sm" disabled={busy} onClick={() => setDnc(false)}>
                  <ShieldOff strokeWidth={1.5} />
                  Remove from Do not contact
                </Button>
              ) : (
                <Button variant="ghost" size="sm" disabled={busy} onClick={() => setAskReason((x) => !x)}>
                  <Ban strokeWidth={1.5} />
                  Do not contact
                </Button>
              )}
              {admin && (
                <Button size="sm" disabled={busy || lead.dnc || isActive(data!.calls[0]?.status ?? "ended")} onClick={call}
                  title={lead.dnc ? "This number is on the Do-Not-Contact list" : undefined}>
                  <Phone strokeWidth={1.75} />
                  Call this lead
                </Button>
              )}
            </>
          }
        />
      ) : (
        <div className="mb-8 space-y-2">
          <SkeletonLine className="h-5 w-48" />
          <SkeletonLine className="w-64" />
        </div>
      )}

      {askReason && lead && !lead.dnc && (
        <motion.div initial={{ opacity: 0, y: -4 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.18, ease: [0.22, 1, 0.36, 1] }} className="mb-6">
          <Panel className="flex flex-wrap items-end gap-3 p-4">
            <label className="min-w-[260px] flex-1 space-y-1.5">
              <span className="text-[12px] font-medium text-muted-foreground">Why? (optional)</span>
              <Input value={reason} onChange={(e) => setReason(e.target.value)} placeholder="Asked not to be contacted" autoFocus />
            </label>
            <Button variant="destructive" disabled={busy} onClick={() => setDnc(true)}>
              <Ban strokeWidth={1.5} />
              Mark Do not contact
            </Button>
            <Button variant="ghost" onClick={() => setAskReason(false)}>Cancel</Button>
          </Panel>
        </motion.div>
      )}

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-12">
        <div className="flex flex-col gap-6 lg:col-span-8">
          <Panel>
            <PanelHeader title="Calls" right={data ? <span className="text-[12px] text-faint tnum">{data.calls.length}</span> : null} />
            {!data ? (
              <div className="space-y-3 p-4"><SkeletonLine className="w-2/3" /><SkeletonLine className="w-1/2" /></div>
            ) : data.calls.length === 0 ? (
              <EmptyState>Not called yet.</EmptyState>
            ) : (
              <ul className="divide-y divide-border">
                {data.calls.map((c, i) => (
                  <motion.li key={c.id} initial={{ opacity: 0 }} animate={{ opacity: 1 }} transition={{ duration: 0.18, delay: Math.min(i, 8) * 0.02 }}>
                    <Link href={`/history/${c.id}`} className="group grid grid-cols-[150px_110px_1fr_64px_20px] items-center gap-4 px-4 py-2.5 text-[13px] transition-colors duration-[120ms] hover:bg-surface-2">
                      <span className="text-muted-foreground tnum">{formatDateTime(c.created_at)}</span>
                      <span>{!isActive(c.status) && c.outcome ? <OutcomeTag outcome={c.outcome} messageLeft={c.vm_message_left} /> : <ResultTag result={c.result} active={isActive(c.status)} />}</span>
                      <span className="truncate text-muted-foreground" title={c.outcome_note || c.reason}>{c.outcome_note || c.reason || "–"}</span>
                      <span className="text-right font-mono text-muted-foreground tnum">{formatDuration(c.duration_s)}</span>
                      <ChevronRight className="size-4 text-faint transition-transform duration-[120ms] group-hover:translate-x-0.5" strokeWidth={1.5} />
                    </Link>
                  </motion.li>
                ))}
              </ul>
            )}
          </Panel>

          <Panel>
            <PanelHeader title="Emails" right={data ? <span className="text-[12px] text-faint tnum">{data.emails.length}</span> : null} />
            {!data ? (
              <div className="space-y-3 p-4"><SkeletonLine className="w-2/3" /></div>
            ) : data.emails.length === 0 ? (
              <EmptyState>
                {lead?.email
                  ? "No emails yet. A follow-up email goes out after a call when the outcome calls for one."
                  : "No email address yet. The AI asks for it on the call (or import it with the CSV)."}
              </EmptyState>
            ) : (
              <EmailList emails={data.emails} onChanged={load} />
            )}
            <EmailSetupNote className="border-t border-border px-4 py-2.5" />
          </Panel>

          <Panel>
            <PanelHeader title="Texts" right={data ? <span className="text-[12px] text-faint tnum">{data.texts.length}</span> : null} />
            {!data ? (
              <div className="space-y-3 p-4"><SkeletonLine className="w-2/3" /></div>
            ) : data.texts.length === 0 ? (
              <EmptyState>No texts yet. A follow-up text goes out after a call when the outcome calls for one.</EmptyState>
            ) : (
              <SmsList texts={data.texts} onChanged={load} />
            )}
            <SmsSetupNote className="border-t border-border px-4 py-2.5" />
          </Panel>
        </div>

        <div className="flex flex-col gap-6 lg:col-span-4">
          {lead && <OutcomePanel lead={lead} lastCall={data?.calls[0]} onSaved={load} />}

          <Panel>
            <PanelHeader title="Details" />
            <dl className="grid grid-cols-[96px_1fr] gap-y-2 p-4 text-[13px]">
              <dt className="text-faint">Phone</dt>
              <dd className="font-mono tnum">{lead ? formatPhone(lead.phone) : "–"}</dd>
              <dt className="text-faint">Email</dt>
              <dd className="truncate text-muted-foreground" title={lead?.email || undefined}>{lead?.email || "–"}</dd>
              <dt className="text-faint">Status</dt>
              <dd>{lead ? <LeadStatusTag status={lead.status} /> : "–"}</dd>
              <dt className="text-faint">Time zone</dt>
              <dd className={cn(lead?.timezone ? "text-muted-foreground" : "text-warning")}>{lead ? tzLabel(lead.timezone, lead.tz_source) : "–"}</dd>
              <dt className="text-faint">Attempts</dt>
              <dd className="font-mono tnum">{lead?.attempts ?? "–"}</dd>
              <dt className="text-faint">Last call</dt>
              <dd className="text-muted-foreground">{lead?.last_call_at ? timeAgo(lead.last_call_at) : "–"}</dd>
              <dt className="text-faint">Source</dt>
              <dd className="truncate text-muted-foreground">{lead?.source || "–"}</dd>
              {lead?.dnc && (
                <>
                  <dt className="text-faint">Do not contact</dt>
                  <dd className="text-danger">{lead.dnc_reason || "Yes"}{lead.dnc_at ? ` · ${timeAgo(lead.dnc_at)}` : ""}</dd>
                </>
              )}
            </dl>
          </Panel>

          <Panel>
            <PanelHeader title="About this lead" />
            <dl className="space-y-3 p-4 text-[13px]">
              {([
                ["Service required", lead?.service_required],
                ["Location", lead?.location || lead?.city],
                ["Business", lead?.business_description],
                ["Job", lead?.job_description],
              ] as const).map(([label, value]) => (
                <div key={label}>
                  <dt className="text-[12px] text-faint">{label}</dt>
                  <dd className={cn("mt-0.5 leading-5 break-words", value ? "text-foreground" : "text-faint")}>{value || "Not given"}</dd>
                </div>
              ))}
            </dl>
          </Panel>

          <Panel>
            <PanelHeader title="Notes" />
            <p className="p-4 text-[13px] leading-5 whitespace-pre-wrap text-muted-foreground">{lead?.notes || "No notes."}</p>
          </Panel>

          <Panel>
            <PanelHeader title="Activity" />
            {data?.events.length ? (
              <ol className="space-y-2 p-4 text-[12px]">
                {data.events.map((e) => (
                  <li key={e.id} className="grid grid-cols-[64px_1fr] gap-3">
                    <span className="font-mono text-faint tnum" title={formatDateTime(e.ts)}>{formatClock(e.ts)}</span>
                    <span className={cn(e.kind === "dnc" ? "text-danger" : "text-muted-foreground")}>{e.text}</span>
                  </li>
                ))}
              </ol>
            ) : (
              <EmptyState>No activity yet.</EmptyState>
            )}
          </Panel>
        </div>
      </div>
    </>
  )
}

function OutcomePanel({ lead, lastCall, onSaved }: { lead: Lead; lastCall?: Call; onSaved: () => void }) {
  const admin = useIsAdmin()
  const [editing, setEditing] = useState(false)
  const [outcome, setOutcome] = useState<Outcome>("not_now")
  const [note, setNote] = useState("")
  const [when, setWhen] = useState("")
  const [busy, setBusy] = useState(false)
  const needsTime = outcome === "not_now" || outcome === "gatekeeper"
  const shown = lastCall && !isActive(lastCall.status) && lastCall.outcome ? lastCall : null

  const open = () => {
    setOutcome(shown?.outcome && MANUAL_OUTCOMES.includes(shown.outcome) ? shown.outcome : "not_now")
    setNote(shown?.outcome_note ?? "")
    setWhen("")
    setEditing(true)
  }

  const save = async (e: React.FormEvent) => {
    e.preventDefault()
    setBusy(true)
    try {
      await api.setOutcome(lead.id, outcome, note, needsTime ? when : "")
      toast.success(`Outcome set: ${OUTCOME_LABEL[outcome]}`)
      setEditing(false)
      onSaved()
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : "Couldn't save the outcome")
    } finally {
      setBusy(false)
    }
  }

  return (
    <Panel>
      <PanelHeader title="Outcome" right={admin && !editing && (
        <Button variant="ghost" size="sm" onClick={open} disabled={lead.dnc}
          title={lead.dnc ? "On Do not contact: remove it from the list first" : "Change the outcome (e.g. after listening to the call)"}>
          Set outcome
        </Button>
      )} />
      {editing ? (
        <form onSubmit={save} className="space-y-3 p-4">
          <Select value={outcome} onValueChange={(v) => setOutcome(v as Outcome)}>
            <SelectTrigger aria-label="Outcome"><SelectValue /></SelectTrigger>
            <SelectContent>
              {MANUAL_OUTCOMES.map((o) => <SelectItem key={o} value={o}>{OUTCOME_LABEL[o]}</SelectItem>)}
            </SelectContent>
          </Select>
          {needsTime && (
            <label className="block space-y-1.5">
              <span className="text-[12px] font-medium text-muted-foreground">Call back at (their local time; empty = in a few days)</span>
              <Input type="datetime-local" value={when} onChange={(e) => setWhen(e.target.value)} className="font-mono tnum" />
            </label>
          )}
          <label className="block space-y-1.5">
            <span className="text-[12px] font-medium text-muted-foreground">Note (optional)</span>
            <Input value={note} onChange={(e) => setNote(e.target.value)} placeholder="Owner is in after 2pm" maxLength={300} />
          </label>
          {outcome === "do_not_contact" && <p className="text-[12px] text-danger">This lead will never be called or texted again.</p>}
          <div className="flex gap-2">
            <Button type="submit" size="sm" variant={outcome === "do_not_contact" ? "destructive" : "default"} disabled={busy}>
              {busy ? "Saving…" : "Save"}
            </Button>
            <Button type="button" size="sm" variant="ghost" onClick={() => setEditing(false)}>Cancel</Button>
          </div>
        </form>
      ) : shown?.outcome ? (
        <div className="space-y-2 p-4 text-[13px]">
          <div className="flex flex-wrap items-center gap-2">
            <OutcomeTag outcome={shown.outcome} messageLeft={shown.vm_message_left} />
            <span className="text-[12px] text-faint">{SOURCE_LABEL[shown.outcome_source ?? ""] ?? ""} · call #{shown.id}</span>
          </div>
          {shown.outcome_note && <p className="leading-5 text-muted-foreground">{shown.outcome_note}</p>}
          <TransferInfo call={shown} />
          {lead.status === "callback" && lead.callback_at && (
            <p className="text-muted-foreground">
              Call back <span className="text-foreground">{formatInZone(lead.callback_at, lead.timezone, true)}</span>
              {lead.callback_count > 1 && <span className="text-faint"> · callback {lead.callback_count} in a row</span>}
            </p>
          )}
          {lead.vm_count > 0 && <p className="text-[12px] text-faint">Voicemail reached {lead.vm_count}×</p>}
        </div>
      ) : (
        <EmptyState>{lastCall && isActive(lastCall.status) ? "Call in progress…" : "No outcome yet."}</EmptyState>
      )}
    </Panel>
  )
}
