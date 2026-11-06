"use client"

import { ArrowLeft } from "lucide-react"
import Link from "next/link"
import { useParams } from "next/navigation"
import { useCallback, useEffect, useMemo, useState } from "react"

import { EmptyState, PageHeader, Panel, PanelHeader, ResultTag, SkeletonLine, Tag } from "@/components/common"
import { EmailList } from "@/components/emails"
import { SmsList } from "@/components/texts"
import { formatInZone, OutcomeTag, SOURCE_LABEL } from "@/components/outcome"
import { CallDebug } from "@/components/call-debug"
import { TransferInfo } from "@/components/transfer"
import { StatusPill } from "@/components/status-pill"
import { Transcript } from "@/components/transcript"
import { api, API_URL, ApiError } from "@/lib/api"
import { formatClock, formatDateTime, formatDuration, formatPhone, formatSeconds, isActive } from "@/lib/format"
import { useLiveStream } from "@/lib/live"
import type { Email, Call, CallEvent, Sms } from "@/lib/types"
import { cn } from "@/lib/utils"

const AGENT_TIMING: [string, string][] = [
  ["reply_gap", "Reply gap"],
  ["llm_first_token", "LLM first token"],
  ["llm_first_sentence", "LLM first sentence"],
  ["tts_first_audio", "TTS first audio"],
  ["playback", "Playback"],
]
const CALLEE_TIMING: [string, string][] = [
  ["stt_final", "STT final"],
  ["end_of_turn", "End of turn"],
]

export default function CallDetailPage() {
  const { id } = useParams<{ id: string }>()
  const callId = Number(id)
  const [call, setCall] = useState<Call | null>(null)
  const [events, setEvents] = useState<CallEvent[]>([])
  const [error, setError] = useState<string | null>(null)
  const [emails, setEmails] = useState<Email[]>([])
  const loadEmails = useCallback(() => {
    api.emails({ call_id: callId }).then(setEmails).catch(() => setEmails([]))
  }, [callId])
  useEffect(loadEmails, [loadEmails])
  const [texts, setTexts] = useState<Sms[]>([])
  const loadTexts = useCallback(() => {
    api.texts({ call_id: callId }).then(setTexts).catch(() => setTexts([]))
  }, [callId])
  useEffect(loadTexts, [loadTexts])

  useEffect(() => {
    api
      .call(callId)
      .then((d) => {
        setCall(d.call)
        setEvents(d.events)
      })
      .catch((e) => setError(e instanceof ApiError ? e.message : "Couldn't load this call"))
  }, [callId])

  useLiveStream((msg) => {
    if (msg.type === "call" && msg.call.id === callId) setCall(msg.call)
    if (msg.type === "email" && msg.email.call_id === callId) loadEmails()
    if (msg.type === "sms" && msg.sms.call_id === callId) loadTexts()
    if (msg.type === "event" && msg.event.call_id === callId)
      setEvents((prev) => (prev.some((p) => p.id === msg.event.id) ? prev : [...prev, msg.event]))
  })

  const timings = useMemo(() => events.filter((e) => e.kind === "timing"), [events])
  const timeline = useMemo(
    () => events.filter((e) => e.kind === "status" || e.kind === "log" || e.kind === "agent"),
    [events],
  )

  const back = (
    <Link href="/history" className="inline-flex items-center gap-1.5 text-[12px] text-muted-foreground transition-colors hover:text-foreground">
      <ArrowLeft className="size-3.5" strokeWidth={1.5} /> History
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
      {call ? (
        <PageHeader
          title={formatPhone(call.number)}
          description={`${formatDateTime(call.created_at)} · ${call.simulated ? "Simulated" : call.direction === "inbound" ? "Inbound: they called us" : call.source === "cli" ? "From terminal" : "From dashboard"}`}
          actions={isActive(call.status) ? <StatusPill status={call.status} /> : call.outcome ? <OutcomeTag outcome={call.outcome} messageLeft={call.vm_message_left} /> : <ResultTag result={call.result} />}
        />
      ) : (
        <div className="mb-8 space-y-2">
          <SkeletonLine className="h-5 w-48" />
          <SkeletonLine className="w-64" />
        </div>
      )}

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-12">
        <Panel className="lg:col-span-8">
          <PanelHeader title="Transcript" />
          {call ? (
            events.some((e) => e.kind === "transcript") ? (
              <Transcript events={events} animate={false} className="max-h-[70vh]" />
            ) : (
              <EmptyState>No conversation was recorded for this call.</EmptyState>
            )
          ) : (
            <div className="space-y-4 p-4">
              {Array.from({ length: 5 }).map((_, i) => (
                <SkeletonLine key={i} className={i % 2 ? "w-2/3" : "w-5/6"} />
              ))}
            </div>
          )}
        </Panel>

        <div className="flex flex-col gap-6 lg:col-span-4">
          <Panel>
            <PanelHeader title="Summary" />
            <dl className="grid grid-cols-[96px_1fr] gap-y-2 p-4 text-[13px]">
              <dt className="text-faint">Result</dt>
              <dd>{call ? <ResultTag result={call.result} active={isActive(call.status)} /> : "–"}</dd>
              {call?.outcome && !isActive(call.status) && (
                <>
                  <dt className="text-faint">Outcome</dt>
                  <dd className="space-y-1">
                    <div className="flex flex-wrap items-center gap-2">
                      <OutcomeTag outcome={call.outcome} messageLeft={call.vm_message_left} />
                      <span className="text-[12px] text-faint">{SOURCE_LABEL[call.outcome_source ?? ""] ?? ""}</span>
                    </div>
                    {call.outcome_note && <div className="text-muted-foreground">{call.outcome_note}</div>}
                    {call.callback_at && <div className="text-muted-foreground">Callback {formatInZone(call.callback_at, null, true)}</div>}
                  </dd>
                </>
              )}
              <dt className="text-faint">Talk time</dt>
              <dd className="font-mono tnum">{formatDuration(call?.duration_s)}</dd>
              <dt className="text-faint">Reason</dt>
              <dd className={cn(call?.result === "failed" ? "text-danger" : "text-muted-foreground")}>{call?.reason || "–"}</dd>
              <dt className="text-faint">{call?.room.startsWith("ac_") ? "Cartesia ID" : "Room"}</dt>
              <dd className="truncate font-mono text-[12px] text-muted-foreground">{call?.room ?? "–"}</dd>
            </dl>
            {call && <TransferInfo call={call} className="mx-4 mb-4" />}
          </Panel>

          {call && (call.email_collected || emails.length > 0) && (
            <Panel>
              <PanelHeader title="Email" />
              {call.email_collected && (
                <p className="px-4 pt-3 text-[13px] text-muted-foreground">
                  Confirmed on the call: <span className="text-foreground">{call.email_collected}</span>
                  {call.email_reason && call.email_reason !== "other" && <span className="text-faint"> · {call.email_reason.replace("_", " ")}</span>}
                </p>
              )}
              {emails.length > 0 ? <EmailList emails={emails} onChanged={loadEmails} /> : (
                <p className="px-4 py-3 text-[13px] text-faint">No follow-up email for this call.</p>
              )}
            </Panel>
          )}

          {texts.length > 0 && (
            <Panel>
              <PanelHeader title="Text" />
              <SmsList texts={texts} onChanged={loadTexts} />
            </Panel>
          )}

          {call && call.room.startsWith("ac_") && !isActive(call.status) && (
            <Panel>
              <PanelHeader title="Recording" />
              <div className="p-4">
                {/* preload=none: the WAV is fetched from Cartesia (and cached) only when played */}
                <audio controls preload="none" className="w-full [color-scheme:dark]" src={`${API_URL}/api/calls/${call.id}/audio`}>
                  Your browser can&apos;t play this recording.
                </audio>
                <p className="mt-2 text-[12px] text-faint">From Cartesia. Saved locally in data/recordings after the first play.</p>
              </div>
            </Panel>
          )}

          <Panel>
            <PanelHeader title="Timeline" />
            {timeline.length ? (
              <ol className="space-y-2 p-4 text-[12px]">
                {timeline.map((e) => (
                  <li key={e.id} className="grid grid-cols-[64px_1fr] gap-3">
                    <span className="font-mono text-faint tnum">{formatClock(e.ts)}</span>
                    <span className={cn(e.text === "failed" || e.data?.level === "error" ? "text-danger" : "text-muted-foreground")}>
                      {eventLabel(e)}
                    </span>
                  </li>
                ))}
              </ol>
            ) : (
              <EmptyState>No events.</EmptyState>
            )}
          </Panel>
        </div>

        <Panel className="lg:col-span-12">
          <PanelHeader title="Timing per turn" right={<span className="text-[12px] text-faint">From the agent&apos;s latency metrics</span>} />
          {timings.length ? (
            <div className="overflow-x-auto">
              <table className="w-full text-[12px]">
                <thead>
                  <tr className="border-b border-border text-left text-faint">
                    <th className="h-9 px-4 font-normal">Time</th>
                    <th className="h-9 px-4 font-normal">Turn</th>
                    {[...AGENT_TIMING, ...CALLEE_TIMING].map(([, label]) => (
                      <th key={label} className="h-9 px-4 text-right font-normal">{label}</th>
                    ))}
                  </tr>
                </thead>
                <tbody className="divide-y divide-border">
                  {timings.map((t) => (
                    <tr key={t.id} className="transition-colors duration-[120ms] hover:bg-surface-2">
                      <td className="h-9 px-4 font-mono text-faint tnum">{formatClock(t.ts)}</td>
                      <td className="h-9 px-4">
                        <Tag tone={t.role === "agent" ? "signal" : "neutral"}>{t.role === "agent" ? "Agent" : "Callee"}</Tag>
                      </td>
                      {[...AGENT_TIMING, ...CALLEE_TIMING].map(([key]) => (
                        <td key={key} className="h-9 px-4 text-right font-mono tnum">
                          {formatSeconds(t.data?.[key]) ?? <span className="text-faint">–</span>}
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <EmptyState>No timing lines for this call.</EmptyState>
          )}
        </Panel>

        <CallDebug events={events} />
      </div>
    </>
  )
}

function eventLabel(e: CallEvent): string {
  const detail = typeof e.data?.detail === "string" ? e.data.detail : ""
  if (e.kind === "status") return detail || e.text
  if (e.kind === "agent") return detail || `Agent: ${e.text}`
  return e.text
}
