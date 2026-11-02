"use client"

import { Copy } from "lucide-react"
import { useMemo } from "react"
import { toast } from "sonner"

import { EmptyState, Panel, PanelHeader, Tag } from "@/components/common"
import { Button } from "@/components/ui/button"
import { formatClockMs } from "@/lib/format"
import type { CallEvent } from "@/lib/types"
import { cn } from "@/lib/utils"

const STEP_LABEL: Record<string, string> = {
  agent_config: "Agent config",
  call_requested: "Call requested",
  call_request_failed: "Call request failed",
  cartesia_status: "Cartesia status",
  tool_call: "Agent tool",
  end_reason: "End reason",
  post_call_analysis: "Call summary",
  cancel_not_active: "Hang up: call not active",
  silence_checkin: "Silence check-in",
  closed_after_goodbye: "Closed after goodbye",
  closed_after_silence: "Closed after silence",
  closed_by_carrier: "Closed: callee hung up",
  carrier_hangup: "Telnyx: hang-up",
  recheck_after_restart: "Re-checked after restart",
  hangup_not_effective: "Hang up not effective",
  line_still_open: "Line still open",
  room_created: "Room created",
  agent_dispatched: "Agent dispatched",
  dial_sent: "Dial sent",
  sip_participant_joined: "SIP participant joined",
  sip_status: "SIP status",
  dial_result: "Dial result",
  hangup_requested: "Hang up requested",
  sip_participant_left: "SIP participant left",
  room_gone: "Room gone",
  room_deleted: "Room deleted",
  poll_error: "Status check failed",
  poll_recovered: "Status check recovered",
}

const SOURCE_LABEL: Record<string, string> = { debug: "Dialer", status: "Status", agent: "Agent", log: "Agent" }

/** Raw call steps (dialer debug + status + agent events) with timestamps, for diagnosing calls. */
export function CallDebug({ events }: { events: CallEvent[] }) {
  const rows = useMemo(() => {
    const raw = events.filter((e) => e.kind === "debug" || e.kind === "status" || e.kind === "agent" || e.kind === "log")
    const t0 = raw.length ? new Date(raw[0].ts).getTime() : 0
    return raw.map((e) => ({
      e,
      offset: (new Date(e.ts).getTime() - t0) / 1000,
      step: stepLabel(e),
      detail: typeof e.data?.detail === "string" ? e.data.detail : "",
      extra: formatData(e.data),
      tone: toneOf(e),
    }))
  }, [events])

  const copy = async () => {
    const text = rows
      .map((r) => [formatClockMs(r.e.ts), `+${r.offset.toFixed(2)}s`, SOURCE_LABEL[r.e.kind], r.step, r.detail, r.extra].filter(Boolean).join("  "))
      .join("\n")
    try {
      await navigator.clipboard.writeText(text)
      toast.success("Call debug copied")
    } catch {
      toast.error("Couldn't copy to the clipboard")
    }
  }

  return (
    <Panel className="lg:col-span-12">
      <PanelHeader
        title="Call debug"
        right={
          rows.length ? (
            <Button variant="ghost" size="xs" onClick={copy}>
              <Copy strokeWidth={1.5} />
              Copy
            </Button>
          ) : null
        }
      />
      {rows.length ? (
        <div className="overflow-x-auto">
          <table className="w-full text-[12px]">
            <thead>
              <tr className="border-b border-border text-left text-faint">
                <th className="h-9 px-4 font-normal">Time</th>
                <th className="h-9 px-4 text-right font-normal">+</th>
                <th className="h-9 px-4 font-normal">Source</th>
                <th className="h-9 px-4 font-normal">Step</th>
                <th className="h-9 px-4 font-normal">Detail</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {rows.map((r) => (
                <tr key={r.e.id} className="align-top transition-colors duration-[120ms] hover:bg-surface-2">
                  <td className="whitespace-nowrap px-4 py-2 font-mono text-faint tnum">{formatClockMs(r.e.ts)}</td>
                  <td className="whitespace-nowrap px-4 py-2 text-right font-mono text-faint tnum">{r.offset.toFixed(2)}s</td>
                  <td className="whitespace-nowrap px-4 py-2">
                    <Tag tone={r.e.kind === "status" ? "signal" : "neutral"}>{SOURCE_LABEL[r.e.kind]}</Tag>
                  </td>
                  <td className={cn("whitespace-nowrap px-4 py-2", r.tone === "danger" ? "text-danger" : "text-foreground")}>{r.step}</td>
                  <td className="px-4 py-2">
                    <div className={cn(r.tone === "danger" ? "text-danger" : "text-muted-foreground")}>{r.detail || "–"}</div>
                    {r.extra && <div className="mt-0.5 break-all font-mono text-[11px] text-faint">{r.extra}</div>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <EmptyState>No debug steps were recorded for this call.</EmptyState>
      )}
    </Panel>
  )
}

function stepLabel(e: CallEvent): string {
  if (e.kind === "debug") return STEP_LABEL[e.text] ?? e.text
  if (e.kind === "status") return e.text.charAt(0).toUpperCase() + e.text.slice(1)
  if (e.kind === "log") return "Log"
  return e.text.replaceAll("_", " ")
}

function toneOf(e: CallEvent): "neutral" | "danger" {
  if (e.kind === "status" && e.text === "failed") return "danger"
  if (e.kind === "debug" && (e.text === "poll_error" || e.text === "call_request_failed" || (e.text === "dial_result" && e.data?.ok === false))) return "danger"
  if (e.data?.level === "error") return "danger"
  return "neutral"
}

/** Everything except detail/t as compact key=value pairs; attribute maps are flattened. */
function formatData(data: Record<string, unknown>): string {
  const parts: string[] = []
  for (const [k, v] of Object.entries(data ?? {})) {
    if (k === "detail" || k === "t" || k === "level" || v === null || v === undefined || v === "") continue
    if (k === "attributes" && typeof v === "object") {
      for (const [ak, av] of Object.entries(v as Record<string, unknown>)) parts.push(`${ak}=${String(av)}`)
    } else if (typeof v === "object") {
      if (Object.keys(v as object).length) parts.push(`${k}=${JSON.stringify(v)}`)
    } else {
      parts.push(`${k}=${String(v)}`)
    }
  }
  return parts.join("  ")
}
