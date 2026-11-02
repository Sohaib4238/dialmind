"use client"

import { BellRing, PhoneForwarded, PhoneOff, VolumeX } from "lucide-react"
import { AnimatePresence, motion } from "motion/react"
import { useCallback, useEffect, useRef, useState } from "react"
import { toast } from "sonner"

import { Button } from "@/components/ui/button"
import { api, ApiError } from "@/lib/api"
import { formatPhone } from "@/lib/format"
import { useLiveStatus, useLiveStream } from "@/lib/live"
import type { SupervisorRequest } from "@/lib/types"
import { cn } from "@/lib/utils"

const EASE = [0.22, 1, 0.36, 1] as const
const CHIME_EVERY_MS = 3000
const POLL_FALLBACK_MS = 2000

// ---- sound: a short two-tone chime (Web Audio, no file). Browsers only allow sound after the
// page has been clicked once, so the context is unlocked on the first click / key press.
let ctx: AudioContext | null = null

function audio(): AudioContext | null {
  if (typeof window === "undefined") return null
  if (!ctx) {
    const AC = window.AudioContext || (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext
    if (!AC) return null
    ctx = new AC()
  }
  return ctx
}

/** Play the pop-up sound once (the Supervisor page's "Test sound" button; it's a click, so sound is allowed). */
export function testChime() {
  audio()?.resume().then(chime).catch(() => {})
}

function chime() {
  const a = audio()
  if (!a || a.state !== "running") return
  const now = a.currentTime
  ;[880, 1320].forEach((freq, i) => {
    const osc = a.createOscillator()
    const gain = a.createGain()
    osc.type = "sine"
    osc.frequency.value = freq
    const t = now + i * 0.18
    gain.gain.setValueAtTime(0.0001, t)
    gain.gain.exponentialRampToValueAtTime(0.25, t + 0.02)
    gain.gain.exponentialRampToValueAtTime(0.0001, t + 0.35)
    osc.connect(gain).connect(a.destination)
    osc.start(t)
    osc.stop(t + 0.4)
  })
}

// Installed once at load, so any click counts (also the click on "Sign in" before the pop-up exists).
const soundListeners = new Set<() => void>()
if (typeof window !== "undefined") {
  const unlock = () => {
    const a = audio()
    a?.resume().then(() => soundListeners.forEach((l) => l())).catch(() => {})
  }
  window.addEventListener("pointerdown", unlock, true)
  window.addEventListener("keydown", unlock, true)
}

function useSoundUnlocked(): boolean {
  const [ok, setOk] = useState(false)
  useEffect(() => {
    const check = () => setOk(ctx?.state === "running")
    check()
    soundListeners.add(check)
    return () => {
      soundListeners.delete(check)
    }
  }, [])
  return ok
}

/** Pop-up on every open dashboard (this computer and the supervisor's laptop) when the AI asks for the supervisor. */
export function SupervisorPopup() {
  const [requests, setRequests] = useState<Record<number, SupervisorRequest>>({})
  const [busy, setBusy] = useState(false)
  const [now, setNow] = useState(() => Date.now())
  const soundOk = useSoundUnlocked()
  const answeredHere = useRef<Set<number>>(new Set())

  const load = useCallback(() => {
    api.supervisorRequests()
      .then((list) => setRequests(Object.fromEntries(list.map((r) => [r.call_id, r]))))
      .catch(() => {})
  }, [])
  useEffect(load, [load])

  // Safety net: live stream down for 5+ s -> ask every 2 s, so the pop-up still appears (and closes).
  const live = useLiveStatus()
  useEffect(() => {
    if (live !== "fallback") return
    load()
    const id = setInterval(load, POLL_FALLBACK_MS)
    return () => clearInterval(id)
  }, [live, load])
  useEffect(() => {
    if (live === "connected") load() // catch up on anything missed while disconnected
  }, [live, load])

  useLiveStream((msg) => {
    if (msg.type === "supervisor_request") {
      const r = msg.request
      setRequests((prev) => {
        const next = { ...prev }
        if (r.status === "waiting") next[r.call_id] = r
        else {
          if (prev[r.call_id] && !answeredHere.current.has(r.call_id)) {
            if (r.status === "accepted") toast.success(`${r.lead.lead_name || "The lead"}: accepted on another screen`)
            else if (r.status === "timeout") toast.message(`${r.lead.lead_name || "The lead"}: no answer in time, the AI is booking a callback`)
          }
          delete next[r.call_id]
        }
        return next
      })
    } else if (msg.type === "event" && msg.event.kind === "transcript") {
      const e = msg.event
      setRequests((prev) => prev[e.call_id]
        ? { ...prev, [e.call_id]: { ...prev[e.call_id], transcript: [...prev[e.call_id].transcript, { role: e.role, text: e.text }] } }
        : prev)
    } else if (msg.type === "call" && !["dialing", "ringing", "answered"].includes(msg.call.status)) {
      setRequests((prev) => {
        if (!prev[msg.call.id]) return prev
        const next = { ...prev }
        delete next[msg.call.id]
        return next
      })
    }
  })

  const current = Object.values(requests).sort((a, b) => (a.requested_at ?? "").localeCompare(b.requested_at ?? ""))[0]

  // chime + countdown + tab title while a request is open
  useEffect(() => {
    if (!current) return
    chime()
    const sound = setInterval(chime, CHIME_EVERY_MS)
    const tick = setInterval(() => setNow(Date.now()), 250)
    const title = document.title
    let flip = false
    const blink = setInterval(() => {
      flip = !flip
      document.title = flip ? "Transfer request" : title
    }, 1000)
    return () => {
      clearInterval(sound)
      clearInterval(tick)
      clearInterval(blink)
      document.title = title
    }
  }, [current?.call_id]) // eslint-disable-line react-hooks/exhaustive-deps

  const answer = async (accept: boolean) => {
    if (!current) return
    setBusy(true)
    answeredHere.current.add(current.call_id)
    try {
      await api.answerRequest(current.call_id, accept)
      if (accept) toast.success("Accepted. The AI is connecting the call to your phone now.", { duration: 8000 })
      else toast.message("Marked busy. The AI will ask when you can call them back.")
    } catch (e) {
      toast.error(e instanceof ApiError ? e.message : "Couldn't send your answer")
      load()
    } finally {
      setBusy(false)
    }
  }

  const left = current?.deadline ? Math.max(0, Math.ceil((Date.parse(current.deadline) - now) / 1000)) : null

  return (
    <AnimatePresence>
      {current && (
        <motion.div
          key="sup-backdrop"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.15 }}
          className="fixed inset-0 z-50 grid place-items-center bg-black/60 px-4"
          role="dialog"
          aria-modal="true"
          aria-labelledby="sup-title"
        >
          <motion.div
            key={current.call_id}
            initial={{ opacity: 0, y: 8, scale: 0.98 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: 4 }}
            transition={{ duration: 0.2, ease: EASE }}
            className="flex max-h-[90dvh] w-full max-w-[560px] flex-col overflow-hidden rounded-lg border border-line-strong bg-surface shadow-lg"
          >
            <div className="flex items-start justify-between gap-4 border-b border-border px-5 py-4">
              <div className="flex items-start gap-3">
                <div className="mt-0.5 grid size-8 place-items-center rounded-md bg-signal-soft">
                  <BellRing className="size-4 text-signal" strokeWidth={1.75} />
                </div>
                <div>
                  <h2 id="sup-title" className="text-[15px] font-semibold tracking-[-0.01em]">
                    Interested lead: {current.lead.lead_name || formatPhone(current.lead.phone)}
                  </h2>
                  <p className="text-[12px] text-muted-foreground">The AI is keeping them on the line. Take the call?</p>
                </div>
              </div>
              {left != null && (
                <div className={cn("font-mono text-[20px] leading-7 tnum", left <= 5 ? "text-danger" : "text-foreground")} aria-label={`${left} seconds left`}>
                  {left}s
                </div>
              )}
            </div>

            <div className="min-h-0 flex-1 space-y-4 overflow-y-auto px-5 py-4 text-[13px]">
              <dl className="grid grid-cols-[110px_1fr] gap-x-3 gap-y-1.5">
                {([
                  ["Business", current.lead.business_name],
                  ["Service", current.lead.service_required],
                  ["Job", current.lead.job_description],
                  ["About them", current.lead.business_description],
                  ["Location", current.lead.location],
                  ["Their time", current.lead.lead_local_time ? `${current.lead.lead_local_time} (${current.lead.lead_timezone})` : ""],
                  ["Phone", current.lead.phone ? formatPhone(current.lead.phone) : ""],
                ] as const).filter(([, v]) => v).map(([k, v]) => (
                  <div key={k} className="contents">
                    <dt className="text-faint">{k}</dt>
                    <dd className="break-words text-foreground">{v}</dd>
                  </div>
                ))}
              </dl>

              <div>
                <div className="text-[12px] font-medium text-muted-foreground">Summary so far</div>
                <p className="mt-1 leading-5">{current.summary || "No summary given."}</p>
              </div>

              <div>
                <div className="text-[12px] font-medium text-muted-foreground">Transcript</div>
                <ol className="mt-1.5 max-h-[180px] space-y-1 overflow-y-auto rounded-md border border-border bg-surface-2 p-3 text-[12px] leading-5">
                  {current.transcript.length === 0 && <li className="text-faint">Nothing yet.</li>}
                  {current.transcript.map((t, i) => (
                    <li key={i}>
                      <span className={cn("font-medium", t.role === "agent" ? "text-signal" : "text-foreground")}>
                        {t.role === "agent" ? "AI" : "Lead"}:
                      </span>{" "}
                      <span className="text-muted-foreground">{t.text}</span>
                    </li>
                  ))}
                </ol>
              </div>

              {!soundOk && (
                <p className="flex items-center gap-1.5 text-[12px] text-warning">
                  <VolumeX className="size-3.5" strokeWidth={1.5} />
                  Sound is blocked until you click anywhere on this page once.
                </p>
              )}
            </div>

            <div className="flex gap-2 border-t border-border px-5 py-4">
              <Button className="flex-1" disabled={busy} onClick={() => answer(true)}>
                <PhoneForwarded strokeWidth={1.5} />
                Accept: ring my phone
              </Button>
              <Button className="flex-1" variant="outline" disabled={busy} onClick={() => answer(false)}>
                <PhoneOff strokeWidth={1.5} />
                Busy
              </Button>
            </div>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  )
}
