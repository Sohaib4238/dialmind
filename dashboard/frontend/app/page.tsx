"use client"

import { ArrowUpRight, FlaskConical, Phone, PhoneOff } from "lucide-react"
import { AnimatePresence, motion } from "motion/react"
import Link from "next/link"
import { useCallback, useEffect, useRef, useState } from "react"
import { toast } from "sonner"

import { EmptyState, Kbd, PageHeader, Panel, PanelHeader, SkeletonLine } from "@/components/common"
import { StatusPill } from "@/components/status-pill"
import { Transcript } from "@/components/transcript"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Waveform } from "@/components/waveform"
import { api, ApiError } from "@/lib/api"
import { E164, formatDuration, formatPhone, isActive } from "@/lib/format"
import { useLiveStream } from "@/lib/live"
import type { AppConfig, Call, CallEvent } from "@/lib/types"
import { cn } from "@/lib/utils"

function useElapsed(since: string | null, running: boolean, endedAt: string | null) {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!running) return
    const id = setInterval(() => setNow(Date.now()), 250)
    return () => clearInterval(id)
  }, [running])
  if (!since) return null
  const end = running ? now : endedAt ? new Date(endedAt).getTime() : now
  return Math.max(0, (end - new Date(since).getTime()) / 1000)
}

export default function CallPage() {
  const [config, setConfig] = useState<AppConfig | null>(null)
  const [number, setNumber] = useState("")
  const [call, setCall] = useState<Call | null>(null)
  const [events, setEvents] = useState<CallEvent[]>([])
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [hangingUp, setHangingUp] = useState(false)
  const [speaking, setSpeaking] = useState<{ agent: boolean; callee: boolean }>({ agent: false, callee: false })
  const callRef = useRef<Call | null>(null)
  callRef.current = call

  // Initial load: config (default number) + resume an in-progress call, if any.
  useEffect(() => {
    Promise.all([api.config(), api.activeCall()])
      .then(async ([cfg, active]) => {
        setConfig(cfg)
        setNumber(cfg.default_number)
        if (active.call) {
          const full = await api.call(active.call.id)
          setCall(full.call)
          setEvents(full.events)
        }
      })
      .catch((e) => toast.error(e instanceof ApiError ? e.message : "Couldn't load the call page"))
      .finally(() => setLoading(false))
  }, [])

  useLiveStream((msg) => {
    const current = callRef.current
    if (msg.type === "call") {
      // Follow the current call, or switch to a new call that just started (e.g. from the terminal).
      if (current?.id === msg.call.id) setCall(msg.call)
      else if (isActive(msg.call.status) && (!current || !isActive(current.status))) {
        setCall(msg.call)
        setEvents([])
        setSpeaking({ agent: false, callee: false })
      }
      return
    }
    if (msg.type !== "event") return
    const ev = msg.event
    if (!current || ev.call_id !== current.id) return
    setEvents((prev) => (prev.some((p) => p.id === ev.id) ? prev : [...prev, ev]))
    if (ev.kind === "state" && (ev.role === "agent" || ev.role === "callee")) {
      setSpeaking((s) => ({ ...s, [ev.role as "agent" | "callee"]: ev.text === "speaking" }))
    }
  })

  const active = call ? isActive(call.status) : false
  const live = call?.status === "answered"
  const elapsed = useElapsed(call?.answered_at ?? null, live, call?.ended_at ?? null)
  const speaker = live ? (speaking.agent ? "agent" : speaking.callee ? "callee" : null) : null
  const numberValid = E164.test(number.trim())

  const start = useCallback(
    async (simulate = false) => {
      if (busy || (callRef.current && isActive(callRef.current.status))) return
      if (!simulate && !E164.test(number.trim())) {
        toast.error("Enter the number in E.164 format, like +15551234567")
        return
      }
      setBusy(true)
      try {
        const c = simulate ? await api.simulate(number.trim()) : await api.startCall(number.trim())
        setCall(c)
        setEvents([])
        setSpeaking({ agent: false, callee: false })
      } catch (e) {
        toast.error(e instanceof ApiError ? e.message : "Couldn't start the call")
      } finally {
        setBusy(false)
      }
    },
    [busy, number],
  )

  const hangUp = useCallback(async (force = false) => {
    const c = callRef.current
    if (!c || !isActive(c.status) || hangingUp) return
    setHangingUp(true)
    try {
      await api.hangUp(c.id, force) // resolves once Cartesia confirms the call is over
      toast.success("Call ended")
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        // transferred to the supervisor: hanging up could cut him and the lead off
        toast.warning(e.message, { duration: 15000, action: { label: "Hang up anyway", onClick: () => hangUp(true) } })
      } else {
        toast.error(e instanceof ApiError ? e.message : "Couldn't hang up", { duration: 12000 })
      }
    } finally {
      setHangingUp(false)
    }
  }, [hangingUp])

  // Ctrl/Cmd+Enter = call, Esc = hang up.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
        e.preventDefault()
        start(false)
      } else if (e.key === "Escape" && callRef.current && isActive(callRef.current.status)) {
        e.preventDefault()
        hangUp()
      }
    }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  }, [start, hangUp])

  return (
    <>
      <PageHeader
        title="Call"
        description="Place a test call and follow the conversation live."
        actions={
          config?.dev ? (
            <Button variant="ghost" size="sm" onClick={() => start(true)} disabled={busy || active}>
              <FlaskConical strokeWidth={1.5} />
              Simulate call
            </Button>
          ) : null
        }
      />

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-12">
        {/* Left: controls + call state */}
        <div className="flex flex-col gap-6 lg:col-span-5">
          <Panel>
            <PanelHeader title="New call" />
            <div className="space-y-4 p-4">
              <div className="space-y-1.5">
                <label htmlFor="number" className="text-[12px] font-medium text-muted-foreground">
                  Phone number
                </label>
                {loading ? (
                  <SkeletonLine className="h-9 w-full rounded-md" />
                ) : (
                  <Input
                    id="number"
                    value={number}
                    onChange={(e) => setNumber(e.target.value)}
                    placeholder="+15551234567"
                    spellCheck={false}
                    autoComplete="off"
                    disabled={active}
                    aria-invalid={number.length > 0 && !numberValid}
                    className="font-mono tnum"
                  />
                )}
                <p className={cn("text-[12px]", number && !numberValid ? "text-danger" : "text-faint")}>
                  {number && !numberValid ? "Use E.164 format: + then country code and number." : "E.164 format. Defaults to TEST_CALL_NUMBER."}
                </p>
              </div>

              <div className="flex items-center gap-2">
                <AnimatePresence mode="popLayout" initial={false}>
                  {active ? (
                    <motion.div key="hang" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}>
                      <Button variant="destructive" onClick={() => hangUp()} disabled={hangingUp}>
                        <PhoneOff strokeWidth={1.5} />
                        {hangingUp ? "Hanging up…" : "Hang up"}
                        <Kbd>Esc</Kbd>
                      </Button>
                    </motion.div>
                  ) : (
                    <motion.div key="call" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}>
                      <Button onClick={() => start(false)} disabled={busy || loading || !numberValid}>
                        <Phone strokeWidth={1.75} />
                        {busy ? "Starting…" : "Call"}
                        <span className="ml-1 flex gap-0.5 opacity-70">
                          <Kbd>Ctrl</Kbd>
                          <Kbd>↵</Kbd>
                        </span>
                      </Button>
                    </motion.div>
                  )}
                </AnimatePresence>
              </div>
            </div>
          </Panel>

          <Panel>
            <PanelHeader title="Status" right={<StatusPill status={call?.status ?? "idle"} />} />
            <div className="space-y-5 p-4">
              <div className="flex items-end justify-between gap-4">
                <div>
                  <div className="text-[12px] text-faint">Talk time</div>
                  <div className="font-mono text-[28px] leading-[34px] font-medium tracking-[-0.02em] tnum">
                    {formatDuration(elapsed ?? (call ? 0 : null))}
                  </div>
                </div>
                <Waveform live={live} speaker={speaker} />
              </div>
              <dl className="grid grid-cols-[96px_1fr] gap-y-2 text-[13px]">
                <dt className="text-faint">Number</dt>
                <dd className="font-mono tnum">{call ? formatPhone(call.number) : "–"}</dd>
                <dt className="text-faint">Detail</dt>
                <dd className={cn(call?.status === "failed" ? "text-danger" : call && lineWarning(events, call) ? "text-warning" : "text-muted-foreground")}>
                  {call ? lastDetail(events, call) : "No call yet"}
                </dd>
                {call?.simulated && (
                  <>
                    <dt className="text-faint">Mode</dt>
                    <dd className="text-muted-foreground">Simulated (no real call)</dd>
                  </>
                )}
              </dl>
              {call && !active && (
                <Link
                  href={`/history/${call.id}`}
                  className="inline-flex items-center gap-1 text-[12px] text-muted-foreground transition-colors hover:text-foreground"
                >
                  View in history <ArrowUpRight className="size-3.5" strokeWidth={1.5} />
                </Link>
              )}
            </div>
          </Panel>
        </div>

        {/* Right: live transcript */}
        <Panel className="flex min-h-[520px] flex-col lg:col-span-7">
          <PanelHeader
            title="Transcript"
            right={
              live ? (
                <span className="flex items-center gap-1.5 text-[12px] text-signal">
                  <span className="dot-pulse relative size-1.5 rounded-full bg-signal text-signal" />
                  Live
                </span>
              ) : null
            }
          />
          {events.some((e) => e.kind === "transcript") || (call && !active) ? (
            <Transcript events={events} speaking={speaker} className="max-h-[640px] flex-1" />
          ) : (
            <EmptyState>
              {call && active
                ? call.status === "answered"
                  ? "Connected. Waiting for the first words…"
                  : "Waiting for the call to be answered…"
                : "Start a call to see the conversation here, line by line."}
            </EmptyState>
          )}
        </Panel>
      </div>
    </>
  )
}

function lastDetail(events: CallEvent[], call: Call): string {
  for (let i = events.length - 1; i >= 0; i--) {
    const e = events[i]
    // A "line still open" style warning outranks the last status while the call is live.
    if (e.kind === "log" && e.data?.level === "error" && isActive(call.status)) return e.text
    const d = e.kind === "status" ? e.data?.detail : null
    if (typeof d === "string" && d) return d
  }
  return call.reason || "–"
}

function lineWarning(events: CallEvent[], call: Call): boolean {
  return isActive(call.status) && events.some((e) => e.kind === "log" && e.data?.level === "error")
}
