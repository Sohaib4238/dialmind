"use client"

import { AlertTriangle, FlaskConical, PhoneCall, PhoneForwarded } from "lucide-react"
import { AnimatePresence, motion } from "motion/react"
import Link from "next/link"
import { useCallback, useEffect, useState } from "react"
import { toast } from "sonner"

import { Panel, SkeletonLine } from "@/components/common"
import { useIsAdmin } from "@/components/login-gate"
import { Button } from "@/components/ui/button"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { api, ApiError } from "@/lib/api"
import { formatPhone } from "@/lib/format"
import { useLiveStream } from "@/lib/live"
import type { DialerOrder, DialerState } from "@/lib/types"
import { cn } from "@/lib/utils"

const EASE = [0.22, 1, 0.36, 1] as const

export function DialerPanel() {
  const admin = useIsAdmin()
  const [d, setD] = useState<DialerState | null>(null)
  const [busy, setBusy] = useState(false)
  const [confirmOff, setConfirmOff] = useState(false)

  const load = useCallback(() => {
    api.dialer().then(setD).catch((e) => toast.error(e instanceof ApiError ? e.message : "Couldn't load the dialer"))
  }, [])
  useEffect(() => {
    load()
    const id = setInterval(load, 15_000) // keeps "next lead" fresh as calling hours change
    return () => clearInterval(id)
  }, [load])
  useLiveStream((msg) => {
    if (msg.type === "dialer") setD(msg.dialer)
  })

  const toggle = async () => {
    if (!d) return
    setBusy(true)
    try {
      setD(d.state === "on" ? await api.dialerOff() : await api.dialerOn())
    } catch (e) {
      toast.error(e instanceof ApiError ? e.message : "Couldn't switch the dialer")
    } finally {
      setBusy(false)
    }
  }

  const setTest = async (on: boolean) => {
    setBusy(true)
    try {
      setD(await api.setTestMode(on, !on))
      setConfirmOff(false)
      toast[on ? "success" : "warning"](on ? "Test mode on: every call goes to the test number" : "Test mode off: leads are called at their own numbers")
    } catch (e) {
      toast.error(e instanceof ApiError ? e.message : "Couldn't change test mode")
    } finally {
      setBusy(false)
    }
  }

  const setOrder = async (order: DialerOrder) => {
    try {
      setD(await api.setDialerOrder(order))
      toast.success(order === "sheet" ? "New leads are called in sheet order" : "Newest leads are called first")
    } catch (e) {
      toast.error(e instanceof ApiError ? e.message : "Couldn't change the order")
    }
  }

  if (!d) {
    return (
      <Panel className="mb-6 p-4">
        <SkeletonLine className="h-8 w-64" />
      </Panel>
    )
  }

  const on = d.state === "on"
  const paused = d.state === "paused"
  const max = d.limits.max_calls ?? 0

  return (
    <>
      <Panel className="mb-6 overflow-hidden">
        {d.test_mode && (
          <div className="flex flex-wrap items-center gap-2 border-b border-warning/20 bg-warning/10 px-4 py-2 text-[12px] text-warning">
            <FlaskConical className="size-3.5" strokeWidth={1.75} />
            <span className="font-medium">TEST MODE: all calls go to {formatPhone(d.test_number) || "TEST_CALL_NUMBER (not set!)"}</span>
            <span className="text-warning/80">Leads, statuses and history still update on the real lead.</span>
            {admin && (
              <Button variant="ghost" size="xs" className="ml-auto text-warning hover:bg-warning/10 hover:text-warning" disabled={busy || d.state !== "off"}
                title={d.state !== "off" ? "Turn the dialer off first" : undefined} onClick={() => setConfirmOff(true)}>
                Turn off test mode
              </Button>
            )}
          </div>
        )}
        {!d.test_mode && (
          <div className="flex flex-wrap items-center gap-2 border-b border-danger/20 bg-danger-soft px-4 py-2 text-[12px] text-danger">
            <AlertTriangle className="size-3.5" strokeWidth={1.75} />
            <span className="font-medium">LIVE MODE: leads are called at their own numbers.</span>
            {admin && (
              <Button variant="ghost" size="xs" className="ml-auto text-danger hover:bg-danger/10 hover:text-danger" disabled={busy} onClick={() => setTest(true)}>
                Back to test mode
              </Button>
            )}
          </div>
        )}

        {d.alert && (
          <div role="alert" className="flex items-start gap-2 border-b border-danger/30 bg-danger-soft px-4 py-2.5 text-[13px] leading-5 text-danger">
            <AlertTriangle className="mt-0.5 size-4 shrink-0" strokeWidth={1.75} />
            <span><span className="font-semibold">Dialer stopped. </span>{d.alert}</span>
          </div>
        )}

        <div className="grid grid-cols-1 gap-5 p-4 lg:grid-cols-[1fr_minmax(280px,380px)]">
          <div className="min-w-0 space-y-4">
            <div className="flex items-center gap-4">
              <button
                role="switch"
                aria-checked={on}
                aria-label="Auto-dialer"
                disabled={busy || !admin}
                onClick={toggle}
                className={cn(
                  "relative inline-flex h-9 w-[68px] shrink-0 items-center rounded-full border transition-colors duration-[180ms] active:scale-[0.98] disabled:opacity-60",
                  on ? "border-transparent bg-signal" : paused ? "border-warning/40 bg-warning/15" : "border-line-strong bg-surface-2",
                )}
              >
                <motion.span
                  layout
                  transition={{ type: "spring", stiffness: 420, damping: 34 }}
                  className={cn("size-7 rounded-full shadow-sm", on ? "ml-[36px] bg-signal-fg" : "ml-[3px] bg-foreground/80")}
                />
              </button>
              <div className="min-w-0">
                <div className="flex items-center gap-2 text-[15px] font-semibold tracking-[-0.01em]">
                  Auto-dialer
                  <span className={cn("rounded-full px-2 py-0.5 text-[11px] font-medium",
                    on ? "bg-signal-soft text-signal" : paused ? "bg-warning/10 text-warning" : "bg-surface-2 text-muted-foreground")}>
                    {on ? "On" : paused ? "Paused" : "Off"}
                  </span>
                </div>
                <AnimatePresence mode="wait" initial={false}>
                  <motion.div key={statusLine(d)} initial={{ opacity: 0, y: 2 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0 }}
                    transition={{ duration: 0.18, ease: EASE }}
                    className={cn("mt-0.5 truncate text-[13px]", paused ? "text-warning" : "text-muted-foreground")}>
                    {statusLine(d)}
                  </motion.div>
                </AnimatePresence>
              </div>
            </div>

            <dl className="grid grid-cols-2 gap-3 sm:grid-cols-4">
              <Stat label="Calls this session" value={`${d.session_calls} / ${max}`} />
              <Stat label="Failed in a row" value={String(d.failures_in_row)} tone={d.failures_in_row ? "warn" : undefined} />
              <Stat label="Minutes left" value={`~${d.minutes_left}`} tone={d.minutes_left < (d.limits.min_free_minutes ?? 2) + 2 ? "warn" : undefined} />
              <Stat label="Calling hours" value={d.limits.hours ?? "–"} sub="lead's local time" />
            </dl>

            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
              <div className="rounded-lg border border-border p-3">
                <div className="mb-1 flex items-center gap-1.5 text-[12px] text-faint"><PhoneCall className="size-3.5" strokeWidth={1.5} /> Calling now</div>
                {d.current ? (
                  <Link href={`/history/${d.current.call_id}`} className="block truncate text-[13px] font-medium hover:underline">
                    {d.current.name || formatPhone(d.current.phone)}
                    <span className="ml-2 font-mono text-[12px] font-normal text-muted-foreground tnum">
                      {formatPhone(d.current.phone)} · try {d.current.attempt}
                    </span>
                  </Link>
                ) : (
                  <div className="text-[13px] text-faint">Nobody</div>
                )}
              </div>
              <div className="rounded-lg border border-border p-3">
                <div className="mb-1 flex items-center gap-1.5 text-[12px] text-faint">
                  <PhoneForwarded className="size-3.5" strokeWidth={1.5} /> Next in the queue
                  {d.order && d.orders && (admin ? (
                    <Select value={d.order} onValueChange={(x) => setOrder(x as DialerOrder)}>
                      <SelectTrigger size="sm" aria-label="Order of new leads"
                        className="ml-auto h-6 gap-1 border-transparent bg-transparent px-1.5 text-[12px] text-muted-foreground hover:bg-surface-2 data-[size=sm]:h-6">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent align="end">
                        {(Object.keys(d.orders) as DialerOrder[]).map((k) => <SelectItem key={k} value={k}>{d.orders![k]}</SelectItem>)}
                      </SelectContent>
                    </Select>
                  ) : (
                    <span className="ml-auto">{d.orders[d.order]}</span>
                  ))}
                </div>
                {d.next ? (
                  <Link href={`/leads/${d.next.lead_id}`} className="block truncate text-[13px] font-medium hover:underline">
                    {d.next.name || formatPhone(d.next.phone)}
                    <span className="ml-2 text-[12px] font-normal text-muted-foreground">{d.next.local_time}</span>
                  </Link>
                ) : (
                  <div className="truncate text-[13px] text-faint">{d.current ? "After this call" : queueLine(d)}</div>
                )}
              </div>
            </div>
          </div>

          <div className="min-w-0 rounded-lg border border-border">
            <div className="flex h-9 items-center border-b border-border px-3 text-[12px] font-medium text-muted-foreground">Dialer log</div>
            {d.log.length === 0 ? (
              <div className="px-3 py-6 text-center text-[12px] text-faint">Nothing yet. Turn the dialer on to start.</div>
            ) : (
              <ol className="max-h-[188px] space-y-1.5 overflow-y-auto px-3 py-2.5 text-[12px]">
                <AnimatePresence initial={false}>
                  {[...d.log].reverse().map((e) => (
                    <motion.li key={`${e.ts}-${e.text}`} layout="position" initial={{ opacity: 0, y: -4 }} animate={{ opacity: 1, y: 0 }}
                      transition={{ duration: 0.18, ease: EASE }} className="grid grid-cols-[58px_1fr] gap-2">
                      <span className="font-mono text-faint tnum">{new Date(e.ts * 1000).toLocaleTimeString([], { hourCycle: "h23" })}</span>
                      <span className={cn(e.level === "error" ? "text-danger" : e.level === "warn" ? "text-warning" : "text-muted-foreground")}>{e.text}</span>
                    </motion.li>
                  ))}
                </AnimatePresence>
              </ol>
            )}
          </div>
        </div>
      </Panel>

      <AnimatePresence>
        {confirmOff && (
          <motion.div className="fixed inset-0 z-50 grid place-items-center bg-black/60 p-4" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}
            onClick={() => setConfirmOff(false)}>
            <motion.div role="dialog" aria-modal="true" aria-labelledby="tm-title" onClick={(e) => e.stopPropagation()}
              initial={{ opacity: 0, y: 8, scale: 0.98 }} animate={{ opacity: 1, y: 0, scale: 1 }} exit={{ opacity: 0, y: 4 }}
              transition={{ duration: 0.18, ease: EASE }}
              className="w-full max-w-[440px] rounded-lg border border-line-strong bg-surface p-5 shadow-2xl">
              <div className="flex items-center gap-2 text-[15px] font-semibold" id="tm-title">
                <AlertTriangle className="size-4 text-danger" strokeWidth={1.75} /> Turn off test mode?
              </div>
              <p className="mt-2 text-[13px] leading-5 text-muted-foreground">
                The dialer will call every lead <span className="text-foreground">at their own phone number</span>. Only do this if you have consent and legal review for those calls.
              </p>
              <div className="mt-5 flex justify-end gap-2">
                <Button variant="ghost" onClick={() => setConfirmOff(false)} autoFocus>Keep test mode</Button>
                <Button variant="destructive" disabled={busy} onClick={() => setTest(false)}>Turn off test mode</Button>
              </div>
            </motion.div>
          </motion.div>
        )}
      </AnimatePresence>
    </>
  )
}

function Stat({ label, value, sub, tone }: { label: string; value: string; sub?: string; tone?: "warn" }) {
  return (
    <div className="rounded-lg border border-border px-3 py-2">
      <div className="text-[11px] text-faint">{label}</div>
      <div className={cn("font-mono text-[15px] font-medium tnum", tone === "warn" && "text-warning")}>{value}</div>
      {sub && <div className="text-[11px] text-faint">{sub}</div>}
    </div>
  )
}

function statusLine(d: DialerState): string {
  if (d.state === "paused") return d.reason
  if (d.state === "off") return d.reason && d.reason !== "Turned off from the dashboard" ? `Stopped: ${d.reason}` : "Not calling. Turn it on to call leads one at a time."
  if (d.current) return `Calling ${d.current.name || formatPhone(d.current.phone)}…`
  return d.wait_detail || "Starting…"
}

function queueLine(d: DialerState): string {
  const q = d.queue
  if (!q || q.new === undefined) return "–"
  if ((q.outside_hours ?? 0) > 0) return `${q.outside_hours} lead(s) outside calling hours`
  if ((q.retries_waiting ?? 0) > 0) return `${q.retries_waiting} retr${q.retries_waiting === 1 ? "y" : "ies"} not due yet`
  if ((q.callbacks_waiting ?? 0) > 0) return `${q.callbacks_waiting} callback${q.callbacks_waiting === 1 ? "" : "s"} scheduled for later`
  if ((q.unknown_tz ?? 0) > 0) return `${q.unknown_tz} lead(s) with no known time zone`
  return "No leads left to call"
}

