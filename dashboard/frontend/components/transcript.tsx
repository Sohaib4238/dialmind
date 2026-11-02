"use client"

import { AnimatePresence, motion } from "motion/react"
import { useEffect, useMemo, useRef } from "react"

import { formatClock, formatSeconds } from "@/lib/format"
import type { CallEvent } from "@/lib/types"
import { cn } from "@/lib/utils"

interface Line {
  id: number
  ts: string
  role: "agent" | "callee"
  text: string
  timing?: Record<string, unknown>
}

interface Note {
  id: number
  ts: string
  text: string
  tone: "neutral" | "danger"
}

type Item = ({ type: "line" } & Line) | ({ type: "note" } & Note)

/** Build transcript lines (with their timing attached) and inline system notes from raw events. */
export function buildItems(events: CallEvent[]): Item[] {
  const items: Item[] = []
  for (const e of events) {
    if (e.kind === "transcript" && (e.role === "agent" || e.role === "callee")) {
      items.push({ type: "line", id: e.id, ts: e.ts, role: e.role, text: e.text })
    } else if (e.kind === "timing") {
      for (let i = items.length - 1; i >= 0; i--) {
        const it = items[i]
        if (it.type === "line" && it.role === e.role && !it.timing) {
          it.timing = e.data
          break
        }
      }
    } else if (e.kind === "log") {
      items.push({ type: "note", id: e.id, ts: e.ts, text: e.text, tone: e.data?.level === "error" ? "danger" : "neutral" })
    } else if (e.kind === "status" && (e.text === "failed" || e.text === "ended")) {
      const detail = typeof e.data?.detail === "string" ? e.data.detail : ""
      items.push({ type: "note", id: e.id, ts: e.ts, text: detail || e.text, tone: e.text === "failed" ? "danger" : "neutral" })
    }
  }
  return items
}

function timingSummary(role: "agent" | "callee", t?: Record<string, unknown>): string | null {
  if (!t) return null
  const parts =
    role === "agent"
      ? [
          ["reply", t.reply_gap],
          ["llm", t.llm_first_token],
          ["tts", t.tts_first_audio],
        ]
      : [
          ["stt", t.stt_final],
          ["end of turn", t.end_of_turn],
        ]
  const out = parts.map(([k, v]) => (formatSeconds(v) ? `${k} ${formatSeconds(v)}` : null)).filter(Boolean)
  return out.length ? out.join(" · ") : null
}

export function Transcript({
  events,
  animate = true,
  speaking,
  className,
}: {
  events: CallEvent[]
  animate?: boolean
  speaking?: "agent" | "callee" | null
  className?: string
}) {
  const items = useMemo(() => buildItems(events), [events])
  const scroller = useRef<HTMLDivElement>(null)
  const stick = useRef(true)

  // Live view: auto-scroll to the newest line unless the user scrolled up.
  // History view (animate=false): start at the top.
  useEffect(() => {
    const el = scroller.current
    if (el && animate && stick.current) el.scrollTo({ top: el.scrollHeight, behavior: "smooth" })
  }, [items.length, animate])

  return (
    <div
      ref={scroller}
      onScroll={(e) => {
        const el = e.currentTarget
        stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 48
      }}
      className={cn("overflow-y-auto", className)}
    >
      <ol className="flex flex-col gap-5 px-4 py-5">
        <AnimatePresence initial={false}>
          {items.map((it) =>
            it.type === "line" ? (
              <motion.li
                key={it.id}
                layout="position"
                initial={animate ? { opacity: 0, y: 8 } : false}
                animate={{ opacity: 1, y: 0 }}
                transition={{ duration: 0.18, ease: [0.22, 1, 0.36, 1] }}
                className="grid grid-cols-[72px_1fr] gap-4"
              >
                <div className="pt-px">
                  <div className="flex items-center gap-1.5 text-[12px] font-medium">
                    <span className={cn("size-1.5 rounded-full", it.role === "agent" ? "bg-signal" : "bg-muted-foreground")} />
                    <span className={it.role === "agent" ? "text-foreground" : "text-muted-foreground"}>
                      {it.role === "agent" ? "Agent" : "Callee"}
                    </span>
                  </div>
                  <div className="mt-0.5 pl-3 font-mono text-[11px] text-faint tnum">{formatClock(it.ts)}</div>
                </div>
                <div>
                  <p className="text-[14px] leading-[22px] text-foreground">{it.text}</p>
                  {timingSummary(it.role, it.timing) && (
                    <p className="mt-1 font-mono text-[11px] text-faint tnum">{timingSummary(it.role, it.timing)}</p>
                  )}
                </div>
              </motion.li>
            ) : (
              <motion.li
                key={it.id}
                layout="position"
                initial={animate ? { opacity: 0 } : false}
                animate={{ opacity: 1 }}
                className={cn(
                  "grid grid-cols-[72px_1fr] gap-4 text-[12px]",
                  it.tone === "danger" ? "text-danger" : "text-faint",
                )}
              >
                <span className="pl-3 font-mono text-[11px] tnum">{formatClock(it.ts)}</span>
                <span>{it.text}</span>
              </motion.li>
            ),
          )}
        </AnimatePresence>
        {speaking && (
          <li className="grid grid-cols-[72px_1fr] gap-4 text-[12px] text-faint">
            <span />
            <span>{speaking === "agent" ? "Agent is speaking…" : "Callee is speaking…"}</span>
          </li>
        )}
      </ol>
    </div>
  )
}
